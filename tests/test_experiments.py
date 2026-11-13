"""Controlled A/B tests of playbook versions, lead detail and version comparison."""

import pytest
from fastapi.testclient import TestClient

from callagent import db
from callagent.config import get_settings, resolve_api_token
from callagent.intelligence import experiments
from callagent.intelligence.scoring import apply_evidence
from callagent.playbook.schema import Objection, Playbook
from callagent.postcall.summarizer import CallReview, ObjectionEvent
from callagent.training import versions
from callagent.voice.pipeline import _playbook_for


def _ready(sample_playbook, **changes) -> dict:
    return sample_playbook.model_copy(update={"open_questions": [], **changes}).model_dump()


@pytest.fixture
def campaign(tmp_path, sample_playbook):
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'exp.db'}")
    with db.session_scope() as s:
        c = db.Campaign(name="q4", status="running")
        s.add(c)
        s.flush()
        versions.record_active(s, c, _ready(sample_playbook), source="onboard")
        versions.propose(s, c, _ready(sample_playbook, opening_line="Hi {first_name}, quick question about month-end?"), source="coach")
        return c.id


def _leads(s, cid, n, start=0):
    leads = [db.Lead(campaign_id=cid, phone_e164=f"+1415555{start + i:04d}") for i in range(n)]
    s.add_all(leads)
    s.flush()
    return leads


def _dial(s, c, lead, *, answered=True, disposition="not_interested"):
    experiment_id, arm, version = experiments.assign(s, c, lead)
    run = db.CallRun(lead_id=lead.id, campaign_id=c.id, status="completed", playbook_version=version, experiment_id=experiment_id, experiment_arm=arm,
                     stream_connected=answered, transcript=[{"role": "user", "text": "hi"}] if answered else [], disposition=disposition)
    s.add(run)
    s.flush()
    return run


def test_starting_a_test_is_checked(campaign, sample_playbook):
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        for kwargs, reason in (
            (dict(variant_version=1), "different version"),
            (dict(variant_version=9), "no version 9"),
            (dict(variant_version=2, variant_share=0.95), "between 10% and 90%"),
            (dict(variant_version=2, min_answered_per_arm=5), "at least 30"),
        ):
            with pytest.raises(experiments.ExperimentError, match=reason):
                experiments.start(s, c, **kwargs)
        versions.propose(s, c, sample_playbook.model_dump(), source="training")  # v3 has a blocking question
        with pytest.raises(experiments.ExperimentError, match="blocking open questions"):
            experiments.start(s, c, 3)
        versions.reject(s, c, 3)
        with pytest.raises(experiments.ExperimentError, match="rejected"):
            experiments.start(s, c, 3)

        e = experiments.start(s, c, 2, by="sara@acme.test")
        assert (e.control_version, e.variant_version, e.status, e.name) == (1, 2, "running", "Version 2 vs version 1")
        with pytest.raises(experiments.ExperimentError, match="already has a test running"):
            experiments.start(s, c, 2)
        # Starting a test publishes nothing: version 2 is still a draft and version 1 is live.
        assert versions.get_version(s, c, 2).status == "draft" and versions.active_number(s, c) == 1


def test_leads_are_split_at_random_and_each_lead_keeps_its_side(campaign):
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        e = experiments.start(s, c, 2, variant_share=0.5)
        leads = _leads(s, c.id, 400)
        arms = [experiments.arm_for(e, lead.id) for lead in leads]
        assert 160 <= arms.count("variant") <= 240  # close to half, not exactly half
        assert arms == [experiments.arm_for(e, lead.id) for lead in leads]  # the same answer every time

        lead = leads[0]
        first = _dial(s, c, lead, answered=False, disposition="no_answer")
        retry = _dial(s, c, lead)
        callback = _dial(s, c, lead)
        assert first.experiment_arm == retry.experiment_arm == callback.experiment_arm
        assert first.playbook_version == retry.playbook_version == callback.playbook_version
        assert first.playbook_version == (2 if first.experiment_arm == "variant" else 1)

        skewed = experiments.Experiment(id=99, variant_share=0.2)
        share = sum(experiments.arm_for(skewed, i) == "variant" for i in range(2000)) / 2000
        assert 0.16 <= share <= 0.24


def test_leads_dialed_before_the_test_do_not_take_part(campaign):
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        veteran, fresh = _leads(s, c.id, 2)
        before = _dial(s, c, veteran, answered=False, disposition="no_answer")
        assert (before.experiment_id, before.playbook_version) == (None, 1)
        e = experiments.start(s, c, 2)
        again = _dial(s, c, veteran)
        assert (again.experiment_id, again.experiment_arm, again.playbook_version) == (None, None, 1)  # stays on the live version
        new = _dial(s, c, fresh)
        assert new.experiment_id == e.id and new.experiment_arm in ("control", "variant")
        # An inbound call from a test lead does not change their side on the next outbound call.
        s.add(db.CallRun(lead_id=fresh.id, campaign_id=c.id, direction="inbound", status="completed"))
        s.flush()
        assert _dial(s, c, fresh).experiment_arm == new.experiment_arm


def test_each_call_runs_the_version_pinned_for_its_side_even_if_live_changes(campaign, sample_playbook):
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        e = experiments.start(s, c, 2)
        leads = _leads(s, c.id, 40)
        by_arm = {arm: next(l for l in leads if experiments.arm_for(e, l.id) == arm) for arm in ("control", "variant")}
        control_run, variant_run = _dial(s, c, by_arm["control"]), _dial(s, c, by_arm["variant"])
        assert _playbook_for(s, c, variant_run).opening_line == "Hi {first_name}, quick question about month-end?"
        assert _playbook_for(s, c, control_run).opening_line == sample_playbook.opening_line
        # Someone publishes a third version mid-test: the test's two sides are unaffected.
        versions.record_active(s, c, _ready(sample_playbook, opening_line="Brand new opener."), source="import")
        assert versions.active_number(s, c) == 3
        assert _dial(s, c, by_arm["control"]).playbook_version == 1
        assert _playbook_for(s, c, control_run).opening_line == sample_playbook.opening_line
        outsider = db.CallRun(lead_id=leads[0].id, campaign_id=c.id, playbook_version=3)
        assert _playbook_for(s, c, outsider).opening_line == "Brand new opener."


def _fill(s, c, e, *, control, variant):
    """Dial fresh leads until each side has the given (answered, converted) counts."""
    want = {"control": list(control), "variant": list(variant)}
    start = 1000
    while any(w[0] > 0 for w in want.values()):
        (lead,) = _leads(s, c.id, 1, start=start)
        start += 1
        arm = experiments.arm_for(e, lead.id)
        if want[arm][0] <= 0:
            continue
        converted = want[arm][1] > 0
        _dial(s, c, lead, disposition="meeting_booked" if converted else "not_interested")
        want[arm][0] -= 1
        want[arm][1] -= converted


def test_no_side_is_called_ahead_before_there_is_enough_evidence(campaign):
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        e = experiments.start(s, c, 2, min_answered_per_arm=40)
        _fill(s, c, e, control=(10, 1), variant=(10, 9))  # looks dramatic, means nothing yet
        r = experiments.results(s, e)
        assert r["verdict"] == "collecting" and r["leader"] is None and r["enough_data"] is False and r["p_value"] is None
        assert r["control"]["answered_needed"] == 30 and r["variant"]["conversion_rate"] == 0.9
        assert "30 more answered leads" in r["summary"]
        with pytest.raises(experiments.ExperimentError, match="do not show the variant ahead"):
            experiments.promote(s, c, e.id)


def test_a_small_gap_with_enough_data_is_reported_as_no_clear_difference(campaign):
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        e = experiments.start(s, c, 2, min_answered_per_arm=40)
        _fill(s, c, e, control=(40, 8), variant=(40, 10))
        r = experiments.results(s, e)
        assert r["enough_data"] is True and r["verdict"] == "no_clear_difference" and r["leader"] is None
        assert r["p_value"] > 0.05 and "within what chance alone produces" in r["summary"]


def test_a_clear_result_names_the_leader_and_promotion_is_a_human_action(campaign):
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        e = experiments.start(s, c, 2, min_answered_per_arm=40)
        _fill(s, c, e, control=(60, 6), variant=(60, 24))
        r = experiments.results(s, e)
        assert (r["verdict"], r["leader"]) == ("variant_ahead", "variant") and r["p_value"] < 0.05
        assert r["control"]["conversion_rate"] == 0.1 and r["variant"]["conversion_rate"] == 0.4
        assert "Version 2 converts 40.0% of answered leads against 10.0% for version 1" in r["summary"]
        assert versions.active_number(s, c) == 1  # a clear result still publishes nothing by itself
        done = experiments.promote(s, c, e.id, by="sara@acme.test")
        assert done.status == "promoted" and done.ended_by == "sara@acme.test"
        assert versions.active_number(s, c) == 2 and versions.get_version(s, c, 2).approved_by == "sara@acme.test"
        assert experiments.running(s, c.id) is None
        with pytest.raises(experiments.ExperimentError, match="already ended"):
            experiments.stop(s, c, e.id)


def test_when_the_live_version_wins_the_variant_is_not_promoted(campaign):
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        e = experiments.start(s, c, 2, min_answered_per_arm=40)
        _fill(s, c, e, control=(60, 24), variant=(60, 6))
        assert experiments.results(s, e)["verdict"] == "control_ahead"
        with pytest.raises(experiments.ExperimentError):
            experiments.promote(s, c, e.id)
        stopped = experiments.stop(s, c, e.id, by="sara")
        assert stopped.status == "stopped" and versions.active_number(s, c) == 1
        (lead,) = _leads(s, c.id, 1, start=9000)
        assert _dial(s, c, lead).experiment_id is None  # after the test, everyone is back on the live version
        experiments.start(s, c, 2)  # and a new test can begin


def test_results_count_leads_not_call_attempts(campaign):
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        e = experiments.start(s, c, 2)
        (lead,) = _leads(s, c.id, 1)
        _dial(s, c, lead, answered=False, disposition="no_answer")
        _dial(s, c, lead)
        _dial(s, c, lead, disposition="meeting_booked")
        side = experiments.results(s, e)[experiments.arm_for(e, lead.id)]
        assert (side["calls"], side["leads_dialed"], side["leads_answered"], side["conversions"], side["conversion_rate"]) == (3, 1, 1, 1, 1.0)


def test_version_comparison_shows_what_changed_line_by_line(sample_playbook):
    base = _ready(sample_playbook)
    other = _ready(sample_playbook, opening_line="New opener.",
                   objections=[*sample_playbook.objections, Objection(objection="Too expensive", response="What does a day cost you?")])
    changes = {c["field"]: c for c in versions.diff(base, other)}
    assert set(changes) == {"opening_line", "objections"}
    assert changes["opening_line"] == {"field": "opening_line", "label": "Opening line", "removed": [sample_playbook.opening_line], "added": ["New opener."]}
    assert changes["objections"]["removed"] == [] and changes["objections"]["added"] == ["objection: Too expensive | response: What does a day cost you?"]
    assert versions.diff(base, base) == []


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
        c.post("/api/campaigns", json={"name": "q4", "playbook": _ready(sample_playbook)})
        with db.session_scope() as s:
            campaign = db.get_campaign_by_name(s, "q4")
            versions.propose(s, campaign, _ready(sample_playbook, opening_line="Hi {first_name}, quick question?"), source="coach",
                             note="Coach proposal from 24 reviewed calls.", changes=[{"change": "Open with a question.", "evidence": "24 of 60 calls raised price first."}])
        yield c
    get_settings.cache_clear()


def test_experiments_over_http(client):
    assert client.get("/api/campaigns/q4/experiments").json() == []
    assert client.post("/api/campaigns/q4/experiments", json={"variant_version": 1}).status_code == 400
    assert client.post("/api/campaigns/q4/experiments", json={"variant_version": 2, "min_answered_per_arm": 5}).status_code == 422
    created = client.post("/api/campaigns/q4/experiments", json={"variant_version": 2, "variant_share": 0.3, "started_by": "sara"})
    assert created.status_code == 201
    body = created.json()
    assert (body["status"], body["verdict"], body["variant_share"], body["created_by"]) == ("running", "collecting", 0.3, "sara")
    assert body["control"]["version"] == 1 and body["variant"]["version"] == 2 and body["variant"]["answered_needed"] == 50
    assert client.post("/api/campaigns/q4/experiments", json={"variant_version": 2}).status_code == 409
    assert client.get(f"/api/campaigns/q4/experiments/{body['id']}").json()["name"] == "Version 2 vs version 1"
    assert client.get("/api/campaigns/q4/experiments/999").status_code == 404
    refused = client.post(f"/api/campaigns/q4/experiments/{body['id']}/promote")
    assert refused.status_code == 400 and "do not show the variant ahead" in refused.json()["detail"]
    assert client.post(f"/api/campaigns/q4/experiments/{body['id']}/stop", json={"by": "sara"}).json()["status"] == "stopped"
    assert [e["status"] for e in client.get("/api/campaigns/q4/experiments").json()] == ["stopped"]
    assert client.get("/api/campaigns/q4/versions").json()[1]["status"] == "active"  # version 1 is still live


def test_coach_recommendations_and_diff_over_http(client):
    listed = client.get("/api/campaigns/q4/versions").json()
    assert listed[0]["source"] == "coach" and listed[0]["changes"] == [{"change": "Open with a question.", "evidence": "24 of 60 calls raised price first."}]
    assert listed[1]["changes"] == []
    d = client.get("/api/campaigns/q4/versions/2/diff").json()
    assert (d["version"], d["compared_with"]) == (2, 1)
    assert [c["field"] for c in d["changes"]] == ["opening_line"] and d["changes"][0]["added"] == ["Hi {first_name}, quick question?"]
    assert client.get("/api/campaigns/q4/versions/1/diff").json()["changes"] == []
    assert client.get("/api/campaigns/q4/versions/8/diff").status_code == 404


def test_lead_intelligence_over_http(client):
    def review(**kw):
        base = dict(disposition="interested_follow_up", summary="Wants a demo next month.", interest_level=7, objections_raised=[], commitments_made_by_agent=[],
                    prospect_facts=[], next_action="Send the pricing sheet and call back on the 3rd.", retry_recommended=False, retry_after_hours=0,
                    agent_quality_score=8, coaching_notes=[], playbook_suggestions=[])
        return CallReview(**{**base, **kw}).model_dump()

    with db.session_scope() as s:
        c = db.get_campaign_by_name(s, "q4")
        maria = db.Lead(campaign_id=c.id, phone_e164="+14155550101", first_name="Maria", last_name="Lopez", company="Lopez Accounting", status="completed")
        tom = db.Lead(campaign_id=c.id, phone_e164="+14155550102", first_name="Tom", status="completed")
        ana = db.Lead(campaign_id=c.id, phone_e164="+14155550103", first_name="Ana")
        s.add_all([maria, tom, ana])
        s.flush()
        apply_evidence(maria, {"fits_target_customer": "yes", "has_need": "yes", "decision_maker": "yes", "timeline": "now", "quotes": ["I sign the contracts"]})
        apply_evidence(tom, {"has_need": "no"})
        price = [ObjectionEvent(category="price", quote="that's more than we pay now", handled=True)]
        s.add(db.CallRun(lead_id=maria.id, campaign_id=c.id, status="completed", disposition="interested_follow_up", stream_connected=True, playbook_version=1,
                         transcript=[{"role": "user", "text": "I sign the contracts"}],
                         summary={"review": review(objection_events=price, buying_intent_score=82, customer_sentiment="positive", pain_points=["Month-end takes a week"])}))
        s.add(db.CallRun(lead_id=tom.id, campaign_id=c.id, status="completed", disposition="not_interested", stream_connected=True,
                         summary={"review": review(disposition="not_interested", lost_reason="no_need", buying_intent_score=10, next_action="None.")}))
        maria_id = maria.id

    leads = client.get("/api/campaigns/q4/leads").json()
    assert leads["total"] == 3 and [l["name"] for l in leads["leads"]] == ["Maria Lopez", "Tom", "Ana"]
    top = leads["leads"][0]
    assert (top["score"], top["category"], top["buying_intent"], top["buying_intent_score"]) == (80, "hot", "high", 82)
    assert top["next_action"] == "Send the pricing sheet and call back on the 3rd." and top["evidence"]["quotes"] == ["I sign the contracts"]
    assert top["points"]["has_need"] == 25 and top["objections"] == [{"category": "price", "raised": 1, "handled": 1, "quotes": ["that's more than we pay now"]}]
    assert leads["leads"][1]["lost_reason"] == "no_need" and leads["leads"][2]["buying_intent"] is None and leads["leads"][2]["calls"] == 0

    names = lambda **params: [l["name"] for l in client.get("/api/campaigns/q4/leads", params=params).json()["leads"]]  # noqa: E731
    assert names(category="hot") == ["Maria Lopez"] and names(intent="low") == ["Tom"] and names(status="pending") == ["Ana"]
    assert names(min_score=50) == ["Maria Lopez"] and names(max_score=10) == ["Tom"] and names(q="lopez acc") == ["Maria Lopez"]
    assert names(limit=1, offset=1) == ["Tom"] and names(category="warm") == []
    assert client.get("/api/campaigns/q4/leads", params={"intent": "maybe"}).status_code == 400

    detail = client.get(f"/api/campaigns/q4/leads/{maria_id}").json()
    assert detail["company"] == "Lopez Accounting" and detail["pain_points"] == ["Month-end takes a week"]
    assert len(detail["call_history"]) == 1 and detail["call_history"][0]["summary"] == "Wants a demo next month."
    assert detail["call_history"][0]["transcript"] == [{"role": "user", "text": "I sign the contracts"}]
    assert client.get("/api/campaigns/q4/leads/9999").status_code == 404
