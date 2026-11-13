"""Keeps track of the language the prospect is speaking, utterance by utterance."""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from ..languages import LanguageSwitch, LanguageTracker

log = logging.getLogger(__name__)


class LanguageFollower(FrameProcessor):
    """Place right after speech-to-text, before the user aggregator consumes the transcript.

    The model follows the prospect's language on its own (it reads their words); this
    processor exists so the rest of the call knows the current language too: fillers,
    the voice, and the call report. It adds no latency: frames pass straight through.
    """

    def __init__(
        self,
        tracker: LanguageTracker,
        on_switch: Callable[[LanguageSwitch], Awaitable[Frame | None]] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._tracker = tracker
        self._on_switch = on_switch

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text:
            switch = self._tracker.observe(frame.text, str(frame.language) if frame.language else None)
            if switch is not None:
                log.info("prospect switched language: %s -> %s", switch.from_language, switch.to_language)
                if self._on_switch is not None:
                    update = await self._on_switch(switch)
                    if update is not None:
                        # Ahead of the transcript, so the voice is ready before the reply is spoken.
                        await self.push_frame(update, direction)
        await self.push_frame(frame, direction)
