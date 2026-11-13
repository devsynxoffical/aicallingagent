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
  `Authorization: Bearer <token>`. Set `API_TOKEN`, or let the server generate one into
  `DATA_DIR/api_token` on first start. The CLI reads the same file, so on one machine it
  just works. Toll fraud is real; do not disable this.

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
  voice/pipeline.py      Pipecat pipeline for one call
  voice/language_follower.py  tracks the prospect's language during the call
  voice/tools.py         in-call tools (book_meeting, end_call, ...)
  voice/transcript.py    transcript capture
  postcall/summarizer.py review + lead update
tests/                   unit tests (no network)
examples/                sample script + sample lead sheet
```

## Tests

```bash
pytest
```

## Notes on compliance

You are responsible for consent, do-not-call lists, calling hours, recording consent and
AI-disclosure rules in your jurisdiction (e.g. TCPA in the US, GDPR/ePrivacy in the EU).
The agent honors opt-outs immediately and never calls a number on the do-not-call table.
