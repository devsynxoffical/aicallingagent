"""Shared Claude client helpers for the offline (non-realtime) paths.

Live-call inference goes through Pipecat's AnthropicLLMService (see voice/pipeline.py);
everything else - script analysis, rehearsal, post-call review - uses this module.
"""

from __future__ import annotations

from typing import Any, TypeVar

import anthropic
from pydantic import BaseModel

from .config import Settings, get_settings

T = TypeVar("T", bound=BaseModel)

REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def make_client(settings: Settings | None = None) -> anthropic.Anthropic:
    settings = settings or get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _fallback_kwargs(settings: Settings) -> dict[str, Any]:
    """Server-side refusal fallbacks: if a safety classifier declines, the API
    re-runs the request on a fallback model inside the same call."""
    if not settings.enable_refusal_fallbacks:
        return {}
    return {"betas": [REFUSAL_FALLBACK_BETA], "fallbacks": "default"}


def parse_structured(
    client: anthropic.Anthropic,
    *,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    output_model: type[T],
    effort: str = "high",
    max_tokens: int = 16000,
    settings: Settings | None = None,
) -> T:
    """One structured-output call with adaptive thinking. Raises on refusal."""
    settings = settings or get_settings()
    response = client.beta.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        output_format=output_model,
        **_fallback_kwargs(settings),
    )
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise RuntimeError(f"Claude declined the request: {getattr(details, 'explanation', '') or details}")
    if response.parsed_output is None:
        raise RuntimeError(f"No structured output returned (stop_reason={response.stop_reason})")
    return response.parsed_output


def text_completion(
    client: anthropic.Anthropic,
    *,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    effort: str = "medium",
    max_tokens: int = 4000,
    tools: list[dict[str, Any]] | None = None,
    settings: Settings | None = None,
) -> anthropic.types.beta.BetaMessage:
    """Plain (optionally tool-enabled) message call. Returns the full message."""
    settings = settings or get_settings()
    kwargs: dict[str, Any] = {}
    if tools:
        kwargs["tools"] = tools
    response = client.beta.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        **kwargs,
        **_fallback_kwargs(settings),
    )
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise RuntimeError(f"Claude declined the request: {getattr(details, 'explanation', '') or details}")
    return response


def text_of(message: Any) -> str:
    return "".join(block.text for block in message.content if getattr(block, "type", "") == "text").strip()
