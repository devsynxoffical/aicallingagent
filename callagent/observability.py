"""What lets someone see how the engine is doing and what happened: logs, metrics,
health and an audit trail.

- **Logs** are one JSON object per line with the time, level, logger and message, and
  the ids that tie lines together: the request, the call, the job and the
  organization. Phone numbers and email addresses are masked wherever they appear.
- **Metrics** are in Prometheus text format at ``/metrics``.
- **Health**: ``/health/live`` says the process runs; ``/health/ready`` says it can do
  its work (database, schema version, background worker).
- **Audit**: every attempt to change something through the API is recorded with who,
  what, when, from where and how it ended.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

log = logging.getLogger(__name__)

# ----------------------------------------------------------------------------- what ties log lines together

request_id: ContextVar[str | None] = ContextVar("callagent_request_id", default=None)
call_id: ContextVar[int | None] = ContextVar("callagent_call_id", default=None)
job_id: ContextVar[int | None] = ContextVar("callagent_job_id", default=None)

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def new_request_id() -> str:
    return uuid.uuid4().hex


def accepted_request_id(given: str | None) -> str:
    """The caller's id when it is safe to repeat in logs and headers, otherwise a new one."""
    return given if given and _REQUEST_ID.match(given) else new_request_id()


# ----------------------------------------------------------------------------- masking

_PHONE = re.compile(r"(\+|%2B)(\d{2})\d{4,11}(\d{2})(?!\d)", re.IGNORECASE)
_EMAIL = re.compile(r"([A-Za-z0-9])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def mask(text: str) -> str:
    """Hide who a line is about: +14155550123 becomes +14*******23, jane@firm.com becomes j***@firm.com.
    Enough stays to tell two numbers apart while debugging; not enough to call one."""
    text = _PHONE.sub(lambda m: f"{m.group(1)}{m.group(2)}*******{m.group(3)}", text)
    return _EMAIL.sub(lambda m: f"{m.group(1)}***{m.group(2)}", text)


# ----------------------------------------------------------------------------- logging


def _context() -> dict[str, Any]:
    from .db import current_organization

    found = {"request_id": request_id.get(), "call_id": call_id.get(), "job_id": job_id.get(), "organization_id": current_organization.get()}
    return {key: value for key, value in found.items() if value is not None}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": mask(record.getMessage()),
            **_context(),
        }
        if record.exc_info:
            entry["exception"] = mask(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """For a person reading a terminal. Same masking, same ids."""

    def format(self, record: logging.LogRecord) -> str:
        ids = " ".join(f"{key}={value}" for key, value in _context().items())
        line = f"{datetime.fromtimestamp(record.created).strftime('%H:%M:%S')} {record.levelname:<7} {record.name}: {mask(record.getMessage())}"
        if ids:
            line += f"  [{ids}]"
        if record.exc_info:
            line += "\n" + mask(self.formatException(record.exc_info))
        return line


_handler: logging.Handler | None = None


def configure_logging(level: str = "INFO", style: str = "json", voice_level: str = "WARNING", stream=None) -> logging.Handler:
    """Send every log line of the engine and its libraries through one handler.

    ``voice_level`` is for the voice library, which at its default level prints the
    whole of a call's instructions and every sentence spoken: customer data that has
    no place in logs. Warnings and errors from it are kept.
    """
    global _handler
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter() if style == "json" else TextFormatter())
    root = logging.getLogger()
    if _handler is not None:
        root.removeHandler(_handler)  # configured again (a restart of the app inside one process): replace our own, keep anyone else's
    root.addHandler(handler)
    _handler = handler
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):  # the web server configures its own handlers: replace them
        server_logger = logging.getLogger(name)
        server_logger.handlers, server_logger.propagate = [], True
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic", "twilio.http_client"):  # these log each request's address at INFO
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        from loguru import logger as voice_logger
    except ImportError:  # the voice library is not installed in this environment
        return handler

    def forward(message) -> None:
        record = message.record
        number = logging.getLevelName(record["level"].name)  # the library has levels the standard one lacks (SUCCESS, TRACE)
        logging.getLogger(f"voice.{record['name']}").log(number if isinstance(number, int) else logging.INFO, record["message"])

    voice_logger.remove()
    voice_logger.add(forward, level=voice_level.upper(), format="{message}")
    return handler


# ----------------------------------------------------------------------------- metrics

REGISTRY = CollectorRegistry()
_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30)

HTTP_REQUESTS = Counter("callagent_http_requests_total", "HTTP requests answered", ["method", "route", "status"], registry=REGISTRY)
HTTP_DURATION = Histogram("callagent_http_request_duration_seconds", "Time to answer an HTTP request", ["method", "route"], buckets=_BUCKETS, registry=REGISTRY)
JOBS_FINISHED = Counter("callagent_jobs_finished_total", "Background job attempts, by how they ended", ["kind", "queue", "outcome"], registry=REGISTRY)
JOB_DURATION = Histogram("callagent_job_duration_seconds", "Time a background job attempt ran", ["kind"], buckets=(0.05, 0.25, 1, 5, 15, 60, 300, 900), registry=REGISTRY)
AI_REQUESTS = Counter("callagent_ai_requests_total", "AI model requests, by workflow and how they ended", ["workflow", "outcome"], registry=REGISTRY)
AI_TOKENS = Counter("callagent_ai_tokens_total", "Tokens used, as the provider reported them", ["workflow", "kind"], registry=REGISTRY)
AI_DURATION = Histogram("callagent_ai_request_duration_seconds", "Time an AI request took, retries included", ["workflow"], buckets=(0.5, 1, 2.5, 5, 10, 30, 60, 150, 300, 600), registry=REGISTRY)
AI_COST = Counter("callagent_ai_cost_usd_total", "AI cost at the price table in force (requests without a known price add nothing)", ["workflow"], registry=REGISTRY)
AI_UNRECORDED = Counter("callagent_ai_usage_unrecorded_total", "AI usage rows that could not be written: usage that will be missing from reports", registry=REGISTRY)
CALLS_FINISHED = Counter("callagent_calls_finished_total", "Calls that ended, by direction and outcome", ["direction", "outcome"], registry=REGISTRY)
CALL_MODEL_FAILURES = Counter("callagent_call_model_failures_total", "Turns of a live call the AI model failed to answer", registry=REGISTRY)
CALL_RESPONSE = Histogram("callagent_call_response_seconds", "Per call: median time from the caller finishing to the agent's first audio", buckets=(0.3, 0.5, 0.8, 1, 1.25, 1.5, 2, 3, 5), registry=REGISTRY)
CAMPAIGNS_STOPPED = Counter("callagent_campaigns_stopped_total", "Campaigns the engine stopped by itself, by kind of reason", ["reason"], registry=REGISTRY)
BACKUP_LAST = Gauge("callagent_backup_last_success_timestamp_seconds", "When the last scheduled backup was taken and verified (0: none since this process started)", registry=REGISTRY)
BACKUP_FAILURES = Counter("callagent_backup_failures_total", "Scheduled backups that failed", registry=REGISTRY)
AUDIT_UNRECORDED = Counter("callagent_audit_unrecorded_total", "Audit entries that could not be written", registry=REGISTRY)

# Read from the database and the process at the moment of a scrape.
CALLS_ACTIVE = Gauge("callagent_calls_active", "Calls not yet ended, by status", ["status"], registry=REGISTRY)
CAMPAIGNS_RUNNING = Gauge("callagent_campaigns_running", "Campaigns that are dialing", registry=REGISTRY)
JOBS_WAITING = Gauge("callagent_jobs", "Background jobs, by queue and status", ["queue", "status"], registry=REGISTRY)
JOBS_OLDEST = Gauge("callagent_jobs_oldest_due_seconds", "How long the longest-waiting due job has been due, by queue", ["queue"], registry=REGISTRY)
DB_POOL_IN_USE = Gauge("callagent_db_pool_in_use", "Database connections checked out", registry=REGISTRY)
DB_POOL_SIZE = Gauge("callagent_db_pool_size", "Database connections the pool may hold open", registry=REGISTRY)
THREADS_BACKLOG = Gauge("callagent_thread_pool_backlog", "Work waiting for a thread, by pool", ["pool"], registry=REGISTRY)
WORKER_RUNNING = Gauge("callagent_worker_running", "1 when the background job worker is running", registry=REGISTRY)
READY = Gauge("callagent_ready", "1 when the last readiness check passed", registry=REGISTRY)


def stop_reason_kind(reason: str) -> str:
    """A campaign's stop reason, reduced to a handful of kinds a chart can show."""
    text = reason.lower()
    for kind, words in (("provider_failure", ("provider is probably not working",)), ("budget", ("budget", "spend")), ("opt_outs", ("asked not to be called",)),
                        ("target", ("booked meetings",)), ("ended", ("end date",)), ("finished", ("no leads", "all leads"))):
        if any(word in text for word in words):
            return kind
    return "other"


def gather(s) -> dict[str, Any]:
    """The figures that are read, not counted: what the database and the process say now."""
    from sqlalchemy import func, select

    from . import db

    now = db.utcnow()
    calls = dict(s.execute(select(db.CallRun.status, func.count()).where(db.CallRun.status.in_(db.ACTIVE_CALL_STATUSES)).group_by(db.CallRun.status)).all())
    jobs = s.execute(select(db.Job.queue, db.Job.status, func.count()).where(db.Job.status.in_(("queued", "running", "dead"))).group_by(db.Job.queue, db.Job.status)).all()
    oldest = {}
    for queue in {queue for queue, status, _count in jobs if status == "queued"}:
        due = s.execute(select(db.Job.run_at).where(db.Job.queue == queue, db.Job.status == "queued", db.Job.run_at <= now).order_by(db.Job.run_at).limit(1)).scalar()
        if due is not None:
            oldest[queue] = max(0.0, (now - due).total_seconds())
    running = int(s.execute(select(func.count()).select_from(db.Campaign).where(db.Campaign.status == "running")).scalar() or 0)
    pool = db.get_engine().pool
    return {
        "calls": {status: int(calls.get(status, 0)) for status in db.ACTIVE_CALL_STATUSES},
        "jobs": [(queue, status, int(count)) for queue, status, count in jobs],
        "oldest": oldest,
        "campaigns_running": running,
        "pool_in_use": pool.checkedout() if hasattr(pool, "checkedout") else None,
        "pool_size": (pool.size() + max(0, getattr(pool, "_max_overflow", 0))) if hasattr(pool, "size") else None,
    }


def render(snapshot: dict[str, Any], worker_running: bool) -> bytes:
    """The metrics page: the counters kept by this process plus the snapshot just read."""
    from . import db, jobs

    for status, count in snapshot["calls"].items():
        CALLS_ACTIVE.labels(status).set(count)
    CAMPAIGNS_RUNNING.set(snapshot["campaigns_running"])
    JOBS_WAITING.clear()
    JOBS_OLDEST.clear()
    for queue in jobs.QUEUES:
        JOBS_OLDEST.labels(queue).set(snapshot["oldest"].get(queue, 0.0))
        for status in ("queued", "running", "dead"):
            JOBS_WAITING.labels(queue, status).set(0)
    for queue, status, count in snapshot["jobs"]:
        JOBS_WAITING.labels(queue, status).set(count)
    if snapshot["pool_in_use"] is not None:
        DB_POOL_IN_USE.set(snapshot["pool_in_use"])
        DB_POOL_SIZE.set(snapshot["pool_size"] or 0)
    for pool in ("db", "background"):
        THREADS_BACKLOG.labels(pool).set(db.executor(pool)._work_queue.qsize())
    WORKER_RUNNING.set(1 if worker_running else 0)
    return generate_latest(REGISTRY)


# ----------------------------------------------------------------------------- readiness


def readiness(s, worker_running: bool) -> dict[str, Any]:
    """Can this process do its work? Each check is named so the answer says what is wrong,
    without saying anything about how the deployment is configured."""
    from sqlalchemy import text

    from . import migrate

    checks: dict[str, str] = {}
    try:
        s.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"unreachable ({type(e).__name__})"
    if checks["database"] == "ok":
        try:
            current, head = migrate.current_revision(s.connection()), migrate.head_revision()
            checks["schema"] = "ok" if current == head else f"at version {current}, this release needs {head}"
        except Exception as e:
            checks["schema"] = f"unknown ({type(e).__name__})"
    checks["worker"] = "ok" if worker_running else "not running"
    if draining:
        checks["accepting"] = "stopping: finishing calls in progress, taking no new work"
    ok = all(value == "ok" for value in checks.values())
    READY.set(1 if ok else 0)
    return {"ready": ok, "checks": checks}


# ----------------------------------------------------------------------------- audit

AUDITED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def audit_entry(s, *, organization_id: int, actor: str, role: str, action: str, method: str = "", route: str = "", target: dict[str, Any] | None = None,
                status: int = 0, address: str | None = None) -> None:
    """Add one audit row in the caller's transaction."""
    from .db import AuditEntry

    s.add(AuditEntry(organization_id=organization_id, actor=(actor or "unknown")[:254], actor_role=role[:12], action=action[:80], method=method[:8], route=route[:200],
                     target={k: str(v)[:200] for k, v in (target or {}).items()}, status=status, request_id=request_id.get(), address=(address or None) and address[:64]))


# ----------------------------------------------------------------------------- the request wrapper


class RequestContext:
    """Around every HTTP request: give it an id, time it, count it, and record an audit
    entry for an attempt to change something. Plain ASGI, so streamed answers and
    WebSockets pass through untouched."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {name: value for name, value in scope.get("headers", [])}
        rid = accepted_request_id((headers.get(b"x-request-id") or b"").decode("latin-1") or None)
        token = request_id.set(rid)
        started = time.perf_counter()
        status = 500

        async def send_with_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message = {**message, "headers": [*message.get("headers", []), (b"x-request-id", rid.encode())]}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            method = scope.get("method", "")
            HTTP_REQUESTS.labels(method, route, str(status)).inc()
            HTTP_DURATION.labels(method, route).observe(time.perf_counter() - started)
            try:
                await self._audit(scope, method, route, status, headers)
            finally:
                request_id.reset(token)

    async def _audit(self, scope, method: str, route: str, status: int, headers: dict[bytes, bytes]) -> None:
        principal = (scope.get("state") or {}).get("caller")  # set once the caller is known, before their role is checked
        if principal is None or method not in AUDITED_METHODS or not route.startswith("/api/"):
            return  # reads, provider webhooks and unauthenticated attempts are not changes by a known actor
        from .db import run_db

        endpoint = getattr(scope.get("route"), "name", None) or route
        forwarded = (headers.get(b"x-forwarded-for") or b"").decode("latin-1").split(",")[0].strip()
        address = forwarded or (scope.get("client") or ("", 0))[0]
        actor = principal.email if principal.kind == "user" else "api token"

        def _write(s) -> None:
            audit_entry(s, organization_id=principal.organization_id, actor=actor, role=principal.role, action=endpoint, method=method, route=route,
                        target=dict(scope.get("path_params") or {}), status=status, address=address)

        try:
            await run_db(_write, unrestricted=True)
        except Exception:
            AUDIT_UNRECORDED.inc()
            log.exception("an audit entry could not be written (%s %s by %s, status %s)", method, route, actor, status)


class BodyLimit:
    """Refuse a request whose body is larger than the limit: at once when it says so
    (Content-Length), and as soon as it has sent that much when it does not."""

    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope.get("headers", [])).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            return await self._refuse(send)
        received, refused = 0, False

        async def counted():
            nonlocal received, refused
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    refused = True
                    return {"type": "http.disconnect"}  # the application stops reading; nothing more is buffered
            return message

        started = False

        async def sending(message):
            nonlocal started
            if refused and not started:
                started = True
                return await self._refuse(send)
            if not refused:
                started = started or message["type"] == "http.response.start"
                await send(message)

        try:
            await self.app(scope, counted, sending)
        except Exception:
            if not refused:
                raise  # not ours: an application that stops reading because we cut the body short is expected
        if refused and not started:
            await self._refuse(send)

    async def _refuse(self, send) -> None:
        body = json.dumps({"detail": f"the request is larger than {self.max_bytes // (1024 * 1024)} MB"}).encode()
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


# ----------------------------------------------------------------------------- stopping without cutting calls off

active_calls: set[int] = set()  # calls whose audio this process is carrying right now
draining = False


async def drain(seconds: float, poll: float = 1.0) -> int:
    """Wait for the calls this process carries to finish, up to ``seconds``. Returns how
    many were still in progress when the wait ended."""
    import asyncio

    deadline = time.monotonic() + seconds
    while active_calls and time.monotonic() < deadline:
        await asyncio.sleep(poll)
    return len(active_calls)

