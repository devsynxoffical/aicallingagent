"""The seam between the call pipeline and whoever supplies ears and voice.

The pipeline never names a vendor. It asks the registry for the configured adapter,
hands it a :class:`SpeechPlan`, and gets back a Pipecat processor. Adding a vendor is
one adapter class and one line in ``__init__``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pipecat.frames.frames import Frame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transcriptions.language import Language

from ..config import Settings
from ..languages import base_language


@dataclass(frozen=True)
class SpeechPlan:
    """What one call needs from ears and voice."""

    primary_tag: str  # full BCP-47 tag of the opening language, e.g. "en-US"
    languages: tuple[str, ...]  # base codes, opening language first
    keyterms: tuple[str, ...] = ()

    @property
    def primary(self) -> str:
        return self.languages[0]

    @property
    def follows_prospect(self) -> bool:
        """True when the call may change language mid-conversation."""
        return len(self.languages) > 1

    @classmethod
    def from_playbook(cls, playbook) -> "SpeechPlan":
        return cls(
            primary_tag=playbook.persona.language,
            languages=tuple(playbook.languages) or ("en",),
            keyterms=tuple(playbook.keyterms or ()),
        )


def to_pipecat_language(tag: str) -> Language | None:
    """Best-effort map of a BCP-47 tag to Pipecat's Language enum (``ar-SA`` -> ``ar`` if needed)."""
    for candidate in (tag.replace("_", "-"), base_language(tag)):
        try:
            return Language(candidate)
        except ValueError:
            continue
    return None


class STTAdapter(Protocol):
    name: str

    def required_settings(self, settings: Settings) -> dict[str, Any]:
        """Env-var name -> current value. Falsy values are reported as missing."""

    def unsupported_languages(self, settings: Settings, plan: SpeechPlan) -> list[str]:
        """Languages of this plan the provider cannot transcribe in the mode the plan needs."""

    def build(self, settings: Settings, plan: SpeechPlan) -> FrameProcessor: ...


class TTSAdapter(Protocol):
    name: str

    def required_settings(self, settings: Settings) -> dict[str, Any]: ...

    def unsupported_languages(self, settings: Settings, plan: SpeechPlan) -> list[str]: ...

    def build(self, settings: Settings, plan: SpeechPlan, text_filters: list | None = None) -> FrameProcessor: ...

    def on_language_switch(self, settings: Settings, plan: SpeechPlan, language: str) -> Frame | None:
        """A settings frame to push when the prospect changes language, or None when the
        voice follows the text on its own (no reconnect, no added latency)."""
