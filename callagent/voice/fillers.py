"""Natural backchannel while the model is still composing its first word.

Humans say "Mm-hm." or "Right." before a considered answer; dead air is what sounds
robotic. When the model starts a response (the request goes out) we start a timer; if
no model text has arrived by the deadline we speak one short filler, at most once per
prospect turn, and never when the prospect is talking.
"""

from __future__ import annotations

import asyncio
import random

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

NEUTRAL_FILLERS = ("Mm-hm.", "Right.", "Okay.", "Got it.", "Uh-huh.", "Sure.")


class FillerProcessor(FrameProcessor):
    """Place between the LLM and TTS."""

    def __init__(self, *, delay_secs: float = 0.7, fillers: tuple[str, ...] = NEUTRAL_FILLERS, **kwargs):
        super().__init__(**kwargs)
        self._delay = delay_secs
        self._fillers = fillers
        self._timer: asyncio.Task | None = None
        self._spoke_this_turn = False
        self._last_filler: str | None = None
        self.filler_count = 0

    def _cancel_timer(self) -> None:
        if self._timer and not self._timer.done():
            self._timer.cancel()
        self._timer = None

    async def _fire(self) -> None:
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            return
        self._timer = None
        if self._spoke_this_turn:
            return
        self._spoke_this_turn = True
        choices = [f for f in self._fillers if f != self._last_filler] or list(self._fillers)
        filler = random.choice(choices)
        self._last_filler = filler
        self.filler_count += 1
        await self.push_frame(TTSSpeakFrame(filler, append_to_context=False))

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStoppedSpeakingFrame):
            # New prospect turn: a filler is allowed again once the model starts answering.
            self._cancel_timer()
            self._spoke_this_turn = False
        elif isinstance(frame, LLMFullResponseStartFrame):
            # The request is on its way. If the first word is slow, bridge the gap.
            if not self._spoke_this_turn and self._timer is None:
                self._timer = self.create_task(self._fire())
        elif isinstance(frame, (UserStartedSpeakingFrame, InterruptionFrame)):
            self._cancel_timer()
            self._spoke_this_turn = True  # never talk over the prospect
        elif isinstance(frame, LLMTextFrame):
            # The model is already speaking: no filler needed this turn.
            self._cancel_timer()
            self._spoke_this_turn = True
        elif isinstance(frame, (EndFrame, CancelFrame)):
            self._cancel_timer()

        await self.push_frame(frame, direction)
