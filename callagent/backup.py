"""Backups of the database and the uploaded training files, and restoring them.

A backup is a directory:

    backup-20261113T181500Z/
        database.sqlite | database.pgdump    a consistent copy taken while the server runs
        uploads.tar.gz                       DATA_DIR/uploads
        manifest.json                        when, which schema version, row counts, checksums

Secrets are never part of a backup. Provider keys live in the environment; the API token
file in DATA_DIR is left out on purpose (see docs/DISASTER_RECOVERY.md).

A backup that has not been restored is a hope, not a backup: ``verify`` re-checks the
checksums, and tests/test_backup.py restores one for real on every test run.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
from datetime import datetime, timezone
from glob import glob
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from .config import Settings, get_settings

COUNTED_TABLES = ("organizations", "campaigns", "leads", "call_runs", "appointments", "playbook_versions", "training_documents", "do_not_call")
MANIFEST = "manifest.json"


class BackupError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sqlite_path(url: str) -> Path:
    database = make_url(url).database
    if not database or database == ":memory:":
        raise BackupError("an in-memory database cannot be backed up")
    return Path(database)


def _pg_tool(name: str) -> str:
    found = shutil.which(name) or next(iter(sorted(glob(f"/usr/lib/postgresql/*/bin/{name}"), reverse=True)), None)
    if not found:
        raise BackupError(f"{name} was not found. Install the PostgreSQL client tools on this machine.")
    return found


def _pg_env(url: str) -> tuple[dict[str, str], str]:
    """Connection details for the PostgreSQL tools. The password travels in the
    environment, never on the command line where other users could read it."""
    parsed = make_url(url)
    env = dict(os.environ)
    for key, value in (("PGHOST", parsed.host), ("PGPORT", parsed.port), ("PGUSER", parsed.username), ("PGPASSWORD", parsed.password)):
        if value:
            env[key] = str(value)
    return env, parsed.database or ""


def _run(command: list[str], env: dict[str, str]) -> None:
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise BackupError(f"{Path(command[0]).name} failed: {result.stderr.strip()[-500:]}")


def _describe_database(url: str) -> dict[str, Any]:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar() if "alembic_version" in tables else None
            counts = {t: connection.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar() for t in COUNTED_TABLES if t in tables}
        return {"schema_revision": revision, "rows": counts}
    finally:
        engine.dispose()


def create_backup(destination: Path, settings: Settings | None = None) -> Path:
    """Write a new backup directory under ``destination`` and return its path."""
    settings = settings or get_settings()
    url = settings.database_url
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = Path(destination) / f"backup-{stamp}"
    target.mkdir(parents=True, exist_ok=False)
    try:
        if url.startswith("sqlite"):
            database_file = target / "database.sqlite"
            source = sqlite3.connect(_sqlite_path(url))
            copy = sqlite3.connect(database_file)
            try:
                source.backup(copy)  # SQLite's online backup: consistent while other connections write
            finally:
                copy.close()
                source.close()
            kind = "sqlite"
        else:
            database_file = target / "database.pgdump"
            env, name = _pg_env(url)
            _run([_pg_tool("pg_dump"), "--format=custom", "--no-owner", "--no-privileges", f"--file={database_file}", name], env)
            kind = "postgresql"
        uploads = target / "uploads.tar.gz"
        with tarfile.open(uploads, "w:gz") as archive:
            source_dir = settings.data_dir / "uploads"
            if source_dir.is_dir():
                archive.add(source_dir, arcname="uploads")
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": kind,
            **_describe_database(url),
            "files": {f.name: {"sha256": _sha256(f), "bytes": f.stat().st_size} for f in (database_file, uploads)},
        }
        (target / MANIFEST).write_text(json.dumps(manifest, indent=2))
    except Exception:
        shutil.rmtree(target, ignore_errors=True)  # never leave half a backup that looks like a whole one
        raise
    return target


def verify_backup(path: Path) -> dict[str, Any]:
    """Check that every file is present and unchanged since the backup was taken. Returns the manifest."""
    path = Path(path)
    try:
        manifest = json.loads((path / MANIFEST).read_text())
    except (OSError, ValueError) as e:
        raise BackupError(f"{path} is not a backup (no readable {MANIFEST}): {e}") from None
    for name, expected in manifest.get("files", {}).items():
        file = path / name
        if not file.is_file():
            raise BackupError(f"{name} is missing from the backup")
        if _sha256(file) != expected["sha256"]:
            raise BackupError(f"{name} does not match its checksum: the backup is damaged or was altered")
    return manifest


def _has_data(url: str) -> bool:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            if "campaigns" not in inspect(connection).get_table_names():
                return False
            return connection.execute(text("SELECT COUNT(*) FROM campaigns")).scalar() > 0
    finally:
        engine.dispose()


def restore_backup(path: Path, settings: Settings | None = None, *, replace_existing: bool = False) -> dict[str, Any]:
    """Replace the database and uploads with a backup's contents. Stop the server first.

    Refuses a database that already holds campaigns unless ``replace_existing`` is set:
    restoring is destructive and must be a decision. Returns the manifest, with the row
    counts found after the restore under ``restored_rows``.
    """
    settings = settings or get_settings()
    path = Path(path)
    manifest = verify_backup(path)
    url = settings.database_url
    kind = "sqlite" if url.startswith("sqlite") else "postgresql"
    if manifest["database"] != kind:
        raise BackupError(f"this backup is of a {manifest['database']} database; DATABASE_URL points at {kind}")
    if not replace_existing and (kind == "postgresql" or _sqlite_path(url).exists()) and _has_data(url):
        raise BackupError("the target database already holds campaigns; restoring would replace them. Pass replace_existing to confirm.")

    from . import db

    if db._engine is not None:  # this process must not keep the old file or connections open
        db._engine.dispose()
        db._engine = None
        db._SessionLocal = None

    if kind == "sqlite":
        target = _sqlite_path(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        for leftover in (target, Path(f"{target}-wal"), Path(f"{target}-shm")):
            leftover.unlink(missing_ok=True)
        shutil.copyfile(path / "database.sqlite", target)
    else:
        env, name = _pg_env(url)
        _run([_pg_tool("pg_restore"), "--clean", "--if-exists", "--no-owner", "--no-privileges", "--single-transaction", f"--dbname={name}", str(path / "database.pgdump")], env)

    uploads = settings.data_dir / "uploads"
    shutil.rmtree(uploads, ignore_errors=True)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path / "uploads.tar.gz", "r:gz") as archive:
        archive.extractall(settings.data_dir, filter="data")  # "data": no absolute paths, links or device files

    restored = _describe_database(url)
    if restored["rows"] != manifest["rows"]:
        raise BackupError(f"row counts after restoring ({restored['rows']}) differ from the backup's ({manifest['rows']})")
    return {**manifest, "restored_rows": restored["rows"]}
