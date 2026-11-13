"""Durable background jobs, kept in the application's own database.

Why a table and not a broker: the work that must not be lost (a call's review, a
booking notification, a training run) is always caused by a database change. Writing
the job in the same transaction as that change means the two are saved together or
not at all, and there is no second system to keep consistent. At this scale the
table is the queue; if volume outgrows it, the same handlers can be fed by a broker.

Guarantees:

- **Idempotent enqueue**: ``key`` is unique; asking twice for the same work is one job.
- **At-least-once execution**: a job is retried until it succeeds or runs out of
  attempts. Handlers must therefore be safe to run twice.
- **Retry with exponential backoff**, capped, with jitter.
- **Timeout** per attempt.
- **Lease**: a running job belongs to a worker until ``locked_until``. A worker that
  dies stops finishing its jobs; once the lease has passed, another worker takes over.
- **Dead letter**: after the last attempt the job stays in the table as ``dead`` with
  its error, and an optional ``on_dead`` hook tells the owner of the work.
- **History**: every attempt is a row in ``job_attempts``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import socket
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import Job, JobAttempt, run_db, utcnow

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]
DeadHook = Callable[[dict[str, Any], str], Awaitable[None]]

BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 3600
LEASE_GRACE_SECONDS = 30  # beyond the timeout, before another worker may take the job

_handlers: dict[str, Handler] = {}
_dead_hooks: dict[str, DeadHook] = {}


def handler(kind: str, *, on_dead: DeadHook | None = None) -> Callable[[Handler], Handler]:
    """Register the coroutine that does jobs of this kind. It receives the payload and
    must be safe to run more than once for the same job."""

    def register(fn: Handler) -> Handler:
        _handlers[kind] = fn
        if on_dead is not None:
            _dead_hooks[kind] = on_dead
        return fn

    return register


def backoff_seconds(attempts: int, rng: random.Random | None = None) -> float:
    """Wait before attempt ``attempts + 1``: 30 s, 60 s, 120 s ... capped at an hour, with
    up to 20% added at random so jobs that failed together do not retry together."""
    base = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** max(0, attempts - 1))
    return base * (1 + 0.2 * (rng or random).random())


def enqueue(
    s: Session,
    kind: str,
    key: str,
    payload: dict[str, Any] | None = None,
    *,
    delay_seconds: float = 0,
    max_attempts: int = 5,
    timeout_seconds: int = 300,
    call_run_id: int | None = None,
) -> Job:
    """Add a job inside the caller's transaction. If a job with this key already exists
    it is returned unchanged: the same work is never queued twice."""
    existing = s.execute(select(Job).where(Job.key == key)).scalars().first()
    if existing is not None:
        return existing
    job = Job(
        kind=kind, key=key[:200], payload=dict(payload or {}), run_at=utcnow() + timedelta(seconds=delay_seconds),
        max_attempts=max_attempts, timeout_seconds=timeout_seconds, call_run_id=call_run_id,
    )
    try:
        with s.begin_nested():  # a concurrent enqueue of the same key loses here, without undoing the caller's work
            s.add(job)
            s.flush()
    except IntegrityError:
        return s.execute(select(Job).where(Job.key == key)).scalars().one()
    return job


def _take(s: Session, candidate: Job, worker: str, now: datetime) -> dict[str, Any] | None:
    """Move one job to ``running`` under this worker's lease. None if someone else got it."""
    abandoned = candidate.status == "running"
    lease = now + timedelta(seconds=candidate.timeout_seconds + LEASE_GRACE_SECONDS)
    took = s.execute(
        update(Job)
        .where(Job.id == candidate.id, Job.status == candidate.status, Job.attempts == candidate.attempts)
        .values(status="running", locked_by=worker, locked_until=lease, attempts=Job.attempts + 1)
    ).rowcount
    if took != 1:
        return None  # another worker was faster
    if abandoned:
        _close_attempts(s, candidate.id, "abandoned", "the worker stopped before finishing")
    s.refresh(candidate)
    s.add(JobAttempt(job_id=candidate.id, attempt=candidate.attempts, worker=worker, started_at=now))
    return {"id": candidate.id, "kind": candidate.kind, "key": candidate.key, "payload": dict(candidate.payload or {}),
            "attempt": candidate.attempts, "max_attempts": candidate.max_attempts, "timeout_seconds": candidate.timeout_seconds}


def claim(s: Session, worker: str, now: datetime | None = None) -> dict[str, Any] | None:
    """Take one job that is due, or whose worker stopped answering. Returns its details,
    or None when there is nothing to do. Only one caller can take a given job."""
    now = now or utcnow()
    due = or_(
        (Job.status == "queued") & (Job.run_at <= now),
        (Job.status == "running") & (Job.locked_until < now),
    )
    for candidate in s.execute(select(Job).where(due).order_by(Job.run_at, Job.id).limit(10)).scalars():
        if candidate.status == "running" and candidate.attempts >= candidate.max_attempts:
            _close_attempts(s, candidate.id, "abandoned", "the worker stopped before finishing")
            candidate.status, candidate.finished_at, candidate.locked_by, candidate.locked_until = "dead", now, None, None
            candidate.last_error = "the worker stopped before finishing, on the last attempt"
            s.flush()
            continue
        taken = _take(s, candidate, worker, now)
        if taken is not None:
            return taken
    return None


def claim_key(s: Session, worker: str, key: str) -> dict[str, Any] | None:
    """Take one particular waiting job now, whatever its scheduled time."""
    candidate = s.execute(select(Job).where(Job.key == key, Job.status == "queued")).scalars().first()
    return _take(s, candidate, worker, utcnow()) if candidate is not None else None


def _close_attempts(s: Session, job_id: int, outcome: str, error: str | None) -> None:
    for attempt in s.execute(select(JobAttempt).where(JobAttempt.job_id == job_id, JobAttempt.outcome == "running")).scalars():
        attempt.outcome, attempt.error, attempt.finished_at = outcome, error, utcnow()


def finish(s: Session, job_id: int, worker: str, attempt: int, error: str | None = None, *, timed_out: bool = False) -> str:
    """Record how an attempt ended. Returns the job's new status. A worker that lost its
    lease (the job was taken over) changes nothing."""
    job = s.get(Job, job_id)
    if job is None or job.status != "running" or job.locked_by != worker or job.attempts != attempt:
        return "lost"
    _close_attempts(s, job_id, "ok" if error is None else ("timeout" if timed_out else "error"), error)
    job.locked_by, job.locked_until = None, None
    if error is None:
        job.status, job.finished_at, job.last_error = "done", utcnow(), None
    elif job.attempts >= job.max_attempts:
        job.status, job.finished_at, job.last_error = "dead", utcnow(), error[:4000]
    else:
        job.status, job.last_error = "queued", error[:4000]
        job.run_at = utcnow() + timedelta(seconds=backoff_seconds(job.attempts))
    return job.status


def complete_by_key(s: Session, key: str) -> bool:
    """The work was done by another path (for example inline, right away): the job that
    stood ready as its safety net is no longer needed."""
    job = s.execute(select(Job).where(Job.key == key, Job.status == "queued")).scalars().first()
    if job is None:
        return False
    job.status, job.finished_at = "done", utcnow()
    return True


def stats(s: Session) -> dict[str, Any]:
    """Counts by state and the age of the oldest job still waiting, for health and metrics."""
    counts = dict(s.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    oldest = s.execute(select(func.min(Job.run_at)).where(Job.status == "queued", Job.run_at <= utcnow())).scalar()
    return {
        "queued": counts.get("queued", 0), "running": counts.get("running", 0), "done": counts.get("done", 0), "dead": counts.get("dead", 0),
        "oldest_due_seconds": round((utcnow() - oldest).total_seconds(), 1) if oldest else 0.0,
    }


def describe(job: Job) -> dict[str, Any]:
    return {
        "id": job.id, "kind": job.kind, "key": job.key, "status": job.status, "attempts": job.attempts, "max_attempts": job.max_attempts,
        "run_at": job.run_at.isoformat() if job.run_at else None, "last_error": job.last_error, "call_run_id": job.call_run_id,
        "created_at": job.created_at.isoformat() if job.created_at else None, "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def retry_dead(s: Session, job_id: int) -> bool:
    """A person looked at a dead job, fixed the cause, and wants it run again."""
    job = s.get(Job, job_id)
    if job is None or job.status != "dead":
        return False
    job.status, job.attempts, job.run_at, job.finished_at = "queued", 0, utcnow(), None
    return True


class Worker:
    """Runs due jobs inside the server process. Several processes may each run one: a job
    is only ever held by the worker whose lease it carries."""

    def __init__(self, *, concurrency: int = 4, poll_seconds: float = 2.0, name: str | None = None):
        self.name = name or f"{socket.gethostname()}:{os.getpid()}"
        self.poll_seconds = poll_seconds
        self._slots = asyncio.Semaphore(concurrency)
        self._task: asyncio.Task | None = None
        self._running: set[asyncio.Task] = set()

    async def run_once(self) -> int:
        """Take and run every job that is due right now. Returns how many ran."""
        ran = 0
        while True:
            job = await run_db(lambda s: claim(s, self.name))
            if job is None:
                return ran
            await self._execute(job)
            ran += 1

    async def run_job_now(self, key: str) -> None:
        """Run one job immediately instead of waiting for the next poll, if it is still
        waiting. Used right after enqueueing, so normal work is not delayed by the queue;
        the queue remains the safety net if this process stops first."""
        job = await run_db(lambda s: claim_key(s, self.name, key))
        if job is not None:
            await self._execute(job)

    async def _execute(self, job: dict[str, Any]) -> None:
        fn = _handlers.get(job["kind"])
        error: str | None = None
        timed_out = False
        try:
            if fn is None:
                raise RuntimeError(f"no handler is registered for job kind '{job['kind']}'")
            await asyncio.wait_for(fn(job["payload"]), timeout=job["timeout_seconds"])
        except asyncio.TimeoutError:
            error, timed_out = f"timed out after {job['timeout_seconds']} seconds", True
        except asyncio.CancelledError:
            raise  # the process is stopping: leave the job leased; it is taken over after the lease
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
        status = await run_db(lambda s: finish(s, job["id"], self.name, job["attempt"], error, timed_out=timed_out))
        if error:
            log.warning("job %s (%s) attempt %s/%s failed: %s -> %s", job["id"], job["kind"], job["attempt"], job["max_attempts"], error, status)
        if status == "dead" and job["kind"] in _dead_hooks:
            try:
                await _dead_hooks[job["kind"]](job["payload"], error or "")
            except Exception:
                log.exception("job %s: the dead-letter hook failed", job["id"])

    async def _loop(self) -> None:
        while True:
            try:
                while True:
                    await self._slots.acquire()
                    job = await run_db(lambda s: claim(s, self.name))
                    if job is None:
                        self._slots.release()
                        break
                    task = asyncio.create_task(self._execute(job))
                    self._running.add(task)
                    task.add_done_callback(lambda t: (self._running.discard(t), self._slots.release()))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("job worker: polling failed")
            await asyncio.sleep(self.poll_seconds)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="job-worker")

    async def stop(self) -> None:
        tasks = [t for t in (self._task, *self._running) if t is not None]
        self._task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
