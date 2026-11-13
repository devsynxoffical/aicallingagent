"""Provider registry: the one place that knows which vendors exist."""

from __future__ import annotations

from ..config import Settings
from ..languages import language_name
from .base import SpeechPlan, STTAdapter, TTSAdapter
from .stt import DeepgramSTT, SonioxSTT
from .tts import ElevenLabsTTS

STT_PROVIDERS: dict[str, STTAdapter] = {a.name: a for a in (DeepgramSTT(), SonioxSTT())}
TTS_PROVIDERS: dict[str, TTSAdapter] = {a.name: a for a in (ElevenLabsTTS(),)}


def get_stt(settings: Settings) -> STTAdapter:
    try:
        return STT_PROVIDERS[settings.stt_provider]
    except KeyError:
        raise RuntimeError(f"STT_PROVIDER '{settings.stt_provider}' is not one of: {', '.join(STT_PROVIDERS)}") from None


def get_tts(settings: Settings) -> TTSAdapter:
    try:
        return TTS_PROVIDERS[settings.tts_provider]
    except KeyError:
        raise RuntimeError(f"TTS_PROVIDER '{settings.tts_provider}' is not one of: {', '.join(TTS_PROVIDERS)}") from None


def speech_plan_problems(settings: Settings, plan: SpeechPlan) -> list[str]:
    """Why this call could not be heard or spoken correctly with the configured providers.
    Checked before a campaign dials, so a language gap stops the campaign instead of
    producing calls where the agent cannot understand the prospect."""
    problems: list[str] = []
    stt, tts = get_stt(settings), get_tts(settings)
    mode = "while following the prospect's language" if plan.follows_prospect else "on this call"
    for lang in stt.unsupported_languages(settings, plan):
        problems.append(f"{stt.name} speech recognition is not confirmed for {language_name(lang)} {mode}")
    for lang in tts.unsupported_languages(settings, plan):
        problems.append(f"{tts.name} voice model '{settings.elevenlabs_model}' does not speak {language_name(lang)}")
    return problems


__all__ = ["SpeechPlan", "STT_PROVIDERS", "TTS_PROVIDERS", "get_stt", "get_tts", "speech_plan_problems"]
