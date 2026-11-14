"""What the caller hears when the AI model fails to answer a turn.

Without this the line simply goes quiet: the person says "hello?" a few times and
hangs up, and the call looks like any other short call. With it, the first failure is
covered by one short sentence asking them to repeat (most failures are a single
dropped or slow request), and a second failure on the same call ends it politely and
marks the call as failed, which is what the campaign's stop-on-failing-provider rule
counts (dialer.rules.provider_failure).

The sentences are fixed and spoken by the voice directly, so they need no model.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from .session import CallSession

log = logging.getLogger(__name__)

FAILURES_TO_END_CALL = 2

ASK_AGAIN = {
    "en": "Sorry, I missed that. Could you say it again?",
    "fr": "Pardon, je n'ai pas bien entendu. Pouvez-vous répéter ?",
    "ar": "عذراً، لم أسمع جيداً. هل يمكنكم الإعادة؟",
}
GIVE_UP = {
    "en": "I'm sorry, we have a technical problem on our side. A colleague will get back to you. Goodbye.",
    "fr": "Je suis désolé, nous avons un problème technique de notre côté. Un collègue vous recontactera. Au revoir.",
    "ar": "نعتذر، لدينا مشكلة تقنية من جهتنا. سيتواصل معكم أحد زملائنا. مع السلامة.",
}


class ModelFailure:
    """Handles the model service's errors for one call. ``say`` speaks a fixed sentence;
    ``end`` speaks one and then ends the call."""

    def __init__(self, session: CallSession, say: Callable[[str], Awaitable[None]], end: Callable[[str, str], Awaitable[None]]):
        self._session, self._say, self._end = session, say, end

    async def __call__(self, _processor: Any, error: Any) -> None:
        session = self._session
        if session.ended_by_agent or session.failure:
            return  # the call is already ending
        session.model_failures += 1
        language = session.language.current if session.language else "en"
        reason = type(getattr(error, "exception", None)).__name__ if getattr(error, "exception", None) else "error"
        session.log_tool("model_failure", {"count": session.model_failures}, {"ok": False, "kind": reason})
        log.warning("call %s: the AI model failed to answer (%s), failure %s on this call", session.call_run_id, reason, session.model_failures)
        if session.model_failures < FAILURES_TO_END_CALL:
            await self._say(ASK_AGAIN.get(language, ASK_AGAIN["en"]))
            return
        session.failure = f"the AI model failed {session.model_failures} times during the call ({reason})"
        session.end_reason = "model unavailable"
        await self._end(GIVE_UP.get(language, GIVE_UP["en"]), "model unavailable")
