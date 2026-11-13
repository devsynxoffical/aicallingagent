import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    TTSUpdateSettingsFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.transcriptions.language import Language

from callagent.languages import LanguageTracker
from callagent.voice.fillers import FILLERS_BY_LANGUAGE, FillerProcessor
from callagent.voice.language_follower import LanguageFollower
from callagent.voice.latency import LatencyMonitor, record_response_latency, summarize


def _said(text: str, language: Language | None = None) -> TranscriptionFrame:
    return TranscriptionFrame(text=text, user_id="", timestamp="", language=language)


@pytest.mark.asyncio
async def test_follower_tracks_a_mid_call_switch_and_passes_transcripts_through():
    tracker = LanguageTracker(allowed=("en", "ar", "fr"))
    switches = []

    async def on_switch(switch):
        switches.append((switch.from_language, switch.to_language))
        return None

    down, _ = await run_test(
        LanguageFollower(tracker, on_switch=on_switch),
        frames_to_send=[_said("Can you explain your pricing?", Language.EN), _said("ممكن تشرح لي بالعربي؟", Language.AR)],
        expected_down_frames=[TranscriptionFrame, TranscriptionFrame],
    )
    assert [f.text for f in down] == ["Can you explain your pricing?", "ممكن تشرح لي بالعربي؟"]  # nothing dropped or reordered
    assert switches == [("en", "ar")] and tracker.current == "ar"


@pytest.mark.asyncio
async def test_follower_sends_the_voice_update_ahead_of_the_transcript():
    from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

    tracker = LanguageTracker(allowed=("en", "fr"))

    async def on_switch(switch):
        return TTSUpdateSettingsFrame(delta=ElevenLabsTTSService.Settings(voice=f"voice-{switch.to_language}"))

    down, _ = await run_test(
        LanguageFollower(tracker, on_switch=on_switch),
        frames_to_send=[_said("Bonjour, vous parlez français ?", Language.FR)],
        expected_down_frames=[TTSUpdateSettingsFrame, TranscriptionFrame],
    )
    assert down[0].delta.voice == "voice-fr"


@pytest.mark.asyncio
async def test_filler_is_spoken_in_the_language_of_the_call():
    current = {"lang": "ar"}
    proc = FillerProcessor(delay_secs=0.05, language=lambda: current["lang"])
    down, _ = await run_test(
        proc,
        frames_to_send=[UserStoppedSpeakingFrame(), LLMFullResponseStartFrame(), SleepFrame(0.2), LLMTextFrame("نعم")],
        expected_down_frames=[UserStoppedSpeakingFrame, LLMFullResponseStartFrame, TTSSpeakFrame, LLMTextFrame],
    )
    filler = [f for f in down if isinstance(f, TTSSpeakFrame)][0]
    assert filler.text in FILLERS_BY_LANGUAGE["ar"] and filler.text not in FILLERS_BY_LANGUAGE["en"]


@pytest.mark.asyncio
async def test_no_filler_rather_than_one_in_the_wrong_language():
    proc = FillerProcessor(delay_secs=0.05, language=lambda: "de")  # no German fillers written
    await run_test(
        proc,
        frames_to_send=[UserStoppedSpeakingFrame(), LLMFullResponseStartFrame(), SleepFrame(0.2), LLMTextFrame("Ja")],
        expected_down_frames=[UserStoppedSpeakingFrame, LLMFullResponseStartFrame, LLMTextFrame],
    )
    assert proc.filler_count == 0


@pytest.mark.asyncio
async def test_interruption_time_is_measured_from_barge_in_to_silence():
    sink: dict = {}
    await run_test(
        LatencyMonitor(sink),
        frames_to_send=[
            BotStartedSpeakingFrame(), SleepFrame(0.05),
            UserStartedSpeakingFrame(), SleepFrame(0.12),
            BotStoppedSpeakingFrame(), SleepFrame(0.02),
            # The prospect starting to talk while the agent is silent is a normal turn, not an interruption.
            UserStartedSpeakingFrame(), SleepFrame(0.02),
            BotStartedSpeakingFrame(), SleepFrame(0.02), BotStoppedSpeakingFrame(),
        ],
    )
    assert len(sink["interruption"]) == 1
    assert 0.08 <= sink["interruption"][0] <= 0.4


def test_response_latency_is_summarised_per_call():
    sink: dict = {}
    for seconds in (0.62, 0.71, 0.0, 0.95, 0.58):
        record_response_latency(sink, seconds)
    assert sink["response"] == [0.62, 0.71, 0.95, 0.58]  # a zero is a non-measurement, not a fast turn
    out = summarize(sink)
    assert out["response"]["n"] == 4 and out["response"]["p50"] == pytest.approx(0.665) and out["response"]["max"] == 0.95
