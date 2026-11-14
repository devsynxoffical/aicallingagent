"""One-time links by which a person joins an organization or resets their password."""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "invitations" not in set(sa.inspect(bind).get_table_names()):
        op.create_table(
            "invitations",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("purpose", sa.String(8), nullable=False, server_default="join"),
            sa.Column("email", sa.String(254), nullable=False),
            sa.Column("name", sa.String(200), nullable=False, server_default=""),
            sa.Column("role", sa.String(12), nullable=False, server_default="viewer"),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id")),
            sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("created_by", sa.String(254), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.Column("expires_at", sa.DateTime, nullable=False),
            sa.Column("used_at", sa.DateTime),
            sa.Column("revoked_at", sa.DateTime),
            sa.CheckConstraint("role IN ('admin', 'operator', 'viewer')", name="ck_invitations_role"),
            sa.CheckConstraint("purpose IN ('join', 'reset')", name="ck_invitations_purpose"),
        )
        op.create_index("ix_invitations_organization_id", "invitations", ["organization_id"])

    from callagent.db import ensure_row_level_security

    ensure_row_level_security(bind)


def downgrade() -> None:
    op.drop_table("invitations")
