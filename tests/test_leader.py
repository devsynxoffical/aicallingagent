"""Several engine processes, one of which dials (audit D-6, B-7). The processes here
are objects in one test process sharing a database, which is what decides."""

import asyncio
import threading
from datetime import timedelta

import pytest
from sqlalchemy import select

from callagent import db, leader
from callagent.config import Settings
from callagent.db import Campaign, Lease, session_scope, utcnow
from callagent.dialer.campaign_runner import CampaignRunner
from conftest import POSTGRES_URL, postgres_app_url

SETTINGS = Settings(anthropic_api_key="x")


@pytest.fixture(params=["sqlite", pytest.param("postgresql", marks=pytest.mark.skipif(not POSTGRES_URL, reason="TEST_POSTGRES_URL is not set"))])
def database(request, tmp_path):
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'lease.db'}" if request.param == "sqlite" else postgres_app_url())
    yield request.param
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'after.db'}")


def take(holder, ttl=30, now=None):
    with session_scope() as s:
        return leader.acquire(s, "dialer", holder, ttl, now=now)


def test_one_holder_at_a_time_and_the_next_takes_over_only_after_the_lease_runs_out(database):
    now = utcnow()
    assert take("a", now=now) is True
    assert take("b", now=now + timedelta(seconds=10)) is False  # held, not expired
    assert take("a", now=now + timedelta(seconds=20)) is True  # renewed: now good until second 50
    assert take("b", now=now + timedelta(seconds=45)) is False
    assert take("b", now=now + timedelta(seconds=51)) is True  # a stopped renewing
    assert take("a", now=now + timedelta(seconds=52)) is False  # and does not get it back while b holds it
    with session_scope() as s:
        assert leader.holder_of(s, "dialer") is None or leader.holder_of(s, "dialer") == "b"
        assert s.query(Lease).count() == 1


def test_of_many_processes_asking_at_the_same_moment_one_leads(database):
    start, results = threading.Barrier(10), []

    def ask(i):
        start.wait(5)
        try:
            results.append(take(f"process-{i}"))
        except Exception:  # on SQLite a writer may be turned away while another holds the lock: that process does not lead either
            results.append(False)

    threads = [threading.Thread(target=ask, args=(i,)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert results.count(True) == 1, results


def test_a_clean_stop_hands_the_lease_over_at_once(database):
    assert take("a") is True and take("b") is False
    with session_scope() as s:
        leader.release(s, "dialer", "b")  # not the holder: nothing happens
    assert take("b") is False
    with session_scope() as s:
        leader.release(s, "dialer", "a")
    assert take("b") is True


async def test_leadership_is_gained_lost_and_handed_over(database):
    events = []

    def process(name):
        async def gained():
            events.append(f"{name} leads")

        async def lost():
            events.append(f"{name} stops")

        return leader.Leadership("dialer", 30, on_gained=gained, on_lost=lost, holder=name)

    a, b = process("a"), process("b")
    assert await a.tick() is True and await b.tick() is False
    assert await a.tick() is True  # renewing is not gaining again
    assert events == ["a leads"]

    with session_scope() as s:  # a froze: its lease ran out without a renewal
        s.get(Lease, "dialer").expires_at = utcnow() - timedelta(seconds=1)
    assert await b.tick() is True
    assert await a.tick() is False  # a wakes up, finds the lease is someone else's, and stops dialing
    assert events == ["a leads", "b leads", "a stops"]

    await b.stop()  # a clean stop: the lease is given up, not left to run out
    assert await a.tick() is True
    assert events == ["a leads", "b leads", "a stops", "b stops", "a leads"]


async def test_a_leader_that_cannot_reach_the_database_stops_dialing_before_its_lease_runs_out(database, monkeypatch):
    stopped = []

    async def nothing():
        pass

    async def lost():
        stopped.append(True)

    clock = {"now": 100.0}
    monkeypatch.setattr(leader.time, "monotonic", lambda: clock["now"])
    process = leader.Leadership("dialer", 30, on_gained=nothing, on_lost=lost, holder="a")
    assert await process.tick() is True

    async def unreachable(*_args, **_kwargs):
        raise ConnectionError("database is away")

    monkeypatch.setattr(leader, "run_db", unreachable)
    clock["now"] += 10
    assert await process.tick() is True and stopped == []  # one missed renewal: the lease is still ours for 20 seconds
    clock["now"] += 11
    assert await process.tick() is False and stopped == [True]  # two thirds of it gone without a renewal: stop, before anyone can take over


async def test_only_the_leading_process_runs_dial_loops_and_it_picks_up_campaigns_started_elsewhere(database, monkeypatch):
    with session_scope() as s:
        s.add_all([Campaign(name="already running", status="running", playbook_json={}), Campaign(name="started later", status="ready", playbook_json={})])
        s.flush()
        first, second = [c.id for c in s.execute(select(Campaign).order_by(Campaign.id)).scalars()]

    async def idle_loop(self, campaign_id):  # the dialing itself is not what is tested here
        await asyncio.sleep(3600)

    monkeypatch.setattr(CampaignRunner, "_loop", idle_loop)
    leading, following = CampaignRunner(SETTINGS), CampaignRunner(SETTINGS)
    following.leading = False
    try:
        await leading.sync()
        await following.sync()
        assert leading.is_running(first) and not following.is_running(first)

        await following.start(second)  # an operator's click reached the process that does not lead
        assert not following.is_running(second) and not leading.is_running(second)
        with session_scope() as s:
            assert s.get(Campaign, second).status == "running"  # recorded, for the leader to act on
        await leading.sync()
        assert leading.is_running(second)

        await leading.step_down()  # the lease moved on
        assert not leading.is_running(first) and not leading.is_running(second) and leading.leading is False
        with session_scope() as s:
            assert [c.status for c in s.execute(select(Campaign).order_by(Campaign.id)).scalars()] == ["running", "running"]  # nothing was paused
        following.leading = True
        await following.sync()
        assert following.is_running(first) and following.is_running(second)
    finally:
        await leading.shutdown()
        await following.shutdown()
