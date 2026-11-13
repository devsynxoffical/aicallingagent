# Production readiness

Status on 2026-11-13. **The service is not production-ready for a multi-customer
launch.** It is considerably safer than it was a day ago for a single-organization
pilot, with one large caveat: no part of it has ever handled a real phone call in the
environment where it was built.

Evidence levels used below:

- **Verified**: an automated test in this repository proves it, and the test was run.
- **Built, not validated with a provider**: tested against stand-ins only. Stand-ins
  prove the logic, not the integration.
- **Blocked**: cannot be done without credentials.
- **Open**: not done.

## Acceptance criteria

| Criterion | Status | Evidence |
| --- | --- | --- |
| No cross-organization data access in security tests | **Open** | Organization scoping is not implemented; the tests cannot exist yet. See `SECURITY_REVIEW.md` |
| No duplicate business operations from repeated events | Verified | Lifecycle, inbound-webhook, booking and job tests; load test (2,000 webhooks per level, final status sent twice) |
| No duplicate billing from repeated processing | Verified, for the usage ledger | One usage row per call under replays and concurrency. There is no invoicing or payment code to test |
| Safe worker recovery | Verified for the job queue and training runs | Lease takeover, late results ignored, interrupted training re-run. Open: a transcript in memory at the moment of a crash |
| Safe campaign and call state transitions | Verified in logic; not validated with a provider | Every (state, event) pair tested; atomic lead reservation tested on SQLite and PostgreSQL under concurrency |
| Verified backups and successful restoration | Verified | Restore drill on SQLite and PostgreSQL (pg_dump/pg_restore) on every test run. Not verified: production-sized data, another machine, scheduled operation |
| Measured concurrent-call performance | **Blocked** | Bookkeeping measured at 5/10/25/50 (`LOAD_TEST_RESULTS.md`). Live calls need providers |
| Traceable failures | Partly | Job failures and dead jobs are stored and listed; call status carries who reported it. Open: structured logs, correlation ids, metrics, tracing |
| No exposed secrets | Partly verified | Secrets are not in backups (tested) and not returned by the API. Open: phone numbers in logs, no log redaction |

## The ten steps

| Step | State |
| --- | --- |
| 1. Audit | Done: `BACKEND_AUDIT.md`, 40 findings with priorities and acceptance criteria |
| 2. Database | Mostly done: migrations, uniqueness, indexes, pool settings, transactions for reservation and publishing, backup and restore. Open: organization columns and row-level security, status CHECK constraints, decimal call costs |
| 3. Background jobs | Done for the core, booking notifications, review safety net and training. Open: document reading as a queue job, a separate executor for slow jobs |
| 4. Calling engine | Mostly done: state machine, duplicate-dial prevention, provider timeout, reconciliation, restart behaviour, usage ledger. Open: incremental transcript saving, atomic WebSocket claim, recording references, dial-loop lease across processes |
| 5. AI orchestration | **Open.** Structured output is validated and the live voice path does not wait for analysis (both pre-existing). No usage accounting, prompt versions, explicit timeouts, fallback or injection hardening |
| 6. Security | Reviewed (`SECURITY_REVIEW.md`); several fixes landed. **Open: organization isolation, roles, rate limits, audit log, retention** |
| 7. Observability | **Open**, apart from job statistics and stored per-call latency |
| 8. Load tests | Done for what can run without providers. Open: live calls, ingestion, scheduling, AI analysis |
| 9. Disaster recovery | Done: backup, verify, restore, drill, documented objectives and procedures. Open: scheduling, off-machine copies, point-in-time recovery (deployment choices) |
| 10. Testing | 242 tests (227 on SQLite plus 15 repeated on PostgreSQL), including concurrency, failure-injection, recovery and ledger-consistency tests. Open: security isolation tests, API contract tests, any real-provider test |

## Before a single-organization pilot with real calls

1. Provider credentials in the environment, and the five checks at the end of
   `RELIABILITY_PLAN.md` run on real calls.
2. PostgreSQL, with backups scheduled and copied off the machine.
3. TLS in front of the server; `API_TOKEN` and `BOOKING_WEBHOOK_SECRET` set explicitly.
4. One process only (audit A-1).
5. Measured cost rates configured, or budgets cannot be enforced.
6. A real-call ramp to find the concurrent-call ceiling of one process.

## Before a second customer

Everything in "What has to happen before a second customer" in `SECURITY_REVIEW.md`,
and observability good enough to answer "what happened to this call" without reading
the database by hand.

## Remaining risks, in order

1. **Nothing has been exercised against real providers.** Integration details (Twilio
   status values and timing, media stream behaviour under load, model identifiers and
   beta flags) are untested assumptions.
2. **No tenant isolation.** One token, no scoping.
3. **Unknown call capacity.** Audio processing shares one process with everything else.
4. **Little observability.** A production incident would be investigated from
   free-text logs and SQL.
5. **A crash mid-call loses that call's transcript.**
6. **SQLite under concurrent reads** degrades sharply (measured); the report and lead
   list load whole campaigns into memory on any database.
7. **Prompt injection through training material** is unmitigated beyond tool scoping.
