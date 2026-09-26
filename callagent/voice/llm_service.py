"""Pipecat Anthropic service tuned for the phone: no thinking pause, optional fast mode."""

from __future__ import annotations

import time
from typing import Any

from loguru import logger
from pipecat.services.anthropic.llm import AnthropicLLMService

from ..config import Settings
from ..llm import live_call_request_options


class LowLatencyAnthropicLLMService(AnthropicLLMService):
    """AnthropicLLMService with the live-call request options applied.

    Pipecat overwrites ``betas`` right before sending, so fast mode (a beta) has to be
    merged in at the request hook rather than through ``Settings.extra``.
    """

    def __init__(self, *, api_key: str, system_prompt: str, settings: Settings, **kwargs):
        opts = live_call_request_options(settings)
        self._extra_betas: list[str] = list(opts.pop("betas", []))
        self._speed: str | None = opts.pop("speed", None)
        thinking = opts.pop("thinking")
        super().__init__(
            api_key=api_key,
            settings=AnthropicLLMService.Settings(
                model=opts["model"],
                system_instruction=system_prompt,
                max_tokens=opts["max_tokens"],
                enable_prompt_caching=True,
                thinking=thinking,
                extra={"output_config": opts["output_config"]},
            ),
            **kwargs,
        )
        self._turn_started_at: float | None = None

    async def _create_message_stream(self, api_call, params: dict[str, Any]):
        if self._extra_betas:
            params["betas"] = list(dict.fromkeys([*params.get("betas", []), *self._extra_betas]))
        if self._speed:
            params["speed"] = self._speed
        self._turn_started_at = time.monotonic()
        return await super()._create_message_stream(api_call, params)

    async def _push_llm_text(self, text: str):
        if self._turn_started_at is not None:
            logger.info(f"{self}: first token after {time.monotonic() - self._turn_started_at:.2f}s")
            self._turn_started_at = None
        await super()._push_llm_text(text)
