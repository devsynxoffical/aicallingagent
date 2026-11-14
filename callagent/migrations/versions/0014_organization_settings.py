"""Each organization's own numbers and settings, the number a call was placed from, and API keys."""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "settings_json" not in {c["name"] for c in inspector.get_columns("organizations")}:
        # May stay empty: an organization with nothing set has no settings of its own.
        op.add_column("organizations", sa.Column("settings_json", sa.JSON, nullable=True))
    if "from_number" not in {c["name"] for c in inspector.get_columns("call_runs")}:
        op.add_column("call_runs", sa.Column("from_number", sa.String(20)))
    if "api_keys" not in set(inspector.get_table_names()):
        op.create_table(
            "api_keys",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("role", sa.String(12), nullable=False),
            sa.Column("prefix", sa.String(16), nullable=False),
            sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("created_by", sa.String(254), nullable=False),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.Column("last_used_at", sa.DateTime),
            sa.Column("revoked_at", sa.DateTime),
            sa.CheckConstraint("role IN ('admin', 'operator', 'viewer')", name="ck_api_keys_role"),
        )
        op.create_index("ix_api_keys_organization_id", "api_keys", ["organization_id"])

    from callagent.db import ensure_row_level_security

    ensure_row_level_security(bind)


def downgrade() -> None:
    op.drop_table("api_keys")
    op.drop_column("call_runs", "from_number")
    op.drop_column("organizations", "settings_json")  # in place: other tables refer to this one
