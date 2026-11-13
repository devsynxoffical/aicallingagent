# Backend audit

Audit date: 2026-11-13. Commit audited: `4476176` (branch `claude/epic-newton-y166t4`).

Scope: everything under `callagent/`, the deployment files, the tests, and
`docs/ARCHITECTURE.md`. About 6,000 lines of Python in one FastAPI process.

How to read this document:

- Every finding was confirmed by reading the code named in it. Nothing here comes from
  a real phone call: this environment has no Twilio, speech or model credentials, so no
  provider has ever been exercised. Findings that can only be confirmed against a real
  provider say so.
- Priority: **P0** can call the wrong person, call someone twice, lose data or leak
  data across customers. **P1** breaks reliability or operations under normal
  production conditions. **P2** is a real weakness with a workaround. **P3** is hygiene.
- Status at the end of each finding is filled in as the hardening work proceeds.

## 1. What exists

| Area | Implementation |
| --- | --- |
| Process model | One uvicorn process: HTTP API, Twilio webhooks, the media WebSocket, one asyncio task per running campaign (`CampaignRunner`), voice pipelines, and background work, all together |
| Database | SQLAlchemy 2, SQLite by default (also in `docker-compose.yml`), Postgres through `DATABASE_URL`. Tables are created by `create_all`; new columns are added at start by `add_missing_columns` |
| Auth | One static bearer token for all of `/api/*`. Twilio routes use Twilio's request signature. The media WebSocket uses a per-call random token |
| Tenancy | An `organizations` table and `organization_id` on campaigns and training tables. Exactly one organization exists; no request is scoped by organization |
| Calls | Twilio Programmable Voice + Media Streams into a Pipecat pipeline (speech-to-text adapter, Claude, voice adapter). State lives in `call_runs.status` and `leads.status` |
| Background work | FastAPI `BackgroundTasks` and `asyncio.create_task`; post-call review runs inline when the media stream closes |
| AI | Live turns through Pipecat's Anthropic service; offline work (playbook drafting, review, coach) through `callagent/llm.py` with schema-validated structured output |
| Files | Uploads on local disk under `DATA_DIR/uploads/<organization>/<campaign>/<id>.<type>` |
| Billing | None. `call_runs.cost_usd` is an estimate from two configured rates; nothing is charged |
| Observability | stdlib `logging` with free-text messages; per-call latency numbers stored on the call |
| Tests | 177 tests, all without network. No concurrency, load, recovery or real-provider tests |

What is already sound and should be kept: provider adapters with capability checks;
Twilio signature validation on by default; per-call single-use stream token; SSRF
checks on the website crawler at every redirect hop; upload type sniffing with stored
names that never contain user input; playbook versions with approval; budget checks
against the worst case before every dial; a late "ringing" cannot reopen a finished
call; the voice loop never waits for the post-call review.

## 2. Findings

### Calling engine

**C-1. A lead can be dialed twice at the same time** · P0
- Component: `dialer/campaign_runner.py` `dial_lead`, `pick_next_lead`; `server.py` `/api/campaigns/{name}/dial`.
- Current: the dialer picks a lead in one transaction and marks it `calling` in another. `dial_lead` does not look at the lead's current status, at calls already in progress for that lead, or at the do-not-call list.
- Weakness: two `/dial` requests, a `/dial` during a campaign tick, or two processes on one database each place a call to the same person. A lead whose status became `dnc` between pick and dial is still called.
- Impact: a prospect's phone rings twice at once; a suppressed number can be called. Legal and reputational exposure, and double cost.
- Correction: claim the lead atomically (`UPDATE ... WHERE status IN (...)` and check the row count), refuse when the lead has a call that is not finished, re-check the do-not-call list inside the claim, and back it with a partial unique index: one unfinished outbound call per lead.
- Dependencies: D-2.
- Acceptance: a test with many concurrent dial attempts for one lead yields exactly one call record and one provider request; a lead put on the do-not-call list is never dialed.

**C-2. A call is redialed without asking the provider what happened to the last one** · P0
- Component: `reap_stale_calls`, `dial_lead` failure path.
- Current: a call with no final status after about 20 minutes is marked `failed` and its lead returns to `pending`. If the request to Twilio raised (timeout, connection reset), the call is marked `failed` and retried later. Twilio is never queried.
- Weakness: a timeout does not mean Twilio did not place the call. A crash between Twilio accepting the call and the Call SID being stored leaves a real call with no SID on record.
- Impact: duplicate calls to the same person; a call still in progress can be recorded as failed.
- Correction: before any call is declared dead or redialed, fetch its status from the provider (by SID, or by destination number and time when no SID was stored). Only a provider answer, or a confirmed absence, moves the state.
- Dependencies: C-4, C-5.
- Acceptance: tests with a fake provider API: an "in-progress" call is not reaped; a timed-out create that actually succeeded is adopted, not redialed. Marked as not validated against real Twilio.

**C-3. Stale calls are only cleaned up while their campaign is running** · P1
- Component: `reap_stale_calls` is called only from `CampaignRunner._tick_snapshot`.
- Weakness: after a pause, an automatic stop or a finished campaign, leads stuck in `calling` and calls stuck in `ringing` stay that way forever. Inbound-only campaigns are never reconciled.
- Impact: wrong dashboards, leads that are never retried, an inaccurate concurrency count on resume.
- Correction: one reconciliation job for all campaigns, independent of campaign state.
- Acceptance: a stuck call on a paused campaign is resolved by the job.

**C-4. No explicit call state machine** · P1
- Component: `call_runs.status` and `leads.status` are written from six places (dialer, Twilio webhook, stream start, setup failure, reaper, post-call).
- Weakness: each writer has its own ad hoc guard. `load_call_context` sets `in-progress` unconditionally. Nothing lists the legal transitions, so a new writer can break an invariant without a test failing.
- Impact: inconsistent final statuses; hard-to-diagnose state bugs.
- Correction: one transition function with a table of allowed moves; illegal moves are refused and logged; terminal states are final.
- Acceptance: property-style test over every (state, event) pair; no code writes `status` directly.

**C-5. Provider requests have no timeout** · P1
- Component: `dialer/twilio_client.py`. The Twilio client is built without a timeout (its HTTP client's default is none).
- Weakness: one hung request blocks that campaign's dial loop indefinitely and occupies a worker thread. `transfer_call` and `send_sms` run during a live call.
- Impact: a campaign silently stops dialing; dead air on a call.
- Correction: explicit connect and read timeouts, plus `asyncio.wait_for` around the thread call.
- Acceptance: a test with a provider that never answers shows the dial failing within the limit and going to reconciliation (C-2), not to blind retry.

**C-6. A restart pauses every campaign** · P1
- Component: `server.py` `lifespan` → `runner.stop_all()` → `stop(status="paused")`.
- Weakness: a graceful shutdown marks every running campaign `paused`; at start only `running` campaigns resume. The resume code therefore works after a crash but not after a deploy.
- Impact: every deployment silently stops all dialing until someone notices.
- Correction: shutdown stops the loops without changing campaign status.
- Acceptance: test: start, shut down gracefully, start again, the campaign is running.

**C-7. The transcript exists only in memory until the call ends** · P1
- Component: `voice/session.py`, `voice/pipeline.py`, `postcall/summarizer.py`.
- Weakness: a crash or restart during a call loses its transcript, tool events and review. Review failures are written to `call_runs.error` and never retried.
- Impact: lost sales evidence and lost opt-out context; calls with no disposition.
- Correction: save the transcript as it grows; make the review a durable job with retries (J-1).
- Acceptance: killing the worker mid-call leaves the transcript so far; a failed review is retried and, after the limit, visible as a dead job.

**C-8. The media WebSocket's single-use check is not atomic** · P2
- Component: `authorize_stream` reads `stream_connected`; `load_call_context` sets it later, in another transaction.
- Weakness: two connections presenting the same valid token at the same moment can both pass.
- Impact: two pipelines on one call. Needs the per-call token, so the exposure is small.
- Correction: compare-and-set in one statement.
- Acceptance: concurrent connections with one token: one accepted.

**C-9. A booking webhook blocks the live call and is never retried** · P1
- Component: `voice/tools.py` `book_meeting`.
- Weakness: the HTTP request to `BOOKING_WEBHOOK_URL` runs inside the tool call with a 10 second timeout, so the prospect hears silence while it hangs. A failure is logged and forgotten. The request is unsigned. Calling the tool twice creates two appointments and two webhook deliveries.
- Impact: dead air; lost bookings in the customer's calendar; duplicate meetings.
- Correction: write the appointment and an outbox row in one transaction; deliver from a durable job with retries and an HMAC signature; one appointment per call unless the time changes.
- Acceptance: a failing endpoint does not delay the tool result; delivery is retried with backoff and then dead-lettered; repeated tool calls with the same time create one appointment and one delivery.

**C-10. Follow-up texts can be sent more than once per call** · P2
- Component: `voice/tools.py` `send_followup_sms`. No per-call limit, and the body is whatever the model wrote.
- Impact: duplicate or off-script texts to a prospect (see also AI-3).
- Correction: per-call limit, an idempotency key per (call, message), length and link checks.
- Acceptance: a second identical send in one call is refused.

**C-11. Recording references are probably never stored** · P2
- Component: `place_call` sets `record=`, but no recording status callback is configured; `recording_url` is only read from the call status callback.
- Impact: recordings exist at the provider with no reference on the call. Needs a real Twilio account to confirm.
- Correction: set the recording status callback and store the recording SID.
- Acceptance: blocked on credentials.

### Background work

**J-1. There is no durable job system** · P1
- Component: `BackgroundTasks` in `training/api.py`, `asyncio.create_task` in `training/service.py`, inline review in `voice/pipeline.py`, inline HTTP in `book_meeting`.
- Weakness: no job identity, retry, backoff, timeout, failure record or history. A restart drops whatever was in flight.
- Impact: silently lost work.
- Correction: a `jobs` table in the existing database (unique idempotency key, attempts, backoff, timeout, lease, dead state, history) and a worker loop in the existing process. No new infrastructure is needed at this size; the table is the queue.
- Acceptance: jobs survive a restart; a failing job retries with backoff and then goes dead; a duplicate enqueue is a no-op; an expired lease is taken over.

**J-2. A training run interrupted by a restart blocks training forever** · P1
- Component: `training/api.py` `start_training`, `training/builder.py`. `resume_unfinished` handles documents only.
- Weakness: the run stays `running`; every later request gets "a training run is already in progress".
- Correction: run training as a job (J-1); a run whose job is dead is marked failed.
- Acceptance: after a simulated crash, training can be started again.

**J-3. Website crawling runs inside an open write transaction** · P1 (P0 on SQLite)
- Component: `training/service.py` `process_document_sync`: sets `status = "processing"`, flushes, then fetches up to 25 pages inside the same session.
- Weakness: on SQLite the write lock is held for the whole crawl, so every other write waits and then fails with "database is locked": status webhooks, dialing, tool calls on live calls.
- Impact: live calls lose their state updates while someone uploads a website.
- Correction: claim and commit, do the I/O with no transaction open, then store the result in a second short transaction.
- Acceptance: a test with a slow fetcher shows other writes succeeding during the crawl.

**J-4. Slow jobs share the thread pool with live calls** · P2
- Component: `db.run_db` uses the default executor for everything, including crawls and multi-minute model calls.
- Impact: at high concurrency a tool call on a live call can queue behind a crawl.
- Correction: a separate, bounded executor for jobs.
- Acceptance: load test (step 8).

### Database

**D-1. No migrations** · P1
- Component: `db.get_engine`: `create_all` plus `add_missing_columns` on every start.
- Weakness: additive columns only. Constraints, indexes, type changes and data backfills cannot be expressed; there is no record of what was applied; DDL runs implicitly at import time.
- Correction: versioned, ordered migrations recorded in the database (Alembic, the standard tool for SQLAlchemy), with the current schema as the baseline.
- Acceptance: a fresh database and a database from the audited commit both migrate to the same schema; the migration is tested on both SQLite and, when available, Postgres.

**D-2. Missing uniqueness, so duplicates are prevented only by code that can race** · P0
- `call_runs.twilio_call_sid` is not unique: two deliveries of the inbound webhook at once create two call records for one call.
- `leads (campaign_id, phone_e164)` is not unique: importer and inbound path de-duplicate with select-then-insert.
- `playbook_versions (campaign_id, version)` is not unique, and "one active version per campaign" is not enforced.
- "One running experiment per campaign" is check-then-insert.
- `campaigns.inbound_number` is check-then-set.
- `appointments` has no uniqueness per call.
- Impact: duplicate call records, duplicate leads (and so duplicate calls), two live playbooks.
- Correction: unique and partial unique indexes; handle the constraint error as "already exists".
- Acceptance: concurrent inserts in tests produce one row; existing duplicates are reported by the migration, never silently deleted.

**D-3. Business tables carry no organization** · P1
- Component: `leads`, `call_runs`, `appointments`, `do_not_call` have no `organization_id`; `campaigns.organization_id` is nullable; `campaigns.name` is unique across all organizations; `call_runs.experiment_id` is not a foreign key.
- Impact: tenant scoping needs a join through the campaign, row-level security cannot be applied, and one customer's campaign name blocks (and reveals) another's.
- Correction: add and backfill `organization_id`, make it NOT NULL, unique campaign name per organization.
- Dependencies: D-1, S-1.
- Acceptance: every business row has an organization; the cross-organization tests in S-1 pass.

**D-4. No status constraints and few composite indexes** · P2
- Statuses are free strings. The hot queries (`pick_next_lead`, active-call count, latest call per lead) have single-column indexes only.
- Correction: CHECK constraints on status columns; indexes on `leads (campaign_id, status, next_attempt_at)`, `call_runs (campaign_id, status)`, `call_runs (lead_id, id)`.
- Acceptance: query plans in the load test use the indexes.

**D-5. Money is stored as floating point** · P2
- `call_runs.cost_usd` is `Float` and spend is a float sum.
- Correction: `Numeric(12, 4)` and decimal arithmetic.
- Acceptance: spend equals the exact sum of call costs.

**D-6. Lead and report endpoints load whole campaigns into memory** · P2
- `intelligence/api.py` `list_leads` and `intelligence/report.py` read every lead and every call (with transcripts) and filter in Python.
- Impact: slow and memory-hungry at tens of thousands of calls.
- Correction: filter and page in SQL; load only the columns needed.
- Acceptance: load test with a large campaign.

**D-7. Connection handling is left at defaults** · P2
- No pool size, no `pool_pre_ping`, no SQLite WAL or busy timeout, no statement timeout.
- Correction: explicit pool settings from configuration; WAL and busy timeout on SQLite; pre-ping on Postgres.
- Acceptance: a dropped connection is replaced without a failed request.

**D-8. No backups** · P1
- Nothing backs up the database or the uploads; no restore has ever been tried.
- Correction: step 9 (backup command, restore drill, documented objectives).

### Security and tenancy

**S-1. One shared token, no organization scoping** · P0 before a second customer
- Component: `apideps.require_api_token`; every route resolves campaigns by global name or id; `GET /api/calls/{id}` returns any call.
- Weakness: no users, roles, expiry or rotation. Whoever holds the token is every customer. `approved_by` is free text from the client.
- Impact: with two customers on one deployment, each can read and change the other's leads, transcripts and playbooks.
- Correction: API keys stored hashed, each bound to an organization and a role; every query scoped by that organization; the legacy token maps to the default organization as owner so existing installs keep working.
- Acceptance: automated tests in which organization B gets 404 on every route for every object of organization A.

**S-2. No rate limits and one unbounded upload** · P1
- No request limits anywhere. `POST /api/campaigns/{name}/leads` reads the whole upload into memory with no size cap.
- Correction: per-key rate limits; a size cap on lead uploads.
- Acceptance: tests for 429 and 413.

**S-3. Webhooks can be replayed, and signature checking can be switched off** · P2
- Twilio signatures carry no timestamp. A captured, validly signed request is accepted again. `TWILIO_VALIDATE_SIGNATURE=false` is accepted silently.
- Correction: record each webhook delivery (call SID, status, sequence) and ignore one already processed; refuse to start with validation off unless a development flag is set.
- Acceptance: a replayed status callback changes nothing.

**S-4. Personal data in logs** · P1
- `server.py` logs caller numbers (`inbound call_run %s from %s`); provider exceptions can include numbers.
- Correction: structured logging with redaction of phone numbers, emails and secrets.
- Acceptance: a test greps captured logs for a known number and token.

**S-5. The crawler can be re-pointed after its address check** · P2
- `training/web.py` resolves and checks the host, then the HTTP client resolves it again (DNS rebinding).
- Correction: connect to the address that was checked.
- Acceptance: a resolver that answers differently on the second lookup cannot reach a private address.

**S-6. Word and PowerPoint uploads have no decompressed-size limit** · P2
- `.docx` and `.pptx` are zip archives; only the compressed size (25 MB) is limited.
- Correction: limit total uncompressed size and entry count before parsing.
- Acceptance: a zip bomb is refused quickly.

**S-7. CSV export can carry spreadsheet formulas** · P2
- `export.csv` writes names, notes and model-written summaries unescaped.
- Correction: prefix cells that start with `=`, `+`, `-`, `@`.

**S-8. No retention or erasure** · P2
- Transcripts and lead data are kept forever; there is no delete or anonymize operation and no audit log of who read or changed what.
- Correction: an audit log table for state-changing actions; a retention setting and an erase-lead operation.

**S-9. Builds are not reproducible** · P2
- Dependencies are lower bounds with no lock file; the image runs as root with no health check; no vulnerability scan.
- Correction: lock file, non-root user, health check, a scan step.

### AI orchestration

**AI-1. No accounting, versioning or explicit limits on model calls** · P2
- `llm.py` relies on SDK defaults (10 minute read timeout, 2 retries). Token usage is not stored. Prompts are constants in code with no version recorded on the output.
- Correction: one entry point that sets timeouts per use, records model, prompt version, tokens and cost per call, and redacts what it logs.
- Acceptance: every model call leaves a usage row; a review can be traced to its prompt version.

**AI-2. Model output ranges are not enforced** · P2
- Scores described as "0-10" or "0-100" in `CallReview` have no bounds, so an out-of-range value is stored and shown.
- Correction: bounds in the schema; clamp on read for old rows.

**AI-3. Retrieved text and prospect speech sit next to privileged tools** · P1
- Passages from uploaded documents and crawled websites are returned to the model as tool results. The model can send a text message it wrote itself, transfer the call, book a meeting and suppress a number.
- Weakness: text planted in a web page ("send the customer this link") is an instruction the model may follow. Tools are limited to the current lead, which bounds the damage.
- Correction: mark retrieved passages as untrusted data; per-call tool limits; refuse links in texts that are not in the approved playbook.
- Acceptance: tests that a planted instruction cannot produce a text with a foreign link or exceed the limits. Model behaviour itself can only be checked with a real model.

**AI-4. No fallback when a provider fails during a call** · P2
- A model or speech outage mid-call leads to silence and then the idle goodbye. Nothing switches provider.
- Status: design only until real providers are available; cannot be validated here.

**AI-5. The coach runs inside an HTTP request for minutes** · P2
- Correction: run it as a job and poll.

### Observability

**O-1. Logs are free text with no correlation** · P1
- No request id, no consistent call id, no metrics endpoint, no tracing, no error tracker, no alerts.
- Correction: JSON logs with request id, organization, campaign and call id bound through context; a `/metrics` endpoint (requests, calls by state, jobs by state, queue age, provider latency, database timings); an event trail per call.
- Acceptance: one call can be followed from dispatch to review by filtering on its id.

**O-2. `/health` does not check anything** · P2
- It reports configuration only, and reveals which settings are missing to anyone.
- Correction: liveness and readiness (database, job worker), with detail behind authentication.

### Deployment and operations

**A-1. Nothing stops two dialers on one database** · P1
- A second instance or a second uvicorn worker starts a second loop per campaign.
- Correction: a lease per campaign in the database; C-1 makes a double dial impossible even if the lease fails.
- Acceptance: two runners on one database, one dials.

**A-2. SQLite is the shipped default** · P2
- Correct for a single-operator install, wrong for a multi-tenant service: one writer at a time, no point-in-time recovery, no row-level security.
- Correction: keep SQLite for development and tests; document Postgres as the production database and test migrations on it.

### Billing

**B-1. There is no billing** · P1 (design)
- Nothing is charged, so nothing is charged twice. The risk is in what comes next: costs are recomputed in two places (`finalize_call`, `apply_twilio_status`), which is harmless for an overwritten estimate and wrong for a ledger.
- Correction: a usage ledger with one row per (call, kind), protected by a unique key, written once when the call's final duration is known.
- Acceptance: replaying the final status callback any number of times leaves one usage row and the same total.

### Tests

**T-1. No test reaches a provider, a second process or a failure** · P1
- All 177 tests run on in-memory or temp-file SQLite with stand-ins for Twilio and the model.
- Correction: concurrency, recovery, failure-injection and load tests in this project; a short real-provider checklist to run once credentials exist.

## 3. Order of work

1. Database foundation: migrations (D-1), uniqueness (D-2), organization columns (D-3), indexes and constraints (D-4), pool settings (D-7).
2. Call safety: atomic lead claim (C-1), state machine (C-4), provider timeouts (C-5), reconciliation that asks the provider (C-2, C-3), restart behaviour (C-6).
3. Durable jobs and outbox (J-1 to J-4, C-7, C-9), webhook de-duplication (S-3), usage ledger (B-1).
4. Tenancy and security: API keys and scoping with cross-organization tests (S-1), limits (S-2), logging redaction (S-4), upload and crawler fixes (S-5, S-6, S-7), audit log (S-8).
5. Observability (O-1, O-2), AI accounting and limits (AI-1 to AI-3).
6. Load tests, backup and restore drill, readiness report.

Each step ends with the full test suite.

## 4. What this audit cannot establish

- How any of this behaves on a real call. No credential for Twilio, a speech provider or
  the model exists in this environment.
- The concurrent-call capacity of one process. Voice activity detection and turn
  detection are CPU-bound and share the process with the API; the number must be
  measured, and the load tests in step 8 can only measure the parts that do not need a
  provider.
- Whether the configured model identifiers and beta flags are accepted by the live API.
