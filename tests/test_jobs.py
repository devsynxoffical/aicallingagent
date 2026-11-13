"""The durable job queue: nothing is lost, nothing runs twice at once, failures end somewhere visible."""

import asyncio
import random
import threading
from datetime import timedelta

import pytest

from callagent import db, jobs
from callagent.db import Job, JobAttempt, utcnow


@pytest.fixture(autouse=True)
def database(tmp_path, monkeypatch):
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'jobs.db'}")
    monkeypatch.setattr(jobs, "_handlers", {})
    monkeypatch.setattr(jobs, "_dead_hooks", {})


def _enqueue(key="k1", kind="work", **kw) -> int:
    with db.session_scope() as s:
        return jobs.enqueue(s, kind, key, {"n": 1}, **kw).id


def _job(job_id: int) -> Job:
    with db.session_scope() as s:
        return s.get(Job, job_id)


def _make_due(job_id: int) -> None:
    with db.session_scope() as s:
        s.get(Job, job_id).run_at = utcnow() - timedelta(seconds=1)


def test_asking_twice_for_the_same_work_is_one_job():
    first = _enqueue("review:call:7")
    assert _enqueue("review:call:7") == first
    with db.session_scope() as s:
        assert s.query(Job).count() == 1


def test_a_job_is_saved_or_dropped_together_with_the_change_that_caused_it():
    with pytest.raises(RuntimeError), db.session_scope() as s:
        s.add(db.Campaign(name="never saved"))
        jobs.enqueue(s, "work", "with-the-campaign")
        raise RuntimeError("the request failed after enqueueing")
    with db.session_scope() as s:
        assert s.query(Job).count() == 0 and s.query(db.Campaign).count() == 0


def test_a_successful_job_runs_once_and_keeps_its_history():
    seen = []

    @jobs.handler("work")
    async def work(payload):
        seen.append(payload)

    job_id = _enqueue()
    worker = jobs.Worker(name="w1")
    assert asyncio.run(worker.run_once()) == 1
    assert asyncio.run(worker.run_once()) == 0  # nothing left: it does not run again
    assert seen == [{"n": 1}]
    job = _job(job_id)
    assert (job.status, job.attempts, job.locked_by) == ("done", 1, None) and job.finished_at is not None
    with db.session_scope() as s:
        attempt = s.query(JobAttempt).one()
        assert (attempt.outcome, attempt.attempt, attempt.worker) == ("ok", 1, "w1")


def test_a_failing_job_is_retried_later_with_growing_waits_then_parked_as_dead():
    calls, dead = [], []

    async def on_dead(payload, error):
        dead.append((payload, error))

    @jobs.handler("work", on_dead=on_dead)
    async def work(payload):
        calls.append(1)
        raise ValueError("the webhook answered 503")

    job_id = _enqueue(max_attempts=3)
    worker = jobs.Worker(name="w1")
    waits = []
    for attempt in (1, 2, 3):
        assert asyncio.run(worker.run_once()) == 1
        job = _job(job_id)
        if attempt < 3:
            assert job.status == "queued" and "503" in job.last_error
            waits.append((job.run_at - utcnow()).total_seconds())
            assert asyncio.run(worker.run_once()) == 0  # not before its time
            _make_due(job_id)
    assert 25 < waits[0] < 40 and 55 < waits[1] < 75  # about 30 s, then about 60 s
    job = _job(job_id)
    assert (job.status, job.attempts) == ("dead", 3) and "ValueError" in job.last_error
    assert len(calls) == 3 and dead == [({"n": 1}, "ValueError: the webhook answered 503")]
    assert asyncio.run(worker.run_once()) == 0  # a dead job is not picked up again
    with db.session_scope() as s:
        assert [a.outcome for a in s.query(JobAttempt).order_by(JobAttempt.attempt)] == ["error", "error", "error"]
        assert jobs.stats(s)["dead"] == 1
        assert jobs.retry_dead(s, job_id)  # a person can send it round again after fixing the cause
    assert _job(job_id).status == "queued"


def test_backoff_doubles_and_is_capped():
    fixed = random.Random(0)
    values = [jobs.backoff_seconds(n, fixed) for n in (1, 2, 3, 4, 12)]
    assert 30 <= values[0] <= 36 and 60 <= values[1] <= 72 and 120 <= values[2] <= 144 and 240 <= values[3] <= 288
    assert 3600 <= values[4] <= 4320


def test_a_job_that_hangs_is_stopped_at_its_timeout_and_retried():
    @jobs.handler("work")
    async def work(payload):
        await asyncio.sleep(30)

    job_id = _enqueue(timeout_seconds=1, max_attempts=2)
    asyncio.run(jobs.Worker(name="w1").run_once())
    job = _job(job_id)
    assert job.status == "queued" and "timed out after 1 seconds" in job.last_error
    with db.session_scope() as s:
        assert s.query(JobAttempt).one().outcome == "timeout"


def test_a_job_whose_worker_died_is_taken_over_after_the_lease_and_not_before():
    ran = []

    @jobs.handler("work")
    async def work(payload):
        ran.append(1)

    job_id = _enqueue(timeout_seconds=60)
    with db.session_scope() as s:
        assert jobs.claim(s, "worker-that-dies")["id"] == job_id  # ... and it never reports back
    survivor = jobs.Worker(name="survivor")
    assert asyncio.run(survivor.run_once()) == 0  # the lease is still valid: nobody else touches it
    with db.session_scope() as s:
        s.get(Job, job_id).locked_until = utcnow() - timedelta(seconds=1)  # time passes
    assert asyncio.run(survivor.run_once()) == 1
    job = _job(job_id)
    assert (job.status, job.attempts) == ("done", 2) and ran == [1]
    with db.session_scope() as s:
        assert [(a.worker, a.outcome) for a in s.query(JobAttempt).order_by(JobAttempt.id)] == [("worker-that-dies", "abandoned"), ("survivor", "ok")]
        # The dead worker waking up late cannot overwrite the result.
        assert jobs.finish(s, job_id, "worker-that-dies", 1, "late failure") == "lost"
    assert _job(job_id).status == "done"


def test_a_job_abandoned_on_its_last_attempt_ends_dead_not_stuck():
    job_id = _enqueue(max_attempts=1)
    with db.session_scope() as s:
        jobs.claim(s, "worker-that-dies")
        s.get(Job, job_id).locked_until = utcnow() - timedelta(seconds=1)
    assert asyncio.run(jobs.Worker(name="survivor").run_once()) == 0
    assert _job(job_id).status == "dead"


def test_many_workers_polling_at_once_run_each_job_exactly_once():
    for i in range(20):
        _enqueue(f"job-{i}")
    barrier = threading.Barrier(8)
    taken, errors = [], []

    def poll(name):
        barrier.wait()
        try:
            while True:
                with db.session_scope() as s:
                    job = jobs.claim(s, name)
                if job is None:
                    return
                taken.append(job["id"])
        except Exception as e:
            errors.append(repr(e))

    threads = [threading.Thread(target=poll, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    assert len(taken) == 20 and len(set(taken)) == 20


def test_work_done_inline_cancels_its_safety_net_and_the_safety_net_fires_if_it_was_not():
    ran = []

    @jobs.handler("work")
    async def work(payload):
        ran.append(1)

    _enqueue("done-inline", delay_seconds=600)
    job_id = _enqueue("never-done-inline", delay_seconds=600)
    with db.session_scope() as s:
        assert jobs.complete_by_key(s, "done-inline")
    worker = jobs.Worker(name="w1")
    assert asyncio.run(worker.run_once()) == 0  # not due yet
    _make_due(job_id)
    assert asyncio.run(worker.run_once()) == 1 and ran == [1]


def test_a_job_can_be_run_right_away_instead_of_waiting_for_the_poll():
    ran = []

    @jobs.handler("work")
    async def work(payload):
        ran.append(payload["n"])

    _enqueue("now", delay_seconds=600)
    worker = jobs.Worker(name="w1")
    asyncio.run(worker.run_job_now("now"))
    asyncio.run(worker.run_job_now("now"))  # already done: nothing happens
    assert ran == [1]


def test_a_job_with_no_handler_fails_visibly_instead_of_vanishing():
    job_id = _enqueue(kind="nobody-handles-this", max_attempts=1)
    asyncio.run(jobs.Worker(name="w1").run_once())
    job = _job(job_id)
    assert job.status == "dead" and "no handler is registered" in job.last_error
