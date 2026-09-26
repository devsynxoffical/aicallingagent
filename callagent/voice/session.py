"""Per-call state shared between the pipeline, tools and post-call processing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import Settings
from ..playbook.schema import Playbook


@dataclass
class CallSession:
    call_run_id: int
    lead: dict[str, Any]
    campaign_id: int
    playbook: Playbook
    settings: Settings
    twilio_call_sid: str
    mode: str = "live"  # live | voicemail
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

    def transcript_text(self) -> str:
        who = {"user": "Prospect", "assistant": self.playbook.persona.agent_name}
        return "\n".join(f"{who.get(t['role'], t['role'])}: {t['text']}" for t in self.transcript)
