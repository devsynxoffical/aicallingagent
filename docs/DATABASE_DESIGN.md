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
