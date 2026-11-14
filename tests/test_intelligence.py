"""Lead scoring from evidence, the campaign report, and the coach."""

import asyncio

import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient

from callagent import db
from callagent.dialer import lifecycle
from callagent.config import Settings, get_settings, resolve_api_token
from callagent.intelligence import coach
from callagent.intelligence.report import campaign_report
from callagent.intelligence.scoring import apply_evidence, merge_evidence, score
from callagent.playbook.schema import Objection, Playbook
from callagent.postcall import summarizer
from callagent.postcall.summarizer import CallReview, ObjectionEvent, QualificationEvidence
from callagent.training import versions


def test_score_is_a_weighted_sum_of_what_the_prospect_actually_said():
    strong = score({"fits_target_customer": "yes", "has_need": "yes", "decision_maker": "yes", "timeline": "now", "budget_fit": "yes", "expressed_interest": "yes"})
    assert strong["score"] == 100 and strong["category"] == "hot" and strong["known"] == 6
    partial = score({"fits_target_customer": "yes", "has_need": "yes", "timeline": "this_quarter"})
    assert partial["points"] == {"fits_target_customer": 20, "has_need": 25, "decision_maker": 0, "timeline": 10.5, "budget_fit": 0, "expressed_interest": 0}
    assert partial["score"] == 56 and partial["category"] == "warm"  # 55.5 of 100
    assert score({"expressed_interest": "yes"})["category"] == "cold"  # interest alone is 10 points


def test_no_evidence_means_no_score_not_a_guess():
    empty = score({})
    assert empty["score"] is None and empty["category"] == "unscored" and empty["known"] == 0
    assert score({"has_need": "unknown", "timeline": "unknown"})["score"] is None


def test_a_prospect_who_is_not_a_fit_is_cold_whatever_else_they_said():
    result = score({"fits_target_customer": "no", "has_need": "yes", "decision_maker": "yes", "timeline": "now", "budget_fit": "yes", "expressed_interest": "yes"})
    assert result["score"] == 80 and result["category"] == "cold"
    assert score({"has_need": "no", "decision_maker": "yes", "fits_target_customer": "yes"})["category"] == "cold"


def test_campaign_weights_change_the_score():
    evidence = {"decision_maker": "yes", "has_need": "yes"}
    assert score(evidence)["score"] == 45
    assert score(evidence, {"decision_maker": 60, "bogus_factor": 500})["score"] == round(85 * 100 / 140)  # unknown factors are ignored


def test_evidence_accumulates_across_calls_and_unknown_never_erases_it():
    first = {"has_need": "yes", "decision_maker": "unknown", "quotes": ["we lose a day a week on this"]}
    second = {"has_need": "unknown", "decision_maker": "no", "timeline": "later", "quotes": ["my partner decides"]}
    merged = merge_evidence(first, second)
    assert merged["has_need"] == "yes" and merged["decision_maker"] == "no" and merged["timeline"] == "later"
    assert merged["quotes"] == ["we lose a day a week on this", "my partner decides"]
    third = merge_evidence(merged, {"decision_maker": "yes"})  # a later definite answer replaces an earlier one
    assert third["decision_maker"] == "yes"

    lead = db.Lead(campaign_id=1, phone_e164="+14155550100", score_details={})
    assert apply_evidence(lead, first)["score"] == 25
    assert apply_evidence(lead, second)["score"] == round(25 + 15 * 0.3)
    assert lead.score == 30 and lead.score_details["category"] == "cold" and lead.score_details["evidence"]["has_need"] == "yes"


def _review(**kw) -> dict:
    base = dict(disposition="not_interested", summary="s", interest_level=3, objections_raised=[], commitments_made_by_agent=[], prospect_facts=[],
                next_action="none", retry_recommended=False, retry_after_hours=0, agent_quality_score=6, coaching_notes=[], playbook_suggestions=[])
    return CallReview(**{**base, **kw}).model_dump()


def _seed(s, campaign, n, *, version, disposition, language="en", review=None, cost=0.5, answered=True, start=0):
    for i in range(n):
        lead = db.Lead(campaign_id=campaign.id, phone_e164=f"+1415{version}{start + i:06d}", status="completed")
        s.add(lead)
        s.flush()
        run = db.CallRun(lead_id=lead.id, campaign_id=campaign.id, status="completed", disposition=disposition, stream_connected=answered,
                         transcript=[{"role": "user", "text": "hi"}] if answered else [], playbook_version=version, language=language,
                         summary={"review": review} if review else {})
        s.add(run)
        s.flush()
        lifecycle.record_usage(s, run, 60, cost)  # what a call cost is what the ledger says it cost


@pytest.fixture
def campaign(tmp_path, sample_playbook):
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'intel.db'}")
    with db.session_scope() as s:
        c = db.Campaign(name="q4", status="running")
        s.add(c)
        s.flush()
        versions.record_active(s, c, sample_playbook.model_copy(update={"open_questions": []}).model_dump(), source="onboard")
        return c.id


def test_report_counts_objections_lost_reasons_and_compares_versions(campaign):
    price = [ObjectionEvent(category="price", quote="that's more than we pay now", handled=False)]
    agency = [ObjectionEvent(category="competitor", quote="we already use LedgerPro", handled=True)]
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        _seed(s, c, 24, version=1, disposition="not_interested",
              review=_review(objection_events=price, lost_reason="price", customer_sentiment="negative", missed_opportunities=["Did not ask about monthly volume"]))
        _seed(s, c, 6, version=1, disposition="meeting_booked", start=100,
              review=_review(disposition="meeting_booked", objection_events=agency, competitors_mentioned=["ledgerpro"], agent_quality_score=9))
        _seed(s, c, 21, version=2, disposition="not_interested", start=200, language="ar", review=_review(lost_reason="timing"))
        _seed(s, c, 9, version=2, disposition="meeting_booked", start=300, language="ar",
              review=_review(disposition="meeting_booked", incorrect_statements=["Said setup is free; the playbook does not say that."]))
        _seed(s, c, 5, version=2, disposition="no_answer", start=400, answered=False, cost=0.01)
        report = campaign_report(s, c)

    assert report["calls"] == {"total": 65, "answered": 60, "reviewed": 60, "conversions": 15, "conversion_rate": 0.25,
                               "dispositions": {"not_interested": 45, "meeting_booked": 15, "no_answer": 5}}
    assert report["cost"]["spend_usd"] == pytest.approx(30.05) and report["cost"]["cost_per_conversion"] == pytest.approx(2.0)

    by_category = {o["category"]: o for o in report["objections"]}
    assert report["objections"][0]["category"] == "price"  # most frequent first
    assert by_category["price"] == {"category": "price", "raised": 24, "handled": 0, "handled_rate": 0.0, "calls": 24,
                                    "conversion_rate_when_raised": 0.0, "examples": ["that's more than we pay now"] * 3}
    assert by_category["competitor"]["handled_rate"] == 1.0 and by_category["competitor"]["conversion_rate_when_raised"] == 1.0
    assert report["lost_reasons"] == {"price": 24, "timing": 21}
    assert report["competitors"] == {"Ledgerpro": 6}
    assert report["missed_opportunities"] == [{"text": "Did not ask about monthly volume", "calls": 24}]
    assert len(report["unsupported_statements"]) == 9 and "setup is free" in report["unsupported_statements"][0]["statement"]

    v1, v2 = report["by_playbook_version"]["1"], report["by_playbook_version"]["2"]
    assert (v1["answered"], v1["conversions"], v1["conversion_rate"], v1["enough_data"]) == (30, 6, 0.2, True)
    assert (v2["calls"], v2["answered"], v2["conversions"], v2["conversion_rate"]) == (35, 30, 9, 0.3)
    assert report["best_version"] == "2"
    assert report["by_language"]["ar"]["answered"] == 30 and report["by_language"]["en"]["conversion_rate"] == 0.2


def test_no_winner_is_named_on_too_little_data(campaign):
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        _seed(s, c, 30, version=1, disposition="not_interested", review=_review())
        _seed(s, c, 4, version=2, disposition="meeting_booked", start=100, review=_review(disposition="meeting_booked"))
        report = campaign_report(s, c)
    assert report["by_playbook_version"]["2"]["conversion_rate"] == 1.0 and report["by_playbook_version"]["2"]["enough_data"] is False
    assert report["best_version"] is None  # 4 calls at 100% is not a result


def test_finishing_a_call_scores_the_lead_from_the_review(campaign, sample_playbook, monkeypatch):
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        c.rules_json = {"scoring_weights": {"has_need": 50}}
        lead = db.Lead(campaign_id=c.id, phone_e164="+14155550100", status="calling", attempts=1)
        s.add(lead)
        s.flush()
        run = db.CallRun(lead_id=lead.id, campaign_id=c.id, status="in-progress", stream_connected=True)
        s.add(run)
        s.flush()
        run_id, lead_id = run.id, lead.id
    review = CallReview(**{**_review(), "evidence": QualificationEvidence(has_need="yes", decision_maker="yes", quotes=["I sign the contracts"])})
    monkeypatch.setattr(summarizer, "make_client", lambda settings=None: object())
    monkeypatch.setattr(summarizer, "review_call", lambda *a, **k: review)
    settings = Settings(anthropic_api_key="x", cost_per_connected_minute_usd=0.10)
    asyncio.run(summarizer.finalize_call(run_id, sample_playbook, [{"role": "user", "text": "I sign the contracts"}], [], "live", None, settings))
    with db.session_scope() as s:
        lead = s.get(db.Lead, lead_id)
        assert lead.score == round((50 + 20) * 100 / 125) and lead.score_details["category"] == "warm"
        assert lead.score_details["evidence"]["quotes"] == ["I sign the contracts"]
        assert s.get(db.CallRun, run_id).cost_usd is not None


class FakeClient:
    """Stands in for the model; `parse_structured` is patched to return its proposal."""


def _coach_with(monkeypatch, make_proposal):
    captured = {}

    def fake_parse(client, *, output_model, messages, system, **kwargs):
        captured["prompt"] = messages[0]["content"]
        captured["system"] = system
        return make_proposal()

    monkeypatch.setattr("callagent.llm.parse_structured", fake_parse)
    return captured


def test_coach_waits_for_enough_calls(campaign):
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        _seed(s, c, 5, version=1, disposition="not_interested", review=_review())
        with pytest.raises(coach.CoachError, match="only 5 reviewed calls"):
            coach.propose_improvements(s, c, client=FakeClient(), settings=Settings(anthropic_api_key="x"))


def test_coach_drafts_improvements_but_cannot_change_prices_or_promises(campaign, sample_playbook, monkeypatch):
    def proposal():
        pb = sample_playbook.model_copy(deep=True, update={"open_questions": []})
        pb.opening_line = "Hi {first_name}, Jordan here, an AI assistant with BrightBooks. Quick question about your month-end close?"
        pb.objections = [*pb.objections, Objection(objection="That's more than we pay now", response="Fair. What does a bookkeeper's day cost you?")]
        # Things a model must not be able to slip through:
        pb.pricing_and_terms = "Now only $9 per client!"
        pb.proof_points = ["10,000 firms", "Guaranteed 50% savings"]
        pb.must_not_say = []
        pb.disclose_ai_upfront = False
        pb.additional_languages = ["de"]
        pb.persona.language = "de-DE"
        return coach.CoachProposal(
            changes=[coach.CoachChange(change="Open with a question about month-end close.", evidence="24 of 60 answered calls raised price before any need was established.")],
            playbook=pb,
        )

    captured = _coach_with(monkeypatch, proposal)
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        price = [ObjectionEvent(category="price", quote="that's more than we pay now", handled=False)]
        _seed(s, c, 24, version=1, disposition="not_interested", review=_review(objection_events=price, lost_reason="price", coaching_notes=["Pitched before asking anything."]))
        result = coach.propose_improvements(s, c, client=FakeClient(), settings=Settings(anthropic_api_key="x"))
        assert result["version"] == 2 and result["reviewed_calls"] == 24 and len(result["changes"]) == 1

        draft = versions.get_version(s, c, 2)
        assert (draft.status, draft.source) == ("draft", "coach")
        assert "24 reviewed calls" in draft.note and "24 of 60 answered calls raised price" in draft.note
        pb = Playbook.model_validate(draft.playbook_json)
        assert pb.opening_line.startswith("Hi {first_name}, Jordan here") and len(pb.objections) == 2  # the coaching is kept
        # ...and everything the business decided is exactly as before.
        assert pb.pricing_and_terms == "Starts at $49 per client per month." and pb.proof_points == ["400+ firms", "4.8 on Capterra"]
        assert pb.must_not_say == ["certified by Xero"] and pb.disclose_ai_upfront is True
        assert pb.additional_languages == [] and pb.persona.language == "en-US"
        # Live calls still run version 1.
        assert versions.active_number(s, c) == 1 and c.playbook_json["opening_line"] == sample_playbook.opening_line

    assert '"category": "price"' in captured["prompt"] and "Pitched before asking anything." in captured["prompt"]
    assert "Do not change prices" in captured["system"]


def test_coach_proposes_nothing_when_the_calls_do_not_justify_it(campaign, sample_playbook, monkeypatch):
    current = sample_playbook.model_copy(update={"open_questions": []})
    _coach_with(monkeypatch, lambda: coach.CoachProposal(changes=[], playbook=current))
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        _seed(s, c, 25, version=1, disposition="meeting_booked", review=_review(disposition="meeting_booked"))
        assert coach.propose_improvements(s, c, client=FakeClient(), settings=Settings(anthropic_api_key="x")) == {"version": None, "changes": [], "reviewed_calls": 25}
        # A "change" that only touched locked fields is not a change either.
        sneaky = current.model_copy(update={"pricing_and_terms": "Free!"})
        _coach_with(monkeypatch, lambda: coach.CoachProposal(changes=[coach.CoachChange(change="Cut the price.", evidence="none")], playbook=sneaky))
        assert coach.propose_improvements(s, c, client=FakeClient(), settings=Settings(anthropic_api_key="x"))["version"] is None
        assert [v.version for v in versions.list_versions(s, c)] == [1]


@pytest.fixture
def client(tmp_path, monkeypatch, sample_playbook):
    monkeypatch.setenv("TWILIO_VALIDATE_SIGNATURE", "false")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'api.db'}")
    from callagent.server import app

    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {resolve_api_token(get_settings())}"
        c.post("/api/campaigns", json={"name": "q4", "playbook": sample_playbook.model_copy(update={"open_questions": []}).model_dump()})
        yield c
    get_settings.cache_clear()


def test_report_leads_and_coach_over_http(client):
    with db.session_scope() as s:
        c = db.get_campaign_by_name(s, "q4")
        hot = db.Lead(campaign_id=c.id, phone_e164="+14155550101", first_name="Maria", status="completed")
        cold = db.Lead(campaign_id=c.id, phone_e164="+14155550102", first_name="Tom", status="completed")
        new = db.Lead(campaign_id=c.id, phone_e164="+14155550103", first_name="Ana")
        s.add_all([hot, cold, new])
        s.flush()
        apply_evidence(hot, {"fits_target_customer": "yes", "has_need": "yes", "decision_maker": "yes", "timeline": "now"})
        apply_evidence(cold, {"has_need": "no"})

    assert client.get("/api/campaigns/q4/report", headers={"Authorization": ""}).status_code == 401
    report = client.get("/api/campaigns/q4/report").json()
    assert report["calls"]["total"] == 0 and report["calls"]["conversion_rate"] is None
    assert report["leads"] == {"total": 3, "hot": 1, "warm": 0, "cold": 1, "unscored": 1}

    leads = client.get("/api/campaigns/q4/leads").json()
    assert [(l["name"], l["score"], l["category"]) for l in leads["leads"]] == [("Maria", 80, "hot"), ("Tom", 0, "cold"), ("Ana", None, "unscored")]
    assert [l["name"] for l in client.get("/api/campaigns/q4/leads", params={"category": "hot"}).json()["leads"]] == ["Maria"]
    assert client.get("/api/campaigns/q4/leads", params={"category": "lukewarm"}).status_code == 400

    too_early = client.post("/api/campaigns/q4/coach")
    assert too_early.status_code == 409 and "only 0 reviewed calls" in too_early.json()["detail"]


def test_timeline_counts_calls_per_day_with_no_gaps_and_nothing_invented(client):
    from datetime import timedelta

    from callagent.db import utcnow

    http, headers = client, None  # the fixture's client already carries the token
    now = utcnow()
    with db.session_scope() as s:
        c = s.query(db.Campaign).first()
        for i, (days_ago, answered, disposition) in enumerate([(0, True, "meeting_booked"), (0, True, "not_interested"), (0, False, "no_answer"),
                                                               (3, True, "sale_closed"), (40, True, "meeting_booked")]):
            lead = db.Lead(campaign_id=c.id, phone_e164=f"+1415777{i:04d}", status="completed")
            s.add(lead)
            s.flush()
            s.add(db.CallRun(lead_id=lead.id, campaign_id=c.id, status="completed", disposition=disposition, stream_connected=answered,
                             transcript=[{"role": "user", "text": "hi"}] if answered else [], started_at=now - timedelta(days=days_ago, minutes=5)))

    assert http.get("/api/timeline", headers={"Authorization": "Bearer not-the-token"}).status_code == 401
    body = http.get("/api/timeline?days=7", headers=headers).json()
    days = body["days"]
    assert len(days) == 7 and [d["date"] for d in days] == sorted(d["date"] for d in days)  # one row per day, oldest first
    assert sum(d["calls"] for d in days) == 4  # the call from 40 days ago is outside the window
    assert sum(d["answered"] for d in days) == 3 and sum(d["conversions"] for d in days) == 2
    assert sum(1 for d in days if d["calls"] == 0) >= 5  # quiet days are zeros, not missing and not filled in
    assert len(http.get("/api/timeline?days=500", headers=headers).json()["days"]) == 90  # capped


# ----------------------------------------------------------------------------- the lead list pages in the database (audit D-4)


def _many_leads(n: int) -> None:
    with db.session_scope() as s:
        c = db.get_campaign_by_name(s, "q4")
        for i in range(n):
            lead = db.Lead(campaign_id=c.id, phone_e164=f"+1415888{i:04d}", first_name=["Maria", "Tom", "Ana", "Omar"][i % 4], last_name=f"L{i}",
                           company=["Clinic North", "Taylor & Co", ""][i % 3], status=["completed", "pending", "callback"][i % 3])
            s.add(lead)
            s.flush()
            if i % 5 != 0:  # one in five was never reviewed: unscored
                apply_evidence(lead, {"fits_target_customer": "yes" if i % 2 else "no", "has_need": "yes" if i % 3 else "unknown", "decision_maker": "yes" if i % 4 == 1 else "unknown",
                                      "timeline": "now" if i % 7 == 1 else "unknown"})
            for attempt in range(i % 3):  # none, one or two calls, the last one deciding the buying intent
                s.add(db.CallRun(lead_id=lead.id, campaign_id=c.id, status="completed", disposition="not_interested", stream_connected=True,
                                 transcript=[{"role": "user", "text": "x" * 500}], summary={"review": _review(buying_intent_score=(i * 13 + attempt * 31) % 101)}))


def test_every_filter_of_the_lead_list_gives_what_filtering_everything_by_hand_gives(client):
    from callagent.intelligence.api import _lead_row

    _many_leads(60)
    with db.session_scope() as s:  # the reference: every lead with all its calls, filtered here the plain way
        c = db.get_campaign_by_name(s, "q4")
        leads = s.execute(select(db.Lead).where(db.Lead.campaign_id == c.id).order_by(db.Lead.score.desc().nulls_last(), db.Lead.id)).scalars().all()
        everything = [_lead_row(lead, list(s.execute(select(db.CallRun).where(db.CallRun.lead_id == lead.id).order_by(db.CallRun.id)).scalars())) for lead in leads]

    def by_hand(category=None, status=None, intent=None, min_score=None, max_score=None, q=None):
        rows = []
        for row in everything:
            if category and row["category"] != category or status and row["status"] != status or intent and row["buying_intent"] != intent:
                continue
            if min_score is not None and (row["score"] is None or row["score"] < min_score) or max_score is not None and (row["score"] is None or row["score"] > max_score):
                continue
            if q and q.strip().lower() not in f"{row['name']} {row['company']} {row['phone']}".lower():
                continue
            rows.append(row)
        return rows

    cases = [{}, {"status": "callback"}, {"category": "hot"}, {"category": "cold"}, {"category": "unscored"}, {"intent": "high"}, {"intent": "low"}, {"min_score": 40}, {"max_score": 39},
             {"min_score": 20, "max_score": 60}, {"q": "maria"}, {"q": "Taylor &"}, {"q": "8880012"}, {"q": " OMAR l1"}, {"status": "completed", "intent": "medium"},
             {"category": "warm", "q": "clinic", "min_score": 10}, {"intent": "high", "category": "hot", "status": "pending"}, {"q": "nobody by this name"}]
    for filters in cases:
        expected = by_hand(**filters)
        answer = client.get("/api/campaigns/q4/leads", params={**filters, "limit": 200}).json()
        assert answer["total"] == len(expected), filters
        assert answer["leads"] == expected, filters  # the same leads, in the same order, with the same details
        page = client.get("/api/campaigns/q4/leads", params={**filters, "limit": 7, "offset": 5}).json()
        assert (page["total"], page["leads"]) == (len(expected), expected[5:12]), filters


def test_a_page_of_leads_costs_the_same_number_of_queries_however_long_the_list(client):
    from sqlalchemy import event

    def queries_for(params) -> int:
        seen = []

        def count(_conn, _cursor, statement, *_rest):
            seen.append(statement)

        engine = db.get_engine()
        event.listen(engine, "before_cursor_execute", count)
        try:
            assert client.get("/api/campaigns/q4/leads", params=params).status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", count)
        return len(seen)

    _many_leads(20)
    small = {name: queries_for(params) for name, params in (("page", {"limit": 10}), ("search", {"q": "maria", "limit": 10}), ("intent", {"intent": "high", "limit": 10}))}
    with db.session_scope() as s:  # ten times the list
        c = db.get_campaign_by_name(s, "q4")
        s.add_all([db.Lead(campaign_id=c.id, phone_e164=f"+1415999{i:04d}", first_name="Extra", status="pending") for i in range(200)])
    large = {name: queries_for(params) for name, params in (("page", {"limit": 10}), ("search", {"q": "maria", "limit": 10}), ("intent", {"intent": "high", "limit": 10}))}
    assert large == small and max(small.values()) <= 6, (small, large)
