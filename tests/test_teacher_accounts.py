import sqlite3

import pytest

from smartschool import database as db


@pytest.fixture
def accounts_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    db.create_user("admin", "admin-hash", "admin")
    db.create_user("unlinked_teacher", "teacher-hash", "teacher")
    teacher_id = db.add_row("Teachers", ["T-101", "Amina", "Okafor", "", "", "Active"], actor="admin")
    return {"teacher": teacher_id}


def test_create_and_link_teacher_logins_enforce_one_active_staff_link(accounts_db):
    teacher_id = accounts_db["teacher"]
    db.link_teacher_user("unlinked_teacher", teacher_id, "admin")
    row = next(account for account in db.list_user_accounts() if account["username"] == "unlinked_teacher")
    assert row["teacher_id"] == teacher_id
    assert row["staff_no"] == "T-101"
    with pytest.raises(ValueError, match="already linked"):
        db.create_teacher_user("second_teacher", "synthetic-hash", teacher_id, "admin")


def test_create_teacher_login_validates_role_staff_and_admin(accounts_db):
    teacher_id = accounts_db["teacher"]
    with pytest.raises(PermissionError):
        db.create_teacher_user("new_teacher", "synthetic-hash", teacher_id, "unlinked_teacher")
    inactive_id = db.add_row("Teachers", ["T-102", "Musa", "Staff", "", "", "Inactive"], actor="admin")
    with pytest.raises(ValueError, match="active teacher"):
        db.create_teacher_user("new_teacher", "synthetic-hash", inactive_id, "admin")
    db.create_teacher_user("new_teacher", "synthetic-hash", teacher_id, "admin")
    row = next(account for account in db.list_user_accounts() if account["username"] == "new_teacher")
    assert row["role"] == "teacher" and row["teacher_id"] == teacher_id


def test_teacher_account_reset_and_deactivation_are_audited(accounts_db):
    teacher_id = accounts_db["teacher"]
    db.create_teacher_user("staff_login", "old-hash", teacher_id, "admin")
    db.set_user_password("staff_login", "new-hash", "admin")
    db.set_user_active("staff_login", False, "admin")
    with db.connection() as conn:
        row = conn.execute("SELECT password_hash,is_active FROM users WHERE username='staff_login'").fetchone()
        events = conn.execute(
            "SELECT action FROM audit_log WHERE entity='users' ORDER BY id"
        ).fetchall()
    assert tuple(row) == ("new-hash", 0)
    assert [event[0] for event in events] == ["CREATE", "RESET_PASSWORD", "DEACTIVATE"]


def test_user_cannot_deactivate_the_last_active_administrator(accounts_db):
    with pytest.raises(ValueError, match="At least one active administrator"):
        db.set_user_active("admin", False, "admin")
    assert next(row for row in db.list_user_accounts() if row["username"] == "admin")["is_active"] == 1


def test_user_account_writes_roll_back_when_audit_fails(accounts_db, monkeypatch):
    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic account audit failure")

    monkeypatch.setattr(db, "_audit_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="account audit failure"):
        db.create_teacher_user("staff_login", "hash", accounts_db["teacher"], "admin")
    assert all(row["username"] != "staff_login" for row in db.list_user_accounts())


def test_teacher_link_migration_failure_rolls_back_column_and_triggers(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    monkeypatch.setattr(db, "SCHEMA_VERSION", 17)
    db.initialize()
    db.create_user("admin", "hash", "admin")
    monkeypatch.setattr(db, "SCHEMA_VERSION", 18)
    migration = db._migration_18

    def fail_after_teacher_link_migration(conn):
        migration(conn)
        raise RuntimeError("synthetic teacher-link migration failure")

    monkeypatch.setattr(db, "_migration_18", fail_after_teacher_link_migration)
    with pytest.raises(RuntimeError, match="teacher-link migration failure"):
        db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 17
        assert "teacher_id" not in {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='users_teacher_link_insert_guard'"
        ).fetchone() is None
