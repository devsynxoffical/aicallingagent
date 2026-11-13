"""Text-to-speech adapters."""

from __future__ import annotations

from typing import Any

from pipecat.frames.frames import Frame, TTSUpdateSettingsFrame

from ..config import Settings
from .base import SpeechPlan, to_pipecat_language

# ElevenLabs models that only speak English.
_ENGLISH_ONLY_MODELS = {"eleven_flash_v2", "eleven_turbo_v2", "eleven_monolingual_v1"}


class ElevenLabsTTS:
    """ElevenLabs streaming voice.

    On a single-language call the language is pinned. On a call that follows the
    prospect, the language is left open: the multilingual model reads each sentence in
    the language it is written in, so switching costs no reconnect and no latency. A
    per-language voice (``ELEVENLABS_VOICE_ID_AR`` ...) is applied on switch when set.
    """

    name = "elevenlabs"

    def required_settings(self, settings: Settings) -> dict[str, Any]:
        return {"ELEVENLABS_API_KEY": settings.elevenlabs_api_key, "ELEVENLABS_VOICE_ID": settings.elevenlabs_voice_id}

    def unsupported_languages(self, settings: Settings, plan: SpeechPlan) -> list[str]:
        if settings.elevenlabs_model in _ENGLISH_ONLY_MODELS:
            return [lang for lang in plan.languages if lang != "en"]
        return []

    def voice_for(self, settings: Settings, language: str) -> str | None:
        return settings.elevenlabs_voices.get(language) or settings.elevenlabs_voice_id

    def tts_settings(self, settings: Settings, plan: SpeechPlan) -> dict[str, Any]:
        out: dict[str, Any] = {
            "model": settings.elevenlabs_model,
            "voice": self.voice_for(settings, plan.primary),
            "stability": settings.elevenlabs_stability,
            "similarity_boost": settings.elevenlabs_similarity,
            "style": settings.elevenlabs_style,
            "use_speaker_boost": True,
            "speed": settings.elevenlabs_speed,
        }
        if not plan.follows_prospect:
            lang = to_pipecat_language(plan.primary_tag)
            if lang is not None:
                out["language"] = lang
        return out

    def build(self, settings: Settings, plan: SpeechPlan, text_filters: list | None = None):
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        return ElevenLabsTTSService(
            api_key=settings.elevenlabs_api_key or "",
            settings=ElevenLabsTTSService.Settings(**self.tts_settings(settings, plan)),
            text_filters=text_filters or [],
        )

    def on_language_switch(self, settings: Settings, plan: SpeechPlan, language: str) -> Frame | None:
        if not settings.elevenlabs_voices:
            return None  # one voice for every language: nothing to change
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

        return TTSUpdateSettingsFrame(delta=ElevenLabsTTSService.Settings(voice=self.voice_for(settings, language)))
