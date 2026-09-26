"""Plain constructors shared by tests and smoke scripts."""

from callagent.playbook.schema import (
    CallStage,
    Objection,
    OpenQuestion,
    Persona,
    Playbook,
    QualificationQuestion,
    RequiredCapability,
)


def make_sample_playbook() -> Playbook:
    return Playbook(
        company_name="BrightBooks",
        company_description="Bookkeeping automation for accounting firms.",
        offer_summary="AI categorization on top of Xero/QuickBooks that saves 6-10 hours per client per month.",
        target_customer="Accounting firms with 3-30 staff.",
        value_props=["Get a day back per bookkeeper per week", "Nothing changes for clients"],
        proof_points=["400+ firms", "4.8 on Capterra"],
        call_goal="book_meeting",
        call_goal_details="A 20-minute demo booked with the owner or practice manager.",
        persona=Persona(
            agent_name="Jordan",
            role_title="from the team at BrightBooks",
            tone="warm, upbeat, direct",
            speaking_style_notes="Short sentences.",
            language="en-US",
        ),
        opening_line="Hi, is this {first_name}? This is Jordan from BrightBooks.",
        permission_line="Do you have a quick minute?",
        stages=[CallStage(name="Open", goal="Get permission", talk_track="Be brief", success_signal="They say yes")],
        qualification_questions=[QualificationQuestion(question="How many clients?", purpose="Size", disqualify_if="fewer than 15")],
        objections=[Objection(objection="Send me an email", response="Happy to text you a link instead.")],
        pricing_and_terms="Starts at $49 per client per month.",
        must_say=[],
        must_not_say=["certified by Xero"],
        closing_script="Offer two slots.",
        voicemail_script="Hi, Jordan from BrightBooks, call me back at {callback_number}.",
        callback_policy="Offer a callback if busy.",
        disclose_ai_if_asked=True,
        required_capabilities=[RequiredCapability(key="book_meeting", description="Book demos", required=True)],
        keyterms=["BrightBooks", "Xero", "QuickBooks"],
        open_questions=[
            OpenQuestion(question="Which demo slots are available?", why_it_matters="Booking", blocking=True, suggested_default="")
        ],
        readiness_score=70,
        readiness_notes="Needs calendar availability.",
    )
