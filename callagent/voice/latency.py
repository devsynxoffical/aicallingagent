"""Collect per-turn latency numbers so every call ends with a measurable answer to
"was it fast?": Deepgram time-to-final, Claude time-to-first-token, ElevenLabs
time-to-first-audio."""

from __future__ import annotations

from statistics import median
from typing import Any

from pipecat.frames.frames import Frame, MetricsFrame
from pipecat.metrics.metrics import TTFBMetricsData
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class LatencyMonitor(FrameProcessor):
    def __init__(self, sink: dict[str, list[float]], **kwargs):
        super().__init__(**kwargs)
        self._sink = sink

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, MetricsFrame):
            for d in frame.data:
                if isinstance(d, TTFBMetricsData) and d.value > 0:
                    key = _short_name(d.processor)
                    self._sink.setdefault(key, []).append(round(d.value, 3))
        await self.push_frame(frame, direction)


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
