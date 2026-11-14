"""What an organization owes the people it calls: not calling those who asked not to
be called, removing what it holds about someone who asks, and not keeping call
content longer than it decided to.

These are mechanisms. Which of them the law requires, in which country, for which
kind of call, is a legal question this code does not answer: whether consent is
needed at all, which public do-not-call registers apply and how often they must be
re-read, how long a recording may be kept. The organization decides and configures;
the engine then enforces what was decided, the same way every time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import orgsettings
from .db import Appointment, CallRun, DoNotCall, Job, Lead, Organization, WebsiteRequest, add_dnc, utcnow
from .leads.importer import normalize_phone

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------- the do-not-call list


def import_do_not_call(s: Session, organization_id: int, numbers: Iterable[str], reason: str, default_region: str) -> dict[str, Any]:
    """Add many numbers at once: a public register, another system's suppression list.
    Numbers already listed are left as they are; what cannot be read as a phone number
    is counted and a few are returned, so a wrong file is noticed."""
    listed = set(s.execute(select(DoNotCall.phone_e164).where(DoNotCall.organization_id == organization_id)).scalars())
    added = already = 0
    invalid: list[str] = []
    for raw in numbers:
        text = str(raw or "").strip()
        if not text or text.lower() in ("phone", "number", "phone number", "phone_e164"):
            continue  # an empty line, or the sheet's header
        number = normalize_phone(text, default_region)
        if not number:
            invalid.append(text[:40])
        elif number in listed:
            already += 1
        else:
            add_dnc(s, number, organization_id, reason[:200])
            listed.add(number)
            added += 1
    # Anyone in a call list who is now suppressed is taken out of it at once, not at their next turn.
    if added:
        for lead in s.execute(select(Lead).where(Lead.organization_id == organization_id, Lead.status.in_(("pending", "callback", "queued")))).scalars():
            if lead.phone_e164 in listed:
                lead.status = "dnc"
    return {"added": added, "already_listed": already, "invalid": len(invalid), "samples_invalid": invalid[:5]}


def describe_entry(entry: DoNotCall) -> dict[str, Any]:
    return {"id": entry.id, "phone": entry.phone_e164, "reason": entry.reason, "added_at": entry.created_at.isoformat() if entry.created_at else None}


def list_do_not_call(s: Session, organization_id: int, q: str | None = None, limit: int = 100, before_id: int | None = None) -> dict[str, Any]:
    from sqlalchemy import func

    scope = [DoNotCall.organization_id == organization_id]
    total = int(s.execute(select(func.count()).select_from(DoNotCall).where(*scope)).scalar() or 0)
    if q and q.strip():
        scope.append(DoNotCall.phone_e164.contains("".join(ch for ch in q if ch.isdigit() or ch == "+")))
    if before_id is not None:
        scope.append(DoNotCall.id < before_id)
    entries = s.execute(select(DoNotCall).where(*scope).order_by(DoNotCall.id.desc()).limit(max(1, min(limit, 500)))).scalars()
    return {"total": total, "entries": [describe_entry(e) for e in entries]}


def remove_do_not_call(s: Session, organization_id: int, entry_id: int) -> DoNotCall | None:
    """Take a number off the list. Leads that were suppressed stay suppressed: putting
    someone back into a call list is a separate, deliberate act."""
    entry = s.get(DoNotCall, entry_id)
    if entry is None or entry.organization_id != organization_id:
        return None
    s.delete(entry)
    return entry


# ----------------------------------------------------------------------------- removing what is held


def erase_call_content(run: CallRun, now: datetime | None = None) -> bool:
    """Remove what was said on a call: the transcript, the tools' record, the review,
    the link to the recording. What stays is what accounting needs and says nothing
    about the person: when, how long, how it ended, what it cost."""
    if run.content_erased_at is not None:
        return False
    kept = {key: value for key, value in (run.summary or {}).items() if key in ("latency", "language")}
    run.transcript, run.tool_events, run.summary, run.recording_url = [], [], kept, None
    run.content_erased_at = now or utcnow()
    return True


def erase_person(s: Session, organization_id: int, phone: str, default_region: str, *, keep_on_do_not_call: bool = True) -> dict[str, Any]:
    """Remove what this organization holds about the person with this number.

    Their lead rows lose every personal detail and the number itself; their calls lose
    their content; messages they sent through the website are deleted; queued or
    stored notifications about them are emptied. Unless told otherwise the number goes
    on the do-not-call list, which is the one place it is kept, and has to be: it is
    how "never call me again" is honoured.

    Recordings are held by the phone provider, not here. Their addresses are returned
    once so that they can be deleted there; the engine cannot delete them.
    """
    number = normalize_phone(phone, default_region)
    if not number:
        raise ValueError(f"'{phone}' is not a phone number")
    now = utcnow()
    leads = list(s.execute(select(Lead).where(Lead.organization_id == organization_id, Lead.phone_e164 == number)).scalars())
    recordings: list[str] = []
    calls = appointments = notifications = 0
    for lead in leads:
        for run in s.execute(select(CallRun).where(CallRun.lead_id == lead.id)).scalars():
            if run.recording_url:
                recordings.append(run.recording_url)
            calls += erase_call_content(run, now)
            for job in s.execute(select(Job).where(Job.call_run_id == run.id)).scalars():
                if (job.payload or {}).get("body"):
                    job.payload = {"erased": True}
                    if job.status == "queued":
                        job.status = "done"  # there is nothing left to deliver
                    notifications += 1
        for appointment in s.execute(select(Appointment).where(Appointment.lead_id == lead.id)).scalars():
            appointment.notes = ""
            appointments += 1
        lead.phone_e164, lead.raw_phone = f"erased-{lead.id}", ""
        lead.first_name = lead.last_name = lead.company = lead.email = lead.notes = ""
        lead.extra, lead.score_details, lead.timezone = {}, {}, None
        lead.consent_basis, lead.consent_at, lead.consent_source = None, None, ""
        lead.status = "dnc"
    messages = list(s.execute(select(WebsiteRequest).where(WebsiteRequest.organization_id == organization_id, WebsiteRequest.phone == number)).scalars())
    for message in messages:
        s.delete(message)
    if keep_on_do_not_call:
        add_dnc(s, number, organization_id, "erased at the person's request")
    return {
        "leads": len(leads), "calls": calls, "appointments": appointments, "website_messages": len(messages), "notifications": notifications,
        "on_do_not_call_list": keep_on_do_not_call,
        # Held by the phone provider. Delete them there: the engine has only forgotten where they are.
        "recordings_to_delete_at_the_provider": recordings,
    }


def apply_retention(s: Session, now: datetime | None = None, batch: int = 500) -> dict[int, int]:
    """For every organization that set a retention period: remove the content of calls
    that ended longer ago than that. Returns how many calls were emptied, by organization."""
    now = now or utcnow()
    done: dict[int, int] = {}
    for org in s.execute(select(Organization)).scalars():
        days = orgsettings.of(org).retention_days
        if not days:
            continue
        due = s.execute(
            select(CallRun).where(CallRun.organization_id == org.id, CallRun.content_erased_at.is_(None), CallRun.ended_at.is_not(None),
                                  CallRun.ended_at < now - timedelta(days=days)).order_by(CallRun.id).limit(batch)
        ).scalars()
        emptied = sum(erase_call_content(run, now) for run in due)
        if emptied:
            done[org.id] = emptied
    if done:
        log.info("retention: call content removed for %s", done)
    return done
