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

from .db import CallRun, Job, JobAttempt, run_db, utcnow

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]
DeadHook = Callable[[dict[str, Any], str], Awaitable[None]]

BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 3600
LEASE_GRACE_SECONDS = 30  # beyond the timeout, before another worker may take the job

# Worker slots per queue, in one process. Each queue has its own, so a backlog in one
# cannot take the slots of another: fifty call reviews waiting on a slow model do not
# delay a booking notification or the job that finishes an interrupted call.
#   calls      what a call or its customer depends on: finishing interrupted calls, booking notifications
#   analysis   call reviews and other model work about calls that are over
#   knowledge  reading documents and websites, building drafts
#   default    everything else
QUEUES: dict[str, int] = {"calls": 4, "analysis": 2, "knowledge": 2, "default": 2}

_handlers: dict[str, Handler] = {}
_dead_hooks: dict[str, DeadHook] = {}
_queue_of: dict[str, str] = {}


def handler(kind: str, *, queue: str = "default", on_dead: DeadHook | None = None) -> Callable[[Handler], Handler]:
    """Register the coroutine that does jobs of this kind, and the queue they wait in.
    It receives the payload and must be safe to run more than once for the same job."""

    def register(fn: Handler) -> Handler:
        _handlers[kind] = fn
        _queue_of[kind] = queue
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
    organization_id: int | None = None,
    priority: int = 0,
    queue: str | None = None,
) -> Job:
    """Add a job inside the caller's transaction. If a job with this key already exists
    it is returned unchanged: the same work is never queued twice.

    The job belongs to ``organization_id``, or to the organization of the call it is
    about. Only the deployment's own housekeeping has neither."""
    existing = s.execute(select(Job).where(Job.key == key)).scalars().first()
    if existing is not None:
        return existing
    if organization_id is None and call_run_id is not None:
        organization_id = s.execute(select(CallRun.organization_id).where(CallRun.id == call_run_id)).scalar_one_or_none()
    job = Job(
        kind=kind, key=key[:200], payload=dict(payload or {}), run_at=utcnow() + timedelta(seconds=delay_seconds),
        max_attempts=max_attempts, timeout_seconds=timeout_seconds, call_run_id=call_run_id, organization_id=organization_id,
        queue=queue or _queue_of.get(kind, "default"), priority=priority,
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
    return {"id": candidate.id, "kind": candidate.kind, "key": candidate.key, "queue": candidate.queue, "payload": dict(candidate.payload or {}),
            "attempt": candidate.attempts, "max_attempts": candidate.max_attempts, "timeout_seconds": candidate.timeout_seconds}


def _in_queue(queue: str | None):
    """Jobs a pool of slots is responsible for. The default pool also takes jobs whose
    queue no pool is configured for, so a job can never be stranded by a typo."""
    if queue is None:
        return ()
    if queue == "default":
        return (Job.queue.not_in([name for name in QUEUES if name != "default"]),)
    return (Job.queue == queue,)


def claim(s: Session, worker: str, now: datetime | None = None, *, queue: str | None = None) -> dict[str, Any] | None:
    """Take one job that is due, or whose worker stopped answering. Returns its details,
    or None when there is nothing to do. Only one caller can take a given job.

    Order: higher priority first; then the organization with the fewest jobs running
    right now in this queue, so one customer's backlog cannot starve another's; then
    the job that has waited longest."""
    now = now or utcnow()
    due = or_(
        (Job.status == "queued") & (Job.run_at <= now),
        (Job.status == "running") & (Job.locked_until < now),
    )
    candidates = list(s.execute(select(Job).where(due, *_in_queue(queue)).order_by(Job.priority.desc(), Job.run_at, Job.id).limit(50)).scalars())
    if len({c.organization_id for c in candidates}) > 1:
        busy = dict(s.execute(select(Job.organization_id, func.count()).where(Job.status == "running", Job.locked_until >= now, *_in_queue(queue)).group_by(Job.organization_id)).all())
        candidates.sort(key=lambda c: (-c.priority, busy.get(c.organization_id, 0), c.run_at, c.id))
    for candidate in candidates:
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


def _owned_by(organization_id: int | None, include_unowned: bool) -> tuple:
    if organization_id is None:
        return ()
    if include_unowned:
        return (or_(Job.organization_id == organization_id, Job.organization_id.is_(None)),)
    return (Job.organization_id == organization_id,)


def stats(s: Session, organization_id: int | None = None, *, include_unowned: bool = False) -> dict[str, Any]:
    """Counts by state and the age of the oldest job still waiting, for health and
    metrics: for one organization, or for the whole deployment when none is given."""
    mine = _owned_by(organization_id, include_unowned)
    now = utcnow()
    counts = dict(s.execute(select(Job.status, func.count()).where(*mine).group_by(Job.status)).all())
    oldest = s.execute(select(func.min(Job.run_at)).where(Job.status == "queued", Job.run_at <= now, *mine)).scalar()
    return {
        "queued": counts.get("queued", 0), "running": counts.get("running", 0), "done": counts.get("done", 0), "dead": counts.get("dead", 0),
        "oldest_due_seconds": round((now - oldest).total_seconds(), 1) if oldest else 0.0,
    }


def queue_stats(s: Session) -> dict[str, dict[str, Any]]:
    """For each queue: how many jobs are due and waiting, how many are running, how long
    the oldest due one has waited, and its slots. This is what backpressure and alerts
    look at: a growing wait in one queue is that queue falling behind."""
    now = utcnow()
    out = {name: {"waiting": 0, "running": 0, "oldest_due_seconds": 0.0, "slots": slots} for name, slots in QUEUES.items()}
    rows = s.execute(
        select(Job.queue, Job.status, func.count(), func.min(Job.run_at))
        .where(or_((Job.status == "queued") & (Job.run_at <= now), Job.status == "running"))
        .group_by(Job.queue, Job.status)
    ).all()
    for queue, status, n, oldest in rows:
        entry = out.setdefault(queue, {"waiting": 0, "running": 0, "oldest_due_seconds": 0.0, "slots": 0})
        if status == "running":
            entry["running"] = n
        else:
            entry["waiting"] = n
            if oldest is not None:
                if oldest.tzinfo is None:
                    oldest = oldest.replace(tzinfo=now.tzinfo)
                entry["oldest_due_seconds"] = round(max(0.0, (now - oldest).total_seconds()), 1)
    return out


def describe(job: Job) -> dict[str, Any]:
    return {
        "id": job.id, "kind": job.kind, "key": job.key, "status": job.status, "queue": job.queue, "priority": job.priority, "attempts": job.attempts, "max_attempts": job.max_attempts,
        "run_at": job.run_at.isoformat() if job.run_at else None, "last_error": job.last_error, "call_run_id": job.call_run_id,
        "created_at": job.created_at.isoformat() if job.created_at else None, "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def retry_dead(s: Session, job_id: int, organization_id: int | None = None, *, include_unowned: bool = False) -> bool:
    """A person looked at a dead job, fixed the cause, and wants it run again. With an
    organization given, only that organization's job can be retried (and, for the
    deployment's operator, housekeeping jobs that belong to none)."""
    job = s.get(Job, job_id)
    theirs = organization_id is None or job is None or job.organization_id == organization_id or (include_unowned and job.organization_id is None)
    if job is None or job.status != "dead" or not theirs:
        return False
    job.status, job.attempts, job.run_at, job.finished_at = "queued", 0, utcnow(), None
    return True


class Worker:
    """Runs due jobs inside the server process. Several processes may each run one: a job
    is only ever held by the worker whose lease it carries."""

    def __init__(self, *, queues: dict[str, int] | None = None, poll_seconds: float = 2.0, name: str | None = None):
        self.name = name or f"{socket.gethostname()}:{os.getpid()}"
        self.poll_seconds = poll_seconds
        self.queues = dict(queues or QUEUES)
        self._slots = {queue: asyncio.Semaphore(max(1, slots)) for queue, slots in self.queues.items()}
        self._task: asyncio.Task | None = None
        self._running: set[asyncio.Task] = set()

    def _pool(self, queue: str) -> asyncio.Semaphore:
        return self._slots.get(queue) or self._slots.get("default") or next(iter(self._slots.values()))

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
        waiting and its queue has a free slot. Used right after enqueueing, so normal
        work is not delayed by the queue; the queue remains the safety net if this
        process stops first. When the queue's slots are all taken the job simply waits
        its turn: starting it anyway is how a burst would overrun the limit."""
        queue = await run_db(lambda s: s.execute(select(Job.queue).where(Job.key == key, Job.status == "queued")).scalar())
        if queue is None:
            return
        pool = self._pool(queue)
        if pool.locked():
            return
        await pool.acquire()
        try:
            job = await run_db(lambda s: claim_key(s, self.name, key))
            if job is not None:
                await self._execute(job)
        finally:
            pool.release()

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
                for queue in self.queues:
                    pool = self._pool(queue)
                    while not pool.locked():  # never wait for a slot here: a full queue must not hold up the others
                        await pool.acquire()
                        job = await run_db(lambda s, q=queue: claim(s, self.name, queue=q))
                        if job is None:
                            pool.release()
                            break
                        task = asyncio.create_task(self._execute(job))
                        self._running.add(task)
                        task.add_done_callback(lambda t, p=pool: (self._running.discard(t), p.release()))
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
