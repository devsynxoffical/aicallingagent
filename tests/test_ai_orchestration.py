"""Every use of the AI model outside a live call: a named workflow with a deadline,
bounded retries, a limit on how many run at once, and a usage record of what it cost
and who it was for. The model here is a stand-in: nothing in this file reaches the
provider, and nothing in it shows that the provider accepts these requests."""

import ast
import pathlib
import threading
import time
from decimal import Decimal
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select

from callagent import db, llm
from callagent.config import Settings, get_settings, resolve_api_token
from callagent.db import AIUsage, CallRun, Campaign, Lead, Organization, TrainingRun, session_scope
from callagent.training import builder, versions
from callagent.voice.latency import add_model_usage

SETTINGS = Settings(anthropic_api_key="x", enable_refusal_fallbacks=False)
REQUEST = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


class Answer(BaseModel):
    verdict: str


def answered(tokens_in=1200, tokens_out=300, cache_read=0, cache_write=0, stop_reason="end_turn"):
    usage = SimpleNamespace(input_tokens=tokens_in, output_tokens=tokens_out, cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write)
    return SimpleNamespace(stop_reason=stop_reason, parsed_output=Answer(verdict="fine"), usage=usage, _request_id="req_test_1", stop_details=None, content=[])


def status_error(status, headers=None):
    response = httpx2.Response(status, request=REQUEST, headers=headers or {})
    kind = anthropic.RateLimitError if status == 429 else anthropic.APIStatusError
    return kind(f"status {status}", response=response, body=None)


class Model:
    """Stands in for the provider's client. ``script`` is what each request does in turn:
    an exception is raised, anything else is returned."""

    def __init__(self, *script):
        self.script, self.options, self.requests = list(script), [], []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._request, create=self._request))

    def with_options(self, **options):
        self.options.append(options)
        return self

    def _request(self, **request):
        self.requests.append(request)
        step = self.script.pop(0)
        if callable(step):
            step = step()
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def campaign(tmp_path, sample_playbook, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'ai.db'}")
    monkeypatch.setattr(llm, "_sleep", lambda seconds: None)  # retries wait; tests do not
    with session_scope() as s:
        c = Campaign(name="q4", status="running")
        s.add(c)
        s.flush()
        versions.record_active(s, c, sample_playbook.model_copy(update={"open_questions": []}).model_dump(), source="onboard")
        lead = Lead(campaign_id=c.id, phone_e164="+14155550100", status="completed")
        s.add(lead)
        s.flush()
        run = CallRun(lead_id=lead.id, campaign_id=c.id, status="completed")
        s.add(run)
        s.flush()
        ids = SimpleNamespace(campaign=c.id, org=c.organization_id, run=run.id)
    yield ids
    get_settings.cache_clear()


def ask(model, workflow="call_review", settings=SETTINGS):
    return llm.parse_structured(model, workflow=workflow, model="claude-opus-5", system="Review the call.", messages=[{"role": "user", "content": "hi"}],
                                output_model=Answer, settings=settings)


def usage_rows():
    with session_scope() as s:
        return [
            SimpleNamespace(workflow=u.workflow, model=u.model, outcome=u.outcome, attempts=u.attempts, tokens=(u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens),
                            cost=u.cost_usd, org=u.organization_id, campaign=u.campaign_id, call=u.call_run_id, prompt=u.prompt_hash, requests=u.requests, request_id=u.request_id)
            for u in s.execute(select(AIUsage).order_by(AIUsage.id)).scalars()
        ]


# ----------------------------------------------------------------------------- one way in (audit C-6)


def test_no_module_talks_to_the_model_except_through_the_orchestration_module():
    """A direct client call elsewhere would have no deadline, no limit and no usage record."""
    allowed = {
        "llm.py",  # the orchestration module itself
        "chat.py",  # `callagent chat`: a person at a terminal talking to the agent, streamed
        "twilio_client.py",  # text messages through the phone provider, not the model
    }
    offenders = []
    for path in pathlib.Path(llm.__file__).parent.rglob("*.py"):
        if path.name in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                target = ast.unparse(node.func)
                if target.endswith(("messages.create", "messages.parse", "messages.stream")) or target in ("anthropic.Anthropic", "anthropic.AsyncAnthropic"):
                    offenders.append(f"{path.name}:{node.lineno} {target}")
    assert offenders == []


def test_every_background_request_names_its_workflow():
    missing = []
    for path in pathlib.Path(llm.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and ast.unparse(node.func).split(".")[-1] in ("parse_structured", "text_completion"):
                names = {k.arg: k.value for k in node.keywords}
                workflow = names.get("workflow")
                if not (isinstance(workflow, ast.Constant) and workflow.value in llm.WORKFLOWS):
                    missing.append(f"{path.name}:{node.lineno}")
    assert missing == []
    with pytest.raises(ValueError, match="not a known AI workflow"):
        ask(Model(answered()), workflow="something_new")


def test_each_workflow_ends_before_the_job_that_runs_it_is_declared_timed_out():
    # The jobs' timeouts are in the code that enqueues them: reviews 300 s, training runs 900 s.
    assert llm.WORKFLOWS["call_review"].deadline_seconds < 300
    assert llm.WORKFLOWS["playbook_analysis"].deadline_seconds < 900
    assert all(policy.attempts <= 3 for policy in llm.WORKFLOWS.values())


# ----------------------------------------------------------------------------- deadline and retries


def test_a_request_carries_a_deadline_and_the_librarys_own_retries_are_off(campaign):
    model = Model(answered())
    assert ask(model).verdict == "fine"
    (options,) = model.options
    assert options["max_retries"] == 0 and 0 < options["timeout"] <= llm.WORKFLOWS["call_review"].deadline_seconds


def test_an_overloaded_provider_is_retried_a_bounded_number_of_times_then_reported_as_temporary(campaign):
    model = Model(status_error(529), status_error(429), answered())
    assert ask(model).verdict == "fine"  # third time lucky, within the three attempts a review gets
    assert len(model.requests) == 3

    model = Model(*[status_error(529) for _ in range(10)])
    with pytest.raises(llm.AITransientError, match="overloaded after 3 attempt"):
        ask(model)
    assert len(model.requests) == 3  # not ten

    assert [(u.outcome, u.attempts) for u in usage_rows()] == [("ok", 3), ("overloaded", 3)]


def test_unreachable_and_slow_are_temporary_too(campaign):
    with pytest.raises(llm.AITransientError, match="unreachable"):
        ask(Model(*[anthropic.APIConnectionError(request=REQUEST) for _ in range(3)]))
    with pytest.raises(llm.AITransientError, match="timeout"):
        ask(Model(*[anthropic.APITimeoutError(request=REQUEST) for _ in range(3)]))


def test_a_rejected_request_is_not_retried(campaign):
    model = Model(status_error(400), answered())
    with pytest.raises(llm.AIError) as raised:
        ask(model)
    assert not isinstance(raised.value, llm.AITransientError) and len(model.requests) == 1
    assert usage_rows()[-1].outcome == "failed"


def test_a_refusal_is_recorded_with_what_it_used_and_is_not_retried(campaign):
    model = Model(answered(stop_reason="refusal"), answered())
    with pytest.raises(llm.AIError, match="declined"):
        ask(model)
    assert len(model.requests) == 1
    row = usage_rows()[-1]
    assert row.outcome == "refused" and row.tokens[:2] == (1200, 300)


def test_the_deadline_bounds_the_whole_request_including_waits(campaign, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(llm, "_now", lambda: clock["now"])
    monkeypatch.setattr(llm, "_sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))

    def slow_failure():
        clock["now"] += 100  # each attempt takes 100 s before the provider answers "overloaded"
        return status_error(529)

    model = Model(slow_failure, slow_failure, slow_failure)
    with pytest.raises(llm.AITransientError):
        ask(model)
    # A review has 150 s in all: the second attempt was given what was left, a third was never started.
    assert len(model.requests) == 2
    assert model.options[1]["timeout"] < 50


def test_the_provider_saying_when_to_come_back_is_respected_within_the_deadline(campaign, monkeypatch):
    waits = []
    monkeypatch.setattr(llm, "_sleep", waits.append)
    assert ask(Model(status_error(429, {"retry-after": "7"}), answered())).verdict == "fine"
    assert waits == [7.0]

    waits.clear()
    with pytest.raises(llm.AITransientError):  # "come back in ten minutes" does not fit in a review's deadline
        ask(Model(status_error(429, {"retry-after": "600"}), answered()))
    assert waits == []


# ----------------------------------------------------------------------------- not everything at once


def test_no_more_than_the_configured_number_of_background_requests_run_at_once(campaign):
    settings = Settings(anthropic_api_key="x", enable_refusal_fallbacks=False, ai_background_concurrency=2)
    running, most, lock, release = [0], [0], threading.Lock(), threading.Event()

    def held_request():
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        release.wait(5)
        with lock:
            running[0] -= 1
        return answered()

    threads = [threading.Thread(target=ask, args=(Model(held_request),), kwargs={"settings": settings}) for _ in range(6)]
    for thread in threads:
        thread.start()
    time.sleep(0.3)
    assert most[0] == 2  # four are waiting their turn
    release.set()
    for thread in threads:
        thread.join(10)
    assert most[0] == 2 and len([u for u in usage_rows() if u.outcome == "ok"]) == 6


def test_a_request_that_cannot_get_a_turn_in_time_is_temporary_and_says_so(campaign, monkeypatch):
    settings = Settings(anthropic_api_key="x", enable_refusal_fallbacks=False, ai_background_concurrency=1)
    monkeypatch.setitem(llm.WORKFLOWS, "call_review", llm.Policy(deadline_seconds=0.4, attempts=1))
    release = threading.Event()
    holder = threading.Thread(target=lambda: llm.text_completion(Model(lambda: (release.wait(5), answered())[1]), workflow="coach", model="claude-opus-5",
                                                                   system="s", messages=[], settings=settings))
    holder.start()
    time.sleep(0.1)
    try:
        with pytest.raises(llm.AITransientError, match="did not finish in time"):
            ask(Model(answered()), settings=settings)
    finally:
        release.set()
        holder.join(10)
    assert [u.outcome for u in usage_rows() if u.workflow == "call_review"] == ["busy"]


# ----------------------------------------------------------------------------- what it cost and for whom (audit C-9, C-10)


def test_a_request_leaves_a_usage_row_for_the_right_organization_campaign_and_call(campaign):
    with llm.attributed(call_run_id=campaign.run):  # the campaign and organization follow from the call
        ask(Model(answered(tokens_in=1_000_000, tokens_out=100_000, cache_read=2_000_000, cache_write=400_000)))
    (row,) = usage_rows()
    assert (row.workflow, row.model, row.outcome, row.attempts, row.request_id) == ("call_review", "claude-opus-5", "ok", 1, "req_test_1")
    assert (row.org, row.campaign, row.call) == (campaign.org, campaign.campaign, campaign.run)
    assert row.tokens == (1_000_000, 100_000, 2_000_000, 400_000)
    # 1M in at $5 + 0.1M out at $25 + 2M cached at $0.50 + 0.4M written to the cache at $6.25
    assert row.cost == Decimal("5") + Decimal("2.5") + Decimal("1") + Decimal("2.5")
    assert row.prompt == llm.prompt_hash("Review the call.") and len(row.prompt) == 16


def test_usage_outside_any_campaign_belongs_to_the_deployments_own_organization(campaign):
    ask(Model(answered()), workflow="rehearsal")
    (row,) = usage_rows()
    assert (row.org, row.campaign, row.call) == (campaign.org, None, None)


def test_a_model_without_a_known_price_is_recorded_as_unknown_cost_not_as_free(campaign):
    tokens = {"input": 1000, "output": 1000, "cache_read": 0, "cache_write": 0}
    assert llm.cost_of("some-future-model", tokens, SETTINGS) is None
    own = Settings(anthropic_api_key="x", ai_prices='{"some-future-model": {"input": 3, "output": 15}}')
    assert llm.cost_of("some-future-model", tokens, own) == Decimal("0.018000")
    # Fast mode is the same model at a higher price; a model with no fast price listed is unknown, not standard.
    assert llm.cost_of("claude-opus-5", tokens, SETTINGS, fast=True) == Decimal("0.060000")
    assert llm.cost_of("claude-sonnet-5", tokens, SETTINGS, fast=True) is None
    assert llm.cost_of("claude-opus-5", tokens, Settings(anthropic_api_key="x", ai_prices="not json")) == Decimal("0.030000")


def test_a_conversation_is_one_usage_row_however_often_its_ending_is_processed(campaign):
    total = {"requests": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for _turn in range(4):  # what the voice pipeline reports after each model turn
        add_model_usage(total, SimpleNamespace(prompt_tokens=200, completion_tokens=40, cache_read_input_tokens=3000, cache_creation_input_tokens=0))
    assert total == {"requests": 4, "input": 800, "output": 160, "cache_read": 12000, "cache_write": 0}

    with llm.attributed(campaign_id=campaign.campaign, call_run_id=campaign.run):
        for _ in range(2):
            llm.record_live_call("claude-opus-5", total, requests=total["requests"], duration_ms=61_000, fingerprint="abc", fast=False, settings=SETTINGS)
        llm.record_live_call("claude-opus-5", {}, requests=0, duration_ms=5, fingerprint="abc", fast=False, settings=SETTINGS)  # nobody spoke
    (row,) = usage_rows()
    assert (row.workflow, row.requests, row.call, row.tokens) == ("live_call", 4, campaign.run, (800, 160, 12000, 0))
    assert row.cost == Decimal("0.014000")  # 800 x $5 + 160 x $25 + 12,000 x $0.50, per million


def test_metering_that_fails_does_not_fail_the_work(campaign, monkeypatch, caplog):
    def broken(*_a, **_k):
        raise RuntimeError("database is away")

    monkeypatch.setattr(db, "session_scope", broken)
    assert ask(Model(answered())).verdict == "fine"
    assert "AI usage could not be recorded" in caplog.text  # and it is loud about the money it did not count


def test_the_usage_report_is_per_organization_and_says_when_a_price_was_missing(campaign, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "secret-test-token")
    get_settings.cache_clear()
    from callagent.server import app

    with session_scope() as s:
        other = Organization(name="another customer")
        s.add(other)
        s.flush()
        other_id = other.id
    with llm.attributed(campaign_id=campaign.campaign):
        ask(Model(answered(tokens_in=1_000_000, tokens_out=0)))
        with pytest.raises(llm.AIError):
            ask(Model(status_error(400)), workflow="coach")
    with llm.attributed(organization_id=other_id):
        llm.parse_structured(Model(answered()), workflow="coach", model="some-future-model", system="s", messages=[], output_model=Answer, settings=SETTINGS)

    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {resolve_api_token(get_settings())}"}
        mine = client.get("/api/ai-usage", headers=headers).json()
        assert mine["cost_usd"] == "5.000000" and mine["cost_complete"] is True
        assert [(r["workflow"], r["records"], r["not_ok"]) for r in mine["by_workflow"]] == [("call_review", 1, 0), ("coach", 1, 1)]
        assert client.get("/api/ai-usage", params={"campaign": "q4"}, headers=headers).json()["cost_usd"] == "5.000000"

        theirs = client.get("/api/ai-usage", headers={**headers, "X-DevyTalk-Organization": str(other_id)}).json()
        assert [r["model"] for r in theirs["by_workflow"]] == ["some-future-model"]
        assert theirs["cost_complete"] is False and theirs["cost_usd"] == "0"


# ----------------------------------------------------------------------------- temporary failures go back to the queue (audit C-11)


async def test_a_training_run_that_meets_a_busy_provider_stays_running_for_the_queue_to_retry(campaign):
    with session_scope() as s:
        c = s.get(Campaign, campaign.campaign)
        c.script_text = "We sell bookkeeping automation to accounting firms."
        run = TrainingRun(organization_id=c.organization_id, campaign_id=c.id, status="running")
        s.add(run)
        s.flush()
        run_id = run.id

    class BusyAnalyzer:
        def analyze(self, material, previous=None):
            raise llm.AITransientError("playbook_analysis: the AI provider was overloaded after 2 attempt(s)")

    with pytest.raises(llm.AITransientError):
        await builder.run_training(run_id, analyzer_factory=BusyAnalyzer)
    with session_scope() as s:
        assert s.get(TrainingRun, run_id).status == "running"  # not "failed": nothing was wrong with the material

    class BrokenAnalyzer:
        def analyze(self, material, previous=None):
            raise llm.AIError("the request was rejected")

    await builder.run_training(run_id, analyzer_factory=BrokenAnalyzer)
    with session_scope() as s:
        run = s.get(TrainingRun, run_id)
        assert run.status == "failed" and "rejected" in run.error
