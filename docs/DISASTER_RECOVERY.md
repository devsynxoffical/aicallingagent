# Disaster recovery

What is in place, what was tested, and what is still a decision for whoever operates
the service. Ids such as E-4 refer to `ENTERPRISE_BACKEND_AUDIT.md`.

**What has and has not been checked.** The backup and restore drill runs in the
automated tests on SQLite and on PostgreSQL 16 with the real `pg_dump` and
`pg_restore`, with a few dozen rows. Nothing here has run in production: restore
time on real data, restoring onto another machine, and copying backups off the
machine are unmeasured or not built.

## What has to survive

| Data | Where it lives | In a backup |
| --- | --- | --- |
| Organizations, people, campaigns, leads, calls, transcripts, playbook versions, do-not-call lists, the usage ledger, AI usage, the audit trail, events, queued jobs | The database | Yes (the whole database) |
| Uploaded training files | `DATA_DIR/uploads` | Yes |
| Call recordings | At the telephony provider | No (the provider's retention applies) |
| Provider keys, API token, the backup key | Environment variables / secret store, `DATA_DIR/api_token` | **No, on purpose** |
| Website form submissions kept in a file when the engine was unreachable | The website's own disk | No |

## Backups

```bash
callagent db backup-key                      # once: prints a key for BACKUP_ENCRYPTION_KEY
callagent db backup --to /mnt/backups --keep-days 14
callagent db verify-backup /mnt/backups/backup-20261114T181500Z
```

- **Safe while the server runs**: SQLite's online backup API, or `pg_dump` (one
  consistent snapshot) on PostgreSQL.
- **Encrypted** when `BACKUP_ENCRYPTION_KEY` is set: AES-256-GCM, in pieces that are
  each authenticated with their position and whether they are the last, so a file
  that was cut short, reordered or altered is refused. Without the key nothing in
  the backup is readable except the manifest (time, schema version, row counts,
  checksums). Without a key the backup is written in the clear, and the command
  says so.
- **The key is not in the backup and is not recoverable.** Keep it in a secret
  store that is not where the backups are. Losing it loses every backup made with it.
- `manifest.json` records the time, the schema version, the row count of fifteen
  tables and a SHA-256 of each file (and, when encrypted, of what was encrypted). A
  failed backup removes its directory rather than leaving half of one.
- `verify-backup` checks the files against their checksums; with the key set it
  also decrypts them and compares, which proves this key opens this backup.
- `--keep-days N` removes older backups afterwards. The newest three are always
  kept, so a schedule that stopped running does not end with none.
- The PostgreSQL password is passed to `pg_dump` in the environment, never on the
  command line.

### Scheduled

Set `BACKUP_EVERY_HOURS` (for example 6). The engine then takes, verifies and prunes
backups itself, into `BACKUP_DIR` (default `DATA_DIR/backups`), keeping
`BACKUP_KEEP_DAYS` (default 14). With several engine processes only the one that
leads does it. The time of the last success and a count of failures are metrics
(`callagent_backup_last_success_timestamp_seconds`, `callagent_backup_failures_total`),
and a failure raises the alert `ScheduledBackupFailing`.

**The engine does not copy backups off the machine.** A backup on the same disk as
the database does not survive losing that disk. Point `BACKUP_DIR` at storage that
is somewhere else (a mounted network volume or bucket), or copy the directory with
your own job. This is the largest gap in what is built.

## Restoring

```bash
# 1. stop the server
callagent db verify-backup /mnt/backups/backup-…
callagent db restore /mnt/backups/backup-…                 # refuses a database that holds campaigns
callagent db restore /mnt/backups/backup-… --replace-existing
# 2. start the server: it applies any migration newer than the backup
```

A restore works in two steps:

1. **Stage.** In a scratch directory: check the checksums, decrypt, compare with
   what was encrypted, unpack the uploads, and (SQLite) run the database's own
   integrity check. A wrong key, a damaged file or a missing key ends here with
   nothing live touched.
2. **Swap.** SQLite: the database file is copied next to the live one and moved
   into place in one rename. PostgreSQL: `pg_restore` in a single transaction, so
   the database is the old one or the restored one, never part of each. The uploads
   directory is swapped by rename. Then the row counts are compared with the
   manifest.

After a restore, calls that were in progress at backup time are still marked as in
progress. The dialer does not ring those leads again while that is so; the
reconciler asks the provider about each and closes them, and calls whose audio had
started are finished by their queued job (`CALLING_ENGINE_RELIABILITY.md`, section 5).

## What the drill proved

`tests/test_backup.py`, on SQLite and (with `TEST_POSTGRES_URL`) PostgreSQL:

| Check | Result |
| --- | --- |
| Back up, delete every lead, call, version and uploaded file, restore | Every row and file is back, including Arabic and accented text, JSON transcripts and the do-not-call list |
| The same with an encrypted backup | The same; and no lead's notes, transcript, uploaded file or database header can be found in the backup's bytes |
| Restore without the key, or with another key | Refused in staging; live data and files exactly as they were; no staging directory left |
| An encrypted file cut short, changed by one byte, or with two pieces swapped | Refused |
| One byte changed in an unencrypted backup | Verification fails; restore refuses |
| Restore over live data | Refused without `--replace-existing` |
| Secrets in the backup | None: the API token file is not included |
| Retention | Old backups removed; the newest three kept |
| Schedule | A backup is taken when the newest is older than the interval, and not before |

Not proven: restore time on a production-sized database (the drill holds 25 calls),
restoring onto a different machine, a restore during an incident by someone who has
not done it before, and anything about recordings held by the provider.

## Recovery objectives

Targets to choose, with what each requires. None is met by default.

| Objective | With what is built | To do better |
| --- | --- | --- |
| **RPO** (how much recent data may be lost) | The backup interval: `BACKUP_EVERY_HOURS=1` gives up to an hour, if the backups are stored off the machine | PostgreSQL with WAL archiving, or a managed service with point-in-time recovery: seconds to minutes. Not configured by this project |
| **RTO** (how long until service is back) | Time to provision a machine, restore, and start. Unmeasured on real data | A standby database that can be promoted |

Proposed starting point for a first customer: RPO 1 hour, RTO 2 hours; PostgreSQL on
a managed service with point-in-time recovery switched on; the engine's scheduled
encrypted backup to separate storage as an independent second copy; a restore into
a scratch database every month, timed and written down.

## Secrets

Secrets are not in backups, so a restore onto a new machine needs them from their
source:

| Secret | Source of truth | If lost or leaked |
| --- | --- | --- |
| Phone, speech and AI provider keys | The provider's console; store in your secret manager | Rotate in the provider's console, update the environment, restart |
| `API_TOKEN` | Your secret manager (set it explicitly in production instead of using the generated file) | Set a new value and restart. People sign in again |
| `BACKUP_ENCRYPTION_KEY` | Your secret manager, away from the backups | Lost: backups made with it cannot be read; take a new backup with a new key at once. Leaked: backups made with it are readable by whoever has both; new key, new backups, destroy the old ones |
| `METRICS_TOKEN`, `INTAKE_TOKEN` | Your secret manager | Set a new value on both sides and restart |

## Rolling back a deployment

1. Take a backup before every deployment that includes a migration.
2. To go back: stop the server, deploy the previous version, restore that backup.
   Migrations have `downgrade` steps, tested for the organization and ledger
   migrations, and two of them refuse when going back would lose data (a second
   organization; corrections or AI lines in the ledger). Restoring the backup is
   the path that always works.
3. With `AUTO_MIGRATE=false`, migrating is its own step (`callagent db upgrade`),
   and a process refuses to start against a database that is not at its version.

## Stopping and starting

- **A stop signal** (`callagent serve`): the process stops placing calls, reports
  "not ready", and waits up to `SHUTDOWN_DRAIN_SECONDS` (180) for the calls it is
  carrying to finish. A second signal stops at once. Give the process manager a
  longer grace period than that (the compose file sets 200 s). Tested with stand-in
  calls; not exercised with a real call.
- **On start**: migrations are applied or verified; interrupted training runs are
  closed or resumed; documents accepted but not read are queued again; jobs whose
  worker died are taken over when their lease runs out; the process takes the
  dialing lease if nobody holds it and resumes campaigns marked running.
- **A crash during calls**: the calls are cut off. Their transcripts up to that
  point are saved (they are written as the call proceeds). Each call is given an
  outcome and its lead released by a queued job once the call's maximum length has
  passed, and reviewed from the saved transcript.
- **Several engine processes**: one dials. If it stops, another takes over after at
  most `LEADER_LEASE_SECONDS` (30), at once on a clean stop.

## When something goes wrong

1. Stop outbound dialing first: pause the campaigns (the console, or
   `POST /api/campaigns/{name}/pause`). Calls in progress finish.
2. Record what you see before changing anything: `callagent db status`,
   `callagent db check`, `/health/ready`, the log lines around the time, with their
   request and call ids.
3. If data is damaged, take a backup of the damaged state too, then restore the
   last good backup into a scratch database and compare before replacing production.
4. Before resuming, confirm the do-not-call lists are intact (they are part of the
   backup, and their row count is in the manifest).
5. Use the audit trail (`GET /api/audit`) and the event log (`GET /api/events`) to
   establish who did what and what happened to each call.
6. Write down what happened, when, what was lost, and what will prevent it.
