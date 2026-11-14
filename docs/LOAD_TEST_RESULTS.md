# Load test results

Run on 2026-11-14 with `loadtests/run.py` on the code of this branch. How the tests
work, and what they do not cover, is in `PERFORMANCE_TESTING.md`.

**Read this first.** These numbers measure the server's bookkeeping: reserving leads,
processing provider webhooks, the job queue and the read API. They do **not** measure
live calls. A live call needs a phone provider, a speech provider and an AI model,
and none is available in this environment. How many simultaneous conversations one
process can carry is unknown, and nothing below should be quoted as call capacity.

## Setup

| | |
| --- | --- |
| Machine | 4 virtual CPUs, Linux, Python 3.13.18. Server, database and load generator all on this one machine, competing for the same CPUs |
| Server | One uvicorn process, default settings, database pool 20 + 40 |
| Databases | PostgreSQL 16.15 (local, default configuration) and SQLite (WAL) |
| Data | One campaign, 1,600 leads; 400 leads per concurrency level |
| Levels | 5, 10, 25 and 50 concurrent actors |
| Interference | The machine was otherwise idle, except that a unit test run of a few seconds overlapped the first scenario (the job queue) of the PostgreSQL run |

Reproduce (the database named is wiped):

```bash
python loadtests/run.py --database-url postgresql+psycopg://postgres@127.0.0.1:5432/callagent_load --out results.json
python loadtests/run.py --database-url sqlite:////tmp/load.db --out results.json
```

## Correctness under load

The same at every level, on both databases, in every run:

| Check | Result |
| --- | --- |
| Leads contested by two simultaneous dial attempts (400 per level) | 400 calls opened, 400 attempts refused, **0 leads with two calls** |
| Status webhooks, each call's final status delivered twice (2,000 requests per level, correctly signed) | 400 calls completed, **400 ledger lines** (one per call), 0 errors |
| Background jobs (500 per level, as many workers as the level) | 500 ran, **0 ran twice** |

Errors: none in the two runs reported below. Two were seen in earlier runs of the
same day and are recorded here rather than left out:

- PostgreSQL, 50 actors, `GET /api/campaigns`: 1 of 60 requests ended with a read
  error on the client side (no response). Not seen again in two further runs. Not
  explained.
- SQLite, 50 actors, the campaign report: 1 of 60 requests answered HTTP 500, in a
  run where those requests were taking 40 seconds each. Most likely a request
  waiting longer for a database connection than the pool allows (10 s); not confirmed.

## Timings: PostgreSQL

| Scenario | Level | p50 | p95 | max | Throughput |
| --- | --- | --- | --- | --- | --- |
| Reserve a lead and open its call | 5 | 46 ms | 83 ms | 114 ms | 108 /s |
|  | 10 | 94 ms | 167 ms | 235 ms | 106 /s |
|  | 25 | 210 ms | 424 ms | 524 ms | 110 /s |
|  | 50 | 362 ms | 971 ms | 1,337 ms | 101 /s |
| Status webhook (HTTP, signature checked) | 5 | 31 ms | 115 ms | 195 ms | 109 /s |
|  | 10 | 64 ms | 215 ms | 421 ms | 110 /s |
|  | 25 | 136 ms | 559 ms | 701 ms | 116 /s |
|  | 50 | 263 ms | 1,004 ms | 1,162 ms | 120 /s |
| `GET /api/campaigns` | 5 | 28 ms | 34 ms | 37 ms | 181 /s |
|  | 10 | 73 ms | 99 ms | 105 ms | 136 /s |
|  | 25 | 162 ms | 232 ms | 256 ms | 143 /s |
|  | 50 | 200 ms | 363 ms | 370 ms | 157 /s |
| Lead list, one page of 25 | 5 | 39 ms | 57 ms | 60 ms | 126 /s |
|  | 10 | 86 ms | 113 ms | 124 ms | 112 /s |
|  | 25 | 184 ms | 244 ms | 337 ms | 121 /s |
|  | 50 | 273 ms | 482 ms | 504 ms | 114 /s |
| Campaign report (1,600 calls) | 5 | 300 ms | 506 ms | 528 ms | 15 /s |
|  | 10 | 742 ms | 888 ms | 923 ms | 14 /s |
|  | 25 | 1,647 ms | 2,223 ms | 2,224 ms | 14 /s |
|  | 50 | 2,432 ms | 3,889 ms | 3,904 ms | 14 /s |
| Job queue | 5 | | | | 74 jobs/s |
|  | 10 | | | | 58 jobs/s |
|  | 25 | | | | 46 jobs/s |
|  | 50 | | | | 41 jobs/s |

## Timings: SQLite

| Scenario | Level | p50 | p95 | max | Throughput |
| --- | --- | --- | --- | --- | --- |
| Reserve a lead and open its call | 5 | 8 ms | 86 ms | 1,743 ms | 220 /s |
|  | 10 | 14 ms | 187 ms | 2,636 ms | 197 /s |
|  | 25 | 24 ms | 349 ms | 3,938 ms | 197 /s |
|  | 50 | 24 ms | 1,450 ms | 4,338 ms | 181 /s |
| Status webhook (HTTP, signature checked) | 5 | 20 ms | 50 ms | 1,045 ms | 175 /s |
|  | 10 | 38 ms | 122 ms | 2,686 ms | 154 /s |
|  | 25 | 96 ms | 392 ms | 8,426 ms | 155 /s |
|  | 50 | 210 ms | 595 ms | 13,377 ms | 149 /s |
| `GET /api/campaigns` | 5 | 22 ms | 31 ms | 37 ms | 226 /s |
|  | 10 | 52 ms | 100 ms | 108 ms | 182 /s |
|  | 25 | 95 ms | 193 ms | 210 ms | 229 /s |
|  | 50 | 158 ms | 272 ms | 283 ms | 208 /s |
| Lead list, one page of 25 | 5 | 78 ms | 98 ms | 104 ms | 65 /s |
|  | 10 | 238 ms | 308 ms | 313 ms | 42 /s |
|  | 25 | 545 ms | 859 ms | 1,596 ms | 37 /s |
|  | 50 | 1,188 ms | 1,648 ms | 1,703 ms | 35 /s |
| Campaign report (1,600 calls) | 5 | 2,121 ms | 2,466 ms | 2,544 ms | 2.5 /s |
|  | 10 | 7,169 ms | 7,643 ms | 7,829 ms | 1.5 /s |
|  | 25 | 14,523 ms | 26,137 ms | 51,006 ms | 1.2 /s |
|  | 50 | 28,804 ms | 42,713 ms | 45,427 ms | 1.3 /s |
| Job queue | 5 | | | | 67 jobs/s |
|  | 10 | | | | 40 jobs/s |
|  | 25 | | | | 32 jobs/s |
|  | 50 | | | | 31 jobs/s |

## Before and after this work

The code of the release before the hardening work (commit `77b1e76`) and the
current code, measured on this machine on the same day, PostgreSQL. Ranges are over
the four levels.

| Path | Before | Now |
| --- | --- | --- |
| Reserve a lead and open its call | 135 to 144 /s | 101 to 110 /s |
| Status webhook | 130 to 150 /s | 109 to 120 /s |
| Job queue | 64 to 85 jobs/s | 41 to 74 jobs/s |
| `GET /api/campaigns` | 132 to 186 /s | 136 to 181 /s |
| Lead list, one page | 7.6 to 8.7 /s | 112 to 126 /s |
| Campaign report | 14 to 14 /s | 14 to 15 /s |

The figures recorded for that older commit on 2026-11-13 were higher (210
reservations and 230 webhooks per second). The same commit measured today gives the
"Before" column. The machine is shared; only same-day comparisons mean anything.

## What the numbers say

1. **The write paths are about a fifth slower than before this work**, and the job
   queue more so at high worker counts. Each now does more: every transaction is
   confined to an organization (and to row-level security on PostgreSQL); a call's
   ending writes an event and a ledger line; a reservation checks that the person is
   not on a call from another campaign; jobs are taken in turn across organizations
   and queues. That was accepted. A call makes one reservation and about five
   webhooks over its life, so a hundred of each per second is not what limits the
   number of calls.

2. **Throughput is flat from 5 to 50 actors.** More concurrency adds waiting, not
   work done: one Python process sharing four CPUs with the database and the load
   generator.

3. **The lead list was the slowest endpoint and no longer is.** It loaded every
   lead and every call of the campaign, transcripts included, for each page. It now
   counts and pages in the database and loads the calls of the page only.

4. **The campaign report is unchanged and is now the slowest endpoint**: it still
   loads every call of the campaign. About 14 per second with 1,600 calls on
   PostgreSQL, growing with the campaign. Next to move into SQL (audit D-4).

5. **SQLite still collapses under concurrent reads of the report**: over two
   seconds per request at 5 actors, tens of seconds at 25 and 50. The same code on
   PostgreSQL does not. SQLite is for one operator on one machine; anything with
   several console users, or more than one organization, needs PostgreSQL.

6. **SQLite write latency has a long tail** (maximum 1 to 13 seconds while p50
   stays under 250 ms): one writer at a time, the others wait. No write failed.

## Not measured

| Item | Why |
| --- | --- |
| Concurrent live calls at 5, 10, 25, 50 | Needs phone, speech and AI credentials |
| AI review throughput | Needs an AI credential |
| Document reading and campaign scheduling under load | Not written yet |
| A database on another machine, a pooler, several server processes | One machine available |
| Hours of load, memory growth | Not run |
| The effect of the index added after these runs (leads by organization and phone) | Added afterwards; with 1,600 leads it is not expected to show |

First measurement to make once credentials exist: one process, real calls ramped
5, 10, 25, 50, recording per-call response latency (stored on every call and
exported as a metric), process CPU and the thread pool backlog. Then decide
processes per machine.
