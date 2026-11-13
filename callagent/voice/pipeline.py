"""The real-time voice pipeline for one phone call.

Twilio media stream (8 kHz mu-law over WebSocket)
  -> streaming speech-to-text (provider adapter; one language or following the prospect)
  -> language follower (which language is the prospect speaking right now?)
  -> Claude (Pipecat AnthropicLLMService, tools, prompt caching)
  -> streaming voice (provider adapter)
  -> back to Twilio

Every hop streams. Silero VAD gives barge-in: when the prospect starts talking the bot stops.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndWorkerFrame, LLMMessagesAppendFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

from ..config import Settings, get_settings
from datetime import timedelta

from ..db import CallRun, Campaign, Lead, campaign_organization_id, run_db, utcnow
from ..dialer import lifecycle
from ..languages import LanguageSwitch, LanguageTracker
from ..playbook.prompt_builder import build_system_prompt
from ..playbook.schema import Playbook
from ..postcall.summarizer import finalize_call
from ..providers import SpeechPlan, get_stt, get_tts
from ..training.search import KnowledgeIndex, load_index
from .fillers import FillerProcessor
from .language_follower import LanguageFollower
from .latency import LatencyMonitor, record_response_latency, summarize
from .llm_service import LowLatencyAnthropicLLMService
from .session import CallSession
from .speech_filter import SpeechSanitizer
from .tools import build_tools_schema, register_tool_handlers
from .transcript import TranscriptCollector

log = logging.getLogger(__name__)

TWILIO_SAMPLE_RATE = 8000

# Last resort when the line stays silent and the model did not close the call itself.
SILENCE_GOODBYE = {
    "en": "Alright, I'll let you go. Have a good one.",
    "fr": "Très bien, je vous laisse. Bonne journée.",
    "ar": "تمام، لن أطيل عليكم. يومكم سعيد.",
}


def _playbook_for(s, campaign: Campaign, run: CallRun) -> Playbook:
    """The playbook this call runs: the version pinned on the call when it is part of an
    A/B test, otherwise whatever is live now."""
    if run.experiment_id is not None and run.playbook_version is not None:
        from ..training import versions

        try:
            return Playbook.model_validate(versions.get_version(s, campaign, run.playbook_version).playbook_json)
        except versions.VersionError:
            log.warning("call %s: pinned playbook version %s is missing, using the live one", run.id, run.playbook_version)
    return Playbook.model_validate(campaign.playbook_json)


async def load_call_context(call_run_id: int) -> tuple[dict[str, Any], Playbook, int, str | None, str, KnowledgeIndex]:
    def _load(s):
        run = s.get(CallRun, call_run_id)
        if run is None:
            raise ValueError(f"unknown call_run_id {call_run_id}")
        lead = s.get(Lead, run.lead_id)
        campaign = s.get(Campaign, run.campaign_id)
        if lead is None or campaign is None:
            raise ValueError("call has no lead/campaign")
        lead_dict = {
            "id": lead.id,
            "phone_e164": lead.phone_e164,
            "first_name": lead.first_name,
            "last_name": lead.last_name,
            "company": lead.company,
            "email": lead.email,
            "timezone": lead.timezone,
            "extra": lead.extra or {},
            "notes": lead.notes,
            "attempts": lead.attempts,
        }
        lifecycle.advance(run, "in-progress")
        run.stream_connected = True
        # Built once per call, for this organization and campaign only.
        knowledge = load_index(s, campaign_organization_id(s, campaign), campaign.id)
        return lead_dict, _playbook_for(s, campaign, run), campaign.id, run.twilio_call_sid, run.direction or "outbound", knowledge

    return await run_db(_load)


async def mark_call_setup_failed(call_run_id: int, error: str, base_settings: Settings) -> None:
    """The stream connected but we could not build the pipeline: close the run and give the lead a retry."""

    def _fail(s):
        from ..dialer.rules import campaign_settings

        run = s.get(CallRun, call_run_id)
        if run is None:
            return
        settings = campaign_settings(s, run.campaign_id, base_settings)
        lifecycle.advance(run, "failed")
        run.error = error[:2000]
        lead = s.get(Lead, run.lead_id)
        if lead and lead.status == "calling":
            lead.status = "pending" if lead.attempts < settings.max_attempts else "completed"
            lead.next_attempt_at = utcnow() + timedelta(minutes=settings.retry_delay_minutes)

    await run_db(_fail)


async def run_call(websocket: WebSocket, stream_sid: str, call_sid: str, body: dict[str, Any], settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    call_run_id = int(body.get("call_run_id", 0))
    mode = str(body.get("mode", "live"))
    lead, playbook, campaign_id, stored_sid, direction, knowledge = await load_call_context(call_run_id)
    if direction == "inbound":
        mode = "inbound"
    try:
        await _run_call(websocket, stream_sid, stored_sid or call_sid, body, call_run_id, mode, lead, playbook, campaign_id, settings, direction, knowledge)
    except Exception as e:
        log.exception("call %s: could not start the voice pipeline", call_run_id)
        await mark_call_setup_failed(call_run_id, f"pipeline setup failed: {e}", settings)
        raise


async def _run_call(websocket, stream_sid, call_sid, body, call_run_id, mode, lead, playbook, campaign_id, settings, direction="outbound", knowledge=None) -> None:
    plan = SpeechPlan.from_playbook(playbook)
    session = CallSession(
        call_run_id=call_run_id,
        lead=lead,
        campaign_id=campaign_id,
        playbook=playbook,
        settings=settings,
        twilio_call_sid=call_sid or "",
        mode=mode,
        direction=direction,
        language=LanguageTracker(allowed=plan.languages),
        knowledge=knowledge,
    )
    has_knowledge = knowledge is not None and len(knowledge) > 0 and mode != "voicemail"

    human_transfer = bool(settings.human_transfer_number)
    system_prompt = build_system_prompt(
        playbook,
        lead,
        mode=mode,
        callback_number=settings.twilio_from_number or "",
        human_transfer_available=human_transfer,
        # Outbound recording is requested when the call is placed; inbound calls are not recorded yet.
        call_recorded=settings.record_calls and direction == "outbound",
        knowledge_languages=knowledge.languages if has_knowledge else None,
    )

    # ---- transport
    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=session.twilio_call_sid or None,
        account_sid=settings.twilio_account_sid,
        auth_token=settings.twilio_auth_token,
    )
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
            session_timeout=settings.call_time_limit_seconds + 60,
        ),
    )

    # ---- ears
    stt = get_stt(settings).build(settings, plan)

    # ---- brain (thinking off by default, prompt caching on, optional fast mode)
    llm = LowLatencyAnthropicLLMService(
        api_key=settings.anthropic_api_key or "",
        system_prompt=system_prompt,
        settings=settings,
    )
    register_tool_handlers(llm, session)

    # ---- voice
    tts_adapter = get_tts(settings)
    tts = tts_adapter.build(settings, plan, text_filters=[SpeechSanitizer()])

    async def on_language_switch(switch: LanguageSwitch):
        session.log_tool("language_switch", {"from": switch.from_language, "to": switch.to_language, "turn": switch.turn}, {"ok": True})
        return tts_adapter.on_language_switch(settings, plan, switch.to_language)

    follower = LanguageFollower(session.language, on_switch=on_language_switch)

    # ---- memory & turn-taking
    capabilities = {c.key for c in playbook.required_capabilities}
    context = LLMContext(tools=build_tools_schema(capabilities, human_transfer, knowledge_available=has_knowledge))
    user_params = LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(params=VADParams(confidence=0.7, start_secs=0.2, stop_secs=0.25, min_volume=0.5)),
        user_idle_timeout=settings.user_idle_seconds,
        user_turn_stop_timeout=3.0,
    )
    # Pipecat's default end-of-turn detector is the bundled local Smart Turn v3 model
    # (semantic end-of-turn, fewer awkward cut-offs). USE_SMART_TURN=false falls back to a
    # plain speech-timeout after the VAD sees silence, which is cheaper on CPU.
    if not settings.use_smart_turn:
        from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
        from pipecat.turns.user_turn_strategies import UserTurnStrategies

        user_params.user_turn_strategies = UserTurnStrategies(
            stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)]
        )

    aggregators = LLMContextAggregatorPair(context, user_params=user_params)
    collector = TranscriptCollector(session)
    latency_monitor = LatencyMonitor(session.latency)

    processors = [transport.input(), stt, follower, aggregators.user(), llm]
    if settings.call_fillers and mode != "voicemail":
        processors.append(FillerProcessor(delay_secs=settings.filler_delay_ms / 1000, language=lambda: session.language.current))
    processors += [tts, transport.output(), collector, latency_monitor, aggregators.assistant()]
    pipeline = Pipeline(processors)

    # End of the prospect's turn -> first agent audio: the number the prospect actually feels.
    response_latency = UserBotLatencyObserver()

    @response_latency.event_handler("on_latency_measured")
    async def on_latency_measured(_observer, seconds: float):
        record_response_latency(session.latency, seconds)

    task = PipelineWorker(
        pipeline,
        observers=[response_latency],
        params=PipelineParams(
            audio_in_sample_rate=TWILIO_SAMPLE_RATE,
            audio_out_sample_rate=TWILIO_SAMPLE_RATE,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        idle_timeout_secs=90,
        app_resources=session,
    )

    opener_timer: asyncio.Task | None = None

    async def cue(text: str) -> None:
        """Feed the model a stage direction and let it speak. Never runs on an empty context."""
        await task.queue_frame(LLMMessagesAppendFrame(messages=[{"role": "user", "content": text}], run_llm=True))

    async def speak_first_after_delay():
        # On outbound calls the callee usually says "Hello?" first. Give them a moment;
        # if they stay silent, open the conversation ourselves.
        try:
            await asyncio.sleep(settings.opener_delay_seconds)
            if not session.user_has_spoken:
                await cue("(The call connected but the prospect hasn't said anything yet. Open the call naturally, as if they just picked up.)")
        except asyncio.CancelledError:
            pass

    @transport.event_handler("on_client_connected")
    async def on_connected(_transport, _client):
        nonlocal opener_timer
        log.info("call %s connected (mode=%s)", call_run_id, mode)
        if mode == "voicemail":
            await cue("(You have reached voicemail and the beep has just played. Leave the message now.)")
        elif mode == "inbound":
            # They called us and are waiting: greet straight away.
            await cue("(An inbound caller has just connected. Greet them now.)")
        else:
            opener_timer = asyncio.create_task(speak_first_after_delay())

    @transport.event_handler("on_client_disconnected")
    async def on_disconnected(_transport, _client):
        log.info("call %s: prospect hung up / stream closed", call_run_id)
        if opener_timer:
            opener_timer.cancel()
        await task.cancel()

    @transport.event_handler("on_session_timeout")
    async def on_timeout(_transport, _client):
        await task.cancel()

    @aggregators.user().event_handler("on_user_turn_started")
    async def on_user_turn_started(_agg, *_):
        session.user_has_spoken = True
        session.idle_nudges = 0  # they are still with us
        if opener_timer and not opener_timer.done():
            opener_timer.cancel()

    @aggregators.user().event_handler("on_user_turn_message_added")
    async def on_user_turn_message_added(_agg, message, *_):
        # The user aggregator consumes TranscriptionFrames, so the prospect's words are
        # captured here, from the same aggregated text the model receives.
        text = getattr(message, "content", None)
        if isinstance(text, str) and text.strip():
            collector.flush_bot()
            session.add_transcript("user", text)

    @aggregators.user().event_handler("on_user_turn_idle")
    async def on_user_idle(_agg, *_):
        if session.ended_by_agent:
            return
        if mode == "voicemail":
            # Idle fires once the bot has finished speaking: the message is delivered, hang up
            # even if the model forgot end_call.
            session.end_reason = session.end_reason or "voicemail delivered"
            await task.queue_frame(EndWorkerFrame(reason="voicemail delivered"))
            return
        session.idle_nudges += 1
        if session.idle_nudges == 1:
            await cue("(The line has gone quiet for a while. Check briefly if they are still there, in one short sentence.)")
        elif session.idle_nudges == 2:
            await cue("(Still silence. Say a short, warm goodbye and call end_call with reason 'no response'.)")
        else:
            session.end_reason = "silence"
            goodbye = SILENCE_GOODBYE.get(session.language.current, SILENCE_GOODBYE["en"])
            await task.queue_frames([TTSSpeakFrame(goodbye), EndWorkerFrame(reason="silence")])

    runner = PipelineRunner(handle_sigint=False)
    error: str | None = None
    try:
        await runner.run(task)
    except Exception as e:
        error = str(e)
        log.exception("call %s pipeline failed", call_run_id)
    finally:
        if opener_timer:
            opener_timer.cancel()

    if error:
        def _err(s):
            run = s.get(CallRun, call_run_id)
            if run:
                run.error = error
                run.ended_at = utcnow()
        await run_db(_err)

    try:
        await finalize_call(
            call_run_id,
            playbook,
            session.transcript,
            session.tool_events,
            mode,
            session.disposition,
            settings,
            latency=summarize(session.latency),
            language=session.language.report(),
            direction=direction,
        )
    except Exception:
        log.exception("call %s: post-call processing failed", call_run_id)
    log.info("call %s latency: %s", call_run_id, summarize(session.latency))
