"""A durable event log, and queues with priorities for background jobs."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

# Where each kind of job that may already be waiting belongs from now on.
QUEUE_OF = {"booking_webhook": "calls", "call_review": "analysis", "training_run": "knowledge"}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("jobs")}
    if "queue" not in columns:
        op.add_column("jobs", sa.Column("queue", sa.String(20), nullable=False, server_default="default"))
    if "priority" not in columns:
        op.add_column("jobs", sa.Column("priority", sa.Integer, nullable=False, server_default="0"))
    for kind, queue in QUEUE_OF.items():
        bind.execute(sa.text("UPDATE jobs SET queue = :queue WHERE kind = :kind AND queue = 'default'"), {"queue": queue, "kind": kind})
    if "ix_jobs_queue_due" not in {i["name"] for i in inspector.get_indexes("jobs")}:
        op.create_index("ix_jobs_queue_due", "jobs", ["queue", "status", "run_at"])

    if "events" not in set(inspector.get_table_names()):
        op.create_table(
            "events",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id")),
            sa.Column("type", sa.String(60), nullable=False),
            sa.Column("version", sa.Integer, nullable=False),
            sa.Column("subject", sa.String(30), nullable=False),
            sa.Column("subject_id", sa.Integer),
            sa.Column("campaign_id", sa.Integer),
            sa.Column("call_run_id", sa.Integer),
            sa.Column("key", sa.String(200), nullable=False),
            sa.Column("payload", sa.JSON, nullable=False),
            sa.Column("occurred_at", sa.DateTime, nullable=False),
        )
        op.create_index("ix_events_organization_id", "events", ["organization_id"])
        op.create_index("ix_events_type", "events", ["type"])
        op.create_index("ix_events_campaign_id", "events", ["campaign_id"])
        op.create_index("ix_events_call_run_id", "events", ["call_run_id"])
        op.create_index("uq_events_key", "events", ["key"], unique=True)

    from callagent.db import ensure_row_level_security

    ensure_row_level_security(bind)  # the new table gets the same isolation policy as the others


def downgrade() -> None:
    op.drop_table("events")
    op.drop_index("ix_jobs_queue_due", table_name="jobs")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("priority")
        batch.drop_column("queue")
