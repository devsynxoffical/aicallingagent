"""Who reported a call's status, and the usage ledger."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "status_source" not in {c["name"] for c in inspector.get_columns("call_runs")}:
        op.add_column("call_runs", sa.Column("status_source", sa.String(12), nullable=True))
    if "usage_records" not in set(inspector.get_table_names()):
        op.create_table(
            "usage_records",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id")),
            sa.Column("campaign_id", sa.Integer, sa.ForeignKey("campaigns.id"), nullable=False),
            sa.Column("call_run_id", sa.Integer, sa.ForeignKey("call_runs.id"), nullable=False),
            sa.Column("kind", sa.String(20), nullable=False),
            sa.Column("quantity_seconds", sa.Integer, nullable=False),
            sa.Column("amount_usd", sa.Numeric(12, 4), nullable=False),
            sa.Column("created_at", sa.DateTime, nullable=False),
        )
        op.create_index("ix_usage_records_organization_id", "usage_records", ["organization_id"])
        op.create_index("ix_usage_records_campaign_id", "usage_records", ["campaign_id"])
        op.create_index("uq_usage_records_call_kind", "usage_records", ["call_run_id", "kind"], unique=True)


def downgrade() -> None:
    op.drop_table("usage_records")
    with op.batch_alter_table("call_runs") as batch:
        batch.drop_column("status_source")
