"""Every customer-owned row carries its organization.

- `leads`, `call_runs`, `appointments`, `jobs` and `do_not_call` get an
  `organization_id`, filled from the campaign (or lead, or call) they belong to.
- Campaign names and do-not-call numbers become unique per organization instead of
  across the deployment.
- Status columns get CHECK constraints, and three indexes are added for the daily
  limit check, the calls-per-day chart and the reconciler.

Existing rows all belong to the default organization, which is what a deployment
that predates organizations has. Nothing is deleted.

SQLite cannot change an existing column or drop a constraint without rebuilding the
table, and rebuilding tables that others reference is not safe inside a migration.
So on an existing SQLite database the new columns are added and filled, but stay
nullable in the schema (the application always fills them), the old deployment-wide
uniqueness of campaign names stays, and no CHECK constraints are added. A new
SQLite database, and PostgreSQL in every case, get the full schema. More than one
organization requires PostgreSQL.
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

ACTIVE = "status IN ('created', 'queued', 'initiated', 'ringing', 'in-progress')"
CHECKS = {
    "campaigns": "status IN ('draft', 'ready', 'running', 'paused', 'done')",
    "leads": "status IN ('pending', 'queued', 'calling', 'completed', 'callback', 'failed', 'dnc')",
    "call_runs": "status IN ('created', 'queued', 'initiated', 'ringing', 'in-progress', 'completed', 'busy', 'no-answer', 'failed', 'canceled')",
    "jobs": "status IN ('queued', 'running', 'done', 'dead')",
}


def _columns(inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def _indexes(inspector, table: str) -> set[str]:
    return {i["name"] for i in inspector.get_indexes(table)}


def _drop_uniqueness(bind, inspector, table: str, column: str) -> None:
    """Remove a uniqueness rule on one column, whichever way the database holds it: as a
    constraint (PostgreSQL also reports the index behind it, which goes with it) or as
    a unique index of its own."""
    behind_constraints: set[str] = set()
    if bind.dialect.name == "postgresql":
        for constraint in inspector.get_unique_constraints(table):
            if constraint["column_names"] == [column]:
                op.drop_constraint(constraint["name"], table, type_="unique")
                behind_constraints.add(constraint["name"])
    for index in inspector.get_indexes(table):
        if index.get("unique") and index["column_names"] == [column] and index["name"] not in behind_constraints and not index.get("duplicates_constraint"):
            op.drop_index(index["name"], table_name=table)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    postgres = bind.dialect.name == "postgresql"

    default = bind.execute(sa.text("SELECT id FROM organizations WHERE name = 'default'")).scalar()
    if default is None:
        bind.execute(sa.text("INSERT INTO organizations (name, created_at) VALUES ('default', CURRENT_TIMESTAMP)"))
        default = bind.execute(sa.text("SELECT id FROM organizations WHERE name = 'default'")).scalar()

    # Unknown statuses would make the CHECK constraints fail half-way: look first, change nothing if any exist.
    if postgres:
        for table, rule in CHECKS.items():
            bad = bind.execute(sa.text(f"SELECT DISTINCT status FROM {table} WHERE NOT ({rule})")).scalars().all()
            if bad:
                raise RuntimeError(f"{table} holds rows with a status this release does not know: {sorted(bad)}. Nothing was changed. Correct those rows, then upgrade again.")

    bind.execute(sa.text("UPDATE campaigns SET organization_id = :org WHERE organization_id IS NULL"), {"org": default})

    fills = {
        "leads": "(SELECT c.organization_id FROM campaigns c WHERE c.id = leads.campaign_id)",
        "call_runs": "(SELECT c.organization_id FROM campaigns c WHERE c.id = call_runs.campaign_id)",
        "appointments": "(SELECT l.organization_id FROM leads l WHERE l.id = appointments.lead_id)",
        "jobs": "(SELECT r.organization_id FROM call_runs r WHERE r.id = jobs.call_run_id)",
        "do_not_call": str(int(default)),
    }
    for table, source in fills.items():
        if "organization_id" not in _columns(inspector, table):
            if postgres:
                op.add_column(table, sa.Column("organization_id", sa.Integer, nullable=True))
                op.create_foreign_key(f"fk_{table}_organization_id", table, "organizations", ["organization_id"], ["id"])
            else:  # SQLite can only declare the reference while adding the column
                bind.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN organization_id INTEGER REFERENCES organizations(id)"))
        bind.execute(sa.text(f"UPDATE {table} SET organization_id = {source} WHERE organization_id IS NULL"))
        if f"ix_{table}_organization_id" not in _indexes(sa.inspect(bind), table):
            op.create_index(f"ix_{table}_organization_id", table, ["organization_id"])
    bind.execute(sa.text("UPDATE usage_records SET organization_id = (SELECT c.organization_id FROM campaigns c WHERE c.id = usage_records.campaign_id) WHERE organization_id IS NULL"))

    inspector = sa.inspect(bind)
    _drop_uniqueness(bind, inspector, "do_not_call", "phone_e164")
    if "uq_do_not_call_org_phone" not in _indexes(sa.inspect(bind), "do_not_call"):
        op.create_index("uq_do_not_call_org_phone", "do_not_call", ["organization_id", "phone_e164"], unique=True)
    if postgres:
        _drop_uniqueness(bind, inspector, "campaigns", "name")
    if "uq_campaigns_org_name" not in _indexes(sa.inspect(bind), "campaigns"):
        op.create_index("uq_campaigns_org_name", "campaigns", ["organization_id", "name"], unique=True)

    existing = _indexes(sa.inspect(bind), "call_runs")
    if "ix_call_runs_campaign_started" not in existing:
        op.create_index("ix_call_runs_campaign_started", "call_runs", ["campaign_id", "started_at"])
    if "ix_call_runs_org_started" not in existing:
        op.create_index("ix_call_runs_org_started", "call_runs", ["organization_id", "started_at"])
    if "ix_call_runs_active" not in existing:
        op.create_index("ix_call_runs_active", "call_runs", ["started_at"], sqlite_where=sa.text(ACTIVE), postgresql_where=sa.text(ACTIVE))

    if postgres:
        for table in ("campaigns", "leads", "call_runs", "appointments", "do_not_call", "usage_records"):
            op.alter_column(table, "organization_id", nullable=False)
        have = {c["name"] for table in CHECKS for c in sa.inspect(bind).get_check_constraints(table)}
        for table, rule in CHECKS.items():
            if f"ck_{table}_status" not in have:
                op.create_check_constraint(f"ck_{table}_status", table, rule)


def downgrade() -> None:
    """Back to one deployment-wide namespace. Refused once a second organization holds
    data, because campaign names and do-not-call numbers could then collide.

    Supported for PostgreSQL, and for a SQLite database that was upgraded in place. A
    SQLite database first created by this release has NOT NULL organization columns
    that the previous release does not fill; restore that one from a backup instead."""
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(DISTINCT organization_id) FROM campaigns")).scalar() > 1:
        raise RuntimeError("more than one organization has campaigns: this release cannot be rolled back without losing their separation")
    postgres = bind.dialect.name == "postgresql"
    for name, table in (("ix_call_runs_active", "call_runs"), ("ix_call_runs_org_started", "call_runs"), ("ix_call_runs_campaign_started", "call_runs"),
                        ("uq_campaigns_org_name", "campaigns"), ("uq_do_not_call_org_phone", "do_not_call")):
        op.drop_index(name, table_name=table)
    op.create_index("ix_do_not_call_phone_e164", "do_not_call", ["phone_e164"], unique=True)
    if postgres:
        op.create_unique_constraint("campaigns_name_key", "campaigns", ["name"])
        for table in CHECKS:
            op.drop_constraint(f"ck_{table}_status", table, type_="check")
        for table in ("campaigns", "usage_records"):
            op.alter_column(table, "organization_id", nullable=True)
    for table in ("leads", "call_runs", "appointments", "jobs", "do_not_call"):
        op.drop_index(f"ix_{table}_organization_id", table_name=table)
        if postgres:
            op.drop_column(table, "organization_id")
        # On SQLite the column stays: removing it means rebuilding tables that others
        # reference. It is nullable there and the previous release ignores it.
