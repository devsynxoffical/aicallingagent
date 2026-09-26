
import pytest
from pipecat.frames.frames import (
    LLMFullResponseStartFrame,
    LLMTextFrame,
    MetricsFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import TTFBMetricsData
from pipecat.tests.utils import SleepFrame, run_test

from callagent.config import Settings
from callagent.llm import live_call_request_options
from callagent.voice.fillers import FillerProcessor
from callagent.voice.latency import LatencyMonitor, summarize


def test_live_call_options_defaults():
    opts = live_call_request_options(Settings(anthropic_api_key="x"))
    assert opts["model"] == "claude-opus-5"
    assert opts["thinking"] == {"type": "adaptive"}  # thinks only when a turn needs it
    assert opts["output_config"] == {"effort": "low"}
    assert opts["max_tokens"] >= 1024  # goodbye + log_call_outcome + end_call never truncate
    assert opts["betas"] == [] and "speed" not in opts


def test_live_call_options_thinking_can_be_disabled():
    opts = live_call_request_options(Settings(anthropic_api_key="x", call_thinking="disabled"))
    assert opts["thinking"] == {"type": "disabled"}


def test_live_call_options_fast_mode_and_adaptive():
    opts = live_call_request_options(Settings(anthropic_api_key="x", call_fast_mode=True, call_thinking="adaptive", call_model="claude-sonnet-5"))
    assert opts["speed"] == "fast" and "fast-mode-2026-02-01" in opts["betas"]
    assert opts["thinking"] == {"type": "adaptive"}
    assert opts["model"] == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_filler_speaks_when_model_is_slow():
    proc = FillerProcessor(delay_secs=0.05)
    down, _ = await run_test(
        proc,
        frames_to_send=[UserStoppedSpeakingFrame(), LLMFullResponseStartFrame(), SleepFrame(0.2), LLMTextFrame("So, the reason")],
        expected_down_frames=[UserStoppedSpeakingFrame, LLMFullResponseStartFrame, TTSSpeakFrame, LLMTextFrame],
    )
    filler = [f for f in down if isinstance(f, TTSSpeakFrame)][0]
    from callagent.voice.fillers import NEUTRAL_FILLERS

    assert filler.text in NEUTRAL_FILLERS
    assert filler.append_to_context is False
    assert proc.filler_count == 1


@pytest.mark.asyncio
async def test_no_filler_when_model_is_fast_or_user_interrupts():
    proc = FillerProcessor(delay_secs=0.1)
    await run_test(
        proc,
        frames_to_send=[UserStoppedSpeakingFrame(), LLMFullResponseStartFrame(), LLMTextFrame("Hi"), SleepFrame(0.2),
                        # system frames (interruptions) overtake queued data frames; a tiny sleep keeps the order deterministic
                        UserStoppedSpeakingFrame(), LLMFullResponseStartFrame(), SleepFrame(0.02), UserStartedSpeakingFrame(), SleepFrame(0.2),
                        # a second model round in the same turn (after a tool call) must not add a second filler
                        UserStoppedSpeakingFrame(), LLMFullResponseStartFrame(), SleepFrame(0.2), LLMFullResponseStartFrame(), SleepFrame(0.2)],
        expected_down_frames=[UserStoppedSpeakingFrame, LLMFullResponseStartFrame, LLMTextFrame, UserStoppedSpeakingFrame, LLMFullResponseStartFrame,
                              UserStartedSpeakingFrame, UserStoppedSpeakingFrame, LLMFullResponseStartFrame, TTSSpeakFrame, LLMFullResponseStartFrame],
    )
    assert proc.filler_count == 1  # only the third turn, and only once despite two model rounds


@pytest.mark.asyncio
async def test_latency_monitor_collects_ttfb():
    sink: dict = {}
    mon = LatencyMonitor(sink)
    await run_test(
        mon,
        frames_to_send=[
            MetricsFrame(data=[TTFBMetricsData(processor="LowLatencyAnthropicLLMService#0", value=0.61)]),
            MetricsFrame(data=[TTFBMetricsData(processor="ElevenLabsTTSService#0", value=0.18), TTFBMetricsData(processor="LowLatencyAnthropicLLMService#0", value=0.45)]),
        ],
        expected_down_frames=[MetricsFrame, MetricsFrame],
    )
    assert sink["llm_ttfb"] == [0.61, 0.45] and sink["tts_ttfb"] == [0.18]
    s = summarize(sink)
    assert s["llm_ttfb"]["n"] == 2 and s["est_response_gap_p50"] == pytest.approx(0.53 + 0.18, abs=0.1)
