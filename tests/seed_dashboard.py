"""Fixture data for the dashboard's end-to-end tests. Test use only.

This fills an EMPTY database with invented campaigns, leads and calls so that the
dashboard's browser tests have something to read. It is not part of the product: it is
not importable from the ``callagent`` package, no command or API route runs it, and it
refuses to touch a database that already holds a campaign.

    DATABASE_URL=sqlite:///./e2e.db python tests/seed_dashboard.py

Every number the dashboard shows from this data is counted by the engine's own code
(report, scoring, experiments); nothing here writes a result directly.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from callagent import accounts, db  # noqa: E402
from callagent.config import get_settings  # noqa: E402
from callagent.dialer import rules  # noqa: E402
from callagent.intelligence import experiments  # noqa: E402
from callagent.intelligence.scoring import apply_evidence  # noqa: E402
from callagent.playbook.schema import Objection  # noqa: E402
from callagent.postcall.summarizer import CallReview, ObjectionEvent, QualificationEvidence  # noqa: E402
from callagent.training import versions  # noqa: E402
from fixtures import make_sample_playbook  # noqa: E402

TEST_PASSWORD = "e2e-test-password"

# Calls are spread over the eleven days before the seed runs, so "the last two weeks" has something in it.
START = (datetime.now(timezone.utc) - timedelta(days=11)).replace(hour=9, minute=0, second=0, microsecond=0)
FIRST = ["Alex", "Sam", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Avery", "Quinn"]
LAST = ["Abara", "Bianchi", "Costa", "Dubois", "Eze", "Fischer", "Haddad", "Ito", "Jansen", "Kowalski", "Lindqvist", "Moreau"]


def _review(**kw) -> CallReview:
    base = dict(disposition="not_interested", summary="The prospect listened to the pitch and declined.", interest_level=3, objections_raised=[],
                commitments_made_by_agent=[], prospect_facts=[], next_action="No further action.", retry_recommended=False, retry_after_hours=0,
                agent_quality_score=6, coaching_notes=[], playbook_suggestions=[])
    return CallReview(**{**base, **kw})


class _Seeder:
    def __init__(self, s, campaign: db.Campaign, phone_prefix: str):
        self.s, self.campaign, self.prefix, self.n = s, campaign, phone_prefix, 0

    def lead(self, *, first: str | None = None, last: str | None = None, company: str = "", status: str = "completed") -> db.Lead:
        i = self.n
        self.n += 1
        lead = db.Lead(campaign_id=self.campaign.id, phone_e164=f"{self.prefix}{i:04d}", first_name=first or FIRST[i % len(FIRST)],
                       last_name=last or LAST[(i // len(FIRST) + i) % len(LAST)], company=company or f"{LAST[i % len(LAST)]} Partners", status=status)
        self.s.add(lead)
        self.s.flush()
        return lead

    def call(self, lead: db.Lead, *, version: int | None, review: CallReview | None, answered: bool = True, language: str = "en", cost: float = 0.5,
             transcript: list[dict] | None = None, experiment: tuple[int | None, str | None] = (None, None)) -> db.CallRun:
        started = START + timedelta(minutes=85 * lead.id + 240 * lead.attempts)
        lead.attempts += 1
        disposition = review.disposition if review else "no_answer"
        run = db.CallRun(
            lead_id=lead.id, campaign_id=self.campaign.id, status="completed" if answered else "no-answer", disposition=disposition,
            stream_connected=answered, transcript=(transcript or [{"role": "assistant", "text": "Hi, this is Jordan, an AI assistant from BrightBooks."},
                                                                  {"role": "user", "text": "Go ahead."}]) if answered else [],
            playbook_version=version, language=language if answered else None, cost_usd=cost, started_at=started,
            ended_at=started + timedelta(seconds=95 if answered else 20), duration_seconds=95 if answered else 20,
            summary={"review": review.model_dump()} if review else {}, experiment_id=experiment[0], experiment_arm=experiment[1],
        )
        self.s.add(run)
        lead.last_disposition = disposition
        if review:
            apply_evidence(lead, review.evidence.model_dump(), rules.rules_of(self.campaign).scoring_weights)
        self.s.flush()
        return run


def _ready(**changes) -> dict:
    return make_sample_playbook().model_copy(update={"open_questions": [], "readiness_score": 95, **changes}).model_dump()


def _accounting(s) -> None:
    c = db.Campaign(name="Accounting firms Q4", status="paused", rules_json={"budget_usd": 32.0, "max_calls_per_day": 200})
    s.add(c)
    s.flush()
    versions.record_active(s, c, _ready(), source="onboarding", by="owner")
    versions.record_active(s, c, _ready(opening_line="Hi {first_name}, this is Jordan, an AI assistant calling from BrightBooks."), source="manual", by="owner")
    versions.propose(
        s, c,
        _ready(
            opening_line="Hi {first_name}, Jordan here, an AI assistant from BrightBooks. Quick question about your month-end close?",
            objections=[Objection(objection="Send me an email", response="Happy to text you a link instead."),
                        Objection(objection="It costs more than we pay now", response="It starts at $49 per client per month, and firms tell us it gives a bookkeeper a day back each week.")],
        ),
        source="coach", by="coach",
        note="Coach proposal from 64 reviewed calls.",
        changes=[
            {"change": "Opening now leads with a question about month-end close instead of the company name.", "evidence": "24 of 64 answered calls ended before the pitch."},
            {"change": "Added an answer to the price objection using the published starting price.", "evidence": "Price was raised on 25 calls and handled on 1."},
        ],
    )
    versions.propose(s, c, make_sample_playbook().model_dump(), source="training", by="training", note="Built from 2 documents.")

    seed = _Seeder(s, c, "+1415555")
    price = [ObjectionEvent(category="price", quote="That's more than we pay now.", handled=False)]
    rival = [ObjectionEvent(category="competitor", quote="We already use LedgerPro.", handled=True)]
    fit = QualificationEvidence(fits_target_customer="yes", has_need="yes", decision_maker="yes", timeline="this_quarter", expressed_interest="yes")
    for _ in range(24):
        seed.call(seed.lead(), version=1, review=_review(objection_events=price, lost_reason="price", customer_sentiment="negative", buying_intent_score=15,
                                                         missed_opportunities=["Did not ask about monthly client volume"],
                                                         evidence=QualificationEvidence(fits_target_customer="yes", budget_fit="no")))
    for _ in range(6):
        seed.call(seed.lead(), version=1, review=_review(disposition="meeting_booked", summary="Booked a demo with the practice manager.", objection_events=rival,
                                                         competitors_mentioned=["LedgerPro"], agent_quality_score=9, buying_intent_score=80,
                                                         next_action="Send the demo invite.", customer_sentiment="positive", evidence=fit))
    for _ in range(21):
        seed.call(seed.lead(), version=2, language="ar", review=_review(lost_reason="timing", buying_intent_score=30, customer_sentiment="neutral",
                                                                        next_action="Try again after the tax deadline."))
    for i in range(9):
        seed.call(seed.lead(), version=2, language="ar",
                  review=_review(disposition="meeting_booked", summary="Booked a demo.", buying_intent_score=75, customer_sentiment="positive", evidence=fit,
                                 next_action="Send the demo invite.",
                                 incorrect_statements=["Said setup is free; the playbook does not say that."] if i < 2 else []))
    for _ in range(5):
        seed.call(seed.lead(status="pending"), version=2, review=None, answered=False, cost=0.01)

    maria = seed.lead(first="Maria", last="Okafor", company="Okafor & Lane Accounting")
    seed.call(maria, version=2, review=_review(
        disposition="interested_follow_up", summary="Owner of a 12-person firm. Interested, wanted to check with her partner on price.", interest_level=7,
        buying_intent_score=60, customer_sentiment="positive", next_action="Call back Thursday with the per-client price.",
        objection_events=[ObjectionEvent(category="price", quote="What does that come to for sixty clients?", handled=True)],
        pain_points=["Month-end close takes four days", "Two bookkeepers spend Fridays re-categorizing transactions"],
        evidence=QualificationEvidence(fits_target_customer="yes", has_need="yes", decision_maker="yes", quotes=["We lose most of a week on month-end."])),
        transcript=[{"role": "assistant", "text": "Hi, is this Maria? This is Jordan, an AI assistant calling from BrightBooks."},
                    {"role": "user", "text": "Yes, speaking. We lose most of a week on month-end, so go on."},
                    {"role": "assistant", "text": "Firms your size usually get a day back per bookkeeper each week. It starts at $49 per client per month."},
                    {"role": "user", "text": "What does that come to for sixty clients? Let me check with my partner."}])
    seed.call(maria, version=2, review=_review(
        disposition="meeting_booked", summary="Confirmed budget with her partner and booked a demo for Tuesday.", interest_level=9, buying_intent_score=88,
        customer_sentiment="positive", next_action="Send the calendar invite for Tuesday's demo and the pricing sheet.", agent_quality_score=9,
        evidence=QualificationEvidence(timeline="now", budget_fit="yes", expressed_interest="yes", quotes=["My partner is fine with the price, let's see it."])),
        transcript=[{"role": "assistant", "text": "Hi Maria, Jordan from BrightBooks again, the AI assistant you spoke with on Monday."},
                    {"role": "user", "text": "My partner is fine with the price, let's see it."},
                    {"role": "assistant", "text": "Great. I have Tuesday at ten or Wednesday at two."},
                    {"role": "user", "text": "Tuesday at ten."}])

    daniel = seed.lead(first="Daniel", last="Reyes", company="Reyes Tax & Books", status="callback")
    seed.call(daniel, version=2, review=_review(
        disposition="interested_follow_up", summary="Practice manager. Has the problem but is mid-close.", interest_level=6, buying_intent_score=55,
        customer_sentiment="neutral", next_action="Call back after the 10th, once month-end close is done.", retry_recommended=True, retry_after_hours=120,
        objection_events=[ObjectionEvent(category="timing", quote="Not this week, we are closing the month.", handled=True)],
        pain_points=["Categorization errors found late"],
        evidence=QualificationEvidence(fits_target_customer="yes", has_need="yes", timeline="this_quarter", quotes=["We do need to fix this before year-end."])))

    priya = seed.lead(first="Priya", last="Nair", company="Nair Consulting")
    seed.call(priya, version=2, review=_review(
        disposition="not_qualified", summary="Solo consultant with no bookkeeping clients.", interest_level=1, buying_intent_score=5, lost_reason="bad_fit",
        customer_sentiment="neutral", next_action="No further action: not a bookkeeping firm.",
        evidence=QualificationEvidence(fits_target_customer="no", has_need="no", quotes=["I don't do books for anyone, it's just me."])))

    for _ in range(12):
        seed.lead(status="pending")

    verdict = rules.check_limits(s, c, get_settings(), active_calls=0, now=START + timedelta(days=30))
    assert verdict.action == "stop", f"the fixture expects this campaign to be over budget, got {verdict}"
    c.stop_reason = verdict.reason


def _dental(s) -> None:
    c = db.Campaign(name="Dental clinics pilot", status="paused")
    s.add(c)
    s.flush()
    versions.record_active(s, c, _ready(company_name="BrightBooks"), source="onboarding", by="owner")
    versions.propose(s, c, _ready(permission_line="Is now a bad time for a 30-second question?"), source="coach", by="coach",
                     changes=[{"change": "Permission line asks for 30 seconds instead of a minute.", "evidence": "18 of 40 answered calls ended at the permission line."}])
    test = experiments.start(s, c, 2, name="30-second permission line", min_answered_per_arm=30, by="owner")
    seed = _Seeder(s, c, "+1628555")
    answered = {"control": 0, "variant": 0}
    wins = {"control": 4, "variant": 16}
    for _ in range(90):
        lead = seed.lead()
        experiment_id, arm, version = experiments.assign(s, c, lead)
        booked = answered[arm] < wins[arm]
        answered[arm] += 1
        seed.call(lead, version=version, experiment=(experiment_id, arm),
                  review=_review(disposition="meeting_booked", summary="Booked a demo.", buying_intent_score=75) if booked else _review(buying_intent_score=20))
    outcome = experiments.results(s, test)
    assert outcome["verdict"] == "variant_ahead", f"the fixture expects the variant clearly ahead, got {outcome['summary']}"


def _empty(s) -> None:
    c = db.Campaign(name="Law firms (not started)", status="ready")
    s.add(c)
    s.flush()
    versions.record_active(s, c, _ready(), source="onboarding", by="owner")


def main() -> None:
    with db.session_scope() as s:
        if s.execute(select(db.Campaign.id).limit(1)).first() is not None:
            raise SystemExit("Refusing to add test fixtures: this database already has campaigns. Point DATABASE_URL at a new, empty test database.")
        _accounting(s)
        _dental(s)
        _empty(s)
        # One test account per role. The password is a fixture value, used by the browser tests only.
        for role in accounts.ROLES:
            accounts.create_user(s, f"{role}@example.test", TEST_PASSWORD, name=f"Test {role.title()}", role=role)
    print("Seeded 3 test campaigns and 3 test accounts.")


if __name__ == "__main__":
    main()
