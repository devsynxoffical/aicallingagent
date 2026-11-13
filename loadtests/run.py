"""Reproducible load test of everything that can be measured without a telephony,
speech or model provider.

    python loadtests/run.py --database-url sqlite:////tmp/load.db
    python loadtests/run.py --database-url postgresql+psycopg://postgres@127.0.0.1:5432/callagent_load

The database is WIPED. Each scenario runs at 5, 10, 25 and 50 concurrent actors:

  claim     reserving leads for calls, every lead contested by two attempts at once
  webhooks  provider status callbacks through the real HTTP server, correctly signed,
            with the final status delivered twice
  api       the campaign report and the lead list
  jobs      the background job queue

What this does NOT measure: live calls. A call's cost is dominated by audio (voice
activity and turn detection on the CPU) and by provider round trips, none of which exist
here. The numbers below say how many calls' worth of bookkeeping the server and database
can carry, not how many calls one process can hold.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

LEVELS = (5, 10, 25, 50)
LEADS_PER_LEVEL = 400
TOKEN = "loadtest-api-token"
TWILIO_TOKEN = "loadtest-twilio-token"
PORT = 8020
BASE = f"http://127.0.0.1:{PORT}"


def summarize(latencies: list[float], seconds: float, errors: int = 0) -> dict:
    ordered = sorted(latencies)
    pick = lambda q: round(ordered[min(len(ordered) - 1, int(len(ordered) * q))] * 1000, 1) if ordered else None  # noqa: E731
    return {"requests": len(latencies), "errors": errors, "p50_ms": pick(0.50), "p95_ms": pick(0.95), "max_ms": pick(1.0),
            "mean_ms": round(statistics.mean(latencies) * 1000, 1) if latencies else None, "per_second": round(len(latencies) / seconds, 1) if seconds else None}


def wipe(url: str) -> None:
    from sqlalchemy import create_engine, text

    if url.startswith("sqlite"):
        path = Path(url.split("///", 1)[1])
        for leftover in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            leftover.unlink(missing_ok=True)
        return
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def seed() -> int:
    from callagent import db
    from callagent.training import versions
    from fixtures import make_sample_playbook

    with db.session_scope() as s:
        c = db.Campaign(name="load", status="paused")
        s.add(c)
        s.flush()
        versions.record_active(s, c, make_sample_playbook().model_copy(update={"open_questions": []}).model_dump(), source="loadtest")
        s.add_all([db.Lead(campaign_id=c.id, phone_e164=f"+1415{i:07d}", first_name=f"Lead {i}") for i in range(LEADS_PER_LEVEL * len(LEVELS))])
        return c.id


def scenario_claim(campaign_id: int, lead_ids: list[int], concurrency: int) -> dict:
    """Two attempts per lead, interleaved across threads. Exactly one may win."""
    from sqlalchemy import func, select

    from callagent import db
    from callagent.dialer.campaign_runner import LeadNotDialable, claim_lead_for_call

    work: Queue = Queue()
    for lead_id in lead_ids + lead_ids:
        work.put(lead_id)
    latencies, won, refused, errors = [], [], [], []
    lock = threading.Lock()

    def actor():
        while True:
            try:
                lead_id = work.get_nowait()
            except Empty:
                return
            started = time.perf_counter()
            try:
                with db.session_scope() as s:
                    claim_lead_for_call(s, campaign_id, lead_id)
                outcome = won
            except LeadNotDialable:
                outcome = refused
            except Exception as e:
                outcome = errors
                lead_id = repr(e)
            with lock:
                latencies.append(time.perf_counter() - started)
                outcome.append(lead_id)

    threads = [threading.Thread(target=actor) for _ in range(concurrency)]
    began = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - began
    with db.session_scope() as s:
        doubles = s.execute(select(func.count()).select_from(
            select(db.CallRun.lead_id).where(db.CallRun.lead_id.in_(lead_ids)).group_by(db.CallRun.lead_id).having(func.count() > 1).subquery())).scalar()
    return {**summarize(latencies, elapsed, len(errors)), "leads": len(lead_ids), "calls_opened": len(won), "refused": len(refused),
            "leads_with_two_calls": doubles, "first_error": errors[0] if errors else None}


async def scenario_webhooks(run_ids: list[int], concurrency: int) -> dict:
    import httpx
    from twilio.request_validator import RequestValidator

    validator = RequestValidator(TWILIO_TOKEN)
    gate = asyncio.Semaphore(concurrency)
    latencies, errors = [], []

    async def one_call(client: httpx.AsyncClient, run_id: int):
        url = f"{BASE}/twilio/status?call_run_id={run_id}"
        for status, duration in (("initiated", ""), ("ringing", ""), ("in-progress", ""), ("completed", "61"), ("completed", "61")):
            form = {"CallSid": f"CAload{run_id}", "CallStatus": status, "CallDuration": duration}
            headers = {"X-Twilio-Signature": validator.compute_signature(url, form)}
            async with gate:
                started = time.perf_counter()
                try:
                    response = await client.post(url, data=form, headers=headers)
                    if response.status_code != 200:
                        errors.append(response.status_code)
                except Exception as e:
                    errors.append(repr(e))
                latencies.append(time.perf_counter() - started)

    began = time.perf_counter()
    async with httpx.AsyncClient(timeout=60, limits=httpx.Limits(max_connections=concurrency)) as client:
        await asyncio.gather(*(one_call(client, run_id) for run_id in run_ids))
    elapsed = time.perf_counter() - began

    from sqlalchemy import func, select

    from callagent import db

    with db.session_scope() as s:
        completed = s.execute(select(func.count()).select_from(db.CallRun).where(db.CallRun.id.in_(run_ids), db.CallRun.status == "completed")).scalar()
        usage = s.execute(select(func.count()).select_from(db.UsageRecord).where(db.UsageRecord.call_run_id.in_(run_ids))).scalar()
    return {**summarize(latencies, elapsed, len(errors)), "calls": len(run_ids), "calls_completed": completed, "usage_rows": usage,
            "first_error": errors[0] if errors else None}


async def scenario_api(concurrency: int, requests_per_path: int = 60) -> dict:
    import httpx

    out = {}
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(timeout=120, headers=headers, limits=httpx.Limits(max_connections=concurrency)) as client:
        for name, path in (("report", "/api/campaigns/load/report"), ("leads_page", "/api/campaigns/load/leads?limit=25"), ("campaigns", "/api/campaigns")):
            gate = asyncio.Semaphore(concurrency)
            latencies, errors = [], []

            async def one():
                async with gate:
                    started = time.perf_counter()
                    try:
                        response = await client.get(BASE + path)
                        if response.status_code != 200:
                            errors.append(response.status_code)
                    except Exception as e:
                        errors.append(repr(e))
                    latencies.append(time.perf_counter() - started)

            began = time.perf_counter()
            await asyncio.gather(*(one() for _ in range(requests_per_path)))
            out[name] = {**summarize(latencies, time.perf_counter() - began, len(errors)), "first_error": errors[0] if errors else None}
    return out


async def scenario_jobs(concurrency: int, count: int = 500) -> dict:
    from callagent import db, jobs

    done = []

    @jobs.handler("loadtest")
    async def work(payload):
        done.append(payload["n"])

    def fill(s):
        for n in range(count):
            jobs.enqueue(s, "loadtest", f"loadtest:{concurrency}:{n}", {"n": n})

    await db.run_db(fill)
    workers = [jobs.Worker(name=f"load-{i}") for i in range(concurrency)]
    began = time.perf_counter()
    await asyncio.gather(*(w.run_once() for w in workers))
    elapsed = time.perf_counter() - began
    return {"jobs": count, "ran": len(done), "ran_twice": len(done) - len(set(done)), "seconds": round(elapsed, 2), "per_second": round(len(done) / elapsed, 1)}


def start_server(env: dict) -> subprocess.Popen:
    import httpx

    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "callagent.server:app", "--port", str(PORT), "--log-level", "warning"],
                               env=env, cwd=Path(__file__).resolve().parent.parent, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            if httpx.get(f"{BASE}/health", timeout=1).status_code == 200:
                return process
        except Exception:
            time.sleep(0.2)
    process.kill()
    raise RuntimeError("the server did not start")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    data_dir = tempfile.mkdtemp(prefix="callagent-load-")
    env = {**os.environ, "DATABASE_URL": args.database_url, "DATA_DIR": data_dir, "API_TOKEN": TOKEN, "TWILIO_AUTH_TOKEN": TWILIO_TOKEN,
           "PUBLIC_BASE_URL": BASE, "COST_PER_CONNECTED_MINUTE_USD": "0.10", "COST_PER_CALL_ATTEMPT_USD": "0.02", "ANTHROPIC_API_KEY": "",
           "DB_POOL_SIZE": "20", "DB_MAX_OVERFLOW": "40"}
    env.pop("TWILIO_ACCOUNT_SID", None)  # no provider account: reconciliation has nobody to ask and stays idle
    os.environ.update(env)
    os.environ.pop("TWILIO_ACCOUNT_SID", None)
    wipe(args.database_url)

    from sqlalchemy import select

    from callagent import db

    campaign_id = seed()
    with db.session_scope() as s:
        all_leads = [row[0] for row in s.execute(select(db.Lead.id).order_by(db.Lead.id))]
    results: dict = {"machine": {"cpus": os.cpu_count(), "platform": platform.platform(), "python": platform.python_version()},
                     "database": args.database_url.split(":", 1)[0], "levels": {}}

    for level in LEVELS:
        results["levels"][level] = {"jobs": asyncio.run(scenario_jobs(level))}

    slices = {level: all_leads[i * LEADS_PER_LEVEL:(i + 1) * LEADS_PER_LEVEL] for i, level in enumerate(LEVELS)}
    for level in LEVELS:
        results["levels"][level]["claim"] = scenario_claim(campaign_id, slices[level], level)

    server = start_server(env)
    try:
        for level in LEVELS:
            with db.session_scope() as s:
                run_ids = [row[0] for row in s.execute(select(db.CallRun.id).where(db.CallRun.lead_id.in_(slices[level])))]
            results["levels"][level]["webhooks"] = asyncio.run(scenario_webhooks(run_ids, level))
        for level in LEVELS:
            results["levels"][level]["api"] = asyncio.run(scenario_api(level))
    finally:
        server.terminate()
        server.wait(timeout=20)

    text = json.dumps(results, indent=2)
    if args.out:
        Path(args.out).write_text(text)
    print(text)


if __name__ == "__main__":
    main()
