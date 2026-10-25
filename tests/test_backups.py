import sqlite3

import pytest

from smartschool import backups
from smartschool import database as db


@pytest.fixture
def backup_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    db.create_user("admin", "synthetic-test-hash")
    db.add_row("Students", ["TEST-1", "Synthetic", "Learner", "", "", ""], actor="admin")
    return db.DB_PATH


def test_create_backup_is_consistent_and_verified(backup_db, tmp_path):
    destination = tmp_path / "exports" / "school-copy.db"
    destination.parent.mkdir()

    result = backups.create_backup(destination)

    assert result.path == destination.resolve()
    assert result.schema_version == db.SCHEMA_VERSION
    assert result.integrity == "ok"
    with sqlite3.connect(destination) as conn:
        assert conn.execute("SELECT admission_no FROM students").fetchone()[0] == "TEST-1"
    assert backups.verify_backup(destination).schema_version == db.SCHEMA_VERSION


def test_backup_refuses_source_path_and_existing_destination(backup_db, tmp_path):
    with pytest.raises(ValueError, match="different file"):
        backups.create_backup(backup_db)

    destination = tmp_path / "existing.db"
    destination.write_bytes(b"preserve this existing file")
    with pytest.raises(FileExistsError):
        backups.create_backup(destination)
    assert destination.read_bytes() == b"preserve this existing file"


def test_backup_verification_rejects_a_non_database(tmp_path):
    invalid = tmp_path / "not-a-backup.db"
    invalid.write_bytes(b"not a sqlite database")

    with pytest.raises(sqlite3.DatabaseError):
        backups.verify_backup(invalid)


def test_failed_backup_verification_does_not_publish_partial_file(backup_db, tmp_path, monkeypatch):
    destination = tmp_path / "school-copy.db"

    def fail_verification(_path):
        raise sqlite3.DatabaseError("synthetic verification failure")

    monkeypatch.setattr(backups, "verify_backup", fail_verification)
    with pytest.raises(sqlite3.DatabaseError, match="synthetic verification failure"):
        backups.create_backup(destination)

    assert not destination.exists()
    assert list(tmp_path.glob(".school-copy.db.*.partial")) == []


def test_restore_creates_separate_verified_file_without_touching_source_or_active_db(
    backup_db, tmp_path, monkeypatch
):
    source = tmp_path / "school-backup.db"
    restored_path = tmp_path / "recovered-school.db"
    backups.create_backup(source)
    source_before = source.read_bytes()
    active_before = backup_db.read_bytes()

    restored = backups.restore_to_new_file(source, restored_path)

    assert restored.path == restored_path.resolve()
    assert restored.schema_version == db.SCHEMA_VERSION
    assert backups.verify_backup(restored_path).integrity == "ok"
    with sqlite3.connect(restored_path) as conn:
        assert conn.execute("SELECT admission_no FROM students").fetchone()[0] == "TEST-1"
    assert source.read_bytes() == source_before
    assert backup_db.read_bytes() == active_before

    # Open the restored file through the application's normal database connector.
    monkeypatch.setattr(db, "DB_PATH", restored_path)
    with db.connection() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("SELECT admission_no FROM students").fetchone()[0] == "TEST-1"
    db.add_row("Students", ["RESTORED-ONLY", "Recovered", "Record", "", "", ""], actor="admin")
    with sqlite3.connect(restored_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 2
    with sqlite3.connect(source) as conn:
        assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 1
    assert backup_db.read_bytes() == active_before


def test_restore_refuses_active_database_and_existing_target(backup_db, tmp_path):
    source = tmp_path / "school-backup.db"
    backups.create_backup(source)

    with pytest.raises(ValueError, match="separate"):
        backups.restore_to_new_file(source, backup_db)

    existing = tmp_path / "recovered.db"
    existing.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        backups.restore_to_new_file(source, existing)
    assert existing.read_bytes() == b"keep"


def test_backup_verification_rejects_foreign_key_violations(backup_db, tmp_path):
    invalid_backup = tmp_path / "broken-reference.db"
    backups.create_backup(invalid_backup)
    with sqlite3.connect(invalid_backup) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO student_enrollments "
            "(student_id,class_id,academic_session_id,status,enrolled_on) "
            "VALUES(999999,999999,999999,'active','2026-01-01')"
        )

    with pytest.raises(sqlite3.DatabaseError, match="foreign-key violation"):
        backups.verify_backup(invalid_backup)
    destination = tmp_path / "must-not-publish.db"
    with pytest.raises(sqlite3.DatabaseError, match="foreign-key violation"):
        backups.restore_to_new_file(invalid_backup, destination)
    assert not destination.exists()
    assert list(tmp_path.glob(".must-not-publish.db.*.partial")) == []
