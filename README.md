# callagent - the DEVSYNX AI calling agent

Give it your sales script. It learns the pitch, asks you what's missing, rehearses against
simulated prospects, then dials your lead sheet and answers your inbound line: books
meetings, schedules callbacks, texts follow-ups, respects do-not-call, and writes every
outcome back. It speaks English, Arabic and French, follows the prospect when they switch
language mid-call, and always says it is an AI assistant.

The full product plan, what is built and what is not, is in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

```
business script ──▶ Playbook (Claude reads, extracts, asks what's missing)
                        │
                        ▼
                   rehearsal (agent vs. simulated prospects, coach fixes the playbook)
                        │
lead sheet ──▶ dialer ──▶ Twilio call ──▶ speech-to-text ─▶ Claude ─▶ voice ──▶ prospect
inbound number ─────────▶     (adapters: Deepgram or Soniox)      (ElevenLabs)
                                │                             │ tools: book_meeting, schedule_callback,
                                │                             │        send_followup_sms, mark_do_not_call,
                                ▼                             ▼        transfer_to_human, end_call
                        post-call review ──▶ lead status, retries, CSV export, booking webhook
```

## The stack (and why)

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | The voice-agent ecosystem (Pipecat, Silero VAD, Deepgram, ElevenLabs, Twilio) is Python-first. |
| Brain | **Claude Opus 5** (`claude-opus-5`) via the Anthropic SDK | Deep script understanding and structured outputs for onboarding; low-effort adaptive thinking on the live call for speed. Swap the model with two env vars. |
| Realtime framework | [Pipecat](https://github.com/pipecat-ai/pipecat) 1.12 | Battle-tested audio pipeline: barge-in, VAD, interruption handling, function calling, Twilio media streams. |
| Ears | Deepgram Nova-3 or Soniox, behind an adapter (`STT_PROVIDER`) | Deepgram: fast, keyterm biasing toward your product names. Soniox: one model that identifies the language per word. |
| Voice | ElevenLabs `eleven_flash_v2_5` | Most natural low-latency voice; pick any cloned or stock voice. |
| Phone | Twilio Programmable Voice + Media Streams | Outbound dialing, answering-machine detection, SMS, transfers, recordings. |
| Storage | SQLite via SQLAlchemy (Postgres works with `DATABASE_URL`) | Zero setup. |

## Quick start

```bash
git clone <this repo> && cd aicallingagent
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env         # fill in the keys (see below)
callagent doctor             # shows what's still missing
```

### 1. Teach it the pitch

```bash
callagent onboard examples/sample_script.md --name "BrightBooks Q4"
```

Claude reads the script and produces a **Playbook**: company, offer, value props, proof
points it may cite, call goal, persona, stages, qualification questions, objection
handling, pricing rules, compliance must/must-not-say, voicemail script, required
capabilities, and speech-recognizer key terms. Then it asks you, in plain language, about
anything the script leaves open ("Which demo slots can I offer?", "May I quote the price?").
Blocking questions must be answered before dialing; optional ones get a sensible default.

Review what it learned any time:

```bash
callagent playbook show "BrightBooks Q4"
callagent playbook prompt "BrightBooks Q4"     # the exact system prompt used on calls
callagent playbook import "BrightBooks Q4" edited.json   # hand-edit and reload
```

### 2. Let it practice

```bash
callagent rehearse "BrightBooks Q4" --apply
```

The agent runs the pitch in text against several simulated prospects (busy owner,
price-first skeptic, gatekeeper, "are you a robot?"...). A coach model grades how human it
sounded, whether it moved toward the goal, and whether it used its tools correctly, then
proposes concrete playbook edits. `--apply` (or a prompt) applies them. Repeat until the
coach says it is ready.

### 3. Load the leads

```bash
callagent leads import "BrightBooks Q4" leads.xlsx
callagent leads import "BrightBooks Q4" "https://docs.google.com/spreadsheets/d/<id>/edit#gid=0"
```

CSV, XLSX, or a Google Sheet shared as "anyone with the link". It finds the phone column
(and name / company / email / timezone / notes if present), normalizes numbers to E.164,
drops invalid and duplicate numbers, skips anyone on the do-not-call list, and keeps every
other column as context the agent can use on the call.

### 4. Serve and dial

```bash
callagent serve                       # terminal 1
ngrok http 8000                       # terminal 2 -> put the https URL in PUBLIC_BASE_URL
callagent test-call "BrightBooks Q4" +15551234567   # hear it yourself first
callagent campaign start "BrightBooks Q4" --concurrency 3
callagent campaign status "BrightBooks Q4"
callagent campaign calls "BrightBooks Q4"
callagent campaign export "BrightBooks Q4" results.csv
callagent campaign pause "BrightBooks Q4"
```

The dialer runs inside the server: it respects the calling window in each lead's local time
(no Sundays), the concurrency cap, retry limits and delays, and answering-machine
detection. Voicemail gets a short natural message; a human gets the conversation. After
every call a review model writes a summary, disposition, interest level, objections,
next action and coaching notes, and decides whether to retry.

## Training from documents and websites

```bash
callagent train add "BrightBooks Q4" pricing.pdf services.docx deck.pptx https://brightbooks.example
callagent train list "BrightBooks Q4"                        # status of each source, and why any failed
callagent train search "BrightBooks Q4" "do you support Xero?"   # what the agent would find on a call
callagent train build "BrightBooks Q4"                       # draft a new playbook version from script + documents
callagent playbook versions "BrightBooks Q4"
callagent playbook approve "BrightBooks Q4" 2                # make version 2 live; an older number is a rollback
```

- **What it reads:** PDF, Word, PowerPoint (with speaker notes), HTML, CSV, text, Markdown,
  and websites (the page you give plus same-site pages it links to, robots.txt respected).
  Scanned PDFs are refused with a clear message; OCR is not supported yet.
- **On calls:** once a campaign has documents, the agent can look facts up mid-call. It
  is told to look up anything its briefing does not cover, to answer only from what it
  finds, and to offer a follow-up instead of guessing when it finds nothing. Every
  passage carries its source ("pricing.pdf, page 3").
- **Drafts need approval.** `train build` never changes live calls. It saves a draft
  version with its knowledge gaps listed (missing pricing, documents that contradict
  each other, claims with no evidence). A draft with unanswered blocking questions
  cannot be approved without `--force`.
- **Nothing is lost.** Every playbook change is a version; replaced documents stay on
  record. `playbook approve` with an older number restores it exactly.
- **Isolation.** Documents and knowledge belong to one organization and one campaign,
  and search applies both.

The same is available over HTTP for the dashboard: `POST /api/campaigns/{name}/documents`,
`POST .../sources`, `GET .../documents`, `GET .../knowledge/search?q=`, `POST .../train`,
`GET .../versions`, `POST .../versions/{n}/approve`.

## Campaign rules, limits and budgets

```bash
callagent campaign rules "BrightBooks Q4"                                   # show what is in force
callagent campaign rules "BrightBooks Q4" --days mon-fri --hours 09:00-17:30 \
    --max-attempts 2 --max-per-day 300 --budget 250 --stop-after-meetings 40 --max-opt-out-rate 0.05
callagent campaign rules "BrightBooks Q4" --clear                           # back to the global defaults
```

Each campaign can set its own calling days and hours, attempts and retry delay, daily
and monthly call caps, a start and end date, and conditions that stop it. Anything not
set uses the global default, so existing campaigns behave as before. Rules are checked
before every single dial and take effect without a restart.

- **Budget.** Every call gets an estimated cost from `COST_PER_CONNECTED_MINUTE_USD` and
  `COST_PER_CALL_ATTEMPT_USD` (connected time is billed by the started minute). A
  campaign with a budget stops when spend reaches it, and before that it will not place
  a call it could not afford in the worst case: calls in progress and the next one are
  counted at the full call time limit, so the budget cannot be overshot. A campaign with
  a budget will not start until the call cost is configured.
- **Stops by itself.** When the budget is reached, the end date passes, the meeting
  target is hit, or too many answered calls ask not to be called again, the campaign
  pauses and records why (`campaign status` shows it). Daily and monthly caps do not
  stop a campaign; it resumes the next day or month.
- **Priority.** Leads with a higher `priority` are dialed first; a callback the prospect
  asked for still goes ahead of everything.

The cost is an estimate from your configured rates, not a provider invoice.

## Sales intelligence

```bash
callagent campaign report "BrightBooks Q4"   # outcomes, objections, lost reasons, results per playbook version
callagent campaign coach "BrightBooks Q4"    # draft an improved playbook from patterns across the calls
```

- **Structured call evidence.** Each call review now records every objection with its
  category (price, timing, competitor, authority, ...) and whether it was handled, the
  main reason a call was lost, competitors the prospect named, and what the prospect
  actually said about fit.
- **Lead score from evidence.** Need, decision-making authority, timing, budget, fit and
  interest are each yes, no or unknown, taken from the prospect's own words. The score is
  a weighted sum (weights configurable per campaign); "unknown" earns nothing, and a
  lead with no evidence has no score instead of an invented one. Evidence accumulates
  across a lead's calls. `GET /api/campaigns/{name}/leads?category=hot` lists them.
- **Report.** Counted from stored reviews, never estimated: conversion rate, objections
  by frequency with how often each was handled and how often a call with that objection
  still converted, lost reasons, competitors named, statements the playbook does not
  support, cost per conversion, and results per playbook version and per language. A
  best version is named only when at least two versions each have 30 answered calls.
- **Coach.** Needs 20 reviewed calls, then proposes an improved playbook with the
  evidence for each change. The result is a draft version that waits for approval.
  Prices, terms, proof points, compliance lines, the call goal and language settings
  are copied back from the current playbook whatever the model wrote.

- **Lead intelligence API.** `GET /api/campaigns/{name}/leads` filters by `category`
  (hot, warm, cold, unscored), `status`, `intent` (high, medium, low buying intent from
  the latest reviewed call), `min_score`/`max_score` and `q`, with `limit`/`offset`.
  Each lead carries its score, the points each factor contributed, the evidence and
  quotes, objections with how many were handled, the lost reason and the recommended
  next action. `GET /api/campaigns/{name}/leads/{id}` adds every call with its review
  and transcript.
- **Version comparison.** `GET /api/campaigns/{name}/versions/{n}/diff` lists, field by
  field, what a version removes and adds compared with the live playbook. A coach draft
  also stores each recommendation with its evidence (`changes` on the version).

### A/B tests of playbook versions

```
POST /api/campaigns/{name}/experiments            {"variant_version": 3, "variant_share": 0.5, "min_answered_per_arm": 50}
GET  /api/campaigns/{name}/experiments            every test with current results
POST /api/campaigns/{name}/experiments/{id}/stop     end it, keep the live version
POST /api/campaigns/{name}/experiments/{id}/promote  end it, make the variant live
```

- One running test per campaign, so two tests can never share leads (a second start
  is refused with 409).
- A lead's side comes from a hash of the test and the lead: random across leads, and
  the same on every retry and callback. Only leads never dialed before the test take
  part. Each call records the test, the side and the playbook version it ran, and the
  voice pipeline loads that pinned version.
- Results are counted per lead. No side is called ahead until both have the required
  number of answered leads (minimum 30) and a two-proportion test puts the difference
  below 5% chance. Until then the verdict is `collecting` or `no_clear_difference`.
- Promotion is a person's decision and is refused unless the verdict is
  `variant_ahead` (`force` overrides, on record). Nothing is published automatically.

- **Activity over time.** `GET /api/timeline?days=14` returns calls placed, answered and
  converted per calendar day (in `DEFAULT_TIMEZONE`) across campaigns, with quiet days
  as zeros. The dashboard's activity chart reads it.

### Dashboard

The web dashboard lives in the `devytalk` repository (`apps/web`, under `/dashboard`).
It signs in with this engine's API token, keeps it in an httpOnly cookie, and reaches
the engine only through its own server-side proxy, so no key ever reaches the browser.
Point it here with `DEVYTALK_API_URL`.

## Languages: English, Arabic, French

```bash
callagent playbook languages "BrightBooks Q4" en ar fr   # opening language first
callagent playbook show "BrightBooks Q4"
```

With one language the agent stays in it. With more than one it opens in the first and
then answers in whatever language the prospect last spoke, switching back and forth as
they do, never mixing languages in one reply.

- **Native phrasing, not translation.** The command above also writes how a native rep
  would say the opening, pitch, objections, booking and close in each added language.
  Facts, prices and promises stay exactly as in the playbook. Use `--no-localize` to skip.
- **Detection you can trust.** Arabic is recognized from its script, so a wrong language
  tag from the recognizer cannot hide it. The agent never moves into a language the
  campaign did not list.
- **No delay on a switch.** The voice model reads each sentence in the language it is
  written in. Set `ELEVENLABS_VOICE_ID_AR` / `_FR` if you want a different voice per language.
- **Provider gaps stop the campaign, not the call.** If the configured recognizer is not
  confirmed for one of the campaign's languages, `campaign start` refuses with the reason.
  Deepgram's multilingual mode is not confirmed for Arabic by default: use
  `STT_PROVIDER=soniox`, or verify on test calls and add `ar` to
  `DEEPGRAM_MULTILINGUAL_LANGUAGES`.
- Every call record has a `language` block: the language detected, the final language
  and each switch with its turn number.

## Setting up without the command line

- `GET /api/status` says which providers are connected and names the settings still
  missing. It never returns a value. It also gives the inbound webhook address.
- `POST /api/campaign-briefs` creates a campaign from a short form with a working,
  conservative playbook and no AI model. It states only what the form says, discloses
  that the agent is an AI, and can open in English, French or Arabic.
- `GET`/`PUT /api/campaigns/{name}/languages` reads and sets the languages.
- `POST /api/campaigns/{name}/inbound?number=` sets the number a campaign answers.
- `GET /api/campaigns/{name}/calls?direction=inbound|outbound`, and `by_direction` in
  the report, tell outbound and inbound apart.
- `GET /api/calls/{id}` returns `"live": true` and the transcript so far while a call
  is in progress: the transcript is saved as the call proceeds.

## Inbound calls

```bash
callagent campaign inbound "BrightBooks Q4" +14155550111   # this campaign answers that number
callagent campaign inbound "BrightBooks Q4"                # stop answering
```

In the Twilio console, set the number's "A call comes in" webhook to
`PUBLIC_BASE_URL/twilio/inbound` and its status callback to `PUBLIC_BASE_URL/twilio/status`.
`INBOUND_CAMPAIGN` names a fallback campaign for numbers without one of their own; a
number nobody answers hears a short message and is hung up.

The agent greets the caller instead of pitching, a returning caller is matched to their
lead so earlier notes carry over, and an inbound call never puts that person on the
outbound dial list unless they ask for a callback. Inbound calls are not recorded yet.

## Latency: what to expect and how to tune it

Every hop is streaming, and every knob that costs time is exposed:

| Hop | Default | Typical | Knob |
|---|---|---|---|
| Prospect stops talking -> turn detected | Silero VAD + Smart Turn v3 | 0.25-0.5 s | `USE_SMART_TURN` |
| Deepgram final transcript | Nova-3, endpointing 300 ms | 0.2-0.4 s | `DEEPGRAM_ENDPOINTING_MS` |
| Claude first word | Opus 5, adaptive thinking at effort low (rarely thinks on small talk), prompt caching | 0.5-1.0 s | `CALL_THINKING`, `CALL_FAST_MODE`, `CALL_MODEL` |
| ElevenLabs first audio | `eleven_flash_v2_5` | 0.1-0.3 s | `ELEVENLABS_MODEL` |

So the natural gap between the prospect finishing and the agent starting is about one
second, which is human range. Two things keep it from ever feeling like dead air:

- **Backchannel fillers**: if the model's first word is not out within `FILLER_DELAY_MS`
  (700 ms), the agent says a short "Mm-hm." or "Right." first, exactly as a person does
  before a considered answer. Never when the prospect is interrupting, at most once per
  turn, and not written into the conversation memory. `CALL_FILLERS=false` turns it off.
- **Short replies by design**: the prompt's "one or two sentences, lead with the direct
  part" rule means the first sentence reaches the voice almost immediately and the
  prospect can jump in. (`CALL_MAX_TOKENS` is a safety ceiling, not a target; it is high
  enough that a goodbye plus the end-of-call tool calls never truncate.)
- **Nothing weird is ever read aloud**: a speech sanitizer sits in front of the voice and
  strips XML tags, markdown, stage directions and anything that looks like a tool call or
  JSON, so a rare model slip is silent instead of spoken.

Measure, don't guess:

```bash
callagent chat "BrightBooks Q4"                     # talk to it in the terminal, see first-word time per turn
callagent chat "BrightBooks Q4" --fast              # try fast mode (2.5x output speed, premium price)
callagent chat "BrightBooks Q4" --model claude-sonnet-5
callagent test-call "BrightBooks Q4" +1yournumber   # then a real call
callagent campaign calls "BrightBooks Q4"           # each call stores stt/llm/tts p50/p95 in its summary
```

Every call's record (`GET /api/calls/{id}`, field `summary.latency`) has p50/p95 for:

- `response`: the prospect stops talking -> the agent's audio starts. This is the number
  the prospect actually feels. Target: p95 under one second.
- `interruption`: the prospect starts talking over the agent -> the agent's audio stops.
  Target: under 200 ms.
- `stt_ttfb`, `llm_ttfb`, `tts_ttfb`: where the time went, stage by stage.

So you can see exactly where any delay is coming from. Server logs also print "first token after X s" on every turn.

Model choice: Opus 5 with adaptive thinking at low effort is the default: it answers
small talk instantly and only pauses to think on a genuinely hard objection, and fillers
cover that pause. `CALL_THINKING=disabled` removes even that at a small cost in tool-call
reliability (the sanitizer catches the failure mode). `CALL_MODEL=claude-sonnet-5` shaves
a few hundred milliseconds more. Fast mode (`CALL_FAST_MODE=true`) keeps Opus 5 and
speeds up output tokens; it is Claude API only.

## What makes it sound human

- **Prompting for speech, not text**: short turns, one question at a time, reacting before
  moving on, contractions, numbers written as words, no lists or markdown, sparse and
  varied backchannels, at most one hesitation per turn, never in the close.
- **Barge-in**: Silero VAD stops the bot the moment the prospect speaks; the agent is told to
  drop its sentence and answer, not restart.
- **Listen first**: on connect the agent waits ~1.5 s for the "Hello?" before opening.
- **Silence handling**: after 8 s of silence it checks in once, then says goodbye.
- **Knows when you're done talking**: Pipecat's bundled Smart Turn v3 model detects
  semantic end-of-turn, so the agent doesn't jump in on a pause mid-sentence
  (`USE_SMART_TURN=false` falls back to a plain silence timeout).
- **No dead air**: backchannel fillers cover the rare slow first word (see Latency above),
  spoken in the language of the call or not at all.
- **Honesty**: the agent says it is an AI assistant in its first turn
  (`disclose_ai_upfront`, on by default), never claims to be human, and tells the
  prospect when the call is recorded (`RECORD_CALLS=true`). Check your local rules on AI
  disclosure, recording consent and calling hours; in many places they are legally required.

## Security

The server must be reachable by Twilio, so it is public. Two layers protect it:

- Twilio webhooks (`/twilio/voice`, `/twilio/status`) are verified with the
  `X-Twilio-Signature` header against your auth token. Keep `TWILIO_VALIDATE_SIGNATURE=true`.
- The media-stream WebSocket (`/ws`) only accepts a connection that presents the
  per-call random token our own TwiML handed to Twilio, for a call that is live, not yet
  streamed, and whose Twilio call SID matches. Anything else is closed before a single
  model or voice request is made.
- The control API (`/api/*`, which can start campaigns and dial numbers) requires
  `Authorization: Bearer <token>`: the session of a signed-in person, or the engine's
  API token. Set `API_TOKEN`, or let the server generate one into `DATA_DIR/api_token`
  on first start. The CLI reads the same file, so on one machine it just works. Toll
  fraud is real; do not disable this.

### People and roles

People sign in with an email and a password (`POST /api/auth/login`) and get a session
valid for 12 hours. Passwords are stored as salted scrypt hashes, session tokens as
SHA-256 hashes. Five wrong passwords lock an account for 15 minutes.

| Role | May |
| --- | --- |
| viewer | read everything except the list of users |
| operator | also start and pause dialing, call one number, import leads, manage documents, build drafts, ask the coach, start and stop A/B tests, retry background work, mark website requests handled |
| admin | also approve, reject and roll back playbooks, promote a test, change rules, budgets, languages and the inbound number, create campaigns, manage users |

The role is checked on every route by the same dependency that checks the token. A
route that changes something is admin-only unless it is listed for operators in
`callagent/accounts/__init__.py`. The API token has the admin role. Create the first
admin with `callagent users add you@example.com --role admin`, or sign in to the
console with the API token and add people under Team.

### Organizations

Each customer is an organization with its own campaigns, leads, calls, documents,
do-not-call list and people. A request can only see and change the organization of
the account that makes it. The deployment's operator creates them:

```bash
callagent orgs add "Acme Ltd" --admin-email owner@acme.example
callagent orgs list
```

or `POST /api/organizations` with the engine API token, or the Organizations screen
of the console when signed in with that token. The first admin then adds everyone
else. The API token works in the default organization unless the request names
another with the `X-DevyTalk-Organization` header. More than one organization needs
PostgreSQL, with the engine connecting as an ordinary role so that row-level security
binds it (`GET /api/status` says whether it does). Details: `docs/DATABASE_DESIGN.md`.

### Other credentials

- `INTAKE_TOKEN` lets the website hand over what visitors send through its forms
  (`POST /api/intake/requests`) and nothing else. Unset: intake is off.

## Deploying

```bash
docker compose up -d --build     # uses .env, persists data/ and the SQLite DB
```

Or any host with Python 3.11: `pip install .` and `callagent serve` behind an HTTPS
reverse proxy that also passes WebSockets to `/ws`. Set `PUBLIC_BASE_URL` to that
HTTPS origin. One process runs the dialer and every live call; scale the concurrency with
`MAX_CONCURRENT_CALLS` (each live call uses roughly one CPU core for VAD, turn detection
and audio resampling).

## Configuration

Everything is in `.env` (see `.env.example`). The required keys for live calls:
`ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY` (or `SONIOX_API_KEY` with `STT_PROVIDER=soniox`), `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`,
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `PUBLIC_BASE_URL`.

Models: `ONBOARDING_MODEL` / `CALL_MODEL` default to `claude-opus-5`. Set
`CALL_MODEL=claude-sonnet-5` for lower latency on the phone if you prefer, or
`ONBOARDING_MODEL=claude-fable-5-1` for the deepest script analysis. Refusal fallbacks
(`ENABLE_REFUSAL_FALLBACKS`) are on by default for the offline calls; turn them off on
Bedrock/Vertex/Foundry.

Booked meetings are stored locally and, if `BOOKING_WEBHOOK_URL` is set, POSTed as JSON to
your calendar/CRM automation (Zapier, Make, n8n, or your own endpoint).

## Layout

```
callagent/
  cli.py                 Typer CLI
  server.py              FastAPI: Twilio webhooks, /ws media stream, control API
  config.py              settings (.env)
  db.py                  SQLAlchemy models: Campaign, Lead, CallRun, Appointment, DoNotCall
  llm.py                 shared Claude helpers (structured outputs, fallbacks)
  playbook/schema.py     the Playbook model
  playbook/analyzer.py   script -> Playbook, Q&A refinement
  playbook/prompt_builder.py  Playbook + lead -> live system prompt
  rehearsal.py           simulated prospects + coach + apply edits
  leads/importer.py      CSV/XLSX/Google Sheets import, phone normalization
  dialer/twilio_client.py  place calls, TwiML, SMS, transfers, signature validation
  dialer/campaign_runner.py  calling windows, concurrency, retries, status callbacks
  languages.py           language detection and per-call language tracking
  providers/             speech-to-text and voice adapters, provider registry
  training/ingest.py     read PDF/Word/PowerPoint/HTML/CSV/text and cut into passages
  training/web.py        read websites safely (public addresses only, robots.txt)
  training/search.py     per-campaign knowledge search
  training/versions.py   playbook history: draft, approve, rollback
  training/builder.py    script + documents -> draft playbook
  training/api.py        training HTTP API
  dialer/rules.py        per-campaign rules, limits, cost and budget checks
  intelligence/scoring.py  lead score from call evidence
  intelligence/report.py   campaign report counted from call reviews
  intelligence/coach.py    drafts playbook improvements for approval
  intelligence/api.py      report, scored leads and coach over HTTP
  voice/pipeline.py      Pipecat pipeline for one call
  voice/language_follower.py  tracks the prospect's language during the call
  voice/tools.py         in-call tools (book_meeting, end_call, ...)
  voice/transcript.py    transcript capture
  postcall/summarizer.py review + lead update
tests/                   unit tests (no network)
examples/                sample script + sample lead sheet
```

## Operations

```bash
callagent db status            # which migration the database is at
callagent db upgrade           # apply new migrations (the server also does this at start)
callagent db check             # list duplicates a migration would refuse
callagent db backup --to DIR   # database + uploaded files + manifest, verified
callagent db verify-backup PATH
callagent db restore PATH      # stop the server first
```

- **Database.** SQLite for one operator on one machine; PostgreSQL for anything
  hosted (`pip install "callagent[postgres]"`, `DATABASE_URL=postgresql+psycopg://...`).
  Measured: SQLite degrades sharply when several people use the dashboard at once.
- **Background jobs.** Booking notifications, post-call reviews that failed and
  training runs go through a durable queue in the database: retried with backoff,
  taken over after a crash, parked as `dead` with their error when they give up.
  `GET /api/jobs?status=dead` lists those; `POST /api/jobs/{id}/retry` reruns one.
- **Booking notifications** are signed when `BOOKING_WEBHOOK_SECRET` is set
  (`X-Callagent-Signature: sha256=<HMAC-SHA256 of the body>`) and carry the same
  `X-Callagent-Delivery` id on every retry, so your endpoint can ignore repeats.
- **Several processes on one database** are safe: one of them dials (a lease in the
  database, handed over when that process stops) and all of them serve the API, the
  audio of calls and background jobs. This has been tested, not yet run in a deployment.
- **Seeing what happens.** Logs are JSON lines with the request, call and job each
  line belongs to, with phone numbers and email addresses masked. `/health/live` and
  `/health/ready` for probes; `/metrics` (Prometheus) for the operator; alert rules in
  `ops/prometheus-alerts.yml`. `GET /api/audit` lists who changed what.
- **What it costs.** `GET /api/usage` is the usage ledger (time-based cost per call,
  AI use, corrections); `GET /api/ai-usage` is AI use by workflow and model. These
  are measurements at configured rates and list prices, not invoices.
- **Backups.** `callagent db backup`, encrypted when `BACKUP_ENCRYPTION_KEY` is set
  (`callagent db backup-key` makes one), on a schedule with `BACKUP_EVERY_HOURS`.
  Copying them off the machine is yours to arrange.
- **Stopping.** `callagent serve` waits for calls in progress when it is told to
  stop (`SHUTDOWN_DRAIN_SECONDS`). Give your process manager a longer grace period.

### Several customers on one deployment

Each organization has its own caller numbers, transfer number, booking webhook and
rates, and its own API keys. The deployment's environment settings
(`TWILIO_FROM_NUMBER`, `HUMAN_TRANSFER_NUMBER`, `BOOKING_WEBHOOK_URL`) belong to the
default organization only: another organization never borrows them, and cannot place
calls until it has a number.

```bash
# the operator (the engine's API token): a new customer, then the numbers it calls from
curl -X POST $ENGINE/api/organizations -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name": "Bolt", "admin_email": "boss@bolt.example", "admin_password": "…"}'
curl -X PUT $ENGINE/api/organizations/2/telephony -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"from_numbers": ["+14155550210", "+14155550211"]}'
```

The numbers must be ones you hold at the phone provider. With several, each lead is
always called from the same one. The organization's admin then sets the transfer
number and booking webhook (`PUT /api/organization/settings`) and creates API keys
for their own systems (`POST /api/keys`): a key has a role, is shown once, and is
sent as `Authorization: Bearer dtk_…`.

To be told what happens instead of asking, an admin registers webhook endpoints
(`POST /api/webhooks`): signed deliveries for calls, bookings, campaigns and
playbooks, retried for about an hour. `docs/WEBHOOKS.md` has the events, the body
and the signature check to copy.

### Where things stand

**No part of this has handled a real phone call.** It was built and tested without
provider credentials; every provider in the tests is a stand-in. Read
`docs/PRODUCTION_READINESS.md` before relying on anything here.

| Document | What it covers |
| --- | --- |
| `docs/PRODUCTION_READINESS.md` | What is verified, what is only built, what is blocked, what is not done |
| `docs/ENTERPRISE_BACKEND_AUDIT.md` | The audit: every finding, its status and the test that shows it |
| `docs/ENTERPRISE_ARCHITECTURE.md` | Modules, their boundaries, how it scales |
| `docs/DATABASE_DESIGN.md` | Tables, rules the database enforces, migrations, organizations |
| `docs/EVENT_ARCHITECTURE.md` | The event log and background job queues |
| `docs/CALLING_ENGINE_RELIABILITY.md` | How a call stays correct when something fails |
| `docs/AI_ORCHESTRATION.md` | How the AI model is used, bounded and metered |
| `docs/BILLING_ARCHITECTURE.md` | The usage ledger and budgets |
| `docs/OBSERVABILITY.md` | Logs, health, metrics, alerts, audit trail |
| `docs/SECURITY_REVIEW.md` | Security by area, and what is open |
| `docs/COMPLIANCE.md` | Consent, do-not-call, erasure and retention: what is enforced, and what a lawyer must decide per market |
| `docs/PERFORMANCE_TESTING.md`, `docs/LOAD_TEST_RESULTS.md` | What was measured, how, and the numbers |
| `docs/DISASTER_RECOVERY.md` | Backups, restoring, stopping and starting |

## Tests

```bash
pytest
TEST_POSTGRES_URL=postgresql+psycopg://postgres@127.0.0.1:5432/callagent_test pytest   # also runs integrity and restore tests on PostgreSQL (the database is wiped)
python loadtests/run.py --database-url sqlite:////tmp/load.db                          # load test; see docs/LOAD_TEST_RESULTS.md
```

`tests/seed_dashboard.py` fills an **empty** database with invented campaigns for the
dashboard's browser tests. It is test-only: nothing in the product runs it, and it
refuses a database that already has a campaign.

## Notes on compliance

You are responsible for consent, do-not-call lists, calling hours, recording consent and
AI-disclosure rules in your jurisdiction (e.g. TCPA in the US, GDPR/ePrivacy in the EU).
The agent honors opt-outs immediately and never calls a number on the do-not-call table.
