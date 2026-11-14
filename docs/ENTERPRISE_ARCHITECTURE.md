# Architecture

This describes the system as it is built. Where something is planned and not built,
it says so. The findings that drive the planned changes are in
`ENTERPRISE_BACKEND_AUDIT.md`; ids such as A-2 or D-6 refer to it.

## 1. Shape of the system

```
 browser ──► console (Next.js, repository devytalk)
               │  server-side proxy, session cookie
               ▼
 phone ◄──► phone provider ──webhooks, audio stream──► calling engine (this repository)
 provider                                              one Python process:
                                                         HTTP API (FastAPI)
                                                         media WebSocket, one voice pipeline per call
                                                         dial loop per running campaign
                                                         background job worker
                                                         call reconciler
                                                              │
                                                              ▼
                                                        PostgreSQL (or SQLite for one machine)
                                                        files: uploaded documents, backups
```

It is a **modular monolith**: one deployable, one database, modules with defined
dependencies. That is kept. Nothing measured so far calls for separate services, and
splitting a system whose reliability depends on transactions across campaigns,
leads, calls, jobs and the ledger would trade a known database guarantee for
distributed-systems problems. Two parts are designed to be *run* separately from the
same code when load requires it (section 6): background workers, and later the voice
pipeline.

The database is the source of truth for everything: call state, jobs, events, the
usage ledger. There is no message broker and no cache. Neither is introduced until a
measurement shows the database queue or a query is the bottleneck.

## 2. Modules today

Sizes and dependencies are measured from the code (imports between top-level
packages; "lazy" means imported inside a function).

| Module | Lines | Responsibility | Depends on |
| --- | --- | --- | --- |
| `config` | 198 | Settings from the environment, one place | - |
| `db` | 653 | Models, engine, sessions, `run_db` | config |
| `migrate`, `migrations/` | 84 + | Schema versions and upgrade | db (lazy) |
| `accounts` | 426 | People, passwords, sessions, roles, the role policy | db |
| `apideps` | 53 | Authentication and authorization dependency for routes | accounts, config, db |
| `server` | 787 | HTTP routes for campaigns, calls, setup; provider webhooks; media socket; lifespan | most modules |
| `intake` | 120 | Website form submissions | apideps, db |
| `leads` | 201 | Lead sheet import and phone validation | db |
| `dialer` | 1048 | Dial loop, lead reservation, call state machine, usage records, rules and budgets, reconciliation, phone provider client | config, db; intelligence (lazy) |
| `voice` | 1208 | Voice pipeline per call, in-call tools, transcript, latency, language following | dialer, llm, playbook, postcall, providers, training |
| `providers` | 280 | Speech-to-text and voice adapters with a capability check | config, languages |
| `llm` | 150 | Client for the AI model used by background workflows | config |
| `playbook` | 774 | Playbook schema, prompt builder, analyzer, short-form builder | llm, languages |
| `postcall` | 282 | End-of-call persistence, review, lead update | dialer, llm, playbook, voice |
| `intelligence` | 820 | Reports, lead scoring, coach, A/B tests, their routes | dialer, playbook, training |
| `training` | 1455 | Document and website reading, passages, search, drafts, versions, their routes | playbook, jobs, background |
| `jobs` | 290 | Durable queue in the database: enqueue, claim, lease, retry, dead letter | db |
| `background` | 207 | Job handlers and worker start/stop | jobs; handlers' modules (lazy) |
| `backup` | 206 | Backup, verify, restore | config |
| `cli` | 876 | Command line | most modules |
| `languages` | 133 | Language codes and detection | - |

Two pairs of modules import each other at load time: `postcall` and `voice`
(the pipeline finalizes a call; finalize needs the session type), and `accounts` and
`apideps` (the routes of `accounts` need the dependency, which needs the service).
The second is a package split, not a design problem: `accounts/__init__` (service)
does not import `apideps`; `accounts/api` does. The first is real and is removed in
phase 4 by moving the shared session and transcript types below both.

Six more pairs exist only through imports inside functions, all involving
`background` (it dispatches to the module that owns each job). That is the intended
direction: handlers register with the queue; modules never import each other to run
background work.

## 3. Logical boundaries

The twenty areas of responsibility asked for map onto the modules as follows. An
area marked *absent* has no code today.

| Area | Where it lives | State |
| --- | --- | --- |
| Identity and authentication | `accounts`, `apideps` | Built: passwords, sessions, lockout |
| Organization management | `db.Organization`, `accounts` | **Missing** until phase 2 (A-1 to A-7) |
| Users and permissions | `accounts` (three roles, policy by route) | Built; per-organization scoping in phase 2 |
| Calling engine | `dialer`, `server` (webhooks) | Built |
| Call session | `voice` | Built |
| Campaign engine | `dialer.campaign_runner`, `dialer.rules` | Built; single process (D-6) |
| Lead management | `leads`, `intelligence.scoring` | Built |
| AI orchestration | `llm` plus direct use in `voice.pipeline` | Partial: no timeouts, usage or limits (C-6, C-9) |
| Speech providers | `providers` | Built, with capability checks; no health checks or fallback |
| Knowledge and training | `training` | Built; keyword search only (C-13) |
| Playbooks | `playbook`, `training.versions` | Built |
| Sales intelligence | `intelligence` | Built |
| Background jobs | `jobs`, `background` | Built; no queues or priorities (D-10) |
| Events | - | *Absent*: the job table doubles as an outbox for three kinds of work |
| Calendar integrations | booking webhook only (`voice.tools`, `background`) | Minimal |
| CRM integrations | - | *Absent* |
| Notifications | text message from a call (`voice.tools`) | Minimal |
| Billing and usage | `dialer.lifecycle` (ledger), `dialer.rules` (spend, budget) | Estimates only (D-1) |
| Analytics and reporting | `intelligence.report`, routes in `server` | Built; in-memory (D-4) |
| Monitoring and audit | - | *Absent* (E-1 to E-3) |

## 4. Rules between modules

These hold today except where a finding is cited, and are the rules new code follows.

1. **Dependencies point downward**: `config` → `db` → domain modules (`accounts`,
   `leads`, `dialer`, `playbook`, `training`, `intelligence`) → `voice`/`postcall` →
   `server`/`cli`. A domain module does not import `server`.
2. **Routes hold no business logic.** A route authenticates, validates, calls one
   service function inside one database transaction, and shapes the answer. (Several
   routes in `server.py` still contain logic; they are moved as they are touched.)
3. **One way to reach the database from async code**: `run_db`, which runs a function
   in a worker thread inside one transaction. Nothing else opens sessions in request
   or call paths.
4. **One way to do background work**: enqueue a job in the same transaction as the
   change that causes it. FastAPI background tasks are used only to start a queued
   job sooner. (Document reading and the coach still bypass this: D-11.)
5. **One way to call the AI model outside a live call**: the `llm` module. (Enforced
   by a test from phase 5.)
6. **A module owns its tables.** Other modules read or change them through that
   module's functions, not with their own queries. Known exceptions: `server.py`
   queries calls and leads directly for lists and the export; `intelligence` reads
   `call_runs` for reports.
7. **Every customer-owned row belongs to an organization**, and every request path
   filters by the caller's organization on the server. (Phase 2.)
8. **Settings come from `config.Settings` only.** No module reads the environment.

## 5. Request, call and job paths

**A console request**: browser → console proxy (adds the session token from an
httpOnly cookie) → engine route → `require_api_token` (who, which role, which
organization) → service function in `run_db` → JSON.

**An outbound call**: dial loop picks the next due lead (rules, limits, budget) →
reserves the lead and creates the call in one transaction (conditional update plus a
unique index: a lead cannot have two live calls) → asks the phone provider to dial,
with a timeout → provider webhooks move the call through its states (forward only;
the provider's word beats the engine's inference) → when the call is answered the
provider opens the media socket with a one-time token → the voice pipeline runs →
`finalize_call` saves the transcript, writes the usage record, enqueues the review
and any booking notification → the reconciler asks the provider about any call that
went quiet.

**An inbound call**: provider webhook → number → campaign → call created → media
socket as above.

**A job**: enqueued with an idempotency key → claimed with a lease → handler runs
with a timeout → done, or retried with backoff, or dead after the attempt limit →
every attempt is recorded → a dead job can be retried by hand.

## 6. Scaling plan (not built unless stated)

| Part | Today | When it becomes the limit | Change |
| --- | --- | --- | --- |
| API | In the one process | CPU or thread pool saturation by reads (measured: `LOAD_TEST_RESULTS.md`) | SQL-side filtering and paging (D-4), then several API instances behind a load balancer; needs D-6 first |
| Dial loop, reconciler | In the one process | More than one instance must run | Leader lease in the database (D-6) |
| Background workers | In the one process, four slots | Analysis backlog, or slow jobs affecting calls | Queues with their own concurrency (D-10), then a worker-only process from the same image |
| Voice pipelines | In the one process, one event loop | Audio latency under concurrent calls (not yet measured: needs provider credentials) | Several call-serving instances; the provider's media socket already reaches whichever instance the webhook named |
| Database | One PostgreSQL | Connection or query limits | Pooler, read replica for reports, partition `call_runs` by month only when its size is measured to hurt |

## 7. Decisions

| Decision | Why | Revisit when |
| --- | --- | --- |
| Modular monolith | Cross-module transactions are the reliability mechanism; one team; no measured need | A part needs a different release cadence or isolation the process cannot give |
| PostgreSQL as queue and event store | Exactly-once effects come from the same transaction as the data; fewer moving parts | Queue throughput is measured to limit the system |
| No cache | No query is yet both hot and slow after D-4 | A measured hot query remains slow |
| Keyword search for knowledge | Works offline, no extra service; weak for Arabic (C-13) | After C-13's fixes, if retrieval quality on real calls is insufficient: embeddings with pgvector |
| SQLite supported | One-machine pilots and tests | Never for more than one organization or instance; PostgreSQL is required there |
