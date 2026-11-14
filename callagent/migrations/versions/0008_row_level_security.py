"""PostgreSQL row-level security on every table that holds an organization's rows.

The application already filters every request by the caller's organization. This is
the second lock: while a request's transaction is open, the database returns, and
accepts, only rows of that organization, so a query that forgot its filter cannot
leak or change another customer's data.

How it decides: the application sets `app.organization_id` for the transaction
(`db.session_scope`). With it set, a row is visible and writable only if its
`organization_id` matches. With it unset, nothing is restricted: that is how the
parts of the system that serve every organization work (provider webhooks, the dial
loop, background jobs, migrations), and they address rows by id, never by a name a
user typed.

The policies bind the table owner too (FORCE). A superuser, or a role with
BYPASSRLS, is never bound: run the application as an ordinary role. `GET /api/status`
reports whether the policies are enforced for the role in use.

SQLite has no row-level security; this migration does nothing there.
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None



def upgrade() -> None:
    from callagent.db import ensure_row_level_security

    ensure_row_level_security(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    from callagent.db import TENANT_TABLES

    existing = set(sa.inspect(bind).get_table_names())
    for table in (t for t in TENANT_TABLES if t in existing):
        bind.execute(sa.text(f"DROP POLICY IF EXISTS organization_isolation ON {table}"))
        bind.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
        bind.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
