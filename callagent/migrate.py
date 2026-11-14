"""Bring a database to the current schema, and record how it got there.

Three cases, decided by what the database already holds:

- **Empty**: create the current schema and record it as being at the newest migration.
- **Created before migrations existed** (tables, but no migration record): add the
  columns those releases added one by one, record it as the baseline, then run every
  migration after the baseline.
- **Already tracked**: run the migrations it has not seen.

Migrations live in ``callagent/migrations/versions`` and are applied in order, each in
a transaction where the database supports it. A migration that finds data it cannot
safely change stops with a message and changes nothing.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

log = logging.getLogger(__name__)

BASELINE = "0001"
# Any constant works: it only has to be the same number in every process.
_ADVISORY_LOCK = 0x43414C4C  # "CALL"


def _config(connection: Connection) -> Config:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    config.attributes["connection"] = connection
    return config


def head_revision() -> str:
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head() or BASELINE


def current_revision(connection: Connection) -> str | None:
    if "alembic_version" not in inspect(connection).get_table_names():
        return None
    return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()


def upgrade(engine: Engine) -> str:
    """Returns what was done: "created", "adopted" (a pre-migration database), "upgraded" or "current"."""
    from .db import Base, add_missing_columns, ensure_row_level_security

    with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            # Two processes starting together must not both create the schema.
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADVISORY_LOCK})
        tables = set(inspect(connection).get_table_names())
        config = _config(connection)
        if "alembic_version" not in tables and "campaigns" not in tables:
            Base.metadata.create_all(connection)
            ensure_row_level_security(connection)  # what the models cannot express; migration 0008 does the same for existing databases
            command.stamp(config, "head")
            return "created"
        outcome = "upgraded"
        if "alembic_version" not in tables:
            added = add_missing_columns(connection)
            Base.metadata.create_all(connection)  # tables introduced by releases this database never ran
            command.stamp(config, BASELINE)
            log.info("adopted a database created before migrations existed (columns added: %s)", ", ".join(added) or "none")
            outcome = "adopted"
        elif current_revision(connection) == head_revision():
            return "current"
        command.upgrade(config, "head")
        return outcome


def integrity_problems(connection: Connection) -> list[str]:
    """Rows that break a uniqueness rule, in words. Empty on a healthy database."""
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    return ScriptDirectory.from_config(config).get_revision("0002").module.find_problems(connection)
