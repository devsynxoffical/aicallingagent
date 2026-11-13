import pytest

from callagent import db
from callagent.training import versions


@pytest.fixture
def campaign(tmp_path, sample_playbook):
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'v.db'}")
    with db.session_scope() as s:
        c = db.Campaign(name="acme", status="draft")
        s.add(c)
        s.flush()
        cid = c.id
    return cid


def _ready(sample_playbook, **changes) -> dict:
    data = sample_playbook.model_copy(update={"open_questions": [], **changes}).model_dump()
    return data


def test_owner_changes_go_live_at_once_and_are_all_kept(campaign, sample_playbook):
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        v1 = versions.record_active(s, c, _ready(sample_playbook), source="onboard")
        assert (v1.version, v1.status, c.status) == (1, "active", "ready")
        # Saving the same playbook again is not a new version.
        assert versions.record_active(s, c, _ready(sample_playbook), source="onboard").version == 1
        v2 = versions.record_active(s, c, _ready(sample_playbook, pricing_and_terms="Starts at $59."), source="import")
        assert (v2.version, v2.status) == (2, "active")
        assert c.playbook_json["pricing_and_terms"] == "Starts at $59."
        assert [(v.version, v.status) for v in versions.list_versions(s, c)] == [(2, "active"), (1, "archived")]


def test_a_draft_never_touches_live_calls_until_approved(campaign, sample_playbook):
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        versions.record_active(s, c, _ready(sample_playbook), source="onboard")
        draft = versions.propose(s, c, _ready(sample_playbook, pricing_and_terms="Starts at $79."), source="training", note="Trained from pricing.pdf.")
        assert (draft.version, draft.status) == (2, "draft")
        assert c.playbook_json["pricing_and_terms"] == "Starts at $49 per client per month."  # calls still run v1
        approved = versions.approve(s, c, 2, by="sara@acme.test")
        assert approved.status == "active" and approved.approved_by == "sara@acme.test" and approved.activated_at is not None
        assert c.playbook_json["pricing_and_terms"] == "Starts at $79."
        assert versions.get_version(s, c, 1).status == "archived"


def test_an_incomplete_draft_cannot_go_live_by_accident(campaign, sample_playbook):
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        versions.record_active(s, c, _ready(sample_playbook), source="onboard")
        versions.propose(s, c, sample_playbook.model_dump(), source="training")  # has a blocking open question
        with pytest.raises(versions.VersionError, match="Which demo slots are available"):
            versions.approve(s, c, 2)
        assert versions.get_version(s, c, 1).status == "active"
        assert versions.describe(versions.get_version(s, c, 2))["blocking_questions"] == ["Which demo slots are available?"]
        assert versions.approve(s, c, 2, force=True).status == "active"  # an explicit decision


def test_rollback_restores_an_older_version_exactly(campaign, sample_playbook):
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        versions.record_active(s, c, _ready(sample_playbook), source="onboard")
        versions.record_active(s, c, _ready(sample_playbook, pricing_and_terms="Wrong price."), source="import")
        restored = versions.approve(s, c, 1, by="manager")
        assert restored.status == "active" and "Restored on" in restored.note
        assert c.playbook_json["pricing_and_terms"] == "Starts at $49 per client per month."
        assert [(v.version, v.status) for v in versions.list_versions(s, c)] == [(2, "archived"), (1, "active")]


def test_rejected_drafts_stay_in_history_and_cannot_be_activated(campaign, sample_playbook):
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        versions.record_active(s, c, _ready(sample_playbook), source="onboard")
        versions.propose(s, c, _ready(sample_playbook, pricing_and_terms="Free forever."), source="training")
        rejected = versions.reject(s, c, 2, by="manager", reason="We never said free.")
        assert rejected.status == "rejected" and "We never said free." in rejected.note
        with pytest.raises(versions.VersionError, match="rejected"):
            versions.approve(s, c, 2)
        with pytest.raises(versions.VersionError, match="only a draft"):
            versions.reject(s, c, 1)
        with pytest.raises(versions.VersionError, match="no version 9"):
            versions.get_version(s, c, 9)


def test_campaign_from_before_version_history_gets_a_baseline(campaign, sample_playbook):
    with db.session_scope() as s:
        c = s.get(db.Campaign, campaign)
        c.playbook_json = _ready(sample_playbook)  # written the old way, no version row
        c.status = "running"
        s.flush()
        listed = versions.list_versions(s, c)
        assert [(v.version, v.status, v.source) for v in listed] == [(1, "active", "existing")]
        versions.record_active(s, c, _ready(sample_playbook, pricing_and_terms="New."), source="import")
        assert c.status == "running"  # a running campaign is not knocked back to draft/ready by an edit
        assert [(v.version, v.status) for v in versions.list_versions(s, c)] == [(2, "active"), (1, "archived")]
