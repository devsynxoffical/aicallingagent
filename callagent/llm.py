"""Every use of the AI model outside a live phone conversation goes through this module.

Live-call inference goes through Pipecat's AnthropicLLMService (voice/llm_service.py),
with its own short timeouts; its usage is recorded here too (record_live_call).

What this module guarantees for a background request (a review, a coach run, a
training run, a translation):

- it is made under a named *workflow* with a deadline, so it cannot hang a job;
- an overloaded or unreachable provider is retried a bounded number of times within
  that deadline, and then reported as temporary (AITransientError) so the job queue
  decides when to try again; a rejected request is permanent (AIError) and is not retried;
- no more than a set number run at once, so analysis cannot starve live calls of the
  provider's capacity;
- it leaves one usage row: workflow, model, prompt fingerprint, tokens, duration,
  outcome and cost, attributed to the organization, campaign and call it was for.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Iterator, TypeVar

import anthropic
from pydantic import BaseModel

from .config import Settings, get_settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AIError(RuntimeError):
    """The request was rejected or refused. Trying again will not help."""


class AITransientError(AIError):
    """The provider was overloaded, unreachable or too slow. Worth trying again later."""


@dataclass(frozen=True)
class Policy:
    deadline_seconds: float  # for the whole request, retries and waiting included
    attempts: int


# Every background use of the model has a name here. The deadline of each is shorter
# than the timeout of the job that runs it, so the request ends before the job is
# declared timed out and started again.
WORKFLOWS: dict[str, Policy] = {
    "call_review": Policy(deadline_seconds=150, attempts=3),
    "coach": Policy(deadline_seconds=240, attempts=2),
    "playbook_analysis": Policy(deadline_seconds=600, attempts=2),
    "localization": Policy(deadline_seconds=180, attempts=2),
    "rehearsal": Policy(deadline_seconds=180, attempts=2),
}
LIVE_CALL = "live_call"

# US dollars per million tokens: (input, output, cache read). List prices on 2026-11-03.
# A model that is not listed has no price here, and its cost is recorded as unknown.
# Writing to the cache is billed at 1.25 times the input price.
LIST_PRICES: dict[str, tuple[str, str, str]] = {
    "claude-fable-5-2": ("8", "40", "0.20"),
    "claude-fable-5-1": ("10", "50", "1"),
    "claude-fable-5": ("10", "50", "1"),
    "claude-opus-5-5": ("4", "20", "0.20"),
    "claude-opus-5": ("5", "25", "0.50"),
    "claude-opus-4-8": ("5", "25", "0.50"),
    "claude-opus-4-7": ("5", "25", "0.50"),
    "claude-opus-4-6": ("5", "25", "0.50"),
    "claude-sonnet-5-5": ("2", "10", "0.10"),
    "claude-sonnet-5": ("2", "10", "0.20"),
    "claude-sonnet-4-6": ("3", "15", "0.30"),
    "claude-haiku-5-5": ("0.10", "0.50", "0.01"),
    "claude-haiku-4-5": ("1", "5", "0.10"),
}
# Fast mode is the same model at a higher price: (input, output).
FAST_PRICES: dict[str, tuple[str, str]] = {
    "claude-opus-5-5": ("8", "40"),
    "claude-opus-5": ("10", "50"),
    "claude-opus-4-8": ("10", "50"),
    "claude-fable-5-2": ("16", "80"),
}
CACHE_WRITE_FACTOR = Decimal("1.25")
MILLION = Decimal(1_000_000)


def price_of(model: str, settings: Settings | None = None, *, fast: bool = False) -> dict[str, Decimal] | None:
    """Dollars per million tokens for this model, or None when no price is known."""
    settings = settings or get_settings()
    if settings.ai_prices:
        try:
            own = json.loads(settings.ai_prices).get(model)
        except (ValueError, AttributeError):
            log.error("AI_PRICES is not valid JSON; using the built-in list prices")
            own = None
        if own and "input" in own and "output" in own:
            price_in, price_out = Decimal(str(own["input"])), Decimal(str(own["output"]))
            return {
                "input": price_in,
                "output": price_out,
                "cache_read": Decimal(str(own.get("cache_read", price_in / 10))),
                "cache_write": Decimal(str(own.get("cache_write", price_in * CACHE_WRITE_FACTOR))),
            }
    listed = LIST_PRICES.get(model)
    if listed is None:
        return None
    price_in, price_out, cache_read = (Decimal(v) for v in listed)
    if fast:
        if model not in FAST_PRICES:
            return None
        # The cached-token prices in fast mode are scaled like the input price.
        fast_in, fast_out = (Decimal(v) for v in FAST_PRICES[model])
        cache_read, price_in, price_out = cache_read * fast_in / price_in, fast_in, fast_out
    return {"input": price_in, "output": price_out, "cache_read": cache_read, "cache_write": price_in * CACHE_WRITE_FACTOR}


def cost_of(model: str, tokens: dict[str, int], settings: Settings | None = None, *, fast: bool = False) -> Decimal | None:
    price = price_of(model, settings, fast=fast)
    if price is None:
        return None
    total = sum(Decimal(int(tokens.get(kind) or 0)) * price[kind] for kind in ("input", "output", "cache_read", "cache_write"))
    return (total / MILLION).quantize(Decimal("0.000001"))


def prompt_hash(text: str) -> str:
    """A short fingerprint of a set of instructions: the same text always gives the same one."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------------- who a request is for

_attribution: ContextVar[dict[str, int]] = ContextVar("callagent_ai_attribution", default={})


@contextmanager
def attributed(*, organization_id: int | None = None, campaign_id: int | None = None, call_run_id: int | None = None) -> Iterator[None]:
    """Model requests made inside this block are recorded against these."""
    ids = {k: v for k, v in (("organization_id", organization_id), ("campaign_id", campaign_id), ("call_run_id", call_run_id)) if v is not None}
    token = _attribution.set({**_attribution.get(), **ids})
    try:
        yield
    finally:
        _attribution.reset(token)


def _tokens(usage: Any) -> dict[str, int]:
    return {
        "input": int(getattr(usage, "input_tokens", 0) or 0),
        "output": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_read": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        "cache_write": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    }


def record_usage(
    workflow: str,
    model: str,
    tokens: dict[str, int],
    *,
    outcome: str,
    duration_ms: int,
    attempts: int = 1,
    requests: int = 1,
    fingerprint: str | None = None,
    request_id: str | None = None,
    fast: bool = False,
    once_per_call: bool = False,
    settings: Settings | None = None,
) -> None:
    """Write one usage row, in a transaction of its own so it survives whatever the
    caller does next. Never raises: metering must not break the work it meters, but a
    row that could not be written is logged as an error, because it is money not counted."""
    from sqlalchemy import select

    from . import db

    from . import observability

    cost = cost_of(model, tokens, settings, fast=fast)
    observability.AI_REQUESTS.labels(workflow, outcome).inc(max(1, requests))
    observability.AI_DURATION.labels(workflow).observe(duration_ms / 1000)
    for kind in ("input", "output", "cache_read", "cache_write"):
        if tokens.get(kind):
            observability.AI_TOKENS.labels(workflow, kind).inc(tokens[kind])
    if cost:
        observability.AI_COST.labels(workflow).inc(float(cost))

    ids = _attribution.get()
    try:
        with db.session_scope() as s:
            organization_id = ids.get("organization_id")
            campaign_id = ids.get("campaign_id")
            call_run_id = ids.get("call_run_id")
            if campaign_id is None and call_run_id is not None:
                run = s.get(db.CallRun, call_run_id)
                campaign_id = run.campaign_id if run else None
            if organization_id is None and campaign_id is not None:
                campaign = s.get(db.Campaign, campaign_id)
                organization_id = campaign.organization_id if campaign else None
            if organization_id is None:
                organization_id = db.current_organization.get() or db.default_organization_id(s)
            if once_per_call and call_run_id is not None:
                seen = s.execute(select(db.AIUsage.id).where(db.AIUsage.call_run_id == call_run_id, db.AIUsage.workflow == workflow)).first()
                if seen is not None:
                    return
            s.add(
                db.AIUsage(
                    organization_id=organization_id, campaign_id=campaign_id, call_run_id=call_run_id, workflow=workflow, model=model,
                    prompt_hash=fingerprint, requests=requests, attempts=attempts, input_tokens=tokens.get("input", 0),
                    output_tokens=tokens.get("output", 0), cache_read_tokens=tokens.get("cache_read", 0),
                    cache_write_tokens=tokens.get("cache_write", 0), duration_ms=duration_ms, outcome=outcome,
                    cost_usd=cost, request_id=(request_id or None) and str(request_id)[:80],
                )
            )
            s.flush()
            from . import billing

            billing.record_ai(s, s.execute(select(db.AIUsage).order_by(db.AIUsage.id.desc()).limit(1)).scalar_one())
    except Exception:
        observability.AI_UNRECORDED.inc()
        log.exception("AI usage could not be recorded (workflow %s, model %s, %s tokens in, %s out)", workflow, model, tokens.get("input"), tokens.get("output"))


def record_live_call(model: str, tokens: dict[str, int], *, requests: int, duration_ms: int, fingerprint: str | None, fast: bool, settings: Settings | None = None) -> None:
    """The model use of one whole phone conversation, as counted by the voice pipeline.
    Written once per call, however many times the call's ending is processed."""
    if requests <= 0:
        return  # the model was never asked: nothing was used
    record_usage(LIVE_CALL, model, tokens, outcome="ok", duration_ms=duration_ms, requests=requests, fingerprint=fingerprint, fast=fast,
                 once_per_call=True, settings=settings)


def usage_summary(s: Any, organization_id: int, since: Any, campaign_id: int | None = None) -> dict[str, Any]:
    """What one organization's AI use added up to since a moment, by workflow and model."""
    from sqlalchemy import case, func, select

    from .db import AIUsage

    unpriced = func.sum(case((AIUsage.cost_usd.is_(None) & (AIUsage.outcome == "ok"), 1), else_=0))
    query = (
        select(
            AIUsage.workflow, AIUsage.model, func.count(), func.sum(AIUsage.requests), func.sum(AIUsage.input_tokens),
            func.sum(AIUsage.output_tokens), func.sum(AIUsage.cache_read_tokens), func.sum(AIUsage.cache_write_tokens),
            func.sum(AIUsage.cost_usd), unpriced, func.sum(case((AIUsage.outcome != "ok", 1), else_=0)),
        )
        .where(AIUsage.organization_id == organization_id, AIUsage.created_at >= since)
        .group_by(AIUsage.workflow, AIUsage.model)
        .order_by(AIUsage.workflow, AIUsage.model)
    )
    if campaign_id is not None:
        query = query.where(AIUsage.campaign_id == campaign_id)
    rows = [
        {
            "workflow": workflow, "model": model, "records": int(records), "requests": int(requests or 0), "input_tokens": int(tokens_in or 0),
            "output_tokens": int(tokens_out or 0), "cache_read_tokens": int(cache_read or 0), "cache_write_tokens": int(cache_write or 0),
            "cost_usd": str(cost or Decimal(0)), "records_without_price": int(no_price or 0), "not_ok": int(not_ok or 0),
        }
        for workflow, model, records, requests, tokens_in, tokens_out, cache_read, cache_write, cost, no_price, not_ok in s.execute(query)
    ]
    return {
        "since": since.isoformat(),
        "cost_usd": str(sum((Decimal(r["cost_usd"]) for r in rows), Decimal(0))),
        # True when every successful request had a price: otherwise the total is a floor, not the cost.
        "cost_complete": all(r["records_without_price"] == 0 for r in rows),
        "price_basis": "provider token counts at list prices (or AI_PRICES); not the provider's invoice",
        "by_workflow": rows,
    }


# ----------------------------------------------------------------------------- making a request

# The clock and the wait between attempts, in one place.
_now = time.monotonic
_sleep = time.sleep

_slots_lock = threading.Lock()
_slots: threading.BoundedSemaphore | None = None
_slots_size = 0


def _concurrency(settings: Settings) -> threading.BoundedSemaphore:
    global _slots, _slots_size
    size = max(1, settings.ai_background_concurrency)
    with _slots_lock:
        if _slots is None or _slots_size != size:
            _slots, _slots_size = threading.BoundedSemaphore(size), size
        return _slots


def _retry_after(error: Exception) -> float | None:
    try:
        value = error.response.headers.get("retry-after")  # type: ignore[attr-defined]
        return float(value) if value else None
    except (AttributeError, ValueError, TypeError):
        return None


def _run(workflow: str, model: str, system: str, client: Any, request: Callable[[Any], Any], settings: Settings) -> Any:
    """Make one background request under its workflow's policy and record what it used."""
    policy = WORKFLOWS.get(workflow)
    if policy is None:
        raise ValueError(f"'{workflow}' is not a known AI workflow; add it to llm.WORKFLOWS with a deadline")
    started = _now()
    deadline = started + policy.deadline_seconds
    fingerprint = prompt_hash(system)

    def record(outcome: str, attempts: int, response: Any = None) -> None:
        record_usage(
            workflow, model, _tokens(getattr(response, "usage", None)), outcome=outcome, attempts=attempts,
            duration_ms=int((_now() - started) * 1000), fingerprint=fingerprint,
            request_id=getattr(response, "_request_id", None), settings=settings,
        )

    slots = _concurrency(settings)
    # Waiting for a free slot may take at most half the time: the rest is for the request itself.
    if not slots.acquire(timeout=policy.deadline_seconds / 2):
        record("busy", 0)
        raise AITransientError(f"{workflow}: other AI work did not finish in time; this will be tried again")
    try:
        failure, outcome, attempt = None, "failed", 0
        while attempt < policy.attempts:
            remaining = deadline - _now()
            if remaining <= 1:
                outcome = "timeout"
                break
            attempt += 1
            try:
                # The library's own retries are switched off: retries are counted and timed here, against one deadline.
                response = request(client.with_options(timeout=remaining, max_retries=0))
            except anthropic.APITimeoutError as e:
                failure, outcome = e, "timeout"
            except anthropic.APIConnectionError as e:
                failure, outcome = e, "unreachable"
            except anthropic.RateLimitError as e:
                failure, outcome = e, "overloaded"
            except anthropic.APIStatusError as e:
                if e.status_code < 500 and e.status_code not in (408, 409):
                    record("failed", attempt)
                    raise AIError(f"{workflow}: the AI provider rejected the request ({e.status_code}): {e.message}") from e
                failure, outcome = e, "overloaded"
            else:
                if response.stop_reason == "refusal":
                    record("refused", attempt, response)
                    details = getattr(response, "stop_details", None)
                    raise AIError(f"Claude declined the request: {getattr(details, 'explanation', '') or details}")
                record("ok", attempt, response)
                return response
            if attempt < policy.attempts:
                wait = _retry_after(failure) or min(30.0, 2.0 ** attempt + random.uniform(0, 1))
                if wait >= deadline - _now() - 1:
                    break  # no time left for another attempt
                log.warning("%s: the AI provider is %s (attempt %s of %s); trying again in %.0f s", workflow, outcome, attempt, policy.attempts, wait)
                _sleep(wait)
        record(outcome, attempt)
        raise AITransientError(f"{workflow}: the AI provider was {outcome} after {attempt} attempt(s) ({type(failure).__name__ if failure else 'deadline reached'})") from failure
    finally:
        slots.release()


def make_client(settings: Settings | None = None) -> anthropic.Anthropic:
    settings = settings or get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def make_live_client(settings: Settings | None = None) -> anthropic.AsyncAnthropic:
    """The client of the live conversation. A caller is waiting on the line, so a
    model that goes quiet is given up after seconds, not the library's ten minutes."""
    settings = settings or get_settings()
    return anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key or "",
        timeout=anthropic.Timeout(60.0, read=settings.call_model_read_timeout_seconds, connect=settings.call_model_connect_timeout_seconds),
        max_retries=1,
    )


def _fallback_kwargs(settings: Settings) -> dict[str, Any]:
    """Server-side refusal fallbacks: if a safety classifier declines, the API
    re-runs the request on a fallback model inside the same call."""
    if not settings.enable_refusal_fallbacks:
        return {}
    return {"betas": [REFUSAL_FALLBACK_BETA], "fallbacks": "default"}


def parse_structured(
    client: anthropic.Anthropic,
    *,
    workflow: str,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    output_model: type[T],
    effort: str = "high",
    max_tokens: int = 16000,
    settings: Settings | None = None,
) -> T:
    """One structured-output request with adaptive thinking, under the workflow's policy."""
    settings = settings or get_settings()

    def request(bounded: Any) -> Any:
        return bounded.beta.messages.parse(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            output_format=output_model,
            **_fallback_kwargs(settings),
        )

    response = _run(workflow, model, system, client, request, settings)
    if response.parsed_output is None:
        # Usually the answer was cut off at max_tokens. Asking again costs the same and ends the same.
        raise AIError(f"{workflow}: no structured answer was returned (stop_reason={response.stop_reason})")
    return response.parsed_output


def text_completion(
    client: anthropic.Anthropic,
    *,
    workflow: str,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    effort: str = "medium",
    max_tokens: int = 4000,
    tools: list[dict[str, Any]] | None = None,
    settings: Settings | None = None,
) -> anthropic.types.beta.BetaMessage:
    """Plain (optionally tool-enabled) message request under the workflow's policy. Returns the full message."""
    settings = settings or get_settings()
    kwargs: dict[str, Any] = {}
    if tools:
        kwargs["tools"] = tools

    def request(bounded: Any) -> Any:
        return bounded.beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            **kwargs,
            **_fallback_kwargs(settings),
        )

    return _run(workflow, model, system, client, request, settings)


FAST_MODE_BETA = "fast-mode-2026-02-01"


def live_call_request_options(settings: Settings | None = None) -> dict[str, Any]:
    """Request options for the *live phone conversation*: tuned for time-to-first-word.

    Shared by the Pipecat LLM service (voice/llm_service.py) and `callagent chat`, so
    what you hear on the phone is what you test in the terminal.
    """
    settings = settings or get_settings()
    opts: dict[str, Any] = {
        "model": settings.call_model,
        "max_tokens": settings.call_max_tokens,
        "output_config": {"effort": settings.call_effort},
        "betas": [],
    }
    if settings.call_thinking == "disabled":
        opts["thinking"] = {"type": "disabled"}
    else:
        opts["thinking"] = {"type": "adaptive"}
    if settings.call_fast_mode:
        opts["speed"] = "fast"
        opts["betas"].append(FAST_MODE_BETA)
    return opts


def live_call_completion(
    client: anthropic.Anthropic,
    *,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    settings: Settings | None = None,
    workflow: str = "rehearsal",
) -> anthropic.types.beta.BetaMessage:
    """One agent turn with the same options the phone pipeline uses (see live_call_request_options).
    Used to rehearse a playbook in text; the phone conversation itself does not come through here."""
    settings = settings or get_settings()
    opts = live_call_request_options(settings)
    betas = opts.pop("betas")
    kwargs: dict[str, Any] = dict(opts)
    if betas:
        kwargs["betas"] = betas
    if tools:
        kwargs["tools"] = tools
    return _run(workflow, opts["model"], system, client, lambda bounded: bounded.beta.messages.create(system=system, messages=messages, **kwargs), settings)


def text_of(message: Any) -> str:
    return "".join(block.text for block in message.content if getattr(block, "type", "") == "text").strip()
