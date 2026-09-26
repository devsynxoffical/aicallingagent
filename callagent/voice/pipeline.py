"""The real-time voice pipeline for one phone call.

Twilio media stream (8 kHz mu-law over WebSocket)
  -> Deepgram streaming STT (nova-3, biased to the playbook's key terms)
  -> Claude (Pipecat AnthropicLLMService, tools, prompt caching)
  -> ElevenLabs streaming TTS (flash model for low latency)
  -> back to Twilio

Silero VAD gives barge-in: when the prospect starts talking the bot stops.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import EndWorkerFrame, LLMMessagesAppendFrame, LLMRunFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

from ..config import Settings, get_settings
from ..db import CallRun, Lead, Campaign, run_db, utcnow
from ..playbook.prompt_builder import build_system_prompt
from ..playbook.schema import Playbook
from ..postcall.summarizer import finalize_call
from .fillers import FillerProcessor
from .latency import LatencyMonitor, summarize
from .llm_service import LowLatencyAnthropicLLMService
from .session import CallSession
from .tools import build_tools_schema, register_tool_handlers
from .transcript import TranscriptCollector

log = logging.getLogger(__name__)

TWILIO_SAMPLE_RATE = 8000


def _language(tag: str) -> Language | None:
    """Map a BCP-47 tag from the playbook to Pipecat's Language enum, best effort."""
    candidates = [tag.replace("-", "_"), tag.split("-")[0]]
    for c in candidates:
        try:
            return Language(c.replace("_", "-"))
        except ValueError:
            continue
        except Exception:
            continue
    for name in (tag.replace("-", "_").upper(), tag.split("-")[0].upper()):
        if hasattr(Language, name):
            return getattr(Language, name)
    return None


async def load_call_context(call_run_id: int) -> tuple[dict[str, Any], Playbook, int, str | None]:
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
        run.status = "in-progress"
        return lead_dict, Playbook.model_validate(campaign.playbook_json), campaign.id, run.twilio_call_sid

    return await run_db(_load)


async def run_call(websocket: WebSocket, stream_sid: str, call_sid: str, body: dict[str, Any], settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    call_run_id = int(body.get("call_run_id", 0))
    mode = str(body.get("mode", "live"))
    lead, playbook, campaign_id, stored_sid = await load_call_context(call_run_id)

    session = CallSession(
        call_run_id=call_run_id,
        lead=lead,
        campaign_id=campaign_id,
        playbook=playbook,
        settings=settings,
        twilio_call_sid=call_sid or stored_sid or "",
        mode=mode,
    )

    human_transfer = bool(settings.human_transfer_number)
    system_prompt = build_system_prompt(
        playbook,
        lead,
        mode=mode,
        callback_number=settings.twilio_from_number or "",
        human_transfer_available=human_transfer,
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
    lang = _language(playbook.persona.language)
    stt_settings: dict[str, Any] = {
        "model": settings.deepgram_model,
        "smart_format": True,
        "punctuate": True,
        "interim_results": True,
        "endpointing": settings.deepgram_endpointing_ms,
    }
    if lang is not None:
        stt_settings["language"] = lang
    if playbook.keyterms:
        stt_settings["keyterm"] = playbook.keyterms[:50]
    stt = DeepgramSTTService(api_key=settings.deepgram_api_key or "", settings=DeepgramSTTService.Settings(**stt_settings))

    # ---- brain (thinking off by default, prompt caching on, optional fast mode)
    llm = LowLatencyAnthropicLLMService(
        api_key=settings.anthropic_api_key or "",
        system_prompt=system_prompt,
        settings=settings,
    )
    register_tool_handlers(llm, session)

    # ---- voice
    tts_settings: dict[str, Any] = {
        "model": settings.elevenlabs_model,
        "voice": settings.elevenlabs_voice_id,
        "stability": settings.elevenlabs_stability,
        "similarity_boost": settings.elevenlabs_similarity,
        "style": settings.elevenlabs_style,
        "use_speaker_boost": True,
        "speed": settings.elevenlabs_speed,
    }
    if lang is not None:
        tts_settings["language"] = lang
    tts = ElevenLabsTTSService(api_key=settings.elevenlabs_api_key or "", settings=ElevenLabsTTSService.Settings(**tts_settings))

    # ---- memory & turn-taking
    capabilities = {c.key for c in playbook.required_capabilities}
    context = LLMContext(tools=build_tools_schema(capabilities, human_transfer))
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

    processors = [transport.input(), stt, aggregators.user(), llm]
    if settings.call_fillers and mode == "live":
        processors.append(FillerProcessor(delay_secs=settings.filler_delay_ms / 1000))
    processors += [tts, transport.output(), collector, latency_monitor, aggregators.assistant()]
    pipeline = Pipeline(processors)

    task = PipelineWorker(
        pipeline,
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

    async def speak_first_after_delay():
        # On outbound calls the callee usually says "Hello?" first. Give them a moment;
        # if they stay silent, open the conversation ourselves.
        try:
            await asyncio.sleep(settings.opener_delay_seconds)
            if not session.user_has_spoken:
                await task.queue_frame(LLMRunFrame())
        except asyncio.CancelledError:
            pass

    @transport.event_handler("on_client_connected")
    async def on_connected(_transport, _client):
        nonlocal opener_timer
        log.info("call %s connected (mode=%s)", call_run_id, mode)
        if mode == "voicemail":
            await task.queue_frame(LLMRunFrame())
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
        if opener_timer and not opener_timer.done():
            opener_timer.cancel()

    @aggregators.user().event_handler("on_user_turn_idle")
    async def on_user_idle(_agg, *_):
        if mode == "voicemail" or session.ended_by_agent:
            return
        session.idle_nudges += 1
        if session.idle_nudges == 1:
            await task.queue_frame(
                LLMMessagesAppendFrame(
                    messages=[{"role": "user", "content": "(The line has gone quiet for a while. Check briefly if they are still there, in one short sentence.)"}],
                    run_llm=True,
                )
            )
        elif session.idle_nudges == 2:
            await task.queue_frame(
                LLMMessagesAppendFrame(
                    messages=[{"role": "user", "content": "(Still silence. Say a short, warm goodbye and call end_call with reason 'no response'.)"}],
                    run_llm=True,
                )
            )
        else:
            session.end_reason = "silence"
            await task.queue_frames([TTSSpeakFrame("Alright, I'll let you go. Have a good one."), EndWorkerFrame(reason="silence")])

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
        )
    except Exception:
        log.exception("call %s: post-call processing failed", call_run_id)
    log.info("call %s latency: %s", call_run_id, summarize(session.latency))
