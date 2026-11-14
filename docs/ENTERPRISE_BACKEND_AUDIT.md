# Enterprise backend audit

Scope: the calling engine (`aicallingagent`, package `callagent/`) and the server-side
parts of the console (`devytalk/apps/web`). Every finding below was read from the code
at the commit this file was written against, not from earlier documents; file and line
references are to that commit and will drift.

How to read a finding:

- **Now**: what the code does. **Weakness / cause**: why that is a problem and where
  it comes from. **Scenario**: a concrete way it fails. **Impact**: what it costs the
  business. **Fix**: the recommended change. **Size**: S (hours), M (a day or two),
  L (several days). **Needs**: what must exist first. **Done when**: the acceptance
  criterion, written so a test can check it.
- **Status** is kept current as work lands: *open*, *fixed* (with the test that shows
  it), or *accepted* (with the reason).

Priorities: **P0** critical security, data integrity or production reliability.
**P1** major architecture or performance. **P2** scalability and maintainability.
**P3** optimisation and later work.

Earlier audit: `BACKEND_AUDIT.md` (40 findings, most fixed). Findings from it that are
still open are carried over here under their original ids (for example C-8, A-1).

## Summary

69 findings: 9 at P0, 29 at P1, 28 at P2, 3 at P3.

| Area | P0 | P1 | P2 | P3 |
| --- | --- | --- | --- | --- |
| A. Organization isolation and access | 6 | 5 | 3 | 1 |
| B. Calling engine | 2 | 6 | 8 | 1 |
| C. AI orchestration and knowledge | 0 | 8 | 7 | 1 |
| D. Database, background jobs and billing | 1 | 6 | 5 | 0 |
| E. Observability, security surface and deployment | 0 | 4 | 5 | 0 |

The nine P0 findings, in the order they are worked:

1. A-1 to A-6: nothing separates one customer's data from another's.
2. B-1: slow analysis work can stop live calling.
3. B-2: a call interrupted by a restart is never finished.
4. D-1: the usage ledger holds estimates, and only for telephony.

What could not be judged from code alone, and needs real provider credentials: how
the speech providers behave during an outage and reconnect; the phone provider's
exact status values and timing; the AI provider's actual rate limits; audio latency
under concurrent calls.

## Order of work

| Phase | Findings | Why this order |
| --- | --- | --- |
| 2. Organization isolation and database integrity | A-1 to A-10, A-14 (part), D-8, D-9 | Everything else attributes work and cost to an organization |
| 3. Events and background processing | D-10, D-11, C-11/D-7, B-2 | Calling and analysis must not share fate |
| 4. Calling engine | B-1, B-3 to B-6, B-9 to B-12, B-16 | Reliability of the core |
| 5. AI orchestration and knowledge | C-1 to C-14 | Safety and cost of the AI workflows |
| 6. Observability | E-1 to E-3, B-14, C-15 | See failures before customers do |
| 7. Billing | D-1 to D-3 | Needs organizations (2) and AI usage (5) |
| 8. Performance | D-4, D-6, C-8 | Measured after the structural fixes |
| 9. Recovery | E-4 | |
| 10. Delivery | E-5, E-6, E-7, E-8, E-9, D-5 | |
| Later, needs decisions or providers | A-11, A-13 (reset, second factor), B-13, B-15, C-13 (embeddings) | |

--- | --- | --- | --- | --- |
| A. Organization isolation and access | 6 | 5 | 3 | 1 |
| C. AI orchestration and knowledge | 0 | 8 | 7 | 1 |
| D. Database, background jobs and billing | 1 | 6 | 5 | 0 |
| E. Observability, security surface and deployment | 0 | 4 | 5 | 0 |

(Area B, the calling engine, is added below when its review completes.)

---

## A. Organization isolation and access

The engine was built for one organization. Accounts and roles exist, and several
tables carry an `organization_id`, but no request knows which organization its caller
belongs to, so nothing is filtered by it.

### A-1 (P0) Requests carry no organization

- **Now**: `Principal` (`accounts/__init__.py`) has a role and a user id, no
  organization. `require_api_token` (`apideps.py`) authenticates and checks the role
  only.
- **Weakness / cause**: authorization answers "may this role do this", never "is this
  theirs". The model predates multiple organizations.
- **Scenario**: a viewer of organization B calls any route with an identifier of
  organization A and gets A's data.
- **Impact**: complete loss of confidentiality between customers; blocks selling to a
  second customer.
- **Fix**: put `organization_id` on the principal (from the user's row), make it
  available to every query helper, and make "not yours" indistinguishable from "does
  not exist" (404).
- **Size**: M. **Needs**: nothing.
- **Done when**: a test signs in as a user of organization B and calls every `/api`
  route with organization A's identifiers; every answer is 404 (or an empty list) and
  nothing of A's changes.

### A-2 (P0) Campaigns are looked up globally, by name or by number

- **Now**: `apideps.get_campaign` returns `s.get(Campaign, int(x))` when the path
  segment is all digits, else the campaign with that name, in any organization.
  About 35 routes use it. `campaigns.name` is unique across the whole deployment.
- **Weakness / cause**: one helper, no ownership check; the digit shortcut also makes
  a campaign named "17" resolve to campaign id 17.
- **Scenario**: organization B reads A's leads, transcripts and playbooks, or pauses
  A's dialing, by naming A's campaign. Two customers cannot both have a campaign
  called "Q4".
- **Impact**: cross-customer read and write of everything a campaign owns.
- **Fix**: resolve by `(organization_id, name)` only; drop the numeric shortcut;
  unique index on `(organization_id, name)` instead of `name`.
- **Size**: M. **Needs**: A-1.
- **Done when**: two organizations each have a campaign with the same name and each
  sees only its own; a numeric path segment is treated as a name.

### A-3 (P0) `POST /api/campaigns` overwrites a campaign found by global name

- **Now**: `server.py create_campaign` loads `get_campaign_by_name(body.name)` and
  records the posted playbook as its active version.
- **Scenario**: organization B posts a playbook under a name A already uses and
  replaces what A's agent says on live calls.
- **Impact**: one customer can change another's live sales script.
- **Fix**: same lookup as A-2; a new campaign is created in the caller's organization.
- **Size**: S. **Needs**: A-2.
- **Done when**: the cross-organization test in A-1 covers this route and A's active
  version number is unchanged afterwards.

### A-4 (P0) Rows fetched by primary key without an ownership check

- **Now**: `GET /api/calls/{id}` (`s.get(CallRun, id)`), `POST /api/jobs/{id}/retry`
  (`jobs.retry_dead`), `PATCH /api/users/{id}`, `POST /api/requests/{id}/handled`.
- **Scenario**: counting upwards through call ids returns every customer's
  transcripts, summaries and recording links; an admin of B resets the password of a
  user of A and signs in as them.
- **Impact**: data disclosure and account takeover across customers.
- **Fix**: every fetch by id also filters by the caller's organization.
- **Size**: S each. **Needs**: A-1; `jobs` needs an organization column (A-7).
- **Done when**: covered by the A-1 test for each of these routes.

### A-5 (P0) Lists that span every organization

- **Now**: `GET /api/campaigns`, `/api/timeline`, `/api/jobs`, `/api/requests` and
  `/api/users` select without an organization filter.
- **Scenario**: every customer sees every other customer's campaign names, call
  volumes, team members and failed-job error text.
- **Impact**: disclosure of customer lists and activity.
- **Fix**: filter each by the caller's organization.
- **Size**: S. **Needs**: A-1.
- **Done when**: with two organizations populated, each list returns only the
  caller's rows.

### A-6 (P0) Accounts are always created in the default organization

- **Now**: `accounts.create_user` sets `organization_id=default_organization_id(s)`.
  There is no way to create an organization.
- **Scenario**: a second customer cannot be onboarded at all without sharing the
  first one's organization.
- **Impact**: the product cannot serve more than one customer.
- **Fix**: users are created in the acting admin's organization; organizations are
  created (with their first admin) only by the platform operator.
- **Size**: M. **Needs**: A-1.
- **Done when**: the platform operator creates two organizations with one admin
  each; each admin can add people only to their own.

### A-7 (P1) Tables without an organization of their own

- **Now**: `leads`, `call_runs`, `appointments`, `jobs`, `do_not_call` have no
  `organization_id`; `campaigns.organization_id` and `usage_records.organization_id`
  are nullable. Leads and calls reach their organization only through a join on the
  campaign.
- **Weakness / cause**: filtering the two largest tables by organization needs a
  join every time, row-level security cannot be expressed on them, and a job cannot
  be listed or retried by its owner.
- **Scenario**: a report across an organization's calls joins every call to its
  campaign; a database-level policy cannot protect the tables that hold transcripts.
- **Impact**: slower organization-wide queries and no defence in depth on the most
  sensitive tables.
- **Fix**: add `organization_id` (NOT NULL, indexed, backfilled from the campaign)
  to these tables; fill it in one place when a row is created.
- **Size**: M. **Needs**: nothing.
- **Done when**: the migration backfills an existing database, no row has a null
  organization, and creating any of these rows without one fails.

### A-8 (P1) The do-not-call list is shared by all organizations

- **Now**: `do_not_call.phone_e164` is globally unique; `is_dnc` checks the one list.
- **Scenario**: a person opts out with customer A; customer B's import report and
  dial errors reveal that the number is on a list, and B cannot call someone who
  never opted out of B.
- **Impact**: leaks one customer's opt-outs to another and blocks legitimate calls.
  (An opt-out is owed to the seller that was asked, so per-organization is also the
  legally expected scope.)
- **Fix**: `organization_id` on the list, unique `(organization_id, phone_e164)`.
- **Size**: S. **Needs**: A-7.
- **Done when**: a number suppressed in A is still dialable in B and never dialable
  in A.

### A-9 (P1) Inbound numbers can be claimed by anyone, and a conflict names the owner

- **Now**: `set_inbound_number` accepts any number not already taken and answers 409
  with the other campaign's name; `INBOUND_CAMPAIGN` routes unowned numbers to one
  campaign.
- **Scenario**: organization B learns A's campaign name from the 409, or claims a
  number that A is about to configure.
- **Impact**: minor disclosure; misrouted inbound calls. Proper number ownership
  needs per-organization telephony (A-11).
- **Fix now**: generic 409 across organizations. **Later**: verify ownership against
  the organization's own phone account.
- **Size**: S now. **Needs**: A-1.
- **Done when**: the 409 seen from another organization names nothing.

### A-10 (P1) The engine API token is one secret with full access to everything

- **Now**: `API_TOKEN` (or `DATA_DIR/api_token`) authenticates as an admin with no
  organization.
- **Scenario**: the token leaks from one operator's shell history and every
  customer's data is exposed.
- **Impact**: single point of total compromise.
- **Fix**: treat it as the platform operator's credential: it manages organizations
  and may act inside one it names explicitly; customers never receive it. Per-
  organization API keys (hashed, revocable) for customer automation.
- **Size**: S (operator role) + M (per-organization keys). **Needs**: A-1.
- **Done when**: only the operator credential can create organizations; an
  organization's API key reaches only that organization.

### A-11 (P1) Telephony, booking and transfer settings are per deployment

- **Now**: one Twilio account and caller number, one `BOOKING_WEBHOOK_URL` and
  secret, one `HUMAN_TRANSFER_NUMBER`, one set of cost rates (`config.py`), used for
  every campaign (`voice/tools.py`, `background.py`, `dialer/rules.py`).
- **Scenario**: with two customers, B's booked meetings (lead names, numbers, times)
  are posted to A's webhook and B's prospects are transferred to A's staff.
- **Impact**: personal data sent to the wrong company; wrong caller id; identical
  prices for every customer.
- **Fix**: an organization-settings record (booking webhook and secret, transfer
  number, caller number or phone sub-account, cost rates), with the deployment
  values as defaults for the first organization only.
- **Size**: L. **Needs**: A-1, A-6.
- **Done when**: two organizations with different webhooks each receive only their
  own bookings in a test, and a second organization without settings cannot dial.

### A-12 (P2) Sign-in throttling can be used against a person, and tells accounts apart

- **Now**: five wrong passwords lock that account for 15 minutes and answer 429; an
  unknown email answers 401. There is no per-address limit on the engine.
- **Scenario**: an attacker keeps a known admin locked out, or tests which emails
  have accounts by watching for 429.
- **Fix**: limit by network address as well as by account, and answer the same way
  whether or not the account exists.
- **Size**: S. **Needs**: nothing.
- **Done when**: six wrong passwords for an unknown email and for a real one produce
  the same sequence of answers.

### A-13 (P2) No password reset by email, no second factor, no audit trail of admin actions

- **Now**: passwords are reset by an admin or the command line; nothing records who
  changed a role, deactivated an account or approved a playbook beyond the version's
  `approved_by`.
- **Fix**: an append-only audit log of privileged actions (who, what, when, from
  where); self-service reset and optional TOTP once email sending exists.
- **Size**: M (audit log), L (reset and second factor). **Needs**: A-1.
- **Done when**: every privileged route writes one audit row, readable by the
  organization's admins and by nobody else.

### A-14 (P2) Background work has no organization context or fairness

- **Now**: the dial loop, the job worker and the reconciler work across all rows;
  the reconciler takes the 200 oldest quiet calls deployment-wide.
- **Scenario**: one customer with thousands of stuck calls delays reconciliation for
  everyone else.
- **Fix**: organization on jobs (A-7), per-organization batches and limits.
- **Size**: M. **Needs**: A-7.
- **Done when**: a backlog in one organization does not delay another's jobs in a
  test.

### A-15 (P3) Small disclosures

- `/docs` and `/openapi.json` are public; `/health` lists missing setting names; the
  401 text names the token file; creating a user answers 409 for an email used in
  another organization.
- **Fix**: serve the API description to signed-in callers only; keep `/health` to
  "ok" and move detail to the authenticated status route.
- **Size**: S.

---

## B. Calling engine

Verified as sound and left alone: the call state machine only moves forward, end
states are final except that the provider's report overrides one the engine
inferred, and duplicate, late and out-of-order reports are ignored; a lead is
reserved with a conditional update and a partial unique index, safe across
processes; a timeout or server error from the phone provider keeps the lead
reserved (only a clear refusal releases it), the provider client does not retry on
its own, and reconciliation changes nothing while the provider is unreachable; the
provider's call id is unique and a repeated inbound webhook reuses the same call;
all three provider webhooks check the signature; the media socket needs the
per-call token, a live call and the matching call id; a disconnect cancels the
pipeline and finalizes the call; idle and session timeouts exist; a failing tool
never crashes a call; the transcript is saved during the call and never shortened;
language gaps block campaign start, test dials and inbound assignment.

### B-1 (P0) Live calls share one small thread pool with slow analysis work

- **Now**: every database operation of a live call (socket authorization, tool
  handlers, transcript saves) and of a provider webhook runs through `run_db` on
  the default executor, `min(32, CPUs + 4)` threads. The same pool runs the
  post-call review's model call, website crawls, the coach and language phrasing,
  some of them while holding a database session, with a ten-minute default timeout.
- **Weakness / cause**: one pool for work with millisecond deadlines and work that
  takes minutes. (The earlier audit rated this P2; with the measurements in
  `LOAD_TEST_RESULTS.md` and the missing timeouts it is P0.)
- **Scenario**: several calls end while the AI model is slow. Their reviews hold
  every thread. The provider's "call answered" webhook waits, exceeds the
  provider's 15 seconds and the call fails; tools and transcript saves on every
  other live call stall.
- **Impact**: an analysis slowdown becomes an outage of live calling.
- **Fix**: a dedicated executor for call-path and webhook database work, a separate
  bounded one for background work, no model call inside a database session,
  explicit model timeouts (C-6), and the review moved to the job queue.
- **Size**: M. **Needs**: nothing.
- **Done when**: with the background executor completely blocked in a test, a
  call-path database operation completes in its normal time.

### B-2 (P0) A call cut off by a restart, crash or failed finalize is never finalized

- **Now**: `finalize_call` runs only at the end of the media-socket coroutine, and
  the review job is enqueued only inside it. The reaper for stale calls skips calls
  that connected, and runs only from the tick of a running campaign.
- **Scenario**: a deploy during live calls. Those calls have no outcome and no
  review; their leads are redialed after the retry delay whatever was said,
  including people who asked not to be called. In a paused or inbound-only campaign
  the lead stays "calling" forever and cannot be dialed even manually.
- **Impact**: lost outcomes, repeat calls to people who already answered, stuck leads.
- **Fix**: when the stream connects, enqueue a durable "finish this call" job that
  becomes due after the call's maximum length; normal finalize completes it; if the
  process died, the job finalizes from the transcript saved so far. The reconciler
  finalizes ended, connected calls that have no outcome, whatever the campaign's
  state.
- **Size**: M. **Needs**: nothing (the transcript is already saved during the call).
- **Done when**: a test abandons a connected call mid-way and the job later sets an
  outcome, enqueues the review and releases the lead, in a paused campaign too.

### B-3 (P1) A failed transfer to a person is recorded as successful

- **Now**: `transfer_to_human` marks the call "transferred to human" before asking
  the provider to transfer.
- **Scenario**: the provider refuses; the caller stays on a line where the agent has
  stopped responding, and the lead is marked completed.
- **Fix**: record the transfer only after the provider accepts it; on failure tell
  the model so it can apologise and continue.
- **Size**: S. **Done when**: a refused transfer leaves the call active and unmarked.

### B-4 (P1) The same person can be rung by two campaigns at once

- **Now**: the reservation checks for a live call on the lead row; the same phone
  number in another campaign is another lead.
- **Fix**: refuse the reservation while any live call to that number exists in the
  organization.
- **Size**: S. **Done when**: two campaigns holding the same number cannot both
  hold a live call to it.

### B-5 (P1) No stop when a provider is failing

- **Now**: nothing watches for consecutive failures. A speech or model outage makes
  each answered call silent; it is recorded as "no answer" and the lead is retried.
- **Scenario**: a revoked key at 9:00; by 9:30 a few hundred people have answered a
  silent call from the customer's number, several times each.
- **Impact**: reputation damage and wasted spend, automatically, at scale.
- **Fix**: a circuit breaker per campaign: after N consecutive calls that connected
  but produced no agent audio or ended with a pipeline error, the campaign pauses
  itself with that reason. Provider health probes before dialing come with phase 6.
- **Size**: M. **Done when**: N simulated silent calls pause the campaign and the
  pause reason names the cause.

### B-6 (P1) No explicit timeouts on the live path

- **Now**: the live model client has the library default (ten minutes); tool calls
  can wait the provider's 15 seconds or SQLite's busy timeout; the media socket
  waits indefinitely for its first messages before any authentication.
- **Fix**: a short model deadline with a spoken fallback, a tool deadline, and a
  deadline on the socket handshake.
- **Size**: S. **Done when**: a socket that sends nothing is closed after the
  deadline; a model that does not answer produces the fallback line (needs a real
  call to confirm end to end).

### B-7 (P1) Several processes multiply pacing, limits and budget

Same as D-6. Per-lead duplicates are prevented, but each process runs its own loop,
so concurrency, daily limits and budget are each exceeded once per process.

### B-8 (P1) No draining on shutdown

Same as E-7. Also the provider's instructions have nothing after the media stream,
so a caller whose stream ends abnormally hears silence and a hang-up.

### B-9 (P2) One bad call stalls reconciliation

- **Now**: part of the per-call handling in `reconcile_calls` is outside the
  `try`; adopting a provider call found by number can collide with the unique call
  id when that call belongs to another record.
- **Scenario**: the pass raises on the same call every minute and nothing after it
  is ever reconciled.
- **Fix**: isolate each call; skip provider ids that are already owned.
- **Size**: S. **Done when**: a call that raises is skipped and the rest of the
  batch is processed.

### B-10 (P2) The media socket's claim on a call is not atomic (earlier C-8)

- **Now**: "not yet streamed" is checked in one transaction and set in another.
- **Fix**: one conditional update that claims the call during authorization.
- **Size**: S. **Done when**: two simultaneous sockets for one call: exactly one is
  accepted.

### B-11 (P2) Ended calls can linger as active

- **Now**: finalize sets the end time but not the status; if the provider's final
  report is lost, or an inbound number has no status callback, the call holds a
  concurrency slot until the time limit plus five minutes.
- **Fix**: finalize moves a still-active call to an inferred end state through the
  state machine (the provider can still correct it).
- **Size**: S.

### B-12 (P2) An opt-out depends on the model choosing the right tool

- **Now**: if the model logs the outcome "do not call" instead of calling the
  do-not-call tool, or only the review detects it, the lead is completed but the
  number is not added to the list, so another campaign can call it.
- **Fix**: finalize adds the number whenever the outcome is "do not call".
- **Size**: S. **Done when**: an outcome of "do not call" from either source
  suppresses the number.

### B-13 (P2) Recording and consent

- **Now**: recording starts when the call is answered, before the spoken
  disclosure; the disclosure is a prompt instruction and is not verified; an
  objection ends the call but the recording is neither stopped nor deleted; only a
  link from the status report is stored; there is no retention period for
  recordings, transcripts or notes; inbound calls are not recorded.
- **Impact**: the rules for recording and for AI disclosure differ by country and
  state. A single global behaviour cannot be compliant everywhere.
- **Fix**: per-organization, per-market settings (record or not, disclosure
  wording, retention); stop and delete the recording on objection; a retention job.
- **Size**: L. **Needs**: A-11. **Not started.**

### B-14 (P2) Latency: what is measured and what is missing

- **Now**: per call, count, median, 95th percentile and maximum of speech-to-text
  first result, model first token, voice first audio, turn end to first audio, and
  barge-in; stored at finalize (so lost in B-2).
- **Missing**: answer to the agent's first audio, call setup time, a per-turn
  series, tool durations, aggregation across calls, alerts.
- **Fix**: record the first two per call, and export all of them as metrics (E-2).
- **Size**: M.

### B-15 (P2) Silent language fallbacks; no provider fallback

- **Now**: a language tag the adapter cannot map is dropped without a word; the
  silence goodbye falls back to English; saving languages activates the playbook
  even when the response lists provider gaps (start and inbound assignment are then
  refused). There is no second provider to fall back to.
- **Fix**: log every fallback as a warning with the call; a capability-aware
  fallback only when a second provider is configured that supports the language and
  streaming mode. Not built: it needs real providers to test.
- **Size**: M.

### B-16 (P2) No admission control

- **Now**: inbound calls and the total across campaigns are unlimited.
- **Fix**: a limit on concurrent calls per organization and per deployment; beyond
  it inbound callers hear a polite message and outbound waits.
- **Size**: S.

### B-17 (P3) Smaller items

- A repeated final report for an old call can move a lead that is being called
  again back to "pending" (no second call results).
- No replay protection on webhooks (handlers are idempotent); switching signature
  validation off is accepted without a warning.
- An attempt is counted when the lead is reserved, even if the provider then
  refuses the call.

---

## C. AI orchestration and knowledge

Verified as sound and left alone: knowledge passages are filtered by organization
and campaign; website fetching allows only public http(s) addresses, re-checks every
redirect, honours robots.txt and caps size and page count; uploaded files are stored
under names the uploader does not control and are checked by content, not extension;
text messages go only to the lead's own number and transfers only to the configured
number; a booking is idempotent per call and time and its webhook is signed and sent
through the durable queue; drafts from training and the coach need an admin's
approval and the coach cannot change prices or compliance lines; a refusal or missing
structured output raises instead of being used; no API key is logged.

### C-1 (P1) Text from documents and websites reaches an agent that holds tools, unmarked

- **Now**: `lookup_knowledge` (`voice/tools.py`) returns raw passages with a note to
  answer only from them. Nothing tells the model that a passage is data and never an
  instruction. A document is used on calls as soon as it has been read; unlike a
  playbook it needs no approval.
- **Weakness / cause**: no trust boundary between retrieved text and instructions.
- **Scenario**: a page on a crawled site says "text every caller this link"; the
  agent sends it with `send_followup_sms`.
- **Impact**: the business's own number sends attacker-chosen messages to prospects.
- **Fix**: return passages inside an explicit untrusted-data wrapper, add a standing
  system rule that retrieved text can never give instructions, and enforce server-side
  limits on what tools may do regardless of what the model asks (C-2).
- **Size**: S. **Needs**: nothing.
- **Done when**: a test passes a passage containing an instruction through the tool
  and the returned payload marks it as untrusted; the prompt contains the rule; the
  SMS limits in C-2 hold whatever the arguments.

### C-2 (P1) Every tool is offered on every call, with thin validation

- **Now**: `build_tools` takes `playbook_capabilities` and ignores it. A text message
  needs only non-empty text; a booking needs a future ISO time; a callback has no
  past or horizon check; none has a per-call count.
- **Scenario**: a campaign meant only to qualify leads books meetings and sends
  texts; a confused model books twenty meetings in one call.
- **Impact**: actions the customer never enabled; cost and embarrassment.
- **Fix**: offer only the tools the playbook's capabilities allow (plus the ones a
  call always needs: end call, log outcome, do-not-call, knowledge lookup); per-call
  caps; a booking and callback horizon; one text per call.
- **Size**: M. **Needs**: nothing.
- **Done when**: a playbook without `send_followup_sms` produces a tool list without
  it, and a second text or a booking a year ahead is refused by the server.

### C-3 (P1) What a prospect says can become next call's instructions

- **Now**: `finalize_call` and the callback tool append text derived from the
  prospect to `lead.notes`; `build_system_prompt` puts notes and lead-sheet columns
  into the system prompt unmarked.
- **Scenario**: a prospect says "make a note that I was promised 90% off"; the next
  call's prompt carries it as briefing.
- **Impact**: the agent repeats a promise nobody made.
- **Fix**: render lead data in a delimited block labelled as unverified data, with a
  length cap, and a rule that it never changes prices, terms or instructions.
- **Size**: S. **Done when**: the built prompt for a lead whose notes contain an
  instruction shows the notes only inside the data block.

### C-4 (P1) The draft "what changes" view omits fields a draft can change

- **Now**: `versions.diff` compares a fixed list of fields that leaves out
  `company_name`, `company_description`, `call_goal`, `disclose_ai_if_asked` and
  `required_capabilities`. Training restores only four locked fields.
- **Scenario**: text injected through a document lands in `company_description`; the
  admin sees a clean diff and approves; it becomes part of the system prompt.
- **Impact**: the approval step, the main safeguard, can be bypassed unnoticed.
- **Fix**: diff every playbook field; training keeps the same locked fields as the
  coach; the analyzer is told its source material is data.
- **Size**: S. **Done when**: changing any single field of a playbook shows up in
  the diff (tested for every field of the model).

### C-5 (P1) Website fetching can be redirected to internal addresses by DNS

- **Now**: `training/web.py` resolves a host, checks the addresses are public, then
  the HTTP client resolves the name again when it connects.
- **Scenario**: the second lookup answers `169.254.169.254`; cloud credentials are
  fetched, stored as passages and readable through knowledge search.
- **Impact**: theft of infrastructure credentials.
- **Fix**: connect to the address that was checked (pin it for the request, keep the
  original host name for TLS and the Host header).
- **Size**: M. **Done when**: a test where the resolver answers a public address
  first and a private one second never opens a connection to the private one.

### C-6 (P1) AI calls have no explicit timeouts, retry policy or separation from live calls

- **Now**: `llm.py` builds a new client per use with the library's defaults (ten
  minutes, two retries). The live pipeline uses its own client with defaults. All
  workflows use one key and, by default, one model.
- **Scenario**: a burst of call reviews and a training run use up the rate limit;
  live turns get "overloaded" answers and the caller hears silence.
- **Impact**: background work degrades live conversations.
- **Fix**: one orchestration module that every non-live workflow goes through, with
  a named workflow, a deadline, bounded retries with backoff on overload only, and a
  concurrency limit so background work cannot take all capacity; a short deadline
  and a spoken fallback on the live path.
- **Size**: M. **Needs**: nothing.
- **Done when**: every background model call goes through the module (checked by a
  test that fails on a direct client call elsewhere), carries a timeout, and a
  simulated overload is retried a bounded number of times.

### C-7 (P1) Background work shares one thread pool with live calls

- **Now**: `run_db` and every blocking task use the default executor (CPU count + 4
  threads): website crawls, PDF parsing, model calls made while holding a database
  session, transcript saves and in-call tool handlers.
- **Scenario**: a few slow crawls occupy the pool; a live call's tool handler waits
  behind them and the caller waits in silence.
- **Impact**: slow calls caused by unrelated work. Measured in `LOAD_TEST_RESULTS.md`:
  reads already collapse under five concurrent requests on SQLite.
- **Fix**: a separate, bounded executor for slow background work; live-call database
  work keeps the default one. Longer term: run background workers as their own
  process.
- **Size**: M. **Done when**: a test fills the background executor and a call-path
  database operation still completes within its normal time.

### C-8 (P1) The knowledge index is rebuilt for every call before the agent speaks

- **Now**: `load_index` reads and tokenises every passage of the campaign at stream
  connect; search runs on the event loop. Measured: 2.7 s to build 5,000 passages,
  65 ms per search.
- **Impact**: seconds of dead air at the start of a call for a well-documented
  campaign, and every call pays it again.
- **Fix**: cache the index per campaign, keyed on the newest document change; build
  it off the event loop; cap passages per campaign.
- **Size**: M. **Done when**: a second call on an unchanged campaign builds nothing
  (asserted), and a new document invalidates the cache.

### C-9 (P2) No record of what the AI model was asked, what it used or what it cost

- **Now**: `llm.py` discards `response.usage`. The live pipeline's usage metrics are
  not read. Cost is minutes multiplied by configured rates that default to zero.
- **Impact**: cost per call and per customer is unknown; no bill can be justified.
- **Fix**: an AI usage record per request: workflow, model, input/output/cached
  tokens, duration, outcome, call, campaign and organization; cost from a price
  table. Live-call usage captured from the pipeline's metrics.
- **Size**: M. **Needs**: C-6. **Done when**: a review, a coach run and a training
  run each leave a usage row attributed to the right campaign and organization.

### C-10 (P2) Prompts are not versioned and a call does not record what it ran

- **Now**: prompts are constants in code. A call stores the playbook version taken
  at dial time, while the pipeline loads whatever is live at connect time.
- **Impact**: a call's behaviour cannot be reproduced or attributed after a change.
- **Fix**: a version identifier per prompt (a hash of its text), stored with each AI
  usage record and on the call together with the model; the call runs the playbook
  version it recorded.
- **Size**: S. **Needs**: C-9.

### C-11 (P2) A job that times out keeps running, and transient failures are swallowed

- **Now**: the worker wraps a thread in `wait_for`; the thread continues after the
  timeout while the retry starts a second one. `builder.run_training` catches
  everything and marks the run failed, so a temporary overload is never retried.
- **Scenario**: two training runs produce two drafts and double the spend.
- **Fix**: the model call's own deadline is shorter than the job's, so the thread
  ends first; transient errors are re-raised for the queue to retry.
- **Size**: S. **Needs**: C-6.

### C-12 (P2) Structured answers are not range-checked; the transcript is unmarked

- **Now**: scores and `retry_after_hours` in the review have no bounds; the value
  feeds scheduling. The transcript is inserted raw.
- **Fix**: bounds on every numeric field, clamping on use, and the transcript wrapped
  as untrusted.
- **Size**: S.

### C-13 (P2) Search is keyword-only and weak in Arabic and French

- **Now**: BM25 over tokens. Verified by running it: "سعر" does not find "السعر",
  "٥٠٠" does not match "500", "prix" does not find "tarifs"; one weak shared word
  returns a hit, so "no answer found" is rarely reported.
- **Impact**: wrong or missing facts on Arabic and French calls; weak matches read
  out as answers.
- **Fix**: strip Arabic prefixes and fold Arabic-Indic digits; light French
  stemming; a minimum relevance threshold so a weak match is "not found"; return the
  whole passage (today it is cut to 800 characters of ~900). Embeddings and a vector
  index are the next step, once a measured need exists.
- **Size**: M. **Done when**: the three failing queries above succeed in tests and a
  query sharing one stop-like word with a passage returns nothing.

### C-14 (P2) Document ingestion limits

- **Now**: Word and PowerPoint files are not checked for uncompressed size; PDFs
  have no page limit or extraction time limit; the upload size is checked after the
  whole body has been read; scanned pages inside a mixed PDF are silently dropped;
  a legacy Windows-encoded text file can be decoded as UTF-16 and stored as garbage.
- **Fix**: uncompressed-size and ratio limits, a page cap, UTF-16 only with a byte
  order mark, and a warning on pages with no text. OCR is not implemented (P3).
- **Size**: M.

### C-15 (P2) Personal data in logs

- **Now**: the voice library's debug logging is left at its default and prints the
  full system prompt (lead name, notes) and every spoken sentence; the engine logs
  caller numbers.
- **Fix**: set log levels explicitly, mask phone numbers, never log prompt or
  transcript text.
- **Size**: S. See also E (observability).

### C-16 (P3) Storage and versioning details

- Duplicate detection is exact-hash per campaign; websites are never re-read;
  superseded and failed files are never deleted; `resume_unfinished` takes no lock.

---

## D. Database, background jobs and billing

Verified as sound and left alone: one usage row per call and kind (unique index and
savepoint), stored as `Numeric(12,4)`; a lead cannot be dialed twice at once
(conditional update plus a partial unique index); job claiming is safe on both
databases, with idempotent enqueue, backoff with jitter, dead letter, attempt
history and manual retry; call status only moves forward and the provider corrects
endings the engine inferred; timestamps are consistently UTC; the pool pings and
recycles connections; SQLite runs with foreign keys, write-ahead log and a busy
timeout; PostgreSQL takes an advisory lock around migration; backups use the
database's own snapshot tools with checksums and refuse to overwrite live data.

### D-1 (P0) The usage ledger holds estimates, and only for telephony

- **Now**: `rules.call_cost` = an attempt fee plus started minutes times one blended
  rate from the environment (default 0). The first amount written for a call is kept
  forever. Nothing records AI model, speech-to-text, voice or text-message usage, and
  the provider's own price for a call is never fetched.
- **Weakness / cause**: the ledger was built for budget enforcement, not billing.
- **Scenario**: rates are unset, or corrected a month later; every stored row is 0 or
  wrong and there is no way to correct one without editing history.
- **Impact**: a customer cannot be billed, and margins cannot be known.
- **Fix**: one immutable ledger line per component (telephony, speech-to-text, voice,
  AI model, text message) with quantity, unit, the rate applied, currency and whether
  the amount is an estimate or the provider's figure; corrections only as new
  adjustment lines that reference the line they correct; provider prices recorded
  when the provider reports them.
- **Size**: L. **Needs**: C-9 for AI usage.
- **Done when**: a finished call has a line per component; replaying the final status
  any number of times adds nothing; a later provider price produces an adjustment
  line and the original line is unchanged.

### D-2 (P1) Spend and budgets are read from a second, floating-point copy

- **Now**: `rules.campaign_spend` sums `call_runs.cost_usd` (a float), not the
  ledger. `finalize_call` writes a wall-clock estimate to the same column without a
  lock while the status webhook writes the carrier's duration.
- **Scenario**: on PostgreSQL the two interleave and the estimate overwrites the
  better figure; the budget stop fires early or late.
- **Fix**: spend is the sum of the ledger; `cost_usd` becomes a derived convenience
  written only from the ledger.
- **Size**: S. **Done when**: spend equals the ledger sum in a test with out-of-order
  finalize and status events.

### D-3 (P1) The budget can be exceeded by manual, inbound or multi-process calls

- **Now**: the check lives in the dial loop only. `dial_one` and inbound calls skip
  it. The check and the call's creation are separate transactions.
- **Scenario**: an operator's manual dials, or a second server process, spend past
  the budget; with a shared balance, two campaigns spend the same money.
- **Fix**: reserve the worst-case cost of a call in the same transaction that
  reserves the lead, against the campaign's budget (and later an organization
  balance); release or settle when the call ends.
- **Size**: M. **Needs**: D-2.
- **Done when**: concurrent claims against a budget that covers one call create
  exactly one call, including a manual dial.

### D-4 (P1) Lists and reports load whole tables into memory

- **Now**: the lead list loads every lead and every call of the campaign, then
  filters and pages in Python (`intelligence/api.py`); the report and the coach load
  every call (`report.py`); the timeline selects every transcript in the window; the
  export loads every appointment of every campaign and queries once per lead; the
  call list fetches each lead separately.
- **Impact**: time and memory grow with the campaign; at tens of thousands of leads
  a page takes seconds and a few users exhaust the server. Measured for reads in
  `LOAD_TEST_RESULTS.md`.
- **Fix**: filter, sort, count and page in SQL; select only the columns shown; one
  joined query instead of one per row.
- **Size**: M. **Done when**: the lead list, call list, timeline and export issue a
  bounded number of queries regardless of row count (asserted by counting queries).

### D-5 (P1) Migrations run at start-up, and a new database never runs them

- **Now**: every process migrates on start in one transaction; a new database is
  created from the models and stamped, so most tables have no migration; an index is
  created without `CONCURRENTLY`; a unique constraint has a name in a migration and
  none in the model; downgrades drop tables unguarded.
- **Scenario**: a slow index build blocks writes during a deploy; a failed migration
  stops every instance from starting; a schema built from migrations differs subtly
  from one built from the models.
- **Fix**: a test that the two schemas are identical; migration as an explicit
  deploy step (`callagent db upgrade`) with start-up only verifying; large indexes
  built concurrently on PostgreSQL.
- **Size**: M. **Done when**: the schema-equality test passes on both databases and
  a server started against an out-of-date database refuses with a clear message
  when automatic migration is switched off.

### D-6 (P1) One process does everything

- **Now**: the web server, dial loops, job worker and reconciler share one process
  and one event loop (carried over as A-1 of the earlier audit).
- **Scenario**: starting a second instance for capacity runs two dial loops and two
  reconcilers; one crash stops calling, jobs and the API together.
- **Fix**: a leader lease in the database for the dial loop and the reconciler, so
  any number of instances can run and exactly one dials; a worker-only mode so job
  processing scales separately.
- **Size**: M. **Done when**: two runners started against one database place each
  call once, and stopping the leader hands over within the lease time.

### D-7 (P1) A timed-out job keeps running

Same root as C-11: handlers run in threads that cannot be cancelled. See there.

### D-8 (P2) Schema integrity gaps

- **Now**: status columns are free text with no CHECK constraint; `call_runs.
  experiment_id`, `jobs.call_run_id` and the experiment's version numbers have no
  foreign key; `appointments.starts_at` is a string; money other than the ledger is
  floating point; no `ON DELETE` rule (nothing deletes these rows today).
- **Fix**: CHECK constraints on status columns, the missing foreign keys, and
  validation of the appointment time on write.
- **Size**: M. **Done when**: inserting an unknown status fails on both databases.

### D-9 (P2) Missing indexes and no pruning

- **Now**: no index on `call_runs(campaign_id, started_at)`, used by the daily and
  monthly limits on every dial tick; none for the reconciler's scan of active calls;
  finished jobs, attempts and expired sessions are kept forever.
- **Fix**: the two indexes; a periodic job that deletes finished jobs and expired
  sessions past a retention period.
- **Size**: S.

### D-10 (P2) The job queue has no organization, priority, workload separation or limits

- **Now**: four slots shared by every kind of job; `run_job_now` runs beyond them;
  no priority; no per-kind or per-organization limit; no cancel; leases depend on
  each host's clock.
- **Scenario**: fifty call reviews queue up and a booking notification waits behind
  them; one customer's backlog delays everyone's.
- **Fix**: a queue name and a priority on each job; per-queue concurrency so slow
  analysis cannot take the slots that call-related work needs; organization on the
  job; fairness between organizations when claiming.
- **Size**: M. **Done when**: with the analysis queue saturated, a high-priority job
  is claimed immediately in a test.

### D-11 (P2) Work that bypasses the durable queue

- **Now**: document and website reading use FastAPI background tasks; the call
  review runs inline at the end of the call with the queue only as a ten-minute
  safety net; the coach and language phrasing block an HTTP request; lead import
  runs in the request.
- **Scenario**: a restart during document reading leaves it to a best-effort resume;
  a coach run ties up a request for minutes.
- **Fix**: document reading and the coach as durable jobs with their own queue.
- **Size**: M. **Needs**: D-10.

### D-12 (P2) Read-modify-write without row locks

- **Now**: JSON merges (`run.summary = {**...}`), `failed_logins` and similar are
  read, changed and written with no lock. SQLite's single writer hides it; the tests
  default to SQLite.
- **Fix**: `SELECT ... FOR UPDATE` on the row for these updates on PostgreSQL, and
  the concurrency tests run against PostgreSQL in CI.
- **Size**: S.

---

## E. Observability, security surface and deployment

### E-1 (P1) Logging is not configured

- **Now**: nothing configures logging. The root logger has no handler, so every
  `log.info` in the engine is dropped and warnings print without time or context.
  There is no request or call identifier in any line. Caller numbers appear in
  messages and in access logs (the manual dial passes the number in the URL).
- **Impact**: after an incident there is nothing to read; what is printed contains
  personal data.
- **Fix**: structured (JSON) logs with time, level, logger, request id, call id,
  campaign and organization; phone numbers masked; a correlation id accepted from or
  issued to every request and carried into jobs and calls.
- **Size**: M. **Done when**: a request produces a log line with its id, the same id
  is on the job it enqueued, and a test asserts no unmasked phone number is logged.

### E-2 (P1) No metrics, a health check that checks nothing, no error reporting

- **Now**: `/health` returns a constant and lists missing settings to anyone. There
  is no metrics endpoint and no error reporting.
- **Fix**: `/health/live` (process up) and `/health/ready` (database reachable,
  migration current, worker running); a Prometheus `/metrics` endpoint with request
  rate, errors and latency, active calls and outcomes, job queue depth and age,
  database pool use, AI latency and tokens; alert rules shipped with the code.
- **Size**: M. **Done when**: stopping the database turns readiness red, and the
  metrics endpoint shows a request, a job and a call recorded by tests.

### E-3 (P1) No audit trail

See A-13. Rule and budget changes, approvals, retries and user changes leave no
record of who did them.

### E-4 (P1) Backups are manual, unencrypted and on the same disk

- **Now**: `callagent db backup` covers the database and uploaded files, run by hand,
  written next to the data, unencrypted; verification checks checksums only; restore
  deletes live files before copying; recordings and the website's fallback file are
  not covered.
- **Fix**: encryption with a key from the environment, a retention policy, a
  scheduled mode, restore into a temporary location followed by a swap, and a
  restore drill that runs in CI.
- **Size**: M. **Done when**: an encrypted backup restores into an empty database in
  an automated test and the restored row counts match.

### E-5 (P2) Container and compose

- **Now**: runs as root, floating base image, no health check, build tools in the
  final image, dependencies unpinned with no lock file, PostgreSQL driver not
  installed, compose uses SQLite on a bind mount.
- **Fix**: multi-stage build, non-root user, health check, a lock file, the
  PostgreSQL extra, a compose file with PostgreSQL.
- **Size**: S.

### E-6 (P2) No continuous integration

- **Now**: neither repository has a pipeline, lint or type-check configuration.
- **Fix**: a pipeline that lints, type-checks, runs the tests on SQLite and
  PostgreSQL, checks migrations and scans dependencies; the console's unit and
  browser tests in the other repository.
- **Size**: S.

### E-7 (P2) No draining of live calls on shutdown

- **Now**: on stop the dial loops are cancelled; live calls end with the process.
- **Fix**: stop dialing, wait up to a limit for live calls to finish, then stop.
- **Size**: S.

### E-8 (P2) Console server: headers, rate limiting, body limits

- **Now**: no Content-Security-Policy or other security headers; the rate limiter is
  per process and keyed on the first `X-Forwarded-For` value, which a client can set;
  JSON bodies through the proxy have no size limit; the engine's error text is
  passed to the browser unchanged; when signing in with the engine token, that
  non-expiring token is what the cookie holds.
- **Fix**: security headers; trust the forwarded address only from a configured
  proxy; a body limit; exchange the engine token for a session at sign-in.
- **Size**: M.

### E-9 (P2) Unbounded uploads on the engine

- **Now**: the lead-sheet upload reads the whole body with no limit.
- **Fix**: the same 25 MB limit as documents, enforced while reading.
- **Size**: S.
