"""Call languages: which ones a campaign speaks, and which one the prospect is using right now.

The platform's primary languages are English, Arabic and French. A campaign either
speaks one language (``fixed``) or follows the prospect across several (``auto``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

LANGUAGE_NAMES = {"en": "English", "ar": "Arabic", "fr": "French"}
PRIMARY_LANGUAGES = tuple(LANGUAGE_NAMES)

_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
_LATIN = re.compile(r"[A-Za-zÀ-ɏ]")
_WORD = re.compile(r"[a-zà-ÿœ']+", re.IGNORECASE)

# Function words that almost never appear in the other language. Used only when the
# speech recognizer did not report a language for a Latin-script utterance.
_FRENCH_WORDS = frozenset(
    "je tu il elle nous vous ils elles le la les un une des du de et est sont avec pour pas ne que qui "
    "quoi où oui non merci bonjour bonsoir s'il plaît ça c'est j'ai suis êtes avez combien pourquoi "
    "comment très bien aussi mais donc alors parce votre notre mon ma mes ce cette au aux sur dans "
    "peux pouvez voulez veux fait faire quel quelle d'accord".split()
)
_ENGLISH_WORDS = frozenset(
    "i you he she we they the a an and is are with for not that who what where yes no thanks thank hello "
    "hi please it's i'm i've have has how much why very well also but so because your our my this these "
    "on in can could would want do does did which okay right just about already".split()
)


def base_language(tag: str | None) -> str:
    """``ar-SA`` -> ``ar``; empty or unknown input -> ``""``."""
    return (tag or "").strip().replace("_", "-").split("-")[0].lower()


def language_name(tag: str) -> str:
    return LANGUAGE_NAMES.get(base_language(tag), tag)


def script_language(text: str) -> str | None:
    """``"ar"`` when the text is mostly Arabic script. Script is unambiguous, so this
    outranks whatever language tag the recognizer attached."""
    arabic = len(_ARABIC.findall(text))
    latin = len(_LATIN.findall(text))
    if arabic >= 2 and arabic > latin:
        return "ar"
    return None


def guess_latin_language(text: str) -> str | None:
    """English or French from function words. ``None`` unless the evidence is clear."""
    words = [w.lower() for w in _WORD.findall(text)]
    if len(words) < 3:
        return None
    fr = sum(w in _FRENCH_WORDS for w in words)
    en = sum(w in _ENGLISH_WORDS for w in words)
    if fr >= 2 and fr >= en * 2:
        return "fr"
    if en >= 2 and en >= fr * 2:
        return "en"
    return None


@dataclass
class LanguageSwitch:
    from_language: str
    to_language: str
    turn: int


@dataclass
class LanguageTracker:
    """Follows the prospect's language over a call.

    Feed it each final utterance with the recognizer's language tag (if any). It only
    ever reports a language the campaign allows, so a mis-detected tag can never push
    the agent into a language the business did not approve.
    """

    allowed: tuple[str, ...]
    current: str = ""
    turns: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    switches: list[LanguageSwitch] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.allowed = tuple(dict.fromkeys(base_language(a) for a in self.allowed if base_language(a)))
        if not self.allowed:
            raise ValueError("a call needs at least one language")
        self.current = base_language(self.current) or self.allowed[0]
        if self.current not in self.allowed:
            self.current = self.allowed[0]

    def detect(self, text: str, stt_language: str | None = None) -> str | None:
        """The language of one utterance, or ``None`` when there is no clear signal."""
        candidate = script_language(text)
        if candidate is None:
            tagged = base_language(stt_language)
            if tagged == "ar":
                # The recognizer said Arabic but wrote Latin letters: a name, a brand, noise. No signal.
                tagged = ""
            candidate = tagged or guess_latin_language(text)
        return candidate if candidate in self.allowed else None

    def observe(self, text: str, stt_language: str | None = None) -> LanguageSwitch | None:
        """Record one final utterance. Returns the switch when the prospect changed language."""
        if not text.strip():
            return None
        self.turns += 1
        detected = self.detect(text, stt_language)
        if detected is None:
            self.counts[self.current] = self.counts.get(self.current, 0) + 1
            return None
        self.counts[detected] = self.counts.get(detected, 0) + 1
        if detected == self.current:
            return None
        switch = LanguageSwitch(from_language=self.current, to_language=detected, turn=self.turns)
        self.current = detected
        self.switches.append(switch)
        return switch

    def report(self) -> dict:
        """What ends up in the call record."""
        dominant = max(self.counts, key=self.counts.get) if self.counts else self.current
        return {
            "detected": dominant,
            "final": self.current,
            "turns_by_language": dict(self.counts),
            "switches": [{"from": s.from_language, "to": s.to_language, "turn": s.turn} for s in self.switches],
        }
