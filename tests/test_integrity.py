"""Database integrity: migrations, uniqueness, and the rule that a person is never rung twice at once.

Runs on SQLite always. Set TEST_POSTGRES_URL (an empty database this suite may wipe) to
run the same tests on PostgreSQL, where concurrent transactions really overlap.
"""

import asyncio
import os
import threading

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from callagent import db, migrate
from callagent.config import get_settings
from callagent.dialer.campaign_runner import CampaignRunner, LeadNotDialable, claim_lead_for_call
from callagent.server import open_inbound_call
from callagent.training import versions

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
BACKENDS = ["sqlite"] + (["postgres"] if POSTGRES_URL else [])
EXPECTED_INDEXES = {
    "uq_campaigns_inbound_number", "uq_leads_campaign_phone", "ix_leads_due", "uq_call_runs_twilio_call_sid",
    "uq_call_runs_one_active_outbound_per_lead", "ix_call_runs_campaign_status", "ix_call_runs_lead_latest", "uq_appointments_call_start",
    "uq_playbook_versions_campaign_version", "uq_playbook_versions_one_active", "uq_experiments_one_running",
}


def _wipe_postgres() -> None:
    engine = create_engine(POSTGRES_URL, isolation_level="AUTOCOMMIT")
    with engine.connect() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    engine.dispose()


@pytest.fixture(params=BACKENDS)
def database_url(request, tmp_path):
    if request.param == "postgres":
        _wipe_postgres()
        url = POSTGRES_URL
    else:
        url = f"sqlite:///{tmp_path / 'integrity.db'}"
    yield url
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'after.db'}")  # release the connections before the next wipe


@pytest.fixture
def campaign(database_url, sample_playbook):
    db.reset_engine_for_tests(database_url)
    with db.session_scope() as s:
        c = db.Campaign(name="q4", status="running")
        s.add(c)
        s.flush()
        versions.record_active(s, c, sample_playbook.model_copy(update={"open_questions": []}).model_dump(), source="onboard")
        return c.id


def _indexes(engine) -> set[str]:
    inspector = inspect(engine)
    return {i["name"] for table in inspector.get_table_names() for i in inspector.get_indexes(table)}


def _lead(cid: int, phone: str = "+14155550100", **kw) -> int:
    with db.session_scope() as s:
        lead = db.Lead(campaign_id=cid, phone_e164=phone, **kw)
        s.add(lead)
        s.flush()
        return lead.id


# ----------------------------------------------------------------------------- migrations


def test_a_new_database_is_created_at_the_newest_migration(database_url):
    db.reset_engine_for_tests(database_url)
    engine = db.get_engine()
    assert EXPECTED_INDEXES <= _indexes(engine)
    with engine.connect() as c:
        assert migrate.current_revision(c) == migrate.head_revision()
    assert migrate.upgrade(engine) == "current"  # starting again changes nothing


def _make_pre_migration_database(url: str) -> None:
    """A database as an earlier release left it: no migration record, none of the new
    indexes, and a column missing that a later release added."""
    from sqlalchemy.orm import Session

    engine = create_engine(url)
    db.Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(db.Organization(id=1, name="default"))
        s.flush()
        s.add(db.Campaign(id=1, organization_id=1, name="old", status="paused"))
        s.flush()
        s.add_all([db.Lead(id=1, campaign_id=1, phone_e164="+14155550101"), db.Lead(id=2, campaign_id=1, phone_e164="+14155550102")])
        s.commit()
    with engine.begin() as c:
        for name in EXPECTED_INDEXES:
            c.execute(text(f'DROP INDEX "{name}"'))
        c.execute(text("ALTER TABLE call_runs DROP COLUMN experiment_arm"))
    engine.dispose()


def test_a_database_from_before_migrations_is_adopted_with_its_data(database_url):
    _make_pre_migration_database(database_url)
    db.reset_engine_for_tests(database_url)
    engine = db.get_engine()
    assert EXPECTED_INDEXES <= _indexes(engine)
    assert "experiment_arm" in {c["name"] for c in inspect(engine).get_columns("call_runs")}
    with engine.connect() as c:
        assert migrate.current_revision(c) == migrate.head_revision()
        assert c.execute(text("SELECT COUNT(*) FROM leads")).scalar() == 2  # nothing was lost
    with pytest.raises(IntegrityError), db.session_scope() as s:  # and the new rule now holds for old data
        s.add(db.Lead(campaign_id=1, phone_e164="+14155550101"))


def test_a_migration_stops_on_duplicates_and_names_them_instead_of_deleting_anything(database_url):
    _make_pre_migration_database(database_url)
    engine = create_engine(database_url)
    with engine.begin() as c:
        c.execute(text("UPDATE leads SET phone_e164 = '+14155550101'"))  # the same number twice in one campaign
    engine.dispose()

    with pytest.raises(RuntimeError, match=r"same phone number more than once in a campaign.*\+14155550101"):
        db.reset_engine_for_tests(database_url)
    engine = create_engine(database_url)
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM leads")).scalar() == 2
    assert "uq_leads_campaign_phone" not in _indexes(engine)
    with engine.begin() as c:  # the operator resolves it, and the next start succeeds
        c.execute(text("UPDATE leads SET phone_e164 = '+14155550102' WHERE id = 2"))
    engine.dispose()
    db.reset_engine_for_tests(database_url)
    assert "uq_leads_campaign_phone" in _indexes(db.get_engine())


# ----------------------------------------------------------------------------- uniqueness


def test_the_database_refuses_duplicates_the_code_used_to_only_look_for(campaign):
    lead_id = _lead(campaign)
    with db.session_scope() as s:
        s.add(db.CallRun(lead_id=lead_id, campaign_id=campaign, status="completed", twilio_call_sid="CA1"))
    for duplicate in (
        lambda: db.CallRun(lead_id=lead_id, campaign_id=campaign, status="completed", twilio_call_sid="CA1"),  # one provider call, one record
        lambda: db.Lead(campaign_id=campaign, phone_e164="+14155550100"),  # one number, one lead per campaign
        lambda: db.PlaybookVersion(organization_id=1, campaign_id=campaign, version=1, status="draft"),  # version numbers are not reused
        lambda: db.PlaybookVersion(organization_id=1, campaign_id=campaign, version=2, status="active"),  # one live version
    ):
        with pytest.raises(IntegrityError), db.session_scope() as s:
            s.add(duplicate())
    with db.session_scope() as s:
        s.add(db.Experiment(organization_id=1, campaign_id=campaign, control_version=1, variant_version=2))
    with pytest.raises(IntegrityError), db.session_scope() as s:  # one running test per campaign
        s.add(db.Experiment(organization_id=1, campaign_id=campaign, control_version=1, variant_version=3))


def test_finished_calls_do_not_block_the_next_call_to_the_same_lead(campaign):
    lead_id = _lead(campaign)
    with db.session_scope() as s:
        s.add_all([db.CallRun(lead_id=lead_id, campaign_id=campaign, status="completed"), db.CallRun(lead_id=lead_id, campaign_id=campaign, status="no-answer")])
    with db.session_scope() as s:
        s.add(db.CallRun(lead_id=lead_id, campaign_id=campaign, status="ringing"))
    with pytest.raises(IntegrityError), db.session_scope() as s:
        s.add(db.CallRun(lead_id=lead_id, campaign_id=campaign, status="created"))


# ----------------------------------------------------------------------------- one call per person at a time


def _claim(cid: int, lead_id: int, **kw):
    with db.session_scope() as s:
        return claim_lead_for_call(s, cid, lead_id, **kw)


def test_many_simultaneous_dial_attempts_for_one_lead_place_exactly_one_call(campaign):
    lead_id = _lead(campaign)
    attempts = 12
    barrier = threading.Barrier(attempts)
    won, refused, unexpected = [], [], []

    def attempt(manual: bool):
        barrier.wait()
        try:
            won.append(_claim(campaign, lead_id, manual=manual))
        except LeadNotDialable as e:
            refused.append(str(e))
        except Exception as e:  # a lock timeout or anything else would be a bug
            unexpected.append(repr(e))

    threads = [threading.Thread(target=attempt, args=(i % 2 == 0,)) for i in range(attempts)]  # dialer ticks and manual dials mixed
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert unexpected == []
    assert len(won) == 1 and len(refused) == attempts - 1
    with db.session_scope() as s:
        lead = s.get(db.Lead, lead_id)
        assert (lead.status, lead.attempts) == ("calling", 1)  # counted once
        assert s.execute(text("SELECT COUNT(*) FROM call_runs WHERE lead_id = :l"), {"l": lead_id}).scalar() == 1


def test_a_number_suppressed_after_the_lead_was_picked_is_not_called(campaign):
    lead_id = _lead(campaign)
    with db.session_scope() as s:
        s.add(db.DoNotCall(phone_e164="+14155550100", reason="asked on another call"))
    for manual in (False, True):
        with pytest.raises(LeadNotDialable, match="do-not-call"):
            _claim(campaign, lead_id, manual=manual)
    with db.session_scope() as s:
        assert s.get(db.Lead, lead_id).status == "dnc" and s.get(db.Lead, lead_id).attempts == 0
        assert s.execute(text("SELECT COUNT(*) FROM call_runs")).scalar() == 0


def test_someone_on_the_line_with_us_is_not_rung(campaign, sample_playbook):
    settings = get_settings()
    with db.session_scope() as s:
        s.get(db.Campaign, campaign).inbound_number = "+14155550199"
    with db.session_scope() as s:
        run_id, _token = open_inbound_call(s, {"To": "+14155550199", "From": "+14155550100", "CallSid": "CAinbound"}, settings)
        lead_id = s.get(db.CallRun, run_id).lead_id
    for manual in (False, True):
        with pytest.raises(LeadNotDialable):
            _claim(campaign, lead_id, manual=manual)
    with db.session_scope() as s:
        s.get(db.CallRun, run_id).status = "completed"
        s.get(db.Lead, lead_id).status = "completed"
    assert _claim(campaign, lead_id, manual=True)[1] == "+14155550100"  # an operator may call them back afterwards
    with pytest.raises(LeadNotDialable, match="already being called"):
        _claim(campaign, lead_id, manual=True)


def test_the_dialer_only_calls_leads_that_are_due_but_an_operator_may_redial_a_finished_one(campaign):
    lead_id = _lead(campaign, status="completed")
    with pytest.raises(LeadNotDialable, match="not due"):
        _claim(campaign, lead_id)
    with pytest.raises(LeadNotDialable, match="not found"):
        _claim(campaign + 99, lead_id, manual=True)  # a lead cannot be dialed through another campaign
    run_id, phone = _claim(campaign, lead_id, manual=True)
    with db.session_scope() as s:
        run = s.get(db.CallRun, run_id)
        assert (run.status, run.direction, run.playbook_version, phone) == ("created", "outbound", 1, "+14155550100")


def test_the_same_inbound_webhook_delivered_twice_at_once_opens_one_call(campaign):
    settings = get_settings()
    with db.session_scope() as s:
        s.get(db.Campaign, campaign).inbound_number = "+14155550199"
    form = {"To": "+14155550199", "From": "+14155550123", "CallSid": "CAtwice"}
    barrier = threading.Barrier(6)
    results, errors = [], []

    def deliver():
        barrier.wait()
        for _ in range(2):  # the route answers a constraint error by reading the call the other delivery opened
            try:
                with db.session_scope() as s:
                    results.append(open_inbound_call(s, form, settings)[0])
                return
            except IntegrityError:
                continue
            except Exception as e:
                errors.append(repr(e))
                return

    threads = [threading.Thread(target=deliver) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == [] and len(results) == 6 and len(set(results)) == 1
    with db.session_scope() as s:
        assert s.execute(text("SELECT COUNT(*) FROM call_runs")).scalar() == 1
        assert s.execute(text("SELECT COUNT(*) FROM leads")).scalar() == 1


# ----------------------------------------------------------------------------- restart


def test_a_restart_does_not_pause_running_campaigns(campaign):
    async def run():
        runner = CampaignRunner(get_settings())
        await runner.start(campaign)
        assert runner.is_running(campaign)
        await runner.shutdown()  # what the server does when the process stops
        assert not runner.is_running(campaign)

    asyncio.run(run())
    with db.session_scope() as s:
        assert s.get(db.Campaign, campaign).status == "running"  # so it resumes at the next start

    async def pause():
        runner = CampaignRunner(get_settings())
        await runner.start(campaign)
        await runner.stop(campaign)  # an operator's pause still pauses

    asyncio.run(pause())
    with db.session_scope() as s:
        assert s.get(db.Campaign, campaign).status == "paused"
