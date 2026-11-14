"""Requests sent through the website's forms."""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "website_requests" in set(sa.inspect(op.get_bind()).get_table_names()):
        return  # a database adopted from before migrations already got it from the current models
    op.create_table(
        "website_requests",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("organization_id", sa.Integer, sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("email", sa.String(254)),
        sa.Column("name", sa.String(120)),
        sa.Column("company", sa.String(160)),
        sa.Column("phone", sa.String(32)),
        sa.Column("team_size", sa.String(16)),
        sa.Column("message", sa.Text),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("handled_by", sa.String(254)),
        sa.Column("handled_at", sa.DateTime),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_website_requests_organization_id", "website_requests", ["organization_id"])
    op.create_index("ix_website_requests_kind", "website_requests", ["kind"])
    op.create_index("ix_website_requests_status", "website_requests", ["status"])


def downgrade() -> None:
    op.drop_table("website_requests")
