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

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from glob import glob
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from .config import Settings, get_settings

# Counted when a backup is taken and again after a restore: the two must agree.
COUNTED_TABLES = ("organizations", "users", "campaigns", "leads", "call_runs", "appointments", "playbook_versions", "training_documents", "knowledge_chunks",
                  "do_not_call", "usage_records", "ai_usage", "audit_log", "events", "jobs")
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


# ----------------------------------------------------------------------------- encryption

_MAGIC = b"CABK1\n"
_CHUNK = 1 << 20


def new_key() -> str:
    """A fresh backup key, to keep in a secret store. Without it an encrypted backup cannot be read by anyone."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def parse_key(value: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(value.strip().encode())
    except (ValueError, TypeError):
        key = b""
    if len(key) != 32:
        raise BackupError("BACKUP_ENCRYPTION_KEY is not a backup key. Create one with `callagent db backup-key`.")
    return key


def _encrypt_file(source: Path, target: Path, key: bytes) -> None:
    """AES-256-GCM in 1 MiB pieces, each authenticated together with its position and
    whether it is the last, so a file that was cut short, reordered or altered is refused."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    cipher, prefix, counter = AESGCM(key), os.urandom(8), 0
    with source.open("rb") as plain, target.open("wb") as out:
        out.write(_MAGIC + prefix)
        piece = plain.read(_CHUNK)
        while True:
            following = plain.read(_CHUNK)
            sealed = cipher.encrypt(prefix + counter.to_bytes(4, "big"), piece, b"more" if following else b"last")
            out.write(len(sealed).to_bytes(4, "big") + sealed)
            counter += 1
            if not following:
                break
            piece = following


def _decrypt_file(source: Path, target: Path, key: bytes) -> None:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    cipher, counter = AESGCM(key), 0
    with source.open("rb") as sealed, target.open("wb") as out:
        if sealed.read(len(_MAGIC)) != _MAGIC:
            raise BackupError(f"{source.name} is not an encrypted backup file")
        prefix = sealed.read(8)
        length = sealed.read(4)
        while True:
            piece = sealed.read(int.from_bytes(length, "big"))
            length = sealed.read(4)
            try:
                out.write(cipher.decrypt(prefix + counter.to_bytes(4, "big"), piece, b"more" if length else b"last"))
            except InvalidTag:
                raise BackupError(f"{source.name} could not be decrypted: the key is not the one it was made with, or the file is damaged") from None
            counter += 1
            if not length:
                break


def _key_of(settings: Settings) -> bytes | None:
    return parse_key(settings.backup_encryption_key) if settings.backup_encryption_key else None


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
        key = _key_of(settings)
        files: dict[str, dict[str, Any]] = {}
        for plain in (database_file, uploads):
            if key is None:
                files[plain.name] = {"sha256": _sha256(plain), "bytes": plain.stat().st_size}
                continue
            sealed = plain.with_name(plain.name + ".enc")
            _encrypt_file(plain, sealed, key)
            # The checksum of the stored file shows damage without the key; the plain one confirms a decryption.
            files[sealed.name] = {"sha256": _sha256(sealed), "bytes": sealed.stat().st_size, "plain_sha256": _sha256(plain)}
            plain.unlink()
        manifest = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database": kind,
            "encrypted": key is not None,
            **_describe_database(url),
            "files": files,
        }
        (target / MANIFEST).write_text(json.dumps(manifest, indent=2))
    except Exception:
        shutil.rmtree(target, ignore_errors=True)  # never leave half a backup that looks like a whole one
        raise
    return target


def verify_backup(path: Path, key: bytes | None = None) -> dict[str, Any]:
    """Check that every file is present and unchanged since the backup was taken. Returns
    the manifest. With the key of an encrypted backup, each file is also decrypted and
    compared with what was encrypted: proof that this key can read this backup."""
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
        if key is not None and manifest.get("encrypted"):
            with tempfile.TemporaryDirectory(dir=path.parent) as scratch:
                opened = Path(scratch) / "opened"
                _decrypt_file(file, opened, key)
                if _sha256(opened) != expected["plain_sha256"]:
                    raise BackupError(f"{name} does not match its checksum after decryption")
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
    restoring is destructive and must be a decision. Everything is unpacked, decrypted
    and checked in a staging directory first; nothing live is touched until the backup
    has proved readable. Returns the manifest, with the row counts found after the
    restore under ``restored_rows``.
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
    encrypted = bool(manifest.get("encrypted"))
    key = _key_of(settings)
    if encrypted and key is None:
        raise BackupError("this backup is encrypted. Set BACKUP_ENCRYPTION_KEY to the key it was made with.")

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    staging = settings.data_dir / f".restore-{stamp}"
    staging.mkdir()
    try:
        # 1. Stage: decrypt, unpack, check. A backup that fails here has changed nothing.
        staged: dict[str, Path] = {}
        for name in ("database.sqlite" if kind == "sqlite" else "database.pgdump", "uploads.tar.gz"):
            if encrypted:
                staged[name] = staging / name
                _decrypt_file(path / f"{name}.enc", staged[name], key)
                if _sha256(staged[name]) != manifest["files"][f"{name}.enc"]["plain_sha256"]:
                    raise BackupError(f"{name} does not match its checksum after decryption")
            else:
                staged[name] = path / name
        with tarfile.open(staged["uploads.tar.gz"], "r:gz") as archive:
            archive.extractall(staging / "unpacked", filter="data")  # "data": no absolute paths, links or device files
        if kind == "sqlite":
            candidate = staging / "candidate.sqlite"
            shutil.copyfile(staged["database.sqlite"], candidate)
            check = sqlite3.connect(candidate)
            try:
                verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
            finally:
                check.close()
            if verdict != "ok":
                raise BackupError(f"the database in this backup fails its integrity check: {verdict}")

        # 2. Swap. From here the live data changes.
        from . import db

        if db._engine is not None:  # this process must not keep the old file or connections open
            db._engine.dispose()
            db._engine = None
            db._SessionLocal = None
        if kind == "sqlite":
            target = _sqlite_path(url)
            target.parent.mkdir(parents=True, exist_ok=True)
            arriving = target.with_name(target.name + ".restoring")
            shutil.copyfile(staging / "candidate.sqlite", arriving)  # onto the database's own disk first, then one atomic rename
            for leftover in (Path(f"{target}-wal"), Path(f"{target}-shm")):
                leftover.unlink(missing_ok=True)
            os.replace(arriving, target)
        else:
            env, name = _pg_env(url)
            # One transaction: the database is the old one or the restored one, never part of each.
            _run([_pg_tool("pg_restore"), "--clean", "--if-exists", "--no-owner", "--no-privileges", "--single-transaction", f"--dbname={name}", str(staged["database.pgdump"])], env)

        uploads, arriving_uploads = settings.data_dir / "uploads", staging / "unpacked" / "uploads"
        outgoing = settings.data_dir / f".uploads-replaced-{stamp}"
        if uploads.exists():
            os.replace(uploads, outgoing)
        if arriving_uploads.exists():
            os.replace(arriving_uploads, uploads)
        shutil.rmtree(outgoing, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    restored = _describe_database(url)
    # Compared table by table for what the backup counted (an older backup counted fewer tables).
    found = {table: restored["rows"].get(table) for table in manifest["rows"]}
    if found != manifest["rows"]:
        raise BackupError(f"row counts after restoring ({found}) differ from the backup's ({manifest['rows']})")
    return {**manifest, "restored_rows": found}


# ----------------------------------------------------------------------------- keeping and scheduling


def _taken_at(directory: Path) -> datetime | None:
    try:
        return datetime.strptime(directory.name.removeprefix("backup-"), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def list_backups(destination: Path) -> list[tuple[datetime, Path]]:
    """Backups under ``destination``, oldest first."""
    found = [(_taken_at(d), d) for d in Path(destination).glob("backup-*") if d.is_dir() and (d / MANIFEST).is_file()]
    return sorted((when, d) for when, d in found if when is not None)


def prune_backups(destination: Path, keep_days: int, keep_at_least: int = 3) -> list[Path]:
    """Remove backups older than ``keep_days``. The newest ``keep_at_least`` are kept
    whatever their age, so a schedule that stopped running does not end with no backup."""
    backups = list_backups(destination)
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    removable = backups[: max(0, len(backups) - keep_at_least)]
    removed = []
    for when, directory in removable:
        if when < cutoff:
            shutil.rmtree(directory, ignore_errors=True)
            removed.append(directory)
    return removed


def backup_if_due(settings: Settings | None = None) -> Path | None:
    """Take a backup when the newest one is older than BACKUP_EVERY_HOURS, verify it and
    remove those past their keeping time. Returns the new backup, or None when none was due."""
    settings = settings or get_settings()
    if settings.backup_every_hours <= 0:
        return None
    destination = Path(settings.backup_dir or settings.data_dir / "backups")
    backups = list_backups(destination)
    if backups and datetime.now(timezone.utc) - backups[-1][0] < timedelta(hours=settings.backup_every_hours):
        return None
    made = create_backup(destination, settings)
    verify_backup(made, key=_key_of(settings))
    prune_backups(destination, settings.backup_keep_days)
    return made
