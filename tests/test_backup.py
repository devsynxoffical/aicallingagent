"""A restore drill on every test run: back up, destroy, restore, compare.

Runs on SQLite always, and on PostgreSQL (pg_dump / pg_restore) when TEST_POSTGRES_URL is set.
"""

import os

import pytest
from sqlalchemy import create_engine, text

from callagent import backup, db
from callagent.config import Settings
from callagent.training import versions

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
BACKENDS = ["sqlite"] + (["postgres"] if POSTGRES_URL else [])


@pytest.fixture(params=BACKENDS)
def settings(request, tmp_path):
    if request.param == "postgres":
        engine = create_engine(POSTGRES_URL, isolation_level="AUTOCOMMIT")
        with engine.connect() as c:
            c.execute(text("DROP SCHEMA public CASCADE"))
            c.execute(text("CREATE SCHEMA public"))
        engine.dispose()
        url = POSTGRES_URL
    else:
        url = f"sqlite:///{tmp_path / 'live' / 'callagent.db'}"
        (tmp_path / "live").mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    yield Settings(database_url=url, data_dir=data_dir)
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 'after.db'}")


def _fill(settings, sample_playbook) -> None:
    db.reset_engine_for_tests(settings.database_url)
    with db.session_scope() as s:
        c = db.Campaign(name="q4", status="paused")
        s.add(c)
        s.flush()
        versions.record_active(s, c, sample_playbook.model_copy(update={"open_questions": []}).model_dump(), source="onboard")
        for i in range(25):
            lead = db.Lead(campaign_id=c.id, phone_e164=f"+1415555{i:04d}", first_name=f"Lead {i}", notes="تجربة – accentué")
            s.add(lead)
            s.flush()
            s.add(db.CallRun(lead_id=lead.id, campaign_id=c.id, status="completed", twilio_call_sid=f"CA{i}", cost_usd=0.42,
                             transcript=[{"role": "user", "text": "bonjour, مرحبا"}], summary={"review": {"summary": "ok"}}))
        db.add_dnc(s, "+14155559999", db.default_organization_id(s), "asked")
    upload = settings.data_dir / "uploads" / "1" / "1" / "7.pdf"
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"%PDF-1.4 the pricing document")
    (settings.data_dir / "api_token").write_text("a-secret-token")


def _destroy(settings) -> None:
    """Lose everything: the calls, the leads and the uploaded files."""
    with db.session_scope() as s:
        for table in ("call_runs", "leads", "playbook_versions", "do_not_call", "campaigns"):
            s.execute(text(f"DELETE FROM {table}"))
    (settings.data_dir / "uploads" / "1" / "1" / "7.pdf").unlink()


def test_restore_drill_brings_back_every_row_and_file(settings, sample_playbook, tmp_path):
    _fill(settings, sample_playbook)
    path = backup.create_backup(tmp_path / "backups", settings)
    manifest = backup.verify_backup(path)
    assert manifest["rows"]["leads"] == 25 and manifest["rows"]["call_runs"] == 25 and manifest["schema_revision"]

    _destroy(settings)
    with db.session_scope() as s:
        assert s.execute(text("SELECT COUNT(*) FROM leads")).scalar() == 0

    result = backup.restore_backup(path, settings)
    assert result["restored_rows"] == manifest["rows"]
    db.reset_engine_for_tests(settings.database_url)  # what the server does when it starts after a restore
    with db.session_scope() as s:
        lead = s.execute(text("SELECT first_name, notes FROM leads WHERE phone_e164 = '+14155550007'")).one()
        assert tuple(lead) == ("Lead 7", "تجربة – accentué")
        run = s.query(db.CallRun).filter_by(twilio_call_sid="CA7").one()
        assert run.transcript == [{"role": "user", "text": "bonjour, مرحبا"}] and run.cost_usd == pytest.approx(0.42)
        assert s.query(db.PlaybookVersion).filter_by(status="active").count() == 1
        assert db.is_dnc(s, "+14155559999", db.default_organization_id(s))  # the suppression list survives: nobody who opted out is called after a restore
    assert (settings.data_dir / "uploads" / "1" / "1" / "7.pdf").read_bytes() == b"%PDF-1.4 the pricing document"


def test_secrets_are_not_in_a_backup(settings, sample_playbook, tmp_path):
    _fill(settings, sample_playbook)
    path = backup.create_backup(tmp_path / "backups", settings)
    for file in path.iterdir():
        assert b"a-secret-token" not in file.read_bytes()
    import tarfile

    with tarfile.open(path / "uploads.tar.gz") as archive:
        assert all(name == "uploads" or name.startswith("uploads/") for name in archive.getnames())


def test_a_damaged_backup_is_refused(settings, sample_playbook, tmp_path):
    _fill(settings, sample_playbook)
    path = backup.create_backup(tmp_path / "backups", settings)
    database_file = next(p for p in path.iterdir() if p.name.startswith("database."))
    data = bytearray(database_file.read_bytes())
    data[len(data) // 2] ^= 0xFF
    database_file.write_bytes(bytes(data))
    with pytest.raises(backup.BackupError, match="does not match its checksum"):
        backup.verify_backup(path)
    with pytest.raises(backup.BackupError, match="does not match its checksum"):
        backup.restore_backup(path, settings, replace_existing=True)
    with db.session_scope() as s:
        assert s.execute(text("SELECT COUNT(*) FROM leads")).scalar() == 25  # the live data was not touched


def test_restoring_over_live_data_needs_an_explicit_yes(settings, sample_playbook, tmp_path):
    _fill(settings, sample_playbook)
    path = backup.create_backup(tmp_path / "backups", settings)
    with db.session_scope() as s:
        s.execute(text("UPDATE leads SET first_name = 'changed after the backup'"))
    with pytest.raises(backup.BackupError, match="already holds campaigns"):
        backup.restore_backup(path, settings)
    backup.restore_backup(path, settings, replace_existing=True)
    db.reset_engine_for_tests(settings.database_url)
    with db.session_scope() as s:
        assert s.execute(text("SELECT COUNT(*) FROM leads WHERE first_name = 'changed after the backup'")).scalar() == 0


# ----------------------------------------------------------------------------- encrypted backups (audit E-4)


def test_an_encrypted_backup_restores_into_an_empty_database_with_every_row_and_file(settings, sample_playbook, tmp_path):
    key = backup.new_key()
    sealed_settings = settings.model_copy(update={"backup_encryption_key": key})
    _fill(settings, sample_playbook)
    path = backup.create_backup(tmp_path / "backups", sealed_settings)

    manifest = backup.verify_backup(path)  # damage can be detected without the key
    assert manifest["encrypted"] is True and sorted(p.name for p in path.iterdir()) == sorted([*manifest["files"], "manifest.json"])
    assert all(name.endswith(".enc") for name in manifest["files"])
    # Nothing readable is left on disk: not a lead's notes, not a transcript, not the uploaded file.
    for file in path.iterdir():
        if file.name != "manifest.json":
            content = file.read_bytes()
            assert b"pricing document" not in content and "تجربة".encode() not in content and b"bonjour" not in content and b"SQLite format" not in content
    backup.verify_backup(path, key=backup.parse_key(key))  # and with the key: it opens, and what it opens is what was sealed

    _destroy(settings)
    restored = backup.restore_backup(path, sealed_settings, replace_existing=True)
    assert restored["restored_rows"] == manifest["rows"] and manifest["rows"]["leads"] == 25 and manifest["rows"]["call_runs"] == 25
    db.reset_engine_for_tests(settings.database_url)
    with db.session_scope() as s:
        assert s.query(db.Lead).filter(db.Lead.notes == "تجربة – accentué").count() == 25
    assert (settings.data_dir / "uploads" / "1" / "1" / "7.pdf").read_bytes() == b"%PDF-1.4 the pricing document"
    assert not list(settings.data_dir.glob(".restore-*")) and not list(settings.data_dir.glob(".uploads-replaced-*"))  # no staging left behind


def test_an_encrypted_backup_cannot_be_read_without_its_key_and_fails_before_anything_is_touched(settings, sample_playbook, tmp_path):
    key = backup.new_key()
    _fill(settings, sample_playbook)
    path = backup.create_backup(tmp_path / "backups", settings.model_copy(update={"backup_encryption_key": key}))

    with pytest.raises(backup.BackupError, match="is encrypted"):
        backup.restore_backup(path, settings, replace_existing=True)
    with pytest.raises(backup.BackupError, match="not the one it was made with"):
        backup.restore_backup(path, settings.model_copy(update={"backup_encryption_key": backup.new_key()}), replace_existing=True)
    with pytest.raises(backup.BackupError, match="not a backup key"):
        backup.parse_key("hunter2")

    # Both refusals happened in staging: the live data is exactly as it was.
    db.reset_engine_for_tests(settings.database_url)
    with db.session_scope() as s:
        assert s.query(db.Lead).count() == 25
    assert (settings.data_dir / "uploads" / "1" / "1" / "7.pdf").exists()
    assert not list(settings.data_dir.glob(".restore-*"))


def test_an_encrypted_file_that_was_cut_short_or_changed_is_refused(tmp_path):
    key = backup.parse_key(backup.new_key())
    plain, sealed, opened = tmp_path / "plain", tmp_path / "sealed", tmp_path / "opened"
    plain.write_bytes(os.urandom(3 * (1 << 20) + 12345))  # several pieces and a short last one
    backup._encrypt_file(plain, sealed, key)
    backup._decrypt_file(sealed, opened, key)
    assert opened.read_bytes() == plain.read_bytes()

    whole = sealed.read_bytes()
    piece = 4 + (1 << 20) + 16  # one sealed piece: its length, the data, the authentication tag
    for name, damaged in [("cut after the first piece", whole[: 14 + piece]), ("one byte changed", whole[:5000] + bytes([whole[5000] ^ 1]) + whole[5001:]),
                          ("two pieces swapped", whole[:14] + whole[14 + piece : 14 + 2 * piece] + whole[14 : 14 + piece] + whole[14 + 2 * piece :])]:
        sealed.write_bytes(damaged)
        with pytest.raises(backup.BackupError):
            backup._decrypt_file(sealed, opened, key)

    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    backup._encrypt_file(empty, sealed, key)
    backup._decrypt_file(sealed, opened, key)
    assert opened.read_bytes() == b""


def test_old_backups_are_removed_but_never_the_newest_three(tmp_path):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    for days in (40, 30, 20, 10, 1):
        directory = tmp_path / f"backup-{(now - timedelta(days=days)).strftime('%Y%m%dT%H%M%SZ')}"
        directory.mkdir()
        (directory / "manifest.json").write_text("{}")
    (tmp_path / "not-a-backup").mkdir()
    removed = backup.prune_backups(tmp_path, keep_days=14)
    assert len(removed) == 2 and len(backup.list_backups(tmp_path)) == 3 and (tmp_path / "not-a-backup").exists()
    assert backup.prune_backups(tmp_path, keep_days=0) == []  # even "keep nothing" leaves the newest three


def test_a_scheduled_backup_is_taken_when_the_newest_is_too_old_and_not_before(settings, sample_playbook, tmp_path):
    _fill(settings, sample_playbook)
    scheduled = settings.model_copy(update={"backup_every_hours": 6, "backup_dir": tmp_path / "scheduled", "backup_encryption_key": backup.new_key()})
    assert backup.backup_if_due(settings) is None  # not switched on
    first = backup.backup_if_due(scheduled)
    assert first is not None and backup.verify_backup(first)["encrypted"] is True
    assert backup.backup_if_due(scheduled) is None  # one was just taken
    first.rename(first.with_name("backup-20200101T000000Z"))  # as if it were years old
    assert backup.backup_if_due(scheduled) is not None


def test_the_database_tools_must_be_of_the_servers_version(tmp_path, monkeypatch):
    """Found by the first CI run: newer tools write a backup an older server refuses to
    restore. The mismatch is refused when the backup is taken, with what to install."""

    def tool(directory, version):
        directory.mkdir(parents=True)
        path = directory / "pg_dump"
        path.write_text(f"#!/bin/sh\necho 'pg_dump (PostgreSQL) {version}'\n")
        path.chmod(0o755)
        return str(path)

    elsewhere = tool(tmp_path / "on-the-path", "17.2")  # what the shell finds first: a newer version
    monkeypatch.setattr(backup, "_PG_ROOT", str(tmp_path / "installed"))  # nothing of this machine's own tools is seen
    monkeypatch.setattr(backup.shutil, "which", lambda name: elsewhere)

    with pytest.raises(backup.BackupError, match=r"server is PostgreSQL 16, but the pg_dump on this machine is version 17.*postgresql-client-16"):
        backup._pg_tool("pg_dump", 16)
    assert backup._pg_tool("pg_dump", 17) == elsewhere

    matching = tool(tmp_path / "installed" / "16" / "bin", "16.15 (Ubuntu 16.15-0ubuntu0.24.04.1)")
    tool(tmp_path / "installed" / "18" / "bin", "18.0")
    assert backup._pg_tool("pg_dump", 16) == matching  # several installed: the server's own version is chosen, whatever the shell finds first

    monkeypatch.setattr(backup.shutil, "which", lambda name: None)
    monkeypatch.setattr(backup, "_PG_ROOT", str(tmp_path / "nothing-here"))
    with pytest.raises(backup.BackupError, match="was not found.*postgresql-client-16"):
        backup._pg_tool("pg_dump", 16)
