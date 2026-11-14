# Database design

State: migration `0002`. Tested on SQLite 3 and PostgreSQL 16.

## Which database

| | SQLite | PostgreSQL |
| --- | --- | --- |
| Use | One operator, one machine, development, tests | Any hosted or multi-customer deployment |
| Writers | One at a time (WAL lets readers continue) | Many |
| Point-in-time recovery | No | Yes, with WAL archiving (a deployment choice, not configured by this project) |
| Row-level security | No | Possible; planned with per-organization API keys (audit S-1, D-3) |

`DATABASE_URL` selects it. Nothing in the code is SQLite-only; the integrity and
backup tests run on both when `TEST_POSTGRES_URL` is set.

## Tables and relationships

```
organizations 1─┬─< campaigns 1─┬─< leads 1───< call_runs 1───< appointments
                │               ├─< playbook_versions
                │               ├─< experiments          (call_runs.experiment_id points here)
                │               ├─< training_documents 1───< knowledge_chunks
                │               └─< training_runs
                └─ (organization_id is also stored on the training, version and experiment tables)
do_not_call   one row per suppressed number
```

Known gap, tracked as audit D-3: `leads`, `call_runs`, `appointments` and
`do_not_call` do not carry `organization_id` yet; their organization is their
campaign's. `call_runs.experiment_id` is not a foreign key yet. Both change together
with per-organization API keys.

## Rules the database enforces

These used to be "look, then insert" in code. They are now unique indexes, so two
requests at the same instant cannot both succeed.

| Rule | Index |
| --- | --- |
| A provider call is one record, even if its webhook arrives twice | `uq_call_runs_twilio_call_sid (twilio_call_sid)` |
| A phone number is one lead per campaign | `uq_leads_campaign_phone (campaign_id, phone_e164)` |
| A lead has at most one unfinished outbound call | `uq_call_runs_one_active_outbound_per_lead (lead_id) WHERE direction = 'outbound' AND status IN (created, queued, initiated, ringing, in-progress)` |
| A campaign runs exactly one playbook version | `uq_playbook_versions_one_active (campaign_id) WHERE status = 'active'` |
| Version numbers are not reused | `uq_playbook_versions_campaign_version (campaign_id, version)` |
| One running A/B test per campaign | `uq_experiments_one_running (campaign_id) WHERE status = 'running'` |
| One campaign answers a phone number | `uq_campaigns_inbound_number (inbound_number)` |
| Booking the same time twice on one call is one meeting | `uq_appointments_call_start (call_run_id, starts_at)` |

Foreign keys are enforced on both databases (SQLite needs `PRAGMA foreign_keys=ON`,
which every connection now sets).

Indexes for the dialer's repeated queries: `ix_leads_due (campaign_id, status,
next_attempt_at)`, `ix_call_runs_campaign_status (campaign_id, status)`,
`ix_call_runs_lead_latest (lead_id, id)`.

## Transactions

- Every unit of work runs in one session that commits at the end or rolls back
  entirely (`db.session_scope`, `db.run_db`).
- **Reserving a lead for a call** (`dialer/campaign_runner.claim_lead_for_call`) is one
  transaction: conditional `UPDATE` of the lead to `calling`, a re-read of the
  do-not-call list, a check for any unfinished call, and the insert of the call
  record. If any step refuses, the whole reservation rolls back. The request to the
  telephony provider happens after the commit, never inside the transaction.
- **Publishing a playbook version** archives the previous version first and activates
  the new one second, inside one transaction.
- Still open (audit J-3): reading a website for training happens inside a write
  transaction. On SQLite this blocks other writers for the duration of the crawl.

## Migrations

`callagent/migrations/versions/`, applied by `callagent.migrate.upgrade` when the
server starts or by `callagent db upgrade`.

| Database found | What happens |
| --- | --- |
| Empty | The current schema is created and recorded as the newest migration |
| Created by a release before migrations (no record) | Columns those releases added are added, it is recorded as `0001`, then later migrations run |
| Tracked | Migrations it has not seen run in order |

Rules for writing a migration:

1. Check the data before changing the schema. If rows break the new rule, stop with a
   message that names them. Never delete or merge customer rows in a migration.
2. Declare the same index or constraint on the model, so a new database and an
   upgraded one end up identical.
3. Add a test in `tests/test_integrity.py` that upgrades an older database with data.
4. Back up first: `callagent db backup`.

On PostgreSQL the upgrade takes an advisory lock, so two processes starting together
do not both run it. PostgreSQL runs the whole upgrade in one transaction; SQLite
cannot roll back every kind of schema change, which is one more reason the data check
comes first.

Commands: `callagent db status`, `callagent db upgrade`, `callagent db check`.

## Connections

- PostgreSQL: pool of `DB_POOL_SIZE` (10) plus `DB_MAX_OVERFLOW` (20), checked before
  use (`pool_pre_ping`), recycled after 30 minutes, 10 second wait for a free
  connection. Size the pool to calls in progress plus API traffic; a call holds a
  connection only for short moments.
- SQLite: WAL journal, `synchronous=NORMAL`, 15 second wait for the write lock.

## Not done yet

| Item | Audit |
| --- | --- |
| `organization_id` on leads, calls, appointments, do-not-call; per-organization campaign names; row-level security | D-3, S-1 |
| CHECK constraints on status columns | D-4 |
| Costs as `NUMERIC` instead of floating point; a usage ledger | D-5, B-1 |
| Lead list and report filtering in SQL | D-6 |
| Website crawl outside the write transaction | J-3 |


## Organizations (migrations 0007 and 0008)

Every customer-owned row carries `organization_id`: campaigns, leads, calls,
appointments, usage records, documents, passages, playbook versions, experiments,
training runs, users, website requests, do-not-call entries, and jobs (a job with no
organization is the deployment's own housekeeping).

**Where the organization comes from.** A request's organization is the caller's
account's, set once by the authentication dependency (`apideps.require_api_token`)
and never read from a path, query or body. The deployment's operator (the engine API
token) works in the default organization unless it names another in the
`X-DevyTalk-Organization` header, which is ignored from anyone else. A new lead,
call, appointment or usage record takes the organization of the campaign (or lead) it
hangs from in one place, a `before_flush` listener in `db.py`, so no code path can
forget it or copy the wrong one.

**Two locks.**

1. The application: `apideps.get_campaign` resolves a campaign by `(organization,
   name)`, and every list or fetch by id filters by the caller's organization.
   Another organization's row answers exactly like a missing one (404). A lookup made
   outside an authenticated request raises instead of returning anything.
2. PostgreSQL row-level security: `db.session_scope` sets `app.organization_id` for
   the transaction, and a policy on each tenant table (FORCE, so it binds the table
   owner) makes the database return and accept only that organization's rows. With
   the setting absent nothing is restricted; that is how provider webhooks, the dial
   loop, background jobs and migrations work, and they address rows by id. A
   superuser or a role with BYPASSRLS is never bound: **run the engine as an ordinary
   role.** `GET /api/status` reports `row_level_security` as `enforced`, `bypassed`,
   `missing` or `unavailable` (SQLite), and the console shows it under Setup.

**Uniqueness.** Campaign names are unique per organization
(`uq_campaigns_org_name`); a do-not-call number once per organization
(`uq_do_not_call_org_phone`); an inbound number and a user's email stay unique across
the deployment (a phone number is answered by one campaign; an email signs in to one
account).

**Constraints and indexes added.** CHECK constraints on `campaigns.status`,
`leads.status`, `call_runs.status` and `jobs.status`. Indexes
`call_runs(campaign_id, started_at)` for the daily and monthly limits,
`call_runs(organization_id, started_at)` for the calls-per-day chart, and a partial
index over active calls for the reconciler.

**SQLite.** An existing SQLite database is upgraded in place with the columns added
and filled, but SQLite cannot alter a column or drop a constraint without rebuilding
the table, so there the new columns stay nullable in the schema, campaign names stay
unique across the deployment, and no CHECK constraints are added. A new SQLite
database gets the full schema. There is no row-level security on SQLite. **More than
one organization requires PostgreSQL.**

**Rollback.** `0007` can be rolled back while one organization holds campaigns (it
refuses otherwise, because names and do-not-call numbers could then collide), and
applied again; both directions are tested on SQLite and PostgreSQL. On PostgreSQL an
upgraded database and a newly created one are compared column by column in a test.

## AI usage and what a call ran on (migration 0010)

- `ai_usage`: one row per use of the AI model (a background request, or one whole
  phone conversation). Organization (required), campaign and call (optional),
  workflow, model, prompt fingerprint, token counts as the provider reported them,
  duration, outcome (CHECK constraint on the seven allowed values) and cost
  (`NUMERIC(12,6)`, empty when no price is known for the model). Indexed by
  organization and time, campaign, and call. It is a tenant table: the row-level
  security policy of migration 0008 applies to it.
- `call_runs.ai_model`, `call_runs.prompt_hash`: the model and the fingerprint of
  the instructions a conversation ran on, set when the audio starts.
- `training_documents.warning`: what was left out of a document that was otherwise
  read (pages with no text).

Rolling back 0010 drops the table and the three columns in place. On SQLite the
columns are dropped with `ALTER TABLE ... DROP COLUMN`; rebuilding `call_runs`, which
other tables refer to, is refused there.

`ai_usage` is a measurement, not a bill. The ledger a bill is drawn from is
`usage_records`; turning AI usage into ledger lines is phase 7 (D-1, D-2).

