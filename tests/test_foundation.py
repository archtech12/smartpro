import sqlite3
import time
import csv

import pytest

from smartschool import database as db
from smartschool.security import hash_password, verify_password


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    return db.DB_PATH


def test_password_hashing_and_malformed_hashes():
    encoded = hash_password("a long test-only password")
    assert verify_password("a long test-only password", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("anything", "pbkdf2_sha256$999999999999$00$00")
    assert not verify_password("anything", "pbkdf2_sha256$310000$not-hex$also-not-hex")
    assert not verify_password("anything", "pbkdf2_sha256$310000$" + "00" * 16 + "$00")


def test_foreign_keys_and_versioned_schema(isolated_db):
    with db.connection() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert conn.execute("SELECT version FROM schema_migrations").fetchall()[0][0] == 1


def test_explicit_data_directory_must_be_absolute_and_contain_database(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTSCHOOL_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "missing" / "school.db")
    with pytest.raises(FileNotFoundError, match="no school.db exists"):
        db.validate_configured_data_dir()

    monkeypatch.setattr(db, "DB_PATH", isolated_db)
    db.validate_configured_data_dir()
    assert db.configured_data_dir() == tmp_path

    monkeypatch.setenv("SMARTSCHOOL_DATA_DIR", "relative-recovery-folder")
    with pytest.raises(ValueError, match="absolute folder path"):
        db.configured_data_dir()


def test_exclusive_database_lock_fails_with_bounded_wait(isolated_db):
    blocker = sqlite3.connect(isolated_db, isolation_level=None)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            db.list_rows_page("Students")
        elapsed = time.monotonic() - started
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert elapsed < 4
    with db.connection() as conn:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.SQLITE_BUSY_TIMEOUT_MS
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 0


def test_legacy_database_is_backed_up_before_additive_migration(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    path = tmp_path / "school.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    with sqlite3.connect(path) as old:
        old.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        old.execute("INSERT INTO users(username,password_hash,role) VALUES('legacy','hash','admin')")
    db.initialize()
    backups = list(tmp_path.glob("school.pre-migration-*.db"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT username FROM users").fetchone()[0] == "legacy"
    finally:
        backup.close()
    with db.connection() as conn:
        assert conn.execute("SELECT username FROM users").fetchone()[0] == "legacy"
        assert "locked_until" in {row[1] for row in conn.execute("PRAGMA table_info(users)")}


def test_failed_login_delays_and_service_role_checks(isolated_db):
    db.create_user("admin", hash_password("test administrator password"))
    db.create_user("teacher", hash_password("test teacher password"), "teacher")
    user, retry = db.authenticate("admin", "bad", verify_password)
    assert user is None and retry == 1
    with db.connection() as conn:
        conn.execute("DELETE FROM login_attempts")
        conn.execute("UPDATE users SET locked_until=NULL")
    assert db.authenticate("admin", "test administrator password", verify_password)[0]["role"] == "admin"
    with pytest.raises(PermissionError):
        db.add_row("Students", ["A1", "Test", "Student", "", "", ""], actor="teacher")
    assert db.add_row("Students", ["A1", "Test", "Student", "", "", ""], actor="admin")


def test_invalid_and_duplicate_records_rollback(isolated_db):
    db.create_user("admin", "test-hash")
    with pytest.raises(ValueError):
        db.add_row("Students", ["", "Missing", "", "", "", ""], actor="admin")
    db.add_row("Students", ["A1", "Test", "Student", "", "", ""], actor="admin")
    with pytest.raises(sqlite3.IntegrityError):
        db.add_row("Students", ["A1", "Another", "Student", "", "", ""], actor="admin")
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 1


def test_generic_crud_mutations_and_audit_are_atomic(isolated_db):
    db.create_user("admin", "test-hash")
    student_id = db.add_row("Students", ["A2", "Test", "Student", "", "", ""], actor="admin")
    with db.connection() as conn:
        event = conn.execute(
            "SELECT username,action,entity,entity_id FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert tuple(event) == ("admin", "CREATE", "Students", str(student_id))

    db.delete_row("Students", student_id, actor="admin")
    with db.connection() as conn:
        assert conn.execute("SELECT 1 FROM students WHERE id=?", (student_id,)).fetchone() is None
        event = conn.execute(
            "SELECT action,entity,entity_id FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert tuple(event) == ("DELETE", "Students", str(student_id))
    with pytest.raises(ValueError, match="no longer exists"):
        db.delete_row("Students", student_id, actor="admin")


def test_generic_crud_rolls_back_if_audit_fails(isolated_db):
    db.create_user("admin", "test-hash")

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic audit failure")

    original = db._audit_in_transaction
    db._audit_in_transaction = fail_audit
    try:
        with pytest.raises(sqlite3.OperationalError, match="audit failure"):
            db.add_row("Students", ["A3", "Test", "Student", "", "", ""], actor="admin")
    finally:
        db._audit_in_transaction = original
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 0


def test_generic_update_is_authorized_and_audited(isolated_db):
    db.create_user("admin", "test-hash")
    db.create_user("teacher", "test-hash", "teacher")
    student_id = db.add_row("Students", ["A4", "Old", "Name", "", "", ""], actor="admin")

    with pytest.raises(PermissionError):
        db.update_row("Students", student_id, ["A4", "Changed", "Name", "", "", ""], actor="teacher")
    db.update_row("Students", student_id, ["A4", "New", "Name", "", "", ""], actor="admin")

    with db.connection() as conn:
        student = conn.execute("SELECT first_name FROM students WHERE id=?", (student_id,)).fetchone()
        event = conn.execute(
            "SELECT action,entity,entity_id FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert student["first_name"] == "New"
    assert tuple(event) == ("UPDATE", "Students", str(student_id))


def test_generic_update_rolls_back_if_audit_fails(isolated_db, monkeypatch):
    db.create_user("admin", "test-hash")
    student_id = db.add_row("Students", ["A5", "Original", "Name", "", "", ""], actor="admin")

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic audit failure")

    monkeypatch.setattr(db, "_audit_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="audit failure"):
        db.update_row("Students", student_id, ["A5", "Changed", "Name", "", "", ""], actor="admin")
    with db.connection() as conn:
        student = conn.execute("SELECT first_name FROM students WHERE id=?", (student_id,)).fetchone()
    assert student["first_name"] == "Original"


def test_student_archive_restore_preserves_history_and_audits(isolated_db):
    db.create_user("admin", "test-hash")
    student_id = db.add_row("Students", ["ARC-1", "Archive", "Test", "", "", ""], actor="admin")
    with db.connection() as conn:
        session_id = conn.execute(
            "INSERT INTO academic_sessions(name,starts_on,ends_on) VALUES('Archive test','2026-01-01','2026-12-31')"
        ).lastrowid
        class_id = conn.execute("INSERT INTO classes(class_name) VALUES('Test class')").lastrowid
        conn.execute(
            "INSERT INTO student_enrollments(student_id,class_id,academic_session_id,status,enrolled_on) "
            "VALUES(?,?,?,'active','2026-02-01')", (student_id, class_id, session_id),
        )

    db.set_student_active(student_id, False, actor="admin")
    with db.connection() as conn:
        student = conn.execute("SELECT is_active FROM students WHERE id=?", (student_id,)).fetchone()
        enrollment_count = conn.execute(
            "SELECT COUNT(*) FROM student_enrollments WHERE student_id=?", (student_id,)
        ).fetchone()[0]
        event = conn.execute(
            "SELECT action,entity,entity_id FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert student["is_active"] == 0
    assert enrollment_count == 1
    assert tuple(event) == ("ARCHIVE", "Students", str(student_id))

    db.set_student_active(student_id, True, actor="admin")
    with db.connection() as conn:
        assert conn.execute("SELECT is_active FROM students WHERE id=?", (student_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM student_enrollments WHERE student_id=?", (student_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT action FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0] == "RESTORE"


def test_student_archive_rejects_unauthorized_missing_and_repeated_changes(isolated_db):
    db.create_user("admin", "test-hash")
    db.create_user("teacher", "test-hash", "teacher")
    student_id = db.add_row("Students", ["ARC-2", "Status", "Test", "", "", ""], actor="admin")
    with pytest.raises(PermissionError):
        db.set_student_active(student_id, False, actor="teacher")
    with pytest.raises(ValueError, match="already active"):
        db.set_student_active(student_id, True, actor="admin")
    with pytest.raises(ValueError, match="no longer exists"):
        db.set_student_active(999999, False, actor="admin")
    with db.connection() as conn:
        assert conn.execute("SELECT is_active FROM students WHERE id=?", (student_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='ARCHIVE'").fetchone()[0] == 0


def test_student_archive_rolls_back_if_audit_fails(isolated_db, monkeypatch):
    db.create_user("admin", "test-hash")
    student_id = db.add_row("Students", ["ARC-3", "Audit", "Failure", "", "", ""], actor="admin")

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic audit failure")

    monkeypatch.setattr(db, "_audit_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="audit failure"):
        db.set_student_active(student_id, False, actor="admin")
    with db.connection() as conn:
        assert conn.execute("SELECT is_active FROM students WHERE id=?", (student_id,)).fetchone()[0] == 1


def test_student_register_pages_filter_active_and_archived_students(isolated_db):
    db.create_user("admin", "test-hash")
    active_id = db.add_row("Students", ["FILTER-1", "Active", "Student", "", "", ""], actor="admin")
    archived_id = db.add_row("Students", ["FILTER-2", "Archived", "Student", "", "", ""], actor="admin")
    db.set_student_active(archived_id, False, actor="admin")

    active_rows, active_total = db.list_rows_page("Students", student_status="active")
    archived_rows, archived_total = db.list_rows_page("Students", student_status="archived")
    all_rows, all_total = db.list_rows_page("Students")
    assert active_total == 1 and [row["id"] for row in active_rows] == [active_id]
    assert archived_total == 1 and [row["id"] for row in archived_rows] == [archived_id]
    assert all_total == 2 and len(all_rows) == 2
    with pytest.raises(ValueError, match="Choose all students"):
        db.list_rows_page("Students", student_status="unknown")


def test_connection_rolls_back_when_operation_fails(isolated_db):
    with pytest.raises(RuntimeError):
        with db.connection() as conn:
            conn.execute("INSERT INTO users(username,password_hash,role) VALUES('rollback','hash','admin')")
            raise RuntimeError("force rollback")
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE username='rollback'").fetchone()[0] == 0


def test_failed_migration_rolls_back_schema_and_data(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    with sqlite3.connect(db.DB_PATH) as old:
        old.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        old.execute("INSERT INTO marker(value) VALUES('preserve')")

    def fail_after_ddl(conn):
        conn.execute("CREATE TABLE should_rollback(value TEXT)")
        raise RuntimeError("synthetic migration failure")

    monkeypatch.setattr(db, "_migration_1", fail_after_ddl)
    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT value FROM marker").fetchone()[0] == "preserve"
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='should_rollback'").fetchone() is None


def test_backup_copy_failure_preserves_database_and_removes_partial_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    path = tmp_path / "school.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    with sqlite3.connect(path) as old:
        old.execute("CREATE TABLE marker(value TEXT NOT NULL)")
        old.execute("INSERT INTO marker(value) VALUES('preserve')")

    def partial_copy_then_fail(source, destination):
        destination.write_bytes(b"partial backup")
        raise OSError("synthetic backup failure")

    monkeypatch.setattr(db.shutil, "copy2", partial_copy_then_fail)
    with pytest.raises(OSError, match="synthetic backup failure"):
        db.initialize()
    assert list(tmp_path.glob("school.pre-migration-*.db")) == []
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        assert conn.execute("SELECT value FROM marker").fetchone()[0] == "preserve"


def test_csv_export_neutralizes_spreadsheet_formulas(isolated_db, tmp_path):
    db.create_user("admin", "test-hash")
    db.add_row("Students", ["A-1", "=HYPERLINK(\"https://example.invalid\")", "@SUM(A1:A2)", "", "Class A", "+123456"], actor="admin")
    export_path = tmp_path / "students.csv"
    db.export_csv("Students", str(export_path))
    with export_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["admission_no", "first_name", "last_name", "gender", "class_name", "guardian_phone", "nin", "date_of_birth", "email"]
        row = next(reader)
    assert row["first_name"] == "'=HYPERLINK(\"https://example.invalid\")"
    assert row["last_name"] == "'@SUM(A1:A2)"
    assert row["guardian_phone"] == "'+123456"

    from smartschool.spreadsheet_files import read_rows
    excel_path = tmp_path / "students.xlsx"
    db.export_csv("Students", str(excel_path))
    headers, excel_rows = read_rows(excel_path)
    assert headers[0] == "admission_no"
    assert excel_rows[0]["admission_no"] == "A-1"
    assert excel_rows[0]["first_name"].startswith("'=")


def test_teacher_link_migration_preserves_accounts_unlinked_and_guards_roles(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    monkeypatch.setattr(db, "SCHEMA_VERSION", 17)
    db.initialize()
    db.create_user("admin", "admin-hash", "admin")
    db.create_user("legacy_teacher", "teacher-hash", "teacher")
    monkeypatch.setattr(db, "SCHEMA_VERSION", 18)
    db.initialize()
    teacher_id = db.add_row("Teachers", ["T-1", "Amina", "Staff", "", "", "Active"], actor="admin")
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 18
        assert conn.execute("SELECT teacher_id FROM users WHERE username='legacy_teacher'").fetchone()[0] is None
        with pytest.raises(sqlite3.IntegrityError, match="Only teacher accounts"):
            conn.execute(
                "INSERT INTO users(username,password_hash,role,teacher_id) VALUES('bad_link','hash','admin',?)",
                (teacher_id,),
            )
