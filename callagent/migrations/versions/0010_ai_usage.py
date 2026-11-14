"""A record of every use of the AI model, what each call ran on, and what a document left out."""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

OUTCOMES = "outcome IN ('ok','refused','overloaded','timeout','unreachable','busy','failed')"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("call_runs")}
    if "ai_model" not in columns:
        op.add_column("call_runs", sa.Column("ai_model", sa.String(80)))
    if "prompt_hash" not in columns:
        op.add_column("call_runs", sa.Column("prompt_hash", sa.String(16)))

    if "warning" not in {c["name"] for c in inspector.get_columns("training_documents")}:
        op.add_column("training_documents", sa.Column("warning", sa.Text))

    if "ai_usage" not in set(inspector.get_table_names()):
        op.create_table(
            "ai_usage",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("campaign_id", sa.Integer, sa.ForeignKey("campaigns.id")),
            sa.Column("call_run_id", sa.Integer, sa.ForeignKey("call_runs.id")),
            sa.Column("workflow", sa.String(40), nullable=False),
            sa.Column("model", sa.String(80), nullable=False),
            sa.Column("prompt_hash", sa.String(16)),
            sa.Column("requests", sa.Integer, nullable=False),
            sa.Column("attempts", sa.Integer, nullable=False),
            sa.Column("input_tokens", sa.Integer, nullable=False),
            sa.Column("output_tokens", sa.Integer, nullable=False),
            sa.Column("cache_read_tokens", sa.Integer, nullable=False),
            sa.Column("cache_write_tokens", sa.Integer, nullable=False),
            sa.Column("duration_ms", sa.Integer, nullable=False),
            sa.Column("outcome", sa.String(16), nullable=False),
            sa.Column("cost_usd", sa.Numeric(12, 6)),
            sa.Column("request_id", sa.String(80)),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.CheckConstraint(OUTCOMES, name="ck_ai_usage_outcome"),
        )
        op.create_index("ix_ai_usage_organization_id", "ai_usage", ["organization_id"])
        op.create_index("ix_ai_usage_campaign_id", "ai_usage", ["campaign_id"])
        op.create_index("ix_ai_usage_call_run_id", "ai_usage", ["call_run_id"])
        op.create_index("ix_ai_usage_org_created", "ai_usage", ["organization_id", "created_at"])

    from callagent.db import ensure_row_level_security

    ensure_row_level_security(bind)  # the new table gets the same isolation policy as the others


def downgrade() -> None:
    op.drop_table("ai_usage")
    op.drop_column("training_documents", "warning")
    # Dropped in place: rebuilding the calls table (what a batch operation does on SQLite)
    # is refused while other tables refer to it.
    op.drop_column("call_runs", "prompt_hash")
    op.drop_column("call_runs", "ai_model")
