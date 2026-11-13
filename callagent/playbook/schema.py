"""The Playbook: everything the agent learned about the business pitch.

Produced by Claude from the business's raw script via structured outputs, refined
through a Q&A loop until nothing blocking is left open, then compiled into the
live-call system prompt by ``prompt_builder``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..languages import base_language

CallGoal = Literal["book_meeting", "close_sale", "qualify_lead", "collect_info", "reactivate", "other"]
CapabilityKey = Literal[
    "book_meeting",
    "schedule_callback",
    "send_followup_sms",
    "transfer_to_human",
    "mark_do_not_call",
    "collect_payment",
    "send_email",
    "custom",
]


class Persona(BaseModel):
    agent_name: str = Field(description="First name the agent introduces itself with.")
    role_title: str = Field(description="How the agent describes its role, e.g. 'from the growth team at Acme'.")
    tone: str = Field(description="Two to five adjectives, e.g. 'warm, upbeat, relaxed, direct'.")
    speaking_style_notes: str = Field(description="Concrete notes on phrasing, pacing, humour, formality.")
    language: str = Field(description="BCP-47 language tag for the call, e.g. 'en-US', 'de-DE'.")


class CallStage(BaseModel):
    name: str
    goal: str = Field(description="What must be true for this stage to be done.")
    talk_track: str = Field(description="Guidance in the agent's own words, not a verbatim script.")
    success_signal: str = Field(description="How the agent knows to move to the next stage.")


class QualificationQuestion(BaseModel):
    question: str
    purpose: str
    disqualify_if: str = Field(description="Answer pattern that means the lead is not a fit. Empty if none.")


class Objection(BaseModel):
    objection: str
    response: str = Field(description="Short, conversational rebuttal. One or two sentences.")


class RequiredCapability(BaseModel):
    key: CapabilityKey
    description: str = Field(description="What the agent must be able to do and when.")
    required: bool = Field(description="True if calls cannot succeed without it.")


class OpenQuestion(BaseModel):
    question: str = Field(description="A plain question for the business owner.")
    why_it_matters: str
    blocking: bool = Field(description="True if the agent should not start dialing before this is answered.")
    suggested_default: str = Field(description="Reasonable default if the business does not answer. Empty if none.")


class LocalizedPhrasing(BaseModel):
    """How a native speaker of one language would say the key moments of the call.
    Written for that language and market, not translated word for word."""

    language: str = Field(description="BCP-47 tag this phrasing is for, e.g. 'ar', 'fr-FR'.")
    opening_line: str
    permission_line: str
    pitch: str = Field(description="The core pitch in two or three natural spoken sentences.")
    objections: list[Objection] = Field(description="The playbook's objections, answered naturally in this language.")
    booking_phrases: list[str] = Field(description="Natural ways to propose and confirm a meeting time.")
    closing_script: str
    voicemail_script: str = Field(description="Under 25 seconds spoken. Keep the {callback_number} placeholder.")
    style_notes: str = Field(description="Register, formality and dialect guidance for this language and market.")


class Playbook(BaseModel):
    company_name: str
    company_description: str
    offer_summary: str = Field(description="What is being pitched, in two or three sentences.")
    target_customer: str
    value_props: list[str]
    proof_points: list[str] = Field(description="Numbers, names, results the agent may cite. Only what the script supports.")
    call_goal: CallGoal
    call_goal_details: str = Field(description="What a fully successful call ends with, concretely.")
    persona: Persona
    opening_line: str = Field(description="Natural first sentence after the prospect says hello.")
    permission_line: str = Field(description="How the agent asks for 30 seconds of the prospect's time.")
    stages: list[CallStage]
    qualification_questions: list[QualificationQuestion]
    objections: list[Objection]
    pricing_and_terms: str = Field(description="Exactly what may be said about price and terms. Empty if not disclosed on calls.")
    must_say: list[str] = Field(description="Compliance or brand lines that must be said when relevant.")
    must_not_say: list[str] = Field(description="Claims or topics that are off limits.")
    closing_script: str = Field(description="How to lock in the outcome and end warmly.")
    voicemail_script: str = Field(description="Under 25 seconds when spoken. Includes callback number placeholder {callback_number}.")
    callback_policy: str = Field(description="When and how to offer or schedule callbacks.")
    disclose_ai_if_asked: bool = Field(description="Whether to admit being an AI assistant when asked directly. Default true.")
    disclose_ai_upfront: bool = Field(
        default=True,
        description="Say in the introduction that the caller is an AI assistant. Keep true unless counsel confirmed it is not required where you call.",
    )
    additional_languages: list[str] = Field(
        default_factory=list,
        description="Other languages the agent may switch to when the prospect speaks them (BCP-47). Empty = persona.language only.",
    )
    localized: list[LocalizedPhrasing] = Field(
        default_factory=list,
        description="Native phrasing for languages other than persona.language. Filled by `callagent playbook localize`.",
    )
    required_capabilities: list[RequiredCapability]
    keyterms: list[str] = Field(description="Product, brand and jargon words the speech recognizer should be biased toward.")
    open_questions: list[OpenQuestion] = Field(description="Gaps in the script the business must fill. Empty when nothing is missing.")
    readiness_score: int = Field(description="0-100. How ready this playbook is to run real calls.")
    readiness_notes: str

    @property
    def languages(self) -> list[str]:
        """Base codes of every language this playbook speaks, primary first."""
        return list(dict.fromkeys(b for b in map(base_language, [self.persona.language, *self.additional_languages]) if b))

    @property
    def follows_prospect_language(self) -> bool:
        return len(self.languages) > 1

    def localized_for(self, language: str) -> LocalizedPhrasing | None:
        want = base_language(language)
        return next((l for l in self.localized if base_language(l.language) == want), None)

    @property
    def blocking_questions(self) -> list[OpenQuestion]:
        return [q for q in self.open_questions if q.blocking]

    @property
    def is_ready(self) -> bool:
        return not self.blocking_questions


class QAPair(BaseModel):
    question: str
    answer: str
