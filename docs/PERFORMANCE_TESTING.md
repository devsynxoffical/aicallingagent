# Performance testing

How performance is measured here, what the measurements cover, and what they do not.
The numbers themselves are in `LOAD_TEST_RESULTS.md`.

## The rule

No capacity is claimed that was not measured. In particular: **the number of
simultaneous phone conversations one process can carry has not been measured**,
because that needs a phone provider, a speech provider and an AI provider, and no
credentials for any of them exist in the environment this was built in. Everything
below measures the engine's bookkeeping around calls, not calls.

## What is measured

`loadtests/run.py`, against a real server process and a real database, at 5, 10, 25
and 50 concurrent actors:

| Scenario | What it does | What it checks besides time |
| --- | --- | --- |
| `claim` | Reserves leads for calls; every lead is contested by two attempts at once | Exactly one call per lead; the loser is refused |
| `webhooks` | Provider status callbacks over HTTP, correctly signed, the final status delivered twice | Every call ends once; one ledger line per call |
| `jobs` | The background job queue, as many workers as the level | No job runs twice |
| `api` | The campaign list, the campaign report, one page of the lead list | No errors |

One campaign with 1,600 leads; 400 leads per level. Server, database and load
generator share one machine and its CPUs, so the figures are a floor for a real
deployment with a separate database, and say nothing about network latency.

Unit-level measurements, in the documents where they belong:

| What | Where |
| --- | --- |
| Knowledge index build and search time on 5,000 passages | `AI_ORCHESTRATION.md`, section 7 |
| The number of database queries behind a page of leads does not grow with the list | `tests/test_intelligence.py` |
| A full background thread pool does not delay call-path database work | `tests/test_events_and_queues.py` |
| A saturated analysis queue does not delay call-related jobs | `tests/test_events_and_queues.py` |

## How to run it

The database named is wiped.

```bash
python loadtests/run.py --database-url postgresql+psycopg://postgres@127.0.0.1:5432/callagent_load --out results.json
python loadtests/run.py --database-url sqlite:////tmp/load.db --out results.json
```

About three minutes on PostgreSQL. Run it on an otherwise idle machine: a test
suite running next to it changes the numbers noticeably (it did here, and that run
was discarded).

To compare two versions, run both on the same machine on the same day. Figures
recorded on different days on a shared machine differ by tens of percent for no
reason in the code: the same old commit measured 210 reservations per second on one
day and 136 on another.

## What is not measured, and how it will be

| Not measured | Needs | How |
| --- | --- | --- |
| Concurrent live calls (5, 10, 25, 50): response latency, CPU per call, audio backlog | Phone, speech and AI credentials; a number to call | Ramp real calls against one process. Every call already records its latency per stage and end to end; the metrics endpoint exports the response time, thread pool backlog and database pool use. Decide processes per machine from the knee |
| AI review throughput and cost per call | An AI credential | Run reviews over stored transcripts; read `ai_usage` |
| Provider rate limits and behaviour on overload | Provider accounts | Read from the providers' responses under the ramp above |
| A database on another machine; a connection pooler; a read replica | More than one machine | Repeat `loadtests/run.py` there |
| Several engine processes | The same | Repeat with two and four processes behind a load balancer. One of them dials (the lease); the others serve webhooks, audio and the API |
| Hours of continuous load, memory growth | Time | A soak run with the metrics endpoint scraped |
| Document reading and website crawling under load | Nothing | Not written yet |
| The console (page load, rendering) | Nothing | Not measured in this repository |

## Decisions taken from measurements so far

1. **The lead list is paged in the database.** It was the slowest endpoint
   (8 requests per second with 1,600 leads, every call's transcript loaded for
   every request). It now loads the calls of the page only: about 14 times the
   throughput on PostgreSQL, and a test fixes the number of queries.
2. **The campaign report is still computed in memory** (14 per second with 1,600
   calls). It is read rarely; it is the next endpoint to move to SQL (D-4).
3. **No cache, no partitioning, no separate services.** Nothing measured asks for
   them. The bookkeeping path does about a hundred reservations and a hundred
   webhooks per second on four shared CPUs; a call produces one reservation and
   about five webhooks over its whole life.
4. **SQLite is for one operator on one machine.** Concurrent reads of the heavy
   endpoints collapse on it (`LOAD_TEST_RESULTS.md`). Any deployment with several
   console users, or more than one organization, runs on PostgreSQL.
5. **The correctness added since the first measurement costs throughput**, and that
   was accepted: organization checks and row-level security on every transaction,
   an event and a ledger line per call ending, the cross-campaign check and budget
   lock per reservation. Measured on the same machine on the same day: about a
   fifth fewer reservations and webhooks per second than before that work.
