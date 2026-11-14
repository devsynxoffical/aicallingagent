"""An audit trail of changes made through the API, and the request a job came from."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "request_id" not in {c["name"] for c in inspector.get_columns("jobs")}:
        op.add_column("jobs", sa.Column("request_id", sa.String(64)))

    if "audit_log" not in set(inspector.get_table_names()):
        op.create_table(
            "audit_log",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("actor", sa.String(254), nullable=False),
            sa.Column("actor_role", sa.String(12), nullable=False),
            sa.Column("action", sa.String(80), nullable=False),
            sa.Column("method", sa.String(8), nullable=False),
            sa.Column("route", sa.String(200), nullable=False),
            sa.Column("target", sa.JSON, nullable=False),
            sa.Column("status", sa.Integer, nullable=False),
            sa.Column("request_id", sa.String(64)),
            sa.Column("address", sa.String(64)),
            sa.Column("created_at", sa.DateTime, nullable=False),
        )
        op.create_index("ix_audit_log_organization_id", "audit_log", ["organization_id"])
        op.create_index("ix_audit_log_org_created", "audit_log", ["organization_id", "created_at"])

    from callagent.db import ensure_row_level_security

    ensure_row_level_security(bind)  # the new table gets the same isolation policy as the others


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_column("jobs", "request_id")
