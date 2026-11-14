"""Logs that can be followed and hold no personal data, health checks that check
something, metrics, and a record of who changed what."""

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from callagent import background, db, jobs, llm, observability
from callagent.config import get_settings, resolve_api_token
from callagent.db import AuditEntry, CallRun, Campaign, Job, Lead, Organization, session_scope

PASSWORD = "correct horse battery"
BRIEF = {"name": "q4", "company_name": "BrightBooks", "offer_summary": "Bookkeeping automation for accounting firms.", "target_customer": "Owners of small accounting firms",
         "call_goal": "book_meeting", "agent_name": "Jordan", "language": "en"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TWILIO_VALIDATE_SIGNATURE", "false")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'obs.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("METRICS_TOKEN", "scrape-me")
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'obs.db'}")
    from callagent.server import app

    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {resolve_api_token(get_settings())}"
        yield c
    get_settings.cache_clear()


@pytest.fixture
def logs():
    """The engine's log output as parsed lines, the way a log collector would receive it."""
    stream = io.StringIO()
    handler = observability.configure_logging("INFO", "json", stream=stream)
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    logging.getLogger().removeHandler(handler)


def add_user(client, email, role):
    assert client.post("/api/users", json={"email": email, "role": role, "password": PASSWORD}).status_code == 201


def sign_in(client, email, password=PASSWORD):
    response = client.post("/api/auth/login", json={"email": email, "password": password}, headers={"Authorization": ""})
    return {"Authorization": f"Bearer {response.json()['token']}"} if response.status_code == 200 else response


def audit_rows():
    with session_scope() as s:
        return [(e.actor, e.actor_role, e.action, e.status, dict(e.target or {})) for e in s.execute(select(AuditEntry).order_by(AuditEntry.id)).scalars()]


def metric(page: str, name: str, **labels) -> float | None:
    """The value of one sample on a metrics page."""
    for line in page.splitlines():
        if line.startswith(name) and all(f'{key}="{value}"' in line for key, value in labels.items()):
            if line[len(name)] in " {":
                return float(line.rsplit(" ", 1)[1])
    return None


# ----------------------------------------------------------------------------- logs (audit E-1, C-15)


def test_phone_numbers_and_email_addresses_never_reach_the_logs(logs):
    log = logging.getLogger("callagent.test")
    log.info("dialing %s for jane.doe@clinic.example, callback +33612345678", "+14155550123")
    log.warning("GET /api/campaigns/q4/dial?to=%2B14155550123 answered 200")
    try:
        raise ValueError("no route to +212661234567 (owner: omar@firm.ma)")
    except ValueError:
        log.exception("the provider refused the call")

    text = json.dumps(logs())
    for secret in ("4155550123", "612345678", "661234567", "jane.doe", "omar@"):
        assert secret not in text
    first = logs()[0]
    assert first["message"] == "dialing +14*******23 for j***@clinic.example, callback +33*******78"
    assert first["level"] == "info" and first["logger"] == "callagent.test" and first["time"].endswith("+00:00")
    assert "%2B14*******23" in logs()[1]["message"]
    assert "+21*******67" in logs()[2]["exception"] and "o***@firm.ma" in logs()[2]["exception"]
    # Ids, amounts and dates are not phone numbers.
    assert observability.mask("call 1234567890 cost 12.50 on 2026-11-14, job 42") == "call 1234567890 cost 12.50 on 2026-11-14, job 42"


def test_the_voice_librarys_chatter_stays_out_and_its_warnings_come_through_masked(logs):
    from loguru import logger as voice

    observability.configure_logging("INFO", "json", voice_level="WARNING", stream=logs.__closure__[0].cell_contents)
    voice.debug("Generating TTS: [Hello Jane, your invoice of 500 dollars is ready]")  # what a call says: never logged
    voice.info("LLM system prompt: lead notes ...")
    voice.warning("connection to +14155550123 lost")
    lines = [line for line in logs() if line["logger"].startswith("voice.")]
    assert [line["message"] for line in lines] == ["connection to +14*******23 lost"]


def test_a_request_has_an_id_that_is_on_its_log_lines_its_answer_and_the_job_it_caused(client, logs):
    assert client.post("/api/campaign-briefs", json=BRIEF).status_code == 201

    upload = client.post("/api/campaigns/q4/documents", files={"file": ("pricing.txt", b"Starter is forty-nine dollars per month.", "text/plain")}, headers={"X-Request-ID": "console-7f3a9c2e"})
    assert upload.status_code in (200, 201, 202), upload.text
    assert upload.headers["x-request-id"] == "console-7f3a9c2e"  # the caller's id is kept, so their logs and ours meet
    with session_scope() as s:
        job = s.execute(select(Job).where(Job.kind == background.DOCUMENT_READ)).scalars().one()
        assert job.request_id == "console-7f3a9c2e"

    # No id given, or one that could not be trusted in a log line: a new one is issued.
    issued = client.get("/api/campaigns").headers["x-request-id"]
    assert len(issued) == 32
    assert client.get("/api/campaigns", headers={"X-Request-ID": "x\"}; drop"}).headers["x-request-id"] != "x\"}; drop"

    logging.getLogger("callagent.test").info("outside any request")
    assert "request_id" not in logs()[-1]


async def test_a_jobs_log_lines_carry_the_job_the_request_and_the_call(tmp_path, logs):
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'jobs.db'}")
    seen = {}

    @jobs.handler("observed_test_job")
    async def handler(payload):
        logging.getLogger("callagent.test").info("working on it")
        seen.update(job=observability.job_id.get(), request=observability.request_id.get())

    try:
        token = observability.request_id.set("req-abcdef123456")
        with session_scope() as s:
            job_id = jobs.enqueue(s, "observed_test_job", "observed:1", {}).id
        observability.request_id.reset(token)
        assert await jobs.Worker().run_once() == 1
    finally:
        jobs._handlers.pop("observed_test_job", None)
    assert seen == {"job": job_id, "request": "req-abcdef123456"}
    line = next(entry for entry in logs() if entry["message"] == "working on it")
    assert line["job_id"] == job_id and line["request_id"] == "req-abcdef123456"


# ----------------------------------------------------------------------------- health (audit E-2)


def test_liveness_is_unconditional_and_readiness_names_what_is_wrong(client, monkeypatch):
    assert client.get("/health/live", headers={"Authorization": ""}).json() == {"alive": True}
    ready = client.get("/health/ready", headers={"Authorization": ""})
    assert ready.status_code == 200 and ready.json() == {"ready": True, "checks": {"database": "ok", "schema": "ok", "worker": "ok"}}
    # The public answers say nothing about how the deployment is configured.
    assert client.get("/health", headers={"Authorization": ""}).json() == {"ok": True}

    monkeypatch.setattr(background, "worker_running", lambda: False)
    stopped = client.get("/health/ready", headers={"Authorization": ""})
    assert stopped.status_code == 503 and stopped.json()["checks"]["worker"] == "not running"
    monkeypatch.undo()

    from callagent import migrate

    monkeypatch.setattr(migrate, "head_revision", lambda: "9999")  # a newer release against an older database
    behind = client.get("/health/ready", headers={"Authorization": ""})
    assert behind.status_code == 503 and "this release needs 9999" in behind.json()["checks"]["schema"]


def test_readiness_turns_red_when_the_database_stops_answering(client, monkeypatch):
    from callagent import server

    async def unreachable(*_args, **_kwargs):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(server, "run_db", unreachable)
    down = client.get("/health/ready", headers={"Authorization": ""})
    assert down.status_code == 503 and down.json() == {"ready": False, "checks": {"database": "unreachable (ConnectionError)"}}
    assert client.get("/health/live", headers={"Authorization": ""}).status_code == 200  # still alive: restarting it would not help


# ----------------------------------------------------------------------------- metrics (audit E-2)


def test_metrics_are_for_the_operator_only(client):
    assert client.get("/metrics", headers={"Authorization": ""}).status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer scrape-me"}).status_code == 200
    assert client.get("/metrics").status_code == 200  # the engine's own token
    add_user(client, "admin@example.com", "admin")
    assert client.get("/metrics", headers=sign_in(client, "admin@example.com")).status_code == 401  # not an organization's admin


async def test_the_metrics_page_shows_requests_jobs_calls_and_ai_use(client, tmp_path):
    scrape = lambda: client.get("/metrics", headers={"Authorization": "Bearer scrape-me"}).text  # noqa: E731
    before = scrape()
    count = lambda page, **labels: metric(page, "callagent_http_requests_total", **labels) or 0  # noqa: E731

    client.get("/api/campaigns")
    client.get("/api/campaigns/does-not-exist")
    client.get("/no/such/page")
    with session_scope() as s:
        c = Campaign(name="live", status="running", playbook_json={})
        s.add(c)
        s.flush()
        lead = Lead(campaign_id=c.id, phone_e164="+14155550100", status="calling")
        s.add(lead)
        s.flush()
        s.add(CallRun(lead_id=lead.id, campaign_id=c.id, status="in-progress"))
        jobs.enqueue(s, background.CALL_REVIEW, "review:waiting", {}, delay_seconds=-120)
        campaign_id = c.id
    with llm.attributed(campaign_id=campaign_id):
        llm.record_usage("call_review", "claude-opus-5", {"input": 1000, "output": 200, "cache_read": 0, "cache_write": 0}, outcome="ok", duration_ms=2300)
    observability.CALLS_FINISHED.labels("outbound", "meeting_booked").inc()

    page = scrape()
    # Requests are counted by the route as declared, so one campaign name is not one series each.
    assert count(page, method="GET", route="/api/campaigns", status="200") == count(before, method="GET", route="/api/campaigns", status="200") + 1
    assert count(page, method="GET", route="/api/campaigns/{name}", status="404") >= 1
    assert count(page, method="GET", route="unmatched", status="404") >= 1 and "/no/such/page" not in page
    assert metric(page, "callagent_http_request_duration_seconds_count", method="GET", route="/api/campaigns") >= 1
    # Read from the database at the moment of the scrape.
    assert metric(page, "callagent_calls_active", status="in-progress") == 1
    assert metric(page, "callagent_campaigns_running") == 1
    assert metric(page, "callagent_jobs", queue="analysis", status="queued") == 1
    assert metric(page, "callagent_jobs_oldest_due_seconds", queue="analysis") >= 120
    assert metric(page, "callagent_jobs", queue="calls", status="dead") == 0
    assert metric(page, "callagent_worker_running") == 1
    # Counted by this process as things happen.
    assert metric(page, "callagent_ai_requests_total", workflow="call_review", outcome="ok") >= 1
    assert metric(page, "callagent_ai_tokens_total", workflow="call_review", kind="input") >= 1000
    assert metric(page, "callagent_ai_cost_usd_total", workflow="call_review") >= 0.01
    assert metric(page, "callagent_calls_finished_total", direction="outbound", outcome="meeting_booked") >= 1
    # Nothing on the page identifies a person or an organization's data.
    assert "+1415" not in page and "example" not in page


async def test_a_finished_job_is_counted_by_how_it_ended(tmp_path):
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'jm.db'}")

    @jobs.handler("counted_test_job")
    async def handler(payload):
        if payload.get("fail"):
            raise RuntimeError("nope")

    read = lambda outcome: observability.REGISTRY.get_sample_value("callagent_jobs_finished_total", {"kind": "counted_test_job", "queue": "default", "outcome": outcome}) or 0  # noqa: E731
    try:
        with session_scope() as s:
            jobs.enqueue(s, "counted_test_job", "counted:ok", {})
            jobs.enqueue(s, "counted_test_job", "counted:bad", {"fail": True}, max_attempts=1)
        await jobs.Worker().run_once()
    finally:
        jobs._handlers.pop("counted_test_job", None)
    assert read("ok") == 1 and read("dead") == 1


# ----------------------------------------------------------------------------- audit trail (audit A-13, E-3)


def test_every_attempt_to_change_something_is_recorded_with_who_and_how_it_ended(client):
    add_user(client, "admin@example.com", "admin")
    add_user(client, "viewer@example.com", "viewer")
    admin, viewer = sign_in(client, "admin@example.com"), sign_in(client, "viewer@example.com")
    created = client.post("/api/campaign-briefs", json=BRIEF, headers=admin)
    assert created.status_code == 201, created.text
    assert client.put("/api/campaigns/q4/rules", json={"budget_usd": 250}, headers=admin).status_code == 200
    assert client.put("/api/campaigns/q4/rules", json={"budget_usd": 9999}, headers=viewer).status_code == 403  # refused: recorded too
    client.get("/api/campaigns", headers=admin)  # reading is not a change
    assert client.post("/api/auth/login", json={"email": "viewer@example.com", "password": "wrong password!"}, headers={"Authorization": ""}).status_code == 401

    rows = audit_rows()
    assert ("api token", "admin", "create_user", 201, {}) in rows
    assert ("admin@example.com", "admin", "sign_in", 200, {}) in rows
    assert ("admin@example.com", "admin", "set_rules", 200, {"name": "q4"}) in rows
    assert ("viewer@example.com", "viewer", "set_rules", 403, {"name": "q4"}) in rows
    assert ("viewer@example.com", "viewer", "sign_in_failed", 401, {}) in rows
    assert not any(action.startswith("list_") for _actor, _role, action, _status, _target in rows)

    # Nothing from a request's body is kept: not the password, not the budget.
    with session_scope() as s:
        stored = " ".join(json.dumps({"target": e.target, "action": e.action, "route": e.route}) for e in s.execute(select(AuditEntry)).scalars())
    assert PASSWORD not in stored and "wrong password" not in stored and "9999" not in stored

    listed = client.get("/api/audit", headers=admin).json()
    assert listed[0]["id"] > listed[-1]["id"] and {"at", "actor", "action", "status", "request_id", "address", "target"} <= set(listed[0])
    assert all(entry["request_id"] for entry in listed if entry["action"] == "set_rules")
    mine = client.get("/api/audit", params={"actor": "viewer@example.com"}, headers=admin).json()
    assert {entry["action"] for entry in mine} == {"sign_in", "sign_in_failed", "set_rules"}


def test_the_audit_trail_is_for_an_organizations_admins_and_nobody_elses(client):
    add_user(client, "admin@example.com", "admin")
    add_user(client, "operator@example.com", "operator")
    assert client.get("/api/audit", headers=sign_in(client, "operator@example.com")).status_code == 403

    other = client.post("/api/organizations", json={"name": "another customer", "admin_email": "boss@other.example", "admin_password": PASSWORD})
    assert other.status_code == 201, other.text
    theirs = client.get("/api/audit", headers=sign_in(client, "boss@other.example")).json()
    assert {entry["actor"] for entry in theirs} == {"boss@other.example"}  # only their own sign-in: nothing of the first organization
    assert any(entry["actor"] == "admin@example.com" or entry["actor"] == "api token" for entry in client.get("/api/audit", headers=sign_in(client, "admin@example.com")).json())


def test_no_code_path_changes_or_removes_an_audit_entry():
    """Append-only is a property of the code: nothing updates or deletes these rows."""
    import pathlib
    import re

    offenders = []
    for path in pathlib.Path(observability.__file__).parent.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        source = path.read_text()
        if re.search(r"(update|delete)\(\s*(db\.)?AuditEntry", source) or re.search(r"\.delete\([^)]*audit", source, re.IGNORECASE):
            offenders.append(path.name)
    assert offenders == []


def test_the_shipped_alert_rules_refer_only_to_metrics_that_exist():
    import pathlib
    import re

    yaml = pytest.importorskip("yaml")
    text = (pathlib.Path(observability.__file__).parent.parent / "ops" / "prometheus-alerts.yml").read_text()
    rules = [rule for group in yaml.safe_load(text)["groups"] for rule in group["rules"]]
    assert len(rules) >= 10 and all(rule["annotations"]["summary"] and rule["annotations"]["action"] for rule in rules)
    defined = {metric.name for metric in observability.REGISTRY.collect()}
    for name in set(re.findall(r"callagent_[a-z_]+", " ".join(rule["expr"] for rule in rules))):
        assert re.sub(r"_(total|bucket|count|sum)$", "", name) in defined or name in defined, name


# ----------------------------------------------------------------------------- limits and stopping (audit E-7, E-9, D-5)


def test_a_request_body_over_the_limit_is_refused_whether_or_not_it_says_how_large_it_is():
    from fastapi import FastAPI, Request

    app = FastAPI()
    read = []

    @app.post("/upload")
    async def upload(request: Request):
        body = await request.body()
        read.append(len(body))
        return {"bytes": len(body)}

    app.add_middleware(observability.BodyLimit, max_bytes=1024)
    with TestClient(app) as client:
        assert client.post("/upload", content=b"x" * 1024).json() == {"bytes": 1024}  # at the limit: fine
        too_large = client.post("/upload", content=b"x" * 1025)
        assert too_large.status_code == 413 and "larger than" in too_large.json()["detail"]

        def pieces():  # no Content-Length: the size is only known as it arrives
            for _ in range(8):
                yield b"y" * 400

        streamed = client.post("/upload", content=pieces())
        assert streamed.status_code == 413
    assert read == [1024]  # the oversized bodies never reached the application whole


def test_the_engine_has_a_body_limit_in_front_of_every_route(client):
    assert any(m.cls is observability.BodyLimit for m in client.app.user_middleware)
    limit = next(m for m in client.app.user_middleware if m.cls is observability.BodyLimit).kwargs["max_bytes"]
    assert limit == 30 * 1024 * 1024  # above the 25 MB a document may be, below anything that could fill a disk


async def test_stopping_waits_for_calls_in_progress_and_says_not_ready_meanwhile(client, monkeypatch):
    import asyncio

    from callagent import server

    monkeypatch.setenv("SHUTDOWN_DRAIN_SECONDS", "5")
    get_settings.cache_clear()
    monkeypatch.setattr(server, "leadership", None)
    observability.active_calls.update({41, 42})
    try:
        stopping = asyncio.create_task(server.stop_gracefully())
        await asyncio.sleep(0.2)
        assert not stopping.done()  # two calls are on the line: it waits
        not_ready = await asyncio.to_thread(client.get, "/health/ready")
        assert not_ready.status_code == 503 and "finishing calls in progress" in not_ready.json()["checks"]["accepting"]
        observability.active_calls.clear()  # both callers hung up
        assert await asyncio.wait_for(stopping, 5) == 0
    finally:
        observability.active_calls.clear()
        observability.draining = False

    observability.active_calls.add(7)
    try:
        assert await observability.drain(0.3, poll=0.05) == 1  # the wait ran out with a call still up: reported, not hidden
    finally:
        observability.active_calls.clear()


def test_with_automatic_migration_off_a_process_refuses_an_out_of_date_database(tmp_path, monkeypatch):
    from sqlalchemy import text

    from callagent import migrate

    url = f"sqlite:///{tmp_path / 'old.db'}"
    db.reset_engine_for_tests(url)  # a database at this release's version
    with db.get_engine().begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = '0009'"))  # as if left behind by an older release

    monkeypatch.setenv("AUTO_MIGRATE", "false")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match=r"is at version 0009; this release needs \d+\. Run `callagent db upgrade`"):
            db.reset_engine_for_tests(url)
        engine = db.create_engine(url)
        with engine.connect() as connection:
            assert migrate.current_revision(connection) == "0009"  # refused, and nothing was changed
        engine.dispose()
    finally:
        monkeypatch.delenv("AUTO_MIGRATE")
        get_settings.cache_clear()
        db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'after.db'}")
