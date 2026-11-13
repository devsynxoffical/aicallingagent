import pytest
from pipecat.frames.frames import TTSUpdateSettingsFrame
from pipecat.transcriptions.language import Language

from callagent.config import Settings
from callagent.providers import SpeechPlan, get_stt, get_tts, speech_plan_problems

BASE = dict(anthropic_api_key="x", deepgram_api_key="d", elevenlabs_api_key="e", elevenlabs_voice_id="voice-main")
ENGLISH = SpeechPlan("en-US", ("en",), ("BrightBooks", "Xero"))
FRENCH = SpeechPlan("fr-FR", ("fr",), ("BrightBooks",))
TRILINGUAL = SpeechPlan("en-US", ("en", "ar", "fr"), ("BrightBooks",))


def test_plan_comes_from_the_playbook(sample_playbook):
    assert SpeechPlan.from_playbook(sample_playbook) == SpeechPlan("en-US", ("en",), ("BrightBooks", "Xero", "QuickBooks"))
    sample_playbook.additional_languages = ["ar-SA", "fr", "en"]
    plan = SpeechPlan.from_playbook(sample_playbook)
    assert plan.languages == ("en", "ar", "fr") and plan.follows_prospect and plan.primary == "en"


def test_deepgram_single_language_keeps_keyterms_for_english_only():
    s = Settings(**BASE)
    en = get_stt(s).stt_settings(s, ENGLISH)
    assert en["model"] == "nova-3-general" and en["language"] == Language.EN_US and en["keyterm"] == ["BrightBooks", "Xero"]
    fr = get_stt(s).stt_settings(s, FRENCH)
    assert fr["language"] == Language.FR_FR and "keyterm" not in fr


def test_deepgram_multilingual_mode_hints_the_campaign_languages():
    s = Settings(**BASE, deepgram_multilingual_languages="en,fr,ar")
    cfg = get_stt(s).stt_settings(s, TRILINGUAL)
    assert cfg["model"] == "nova-3" and cfg["language"] == "multi" and cfg["language_hint"] == ["en", "ar", "fr"]
    assert "keyterm" not in cfg and cfg["interim_results"] is True
    assert type(get_stt(s).build(s, TRILINGUAL)).__name__ == "DeepgramSTTService"


def test_a_language_gap_is_reported_before_dialing_not_discovered_on_a_call():
    s = Settings(**BASE)  # Arabic not in the confirmed multilingual list by default
    problems = speech_plan_problems(s, TRILINGUAL)
    assert len(problems) == 1 and "Arabic" in problems[0] and "deepgram" in problems[0]
    assert speech_plan_problems(s, ENGLISH) == []
    # A single-language Arabic campaign does not use the multilingual model, so no gap.
    assert speech_plan_problems(s, SpeechPlan("ar-SA", ("ar",))) == []
    # Soniox identifies the language per token, so the trilingual plan is fine there.
    soniox = Settings(**{**BASE, "stt_provider": "soniox", "soniox_api_key": "k"})
    assert speech_plan_problems(soniox, TRILINGUAL) == []


def test_soniox_restricts_recognition_to_approved_languages():
    s = Settings(**{**BASE, "stt_provider": "SONIOX", "soniox_api_key": "k"})
    cfg = get_stt(s).stt_settings(s, TRILINGUAL)
    assert cfg["language_hints"] == [Language.EN, Language.AR, Language.FR]
    assert cfg["language_hints_strict"] is True and cfg["enable_language_identification"] is True
    assert cfg["context"] == "BrightBooks"
    assert get_stt(s).stt_settings(s, FRENCH)["enable_language_identification"] is False
    assert type(get_stt(s).build(s, TRILINGUAL)).__name__ == "SonioxSTTService"


def test_voice_pins_the_language_only_on_single_language_calls():
    s = Settings(**BASE)
    tts = get_tts(s)
    assert tts.tts_settings(s, FRENCH)["language"] == Language.FR_FR
    # Following the prospect: language left open so a switch needs no reconnect.
    assert "language" not in tts.tts_settings(s, TRILINGUAL)
    assert tts.on_language_switch(s, TRILINGUAL, "ar") is None
    assert type(tts.build(s, TRILINGUAL)).__name__ == "ElevenLabsTTSService"


def test_per_language_voice_is_applied_on_switch_when_configured():
    s = Settings(**BASE, elevenlabs_voice_id_ar="voice-ar")
    tts = get_tts(s)
    assert tts.tts_settings(s, SpeechPlan("ar", ("ar", "en")))["voice"] == "voice-ar"
    frame = tts.on_language_switch(s, TRILINGUAL, "ar")
    assert isinstance(frame, TTSUpdateSettingsFrame) and frame.delta.voice == "voice-ar"
    assert tts.on_language_switch(s, TRILINGUAL, "fr").delta.voice == "voice-main"  # falls back to the main voice


def test_english_only_voice_model_is_a_reported_gap():
    s = Settings(**BASE, elevenlabs_model="eleven_turbo_v2", deepgram_multilingual_languages="en,ar,fr")
    problems = speech_plan_problems(s, TRILINGUAL)
    assert any("Arabic" in p and "eleven_turbo_v2" in p for p in problems) and any("French" in p for p in problems)


def test_required_settings_follow_the_chosen_provider():
    s = Settings(anthropic_api_key="x", stt_provider="soniox")
    missing = s.missing_for_calls()
    assert "SONIOX_API_KEY" in missing and "DEEPGRAM_API_KEY" not in missing and "ELEVENLABS_API_KEY" in missing
    assert "DEEPGRAM_API_KEY" in Settings(anthropic_api_key="x").missing_for_calls()


def test_unknown_provider_fails_loudly():
    with pytest.raises(RuntimeError, match="STT_PROVIDER 'whisperx'"):
        get_stt(Settings(anthropic_api_key="x", stt_provider="whisperx"))
