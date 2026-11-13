# Reliability: what is guaranteed, how, and what is not yet

Every guarantee below names the test that proves it. "Not validated with a real
provider" means exactly that: no telephony, speech or model credential exists in the
environment where this was built.

## Calls

| Guarantee | How | Proven by |
| --- | --- | --- |
| A person is never rung twice at the same time | The lead is reserved by a conditional `UPDATE` in the transaction that opens the call record; a lead with any unfinished call is refused; the database allows one unfinished outbound call per lead | `test_integrity.py::test_many_simultaneous_dial_attempts...` (SQLite, PostgreSQL), load test: 0 double calls in 1,600 contested leads |
| A suppressed number is not called | The do-not-call list is read again inside the reservation, not only when the lead was picked | `test_a_number_suppressed_after_the_lead_was_picked_is_not_called` |
| Someone on the line with us is not rung | An unfinished call in either direction blocks the reservation | `test_someone_on_the_line_with_us_is_not_rung` |
| A call's status only moves forward and its ending is final | One state machine (`dialer/lifecycle.py`) that every writer uses | `test_lifecycle.py::test_every_state_and_event_pair...`, `...out_of_order_late_or_repeated...` |
| A repeated webhook changes nothing | Same state machine; the usage ledger has one row per call | `test_the_final_status_reported_any_number_of_times_is_one_usage_row`, load test |
| The same inbound webhook twice opens one call | Unique index on the provider's call id; the route reads the call the other delivery opened | `test_the_same_inbound_webhook_delivered_twice_at_once_opens_one_call` |
| No call is redialed on a guess | A quiet call is resolved by asking the provider. A create request that timed out keeps the lead reserved until the provider answers. If the provider cannot be reached, nothing changes | `test_lifecycle.py` reconciliation tests. **Not validated with real Twilio** |
| Requests to the provider cannot hang forever | 15 second timeout on the provider client | Configuration; behaviour after the timeout is the row above |
| A restart does not pause campaigns | Shutdown stops dial loops without touching campaign status | `test_a_restart_does_not_pause_running_campaigns` |
| Loose ends of paused and finished campaigns are tied up | Reconciliation runs for every campaign every minute | `test_lifecycle.py` (the fixture campaign is paused on purpose) |

Lifecycle:

```
created → queued → initiated → ringing → in-progress → completed | busy | no-answer | failed | canceled
```

End states are final, except that the provider's report replaces an ending the system
only inferred (a pipeline that could not start, a call with no provider record).

## Background work

One table, `jobs`, in the application database (`callagent/jobs.py`). A job is written
in the same transaction as the change that causes it.

| Property | Value |
| --- | --- |
| Identity | Unique `key`; enqueueing the same key again returns the existing job |
| Delivery | At least once. Handlers are written to be safe to run twice |
| Retries | Per job; wait doubles from 30 s, capped at 1 hour, plus up to 20% jitter |
| Timeout | Per attempt |
| Crash recovery | A running job carries a lease. A worker that dies stops finishing; after the lease another worker takes the job. A result from the dead worker arriving late is ignored |
| Dead letter | After the last attempt the job stays as `dead` with its error; an optional hook tells the owner of the work |
| History | One row per attempt in `job_attempts` |
| Operations | `GET /api/jobs`, `GET /api/jobs?status=dead`, `POST /api/jobs/{id}/retry` |

What runs on it:

| Work | Behaviour |
| --- | --- |
| Booking notification to the customer's system | Saved with the appointment. Delivered off the call, signed (`X-Callagent-Signature`, HMAC-SHA256 with `BOOKING_WEBHOOK_SECRET`), same `X-Callagent-Delivery` id on every retry, 8 attempts |
| Post-call review | A safety-net job is saved with the transcript. An inline review that succeeds cancels it; otherwise the queue reviews the call later from the stored transcript, 4 attempts |
| Training run | Saved with the run. A restart mid-run re-runs it; a run that cannot finish is closed as failed |

Proven by `tests/test_jobs.py` and `tests/test_background.py`.

## Data

See `docs/DATABASE_DESIGN.md` (constraints, migrations) and
`docs/DISASTER_RECOVERY.md` (backup, restore drill, objectives).

## Known gaps

| Gap | Risk | Audit |
| --- | --- | --- |
| A call's transcript is in memory until the call ends | A crash mid-call loses that call's transcript (the review safety net only helps once the transcript was saved) | C-7 (half done) |
| Two server processes on one database each run a dial loop | Double dialing is prevented by the reservation, but pacing and concurrency limits would be applied twice | A-1 |
| The single-use check on the media WebSocket is not atomic | Two connections with the same per-call token at the same instant could both attach | C-8 |
| Follow-up texts have no per-call limit | The model could send more than one | C-10 |
| Recording references | Probably never stored; needs a real account to confirm | C-11 |
| No provider failover during a call | A model or speech outage mid-call ends in silence and a goodbye | AI-4 |
| Document reading is not a queue job | It is resumed after a restart, but has no retry limit, backoff or history | J-1 (partly) |
| Slow jobs share the thread pool with live calls | A tool call on a live call can wait behind a crawl | J-4 |

## What to check on the first real calls

1. Place one call and stop the server during it: the call must be `in-progress`, then
   resolved by reconciliation with the provider's real ending and duration.
2. Block the status callback URL for one call: reconciliation must fetch its ending.
3. Make the provider time out on a create request (firewall the API for a few
   seconds): the lead must stay reserved and the call be adopted, not redialed.
4. Send the same status callback twice from the provider's console: one usage row.
5. Confirm a recording reference is stored when recording is on (C-11).
