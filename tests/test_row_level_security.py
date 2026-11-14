"""PostgreSQL row-level security: the database keeps organizations apart on its own.

These tests deliberately run queries with NO organization filter, the mistake the
policies exist to contain. They need PostgreSQL (set TEST_POSTGRES_URL) and connect as
an ordinary role: a superuser is never bound by row-level security.
"""

import contextvars

import pytest
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.exc import DBAPIError

from callagent import db
from callagent.db import CallRun, Campaign, DoNotCall, Job, Lead, User, WebsiteRequest, current_organization, session_scope
from conftest import POSTGRES_URL, postgres_app_url

pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="needs PostgreSQL: set TEST_POSTGRES_URL")


@pytest.fixture
def orgs(tmp_path):
    """Two organizations with a row each in the tables that matter, created the way the
    system's own background paths create them: with no organization in context."""
    db.reset_engine_for_tests(postgres_app_url())
    ids = {}
    with session_scope() as s:
        for name in ("acme", "bolt"):
            org = db.Organization(name=name)
            s.add(org)
            s.flush()
            campaign = Campaign(organization_id=org.id, name="q4", status="running")
            s.add(campaign)
            s.flush()
            lead = Lead(campaign_id=campaign.id, phone_e164="+14155550101", status="pending")
            s.add(lead)
            s.flush()
            call = CallRun(lead_id=lead.id, campaign_id=campaign.id, status="completed", transcript=[{"role": "user", "text": f"{name} secret"}])
            s.add_all([call, User(organization_id=org.id, email=f"admin@{name}.example", role="admin", password_hash="x"),
                       WebsiteRequest(organization_id=org.id, kind="contact", email=f"visitor@{name}.example"),
                       Job(organization_id=org.id, kind="call_review", key=f"review:{name}")])
            db.add_dnc(s, "+14155550199", org.id)
            s.flush()
            ids[name] = {"org": org.id, "campaign": campaign.id, "lead": lead.id, "call": call.id}
        s.add(Job(kind="housekeeping", key="housekeeping:1"))  # belongs to nobody
    yield ids
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'after.db'}")


def acting_for(organization_id, fn):
    """Run ``fn(session)`` the way a request of that organization runs it."""

    def _run():
        current_organization.set(organization_id)
        with session_scope() as s:
            return fn(s)

    return contextvars.copy_context().run(_run)


def test_an_unfiltered_query_returns_only_the_callers_rows(orgs):
    acme, bolt = orgs["acme"], orgs["bolt"]
    for model in (Campaign, Lead, CallRun, User, WebsiteRequest, DoNotCall):
        seen = acting_for(acme["org"], lambda s, m=model: s.execute(select(m.organization_id)).scalars().all())  # no WHERE at all
        assert seen == [acme["org"]], model.__name__
    assert acting_for(acme["org"], lambda s: s.get(CallRun, bolt["call"])) is None  # by primary key, too
    assert acting_for(acme["org"], lambda s: s.execute(text("SELECT COUNT(*) FROM call_runs")).scalar()) == 1  # and in raw SQL
    # A job of nobody (housekeeping) stays visible; another organization's does not.
    assert sorted(acting_for(acme["org"], lambda s: s.execute(select(Job.key)).scalars().all())) == ["housekeeping:1", "review:acme"]


def test_another_organizations_rows_cannot_be_changed_or_created(orgs):
    acme, bolt = orgs["acme"], orgs["bolt"]
    assert acting_for(acme["org"], lambda s: s.execute(update(Campaign).values(status="paused")).rowcount) == 1  # an UPDATE with no WHERE touches one row: its own
    assert acting_for(acme["org"], lambda s: s.execute(text("DELETE FROM do_not_call")).rowcount) == 1
    with session_scope() as s:  # seen with no organization, as the system sees it
        assert dict(s.execute(select(Campaign.organization_id, Campaign.status)).all()) == {acme["org"]: "paused", bolt["org"]: "running"}
        assert s.execute(select(DoNotCall.organization_id)).scalars().all() == [bolt["org"]]

    with pytest.raises(DBAPIError, match="row-level security"):  # filing a row under someone else is refused by the database
        acting_for(acme["org"], lambda s: s.execute(text("INSERT INTO do_not_call (organization_id, phone_e164, reason, created_at) VALUES (:org, '+14155550111', '', now())"), {"org": bolt["org"]}))
    with pytest.raises(DBAPIError, match="row-level security"):  # and so is moving one's own row to someone else
        acting_for(acme["org"], lambda s: s.execute(update(Lead).values(organization_id=bolt["org"])))


def test_the_restriction_lasts_exactly_one_transaction(orgs):
    acting_for(orgs["acme"]["org"], lambda s: s.execute(select(Campaign.id)).all())
    # The next transaction on the same pooled connection, with no organization, sees everything again:
    # that is what webhooks, the dial loop and background jobs rely on.
    for _ in range(5):
        with session_scope() as s:
            assert len(s.execute(select(Campaign.id)).all()) == 2


def test_a_superuser_is_not_bound_and_the_engine_says_so(orgs):
    assert db.row_level_security(db.get_engine()) == "enforced"
    superuser = create_engine(POSTGRES_URL)
    try:
        assert db.row_level_security(superuser) == "bypassed"
        with superuser.connect() as c:
            c.execute(text("SELECT set_config('app.organization_id', :org, true)"), {"org": str(orgs["acme"]["org"])})
            assert c.execute(text("SELECT COUNT(*) FROM campaigns")).scalar() == 2  # the policies do not apply to it
    finally:
        superuser.dispose()
