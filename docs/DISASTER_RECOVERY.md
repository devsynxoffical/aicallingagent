# Disaster recovery

What is in place, what was tested, and what is still a decision for whoever operates
the service. Nothing here has run in a production environment: the drills below ran on
a development machine against SQLite and a local PostgreSQL 16.

## What has to survive

| Data | Where it lives | In a backup |
| --- | --- | --- |
| Campaigns, leads, calls, transcripts, playbook versions, do-not-call list | The database | Yes |
| Uploaded training files | `DATA_DIR/uploads` | Yes |
| Call recordings | At the telephony provider | No (the provider's retention applies) |
| Provider keys, API token | Environment variables / secret store, `DATA_DIR/api_token` | **No, on purpose** |

## Backups

```bash
callagent db backup --to /mnt/backups        # database + uploads + manifest, verified after writing
callagent db verify-backup /mnt/backups/backup-20261113T181500Z
```

- Safe while the server runs: SQLite's online backup API, or `pg_dump` (one
  consistent snapshot) on PostgreSQL.
- `manifest.json` records the time, the schema migration, row counts per table and a
  SHA-256 of each file. A failed backup removes its directory rather than leaving half
  of one.
- The PostgreSQL password is passed to `pg_dump` in the environment, never on the
  command line.

**Scheduling is not built in.** Run it from the operating system, and copy the result
off the machine. A backup on the same disk as the database does not survive losing
that disk. Example (hourly, keep 14 days):

```
0 * * * *  callagent db backup --to /mnt/backups && find /mnt/backups -maxdepth 1 -name 'backup-*' -mtime +14 -exec rm -rf {} +
```

## Restoring

```bash
# 1. stop the server
callagent db verify-backup /mnt/backups/backup-…
callagent db restore /mnt/backups/backup-…                 # refuses a database that holds campaigns
callagent db restore /mnt/backups/backup-… --replace-existing
# 2. start the server: it applies any migration newer than the backup
```

A restore checks the checksums first, replaces the database and the uploads, and
then compares the row counts with the manifest. A damaged backup is refused before
anything is touched.

After a restore, calls that were in progress at backup time are still marked as in
progress. The dialer does not ring those leads again until the calls are resolved
(one unfinished outbound call per lead is enforced by the database); resolving them
against the provider is audit item C-2.

## What the drill proved

`tests/test_backup.py` runs on every test run, on SQLite and (with
`TEST_POSTGRES_URL`) on PostgreSQL with the real `pg_dump` and `pg_restore`:

| Check | Result |
| --- | --- |
| Back up, delete every lead, call, version and uploaded file, restore | Every row and file is back, including Arabic and accented text, JSON transcripts and the do-not-call list |
| Secrets in the backup | None: the API token file is not included |
| One byte changed in the backup | Verification fails; restore refuses; live data untouched |
| Restore over live data | Refused without `--replace-existing` |

Not proven: restore time on a production-sized database (the drill uses 25 calls),
restoring onto a different machine, and anything about recordings held by the provider.

## Recovery objectives

These are targets to choose, with what each one requires. None is met by default.

| Objective | With what is built | To do better |
| --- | --- | --- |
| **RPO** (how much recent data may be lost) | The backup interval. Hourly backups: up to 1 hour | PostgreSQL with WAL archiving or a managed service with point-in-time recovery: seconds to minutes. Not configured by this project |
| **RTO** (how long until service is back) | Time to provision a machine, restore, and start. Minutes for small databases; must be measured on real data | A standby database that can be promoted |

Proposed starting point for a first customer: RPO 1 hour, RTO 2 hours, PostgreSQL on
a managed service with point-in-time recovery switched on, plus the nightly
`callagent db backup` as an independent second copy. Run a restore into a scratch
database every month and record how long it took.

## Secrets

Secrets are not in backups, so a restore onto a new machine needs them from their
source:

| Secret | Source of truth | If lost or leaked |
| --- | --- | --- |
| `TWILIO_AUTH_TOKEN`, speech and model keys | The provider's console; store in your secret manager | Rotate in the provider's console, update the environment, restart |
| `API_TOKEN` | Your secret manager (set it explicitly in production instead of using the generated file) | Set a new value and restart. Dashboard users sign in again |

## Rolling back a deployment

1. Take a backup before every deployment that includes a migration.
2. To go back: stop the server, deploy the previous version, restore that backup.
   Migrations have `downgrade` steps for the indexes, but restoring the backup is the
   tested path.
3. A graceful stop no longer pauses campaigns (it used to); campaigns that were
   running resume when the process starts.

## Worker recovery

- Campaign dial loops restart with the process for every campaign whose status is
  `running`.
- Training documents left `pending` or `processing` are picked up again at start.
- **Not yet recovered** (audit C-7, J-1, J-2): a call's transcript if the process dies
  mid-call; a post-call review that failed; a training run interrupted by a restart.
  These need the durable job queue, which is the next piece of work.

## When something goes wrong

1. Stop outbound dialing first: pause the campaigns (`POST /api/campaigns/{name}/pause`
   or the CLI). Calls in progress finish.
2. Record what you see before changing anything: `callagent db status`, `callagent db
   check`, the server log.
3. If data is damaged, take a backup of the damaged state too, then restore the last
   good backup into a scratch database and compare before replacing production.
4. Before resuming, confirm the do-not-call list is intact (it is part of the
   backup).
5. Write down what happened, when, what was lost, and what will prevent it.
