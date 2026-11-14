"""Leases (which process currently runs what only one may run), and an index for the cross-campaign call check."""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "ix_leads_org_phone" not in {index["name"] for index in inspector.get_indexes("leads")}:
        # Asked before every dial: is this number on a call from another campaign of the organization.
        # On a large, busy PostgreSQL database build it beforehand with CREATE INDEX CONCURRENTLY under
        # this name; the migration then finds it and does nothing.
        op.create_index("ix_leads_org_phone", "leads", ["organization_id", "phone_e164"])
    if "leases" not in set(inspector.get_table_names()):
        op.create_table(
            "leases",
            sa.Column("name", sa.String(40), primary_key=True),
            sa.Column("holder", sa.String(120), nullable=False),
            sa.Column("expires_at", sa.DateTime, nullable=False),
            sa.Column("acquired_at", sa.DateTime, nullable=False),
        )


def downgrade() -> None:
    op.drop_table("leases")
    op.drop_index("ix_leads_org_phone", table_name="leads")
