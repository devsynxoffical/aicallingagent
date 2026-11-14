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
 provider                                              one or more Python processes, each:
                                                         HTTP API (FastAPI)
                                                         media WebSocket, one voice pipeline per call
                                                         background job worker
                                                       and in exactly one of them (a lease):
                                                         dial loop per running campaign
                                                         call reconciler, scheduled backups
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
usage ledger, and which process dials. There is no message broker and no cache.
Neither is introduced until a measurement shows the database queue or a query is the
bottleneck (`PERFORMANCE_TESTING.md`).

## 2. Modules today

Sizes and dependencies are measured from the code (imports between top-level
packages; "lazy" means imported inside a function).

| Module | Lines | Responsibility | Depends on |
| --- | --- | --- | --- |
| `config` | 216 | Settings from the environment, one place | - |
| `db` | 949 | Models, engine, sessions, thread pools, `run_db`, row-level security | config |
| `migrate`, `migrations/` | 85 + | Schema versions and upgrade | db (lazy) |
| `accounts` | 542 | People, passwords, sessions, roles, the role policy, organizations | db |
| `apideps` | 89 | Who is calling, which role, which organization | accounts, config, db |
| `server` | 880 | HTTP routes for campaigns, calls, setup; provider webhooks; media socket; lifespan | most modules |
| `intake` | 121 | Website form submissions | apideps, db |
| `leads` | 201 | Lead sheet import and phone validation | db |
| `dialer` | 1132 | Dial loop, lead reservation, call state machine, usage records, rules and budgets, reconciliation, phone provider client | config, db, events; intelligence (lazy) |
| `voice` | 1442 | Voice pipeline per call, in-call tools, transcript, latency, language following, spoken fallback | dialer, llm, playbook, postcall, providers, training |
| `providers` | 280 | Speech-to-text and voice adapters with a capability check | config, languages |
| `llm` | 499 | Every use of the AI model outside the live conversation: workflows, deadlines, retries, concurrency limit, usage records, prices | config; db (lazy) |
| `playbook` | 791 | Playbook schema, prompt builder, analyzer, short-form builder | llm, languages |
| `postcall` | 310 | End-of-call persistence, review, lead update | dialer, llm, playbook, voice |
| `intelligence` | 822 | Reports, lead scoring, coach, A/B tests, their routes | dialer, playbook, training |
| `training` | 1660 | Document and website reading, passages, search, drafts, versions, their routes | playbook, jobs, background |
| `jobs` | 384 | Durable queue in the database: queues, priority, fairness, lease, retry, dead letter | db |
| `events` | 132 | Durable event log written with the change; one job per consumer | db, jobs |
| `background` | 307 | Job handlers and worker start/stop | jobs; handlers' modules (lazy) |
| `billing` | 215 | The usage ledger: lines, corrections, spend, summaries | db, events |
| `observability` | 402 | Log format and masking, request ids, metrics, readiness, audit entries, body limit, draining | db (lazy) |
| `leader` | 132 | The lease that decides which process dials | db |
| `backup` | 385 | Backup (optionally encrypted), verify, staged restore, retention, schedule | config |
| `cli` | 927 | Command line | most modules |
| `languages` | 133 | Language codes and detection | - |

Two pairs of modules import each other at load time: `postcall` and `voice`
(the pipeline finalizes a call; finalize needs the session type), and `accounts` and
`apideps` (the routes of `accounts` need the dependency, which needs the service).
The second is a package split, not a design problem: `accounts/__init__` (service)
does not import `apideps`; `accounts/api` does. The first is real and still there:
removing it means moving the shared session and transcript types below both, which
has not been done.

Six more pairs exist only through imports inside functions, all involving
`background` (it dispatches to the module that owns each job). That is the intended
direction: handlers register with the queue; modules never import each other to run
background work.

## 3. Logical boundaries

The twenty areas of responsibility asked for map onto the modules as follows. An
area marked *absent* has no code today.

| Area | Where it lives | State |
| --- | --- | --- |
| Identity and authentication | `accounts`, `apideps` | Built: passwords, sessions, lockout. No password reset by email, no second factor (A-13) |
| Organization management | `db.Organization`, `accounts`, `apideps` | Built: every customer-owned row belongs to an organization; requests are confined to the caller's, in the application and by PostgreSQL row-level security. Telephony settings are still per deployment (A-11) |
| Users and permissions | `accounts` (three roles, policy by route) | Built, per organization |
| Calling engine | `dialer`, `server` (webhooks) | Built. See `CALLING_ENGINE_RELIABILITY.md` |
| Call session | `voice` | Built |
| Campaign engine | `dialer.campaign_runner`, `dialer.rules`, `leader` | Built; with several processes, one dials |
| Lead management | `leads`, `intelligence.scoring` | Built |
| AI orchestration | `llm`; `voice.llm_service` for the live conversation | Built: deadlines, bounded retries, concurrency limit, usage and cost records. See `AI_ORCHESTRATION.md` |
| Speech providers | `providers` | Built, with capability checks; no health checks, no automatic fallback (deliberate) |
| Knowledge and training | `training` | Built; keyword search with a relevance threshold, cached per campaign. No semantic index |
| Playbooks | `playbook`, `training.versions` | Built |
| Sales intelligence | `intelligence` | Built |
| Background jobs | `jobs`, `background` | Built: four queues with their own limits, priority, fairness between organizations |
| Events | `events` | Built: a log in the database, written in the same transaction as the change. See `EVENT_ARCHITECTURE.md` |
| Calendar integrations | booking webhook only (`voice.tools`, `background`) | Minimal |
| CRM integrations | signed webhooks per organization for call, booking, campaign and playbook events (`webhooks`, `WEBHOOKS.md`); no connector for a named CRM | Minimal |
| Notifications | text message from a call (`voice.tools`) | Minimal |
| Billing and usage | `billing` (ledger), `dialer.rules` (budget), `llm` (AI usage) | Built as a ledger of measured usage with corrections, and budgets enforced from it. No invoices, payments or balances. See `BILLING_ARCHITECTURE.md` |
| Analytics and reporting | `intelligence.report`, routes in `server` | Built; the lead list pages in the database, the report is still computed in memory (D-4) |
| Monitoring and audit | `observability` | Built: structured logs, health, metrics, alert rules, audit trail. No dashboards, no external error reporting. See `OBSERVABILITY.md` |

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
   job sooner. (The coach, native phrasing and lead import still run inside the
   request, on the background threads: D-11.)
5. **One way to call the AI model outside a live call**: the `llm` module, under a
   named workflow. A test reads every module's source and fails on a direct client
   call anywhere else.
6. **A module owns its tables.** Other modules read or change them through that
   module's functions, not with their own queries. Known exceptions: `server.py`
   queries calls and leads directly for lists and the export; `intelligence` reads
   `call_runs` for reports.
7. **Every customer-owned row belongs to an organization**, and every request path
   is confined to the caller's organization on the server. A test calls every
   addressable route as another organization and expects 404 from each.
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
provider opens the media socket with a one-time token → the voice pipeline runs,
saving the transcript as it goes → `finalize_call` writes the usage record, reviews
the call and enqueues any booking notification → the reconciler asks the provider
about any call that went quiet, and a queued job finishes any call whose process
stopped. Each step records an event.

**An inbound call**: provider webhook → number → campaign → call created → media
socket as above.

**A job**: enqueued with an idempotency key → claimed with a lease → handler runs
with a timeout → done, or retried with backoff, or dead after the attempt limit →
every attempt is recorded → a dead job can be retried by hand.

## 6. Scaling

What exists, what was measured, and what the next step is when a part becomes the
limit. Nothing here has been run on more than one machine.

| Part | Today | When it becomes the limit | Next step |
| --- | --- | --- | --- |
| API | Every process serves it | CPU, or the report endpoint on large campaigns (measured: `LOAD_TEST_RESULTS.md`) | More processes behind a load balancer (built for: the lease below); the report in SQL (D-4) |
| Dial loops, reconciler, scheduled backups | In the one process that holds the lease; another takes over when it stops | Not expected: pacing is by design | - |
| Background workers | In every process: four queues with their own limits, on their own thread pool; a job is leased to one worker | Analysis backlog, or CPU-heavy parsing next to calls | A worker-only process from the same image (not built: every process also serves the API) |
| Voice pipelines | In whichever process the provider's media connection reaches, one event loop per process | Audio latency under concurrent calls. **Not measured: needs provider credentials** | More processes; the ceiling per process comes from the real-call ramp in `PERFORMANCE_TESTING.md` |
| AI requests outside calls | A limit per process (default 2 at once) | The provider's rate limit shared with live calls | A limit shared across processes; batch pricing for reviews |
| Database | One PostgreSQL | Connections or query time | A pooler, a read replica for reports, partitioning `call_runs` by month only when its size is measured to hurt |

## 7. Decisions

| Decision | Why | Revisit when |
| --- | --- | --- |
| Modular monolith | Cross-module transactions are the reliability mechanism; one team; no measured need | A part needs a different release cadence or isolation the process cannot give |
| PostgreSQL as queue and event store | Exactly-once effects come from the same transaction as the data; fewer moving parts | Queue throughput is measured to limit the system |
| No cache | No query is yet both hot and slow after D-4 | A measured hot query remains slow |
| Keyword search for knowledge | Works offline, no extra service; weak for Arabic (C-13) | After C-13's fixes, if retrieval quality on real calls is insufficient: embeddings with pgvector |
| SQLite supported | One-machine pilots and tests | Never for more than one organization or instance; PostgreSQL is required there |
