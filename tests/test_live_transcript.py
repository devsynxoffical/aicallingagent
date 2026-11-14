"""The transcript is saved while a call is in progress, not only when it ends."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from callagent import db
from callagent.config import Settings, get_settings, resolve_api_token
from callagent.db import CallRun, session_scope
from callagent.voice.session import CallSession
from callagent.voice.transcript import TranscriptSaver, save_transcript


def make_session(sample_playbook, call_run_id=1):
    return CallSession(call_run_id=call_run_id, lead={}, campaign_id=1, playbook=sample_playbook, settings=Settings(anthropic_api_key="x"), twilio_call_sid="CA1")


@pytest.fixture
def call(tmp_path):
    """A call that is in progress."""
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'live.db'}")
    with session_scope() as s:
        campaign = db.Campaign(name="q4", status="running")
        s.add(campaign)
        s.flush()
        lead = db.Lead(campaign_id=campaign.id, phone_e164="+14155550100", status="calling")
        s.add(lead)
        s.flush()
        run = CallRun(lead_id=lead.id, campaign_id=campaign.id, status="in-progress", twilio_call_sid="CA1", stream_connected=True)
        s.add(run)
        s.flush()
        return run.id


def stored(call_run_id):
    with session_scope() as s:
        return [(line["role"], line["text"]) for line in s.get(CallRun, call_run_id).transcript or []]


async def test_each_line_reaches_the_database_while_the_call_is_still_going(call, sample_playbook):
    session = make_session(sample_playbook, call)
    saver = TranscriptSaver(call, session, min_interval=0.01)
    session.on_transcript = saver.notify

    session.add_transcript("assistant", "Hi, this is Jordan, an AI assistant from BrightBooks.")
    await asyncio.sleep(0.1)
    assert stored(call) == [("assistant", "Hi, this is Jordan, an AI assistant from BrightBooks.")]

    session.add_transcript("user", "Go ahead.")
    session.add_transcript("assistant", "   ")  # nothing was said: not a line, not a save
    await asyncio.sleep(0.1)
    assert stored(call) == [("assistant", "Hi, this is Jordan, an AI assistant from BrightBooks."), ("user", "Go ahead.")]
    with session_scope() as s:
        assert s.get(CallRun, call).status == "in-progress"  # saving a line changes nothing else about the call
    await saver.close()


async def test_lines_that_arrive_together_are_saved_together_and_saves_are_spaced(sample_playbook):
    saves = []

    async def save(call_run_id, lines):
        saves.append([line["text"] for line in lines])

    session = make_session(sample_playbook)
    saver = TranscriptSaver(1, session, min_interval=0.05, save=save)
    session.on_transcript = saver.notify
    for text in ("one", "two", "three"):
        session.add_transcript("user", text)
    await asyncio.sleep(0.02)
    assert saves == [["one", "two", "three"]]  # one write, not three

    session.add_transcript("user", "four")  # arrives while the saver is pausing between writes
    await asyncio.sleep(0.12)
    assert saves == [["one", "two", "three"], ["one", "two", "three", "four"]]
    await saver.close()


async def test_a_failed_save_never_interrupts_the_call_and_the_next_line_is_saved(sample_playbook):
    saves, fail = [], [True]

    async def save(call_run_id, lines):
        if fail[0]:
            raise RuntimeError("database is busy")
        saves.append(len(lines))

    session = make_session(sample_playbook)
    saver = TranscriptSaver(1, session, min_interval=0.01, save=save)
    session.on_transcript = saver.notify
    session.add_transcript("user", "hello")  # must not raise
    await asyncio.sleep(0.05)
    fail[0] = False
    session.add_transcript("user", "are you there")
    await asyncio.sleep(0.05)
    assert saves == [2]
    await saver.close()


async def test_the_last_lines_are_saved_when_the_call_ends_and_nothing_after_that(sample_playbook):
    saves = []

    async def save(call_run_id, lines):
        saves.append(len(lines))

    session = make_session(sample_playbook)
    saver = TranscriptSaver(1, session, min_interval=0.01, save=save)
    session.on_transcript = saver.notify
    session.add_transcript("user", "hello")
    await asyncio.sleep(0.005)
    session.add_transcript("assistant", "goodbye")  # said just before the line drops, during the pause between writes
    await saver.close()
    assert saves == [1, 2]
    session.add_transcript("user", "said after the end")
    await asyncio.sleep(0.05)
    assert saves == [1, 2]


async def test_a_late_partial_save_cannot_replace_the_complete_transcript(call):
    full = [{"role": "assistant", "text": "Hi."}, {"role": "user", "text": "Hello."}, {"role": "assistant", "text": "Goodbye."}]
    await save_transcript(call, full)
    await save_transcript(call, full[:1])  # a save that was already under way when the call ended
    assert stored(call) == [("assistant", "Hi."), ("user", "Hello."), ("assistant", "Goodbye.")]
    await save_transcript(999, full)  # a call that does not exist is ignored


def test_a_session_used_outside_a_call_does_not_need_an_event_loop(sample_playbook):
    session = make_session(sample_playbook)
    session.on_transcript = TranscriptSaver(1, session).notify
    session.add_transcript("user", "hello")  # no running loop: nothing to do, no error
    assert [line["text"] for line in session.transcript] == ["hello"]


def test_the_api_serves_what_has_been_said_so_far_and_marks_the_call_live(call, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    asyncio.run(save_transcript(call, [{"role": "assistant", "text": "Hi."}, {"role": "user", "text": "Who is this?"}]))
    from callagent.server import app

    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {resolve_api_token(get_settings())}"
        detail = client.get(f"/api/calls/{call}").json()
        assert detail["live"] is True and detail["status"] == "in-progress"
        assert [line["text"] for line in detail["transcript"]] == ["Hi.", "Who is this?"]

        with session_scope() as s:
            run = s.get(CallRun, call)
            run.status, run.ended_at = "completed", db.utcnow()
        assert client.get(f"/api/calls/{call}").json()["live"] is False
    get_settings.cache_clear()
