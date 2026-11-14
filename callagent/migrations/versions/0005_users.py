"""People who sign in to the console, and their sessions."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "users" not in tables:  # a database adopted from before migrations already got it from the current models
        op.create_table(
            "users",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("email", sa.String(254), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("role", sa.String(12), nullable=False),
            sa.Column("password_hash", sa.String(300), nullable=False),
            sa.Column("active", sa.Boolean, nullable=False),
            sa.Column("failed_logins", sa.Integer, nullable=False),
            sa.Column("locked_until", sa.DateTime),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.Column("last_login_at", sa.DateTime),
            sa.UniqueConstraint("email", name="uq_users_email"),
        )
        op.create_index("ix_users_organization_id", "users", ["organization_id"])
    if "user_sessions" not in tables:
        op.create_table(
            "user_sessions",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.Column("expires_at", sa.DateTime, nullable=False),
            sa.Column("revoked_at", sa.DateTime),
            sa.UniqueConstraint("token_hash", name="uq_user_sessions_token_hash"),
        )
        op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])


def downgrade() -> None:
    op.drop_table("user_sessions")
    op.drop_table("users")
