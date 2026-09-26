# callagent - an outbound AI calling agent that sounds human

Give it your sales script. It learns the pitch, asks you what's missing, rehearses against
simulated prospects, then dials your lead sheet and closes: books meetings, schedules
callbacks, texts follow-ups, respects do-not-call, and writes every outcome back.

```
business script ──▶ Playbook (Claude reads, extracts, asks what's missing)
                        │
                        ▼
                   rehearsal (agent vs. simulated prospects, coach fixes the playbook)
                        │
lead sheet ──▶ dialer ──▶ Twilio call ──▶ Deepgram STT ─▶ Claude ─▶ ElevenLabs TTS ──▶ prospect
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
| Ears | Deepgram Nova-3 streaming | Fast, accurate, keyterm biasing toward your product names. |
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

## Latency: what to expect and how to tune it

Every hop is streaming, and every knob that costs time is exposed:

| Hop | Default | Typical | Knob |
|---|---|---|---|
| Prospect stops talking -> turn detected | Silero VAD + Smart Turn v3 | 0.25-0.5 s | `USE_SMART_TURN` |
| Deepgram final transcript | Nova-3, endpointing 300 ms | 0.2-0.4 s | `DEEPGRAM_ENDPOINTING_MS` |
| Claude first word | Opus 5, **thinking disabled**, effort low, prompt caching | 0.5-0.9 s | `CALL_THINKING`, `CALL_FAST_MODE`, `CALL_MODEL` |
| ElevenLabs first audio | `eleven_flash_v2_5` | 0.1-0.3 s | `ELEVENLABS_MODEL` |

So the natural gap between the prospect finishing and the agent starting is about one
second, which is human range. Two things keep it from ever feeling like dead air:

- **Backchannel fillers**: if the model's first word is not out within `FILLER_DELAY_MS`
  (700 ms), the agent says a short "Mm-hm." or "Right." first, exactly as a person does
  before a considered answer. Never when the prospect is interrupting, at most once per
  turn, and not written into the conversation memory. `CALL_FILLERS=false` turns it off.
- **Short replies by design**: `CALL_MAX_TOKENS=300` and the prompt's "one or two
  sentences, lead with the direct part" rule mean the first sentence reaches the voice
  almost immediately and the prospect can jump in.

Measure, don't guess:

```bash
callagent chat "BrightBooks Q4"                     # talk to it in the terminal, see first-word time per turn
callagent chat "BrightBooks Q4" --fast              # try fast mode (2.5x output speed, premium price)
callagent chat "BrightBooks Q4" --model claude-sonnet-5
callagent test-call "BrightBooks Q4" +1yournumber   # then a real call
callagent campaign calls "BrightBooks Q4"           # each call stores stt/llm/tts p50/p95 in its summary
```

Every call's record (`GET /api/calls/{id}`, field `summary.latency`) has p50/p95 for
speech-to-text, model first token and voice first byte, so you can see exactly where any
delay is coming from. Server logs also print "first token after X s" on every turn.

Model choice: Opus 5 with thinking disabled is the default for the best judgment on the
phone. If you want to shave a few hundred milliseconds more, `CALL_MODEL=claude-sonnet-5`;
if you want the model to reason on hard objections at the cost of pauses,
`CALL_THINKING=adaptive`. Fast mode (`CALL_FAST_MODE=true`) keeps Opus 5 and speeds up
output tokens; it is Claude API only.

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
- **No dead air**: backchannel fillers cover the rare slow first word (see Latency above).
- **Honesty**: if asked whether it is an AI, it says so (configurable per playbook,
  `disclose_ai_if_asked`). Check your local rules on AI disclosure and calling hours;
  in many places both are legally required.

## Security

The server must be reachable by Twilio, so it is public. Two layers protect it:

- Twilio webhooks (`/twilio/voice`, `/twilio/status`) are verified with the
  `X-Twilio-Signature` header against your auth token. Keep `TWILIO_VALIDATE_SIGNATURE=true`.
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
`ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`,
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
  voice/pipeline.py      Pipecat pipeline for one call
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
