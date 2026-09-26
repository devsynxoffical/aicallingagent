
import pytest
from pipecat.frames.frames import (
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


def test_live_call_options_default_to_no_thinking_pause():
    opts = live_call_request_options(Settings(anthropic_api_key="x"))
    assert opts["model"] == "claude-opus-5"
    assert opts["thinking"] == {"type": "disabled"}
    assert opts["output_config"] == {"effort": "low"}
    assert opts["betas"] == [] and "speed" not in opts


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
        frames_to_send=[UserStoppedSpeakingFrame(), SleepFrame(0.2), LLMTextFrame("So, the reason")],
        expected_down_frames=[UserStoppedSpeakingFrame, TTSSpeakFrame, LLMTextFrame],
    )
    filler = [f for f in down if isinstance(f, TTSSpeakFrame)][0]
    assert filler.text in FillerProcessor.__init__.__defaults__[1] if FillerProcessor.__init__.__defaults__ else True
    assert filler.append_to_context is False
    assert proc.filler_count == 1


@pytest.mark.asyncio
async def test_no_filler_when_model_is_fast_or_user_interrupts():
    proc = FillerProcessor(delay_secs=0.1)
    await run_test(
        proc,
        frames_to_send=[UserStoppedSpeakingFrame(), LLMTextFrame("Hi"), SleepFrame(0.2),
                        UserStoppedSpeakingFrame(), UserStartedSpeakingFrame(), SleepFrame(0.2)],
        expected_down_frames=[UserStoppedSpeakingFrame, LLMTextFrame, UserStoppedSpeakingFrame, UserStartedSpeakingFrame],
    )
    assert proc.filler_count == 0


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
