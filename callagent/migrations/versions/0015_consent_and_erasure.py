"""Consent on record per lead, and when a call's content was removed."""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    leads = {c["name"] for c in inspector.get_columns("leads")}
    if "consent_basis" not in leads:
        op.add_column("leads", sa.Column("consent_basis", sa.String(60)))
        op.add_column("leads", sa.Column("consent_at", sa.DateTime))
        op.add_column("leads", sa.Column("consent_source", sa.String(200), nullable=False, server_default=""))
    if "content_erased_at" not in {c["name"] for c in inspector.get_columns("call_runs")}:
        op.add_column("call_runs", sa.Column("content_erased_at", sa.DateTime))


def downgrade() -> None:
    op.drop_column("call_runs", "content_erased_at")
    for column in ("consent_source", "consent_at", "consent_basis"):
        op.drop_column("leads", column)
