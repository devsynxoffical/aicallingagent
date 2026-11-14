"""Database integrity: migrations, uniqueness, and the rule that a person is never rung twice at once.

Runs on SQLite always. Set TEST_POSTGRES_URL (an empty database this suite may wipe) to
run the same tests on PostgreSQL, where concurrent transactions really overlap.
"""

import asyncio
import os
import threading
from datetime import datetime
from pathlib import Path

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
        db.add_dnc(s, "+14155550100", db.default_organization_id(s), "asked on another call")
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


# ----------------------------------------------------------------------------- organization isolation (migration 0007)

LEGACY = Path(__file__).parent / "legacy_schemas"


def _insert(connection, table_name: str, **values):
    """One row in a reflected table, with something valid in every required column the
    caller did not name (the previous release set those in Python, not in the schema)."""
    from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, MetaData, Numeric, Table

    table = Table(table_name, MetaData(), autoload_with=connection)
    row = dict(values)
    for column in table.columns:
        if column.name in row or column.nullable or column.primary_key:
            continue
        kind = column.type
        row[column.name] = (
            datetime(2026, 1, 1) if isinstance(kind, DateTime) else False if isinstance(kind, Boolean) else 0 if isinstance(kind, (Integer, Float, Numeric))
            else {} if isinstance(kind, JSON) else ""
        )
    connection.execute(table.insert().values(**row))


def _make_release_0006_database(url: str, campaign_status: str = "running") -> None:
    """A database exactly as the release before organization isolation left it, with data."""
    engine = create_engine(url)
    dialect = "postgresql" if url.startswith("postgresql") else "sqlite"
    statements = [s.strip() for s in (LEGACY / f"0006_{dialect}.sql").read_text().split(";\n") if s.strip()]
    with engine.begin() as c:
        for statement in statements:
            body = "\n".join(line for line in statement.splitlines() if not line.startswith("--")).strip()
            if body:
                c.execute(text(body))
        c.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        c.execute(text("INSERT INTO alembic_version VALUES ('0006')"))
        _insert(c, "organizations", name="default")
        _insert(c, "campaigns", name="old one", status="paused", organization_id=None)  # from before campaigns had an organization
        _insert(c, "campaigns", name="newer", status=campaign_status, organization_id=1)
        _insert(c, "leads", campaign_id=1, phone_e164="+14155550101", status="pending")
        _insert(c, "leads", campaign_id=2, phone_e164="+14155550102", status="completed")
        _insert(c, "call_runs", lead_id=2, campaign_id=2, status="completed", twilio_call_sid="CA1")
        _insert(c, "appointments", lead_id=2, call_run_id=1, starts_at="2027-01-05T10:00:00+00:00")
        _insert(c, "usage_records", campaign_id=2, call_run_id=1, kind="call", quantity_seconds=60, amount_usd=0.12, organization_id=None)
        _insert(c, "jobs", kind="call_review", key="call-review:1", status="done", call_run_id=1)
        _insert(c, "jobs", kind="housekeeping", key="housekeeping:1", status="done")
        _insert(c, "do_not_call", phone_e164="+14155550199", reason="asked")
        _insert(c, "users", organization_id=1, email="boss@example.com", role="admin", password_hash="x", active=True)
    engine.dispose()


def test_a_database_of_the_previous_release_gets_an_organization_on_every_row(database_url):
    _make_release_0006_database(database_url)
    db.reset_engine_for_tests(database_url)  # starting the new release upgrades it
    engine = db.get_engine()
    postgres = engine.dialect.name == "postgresql"

    with engine.connect() as c:
        assert migrate.current_revision(c) == migrate.head_revision()
        for table in ("campaigns", "leads", "call_runs", "appointments", "usage_records", "do_not_call"):
            assert c.execute(text(f"SELECT organization_id FROM {table}")).scalars().all() == [1] * c.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar(), table
        assert c.execute(text("SELECT organization_id FROM jobs ORDER BY id")).scalars().all() == [1, None]  # housekeeping belongs to nobody
        # Nothing was lost on the way.
        assert [c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in ("campaigns", "leads", "call_runs", "appointments", "usage_records", "jobs", "do_not_call", "users")] == [2, 2, 1, 1, 1, 2, 1, 1]
    assert {"uq_campaigns_org_name", "uq_do_not_call_org_phone", "ix_call_runs_campaign_started", "ix_call_runs_org_started", "ix_call_runs_active", "ix_leads_organization_id"} <= _indexes(engine)
    assert migrate.upgrade(engine) == "current"

    with db.session_scope() as s:
        second = db.Organization(name="second")
        s.add(second)
        s.flush()
        second_id = second.id
        # An opt-out is per organization now: the same number can be on two lists, once each.
        assert db.is_dnc(s, "+14155550199", 1) and not db.is_dnc(s, "+14155550199", second_id)
        assert db.add_dnc(s, "+14155550199", second_id) is True and db.add_dnc(s, "+14155550199", second_id) is False
        # New rows are filed under their campaign's organization by the application.
        lead = db.Lead(campaign_id=2, phone_e164="+14155550103")
        s.add(lead)
        s.flush()
        assert lead.organization_id == 1

    def same_name_elsewhere():
        with db.session_scope() as s:
            s.add(db.Campaign(organization_id=second_id, name="newer", status="draft"))

    if postgres:
        same_name_elsewhere()  # two customers, one campaign name each
        with pytest.raises(IntegrityError):  # the database itself now refuses a row without an organization...
            with engine.begin() as c:
                c.execute(text("INSERT INTO do_not_call (phone_e164, reason, created_at) VALUES ('+14155550111', '', now())"))
        with pytest.raises(IntegrityError):  # ...and a status nobody defined
            with engine.begin() as c:
                c.execute(text("UPDATE campaigns SET status = 'whatever' WHERE id = 1"))
    else:
        # The documented limit of upgrading SQLite in place: the old deployment-wide
        # uniqueness of campaign names cannot be removed. More than one organization needs PostgreSQL.
        with pytest.raises(IntegrityError):
            same_name_elsewhere()


def test_the_upgrade_can_be_rolled_back_and_applied_again(database_url):
    from alembic import command

    _make_release_0006_database(database_url)
    db.reset_engine_for_tests(database_url)
    engine = db.get_engine()
    with engine.begin() as c:
        command.downgrade(migrate._config(c), "0006")
    with engine.connect() as c:
        assert migrate.current_revision(c) == "0006"
        assert c.execute(text("SELECT COUNT(*) FROM leads")).scalar() == 2  # rolling back loses no rows
        # The previous release's rule is back: one do-not-call entry per number.
        with pytest.raises(IntegrityError):
            c.execute(text("INSERT INTO do_not_call (phone_e164, reason, created_at) VALUES ('+14155550199', '', CURRENT_TIMESTAMP)"))
    if engine.dialect.name == "postgresql":
        assert "organization_id" not in {col["name"] for col in inspect(engine).get_columns("leads")}
    assert migrate.upgrade(engine) == "upgraded"
    with engine.connect() as c:
        assert c.execute(text("SELECT organization_id FROM leads")).scalars().all() == [1, 1]


def test_rolling_back_is_refused_once_two_organizations_hold_campaigns(database_url):
    from alembic import command

    db.reset_engine_for_tests(database_url)
    with db.session_scope() as s:
        other = db.Organization(name="second")
        s.add(other)
        s.flush()
        s.add_all([db.Campaign(name="q4", status="draft"), db.Campaign(organization_id=other.id, name="q4", status="draft")])
    engine = db.get_engine()
    with pytest.raises(RuntimeError, match="more than one organization"):
        with engine.begin() as c:
            command.downgrade(migrate._config(c), "0006")
    with engine.connect() as c:
        assert migrate.current_revision(c) == migrate.head_revision()  # still on this release, untouched


@pytest.mark.skipif(not POSTGRES_URL, reason="CHECK constraints are added to an existing database on PostgreSQL only")
def test_the_upgrade_stops_before_changing_anything_when_a_status_is_unknown():
    _wipe_postgres()
    _make_release_0006_database(POSTGRES_URL, campaign_status="archived")  # a status this release does not define
    engine = create_engine(POSTGRES_URL)
    with pytest.raises(RuntimeError, match=r"campaigns holds rows with a status this release does not know: \['archived'\]"):
        migrate.upgrade(engine)
    with engine.connect() as c:
        assert migrate.current_revision(c) == "0006"
        assert "organization_id" not in {col["name"] for col in inspect(c).get_columns("leads")}
    engine.dispose()


def _shape(engine) -> dict:
    """What a schema requires, without the names PostgreSQL makes up for constraints."""
    inspector = inspect(engine)
    shape = {}
    for table in sorted(inspector.get_table_names()):
        if table == "alembic_version":
            continue
        shape[table] = {
            "columns": sorted((c["name"], bool(c["nullable"])) for c in inspector.get_columns(table)),
            "unique": sorted(sorted(i["column_names"]) for i in inspector.get_indexes(table) if i.get("unique")),
            "indexes": sorted(sorted(i["column_names"]) for i in inspector.get_indexes(table) if not i.get("unique")),
            "foreign_keys": sorted((tuple(f["constrained_columns"]), f["referred_table"]) for f in inspector.get_foreign_keys(table)),
            "checks": sorted(c["name"] for c in inspector.get_check_constraints(table)),
        }
    return shape


@pytest.mark.skipif(not POSTGRES_URL, reason="an upgraded SQLite database keeps documented differences; PostgreSQL must have none")
def test_an_upgraded_database_and_a_new_one_have_the_same_schema():
    _wipe_postgres()
    _make_release_0006_database(POSTGRES_URL)
    db.reset_engine_for_tests(POSTGRES_URL)
    upgraded, upgraded_policies = _shape(db.get_engine()), db.row_level_security(db.get_engine())
    db.reset_engine_for_tests("sqlite://")
    _wipe_postgres()
    db.reset_engine_for_tests(POSTGRES_URL)
    created = _shape(db.get_engine())

    differences = {table: (upgraded.get(table), created.get(table)) for table in set(upgraded) | set(created) if upgraded.get(table) != created.get(table)}
    assert differences == {}
    # Row-level security is in place either way (reported as bypassed here only because the test connects as a superuser).
    assert upgraded_policies == db.row_level_security(db.get_engine()) == "bypassed"
