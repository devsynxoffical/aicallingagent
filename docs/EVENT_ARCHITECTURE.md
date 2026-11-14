# Events and background work

What is built. Findings referred to (B-1, D-10, ...) are in
`ENTERPRISE_BACKEND_AUDIT.md`.

## Principles

- **The database is the log.** An event or a job is a row written in the same
  transaction as the change that causes it (the transactional outbox pattern). There
  is no broker; nothing depends on a network message arriving.
- **At least once, made safe.** A job can run more than once (a worker can stop after
  doing the work and before recording it). Every handler is written so that running
  it twice has the effect of running it once.
- **Order is not guaranteed.** A consumer reads the current state of the thing an
  event is about; the event says what to look at, not what the state is.
- **Call-related work never waits behind analysis.**

## Events (`callagent/events.py`, table `events`)

Each event has an id, a type and a version of that type's payload, the organization,
what it is about (`subject`, `subject_id`), the campaign and the call it belongs to,
a payload and the time. `call_run_id` is the correlation id of a call: its events,
jobs and usage record all carry it. `GET /api/events?call_run_id=N` returns the trail
of one call for the caller's organization.

| Type | Recorded when | Key (one event per key) |
| --- | --- | --- |
| `CallInitiated` | a lead is reserved and its call created | `call:{id}:initiated` |
| `CallAnswered` | the call's audio stream connects | `call:{id}:answered` |
| `CallCompleted`, `CallFailed` | the provider reports the final status | `call:{id}:ended` |
| `UsageRecorded` | a call's first ledger line is written (by the provider's final report, or by the engine when it ends the call) | `usage:call:{id}:call` |
| `CallTransferred` | the provider accepted handing the call to a person | `call:{id}:transferred` |
| `CallFinalized` | the outcome is decided and the lead updated | `call:{id}:finalized` |
| `AppointmentBooked` | a meeting is booked on a call | `appointment:{id}:booked` |
| `CampaignPaused` | a person pauses a campaign | per pause |
| `CampaignStopped` | the dialer stops a campaign (budget, schedule, a failing provider, done) | per stop |
| `PlaybookApproved` | a version goes live | per approval |
| `DocumentUploaded`, `DocumentProcessed` | a source is accepted; it has been read | per document, per reading |

`CampaignStarted` is defined and not yet emitted. Corrections to the ledger and AI
usage lines do not produce events; they are in the ledger and the audit trail.

**Duplicates.** The key identifies the fact. Recording it again (a repeated webhook, a
retried job) adds nothing and starts no consumer a second time. Tested with the
provider's final report delivered three times: one `CallCompleted`, one
`UsageRecorded`.

**Consumers.** `events.subscribe(type, consumer, kind=...)` makes a job of that kind
run for every event of the type. One job per consumer is queued in the event's own
transaction, with the key `event:{event id}:{consumer}`. Each consumer therefore has
its own retries, backoff and dead letter; a consumer that fails does not delay
another. Tested: of two consumers of one event, one fails and is retried later while
the other has already completed, and is not run again.

**Atomicity.** If the transaction that recorded an event rolls back, the event and
its consumers' jobs do not exist. Tested on both databases. (This test found that on
SQLite a job written as the first change of a transaction survived a rollback; see
"What the tests found".)

What is not yet routed through subscribers: the booking notification and the call
review are still queued directly by the code that books or finalizes. They are
idempotent jobs written in the same transaction, which is the same guarantee; moving
them to subscribers is a refactoring without a behaviour change.

## Jobs (`callagent/jobs.py`, tables `jobs`, `job_attempts`)

A job has: id, organization, kind, idempotency key (unique), payload, queue,
priority, status (`queued`, `running`, `done`, `dead`), attempts and the maximum,
a timeout, the time it becomes due, the worker holding it and until when (a lease),
the last error, the call it belongs to, and a row per attempt with its outcome.

**Queues.** Each queue has its own worker slots in a process:

| Queue | Slots | Work |
| --- | --- | --- |
| `calls` | 4 | finishing interrupted calls, booking notifications |
| `analysis` | 2 | call reviews |
| `knowledge` | 2 | reading documents and websites, building drafts |
| `default` | 2 | everything else, and any job whose queue no pool is configured for |

A full queue does not hold up the others: the worker never waits for a slot in one
queue while another has work. Tested: with the analysis queue saturated and backed
up, a call-related job runs at once.

**Order within a queue.** Higher priority first; then the organization with the
fewest jobs running in that queue, so one customer's backlog cannot starve another;
then the job that has waited longest.

**"Run it now".** After queueing, a job is started immediately if its queue has a
free slot. If not, it waits its turn; it never exceeds the queue's slots. (Before,
every call's review started at once regardless of how many were running.)

**Failure.** An attempt that raises or times out is retried with exponential backoff
(30 s doubling to an hour, plus up to 20% jitter) until the attempt limit, then the
job is `dead`: kept with its error, shown in the console, retryable by hand. A worker
that stops simply stops renewing its lease and another takes the job over.

**Load.** `jobs.queue_stats` gives, per queue, the jobs waiting, running, and how long
the oldest due job has waited; the deployment's operator sees it in `GET /api/jobs`.
A growing wait in a queue is that queue falling behind.

Not built: cancelling a running job (handlers that call the AI model run in threads,
which cannot be interrupted: C-11), pruning of finished jobs (D-9), and a separate
worker process. The slots are per process, so several processes multiply them.

## Two thread pools (B-1)

Blocking work runs in threads. There are now two pools:

- **db** (`DB_THREADS`, default 16): database work on the request and call path:
  provider webhooks, media-socket authorization, in-call tools, transcript saves,
  API reads.
- **background** (`BACKGROUND_THREADS`, default 4): model calls, website crawls,
  document parsing, lead-sheet imports, the coach.

`run_db(fn)` uses the first; `run_db(fn, slow=True)` and `run_blocking(fn)` the
second. Tested: with the background pool completely blocked and more work queued
behind it, twenty call-path database operations complete immediately.

## A call that is cut off (B-2)

When a call's audio stream connects, the same transaction queues a `call_finalize`
job due after the longest a call can last plus two minutes. The normal end of the
call completes that job, so it usually never runs. If the process stops during the
call, or the normal ending fails, the job gives the call its outcome from the
transcript saved so far, marks it ended, releases the lead and queues the review. It
works whatever state the campaign is in. If a normal ending is under way it backs off
and looks again later, so the work is not done twice.

Tested: a connected call abandoned mid-way in a running and in a paused campaign;
delivered twice; and with a normal ending in progress.

Also: the normal ending now marks the call ended (through the state machine, so the
provider can still correct it) instead of leaving it counted as live until the
provider's final report or the reaper.

## Documents (D-11)

Accepting an upload or a website queues its reading in the same transaction, in the
`knowledge` queue. A document left unread by an earlier release gets its job when the
engine starts.

## What the tests found

- **SQLite: a job could survive the rollback of the change that caused it.** Python's
  SQLite driver begins a transaction before an insert but not before a savepoint, and
  jobs are written inside savepoints. When a job was the first write of a
  transaction, releasing its savepoint committed it. The driver's transaction
  handling is now switched off and the transaction begun explicitly at the first
  write. PostgreSQL was never affected.
- **A migration referred to a table created by a later one**, by using a list that
  had grown since. The policy step now skips tables that do not exist yet.
