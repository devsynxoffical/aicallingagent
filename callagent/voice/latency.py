"""Collect per-turn latency numbers so every call ends with a measurable answer to
"was it fast?".

Per stage: speech-to-text time-to-final, model time-to-first-token, voice
time-to-first-audio. End to end: ``response`` (prospect stops talking -> agent audio
starts, measured by Pipecat's UserBotLatencyObserver) and ``interruption`` (prospect
starts talking over the agent -> agent audio stops).
"""

from __future__ import annotations

from statistics import median
from typing import Any

import time

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    MetricsFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.metrics.metrics import TTFBMetricsData
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class LatencyMonitor(FrameProcessor):
    def __init__(self, sink: dict[str, list[float]], **kwargs):
        super().__init__(**kwargs)
        self._sink = sink
        self._bot_speaking = False
        self._barge_in_at: float | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, MetricsFrame):
            for d in frame.data:
                if isinstance(d, TTFBMetricsData) and d.value > 0:
                    key = _short_name(d.processor)
                    self._sink.setdefault(key, []).append(round(d.value, 3))
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self._barge_in_at = None
        elif isinstance(frame, (UserStartedSpeakingFrame, VADUserStartedSpeakingFrame)):
            if self._bot_speaking and self._barge_in_at is None:
                self._barge_in_at = time.monotonic()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            if self._barge_in_at is not None:
                self._sink.setdefault("interruption", []).append(round(time.monotonic() - self._barge_in_at, 3))
            self._bot_speaking = False
            self._barge_in_at = None
        await self.push_frame(frame, direction)


def record_response_latency(sink: dict[str, list[float]], seconds: float) -> None:
    """One end-of-turn -> first-audio measurement from UserBotLatencyObserver."""
    if seconds and seconds > 0:
        sink.setdefault("response", []).append(round(float(seconds), 3))


def _short_name(processor: str) -> str:
    name = processor.split("#")[0]
    if "STT" in name:
        return "stt_ttfb"
    if "LLM" in name:
        return "llm_ttfb"
    if "TTS" in name:
        return "tts_ttfb"
    return name


def summarize(samples: dict[str, list[float]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, vals in samples.items():
        if vals:
            s = sorted(vals)
            out[k] = {"n": len(s), "p50": round(median(s), 3), "p95": round(s[min(len(s) - 1, int(len(s) * 0.95))], 3), "max": s[-1]}
    if "llm_ttfb" in out and "tts_ttfb" in out:
        out["est_response_gap_p50"] = round(out["llm_ttfb"]["p50"] + out["tts_ttfb"]["p50"], 3)
    return out
