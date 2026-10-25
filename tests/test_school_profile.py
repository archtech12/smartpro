import sqlite3
import base64

import pytest

from smartschool import database as db


@pytest.fixture
def school_profile_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    db.create_user("admin", "synthetic-hash")
    db.create_user("teacher", "synthetic-hash", "teacher")
    return tmp_path


def test_school_profile_saves_and_loads_with_admin_audit(school_profile_db):
    profile = {
        "school_name": "Synthetic Primary School",
        "address": "12 Example Road",
        "phone": "+1 555 0100",
        "email": "office@example.test",
        "head_name": "A. Administrator",
        "head_title": "Head Teacher",
        "class_teacher_name": "T. Teacher",
        "class_teacher_title": "Class Teacher",
        "school_type": "combined",
    }
    db.save_school_profile(profile, "admin")
    assert db.load_school_profile() == profile
    with db.connection() as conn:
        event = conn.execute(
            "SELECT action,entity,entity_id FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert tuple(event) == ("UPDATE", "school_profile", "1")


def test_school_profile_rejects_non_admin_invalid_email_and_overlong_values(school_profile_db):
    with pytest.raises(PermissionError):
        db.save_school_profile({"school_name": "No"}, "teacher")
    with pytest.raises(ValueError, match="valid email"):
        db.save_school_profile({"email": "bad-address"}, "admin")
    with pytest.raises(ValueError, match="120 characters"):
        db.save_school_profile({"school_name": "x" * 121}, "admin")
    with pytest.raises(ValueError, match="school type"):
        db.save_school_profile({"school_type": "tertiary"}, "admin")
    assert db.load_school_profile()["school_name"] == ""


def test_school_profile_audit_failure_rolls_back(school_profile_db, monkeypatch):
    def fail_audit(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic profile audit failure")

    monkeypatch.setattr(db, "_audit_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="profile audit failure"):
        db.save_school_profile({"school_name": "Should not persist"}, "admin")
    assert db.load_school_profile()["school_name"] == ""


def test_school_logo_is_stored_locally_and_validated(school_profile_db):
    logo = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aocAAAAASUVORK5CYII="
    )
    db.save_school_profile({"school_name": "Logo School"}, "admin", logo)
    assert db.load_school_logo() == logo
    with pytest.raises(ValueError, match="PNG or JPEG"):
        db.save_school_profile({"school_name": "Nope"}, "admin", b"not-an-image")
    with pytest.raises(ValueError, match="1 MB"):
        db.save_school_profile({"school_name": "Nope"}, "admin", b"\x89PNG\r\n\x1a\n" + b"0" * 1_000_001)
    assert db.load_school_profile()["school_name"] == "Logo School"


def test_school_profile_can_remove_existing_logo(school_profile_db):
    logo = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aocAAAAASUVORK5CYII="
    )
    db.save_school_profile({}, "admin", logo)
    db.save_school_profile({"school_name": "No Logo"}, "admin", None)
    assert db.load_school_logo() is None


def test_failed_v12_migration_rolls_back_and_keeps_v11_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    conn = sqlite3.connect(db.DB_PATH)
    try:
        for migration in (
            db._migration_1, db._migration_2, db._migration_3, db._migration_4,
            db._migration_5, db._migration_6, db._migration_7, db._migration_8,
            db._migration_9, db._migration_10, db._migration_11,
        ):
            conn.execute("BEGIN IMMEDIATE")
            migration(conn)
            conn.commit()
    finally:
        conn.close()

    migration_12 = db._migration_12

    def fail_after_ddl(conn):
        migration_12(conn)
        raise RuntimeError("synthetic v12 migration failure")

    monkeypatch.setattr(db, "_migration_12", fail_after_ddl)
    with pytest.raises(RuntimeError, match="v12 migration failure"):
        db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='school_profile'").fetchone() is None
    backups = list(tmp_path.glob("school.pre-migration-v11-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11


def test_failed_v13_migration_rolls_back_and_keeps_v12_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    conn = sqlite3.connect(db.DB_PATH)
    try:
        for migration in (
            db._migration_1, db._migration_2, db._migration_3, db._migration_4,
            db._migration_5, db._migration_6, db._migration_7, db._migration_8,
            db._migration_9, db._migration_10, db._migration_11, db._migration_12,
        ):
            conn.execute("BEGIN IMMEDIATE")
            migration(conn)
            conn.commit()
    finally:
        conn.close()

    migration_13 = db._migration_13

    def fail_after_ddl(conn):
        migration_13(conn)
        raise RuntimeError("synthetic v13 migration failure")

    monkeypatch.setattr(db, "_migration_13", fail_after_ddl)
    with pytest.raises(RuntimeError, match="v13 migration failure"):
        db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
        columns = {row[1] for row in conn.execute("PRAGMA table_info(school_profile)")}
        assert "logo_data" not in columns
    backups = list(tmp_path.glob("school.pre-migration-v12-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
