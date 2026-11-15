# Production readiness

State on 2026-11-14, after the ten phases of the backend hardening work. This is the
delivery report: what was found, what was changed, what was verified and how, and
what is not done.

**The service is not ready for production, for one overriding reason: no part of it
has ever handled a real phone call.** There are no phone, speech or AI provider
credentials in the environment it was built in. Every provider in every test is a
stand-in. Stand-ins prove the engine's own logic; they prove nothing about the
providers, the audio, or how many calls a process can carry.

Apart from that, the engine is in a state where a single-organization pilot can be
prepared, and the remaining work for a second customer is known and listed.

## How to read the evidence

Every statement below falls into exactly one of these:

| Level | Meaning |
| --- | --- |
| **Verified** | An automated test in this repository proves it, and the test was run and passed on this code |
| **Built, not fully tested** | The code exists; tests cover part of it, or none exercise it end to end |
| **Tested with stand-ins only** | Tested, but against a substitute for a provider. Not a provider validation |
| **Blocked** | Cannot be done without credentials or infrastructure that is not available here |
| **Not built** | Does not exist |

Test runs behind this report: the engine's suite, 395 tests on SQLite and 447 with
PostgreSQL 16 (the same tests, plus those that need PostgreSQL: row-level security,
real locks, `pg_dump` and `pg_restore`); the console's 70 unit tests and 25 browser
tests against the engine. All pass, locally and (except the browser tests) in CI. No test is skipped except those that need
PostgreSQL when it is not configured.

## 1. What the audit found

69 findings in five areas, each with its present state, the failure it allows, the
fix and a "done when" (`ENTERPRISE_BACKEND_AUDIT.md`). The ones that mattered most:

- **Organizations existed in name only.** No request knew whose data it could see.
  Any signed-in person could read and change every customer's campaigns, calls and
  leads by name or id.
- **Live calls shared threads with slow analysis**, so a few slow website crawls
  could make a caller wait in silence.
- **A call cut off by a restart was never finished**: no outcome, its lead stuck.
- **A campaign would ring through its whole list during a provider outage**, each
  call a silent line.
- **Nothing recorded what the AI model was used for or what it cost.**
- **The usage ledger held one estimate per call that could never be corrected**, and
  spend was read from a different, floating-point copy.
- **No logs, no metrics, no audit trail**, and what was printed contained phone
  numbers.
- **Backups were manual and unencrypted.**

## 2. Architecture as it was

One Python process: the HTTP API, the media WebSocket with one voice pipeline per
call, a dial loop per campaign, a job worker and a reconciler; one database
(PostgreSQL, or SQLite for one machine); a separate Next.js console that reaches the
engine through a server-side proxy. A modular monolith with the database as the
source of truth. That shape was sound and is kept: no new services, no broker, no
cache (`ENTERPRISE_ARCHITECTURE.md`).

## 3. What was changed

| Phase | Change |
| --- | --- |
| 2. Database and isolation | Every customer-owned row carries its organization; every request is confined to its caller's organization on the server; PostgreSQL row-level security underneath; per-organization do-not-call lists and campaign names; status CHECK constraints; migrations tested from the previous release's schema |
| 3. Events and background work | A durable event log written in the same transaction as each change; four job queues with their own limits, priorities and fairness between organizations; two thread pools so slow work cannot take a thread a call needs; a call that was cut off is finished by a queued job |
| 4. Calling engine | Stop-on-failing-provider rule; no two campaigns ringing one person; atomic claim of the audio connection; reconciliation that survives one bad call; in-call tools decided and bounded on the server; opt-outs caught after the call too; a spoken fallback when the model fails a turn; short timeouts on the live model |
| 5. AI and knowledge | One module for every AI request outside calls, with a deadline, bounded retries, a concurrency limit and a usage record; untrusted text marked as data; knowledge index cached; Arabic and French matching and a relevance threshold; document limits |
| 6. Observability | JSON logs with request, call and job ids and masked personal data; liveness and readiness; Prometheus metrics; 16 alert rules; an audit trail of every change attempt |
| 7. Billing | A ledger that is only added to, with corrections as new lines; spend and budgets read from it; the budget enforced where a call is created |
| 8. Performance | Measured before and after on one machine; the lead list paged in the database; a lease so several processes can run and one dials |
| 9. Recovery | Encrypted backups, staged restore, retention, a schedule; draining of calls on stop |
| 10. Delivery | A non-root container image with a health check; a compose file with PostgreSQL; CI for both repositories; explicit-migration mode; a request size limit; console security headers |

Seven migrations (0007 to 0013) take a database of the release before this work to
the current schema; the upgrade is tested on SQLite and PostgreSQL, and an upgraded database is
compared with a new one column by column.

## 4. Verified

Each line names what a test proves. The test names are in the audit's status table.

**Isolation and access**

- Every addressable API route, called as another organization, answers 404 and
  changes nothing (SQLite, and PostgreSQL as an ordinary role).
- Unfiltered queries against PostgreSQL return only the caller's organization's rows.
- Every route called as every role allows exactly what the role is for.
- A refused attempt to change something is recorded with who made it.

**Calls and campaigns (logic)**

- Every pair of call state and event follows the lifecycle; statuses never go back.
- Two simultaneous attempts on one lead open one call (also under load, 400 leads
  per level, on both databases).
- Of twelve simultaneous claims of one audio token, one gets the call.
- A call whose process stopped gets an outcome and frees its lead, once.
- Five answered calls with no agent speech stop the campaign; calls with a real
  outcome do not.
- One call that cannot be reconciled does not stop the others.

**Money and usage**

- One ledger line per finished call; any number of repeated final reports add nothing.
- The engine's figure followed by the carrier's becomes a correction; the first line
  is unchanged.
- Corrections: a new line, a note required, not across organizations, operator only.
- Eight simultaneous claims against a budget that covers one call create one call,
  manual dials included (both databases).
- An AI request leaves a usage row for the right organization, campaign and call;
  a model with no known price is recorded as unknown cost, not as free.

**Background work**

- No job runs twice under 50 workers; a saturated analysis queue does not delay
  call-related jobs; a full background thread pool does not delay call-path work.
- An event recorded twice is one event; a job or event written in a transaction
  that is rolled back does not survive it.
- A training run that meets a busy AI provider goes back to the queue.

**Operations**

- No phone number or email address appears in the logs, exception text included.
- Readiness turns red when the database stops answering, when the schema is not the
  release's, when the worker is not running, and while stopping.
- An encrypted backup restores every row and file into an empty database on both
  databases; without its key, or altered, it is refused before anything is touched.
- Of ten processes asking for the dialing lease at once, one gets it; a clean stop
  hands it over at once; a leader that cannot reach the database stops dialing
  before its lease runs out.
- With automatic migration off, a process refuses an out-of-date database and
  changes nothing.

## 5. Built, not fully tested

| What | What is missing |
| --- | --- |
| Draining calls on a stop signal | The waiting logic is tested with stand-in calls. A real server process was started, sent a stop signal with no call in progress, and stopped cleanly in two seconds. It has never waited for a real call |
| Several engine processes | The lease, the job leases and the lead reservation are each tested. Two real processes behind a load balancer have not been run |
| Scheduled backups | The "is one due" logic and the backup itself are tested; the loop inside the server has not run for hours |
| The container image | Built by CI, where it runs as an ordinary user, starts the command line and carries the PostgreSQL 16 backup tools. It has not been run as a server, and its health check has not been seen to pass |
| The compose file | Written; never started (no container daemon in the environment this was built in) |
| The CI workflows | Run on GitHub for both repositories, and green. The engine's first runs found two faults that the local setup had hidden, both fixed: the tests' database role had no password, and the backup tools were newer than the database server (a backup written that way is refused on restore; the backup now refuses the mismatch up front). The dependency scan step reports as a warning and its output has not been read |
| The alert rules | Valid, and refer only to metrics that exist; never loaded into a monitoring system or seen to fire |
| Live-call AI usage | The accumulation is tested with synthetic usage reports; the voice library's real reports have not been seen |
| The 0012 and 0013 migrations on a large database | Tested on small ones. The index of 0013 should be built concurrently beforehand on a large, busy PostgreSQL database |

## 6. Tested with stand-ins only

None of this is a provider validation.

| Area | Stand-in |
| --- | --- |
| Placing calls, status callbacks, reconciliation, transfers, text messages | A fake phone provider client; signed HTTP requests built by the tests |
| The audio pipeline | Not run at all in tests: its parts (tools, transcript saving, fallback, usage counting) are tested separately |
| Speech-to-text and voice | Configuration and capability checks only |
| The AI model: reviews, coaching, training, native phrasing, retries, refusals, timeouts | A fake client returning scripted answers and errors |
| The provider's price for a call | A fake provider returning a price |
| Token counts and AI cost | Synthetic counts at list prices dated 2026-11-03 |

## 7. Blocked by missing credentials

**Update, 2026-11-15:** a Deepgram key was added and speech recognition was run
against the real provider over HTTPS: models, options and accuracy on clean English
and French audio. Live streaming, Arabic and everything about the other three
providers are still untested. Results and limits: `PROVIDER_VALIDATION.md`.

| What | Needs |
| --- | --- |
| A real outbound and inbound call, in each language | Phone, speech and AI provider accounts, a phone number |
| How many concurrent calls one process carries; response latency on real calls | The same |
| Whether the configured model names and request options are accepted by the provider | An AI provider key |
| Answering-machine detection accuracy | Real calls on real carriers |
| Reconciling any amount with an invoice | A month of real usage |
| The behaviour of each provider during its own outage | Cannot be provoked; observe |

No credential was requested or entered during this work. Provider keys belong in
the environment's settings, not in a conversation.

## 8. Not built

In order of what they block. Details in the audit's "Still open" table.

1. **Sign-up without the operator**: an organization is created by the operator,
   who also assigns its caller numbers. Its people now join by invitation link and
   choose their own passwords (section 15); the organization itself is still made
   by the operator.
2. **A phone provider account per organization**: every organization's numbers
   are numbers of the deployment's one account.
3. **Invoices, payments, plans, taxes, balances.** The ledger is what they would be
   drawn from. Monthly statements of usage exist (section 15); they are not
   invoices.
4. **Data retention and erasure**: built since (section 15). Recordings at the phone
   provider and old backups are not reached.
5. **Copying backups off the machine**; point-in-time recovery.
6. **A second factor, and a password reset a person can start themselves by
   email** (A-13): an admin makes a reset link instead (section 15). A sign-in limit
   per address on the engine (A-12).
7. **Automatic provider fallback** (deliberately), and a written manual procedure.
8. **A limit on concurrent calls across all campaigns of a deployment** (B-16).
9. **The campaign report, timeline and export computed in the database** (D-4).
10. **A semantic knowledge index; reading scanned pages.**
11. **Dashboards; external error reporting; an audit screen in the console.**
12. **CRM and calendar integrations.** Signed webhooks for call, booking, campaign
    and playbook events exist (`WEBHOOKS.md`), so any system can be told; no
    ready-made connector for a named CRM or calendar exists, and no delivery has
    been made to a real receiver.
13. **A lock file for dependencies**; the vulnerability scan as a gate.

## 9. Database changes

Migrations 0007 to 0013, all additive except where stated:

| Migration | Change |
| --- | --- |
| 0007 | `organization_id` on leads, calls, appointments, do-not-call; backfilled; uniqueness per organization; CHECK constraints on statuses. Rollback refused once two organizations hold campaigns |
| 0008 | Row-level security policies on every tenant table (PostgreSQL) |
| 0009 | `events`; `queue` and `priority` on jobs |
| 0010 | `ai_usage`; the model and prompt fingerprint on calls; a warning on documents |
| 0011 | `audit_log`; the request id on jobs |
| 0012 | The usage ledger: a unique key per line replaces one-line-per-call; how each amount was arrived at; corrections. Existing amounts are kept. Rollback refused while corrections or AI lines exist |
| 0013 | `leases`; an index on leads (organization, phone) |

No destructive migration was run against any production database: there is none.

## 10. Performance, measured

Full tables in `LOAD_TEST_RESULTS.md`. PostgreSQL 16, four CPUs shared by the server,
the database and the load generator; one campaign, 1,600 leads; 5 to 50 concurrent
actors. Zero errors, zero leads with two calls, zero jobs run twice at every level.

| Path | Before this work | After | Same machine, same day |
| --- | --- | --- | --- |
| Reserve a lead and open its call | 135 to 144 /s | 101 to 110 /s | About a fifth slower |
| Status webhook over HTTP, signed | 130 to 150 /s | 109 to 120 /s | About a fifth slower |
| Job queue | 64 to 85 jobs/s | 41 to 74 jobs/s | Slower, most at 50 workers |
| Campaign list | 132 to 186 /s | 136 to 181 /s | Unchanged |
| Lead list, a page of 25 | 7.6 to 8.7 /s | 112 to 126 /s | About 14 times faster |
| Campaign report (1,600 calls) | 13.7 to 14.5 /s | 13.7 to 15.0 /s | Unchanged |

The slower write paths are the price of what each now does: the organization check
and row-level security on every transaction, an event and a ledger line when a call
ends, the cross-campaign check before a dial. A call makes one reservation and
about five webhooks in its life, so a hundred of each per second is far from being
the limit for any number of calls one machine could carry. What that number is has
not been measured.

## 11. Remaining risks, in order

1. **Nothing has met a real provider.** Status values and timing, media stream
   behaviour, model names and request options, usage reports: all assumptions.
2. **Unknown call capacity.** Audio shares a process with the API and jobs.
3. **A second organization has not been run for real.** Its numbers, webhook,
   transfer number and rates are its own in every test; no second customer has
   placed a call.
4. **Backups stay on the machine unless you arrange otherwise.**
5. **Costs are estimates** until a month is reconciled with the providers' invoices.
   The budget does not reserve AI cost in advance.
6. **Row-level security does nothing if the engine runs as a database superuser.**
   `/api/status` says which it is.
7. **Prompt injection is reduced, not removed.** A document can still state a false
   fact; the per-call review lists unsupported statements.
8. **Keyword search misses** questions phrased with none of a document's words.
9. **No retention or erasure.**
10. **The work was reviewed by its author only.** No outside review, no penetration
    test.

## 12. What a deployment needs

| Need | Detail |
| --- | --- |
| PostgreSQL 16 | The engine's role must not be a superuser or have BYPASSRLS. A managed service with point-in-time recovery is the recommended start. The backup tools on the engine's machine must be of the server's major version (the image carries 16; build with `PG_MAJOR` for another) |
| One or more engine processes | The image in this repository. Behind a load balancer that uses `/health/ready`; a stop grace period longer than `SHUTDOWN_DRAIN_SECONDS` |
| TLS in front | The phone provider requires HTTPS and WSS; `PUBLIC_BASE_URL` is the public address |
| A proxy that sets `X-Forwarded-For` | The audit trail and the console's sign-in limit read it |
| Secrets in a secret store | Provider keys, `API_TOKEN`, `BOOKING_WEBHOOK_SECRET`, `INTAKE_TOKEN`, `METRICS_TOKEN`, `BACKUP_ENCRYPTION_KEY` |
| Storage for backups that is not the engine's disk | `BACKUP_DIR`, `BACKUP_EVERY_HOURS` |
| A monitoring system | Prometheus scraping `/metrics`; `ops/prometheus-alerts.yml`; somewhere for the JSON logs to go |
| Measured rates | `COST_PER_CONNECTED_MINUTE_USD` and `COST_PER_CALL_ATTEMPT_USD`, or budgets cannot work |
| The console | The Next.js application of the `devytalk` repository, with `DEVYTALK_API_URL` and, behind a proxy, `TRUSTED_PROXY_HOPS` |

## 13. Before a single-organization pilot

1. Provider credentials in the environment's settings.
2. The seven checks at the end of `CALLING_ENGINE_RELIABILITY.md`, on real calls.
3. A ramp of real calls (5, 10, 25, 50) against one process, reading response
   latency per call, thread pool backlog and CPU. Set the concurrency limit from it.
4. Everything in section 12.
5. One restore of a real backup into a scratch database, timed.
6. The first week's AI usage and call costs compared with the providers' own
   figures.
7. A decision on recording and consent wording for each market.

## 14. Recommended next phase

In this order:

1. **Provider validation** (the pilot list above). Everything else rests on it, and
   it will change what else is worth doing.
2. **Plans, invoices and payments**, which need a payment provider. Invitations and
   statements exist.
3. **The legal questions in `COMPLIANCE.md`, section 4**, answered for the first
   market. The mechanisms exist; whether a configuration is lawful is not known.
4. **The report in SQL and pruning of old rows** (D-4, D-9), once real campaign
   sizes are known.
5. **An outside security review.**

## 15. Built after this report was first written

Everything below is at the evidence level "tested with stand-ins": automated tests
on SQLite and PostgreSQL, the console in a real browser against a real engine
process, and **no provider, no real receiver and no real customer**.

| What | Where | Not covered |
| --- | --- | --- |
| Each organization's own caller numbers, transfer number, booking webhook and rates; API keys per organization | `orgsettings.py`, `tests/test_organization_settings.py` | One phone provider account still serves every organization |
| Consent on record per lead and a rule to call only with it; do-not-call import, listing and removal; erasing a person; a retention period | `privacy.py`, `COMPLIANCE.md`, `tests/test_privacy.py` | Whether any setting satisfies the law of a market; recordings at the provider; backups |
| Signed webhooks per organization for call, booking, campaign and playbook events, with retries, switching off, a test delivery and redelivery | `webhooks.py`, `outbound.py`, `WEBHOOKS.md`, `tests/test_webhooks.py` | A delivery to a real receiver; a connector for a named CRM |
| Joining by a one-time link; password reset by link; a new organization handed to its admin by link | `accounts/invitations.py`, `tests/test_invitations.py` | Email delivery: the admin passes the link on. A second factor |
| Monthly statements from the ledger, with every line as CSV | `billing.py`, `BILLING_ARCHITECTURE.md` | Invoices, tax, payment; any amount against a provider's invoice |
| Unit economics: cost per call, per connected call, per minute and per booked meeting, by campaign and by week; quality and cost per playbook version week by week | `intelligence/economics.py`, `intelligence/report.py`, `tests/test_economics.py` | Any figure from a real campaign: the tests use made-up calls |
| A handover brief when the agent transfers a call: who it is, the agent's note, the last things they said | `voice/tools.py`, `tests/test_call_safety.py` | The colleague hears nothing on the line: it is a cold transfer with the brief in the console and the webhook. Speaking it to them before connecting needs a real call to get right |

Migrations 0014 to 0017. Still not built: a semantic knowledge index (it needs an
embeddings model and real documents to judge whether it beats the present search),
connectors for named CRMs, payments.

