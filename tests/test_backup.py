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
        s.add(db.DoNotCall(phone_e164="+14155559999", reason="asked"))
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
        assert db.is_dnc(s, "+14155559999")  # the suppression list survives: nobody who opted out is called after a restore
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
