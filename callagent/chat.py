"""Terminal conversation with the agent using the exact live-call model options.

Lets you judge tone and measure model latency (time to first word) before you spend a
phone minute. Tools are stubbed and shown inline.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import anthropic

from .config import Settings, get_settings
from .llm import live_call_request_options, make_client, text_of
from .playbook.prompt_builder import build_system_prompt
from .playbook.schema import Playbook
from .rehearsal import _tools_as_anthropic
from .voice.tools import build_tools_schema


@dataclass
class TurnStats:
    ttft_secs: float
    total_secs: float
    output_tokens: int
    tools: list[str] = field(default_factory=list)


class ChatSession:
    def __init__(self, playbook: Playbook, settings: Settings | None = None, client: anthropic.Anthropic | None = None, lead: dict[str, Any] | None = None):
        self.settings = settings or get_settings()
        self.client = client or make_client(self.settings)
        self.playbook = playbook
        lead = lead or {"first_name": "Sam", "last_name": "Taylor", "company": "Taylor & Co", "timezone": self.settings.default_timezone}
        self.system = build_system_prompt(
            playbook, lead, callback_number=self.settings.twilio_from_number or "+1 555 010 0100",
            human_transfer_available=bool(self.settings.human_transfer_number), now=datetime.now(timezone.utc),
        )
        self.tools = _tools_as_anthropic(build_tools_schema({c.key for c in playbook.required_capabilities}, bool(self.settings.human_transfer_number)))
        self.messages: list[dict[str, Any]] = []
        self.ended = False
        self.stats: list[TurnStats] = []

    def say(self, user_text: str, on_text: Callable[[str], None] | None = None) -> tuple[str, TurnStats]:
        """Send one prospect utterance; returns the agent's spoken reply and timing."""
        self.messages.append({"role": "user", "content": user_text})
        opts = live_call_request_options(self.settings)
        betas = opts.pop("betas")
        kwargs: dict[str, Any] = dict(opts)
        if betas:
            kwargs["betas"] = betas
        spoken_parts: list[str] = []
        tools_used: list[str] = []
        start = time.monotonic()
        ttft: float | None = None
        out_tokens = 0

        while True:
            with self.client.beta.messages.stream(system=self.system, messages=self.messages, tools=self.tools, **kwargs) as stream:
                for event in stream:
                    if event.type == "content_block_delta" and getattr(event.delta, "type", "") == "text_delta":
                        if ttft is None:
                            ttft = time.monotonic() - start
                        spoken_parts.append(event.delta.text)
                        if on_text:
                            on_text(event.delta.text)
                message = stream.get_final_message()
            out_tokens += message.usage.output_tokens
            self.messages.append({"role": "assistant", "content": message.content})
            tool_uses = [b for b in message.content if b.type == "tool_use"]
            if message.stop_reason != "tool_use" or not tool_uses:
                break
            results = []
            for tu in tool_uses:
                tools_used.append(f"{tu.name}({json.dumps(tu.input)})")
                if tu.name == "end_call":
                    self.ended = True
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps({"ok": True, "simulated": True})})
            self.messages.append({"role": "user", "content": results})
            if self.ended:
                break

        total = time.monotonic() - start
        stats = TurnStats(ttft_secs=round(ttft or total, 3), total_secs=round(total, 3), output_tokens=out_tokens, tools=tools_used)
        self.stats.append(stats)
        return "".join(spoken_parts).strip() or text_of(message), stats
