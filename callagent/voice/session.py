"""Per-call state shared between the pipeline, tools and post-call processing."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ..config import Settings
from ..languages import LanguageTracker
from ..playbook.schema import Playbook


@dataclass
class CallSession:
    call_run_id: int
    lead: dict[str, Any]
    campaign_id: int
    playbook: Playbook
    settings: Settings
    twilio_call_sid: str
    mode: str = "live"  # live | voicemail | inbound
    direction: str = "outbound"  # outbound | inbound
    language: LanguageTracker | None = None
    # Searchable passages of this campaign's training documents (None = nothing uploaded).
    knowledge: Any = None
    transcript: list[dict[str, Any]] = field(default_factory=list)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    disposition: str | None = None
    outcome: dict[str, Any] = field(default_factory=dict)
    ended_by_agent: bool = False
    end_reason: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    idle_nudges: int = 0
    user_has_spoken: bool = False
    latency: dict[str, list[float]] = field(default_factory=dict)
    # What the model used over the whole conversation, as reported by the provider turn by turn.
    ai_usage: dict[str, int] = field(default_factory=lambda: {"requests": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
    # Times the model failed to answer a turn on this call, and what goes on the call record if it ended for that.
    model_failures: int = 0
    failure: str | None = None
    # Called after each line is added, so the transcript can be saved while the call is in progress.
    on_transcript: Callable[[], None] | None = None
    # The tools this call may use (see voice.tools.allowed_tools). None = not restricted (tests, rehearsal).
    allowed_tools: frozenset[str] | None = None
    # How often each limited tool was used on this call.
    tool_uses: dict[str, int] = field(default_factory=dict)
    booked_times: set[str] = field(default_factory=set)

    def count(self, tool: str) -> int:
        return self.tool_uses.get(tool, 0)

    def used(self, tool: str) -> None:
        self.tool_uses[tool] = self.count(tool) + 1

    def link_hosts(self) -> set[str]:
        """Web hosts the approved playbook itself mentions: the only places a text message may link to."""
        text = self.playbook.model_dump_json()
        return {host.lower().removeprefix("www.").rstrip(".,;:)\\\"'") for host in re.findall(r"(?:https?://|www\.)([^\s/?#\"\\]+)", text, re.IGNORECASE)}

    def log_tool(self, name: str, arguments: dict[str, Any], result: dict[str, Any]) -> None:
        self.tool_events.append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "tool": name,
                "arguments": dict(arguments),
                "result": result,
            }
        )

    def add_transcript(self, role: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.transcript.append({"role": role, "text": text, "at": datetime.now(timezone.utc).isoformat()})
        if self.on_transcript is not None:
            self.on_transcript()

    def transcript_text(self) -> str:
        who = {"user": "Prospect", "assistant": self.playbook.persona.agent_name}
        return "\n".join(f"{who.get(t['role'], t['role'])}: {t['text']}" for t in self.transcript)
