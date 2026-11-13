# DEVSYNX AI Calling Agent: architecture and build plan

This document is the answer to "inspect what exists, say what is missing, design the
whole thing before changing it". It is the working plan: each phase lists what is
done, what is next, and how it will be proven.

Last updated: 2026-11-13.

## 1. What exists today (audited)

`callagent` is a working single-company calling agent, driven from a command line
and a small authenticated HTTP API. Everything below is implemented and covered by
the test suite (77 tests, no network needed).

| Area | What it does today |
|---|---|
| Voice loop | Twilio media stream -> streaming speech-to-text -> Claude -> streaming voice -> Twilio. Every hop streams; nothing waits for a full sentence. Built on Pipecat. |
| Turn-taking | Silero voice-activity detection, semantic end-of-turn detection (Smart Turn v3), barge-in (the agent stops when the prospect talks), silence check-ins, voicemail detection with a spoken voicemail. |
| Learning a pitch | `callagent onboard`: Claude reads a script and produces a structured Playbook (offer, persona, stages, objections, pricing rules, must/must-not say), then asks the owner about gaps. Blocking gaps stop dialing. |
| Practice | `callagent rehearse`: the agent runs against simulated prospects, a coach model grades it and proposes playbook edits. |
| Leads and dialing | CSV/XLSX/Google Sheet import, phone normalization, de-duplication, do-not-call list, calling window in the lead's time zone, retries, concurrency cap, requested callbacks. |
| In-call actions | Book a meeting, schedule a callback, send a follow-up text, mark do-not-call, transfer to a human, end the call. |
| After the call | A review model writes summary, disposition, interest, objections, next action, coaching notes; lead status and retry are updated. |
| Safety | Twilio webhook signatures, per-call token on the media socket, bearer token on the control API. |

Added in this release (see section 6): English/Arabic/French with automatic
detection and mid-call switching, speech providers behind adapters, AI disclosure
and recording notice in the opening, inbound calls, real end-to-end response and
interruption timing, a richer call report, and in-place database upgrades.

## 2. Gap analysis against the product brief

Status: **Done** = implemented and tested. **Partial** = works in a limited form.
**Missing** = not built.

| Brief section | Status | Notes |
|---|---|---|
| 2. Human-like conversation | Done | Needs real-call tuning per language (see section 7). |
| 3. Low-latency streaming pipeline | Done | Per-stage and end-to-end timing recorded on every call. Speech-to-speech (Architecture A) not yet benchmarked against the modular pipeline. |
| 4. Multilingual (EN/AR/FR) | Done in code | Detection, switching, native phrasing, language-aware fillers. **Not yet verified on real calls**: needs provider keys. |
| 5. Sales training from uploads | Partial | Scripts as text/markdown only. No PDF/Word/URL ingestion, no retrieval over large document sets, no training dashboard, no approval step. |
| 6. Sales conversation engine | Done | Qualification, objections, closing live in the Playbook and prompt. |
| 7. Appointment booking | Partial | Books into the local database and posts to a webhook. **Does not check real calendar availability.** No Google/Outlook, no rescheduling, reminders or no-show tracking. |
| 8. Post-call analysis | Partial | Per-call report is complete. The improvement loop (patterns across calls, A/B tests, approval, deployment) is missing. |
| 9. CRM and lead management | Partial | Leads, history, statuses exist. No CRM sync, fewer statuses than the brief lists. |
| 10. Campaigns | Partial | One playbook per campaign. Calling rules are global settings, not per campaign. |
| 11. Agent personality | Partial | Persona is part of the playbook. Voice and speed are global settings. |
| 12. Human handoff | Partial | Transfer works. The human does not yet receive the conversation summary. |
| 13-14. Dashboard, live monitoring | Missing | No web interface at all. |
| 15. Multi-tenant SaaS, roles | Missing | Single organization, single API token. |
| 16. Provider independence | Partial | Speech-to-text and voice are adapters. Telephony is still Twilio-specific; the model is Claude-specific. |
| 17. Compliance | Partial | Do-not-call, opt-out, calling hours, AI disclosure, recording notice. No per-country rule sets, consent records, audit log or retention settings. |
| 18. Benchmark suite | Missing | Timing is recorded per call; there is no repeatable cross-provider benchmark. |
| 19. Sales intelligence | Missing | |
| 21. Acceptance tests on real calls | Blocked | Needs telephony and speech credentials. |

## 3. Target architecture

```
                         ┌────────────────────────── Web app (Next.js) ──────────────────────────┐
                         │ dashboard · campaigns · training · leads · live calls · reports · admin│
                         └───────────────────────────────────┬────────────────────────────────────┘
                                                             │ REST + WebSocket (live transcript)
┌───────────────┐   webhooks    ┌────────────────────────────▼───────────────────────────────────┐
│  Telephony    │──────────────▶│  API service (FastAPI)                                          │
│  (adapter:    │               │  auth · organizations · roles · campaigns · leads · training    │
│  Twilio,      │◀──────────────│  calendars · reports · billing · audit log                      │
│  Telnyx, SIP) │   call control└───────┬──────────────────────────────┬──────────────────────────┘
└──────┬────────┘                       │                              │ jobs
       │ media stream (WebSocket)       │                              ▼
┌──────▼────────────────────────────────▼─────────┐        ┌───────────────────────────────────────┐
│  Voice workers (one process per N calls)         │        │  Background workers                    │
│  VAD -> STT adapter -> language follower ->      │        │  dialer · document ingestion ·         │
│  orchestrator (prompt + retrieval + tools) ->    │        │  post-call review · pattern mining ·   │
│  model adapter -> TTS adapter                    │        │  experiments · reminders               │
└──────┬───────────────────────────────────────────┘        └──────────────────┬────────────────────┘
       │                                                                        │
┌──────▼────────────────────────────────────────────────────────────────────────▼───────────────────┐
│ PostgreSQL (all business data, row-level security by organization) · pgvector (knowledge chunks)   │
│ Redis (live call state, dialer queue, rate limits) · object storage (recordings, uploaded files)   │
└────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### Decisions and why

1. **Keep Python and Pipecat for the voice path.** It already works, handles barge-in
   and turn-taking well, and has maintained integrations for every speech vendor
   worth benchmarking. Rewriting it would cost weeks and gain nothing.
2. **One codebase, three process types** (API, voice worker, background worker). Today
   they run in one process, which is fine for a few concurrent calls. They are split
   when concurrency requires it (each live call needs roughly one CPU core). No
   microservices before there is a load reason.
3. **PostgreSQL as the only source of truth**, with pgvector for retrieval. One
   database to secure, back up and isolate per tenant. SQLite stays for local use.
4. **Tenant isolation at the database**, not only in application code: every table
   carries `organization_id`, enforced by PostgreSQL row-level security, with the
   organization set per request from the authenticated session. A forgotten filter
   in a query then returns nothing instead of another company's data. Retrieval
   queries are scoped the same way, so one client's pricing can never be retrieved
   into another client's call.
5. **Training is retrieval, not fine-tuning.** Uploaded material is parsed, chunked
   and embedded per organization and campaign. The Playbook stays the structured
   core (short, cached in the prompt); retrieval adds depth for questions the
   Playbook does not cover. Retrieval runs on the prospect's transcript in parallel
   with turn detection, so it adds no wait before the model starts.
6. **Nothing reaches live calls without approval.** Playbooks are versioned.
   Suggestions from call reviews accumulate as evidence; a change becomes an
   experiment (a share of calls), is measured against the current version, and a
   manager approves the winner. Prices, commitments and policies are never changed
   by the system.
7. **Providers behind adapters, chosen by measurement.** Each adapter declares what
   it can do (languages, automatic language identification). A campaign whose
   languages the configured providers cannot handle is refused before it dials.

### Data model additions (by phase)

| Phase | Tables |
|---|---|
| 3 | `documents`, `knowledge_chunks` (vector), `playbook_versions`, `training_runs` |
| 4 | `agents` (voice, speed, personality), `calendar_connections`, `appointments` (+status, meeting link, reminders), `handoffs`, per-campaign calling rules |
| 5 | `call_reviews` (queryable, not JSON), `improvement_suggestions`, `experiments`, `experiment_arms` |
| 6 | `organizations`, `users`, `memberships` (role), `api_keys`, `audit_log`, `usage_events`, `consent_records`, `suppression_lists` |

## 4. Build phases

| Phase | Scope | State |
|---|---|---|
| 1. Core voice engine | Outbound, inbound, streaming, barge-in, memory, call log, timing | **Done in code.** Real-call verification pending credentials. |
| 2. Multilingual | Arabic, French, detection, switching, voices per language | **Done in code.** Real-call verification pending credentials. |
| 3. AI training | PDF/Word/URL ingestion, retrieval, playbook versions, approval, training API | Next |
| 4. Sales automation | Real calendar availability (Google, Outlook), reminders, handoff summary, CRM sync, per-campaign rules, agent profiles | Planned |
| 5. Post-call intelligence | Pattern mining, experiments, approval and rollout, comparison reports | Planned |
| 6. SaaS and scale | Organizations, roles, row-level security, web dashboard, live monitoring, billing, worker split | Planned |

Tenant columns and row-level security are introduced at the start of phase 3, not
phase 6, because every table created from then on must be born tenant-scoped.

## 5. How each acceptance test will be proven

| Test | Proof |
|---|---|
| 1 Natural conversation, 7 Objection handling | Scripted real calls scored by reviewers, plus `callagent rehearse` regression set |
| 2 Interruption | `interruption` timing in every call record; target p95 under 200 ms |
| 3 Arabic, 4 French, 5 Switching | Real calls with native speakers per language and dialect; `language` block of the call record shows detection and switches |
| 6 New service training | Upload -> playbook version -> rehearsal passes, with no code change (phase 3) |
| 8 Appointment booking | Booked slot appears in the connected calendar; double-booking attempt is refused (phase 4) |
| 9 Post-call analysis | Call record contains every field in brief section 8 (done) |
| 10 Continuous improvement | Suggestion -> experiment -> measured lift -> approval (phase 5) |
| 11 Human transfer | Transferred call arrives with the summary on the rep's screen (phase 4) |
| 12 Concurrency | Load test with simulated callers; latency p95 at 10, 25, 50 concurrent calls |

## 6. What changed in this release

- **Languages.** A campaign lists the languages it speaks (`callagent playbook languages NAME en ar fr`).
  With more than one, the agent opens in the first and follows the prospect, turn by
  turn. Arabic is recognized from its script, so a wrong language tag from the
  recognizer cannot hide it. The agent never moves into a language the campaign did
  not approve. Native phrasing per language is written by a separate step, not
  translated line by line.
- **Zero-cost switching.** On multilingual calls the voice model reads each sentence in
  the language it is written in, so a switch needs no reconnect. A per-language voice
  can be set and is applied on switch.
- **Provider adapters.** `STT_PROVIDER=deepgram|soniox`. A language gap stops the
  campaign from starting with a clear message.
- **Honesty.** The agent says it is an AI assistant in its first turn, never claims to be
  human, and announces recording when recording is on. Fillers are spoken in the
  call's language or not at all.
- **Inbound.** A Twilio number can be answered by a campaign. An inbound call never,
  by itself, places that person on the outbound dial list.
- **Timing.** Each call stores `response` (prospect stops -> agent audio starts) and
  `interruption` (prospect barges in -> agent audio stops) with p50/p95, next to the
  per-stage numbers.
- **Call report.** Sentiment, pain points, buying-intent and qualification scores,
  objections handled, missed opportunities, unsupported statements, whether AI was
  disclosed, whether the language was right.
- **Database.** New columns are added in place on start; existing data is untouched.

## 7. Open items that need a decision or access

1. **Credentials for real calls.** Twilio, a speech recognizer (Deepgram and/or Soniox),
   ElevenLabs and Anthropic keys. Until they are configured, nothing in phases 1-2
   is verified on an actual phone line.
2. **Arabic recognition while switching languages.** Deepgram documents its
   multilingual model as covering many languages, but Arabic in code-switching mode
   is not confirmed from Deepgram's own pages. The default therefore refuses
   Arabic+switching on Deepgram. Options: use Soniox for multilingual campaigns, or
   confirm Deepgram on test calls and add `ar` to `DEEPGRAM_MULTILINGUAL_LANGUAGES`.
3. **Speech-to-speech comparison.** The brief asks for both architectures to be
   benchmarked. That needs the keys above and a fixed set of recorded test scenarios
   per language.
4. **Which markets first.** Calling rules differ by country (US TCPA, EU, UAE, Saudi
   Arabia). The compliance rule sets in phase 6 should be written for the markets
   DEVSYNX will actually call first, with legal review.
