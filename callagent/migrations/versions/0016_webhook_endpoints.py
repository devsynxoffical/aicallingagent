"""Addresses an organization's own systems are told about events at, and each delivery."""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "webhook_endpoints" not in tables:
        op.create_table(
            "webhook_endpoints",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("url", sa.String(500), nullable=False),
            sa.Column("description", sa.String(200), nullable=False, server_default=""),
            sa.Column("secret", sa.String(80), nullable=False),
            sa.Column("events", sa.JSON, nullable=False),
            sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
            sa.Column("disabled_reason", sa.String(300)),
            sa.Column("failures", sa.Integer, nullable=False, server_default="0"),
            sa.Column("created_by", sa.String(254), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.Column("last_delivery_at", sa.DateTime),
            sa.Column("last_status", sa.Integer),
        )
        op.create_index("ix_webhook_endpoints_organization_id", "webhook_endpoints", ["organization_id"])
    if "webhook_deliveries" not in tables:
        op.create_table(
            "webhook_deliveries",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("endpoint_id", sa.Integer, sa.ForeignKey("webhook_endpoints.id"), nullable=False),
            sa.Column("event_id", sa.Integer, sa.ForeignKey("events.id")),
            sa.Column("event_type", sa.String(60), nullable=False),
            sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
            sa.Column("response_status", sa.Integer),
            sa.Column("error", sa.String(300)),
            sa.Column("duration_ms", sa.Integer),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.Column("delivered_at", sa.DateTime),
            sa.CheckConstraint("status IN ('pending', 'delivered', 'failed')", name="ck_webhook_deliveries_status"),
        )
        op.create_index("ix_webhook_deliveries_organization_id", "webhook_deliveries", ["organization_id"])
        op.create_index("ix_webhook_deliveries_endpoint_id", "webhook_deliveries", ["endpoint_id"])
        op.create_index("uq_webhook_deliveries_endpoint_event", "webhook_deliveries", ["endpoint_id", "event_id"], unique=True)

    from callagent.db import ensure_row_level_security

    ensure_row_level_security(bind)


def downgrade() -> None:
    op.drop_table("webhook_deliveries")
    op.drop_table("webhook_endpoints")
