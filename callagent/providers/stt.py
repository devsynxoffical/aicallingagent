"""Speech-to-text adapters."""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..languages import base_language
from .base import SpeechPlan, to_pipecat_language


class DeepgramSTT:
    """Deepgram streaming. One language: the configured model, biased to the playbook's
    key terms. Several languages: the multilingual model with code-switching, hinted
    toward the campaign's languages."""

    name = "deepgram"

    def required_settings(self, settings: Settings) -> dict[str, Any]:
        return {"DEEPGRAM_API_KEY": settings.deepgram_api_key}

    def unsupported_languages(self, settings: Settings, plan: SpeechPlan) -> list[str]:
        if not plan.follows_prospect:
            return []
        confirmed = {base_language(x) for x in settings.deepgram_multilingual_languages.split(",") if x.strip()}
        return [lang for lang in plan.languages if lang not in confirmed]

    def stt_settings(self, settings: Settings, plan: SpeechPlan) -> dict[str, Any]:
        out: dict[str, Any] = {
            "smart_format": True,
            "punctuate": True,
            "interim_results": True,
            "endpointing": settings.deepgram_endpointing_ms,
        }
        if plan.follows_prospect:
            out["model"] = settings.deepgram_multilingual_model
            out["language"] = "multi"
            out["language_hint"] = list(plan.languages)
            return out
        out["model"] = settings.deepgram_model
        lang = to_pipecat_language(plan.primary_tag)
        if lang is not None:
            out["language"] = lang
        if plan.keyterms and plan.primary == "en":
            # Keyterm prompting is English-only; Deepgram rejects it for other languages.
            out["keyterm"] = list(plan.keyterms[:50])
        return out

    def build(self, settings: Settings, plan: SpeechPlan):
        from pipecat.services.deepgram.stt import DeepgramSTTService

        return DeepgramSTTService(
            api_key=settings.deepgram_api_key or "",
            settings=DeepgramSTTService.Settings(**self.stt_settings(settings, plan)),
        )


class SonioxSTT:
    """Soniox real-time. A single model covers every language and tags each token with
    the language it heard, so one stream serves English, Arabic and French callers."""

    name = "soniox"

    def required_settings(self, settings: Settings) -> dict[str, Any]:
        return {"SONIOX_API_KEY": settings.soniox_api_key}

    def unsupported_languages(self, settings: Settings, plan: SpeechPlan) -> list[str]:
        return [lang for lang in plan.languages if to_pipecat_language(lang) is None]

    def stt_settings(self, settings: Settings, plan: SpeechPlan) -> dict[str, Any]:
        hints = [l for l in (to_pipecat_language(x) for x in plan.languages) if l is not None]
        out: dict[str, Any] = {
            "model": settings.soniox_model,
            "language_hints": hints,
            # Only ever transcribe in a language the campaign approved.
            "language_hints_strict": True,
            "enable_language_identification": plan.follows_prospect,
        }
        if plan.keyterms:
            out["context"] = ", ".join(plan.keyterms[:50])
        return out

    def build(self, settings: Settings, plan: SpeechPlan):
        from pipecat.services.soniox.stt import SonioxSTTService

        return SonioxSTTService(
            api_key=settings.soniox_api_key or "",
            settings=SonioxSTTService.Settings(**self.stt_settings(settings, plan)),
        )
