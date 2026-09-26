"""Turn a business's raw script into a Playbook, and refine it with the owner's answers."""

from __future__ import annotations

import anthropic

from ..config import Settings, get_settings
from ..llm import make_client, parse_structured
from .schema import Playbook, QAPair

ANALYST_SYSTEM = """You are the onboarding brain of an outbound AI calling agent.

A business hands you the raw material they have: a sales script, a pitch deck dump,
notes, FAQs, pricing, whatever. Your job is to *learn the pitch* the way a great new
sales hire would in their first week, and to produce a complete Playbook the live
voice agent will run on real phone calls.

How to work:
- Extract, don't invent. Every value prop, proof point and price must be supported by
  the material. If the script implies something but does not say it, put it in
  open_questions instead of guessing.
- Translate script prose into conversational guidance. Phone calls are short turns:
  the talk tracks you write should sound like a relaxed human, never like reading.
- Think about what will actually happen on the call: gatekeepers, voicemail, "how did
  you get my number", "send me an email", "not interested", "how much". Cover the
  objections the material addresses and add the universal ones with sensible responses
  that stay inside what the material allows.
- Decide which capabilities the agent needs (book_meeting, schedule_callback,
  send_followup_sms, transfer_to_human, mark_do_not_call, ...). Mark as required only
  what the call goal truly depends on.
- Ask the business what is missing. Good open questions are specific and answerable in
  one sentence. Mark blocking=true only when dialing without the answer would embarrass
  the business or break the call goal (e.g. no calendar availability for a
  book_meeting goal, unknown price when the script says to quote it, unknown callback
  number for voicemails). Everything else gets a suggested_default.
- The persona must fit the brand and the audience. Pick a plausible first name unless
  one is given. Language follows the script's language unless told otherwise.
- keyterms: brand, product, people and jargon words, 5-25 items, no common words.
- readiness_score reflects how confidently the agent could run calls right now.

Once the business has answered questions (they arrive as Q&A pairs), fold the answers
into the Playbook and drop those questions from open_questions. Keep asking only about
what is still genuinely unknown; do not re-ask answered questions or invent new
trivial ones to look thorough."""


class PlaybookAnalyzer:
    def __init__(self, client: anthropic.Anthropic | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.client = client or make_client(self.settings)

    def analyze(self, script_text: str, answers: list[QAPair] | None = None, previous: Playbook | None = None) -> Playbook:
        parts: list[str] = [
            "Here is the business's raw material:\n\n<script>\n" + script_text.strip() + "\n</script>"
        ]
        if previous is not None:
            parts.append(
                "Here is the Playbook you produced last round. Refine it, do not start over:\n\n<previous_playbook>\n"
                + previous.model_dump_json(indent=2)
                + "\n</previous_playbook>"
            )
        if answers:
            qa = "\n".join(f"Q: {a.question}\nA: {a.answer}" for a in answers)
            parts.append("The business answered your questions:\n\n<answers>\n" + qa + "\n</answers>")
        parts.append("Produce the complete Playbook now.")

        return parse_structured(
            self.client,
            model=self.settings.onboarding_model,
            system=ANALYST_SYSTEM,
            messages=[{"role": "user", "content": "\n\n".join(parts)}],
            output_model=Playbook,
            effort=self.settings.onboarding_effort,
            settings=self.settings,
        )
