"""Durable background jobs and their execution history."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "jobs" not in tables:  # a database adopted from before migrations already got it from the current models
        op.create_table(
            "jobs",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("kind", sa.String(60), nullable=False),
            sa.Column("key", sa.String(200), nullable=False),
            sa.Column("payload", sa.JSON, nullable=False),
            sa.Column("status", sa.String(12), nullable=False),
            sa.Column("attempts", sa.Integer, nullable=False),
            sa.Column("max_attempts", sa.Integer, nullable=False),
            sa.Column("timeout_seconds", sa.Integer, nullable=False),
            sa.Column("run_at", sa.DateTime, nullable=False),
            sa.Column("locked_by", sa.String(80)),
            sa.Column("locked_until", sa.DateTime),
            sa.Column("last_error", sa.Text),
            sa.Column("call_run_id", sa.Integer),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.Column("finished_at", sa.DateTime),
        )
        op.create_index("ix_jobs_kind", "jobs", ["kind"])
        op.create_index("ix_jobs_call_run_id", "jobs", ["call_run_id"])
        op.create_index("uq_jobs_key", "jobs", ["key"], unique=True)
        op.create_index("ix_jobs_due", "jobs", ["status", "run_at"])
    if "job_attempts" not in tables:
        op.create_table(
            "job_attempts",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id"), nullable=False),
            sa.Column("attempt", sa.Integer, nullable=False),
            sa.Column("worker", sa.String(80), nullable=False),
            sa.Column("outcome", sa.String(12), nullable=False),
            sa.Column("error", sa.Text),
            sa.Column("started_at", sa.DateTime, nullable=False),
            sa.Column("finished_at", sa.DateTime),
        )
        op.create_index("ix_job_attempts_job_id", "job_attempts", ["job_id"])


def downgrade() -> None:
    op.drop_table("job_attempts")
    op.drop_table("jobs")
