"""A playbook from a short form, without the AI model.

``callagent onboard`` reads a free-text script with the model and asks follow-up
questions. This is the other way in, used by the console's "New campaign" form: the
business states the essentials and gets a working, conservative playbook at once. It
says only what the form says: no proof points, prices or promises are invented. The
playbook can then be improved from documents (Knowledge > Build a draft) and from
real calls (the coach), both of which go through approval.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from ..languages import LANGUAGE_NAMES, base_language
from .schema import CallGoal, CallStage, Persona, Playbook, RequiredCapability

# Spoken lines the agent starts from, in each language it can open a call in.
_LINES = {
    "en": {
        "opening": "Hi, is this {first_name}? This is {agent}, calling from {company}.",
        "permission": "Do you have a quick minute?",
        "closing": "Thank you for your time. Have a great day.",
        "voicemail": "Hi, this is {agent} from {company}. Please call us back at {callback_number}. Thank you.",
    },
    "fr": {
        "opening": "Bonjour, est-ce bien {first_name} ? Ici {agent}, de la part de {company}.",
        "permission": "Auriez-vous une petite minute ?",
        "closing": "Merci pour votre temps. Très bonne journée.",
        "voicemail": "Bonjour, ici {agent} de {company}. Vous pouvez nous rappeler au {callback_number}. Merci.",
    },
    "ar": {
        "opening": "مرحباً، هل أتحدث مع {first_name}؟ معك {agent} من {company}.",
        "permission": "هل لديك دقيقة من وقتك؟",
        "closing": "شكراً على وقتك. أتمنى لك يوماً سعيداً.",
        "voicemail": "مرحباً، معك {agent} من {company}. يمكنك معاودة الاتصال بنا على الرقم {callback_number}. شكراً لك.",
    },
}

_GOALS: dict[str, tuple[str, str]] = {
    "book_meeting": ("A meeting or demo booked at a time the prospect chose.", "Offer two specific times and confirm the one they pick."),
    "close_sale": ("The prospect agrees to buy, and knows exactly what happens next.", "Confirm what they are buying, the price as written in this playbook, and the next step."),
    "qualify_lead": ("We know whether this prospect is a fit and what they need.", "Summarise what you learned and say what happens next."),
    "collect_info": ("The information we called for is confirmed by the prospect.", "Read back what you noted and thank them."),
    "reactivate": ("A former customer agrees to a next step.", "Agree one concrete next step and when it happens."),
    "other": ("The prospect agrees to a clear next step.", "Agree one concrete next step and when it happens."),
}


class CampaignBrief(BaseModel):
    """What the "New campaign" form asks for."""

    name: str = Field(min_length=1, max_length=120)
    company_name: str = Field(min_length=1, max_length=160)
    company_description: str = Field(default="", max_length=1000)
    offer_summary: str = Field(min_length=1, max_length=1500)
    target_customer: str = Field(default="", max_length=500)
    call_goal: CallGoal = "book_meeting"
    agent_name: str = Field(default="Alex", min_length=1, max_length=60)
    language: str = "en"
    additional_languages: list[str] = Field(default_factory=list, max_length=4)
    value_props: list[str] = Field(default_factory=list, max_length=8)
    pricing_and_terms: str = Field(default="", max_length=1500)
    script_text: str = Field(default="", max_length=20000)

    @field_validator("name", "company_name", "offer_summary", "agent_name")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("this cannot be empty")
        return value

    @field_validator("language")
    @classmethod
    def _primary(cls, value: str) -> str:
        code = base_language(value)
        if code not in LANGUAGE_NAMES:
            raise ValueError(f"unsupported language. Supported: {', '.join(LANGUAGE_NAMES)}")
        return code

    @field_validator("additional_languages")
    @classmethod
    def _additional(cls, values: list[str]) -> list[str]:
        codes = list(dict.fromkeys(base_language(v) for v in values if base_language(v)))
        unknown = [c for c in codes if c not in LANGUAGE_NAMES]
        if unknown:
            raise ValueError(f"unsupported language(s): {', '.join(unknown)}. Supported: {', '.join(LANGUAGE_NAMES)}")
        return codes


def playbook_from_brief(brief: CampaignBrief) -> Playbook:
    lines = {key: text.replace("{agent}", brief.agent_name).replace("{company}", brief.company_name) for key, text in _LINES[brief.language].items()}
    goal, close = _GOALS[brief.call_goal]
    value_props = [v.strip() for v in brief.value_props if v.strip()]
    capabilities = [
        RequiredCapability(key="schedule_callback", description="Call back at a time the prospect asks for.", required=False),
        RequiredCapability(key="mark_do_not_call", description="Stop calling anyone who asks not to be called.", required=True),
    ]
    if brief.call_goal == "book_meeting":
        capabilities.insert(0, RequiredCapability(key="book_meeting", description="Book the meeting during the call.", required=True))

    # How complete the form was. Not a judgement of quality: the AI model has not read this playbook.
    filled = [brief.company_description, brief.target_customer, brief.pricing_and_terms, brief.script_text, " ".join(value_props)]
    score = 45 + 8 * sum(bool(text.strip()) for text in filled)
    missing = [label for label, text in zip(("what the company does", "who the customers are", "prices and terms", "a script or notes", "reasons to buy"), filled) if not text.strip()]
    notes = "Created from the short form; the AI model has not reviewed it. "
    notes += f"Not filled in: {', '.join(missing)}. " if missing else ""
    notes += "Upload price lists, product sheets or FAQs under Knowledge and build a draft to improve it."

    return Playbook(
        company_name=brief.company_name,
        company_description=brief.company_description.strip(),
        offer_summary=brief.offer_summary,
        target_customer=brief.target_customer.strip(),
        value_props=value_props,
        proof_points=[],  # only what the business states: none were given
        call_goal=brief.call_goal,
        call_goal_details=goal,
        persona=Persona(
            agent_name=brief.agent_name,
            role_title=f"from {brief.company_name}",
            tone="warm, clear, respectful",
            speaking_style_notes="Short sentences. One question at a time. Listen more than you talk, and never talk over the prospect.",
            language=brief.language,
        ),
        opening_line=lines["opening"],
        permission_line=lines["permission"],
        stages=[
            CallStage(name="Open", goal="The prospect knows who is calling and why, and agrees to talk.", talk_track="Say who you are and the reason for the call in one sentence, then ask for a minute.", success_signal="They agree to continue."),
            CallStage(name="Understand", goal="We know the prospect's situation and whether the offer is relevant.", talk_track="Ask one or two open questions about how they handle this today. Do not pitch yet.", success_signal="They describe a need or a problem."),
            CallStage(name="Present", goal="The prospect understands the offer in terms of what they just said.", talk_track="Connect the offer to their situation in two or three sentences. Use only facts from this playbook or the documents.", success_signal="They ask a question or show interest."),
            CallStage(name="Next step", goal=goal, talk_track=close, success_signal="A next step is agreed, or they clearly decline."),
        ],
        qualification_questions=[],
        objections=[],
        pricing_and_terms=brief.pricing_and_terms.strip(),
        must_say=[],
        must_not_say=["Any price, discount, guarantee or result that is not written in this playbook or the uploaded documents."],
        closing_script=lines["closing"],
        voicemail_script=lines["voicemail"],
        callback_policy="If the prospect is busy, offer to call back at a time they choose.",
        disclose_ai_if_asked=True,
        disclose_ai_upfront=True,
        additional_languages=[code for code in brief.additional_languages if code != brief.language],
        required_capabilities=capabilities,
        keyterms=[brief.company_name],
        open_questions=[],
        readiness_score=min(score, 85),
        readiness_notes=notes,
    )
