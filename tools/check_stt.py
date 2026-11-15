"""Check the engine's speech-to-text against the real provider.

Two ways, both with the model, language and options a call uses
(`callagent.providers.get_stt(...).stt_settings`):

- **streaming** (the default): recorded sentences go through the same adapter a call
  uses, at the pace and in the format a phone line delivers them (16-bit PCM, 8 kHz,
  20 ms at a time). Reports what was heard and how long after the person stopped
  speaking the final transcript arrived. Needs a direct connection to the provider:
  it is a WebSocket, which some sandboxes and corporate proxies do not carry.
- **--prerecorded**: each sentence is sent whole over HTTPS. Reports what was heard.
  Says nothing about delay on a live call. Deepgram only.

    python tools/check_stt.py --audio-dir DIR --language en [--language fr ...] [--follow] [--prerecorded]

DIR holds `<lang>-<n>.wav` files (8 kHz mono 16-bit) and `phrases.json`:
{"en": {"lines": ["sentence 0", ...]}, ...}. `--follow` uses the multi-language mode
(the call may change language); without it each language is tested on its own.

This spends the provider's credit: a few cents for a few dozen sentences.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
import unicodedata
import wave
from pathlib import Path

from pipecat.frames.frames import EndFrame, InputAudioRawFrame, InterimTranscriptionFrame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from callagent.config import get_settings
from callagent.providers import SpeechPlan, get_stt

RATE = 8000
CHUNK_MS = 20
SILENCE_AFTER_MS = 2500  # the line stays open after the person stops, as on a call


def words(text: str) -> list[str]:
    text = re.sub(r"[ً-ْـ]", "", unicodedata.normalize("NFKC", text.lower()))
    return re.findall(r"[^\W_]+", text, flags=re.UNICODE)


def word_errors(said: str, heard: str) -> tuple[int, int]:
    """Words that differ (insertions, deletions, substitutions), and how many were said."""
    r, h = words(said), words(heard)
    row = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, row[0] = row[0], i
        for j in range(1, len(h) + 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
    return row[len(h)], len(r)


async def prerecorded(path: Path, options: dict) -> dict:
    """One sentence, sent whole to Deepgram with the options a call would use (the
    ones that only mean something on a live stream are left out)."""
    import httpx

    params: list[tuple[str, str]] = []
    for key, value in options.items():
        if key in ("interim_results", "endpointing", "language_hint"):
            continue
        for item in value if isinstance(value, (list, tuple)) else [value]:
            params.append((key, str(item).lower() if isinstance(item, bool) else str(getattr(item, "value", item))))
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post("https://api.deepgram.com/v1/listen", params=params, content=path.read_bytes(),
                                     headers={"Content-Type": "audio/wav", "Authorization": f"Token {get_settings().deepgram_api_key}"})
    if response.status_code != 200:
        return {"heard": "", "error": f"HTTP {response.status_code}: {response.text[:200]}", "pieces": 0, "final_after_speech_ms": None, "first_words_after_start_ms": None}
    best = response.json()["results"]["channels"][0]["alternatives"][0]
    return {"heard": best["transcript"], "confidence": round(best.get("confidence") or 0, 3), "pieces": 1, "final_after_speech_ms": None,
            "first_words_after_start_ms": None, "request_ms": round((time.monotonic() - started) * 1000)}


class Listener(FrameProcessor):
    """Collects what the provider says it heard, with the moment each piece arrived."""

    def __init__(self):
        super().__init__()
        self.finals: list[tuple[float, str]] = []
        self.first_interim: float | None = None

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        now = time.monotonic()
        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            self.finals.append((now, frame.text.strip()))
        elif isinstance(frame, InterimTranscriptionFrame) and self.first_interim is None and frame.text.strip():
            self.first_interim = now
        await self.push_frame(frame, direction)


async def one_sentence(path: Path, plan: SpeechPlan) -> dict:
    settings = get_settings()
    stt = get_stt(settings).build(settings, plan)
    listener = Listener()
    worker = PipelineWorker(Pipeline([stt, listener]), params=PipelineParams(audio_in_sample_rate=RATE, audio_out_sample_rate=RATE), idle_timeout_secs=30)
    with wave.open(str(path)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (RATE, 1, 2), f"{path} must be 8 kHz mono 16-bit"
        audio = w.readframes(w.getnframes())
    step = RATE * 2 * CHUNK_MS // 1000
    marks: dict[str, float] = {}

    async def speak():
        await asyncio.sleep(1.0)  # the connection opens first, as it does before a call's first word
        marks["start"] = time.monotonic()
        for offset in range(0, len(audio), step):
            await worker.queue_frames([InputAudioRawFrame(audio=audio[offset:offset + step], sample_rate=RATE, num_channels=1)])
            await asyncio.sleep(CHUNK_MS / 1000)
        marks["end"] = time.monotonic()
        silence = bytes(step)
        for _ in range(SILENCE_AFTER_MS // CHUNK_MS):
            await worker.queue_frames([InputAudioRawFrame(audio=silence, sample_rate=RATE, num_channels=1)])
            await asyncio.sleep(CHUNK_MS / 1000)
        await worker.queue_frames([EndFrame()])

    speaker = asyncio.create_task(speak())
    await PipelineRunner(handle_sigint=False).run(worker)
    await speaker
    heard = " ".join(text for _at, text in listener.finals)
    return {
        "heard": heard,
        "pieces": len(listener.finals),
        # From the last sample of speech to the last final transcript: what the agent waits before it can answer.
        "final_after_speech_ms": round((listener.finals[-1][0] - marks["end"]) * 1000) if listener.finals else None,
        "first_words_after_start_ms": round((listener.first_interim - marks["start"]) * 1000) if listener.first_interim else None,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--audio-dir", required=True, type=Path)
    parser.add_argument("--language", action="append", required=True)
    parser.add_argument("--follow", action="store_true", help="multi-language mode: one plan with every --language given")
    parser.add_argument("--prerecorded", action="store_true", help="send each sentence whole over HTTPS instead of streaming it")
    parser.add_argument("--keyterm", action="append", default=[], help="a name the call should recognise (English, single-language mode)")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    phrases = json.loads((args.audio_dir / "phrases.json").read_text())
    settings = get_settings()
    adapter = get_stt(settings)
    results = []
    for lang in args.language:
        plan = SpeechPlan(primary_tag=lang, languages=tuple([lang] + [x for x in args.language if x != lang]) if args.follow else (lang,), keyterms=tuple(args.keyterm))
        options = adapter.stt_settings(settings, plan)
        print(f"\n{adapter.name}, {lang}, {'multi-language' if args.follow else 'single-language'}, {'prerecorded' if args.prerecorded else 'streaming'}: {options}")
        errors = total = 0
        delays = []
        for i, said in enumerate(phrases[lang]["lines"]):
            path = args.audio_dir / f"{lang}-{i}.wav"
            got = await (prerecorded(path, options) if args.prerecorded else one_sentence(path, plan))
            e, n = word_errors(said, got["heard"])
            errors, total = errors + e, total + n
            if got["final_after_speech_ms"] is not None:
                delays.append(got["final_after_speech_ms"])
            results.append({"language": lang, "follow": args.follow, "said": said, **got, "word_errors": e, "words": n})
            note = got.get("error") or (f"final {got['final_after_speech_ms']} ms after speech ended, in {got['pieces']} piece(s)" if not args.prerecorded else f"confidence {got.get('confidence')}")
            print(f"  said : {said}\n  heard: {got['heard'] or '(nothing)'}\n         {note}")
        delays.sort()
        timing = "" if args.prerecorded else f"; final transcript after speech: median {delays[len(delays) // 2] if delays else None} ms, worst {delays[-1] if delays else None} ms"
        print(f"  => {errors}/{total} words differ{timing}")
    if args.out:
        args.out.write_text(json.dumps(results, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
