"""Collect a readable transcript from the frames flowing through the pipeline."""

from __future__ import annotations

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

from .session import CallSession


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
