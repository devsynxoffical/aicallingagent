"""SQLAlchemy models and session helpers (SQLite by default)."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    create_engine,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from .config import get_settings

T = TypeVar("T")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TZDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every backend.

    SQLite has no timezone support and SQLAlchemy hands back naive datetimes from it,
    which then blow up when compared with aware ones. Store UTC-naive, load UTC-aware.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


DEFAULT_ORGANIZATION = "default"


class Organization(Base):
    """A customer company. Every piece of business data belongs to exactly one."""

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Filled for every row at startup (see bootstrap_organizations); nullable only so
    # databases created before organizations existed can be upgraded in place.
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    script_text: Mapped[str] = mapped_column(Text, default="")
    playbook_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|ready|running|paused|done
    max_concurrent_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Per-campaign calling rules (see dialer/rules.py). Empty = the global defaults.
    rules_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Why the dialer stopped this campaign on its own (budget reached, schedule ended, ...).
    stop_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # E.164 number whose incoming calls this campaign answers. None = outbound only.
    inbound_number: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow, onupdate=utcnow)

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
    next_attempt_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    last_disposition: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Higher is dialed first. 0 = normal.
    priority: Mapped[int] = mapped_column(Integer, default=0)
    # Qualification score 0-100 from evidence in the lead's calls; None until a call gave evidence.
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

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
    mode: Mapped[str] = mapped_column(String(20), default="live")  # live | voicemail | inbound
    direction: Mapped[str] = mapped_column(String(10), default="outbound")  # outbound | inbound
    # Playbook version this call ran, so results can be compared between versions.
    playbook_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Estimated cost of this call from the configured rates. None until the call ends.
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Language the prospect mostly spoke (base code), filled when the call ends.
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # True once the media stream reached our server (someone or something answered and we talked).
    stream_connected: Mapped[bool] = mapped_column(Boolean, default=False)
    # Random secret handed to Twilio in the TwiML <Parameter>; the /ws handshake must present it.
    stream_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
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
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)


class TrainingDocument(Base):
    """One uploaded file or website source a campaign's agent learns from."""

    __tablename__ = "training_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10), default="file")  # file | url
    filename: Mapped[str] = mapped_column(String(300), default="")
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    file_type: Mapped[str] = mapped_column(String(16), default="")  # pdf, docx, pptx, html, csv, txt, md, url
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    storage_path: Mapped[str | None] = mapped_column(String(600), nullable=True)
    # A re-upload of the same filename is a new version; older versions stay for audit.
    version: Mapped[int] = mapped_column(Integer, default=1)
    # pending | processing | ready | failed | superseded
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    max_pages: Mapped[int] = mapped_column(Integer, default=1)  # website sources: pages to read
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class KnowledgeChunk(Base):
    """A passage of a training document the agent can look up during a call."""

    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("training_documents.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    source_label: Mapped[str] = mapped_column(String(400), default="")  # "pricing.pdf, page 3"
    text: Mapped[str] = mapped_column(Text, default="")


class PlaybookVersion(Base):
    """History of a campaign's playbook. Calls only ever run the active version."""

    __tablename__ = "playbook_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    playbook_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | active | archived | rejected
    source: Mapped[str] = mapped_column(String(30), default="")  # onboard | import | training | api | rollback ...
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(200), default="")
    approved_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class TrainingRun(Base):
    """One "learn from the script and documents" job and what it produced."""

    __tablename__ = "training_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | done | failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)  # the draft it produced
    started_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class DoNotCall(Base):
    __tablename__ = "do_not_call"

    id: Mapped[int] = mapped_column(primary_key=True)
    phone_e164: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    reason: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)


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
        add_missing_columns(_engine)
        bootstrap_organizations(_engine)
    return _engine


def bootstrap_organizations(engine) -> int:
    """Make sure the default organization exists and that every campaign belongs to one.
    Until user accounts exist there is a single organization; data created before
    organizations were introduced is assigned to it. Returns the default organization's id."""
    with Session(engine) as s:
        org = s.execute(select(Organization).where(Organization.name == DEFAULT_ORGANIZATION)).scalar_one_or_none()
        if org is None:
            org = Organization(name=DEFAULT_ORGANIZATION)
            s.add(org)
            s.flush()
        s.execute(text("UPDATE campaigns SET organization_id = :org WHERE organization_id IS NULL"), {"org": org.id})
        s.commit()
        return org.id


def default_organization_id(s: Session) -> int:
    org = s.execute(select(Organization).where(Organization.name == DEFAULT_ORGANIZATION)).scalar_one_or_none()
    if org is None:
        org = Organization(name=DEFAULT_ORGANIZATION)
        s.add(org)
        s.flush()
    return org.id


def campaign_organization_id(s: Session, campaign: "Campaign") -> int:
    """The organization a campaign belongs to (assigning the default one to a campaign created without)."""
    if campaign.organization_id is None:
        campaign.organization_id = default_organization_id(s)
        s.flush()
    return campaign.organization_id


def add_missing_columns(engine) -> list[str]:
    """Bring a database created by an older version up to date.

    ``create_all`` creates missing tables but never alters existing ones, so columns
    added in later releases are added here. Additive only: nothing is dropped, renamed
    or retyped, which keeps it safe to run on every start. Returns what was added.
    """
    added: list[str] = []
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type.compile(engine.dialect)}'
                default = getattr(column.default, "arg", None)
                if isinstance(default, str):
                    ddl += " DEFAULT '" + default.replace("'", "''") + "'"
                elif isinstance(default, bool):
                    ddl += f" DEFAULT {'TRUE' if default else 'FALSE'}" if engine.dialect.name != "sqlite" else f" DEFAULT {int(default)}"
                elif isinstance(default, int):
                    ddl += f" DEFAULT {default}"
                conn.execute(text(ddl))
                added.append(f"{table.name}.{column.name}")
    return added


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
