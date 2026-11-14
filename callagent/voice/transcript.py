"""Collect a readable transcript from the frames flowing through the pipeline."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    TranscriptionFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from ..db import CallRun, run_db
from .session import CallSession

log = logging.getLogger(__name__)


async def save_transcript(call_run_id: int, lines: list[dict[str, Any]]) -> None:
    """Store what has been said so far on the call's record. Never replaces a longer
    transcript with a shorter one, so a late write cannot undo the final one."""

    def _save(s):
        run = s.get(CallRun, call_run_id)
        if run is not None and len(lines) > len(run.transcript or []):
            run.transcript = lines

    await run_db(_save)


class TranscriptSaver:
    """Saves the transcript while the call is in progress.

    Before this, a transcript reached the database only when the call ended: a call in
    progress could not be followed, and a process that stopped mid-call lost what had
    been said. Each new line asks for a save; saves are spaced at least
    ``min_interval`` seconds apart and run off the audio loop, and a failed save is
    logged and never interrupts the call. The save at the end of the call is still done
    by post-call processing.
    """

    def __init__(self, call_run_id: int, session: CallSession, *, min_interval: float = 1.0, save: Callable[[int, list[dict[str, Any]]], Awaitable[None]] = save_transcript):
        self._call_run_id = call_run_id
        self._session = session
        self._min_interval = min_interval
        self._save = save
        self._dirty = False
        self._closed = False
        self._task: asyncio.Task | None = None

    def notify(self) -> None:
        """A line was added. Safe to call from anywhere on the event loop; does nothing outside one."""
        if self._closed:
            return
        self._dirty = True
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.get_running_loop().create_task(self._run(), name=f"transcript-saver-{self._call_run_id}")
            except RuntimeError:  # no running loop (a session used outside a call)
                self._task = None

    async def _run(self) -> None:
        while self._dirty and not self._closed:
            self._dirty = False
            try:
                await self._save(self._call_run_id, [dict(line) for line in self._session.transcript])
            except Exception:
                log.warning("call %s: saving the transcript so far failed", self._call_run_id, exc_info=True)
            await asyncio.sleep(self._min_interval)

    async def close(self) -> None:
        """Stop saving. Waits for a save that is under way, so it cannot land after the
        final one, then saves any line that had not been written yet: if post-call
        processing fails, the record still holds everything that was said."""
        self._closed = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=5)
            except Exception:
                task.cancel()
        if self._dirty:
            self._dirty = False
            try:
                await self._save(self._call_run_id, [dict(line) for line in self._session.transcript])
            except Exception:
                log.warning("call %s: saving the last lines of the transcript failed", self._call_run_id, exc_info=True)


class TranscriptCollector(FrameProcessor):
    """Place after ``transport.output()``: records the bot's spoken text. The prospect's
    words are added by the pipeline from the user aggregator's events (the aggregator
    consumes TranscriptionFrames, so they never reach this processor)."""

    def __init__(self, session: CallSession, **kwargs):
        super().__init__(**kwargs)
        self._session = session
        self._bot_buffer: list[str] = []

    def flush_bot(self) -> None:
        self._flush_bot()

    def _flush_bot(self) -> None:
        if self._bot_buffer:
            text = " ".join(self._bot_buffer)
            self._bot_buffer = []
            self._session.add_transcript("assistant", " ".join(text.split()))

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            self._flush_bot()
            self._session.user_has_spoken = True
            self._session.add_transcript("user", frame.text)
        elif isinstance(frame, TTSTextFrame):
            if frame.text:
                self._bot_buffer.append(frame.text)
        elif isinstance(frame, (BotStoppedSpeakingFrame, InterruptionFrame, EndFrame, CancelFrame)):
            self._flush_bot()

        await self.push_frame(frame, direction)
