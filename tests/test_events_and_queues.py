"""Background work that cannot hold up calls, events that are recorded once, and a
call that is finished even when the process that carried it is gone."""

import asyncio
import io
import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from callagent import background, db, events, jobs
from callagent.config import Settings, get_settings, resolve_api_token
from callagent.db import CallRun, Campaign, Event, Job, Lead, TrainingDocument, run_blocking, run_db, session_scope, utcnow
from callagent.dialer.campaign_runner import apply_twilio_status, claim_lead_for_call
from callagent.postcall import summarizer
from callagent.training import versions
from callagent.voice.pipeline import load_call_context
from callagent.voice.transcript import save_transcript

SETTINGS = Settings(anthropic_api_key="x", cost_per_connected_minute_usd=0.10, cost_per_call_attempt_usd=0.02, max_attempts=3)


@pytest.fixture
def campaign(tmp_path, sample_playbook, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'q.db'}")
    with session_scope() as s:
        c = Campaign(name="q4", status="running")
        s.add(c)
        s.flush()
        versions.record_active(s, c, sample_playbook.model_copy(update={"open_questions": []}).model_dump(), source="onboard")
        s.add_all([Lead(campaign_id=c.id, phone_e164=f"+1415555010{i}", status="pending") for i in range(3)])
        s.flush()
        ids = {"campaign": c.id, "org": c.organization_id, "leads": [l.id for l in s.execute(select(Lead).order_by(Lead.id)).scalars()]}
    yield ids
    get_settings.cache_clear()


def job_of(key):
    with session_scope() as s:
        job = s.execute(select(Job).where(Job.key == key)).scalars().first()
        return None if job is None else {"status": job.status, "queue": job.queue, "org": job.organization_id, "run_at": job.run_at, "attempts": job.attempts}


def event_types(call_run_id=None):
    with session_scope() as s:
        query = select(Event.type).order_by(Event.id)
        return s.execute(query.where(Event.call_run_id == call_run_id) if call_run_id else query).scalars().all()


# ----------------------------------------------------------------------------- two thread pools (audit B-1)


async def test_background_work_that_fills_its_threads_does_not_delay_call_path_database_work(campaign, monkeypatch):
    monkeypatch.setenv("BACKGROUND_THREADS", "2")
    get_settings.cache_clear()
    db.reset_executors_for_tests()
    release, started = threading.Event(), []

    def slow_model_call(n):
        started.append(n)
        release.wait(10)
        return n

    stuck = [asyncio.create_task(run_blocking(slow_model_call, n)) for n in range(5)]  # more than the background pool holds
    stuck.append(asyncio.create_task(run_db(lambda s: (release.wait(10), 1)[1], slow=True)))
    await asyncio.sleep(0.2)
    assert sorted(started) == [0, 1]  # two are running, the rest wait their turn: the pool is bounded

    # A provider webhook, a tool on a live call, a transcript save: all still answer at once.
    begun = asyncio.get_running_loop().time()
    for _ in range(20):
        assert await asyncio.wait_for(run_db(lambda s: s.execute(text("SELECT 1")).scalar()), timeout=2) == 1
    assert asyncio.get_running_loop().time() - begun < 2

    release.set()
    assert sorted(await asyncio.gather(*stuck)) == [0, 1, 1, 2, 3, 4]
    db.reset_executors_for_tests()


# ----------------------------------------------------------------------------- queues, priority, fairness (audit D-10)


@pytest.fixture
def test_kinds():
    """Two throwaway kinds of job: one in the analysis queue that blocks until told, one in the calls queue."""
    gate, ran = asyncio.Event(), []

    @jobs.handler("t_analysis", queue="analysis")
    async def _slow(payload):
        ran.append(("start", payload["n"]))
        await gate.wait()
        ran.append(("end", payload["n"]))

    @jobs.handler("t_call", queue="calls")
    async def _fast(payload):
        ran.append(("call", payload["n"]))

    yield gate, ran
    for kind in ("t_analysis", "t_call"):
        jobs._handlers.pop(kind, None)
        jobs._queue_of.pop(kind, None)


async def test_a_saturated_analysis_queue_does_not_delay_call_related_work(campaign, test_kinds):
    gate, ran = test_kinds
    with session_scope() as s:
        for n in range(4):
            jobs.enqueue(s, "t_analysis", f"analysis:{n}", {"n": n})
    worker = jobs.Worker(queues={"analysis": 2, "calls": 1}, poll_seconds=0.05)
    worker.start()
    await asyncio.sleep(0.3)
    assert sorted(n for what, n in ran if what == "start") == [0, 1]  # its two slots are taken; two more wait

    with session_scope() as s:
        jobs.enqueue(s, "t_call", "call:1", {"n": 99})
    await asyncio.sleep(0.3)
    assert ("call", 99) in ran  # ran at once, although analysis is backed up
    with session_scope() as s:
        stats = jobs.queue_stats(s)
        assert (stats["analysis"]["running"], stats["analysis"]["waiting"], stats["calls"]["waiting"]) == (2, 2, 0)

    # Starting a job "right now" does not jump a full queue: it waits its turn.
    await worker.run_job_now("analysis:3")
    assert job_of("analysis:3")["status"] == "queued"

    gate.set()
    await asyncio.sleep(0.5)
    assert all(job_of(f"analysis:{n}")["status"] == "done" for n in range(4))
    await worker.stop()


def test_higher_priority_goes_first_and_no_organization_is_starved(campaign):
    with session_scope() as s:
        other = db.Organization(name="other")
        s.add(other)
        s.flush()
        acme, bolt = campaign["org"], other.id
        base = utcnow() - timedelta(minutes=10)
        for n in range(3):  # Acme queued three first
            jobs.enqueue(s, "call_review", f"acme:{n}", {}, organization_id=acme).run_at = base + timedelta(seconds=n)
        jobs.enqueue(s, "call_review", "bolt:0", {}, organization_id=bolt).run_at = base + timedelta(seconds=30)
        jobs.enqueue(s, "call_review", "urgent", {}, organization_id=acme, priority=10).run_at = base + timedelta(seconds=60)
        jobs.enqueue(s, "booking_webhook", "calls-queue", {}, organization_id=acme).run_at = base

    def next_key():
        with session_scope() as s:
            taken = jobs.claim(s, "w1", queue="analysis")
            return taken and taken["key"]

    # The urgent one first, whoever's it is. Then Bolt's, although three of Acme's have waited longer:
    # Acme already has a job running and Bolt has none. After that they are level, and the oldest goes.
    assert [next_key() for _ in range(5)] == ["urgent", "bolt:0", "acme:0", "acme:1", "acme:2"]
    assert next_key() is None  # the calls-queue job is not this pool's to take
    with session_scope() as s:
        assert jobs.claim(s, "w1", queue="calls")["key"] == "calls-queue"


def test_a_job_in_a_queue_nobody_configured_is_still_run(campaign):
    with session_scope() as s:
        jobs.enqueue(s, "whatever", "stray", {}, queue="typo-queue")
    with session_scope() as s:
        assert jobs.claim(s, "w1", queue="analysis") is None
        assert jobs.claim(s, "w1", queue="default")["key"] == "stray"  # the default pool takes what no pool owns


# ----------------------------------------------------------------------------- events


@pytest.fixture
def consumers():
    seen, fail = [], {"audit": False}

    @jobs.handler("t_notify", queue="calls")
    async def _notify(payload):
        seen.append(("notify", payload["event_id"], payload["starts_at"]))

    @jobs.handler("t_audit")
    async def _audit(payload):
        if fail["audit"]:
            raise RuntimeError("the audit store is down")
        seen.append(("audit", payload["event_id"], payload["type"]))

    events.subscribe("AppointmentBooked", "notify", kind="t_notify")
    events.subscribe("AppointmentBooked", "audit", kind="t_audit", max_attempts=2)
    yield seen, fail
    for consumer in ("notify", "audit"):
        events.unsubscribe("AppointmentBooked", consumer)
    for kind in ("t_notify", "t_audit"):
        jobs._handlers.pop(kind, None)
        jobs._queue_of.pop(kind, None)


async def test_an_event_is_recorded_once_and_each_consumer_gets_its_own_job(campaign, consumers):
    seen, fail = consumers
    fail["audit"] = True

    def book(s):
        return events.emit(s, "AppointmentBooked", key="appointment:7:booked", subject="appointment", subject_id=7, organization_id=campaign["org"],
                           campaign_id=campaign["campaign"], payload={"starts_at": "2027-01-05T10:00:00+00:00"})

    with session_scope() as s:
        event_id = book(s).id
    with session_scope() as s:
        assert book(s) is None  # the same fact reported again (a repeated webhook, a retried job): nothing is added
    with session_scope() as s:
        assert s.execute(select(Event.type, Event.version, Event.organization_id).where(Event.subject == "appointment")).all() == [("AppointmentBooked", 1, campaign["org"])]
        queued = {j.key: (j.queue, j.organization_id) for j in s.execute(select(Job)).scalars()}
        assert queued == {f"event:{event_id}:notify": ("calls", campaign["org"]), f"event:{event_id}:audit": ("default", campaign["org"])}

    await jobs.Worker(poll_seconds=0.05).run_once()
    # One consumer failing does not hold up the other: the notification went out, the audit will be retried on its own.
    assert seen == [("notify", event_id, "2027-01-05T10:00:00+00:00")]
    assert job_of(f"event:{event_id}:notify")["status"] == "done"
    assert job_of(f"event:{event_id}:audit")["status"] == "queued" and job_of(f"event:{event_id}:audit")["attempts"] == 1

    fail["audit"] = False
    with session_scope() as s:
        s.execute(select(Job).where(Job.key == f"event:{event_id}:audit")).scalar_one().run_at = utcnow()
    await jobs.Worker(poll_seconds=0.05).run_once()
    assert ("audit", event_id, "AppointmentBooked") in seen and len(seen) == 2  # the notification was not sent a second time


def test_an_event_and_its_consumers_exist_only_if_the_change_they_describe_was_saved(campaign, consumers):
    with pytest.raises(RuntimeError, match="booking failed"):
        with session_scope() as s:
            events.emit(s, "AppointmentBooked", key="appointment:8:booked", subject="appointment", subject_id=8, payload={"starts_at": "x"})
            raise RuntimeError("booking failed after the event was recorded")
    with session_scope() as s:
        assert s.execute(select(Event.id).where(Event.subject == "appointment")).all() == [] and s.execute(select(Job.id)).all() == []
    with pytest.raises(ValueError, match="unknown event type"):
        with session_scope() as s:
            events.emit(s, "SomethingElse", key="k", subject="call")


def test_a_call_leaves_a_trail_of_events_and_repeated_reports_add_nothing(campaign):
    with session_scope() as s:
        run_id, _ = claim_lead_for_call(s, campaign["campaign"], campaign["leads"][0])
    final = {"CallStatus": "completed", "CallDuration": "61"}
    for _ in range(3):  # the provider reports the ending three times
        with session_scope() as s:
            apply_twilio_status(s, run_id, final, SETTINGS)
    assert event_types(run_id) == ["CallInitiated", "UsageRecorded", "CallCompleted"]
    with session_scope() as s:
        trail = [events.describe(e) for e in events.for_call(s, run_id)]
        assert trail[0]["payload"]["lead_id"] == campaign["leads"][0] and trail[2]["payload"] == {"status": "completed", "seconds": 61, "direction": "outbound"}
        assert {e["campaign_id"] for e in trail} == {campaign["campaign"]} and all(e["occurred_at"] for e in trail)

    with session_scope() as s:
        failed_id, _ = claim_lead_for_call(s, campaign["campaign"], campaign["leads"][1])
        apply_twilio_status(s, failed_id, {"CallStatus": "no-answer"}, SETTINGS)
    assert event_types(failed_id) == ["CallInitiated", "UsageRecorded", "CallFailed"]


# ----------------------------------------------------------------------------- a call interrupted mid-way (audit B-2)


async def connected_call(campaign, lead_index=0):
    """A call whose audio stream connected and on which two lines were spoken."""
    with session_scope() as s:
        run_id, _ = claim_lead_for_call(s, campaign["campaign"], campaign["leads"][lead_index])
    await load_call_context(run_id)
    await save_transcript(run_id, [{"role": "assistant", "text": "Hi, this is Jordan, an AI assistant."}, {"role": "user", "text": "Not now, call me next week."}])
    return run_id


def state(run_id):
    with session_scope() as s:
        run = s.get(CallRun, run_id)
        return {"status": run.status, "disposition": run.disposition, "ended": run.ended_at is not None, "lead": s.get(Lead, run.lead_id).status, "lines": len(run.transcript or [])}


async def test_a_connected_call_has_a_safety_net_from_the_moment_it_connects(campaign):
    run_id = await connected_call(campaign)
    net = job_of(background.call_finalize_key(run_id))
    assert (net["status"], net["queue"], net["org"]) == ("queued", "calls", campaign["org"])
    # Due only after the longest a call can last, so it never interferes with a call in progress.
    with session_scope() as s:
        due = s.execute(select(Job.run_at).where(Job.key == background.call_finalize_key(run_id))).scalar_one()
        assert due > utcnow() + timedelta(seconds=get_settings().call_time_limit_seconds)
    assert event_types(run_id) == ["CallInitiated", "CallAnswered"]
    assert state(run_id) == {"status": "in-progress", "disposition": None, "ended": False, "lead": "calling", "lines": 2}


@pytest.mark.parametrize("campaign_status", ["running", "paused"])
async def test_a_call_cut_off_by_a_restart_still_gets_an_outcome_and_frees_its_lead(campaign, monkeypatch, campaign_status):
    run_id = await connected_call(campaign)
    with session_scope() as s:
        s.get(Campaign, campaign["campaign"]).status = campaign_status  # the safety net does not depend on a running campaign

    def unreachable(*args, **kwargs):
        raise RuntimeError("the model cannot be reached")

    monkeypatch.setattr(summarizer, "make_client", lambda settings=None: object())
    monkeypatch.setattr(summarizer, "review_call", unreachable)

    # The process died here: nothing called finalize. Later, the queue reaches the job.
    await background.finalize_interrupted_call({"call_run_id": run_id})

    after = state(run_id)
    assert after["status"] == "completed" and after["ended"] and after["lines"] == 2  # no longer counted as live; what was said is kept
    assert after["disposition"] == "other"  # nobody could review it yet, and it says so rather than guess
    assert after["lead"] == "pending"  # not stuck in "calling" for ever: it can be dialed, also by hand
    assert job_of(background.call_review_key(run_id))["status"] == "queued"  # the review will be tried again by the analysis queue
    # Ended by the engine, so the engine wrote the call's ledger line (the carrier's figure corrects it when it comes).
    assert event_types(run_id) == ["CallInitiated", "CallAnswered", "UsageRecorded", "CallFinalized"]

    await background.finalize_interrupted_call({"call_run_id": run_id})  # delivered twice: the second does nothing
    assert state(run_id) == after and event_types(run_id).count("CallFinalized") == 1


async def test_the_normal_ending_cancels_the_safety_net_and_a_running_ending_is_not_doubled(campaign, monkeypatch, sample_playbook):
    monkeypatch.setattr(summarizer, "make_client", lambda settings=None: object())
    monkeypatch.setattr(summarizer, "review_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("unreachable")))

    run_id = await connected_call(campaign)
    with session_scope() as s:
        s.get(CallRun, run_id).ended_at = utcnow()  # the normal ending has started (it set the end time) but not finished
    with pytest.raises(RuntimeError, match="may still be in progress"):
        await background.finalize_interrupted_call({"call_run_id": run_id})  # the job is retried later instead of doing the work twice
    assert state(run_id)["disposition"] is None

    await summarizer.finalize_call(run_id, sample_playbook, [{"role": "user", "text": "Hello"}], [], "live", "not_interested", get_settings())
    assert state(run_id)["disposition"] == "not_interested"
    assert job_of(background.call_finalize_key(run_id))["status"] == "done"  # ended the normal way: the safety net stood down
    await background.finalize_interrupted_call({"call_run_id": run_id})
    assert state(run_id)["disposition"] == "not_interested"


# ----------------------------------------------------------------------------- documents are read through the queue (audit D-11)


@pytest.fixture
def client(campaign, monkeypatch):
    from callagent.server import app

    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {resolve_api_token(get_settings())}"
        yield c


def test_an_accepted_upload_is_always_read_even_if_the_process_stops_first(client, campaign):
    uploaded = client.post("/api/campaigns/q4/documents", files={"file": ("pricing.txt", io.BytesIO(b"Pricing.\n\nThe Growth plan costs 49 dollars per month.\n"), "text/plain")})
    assert uploaded.status_code == 201
    document_id = uploaded.json()["id"]
    net = job_of(background.document_read_key(document_id))
    assert (net["queue"], net["org"]) == ("knowledge", campaign["org"]) and net["status"] == "done"  # read right away, on the queue
    assert client.get("/api/campaigns/q4/documents").json()[0]["status"] == "ready"
    with session_scope() as s:
        assert s.execute(select(Event.type).where(Event.subject == "document").order_by(Event.id)).scalars().all() == ["DocumentUploaded", "DocumentProcessed"]

    # A document left unread by an earlier release (no job behind it) gets one when the engine starts.
    with session_scope() as s:
        orphan = TrainingDocument(organization_id=campaign["org"], campaign_id=campaign["campaign"], kind="file", filename="old.txt", file_type="txt", status="pending")
        s.add(orphan)
        s.flush()
        orphan_id = orphan.id
    assert asyncio.run(background.queue_unread_documents()) == 1
    assert job_of(background.document_read_key(orphan_id))["status"] == "queued"


def test_the_event_log_and_queue_load_are_served_to_the_right_people(client, campaign):
    with session_scope() as s:
        run_id, _ = claim_lead_for_call(s, campaign["campaign"], campaign["leads"][2])
    listed = client.get("/api/events", params={"call_run_id": run_id}).json()
    assert [e["type"] for e in listed] == ["CallInitiated"] and listed[0]["call_run_id"] == run_id
    assert client.get("/api/events", params={"type": "CallAnswered"}).json() == []
    assert client.get("/api/events", params={"campaign": "q4", "limit": 1}).json()[0]["type"] == "CallInitiated"

    assert set(client.get("/api/jobs").json()["queues"]) >= {"calls", "analysis", "knowledge", "default"}  # the operator sees the deployment's load
    client.post("/api/users", json={"email": "admin@example.com", "role": "admin", "password": "correct horse battery"})
    token = client.post("/api/auth/login", json={"email": "admin@example.com", "password": "correct horse battery"}, headers={"Authorization": ""}).json()["token"]
    assert "queues" not in client.get("/api/jobs", headers={"Authorization": f"Bearer {token}"}).json()  # a customer's admin does not
