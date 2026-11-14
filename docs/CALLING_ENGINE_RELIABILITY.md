# Calling engine reliability

How a call is kept correct when something around it fails. This describes what the
code does today. Ids such as B-5 refer to `ENTERPRISE_BACKEND_AUDIT.md`.

**What has and has not been checked.** Everything below is covered by automated
tests in which the phone provider, the speech services and the AI model are
stand-ins. No call has been placed through a real provider from this repository: no
provider credentials exist in the environment it was built in. The behaviour of a
real carrier, real audio and a real outage is therefore unverified, and the section
"Not verified" at the end lists what must be checked on real calls before launch.

## 1. One call, one record, one direction of travel

A call is a row in `call_runs` with a status that only moves forward:

```
created → queued → initiated → ringing → in-progress → completed
                                   └──────────────────→ busy | no-answer | failed | canceled
```

- `dialer/lifecycle.py` is the only code that changes a status. A change that would
  move a call backwards is refused, so a late or repeated provider report cannot
  reopen a finished call.
- Every status has a source: the provider's report or the engine's own inference.
  The provider's word replaces the engine's inference; the reverse never happens.
- A database CHECK constraint lists the allowed statuses (migration 0007).
- A lead can have one live call at a time. That is a unique index on
  (lead, live status), not a check in code, so two requests racing to dial the same
  lead cannot both win.
- The same phone number cannot be rung by two campaigns of one organization at once
  (B-4): the reservation of a lead looks for a live call to the same number first.

## 2. Placing a call

`dialer/campaign_runner.py`, one loop per running campaign.

1. Rules are checked before every call (`dialer/rules.py`): campaign dates, calling
   days and hours in the lead's time zone, calls per day, concurrent calls, the
   booked-meetings target, the opt-out rate, the provider-failure rule (section 6)
   and the budget.
2. The lead is reserved and the call record created in one transaction.
3. The do-not-call list of the organization is checked inside that transaction.
4. The provider is asked to dial with a timeout (`PROVIDER_TIMEOUT_SECONDS`, 15 s).
5. If that request times out, **the call is not placed again**. It keeps its record
   without a provider id, and the reconciler (section 5) asks the provider whether a
   call to that number exists before anything else is decided.

## 3. Provider webhooks

- Signatures are verified (`TWILIO_VALIDATE_SIGNATURE`, on by default).
- A status report is applied with the forward-only rule, so the same report
  delivered twice, or two reports out of order, end in the same state.
- The usage record of a call is written once: the table has a unique index on
  (call, kind).
- An inbound call to a number that has no campaign, or when the public address is
  not configured, is answered with a spoken refusal; no call record is left half-made.

## 4. The audio connection

- The provider opens a WebSocket carrying a one-time token created with the call.
- The token is claimed with a single conditional update (B-10). Of any number of
  connections presenting the same valid token, exactly one gets the call; the test
  opens many at once.
- A connection that does not complete its opening exchange within 10 seconds is
  closed (code 4008), so a stalled or hostile connection does not hold a slot.
- When the audio starts, the call is marked in progress, an event `CallAnswered` is
  recorded, and a job is queued for a time after the call's maximum length. That
  job finishes the call if nothing else does (section 5).

## 5. Finishing a call, whatever happens

A call must end with an outcome, a usage record, and its lead released. Three
mechanisms make that true independently of each other:

| If | Then |
| --- | --- |
| The call ends normally | The pipeline saves the transcript, writes usage, reviews the call and updates the lead (`postcall/summarizer.py`) |
| The review cannot be done (model unavailable, process stops) | A queued job reviews the call later from the stored transcript; the lead's place in the queue was already decided |
| The process stops during the call, or the normal ending fails | The queued finalize job gives the call an outcome and releases the lead (B-2). It refuses to act on a call that may still be in progress |
| A webhook is lost, or the dial request timed out | The reconciler (`dialer/reconcile.py`, every 60 s) asks the provider. Only the provider's answer changes anything: if the provider cannot be reached, nothing changes and it is asked again |

The transcript is saved as the call proceeds, not only at the end, so a call that is
cut off still has what was said up to that point.

One call that cannot be reconciled does not stop the pass (B-9). A provider call
found by phone number that already belongs to another record is not adopted.

## 6. When a provider is failing

**Phone provider.** A dial request that fails is recorded on the call and the lead
goes back to the queue with a delay. Nothing is retried in a tight loop.

**Speech or AI provider during calls (B-5).** If the last five answered calls of a
campaign, since it was last started, all ended with no agent speech or with a
failure during the call, the campaign stops itself and says why. Calls that reached
an outcome only a conversation produces (a meeting, "not interested", an opt-out)
count as working. Starting the campaign again starts the count again. Without this
rule a campaign would ring through its whole list during an outage, several times
each, with a silent line every time.

**The AI model during a call (B-6).** The live conversation uses its own client:
connecting may take 3 seconds, a silence in the middle of an answer 15 seconds, with
one retry (`CALL_MODEL_CONNECT_TIMEOUT_SECONDS`, `CALL_MODEL_READ_TIMEOUT_SECONDS`).
The library's default is ten minutes with two retries. When a turn fails, the caller
hears a fixed sentence asking them to repeat, in the call's language, spoken without
the model. A second failure on the same call ends it politely and marks the call as
failed, which the rule above counts.

**The AI model outside calls.** Reviews, coaching and training runs go through one
module with a limit on how many run at once (default 2), so they cannot use up the
provider capacity that live conversations need. See `AI_ORCHESTRATION.md`.

What is **not** built: automatic switching to another speech or AI provider. A
fallback provider has a different voice, different language coverage and different
latency; switching silently in the middle of a campaign would change what callers
hear without anyone deciding it. The capability check at start-up refuses a
language the configured provider cannot handle rather than degrading.

## 7. Isolation from everything else

| A problem in | Cannot affect live calls because |
| --- | --- |
| Call reviews, coaching, training, document reading | They run on a separate thread pool (`BACKGROUND_THREADS`) from the one the call path uses (`DB_THREADS`), and in their own job queues with their own limits (B-1, D-10) |
| A backlog of jobs for one organization | Jobs are taken in turn across organizations |
| A slow knowledge search | The index is built once per campaign and reused; searches run off the audio loop |
| A handler for an event | Each consumer of an event is its own job with its own retries and dead letter |
| The console or reports | They read through the same database, but on request threads; they share no lock with the call path beyond the database itself |

One process still carries the API, the dial loops, the audio and the background
workers (D-6). The pools and queues separate them inside the process; a crash of the
process takes all of them down together, and sections 5 and 8 are what recover from
that.

## 8. Restart

On start: migrations are applied or verified; interrupted training runs are closed
or resumed; documents accepted but not read are queued again; the job worker takes
over jobs whose lease expired; the reconciler starts. Calls that were in progress
are finished by their queued finalize job once their maximum length has passed.

Stopping the process does not yet wait for calls in progress to end (B-8, E-7): a
deployment during calling hours cuts those calls off, and they are then finished by
the mechanism above. Deploy outside calling hours until that is built.

## 9. In-call actions

What the agent may do during a call is decided on the server, not by the model
(`voice/tools.py`):

- Only the tools the campaign's playbook enables are offered, and a tool that was
  not offered is refused if the model names it anyway.
- Bookings and callbacks must be real future times within 180 days; at most two
  distinct meeting times per call.
- A text message: one per call, never after an opt-out, and links only to web hosts
  that appear in the approved playbook.
- A transfer to a person is recorded only after the provider accepted it (B-3).
- An opt-out takes effect through two routes: the agent's tool during the call, and
  the review of the transcript after it (B-12). Either is enough, and the number
  goes on the organization's do-not-call list. A reply of STOP to a text message is
  handled by the phone provider only; the engine is not told about it yet.
- A tool that fails tells the model what to say, not what broke.

## 10. Measured, and not

Measured in automated tests on this machine (no provider involved):

- Of 12 simultaneous claims of the same token, 1 is accepted.
- With the background pool full, a call-path database operation completes in its
  normal time.
- A knowledge search over 5,000 synthetic passages takes about 1.2 ms, and building
  that index about 1.2 s once per change of the campaign's documents (previously
  65 ms per search and a rebuild on every call).

Not measured: response latency on real calls, audio quality, behaviour at more than
a handful of concurrent calls, and provider rate limits. The latency of each call
(speech-to-text, model first token, voice first audio, end-to-end response,
interruption) is recorded on the call and reported per campaign, so those numbers
exist from the first real call on.

## 11. Not verified, to check on real calls before launch

1. A real outbound and a real inbound call end to end, in each language sold.
2. A dial request that times out at the provider: confirm the reconciler finds the
   call and no second call is placed.
3. Kill the process during a call: confirm the call is finished and the lead
   released after the configured time.
4. Revoke the speech key during a campaign: confirm the campaign stops after five
   silent calls and the reason shown is understandable.
5. Block the AI provider during a call: confirm the spoken fallback is heard, in the
   right language, and how long the silence before it is.
6. Answering-machine detection accuracy on the numbers and carriers actually used.
7. Recording and consent wording per market (B-13): a legal decision, not a
   technical one.

## 12. Open items

| Item | State |
| --- | --- |
| B-7, D-6 Several processes | Not built. One process only; a second instance would double pacing and budgets |
| B-8, E-7 Draining on shutdown | Not built |
| B-13 Recording and consent per market | Needs a decision per market |
| B-14 Latency alarms | Measurements exist per call; no alarm yet (phase 6) |
| B-15 Provider fallback | Deliberately not automatic (section 6) |
| B-16 Admission control | Concurrency is limited per campaign; there is no limit across campaigns of the deployment yet |
