# Load test results

Run on 2026-11-13 with `loadtests/run.py` at commit following `4de3859`.

**Read this first.** These numbers measure the server's bookkeeping: reserving leads,
processing provider webhooks, the job queue and the read API. They do **not** measure
live calls. A live call needs a telephony provider, a speech provider and a model, and
none is available in this environment. How many simultaneous conversations one process
can hold is therefore still unknown, and nothing below should be quoted as call
capacity.

## Setup

| | |
| --- | --- |
| Machine | 4 virtual CPUs, Linux, Python 3.13. Server, database and load generator all on this one machine, so they compete for the same CPUs |
| Server | One uvicorn process, default settings |
| Databases | PostgreSQL 16.15 (local, default configuration, pool 20 + 40) and SQLite (WAL) |
| Data | One campaign, 1,600 leads; 400 leads per concurrency level |
| Levels | 5, 10, 25 and 50 concurrent actors |

Reproduce (the database named is wiped):

```bash
python loadtests/run.py --database-url postgresql+psycopg://postgres@127.0.0.1:5432/callagent_load --out results.json
python loadtests/run.py --database-url sqlite:////tmp/load.db --out results.json
```

## Correctness under load

The same at every level, on both databases:

| Check | Result |
| --- | --- |
| Leads contested by two simultaneous dial attempts (400 per level) | 400 calls opened, 400 attempts refused, **0 leads with two calls** |
| Status webhooks, each call's final status delivered twice (2,000 requests per level, correctly signed) | 400 calls completed, **400 usage rows** (one per call), 0 errors |
| Background jobs (500 per level, as many workers as the level) | 500 ran, **0 ran twice** |
| HTTP errors | 0 |

## Timings: PostgreSQL

| Scenario | Level | p50 | p95 | max | Throughput |
| --- | --- | --- | --- | --- | --- |
| Reserve a lead and open its call | 5 | 22 ms | 42 ms | 59 ms | 210 /s |
| | 10 | 47 ms | 86 ms | 120 ms | 207 /s |
| | 25 | 100 ms | 207 ms | 278 ms | 213 /s |
| | 50 | 166 ms | 431 ms | 596 ms | 217 /s |
| Status webhook (HTTP, signature checked) | 5 | 16 ms | 40 ms | 151 ms | 246 /s |
| | 10 | 35 ms | 85 ms | 114 ms | 234 /s |
| | 25 | 85 ms | 202 ms | 233 ms | 233 /s |
| | 50 | 170 ms | 402 ms | 435 ms | 230 /s |
| Job queue | 5 | | | | 130 jobs/s |
| | 50 | | | | 99 jobs/s |
| `GET /api/campaigns` | 5 | 19 ms | 27 ms | 33 ms | 248 /s |
| | 50 | 134 ms | 223 ms | 227 ms | 253 /s |
| Campaign report (1,600 calls) | 5 | 170 ms | 333 ms | 355 ms | 24 /s |
| | 50 | 1,375 ms | 2,196 ms | 2,221 ms | 23 /s |
| Lead list, one page of 25 | 5 | 353 ms | 437 ms | 631 ms | 14 /s |
| | 50 | 2,380 ms | 3,693 ms | 3,807 ms | 14 /s |

## Timings: SQLite

| Scenario | Level | p50 | p95 | max | Throughput |
| --- | --- | --- | --- | --- | --- |
| Reserve a lead and open its call | 5 | 5 ms | 37 ms | 333 ms | 426 /s |
| | 50 | 8 ms | 1,138 ms | 2,105 ms | 364 /s |
| Status webhook | 5 | 11 ms | 22 ms | 948 ms | 350 /s |
| | 50 | 133 ms | 221 ms | 1,427 ms | 317 /s |
| Job queue | 5 | | | | 232 jobs/s |
| | 50 | | | | 174 jobs/s |
| Campaign report (1,600 calls) | 1 (serial) | 26 ms | 153 ms | | 26 /s |
| | 5 | 2,222 ms | 2,415 ms | | **2.3 /s** |
| | 50 | 17,412 ms | 27,285 ms | | **1.9 /s** |
| Lead list, one page of 25 | 5 | 3,502 ms | 3,765 ms | | **1.4 /s** |
| | 50 | 27,536 ms | 42,805 ms | | **1.2 /s** |

## What the numbers say

1. **Throughput is flat from 5 to 50 actors** (about 210 reservations and 230 webhooks
   per second on PostgreSQL). More concurrency adds waiting, not work done: the limit is
   one Python process sharing four CPUs with the database and the load generator. A
   call produces roughly five webhooks and one reservation over its whole life, so
   bookkeeping is far from being the constraint for 50 concurrent calls. Audio
   processing will be; that is the measurement still missing.

2. **The lead list and the report are the slow endpoints**, as the audit predicted
   (D-6). Both load every lead and every call of the campaign into Python, then filter.
   A page of 25 leads costs 350 ms with 1,600 calls and grows with the campaign.
   Filtering and paging in SQL is the fix.

3. **SQLite collapses under concurrent reads of those two endpoints.** One request at a
   time: 26 per second. Five at a time: 2.3 per second, each taking over two seconds.
   The same code on PostgreSQL does not do this. The effect reproduces outside the
   server with five threads. Shortening the interpreter's thread switch interval from
   5 ms to 0.2 ms doubles throughput (2.4 to 4.5 per second), so part of it is threads
   handing the interpreter back and forth once per row fetched; the rest is not
   explained yet. Consequence: **SQLite is for one operator. A deployment with several
   dashboard users needs PostgreSQL**, and fixing D-6 would shrink the problem on both.

4. **SQLite write latency has a long tail** (maximum 1 to 2 seconds while p50 stays
   under 10 ms): one writer at a time, the others wait. No write failed.

## Not measured

| Item | Why |
| --- | --- |
| Concurrent live calls at 5, 10, 25, 50 | Needs telephony, speech and model credentials |
| AI analysis (post-call review) throughput | Needs a model credential |
| Document ingestion and campaign scheduling under load | Not written yet |
| A database on another machine, a larger pool, several server processes | Single machine available |
| Multi-hour soak, memory growth | Not run |

First measurement to make once credentials exist: one process, real calls ramped
5, 10, 25, 50, recording per-call response latency (already stored on every call),
process CPU and the audio thread's backlog. Then decide processes per machine.
