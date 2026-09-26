"""Last line of defense before text-to-speech.

Whatever the model emits is read aloud. If it ever leaks a tool call as text, an XML
tag, markdown or a JSON blob, the prospect must not hear it. Pipecat applies TTS text
filters to each aggregated sentence before synthesis.
"""

from __future__ import annotations

import re

from pipecat.utils.text.base_text_filter import BaseTextFilter

_TAG_BLOCK = re.compile(r"<\s*(thinking|reasoning|tool_call|function_calls?|invoke|antml:[a-z_]+)[^>]*>.*?(</\s*\1\s*>|$)", re.I | re.S)
_ANY_TAG = re.compile(r"</?\s*[a-zA-Z_:][\w:.-]*(\s[^<>]*)?>")
_MARKDOWN = re.compile(r"[*_`#>|]+")
_BRACKETED = re.compile(r"\[(?:[^\]]{0,60})\]")  # stage directions like [laughs] or [pause]
_JSONISH = re.compile(r"^\s*[\[{].*[\]}]\s*$", re.S)
_TOOL_CALL_TEXT = re.compile(r"^\s*(call|calling|invoke|invoking)\s+[a-z_]+\s*\(.*\)\s*$", re.I | re.S)
_WS = re.compile(r"[ \t]{2,}")


def sanitize_speech(text: str) -> str:
    if not text:
        return text
    cleaned = _TAG_BLOCK.sub(" ", text)
    cleaned = _ANY_TAG.sub(" ", cleaned)
    if _JSONISH.match(cleaned) or _TOOL_CALL_TEXT.match(cleaned):
        return ""
    cleaned = _BRACKETED.sub(" ", cleaned)
    cleaned = _MARKDOWN.sub("", cleaned)
    cleaned = _WS.sub(" ", cleaned)
    return cleaned.strip() if cleaned.strip() else ""


class SpeechSanitizer(BaseTextFilter):
    async def filter(self, text: str) -> str:
        return sanitize_speech(text)
