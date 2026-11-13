"""Alembic environment. Migrations are run by ``callagent.migrate.upgrade`` on a
connection it already holds, so there is no alembic.ini and no second database URL."""

from alembic import context

from callagent.db import Base

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("run migrations through `callagent db upgrade` (or callagent.migrate.upgrade), not the alembic command")

# render_as_batch: SQLite cannot alter a table in place; batch mode rebuilds it.
context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
with context.begin_transaction():
    context.run_migrations()
