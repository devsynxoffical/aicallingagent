"""Work that runs on the job queue: booking notifications and the post-call review safety net."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from callagent import background, db, jobs
from callagent.config import Settings, get_settings
from callagent.db import Job, utcnow
from callagent.postcall import summarizer
from callagent.postcall.summarizer import CallReview, QualificationEvidence
from callagent.training import versions
from callagent.voice import tools
from callagent.voice.session import CallSession

WHEN = (datetime.now(timezone.utc) + timedelta(days=3)).replace(microsecond=0).isoformat()


@pytest.fixture
def call(tmp_path, sample_playbook, monkeypatch):
    monkeypatch.setenv("BOOKING_WEBHOOK_URL", "https://crm.example.test/hooks/booking")
    monkeypatch.setenv("BOOKING_WEBHOOK_SECRET", "shared-secret")
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'bg.db'}")
    with db.session_scope() as s:
        c = db.Campaign(name="q4", status="running")
        s.add(c)
        s.flush()
        versions.record_active(s, c, sample_playbook.model_copy(update={"open_questions": []}).model_dump(), source="onboard")
        lead = db.Lead(campaign_id=c.id, phone_e164="+14155550100", first_name="Maria", status="calling", attempts=1)
        s.add(lead)
        s.flush()
        run = db.CallRun(lead_id=lead.id, campaign_id=c.id, status="in-progress", stream_connected=True, playbook_version=1)
        s.add(run)
        s.flush()
        ids = SimpleNamespace(campaign=c.id, lead=lead.id, run=run.id)
    yield ids
    get_settings.cache_clear()


def _session(call, sample_playbook) -> CallSession:
    return CallSession(call_run_id=call.run, lead={"id": call.lead, "phone_e164": "+14155550100", "first_name": "Maria", "extra": {"plan": "pro"}},
                       campaign_id=call.campaign, playbook=sample_playbook, settings=get_settings(), twilio_call_sid="CA1")


def _book(session, **arguments):
    return asyncio.run(tools.book_meeting(session, SimpleNamespace(arguments={"starts_at": WHEN, "timezone": "America/New_York", **arguments})))


class _Endpoint:
    """The customer's webhook endpoint, answering with the given status codes in turn."""

    def __init__(self, monkeypatch, statuses):
        self.statuses, self.requests = list(statuses), []
        endpoint = self

        class Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, content=None, headers=None):
                endpoint.requests.append(SimpleNamespace(url=url, body=content, headers=headers))
                return httpx.Response(endpoint.statuses.pop(0) if endpoint.statuses else 200)

        monkeypatch.setattr(background.httpx, "AsyncClient", Client)


def _due_now():
    with db.session_scope() as s:
        for job in s.query(Job).filter_by(status="queued"):
            job.run_at = utcnow() - timedelta(seconds=1)


# ----------------------------------------------------------------------------- booking


def test_booking_answers_the_call_at_once_and_the_notification_is_delivered_signed(call, sample_playbook, monkeypatch):
    endpoint = _Endpoint(monkeypatch, [200])
    result = _book(_session(call, sample_playbook))  # no worker is running: nothing here waits on the customer's server
    assert result["ok"] and endpoint.requests == []
    with db.session_scope() as s:
        job = s.query(Job).one()
        assert (job.kind, job.status, job.call_run_id) == ("booking_webhook", "queued", call.run)

    assert asyncio.run(jobs.Worker(name="w").run_once()) == 1
    request = endpoint.requests[0]
    body = json.loads(request.body)
    assert request.url == "https://crm.example.test/hooks/booking"
    assert body["appointment_id"] == result["appointment_id"] and body["lead"]["phone_e164"] == "+14155550100" and body["lead_extra"] == {"plan": "pro"}
    assert request.headers["X-Callagent-Signature"] == background.sign("shared-secret", request.body)
    assert request.headers["X-Callagent-Delivery"] == f"booking_webhook:appointment:{result['appointment_id']}"
    with db.session_scope() as s:
        assert s.query(Job).one().status == "done"


def test_a_failing_endpoint_is_retried_with_the_same_delivery_id_until_it_accepts(call, sample_playbook, monkeypatch):
    endpoint = _Endpoint(monkeypatch, [503, 500, 200])
    _book(_session(call, sample_playbook))
    worker = jobs.Worker(name="w")
    for _ in range(3):
        asyncio.run(worker.run_once())
        _due_now()
    assert len(endpoint.requests) == 3
    assert len({r.headers["X-Callagent-Delivery"] for r in endpoint.requests}) == 1  # the receiver can recognise the retries
    with db.session_scope() as s:
        job = s.query(Job).one()
        assert (job.status, job.attempts) == ("done", 3)
        assert s.query(db.Appointment).count() == 1


def test_an_endpoint_that_never_accepts_ends_as_a_dead_job_and_the_meeting_is_kept(call, sample_playbook, monkeypatch):
    endpoint = _Endpoint(monkeypatch, [500] * 20)
    _book(_session(call, sample_playbook))
    worker = jobs.Worker(name="w")
    for _ in range(10):
        asyncio.run(worker.run_once())
        _due_now()
    with db.session_scope() as s:
        job = s.query(Job).one()
        assert (job.status, job.attempts) == ("dead", 8) and "answered 500" in job.last_error
        assert s.query(db.Appointment).count() == 1  # the booking itself is never lost
    assert len(endpoint.requests) == 8


def test_booking_the_same_time_twice_on_one_call_is_one_meeting_and_one_notification(call, sample_playbook, monkeypatch):
    endpoint = _Endpoint(monkeypatch, [200, 200])
    session = _session(call, sample_playbook)
    first, second = _book(session), _book(session)
    assert first["appointment_id"] == second["appointment_id"]
    asyncio.run(jobs.Worker(name="w").run_once())
    with db.session_scope() as s:
        assert s.query(db.Appointment).count() == 1 and s.query(Job).count() == 1
    assert len(endpoint.requests) == 1

    other = (datetime.fromisoformat(WHEN) + timedelta(hours=2)).isoformat()  # the prospect changed their mind: a different meeting
    assert _book(session, starts_at=other)["appointment_id"] != first["appointment_id"]


def test_without_a_webhook_configured_no_job_is_created(call, sample_playbook, monkeypatch):
    monkeypatch.delenv("BOOKING_WEBHOOK_URL")
    get_settings.cache_clear()
    assert _book(_session(call, sample_playbook))["ok"]
    with db.session_scope() as s:
        assert s.query(Job).count() == 0 and s.query(db.Appointment).count() == 1


# ----------------------------------------------------------------------------- review safety net


def _review(**kw) -> CallReview:
    base = dict(disposition="interested_follow_up", summary="Wants a demo next month.", interest_level=7, objections_raised=[], commitments_made_by_agent=[],
                prospect_facts=["12 bookkeepers"], next_action="Send dates.", retry_recommended=False, retry_after_hours=0, agent_quality_score=8,
                coaching_notes=[], playbook_suggestions=[], evidence=QualificationEvidence(has_need="yes", decision_maker="yes"))
    return CallReview(**{**base, **kw})


TRANSCRIPT = [{"role": "assistant", "text": "Hi, is this Maria?"}, {"role": "user", "text": "Yes. I decide on tools here and we need this."}]


def _finalize(call, sample_playbook):
    return asyncio.run(summarizer.finalize_call(call.run, sample_playbook, TRANSCRIPT, [], "live", None, Settings(anthropic_api_key="x")))


def test_a_review_that_succeeds_when_the_call_ends_leaves_nothing_in_the_queue(call, sample_playbook, monkeypatch):
    monkeypatch.setattr(summarizer, "make_client", lambda settings=None: object())
    monkeypatch.setattr(summarizer, "review_call", lambda *a, **k: _review())
    assert _finalize(call, sample_playbook) is not None
    with db.session_scope() as s:
        job = s.query(Job).one()
        assert (job.kind, job.status, job.attempts) == ("call_review", "done", 0)
    assert asyncio.run(jobs.Worker(name="w").run_once()) == 0


def test_a_call_whose_review_failed_is_reviewed_later_from_the_stored_transcript(call, sample_playbook, monkeypatch):
    def unreachable(settings=None):
        raise RuntimeError("the model is unreachable")

    monkeypatch.setattr(summarizer, "make_client", unreachable)
    assert _finalize(call, sample_playbook) is None
    with db.session_scope() as s:
        run, lead, job = s.get(db.CallRun, call.run), s.get(db.Lead, call.lead), s.query(Job).one()
        assert run.transcript == TRANSCRIPT and run.error.startswith("review failed") and "review" not in run.summary
        assert lead.score is None  # nothing was invented in the meantime
        assert job.status == "queued" and job.run_at > utcnow() + timedelta(minutes=5)  # waiting as the safety net
        status_after_call = lead.status

    worker = jobs.Worker(name="w")
    _due_now()
    asyncio.run(worker.run_once())  # still unreachable: the job fails and is scheduled again
    with db.session_scope() as s:
        assert s.query(Job).one().status == "queued" and "unreachable" in s.query(Job).one().last_error

    seen = {}
    monkeypatch.setattr(summarizer, "make_client", lambda settings=None: object())
    monkeypatch.setattr(summarizer, "review_call", lambda client, playbook, text, tool_events, mode, settings: seen.update(text=text, mode=mode) or _review())
    _due_now()
    asyncio.run(worker.run_once())
    assert "Prospect: Yes. I decide on tools here and we need this." in seen["text"] and seen["mode"] == "live"
    with db.session_scope() as s:
        run, lead = s.get(db.CallRun, call.run), s.get(db.Lead, call.lead)
        assert run.summary["review"]["summary"] == "Wants a demo next month." and run.error is None
        assert run.disposition == "interested_follow_up" and lead.last_disposition == "interested_follow_up"
        assert lead.score == 45 and "12 bookkeepers" in lead.notes  # need 25 + decision-maker 20
        assert lead.status == status_after_call  # the dial queue is not rearranged after the fact
        assert s.query(Job).one().status == "done"

    monkeypatch.setattr(summarizer, "review_call", lambda *a, **k: pytest.fail("a reviewed call must not be reviewed again"))
    asyncio.run(background.review_call_later({"call_run_id": call.run}))  # delivered twice: nothing changes
    with db.session_scope() as s:
        assert s.get(db.Lead, call.lead).notes.count("12 bookkeepers") == 1


def test_a_call_nobody_answered_queues_no_review(call, sample_playbook):
    asyncio.run(summarizer.finalize_call(call.run, sample_playbook, [], [], "live", None, Settings(anthropic_api_key="x")))
    with db.session_scope() as s:
        assert s.query(Job).count() == 0


def test_dead_jobs_are_visible_and_can_be_retried_through_the_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from callagent.config import resolve_api_token

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'api.db'}")
    with db.session_scope() as s:
        job = jobs.enqueue(s, "booking_webhook", "booking_webhook:appointment:9", {"delivery_id": "x", "body": {}})
        job.status, job.last_error, job.attempts = "dead", "the booking webhook answered 500", 8
        dead_id = job.id
    from callagent.server import app

    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {resolve_api_token(get_settings())}"}
        assert client.get("/api/jobs").status_code == 401
        listing = client.get("/api/jobs?status=dead", headers=headers).json()
        assert listing["stats"]["dead"] == 1 and listing["jobs"][0]["last_error"] == "the booking webhook answered 500"
        assert client.post(f"/api/jobs/{dead_id}/retry", headers=headers).status_code == 200
        assert client.post(f"/api/jobs/{dead_id}/retry", headers=headers).status_code == 409
    get_settings.cache_clear()
