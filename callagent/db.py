"""SQLAlchemy models and session helpers (SQLite by default)."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .config import get_settings

T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    script_text: Mapped[str] = mapped_column(Text, default="")
    playbook_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|ready|running|paused|done
    max_concurrent_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    leads: Mapped[list["Lead"]] = relationship(back_populates="campaign", cascade="all, delete-orphan")


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    phone_e164: Mapped[str] = mapped_column(String(32), index=True)
    raw_phone: Mapped[str] = mapped_column(String(64), default="")
    first_name: Mapped[str] = mapped_column(String(120), default="")
    last_name: Mapped[str] = mapped_column(String(120), default="")
    company: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # pending | queued | calling | completed | callback | failed | dnc
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_disposition: Mapped[str | None] = mapped_column(String(40), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    campaign: Mapped[Campaign] = relationship(back_populates="leads")
    calls: Mapped[list["CallRun"]] = relationship(back_populates="lead", cascade="all, delete-orphan")

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class CallRun(Base):
    __tablename__ = "call_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    twilio_call_sid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # created | initiated | ringing | in-progress | completed | busy | no-answer | failed | canceled
    status: Mapped[str] = mapped_column(String(20), default="created")
    answered_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    mode: Mapped[str] = mapped_column(String(20), default="live")  # live | voicemail
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transcript: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    tool_events: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    disposition: Mapped[str | None] = mapped_column(String(40), nullable=True)
    recording_url: Mapped[str | None] = mapped_column(String(400), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    lead: Mapped[Lead] = relationship(back_populates="calls")


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    call_run_id: Mapped[int | None] = mapped_column(ForeignKey("call_runs.id"), nullable=True)
    starts_at: Mapped[str] = mapped_column(String(64))  # ISO 8601 as spoken/confirmed
    timezone: Mapped[str] = mapped_column(String(64), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DoNotCall(Base):
    __tablename__ = "do_not_call"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_e164: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    reason: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------- engine

_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine(database_url: str | None = None):
    global _engine, _SessionLocal
    if _engine is None:
        url = database_url or get_settings().database_url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        _engine = create_engine(url, connect_args=connect_args, future=True)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
        Base.metadata.create_all(_engine)
    return _engine


def reset_engine_for_tests(database_url: str) -> None:
    global _engine, _SessionLocal
    _engine = None
    _SessionLocal = None
    get_engine(database_url)


@contextmanager
def session_scope():
    get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def run_db(fn: Callable[[Session], T]) -> T:
    """Run a blocking DB function in a worker thread so the audio loop never stalls."""

    def _runner() -> T:
        with session_scope() as s:
            return fn(s)

    return await asyncio.to_thread(_runner)


# --------------------------------------------------------------------------- helpers


def get_campaign_by_name(s: Session, name: str) -> Campaign | None:
    return s.execute(select(Campaign).where(Campaign.name == name)).scalar_one_or_none()


def is_dnc(s: Session, phone_e164: str) -> bool:
    return s.execute(select(DoNotCall).where(DoNotCall.phone_e164 == phone_e164)).scalar_one_or_none() is not None


def campaign_stats(s: Session, campaign_id: int) -> dict[str, int]:
    rows = s.execute(
        select(Lead.status, func.count()).where(Lead.campaign_id == campaign_id).group_by(Lead.status)
    ).all()
    stats = {status: count for status, count in rows}
    stats["total"] = sum(stats.values())
    disp = s.execute(
        select(CallRun.disposition, func.count())
        .where(CallRun.campaign_id == campaign_id, CallRun.disposition.is_not(None))
        .group_by(CallRun.disposition)
    ).all()
    stats.update({f"disposition:{d}": c for d, c in disp})
    return stats


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
