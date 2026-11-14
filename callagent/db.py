"""SQLAlchemy models and session helpers (SQLite by default)."""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    create_engine,
    event,
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

# A call in one of these states may still be ringing or talking at the provider.
ACTIVE_CALL_STATUSES = ("created", "queued", "initiated", "ringing", "in-progress")
TERMINAL_CALL_STATUSES = ("completed", "busy", "no-answer", "failed", "canceled")
_ACTIVE_SQL = "status IN ('created', 'queued', 'initiated', 'ringing', 'in-progress')"


class Organization(Base):
    """A customer company. Every piece of business data belongs to exactly one."""

    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    # What is this organization's own: the numbers it calls from, where a call is handed
    # to a person, where bookings are reported, its rates. See callagent/orgsettings.py.
    settings_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=dict)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The customer this campaign belongs to. Set by the route that creates it; a campaign
    # created without one (command line, older code) gets the default organization.
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))  # unique within its organization
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

    __table_args__ = (
        # One number is answered by one campaign.
        Index("uq_campaigns_inbound_number", "inbound_number", unique=True),
        # Two customers may each have a campaign called "Q4"; one customer may not have two.
        Index("uq_campaigns_org_name", "organization_id", "name", unique=True),
        CheckConstraint("status IN ('draft', 'ready', 'running', 'paused', 'done')", name="ck_campaigns_status"),
    )


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Always the campaign's organization; kept on the row so that organization-wide
    # queries and database policies do not need a join. Filled when the row is created.
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
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

    __table_args__ = (
        # A phone number is one lead per campaign, however it arrived (sheet, API, inbound call).
        Index("uq_leads_campaign_phone", "campaign_id", "phone_e164", unique=True),
        # The dialer's "who is due next" query.
        Index("ix_leads_due", "campaign_id", "status", "next_attempt_at"),
        # "Is this person on a call from another campaign of ours right now", asked before every dial.
        Index("ix_leads_org_phone", "organization_id", "phone_e164"),
        CheckConstraint("status IN ('pending', 'queued', 'calling', 'completed', 'callback', 'failed', 'dnc')", name="ck_leads_status"),
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class CallRun(Base):
    __tablename__ = "call_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)  # the campaign's; see Lead
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    twilio_call_sid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # created | initiated | ringing | in-progress | completed | busy | no-answer | failed | canceled
    status: Mapped[str] = mapped_column(String(20), default="created")
    # Who said so: "provider" (the telephony provider reported it) or "system" (we concluded it).
    status_source: Mapped[str | None] = mapped_column(String(12), nullable=True)
    answered_by: Mapped[str | None] = mapped_column(String(40), nullable=True)
    mode: Mapped[str] = mapped_column(String(20), default="live")  # live | voicemail | inbound
    direction: Mapped[str] = mapped_column(String(10), default="outbound")  # outbound | inbound
    # Set when the call was part of an A/B test: which test, and which side ("control" | "variant").
    experiment_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    experiment_arm: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Playbook version this call ran, so results can be compared between versions.
    playbook_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The number this call was placed from (one of its organization's), kept so that a
    # text, a transfer and any question to the provider about this call use the same one.
    from_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # What the conversation actually ran on: the model, and a fingerprint of the instructions it was given.
    ai_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String(16), nullable=True)
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

    __table_args__ = (
        # A provider call is one record, even when its webhook is delivered twice at once.
        Index("uq_call_runs_twilio_call_sid", "twilio_call_sid", unique=True),
        # A person is never rung twice at the same time: at most one unfinished outbound call per lead.
        Index(
            "uq_call_runs_one_active_outbound_per_lead",
            "lead_id",
            unique=True,
            sqlite_where=text(f"direction = 'outbound' AND {_ACTIVE_SQL}"),
            postgresql_where=text(f"direction = 'outbound' AND {_ACTIVE_SQL}"),
        ),
        Index("ix_call_runs_campaign_status", "campaign_id", "status"),
        Index("ix_call_runs_lead_latest", "lead_id", "id"),
        # Daily and monthly limits (every dial tick) and the calls-per-day chart.
        Index("ix_call_runs_campaign_started", "campaign_id", "started_at"),
        Index("ix_call_runs_org_started", "organization_id", "started_at"),
        # The reconciler's scan for calls that may still be live: a small index over the few active rows.
        Index("ix_call_runs_active", "started_at", sqlite_where=text(_ACTIVE_SQL), postgresql_where=text(_ACTIVE_SQL)),
        CheckConstraint(
            "status IN ('created', 'queued', 'initiated', 'ringing', 'in-progress', 'completed', 'busy', 'no-answer', 'failed', 'canceled')",
            name="ck_call_runs_status",
        ),
    )


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)  # the lead's
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), index=True)
    call_run_id: Mapped[int | None] = mapped_column(ForeignKey("call_runs.id"), nullable=True)
    starts_at: Mapped[str] = mapped_column(String(64))  # ISO 8601 as spoken/confirmed
    timezone: Mapped[str] = mapped_column(String(64), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    __table_args__ = (
        # Booking the same time twice on one call is one meeting.
        Index("uq_appointments_call_start", "call_run_id", "starts_at", unique=True),
    )


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
    warning: Mapped[str | None] = mapped_column(Text, nullable=True)  # read, but something was left out (and what)
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
    # Coach proposals: [{"change": ..., "evidence": ...}], so each recommendation can be shown with its evidence.
    changes_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(200), default="")
    approved_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)

    __table_args__ = (
        Index("uq_playbook_versions_campaign_version", "campaign_id", "version", unique=True),
        # Calls run exactly one version.
        Index(
            "uq_playbook_versions_one_active",
            "campaign_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )


class Experiment(Base):
    """A controlled A/B test of two playbook versions on one campaign."""

    __tablename__ = "experiments"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | stopped | promoted
    control_version: Mapped[int] = mapped_column(Integer)
    variant_version: Mapped[int] = mapped_column(Integer)
    variant_share: Mapped[float] = mapped_column(Float, default=0.5)  # share of eligible leads that get the variant
    min_answered_per_arm: Mapped[int] = mapped_column(Integer, default=50)
    created_by: Mapped[str] = mapped_column(String(200), default="")
    ended_by: Mapped[str] = mapped_column(String(200), default="")
    started_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)

    __table_args__ = (
        # Two tests can never share a campaign's leads.
        Index(
            "uq_experiments_one_running",
            "campaign_id",
            unique=True,
            sqlite_where=text("status = 'running'"),
            postgresql_where=text("status = 'running'"),
        ),
    )


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


class UsageRecord(Base):
    """The usage ledger: what was used, by whom, and what it comes to. A bill, a
    budget and a spend figure are all sums over these lines.

    A line is never changed or removed once written. A correction is a new line that
    points at the one it corrects (``adjusts_id``) and holds the difference, so the
    history of every figure can be read back. ``key`` names what a line is for, and
    is unique: writing the same fact twice (a repeated provider report, a retried
    job) finds the existing line.

    ``basis`` says how far the amount can be trusted: "estimate" is measured usage
    at a configured or list price; "provider" is the provider's own figure.
    """

    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True, index=True)
    call_run_id: Mapped[int | None] = mapped_column(ForeignKey("call_runs.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="call")  # call (telephony and speech, by time) | ai_model
    key: Mapped[str] = mapped_column(String(160))
    quantity_seconds: Mapped[int] = mapped_column(Integer, default=0)  # call lines: the seconds billed
    quantity: Mapped[Any] = mapped_column(Numeric(18, 4), default=0)
    unit: Mapped[str] = mapped_column(String(16), default="minute")  # minute | token
    amount_usd: Mapped[Any] = mapped_column(Numeric(12, 4), default=0)  # exact decimal, not floating point; negative on a downward correction
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    basis: Mapped[str] = mapped_column(String(12), default="estimate")  # estimate | provider | manual
    adjusts_id: Mapped[int | None] = mapped_column(ForeignKey("usage_records.id"), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # how the amount was arrived at: rates, counts, source
    note: Mapped[str] = mapped_column(String(300), default="")
    created_by: Mapped[str] = mapped_column(String(254), default="")  # a person, for a manual correction
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    __table_args__ = (
        Index("uq_usage_records_key", "key", unique=True),
        CheckConstraint("basis IN ('estimate', 'provider', 'manual')", name="ck_usage_records_basis"),
    )


class ApiKey(Base):
    """A key an organization's own systems use to call the API. It belongs to one
    organization and carries one role. Only a hash is stored: the key itself is shown
    once, when it is created."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(12), default="viewer")
    prefix: Mapped[str] = mapped_column(String(16))  # the first characters, to tell keys apart in a list
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[str] = mapped_column(String(254), default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)

    __table_args__ = (CheckConstraint("role IN ('admin', 'operator', 'viewer')", name="ck_api_keys_role"),)


class AuditEntry(Base):
    """Who changed what, when and from where. One row per attempt to change something
    through the API (successful or refused) and per sign-in attempt on a known account.
    Rows are only ever added: no code updates or deletes them.

    The request body is never stored: it can hold passwords and customer data. What
    is stored is which operation, on which object, and how it ended.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    actor: Mapped[str] = mapped_column(String(254))  # the person's email, or "api token"
    actor_role: Mapped[str] = mapped_column(String(12), default="")
    action: Mapped[str] = mapped_column(String(80))  # the operation's name, e.g. start_campaign, sign_in
    method: Mapped[str] = mapped_column(String(8), default="")
    route: Mapped[str] = mapped_column(String(200), default="")  # the route as declared, without values
    target: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # what it was aimed at: the route's own parameters
    status: Mapped[int] = mapped_column(Integer, default=0)  # how it ended: the HTTP status
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    address: Mapped[str | None] = mapped_column(String(64), nullable=True)  # where the request came from
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    __table_args__ = (Index("ix_audit_log_org_created", "organization_id", "created_at"),)


AI_OUTCOMES = ("ok", "refused", "overloaded", "timeout", "unreachable", "busy", "failed")


class AIUsage(Base):
    """One row per use of the AI model: what it was for, which model and prompt, how many
    tokens it used, how long it took and how it ended. Token counts are the provider's
    own figures from its answer; the cost is those tokens at the price table in force,
    and stays empty when no price is known for the model (unknown, not free).

    A background request (a review, a coach run, a training run) is one row. A live
    call is one row for the whole conversation, with ``requests`` counting its turns.
    """

    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True, index=True)
    call_run_id: Mapped[int | None] = mapped_column(ForeignKey("call_runs.id"), nullable=True, index=True)
    workflow: Mapped[str] = mapped_column(String(40))  # call_review | coach | playbook_analysis | localization | rehearsal | live_call
    model: Mapped[str] = mapped_column(String(80))
    prompt_hash: Mapped[str | None] = mapped_column(String(16), nullable=True)  # which instructions were in force
    requests: Mapped[int] = mapped_column(Integer, default=1)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)  # not counting the cached ones below
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    outcome: Mapped[str] = mapped_column(String(16), default="ok")
    cost_usd: Mapped[Any] = mapped_column(Numeric(12, 6), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(80), nullable=True)  # the provider's id, for a support case
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint("outcome IN ('ok','refused','overloaded','timeout','unreachable','busy','failed')", name="ck_ai_usage_outcome"),
        Index("ix_ai_usage_org_created", "organization_id", "created_at"),
    )


class User(Base):
    """A person who signs in to the console. What they may do is decided by their role."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)  # stored lower-case
    name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(12), default="viewer")  # admin | operator | viewer
    password_hash: Mapped[str] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class UserSession(Base):
    """One sign-in. Only a hash of the session token is stored, so the table cannot be used to sign in."""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class WebsiteRequest(Base):
    """What someone sent through a form on the website: a demo or trial request, a
    message, a newsletter sign-up or a request to be called back."""

    __tablename__ = "website_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)  # demo | trial | contact | newsletter | callback
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    company: Mapped[str | None] = mapped_column(String(160), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    team_size: Mapped[str | None] = mapped_column(String(16), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="new", index=True)  # new | handled
    handled_by: Mapped[str | None] = mapped_column(String(254), nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)


class Job(Base):
    """A unit of background work that must not be lost: it survives restarts, is retried
    with backoff, and ends either done or dead (kept, with its error, for a person to see).

    ``key`` makes enqueueing idempotent: asking twice for the same work is one job. A job
    is written in the same transaction as the change that causes it, so the change and
    its follow-up are saved together or not at all (a transactional outbox).
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Whose work this is. None only for the deployment's own housekeeping.
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(60), index=True)
    key: Mapped[str] = mapped_column(String(200))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="queued")  # queued | running | done | dead
    # Which pool of worker slots runs it (see jobs.QUEUES): work that a call depends on
    # never waits behind analysis or document reading. Within a queue, higher priority first.
    queue: Mapped[str] = mapped_column(String(20), default="default")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    # The id of the request that caused this work, so its log lines can be found from the request's.
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=300)
    run_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)  # not before this moment
    # While running: who has it and until when. A worker that dies simply stops renewing,
    # and the job is taken over once the lease has passed.
    locked_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # For tracing: the call this work belongs to, when it belongs to one.
    call_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)

    __table_args__ = (
        Index("uq_jobs_key", "key", unique=True),
        Index("ix_jobs_due", "status", "run_at"),
        Index("ix_jobs_queue_due", "queue", "status", "run_at"),
        CheckConstraint("status IN ('queued', 'running', 'done', 'dead')", name="ck_jobs_status"),
    )


class Event(Base):
    """Something that happened, recorded in the same transaction as the change itself
    (a transactional outbox). Events are facts for tracing and for consumers; the
    tables remain the source of truth for current state.

    ``key`` makes recording idempotent: the same fact reported twice (a repeated
    webhook, a retried job) is one event. Consumers are jobs, one per subscriber, so
    each has its own retries and dead letter and a failure in one never holds up another.
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(60), index=True)  # CallAnswered, PlaybookApproved, ...
    version: Mapped[int] = mapped_column(Integer, default=1)  # of this event type's payload
    subject: Mapped[str] = mapped_column(String(30))  # call | campaign | appointment | document | playbook | usage
    subject_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    campaign_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # The correlation id of everything about one call: its events, jobs, usage and AI requests carry it.
    call_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    key: Mapped[str] = mapped_column(String(200))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    __table_args__ = (Index("uq_events_key", "key", unique=True),)


class Lease(Base):
    """Who currently holds a job that only one process may do (running the dial loops,
    asking the provider about quiet calls). A holder renews before ``expires_at``; when
    it stops renewing, another process takes over after that moment."""

    __tablename__ = "leases"

    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    holder: Mapped[str] = mapped_column(String(120))
    expires_at: Mapped[datetime] = mapped_column(TZDateTime)
    acquired_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)


class JobAttempt(Base):
    """History: one row per time a job was run, with how it ended."""

    __tablename__ = "job_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    worker: Mapped[str] = mapped_column(String(80), default="")
    outcome: Mapped[str] = mapped_column(String(12), default="running")  # running | ok | error | timeout | abandoned
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)


class DoNotCall(Base):
    """Numbers an organization must never call. An opt-out is owed to the business that
    was asked, so each organization has its own list: one customer's opt-outs are neither
    visible to nor binding on another."""

    __tablename__ = "do_not_call"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    phone_e164: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)

    __table_args__ = (Index("uq_do_not_call_org_phone", "organization_id", "phone_e164", unique=True),)


# --------------------------------------------------------------------------- engine

_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def _engine_options(url: str) -> dict[str, Any]:
    """Connection settings. SQLite is one file with one writer; a server database gets a
    bounded pool whose connections are checked before use."""
    settings = get_settings()
    if url.startswith("sqlite"):
        return {"connect_args": {"check_same_thread": False, "timeout": settings.sqlite_busy_timeout_seconds}}
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout_seconds,
        "pool_recycle": settings.db_pool_recycle_seconds,
        "pool_pre_ping": True,  # a connection the server dropped is replaced, not handed to a request
    }


def _configure_sqlite(engine) -> None:
    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _record):
        # Python's sqlite3 module opens a transaction by itself, but only before an
        # INSERT, UPDATE or DELETE, not before SAVEPOINT. A transaction whose first write
        # was inside a savepoint therefore had no outer transaction, and releasing that
        # savepoint committed it: the work survived even when the caller then rolled
        # back. Jobs and events are written inside savepoints, so "saved together with
        # the change or not at all" did not hold. The driver's own handling is switched
        # off here and the transaction is begun explicitly (see _begin_before_first_write).
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")  # SQLite ignores foreign keys unless asked
        cursor.execute("PRAGMA journal_mode=WAL")  # readers do not block the writer (no effect on :memory:)
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    @event.listens_for(engine, "before_cursor_execute")
    def _begin_before_first_write(_connection, cursor, statement, _parameters, _context, _executemany):
        # Begun at the first statement that is not a read, savepoints included. Reads before
        # it run outside a transaction, as they always did here: a transaction that began
        # with a read cannot become a writer once another writer has committed, which
        # under concurrency fails at once with "database is locked".
        if not cursor.connection.in_transaction and not statement.lstrip().upper().startswith(_SQLITE_NO_BEGIN):
            cursor.execute("BEGIN")


# Statements that read, or that manage the transaction themselves.
_SQLITE_NO_BEGIN = ("SELECT", "PRAGMA", "BEGIN", "COMMIT", "ROLLBACK", "EXPLAIN", "WITH")


def get_engine(database_url: str | None = None, *, migrate: bool | None = None):
    """The engine, created on first use. ``migrate`` overrides AUTO_MIGRATE (the
    `callagent db upgrade` command always migrates)."""
    global _engine, _SessionLocal
    if _engine is None:
        from .migrate import require_current, upgrade

        settings = get_settings()
        url = database_url or settings.database_url
        engine = create_engine(url, future=True, **_engine_options(url))
        if url.startswith("sqlite"):
            _configure_sqlite(engine)
        if settings.auto_migrate if migrate is None else migrate:
            upgrade(engine)
        else:
            require_current(engine)
        bootstrap_organizations(engine)
        _SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
        _engine = engine
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


# The organization of the request being served, set once the caller is authenticated.
# `run_db` runs in a worker thread that inherits it. None outside a request (webhooks,
# jobs, the dial loop, the command line): those paths work from row ids, never from names.
current_organization: ContextVar[int | None] = ContextVar("callagent_organization", default=None)


@event.listens_for(Session, "before_flush")
def _fill_organization(session: Session, _flush_context, _instances) -> None:
    """Give every new customer-owned row its organization, in one place.

    A lead, call, appointment or usage record always belongs to the organization of
    the campaign (or lead) it hangs from. Code that creates one does not have to
    remember to copy it, and cannot copy the wrong one. A campaign created without an
    organization belongs to the default one.
    """
    pending = [o for o in session.new if isinstance(o, (Campaign, Lead, CallRun, Appointment, UsageRecord)) and o.organization_id is None]
    if not pending:
        return
    pending.sort(key=lambda o: not isinstance(o, Campaign))  # campaigns first: their leads may be in the same flush
    with session.no_autoflush:
        default: int | None = None
        campaigns: dict[int, int | None] = {}

        def of_campaign(campaign_id: int | None) -> int | None:
            if campaign_id is None:
                return None
            if campaign_id not in campaigns:
                campaigns[campaign_id] = session.execute(select(Campaign.organization_id).where(Campaign.id == campaign_id)).scalar_one_or_none()
            return campaigns[campaign_id]

        for o in pending:
            if isinstance(o, Campaign):
                default = default if default is not None else _default_organization_id_in_flush(session)
                o.organization_id = default
            elif isinstance(o, Appointment):
                o.organization_id = session.execute(select(Lead.organization_id).where(Lead.id == o.lead_id)).scalar_one_or_none()
            elif isinstance(o, Lead) and o.campaign_id is None and o.campaign is not None:
                o.organization_id = o.campaign.organization_id
            else:
                o.organization_id = of_campaign(o.campaign_id)


def _default_organization_id_in_flush(session: Session) -> int:
    """The default organization's id from inside a flush, where the session cannot flush again."""
    query = select(Organization.id).where(Organization.name == DEFAULT_ORGANIZATION)
    found = session.execute(query).scalar_one_or_none()
    if found is None:
        session.execute(Organization.__table__.insert().values(name=DEFAULT_ORGANIZATION, created_at=utcnow().replace(tzinfo=None)))
        found = session.execute(query).scalar_one()
    return found


_default_organization: int | None = None  # the default organization's id never changes once it exists; forgotten when the engine is replaced


def default_organization_id(s: Session) -> int:
    global _default_organization
    org = s.execute(select(Organization).where(Organization.name == DEFAULT_ORGANIZATION)).scalar_one_or_none()
    if org is None:
        org = Organization(name=DEFAULT_ORGANIZATION)
        s.add(org)
        s.flush()
        return org.id  # not remembered until it is committed and read back
    _default_organization = org.id
    return org.id


def known_default_organization() -> int | None:
    """The default organization's id if this process has already read it, without a
    database round trip. Every request made with the engine's API token needs it."""
    return _default_organization


def campaign_organization_id(s: Session, campaign: "Campaign") -> int:
    """The organization a campaign belongs to (assigning the default one to a campaign created without)."""
    if campaign.organization_id is None:
        campaign.organization_id = default_organization_id(s)
        s.flush()
    return campaign.organization_id


def add_missing_columns(conn) -> list[str]:
    """Adopt a database created before migrations existed.

    ``create_all`` creates missing tables but never alters existing ones, so columns
    added by releases before the first migration are added here. Additive only. This
    runs once, when a database without a migration record is first seen (see
    ``migrate.upgrade``); every later schema change is a numbered migration.
    """
    added: list[str] = []
    inspector = inspect(conn)
    tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {column.type.compile(conn.dialect)}'
            default = getattr(column.default, "arg", None)
            if isinstance(default, str):
                ddl += " DEFAULT '" + default.replace("'", "''") + "'"
            elif isinstance(default, bool):
                ddl += f" DEFAULT {'TRUE' if default else 'FALSE'}" if conn.dialect.name != "sqlite" else f" DEFAULT {int(default)}"
            elif isinstance(default, int):
                ddl += f" DEFAULT {default}"
            conn.execute(text(ddl))
            added.append(f"{table.name}.{column.name}")
    return added


def reset_engine_for_tests(database_url: str) -> None:
    global _engine, _SessionLocal, _default_organization
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
    _default_organization = None
    get_engine(database_url)


@contextmanager
def session_scope():
    engine = get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        organization = current_organization.get()
        if organization is not None and engine.dialect.name == "postgresql":
            # Row-level security (migration 0008) reads this: for the length of this
            # transaction the database itself returns only this organization's rows.
            session.execute(text("SELECT set_config('app.organization_id', :org, true)"), {"org": str(int(organization))})
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


_executors: dict[str, ThreadPoolExecutor] = {}
_executors_lock = threading.Lock()


def executor(kind: str) -> ThreadPoolExecutor:
    """One of the two thread pools. "db": database work on the request and call path,
    which has deadlines (a provider webhook must be answered within seconds, a caller
    is waiting for a tool). "background": anything that can take long: model calls,
    website crawls, document parsing, imports. They are separate so that the second
    kind, however backed up, never takes a thread the first kind needs."""
    with _executors_lock:
        if kind not in _executors:
            settings = get_settings()
            size = settings.db_threads if kind == "db" else settings.background_threads
            _executors[kind] = ThreadPoolExecutor(max_workers=max(1, size), thread_name_prefix=f"callagent-{kind}")
        return _executors[kind]


def reset_executors_for_tests() -> None:
    with _executors_lock:
        for pool in _executors.values():
            pool.shutdown(wait=False, cancel_futures=True)
        _executors.clear()


async def run_blocking(fn: Callable[..., T], *args: Any) -> T:
    """Run slow blocking work (a model call, a crawl, parsing a file) on the background
    pool, keeping the caller's context."""
    context = contextvars.copy_context()
    return await asyncio.get_running_loop().run_in_executor(executor("background"), lambda: context.run(fn, *args))


async def run_db(fn: Callable[[Session], T], *, unrestricted: bool = False, slow: bool = False) -> T:
    """Run a blocking DB function in a worker thread so the audio loop never stalls.

    The thread inherits the request's organization, which confines the transaction to
    it. ``unrestricted`` is for the few operations that are about the deployment as a
    whole (managing organizations): it runs without an organization, and must only be
    used on routes reserved for the deployment's operator. ``slow`` marks work that
    may take long (it reads a website, parses a sheet, calls the AI model): it runs on
    the background pool, so it cannot hold up calls and webhooks."""

    def _runner() -> T:
        if unrestricted:
            current_organization.set(None)  # in this thread's copy of the context only
        with session_scope() as s:
            return fn(s)

    context = contextvars.copy_context()
    return await asyncio.get_running_loop().run_in_executor(executor("background" if slow else "db"), lambda: context.run(_runner))


# --------------------------------------------------------------------------- helpers


def get_campaign_by_name(s: Session, name: str, organization_id: int | None = None) -> Campaign | None:
    """A campaign of one organization. Without an organization: the default one, which
    is what the command line and a single-customer deployment mean."""
    org = organization_id if organization_id is not None else default_organization_id(s)
    return s.execute(select(Campaign).where(Campaign.organization_id == org, Campaign.name == name)).scalar_one_or_none()


def is_dnc(s: Session, phone_e164: str, organization_id: int) -> bool:
    """Whether this organization must not call this number."""
    return s.execute(select(DoNotCall.id).where(DoNotCall.organization_id == organization_id, DoNotCall.phone_e164 == phone_e164)).first() is not None


def add_dnc(s: Session, phone_e164: str, organization_id: int, reason: str = "") -> bool:
    """Put a number on an organization's do-not-call list. Returns False if it was already there."""
    if is_dnc(s, phone_e164, organization_id):
        return False
    s.add(DoNotCall(organization_id=organization_id, phone_e164=phone_e164, reason=reason[:200]))
    s.flush()
    return True


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


# Every table whose rows belong to one organization. Row-level security covers exactly these.
TENANT_TABLES = (
    "campaigns", "leads", "call_runs", "appointments", "do_not_call", "usage_records", "training_documents", "knowledge_chunks",
    "playbook_versions", "experiments", "training_runs", "users", "website_requests", "jobs", "events", "ai_usage", "audit_log", "api_keys",
)
# Rows of these may belong to no organization (the deployment's own housekeeping and facts).
_SHARED_WHEN_UNOWNED = ("jobs", "events")


def ensure_row_level_security(connection) -> None:
    """Create (or recreate) the isolation policy on every tenant table. PostgreSQL only.

    With `app.organization_id` set for the transaction, a row is visible and writable
    only if its organization matches. With it unset nothing is restricted: provider
    webhooks, the dial loop, background jobs and migrations serve every organization
    and address rows by id. The policies bind the table owner too (FORCE).

    Called by migration 0008 for existing databases and when a new database is
    created, so both end up the same.
    """
    if connection.dialect.name != "postgresql":
        return
    setting = "NULLIF(current_setting('app.organization_id', true), '')"
    existing = set(inspect(connection).get_table_names())
    for table in TENANT_TABLES:
        if table not in existing:
            continue  # created by a later migration, which calls this again
        # A job may belong to no organization (housekeeping); such rows stay visible to the deployment's operator.
        own = f"organization_id = {setting}::int" + (" OR organization_id IS NULL" if table in _SHARED_WHEN_UNOWNED else "")
        rule = f"({setting} IS NULL OR {own})"
        connection.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        connection.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
        connection.execute(text(f"DROP POLICY IF EXISTS organization_isolation ON {table}"))
        connection.execute(text(f"CREATE POLICY organization_isolation ON {table} USING {rule} WITH CHECK {rule}"))


def row_level_security(engine) -> str:
    """Whether the database itself confines a request to its organization.

    "enforced": PostgreSQL, policies in place, and this database role is bound by them.
    "bypassed": the policies exist but the role is a superuser or has BYPASSRLS, so
    only the application's own filters protect the data. "unavailable": SQLite.
    """
    if engine.dialect.name != "postgresql":
        return "unavailable"
    with engine.connect() as c:
        exempt = c.execute(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")).scalar()
        covered = c.execute(text("SELECT COUNT(*) FROM pg_class WHERE relname = ANY(:tables) AND relrowsecurity AND relforcerowsecurity"), {"tables": list(TENANT_TABLES)}).scalar()
    if covered < len(TENANT_TABLES):
        return "missing"
    return "bypassed" if exempt else "enforced"
