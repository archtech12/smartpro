import sqlite3

import pytest

from smartschool import database as db


@pytest.fixture
def calendar_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    db.create_user("admin", "test-hash", "admin")
    db.create_user("teacher", "test-hash", "teacher")
    return tmp_path


def _create_schema_v1(path):
    conn = sqlite3.connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        db._migration_1(conn)
        conn.commit()
    finally:
        conn.close()


def test_latest_migration_backs_up_v1_and_keeps_existing_data(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    _create_schema_v1(db.DB_PATH)
    with db.connection() as conn:
        conn.execute("INSERT INTO students(admission_no,first_name,last_name) VALUES('S-OLD','Legacy','Student')")
        conn.execute("INSERT INTO classes(class_name,section) VALUES('Legacy Class','A')")
        conn.execute("INSERT INTO subjects(subject_code,subject_name) VALUES('LEG','Legacy Subject')")

    db.initialize()

    backups = list(tmp_path.glob("school.pre-migration-v1-*.db"))
    assert len(backups) == 1
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")] == list(range(1, db.SCHEMA_VERSION + 1))
        assert conn.execute("SELECT admission_no FROM students").fetchone()[0] == "S-OLD"
        assert conn.execute("SELECT class_name FROM classes").fetchone()[0] == "Legacy Class"
        assert conn.execute("SELECT educational_stage FROM classes").fetchone()[0] == ""
        assert conn.execute("SELECT school_type FROM school_profile WHERE id=1").fetchone()[0] == ""
        assert conn.execute("SELECT subject_name FROM subjects").fetchone()[0] == "Legacy Subject"
        assert conn.execute("SELECT COUNT(*) FROM student_enrollments").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM terms WHERE is_current=1").fetchone()[0] == 0
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='class_subject_assignments'").fetchone()
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='assessment_components'").fetchone()
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    backup = sqlite3.connect(backups[0])
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT admission_no FROM students").fetchone()[0] == "S-OLD"
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        backup.close()


def test_sessions_terms_date_bounds_current_session_and_audit(calendar_db):
    first = db.save_academic_session("2025/2026", "2025-09-01", "2026-07-31", False, "admin")
    second = db.save_academic_session("2026/2027", "2026-09-01", "2027-07-31", True, "admin")
    term = db.save_term(second, "First Term", 1, "2026-09-01", "2026-12-20", "admin")

    assert [row["id"] for row in db.list_terms(second)] == [term]
    with pytest.raises(ValueError, match="within the selected academic session"):
        db.save_term(second, "Outside", 2, "2026-08-31", "2026-12-20", "admin")
    with pytest.raises(ValueError, match="cannot overlap"):
        db.save_term(second, "Overlapping", 2, "2026-12-20", "2027-01-20", "admin")
    with pytest.raises(ValueError, match="valid date"):
        db.save_academic_session("Bad date", "2026-02-30", "2026-07-31", False, "admin")
    with pytest.raises(ValueError, match="earlier"):
        db.save_term(second, "Same day", 2, "2026-12-20", "2026-12-20", "admin")
    with pytest.raises(ValueError, match="include all dates"):
        db.save_academic_session("2026/2027", "2026-10-01", "2027-07-31", True, "admin", second)

    db.save_term(second, "Updated First Term", 1, "2026-09-02", "2026-12-20", "admin", term)
    assert db.list_terms(second)[0]["term_name"] == "Updated First Term"
    db.save_academic_session("2025/2026", "2025-09-01", "2026-07-31", True, "admin", first)
    current = [row for row in db.list_academic_sessions() if row["is_current"]]
    assert len(current) == 1 and current[0]["id"] == first
    with db.connection() as conn:
        events = conn.execute("SELECT action,entity FROM audit_log ORDER BY id").fetchall()
    assert [(row["action"], row["entity"]) for row in events] == [
        ("CREATE", "academic_sessions"),
        ("CREATE", "academic_sessions"),
        ("CREATE", "terms"),
        ("UPDATE", "terms"),
        ("UPDATE", "academic_sessions"),
    ]


def test_unique_term_order_foreign_key_and_non_admin_denied(calendar_db):
    session = db.save_academic_session("2026/2027", "2026-09-01", "2027-07-31", True, "admin")
    db.save_term(session, "First Term", 1, "2026-09-01", "2026-12-20", "admin")
    with pytest.raises(sqlite3.IntegrityError):
        db.save_term(session, "Duplicate order", 1, "2027-01-01", "2027-04-01", "admin")
    with pytest.raises(PermissionError):
        db.save_academic_session("Blocked", "2027-09-01", "2028-07-31", False, "teacher")
    with pytest.raises(PermissionError):
        db.save_term(session, "Blocked", 2, "2027-01-01", "2027-04-01", "teacher")
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0] == 1


def test_active_session_and_term_can_be_changed_with_one_term_per_session(calendar_db):
    active_session = db.save_academic_session("2026/2027", "2026-09-01", "2027-07-31", True, "admin")
    other_session = db.save_academic_session("2027/2028", "2027-09-01", "2028-07-31", False, "admin")
    first = db.save_term(active_session, "First Term", 1, "2026-09-01", "2026-12-20", "admin")
    second = db.save_term(active_session, "Second Term", 2, "2027-01-01", "2027-04-20", "admin")
    other = db.save_term(other_session, "First Term", 1, "2027-09-01", "2027-12-20", "admin")

    db.set_current_term(first, "admin")
    db.set_current_term(second, "admin")
    db.set_current_term(other, "admin")
    db.set_current_academic_session(other_session, "admin")

    sessions = db.list_academic_sessions()
    assert sessions[0]["id"] == other_session and sessions[0]["is_current"] == 1
    assert sum(row["is_current"] for row in sessions) == 1
    assert {row["id"]: row["is_current"] for row in db.list_terms(active_session)} == {first: 0, second: 1}
    assert db.list_terms(other_session)[0]["id"] == other
    with pytest.raises(PermissionError):
        db.set_current_term(first, "teacher")
    with pytest.raises(PermissionError):
        db.set_current_academic_session(active_session, "teacher")

    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='SET_CURRENT'").fetchone()[0] == 4


def test_active_period_switches_roll_back_when_audit_fails(calendar_db, monkeypatch):
    current = db.save_academic_session("2026/2027", "2026-09-01", "2027-07-31", True, "admin")
    next_session = db.save_academic_session("2027/2028", "2027-09-01", "2028-07-31", False, "admin")
    active_term = db.save_term(current, "First Term", 1, "2026-09-01", "2026-12-20", "admin")
    next_term = db.save_term(next_session, "First Term", 1, "2027-09-01", "2027-12-20", "admin")
    db.set_current_term(active_term, "admin")

    def fail_audit(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic active-period audit failure")

    monkeypatch.setattr(db, "_audit_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="active-period audit failure"):
        db.set_current_academic_session(next_session, "admin")
    with pytest.raises(sqlite3.OperationalError, match="active-period audit failure"):
        db.set_current_term(next_term, "admin")

    assert [row["id"] for row in db.list_academic_sessions() if row["is_current"]] == [current]
    assert [row["id"] for row in db.list_terms(current) if row["is_current"]] == [active_term]
    assert [row["id"] for row in db.list_terms(next_session) if row["is_current"]] == []


def test_calendar_audit_failure_rolls_back_session(calendar_db, monkeypatch):
    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic audit failure")

    monkeypatch.setattr(db, "_audit_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="audit failure"):
        db.save_academic_session("2026/2027", "2026-09-01", "2027-07-31", True, "admin")
    assert db.list_academic_sessions() == []


def test_failed_v2_migration_rolls_back_and_retains_v1_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    _create_schema_v1(db.DB_PATH)

    def fail_after_ddl(conn):
        conn.execute("CREATE TABLE should_rollback(value TEXT)")
        raise RuntimeError("synthetic v2 failure")

    monkeypatch.setattr(db, "_migration_2", fail_after_ddl)
    with pytest.raises(RuntimeError, match="synthetic v2 failure"):
        db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='should_rollback'").fetchone() is None
        assert conn.execute("SELECT version FROM schema_migrations").fetchone()[0] == 1
    assert len(list(tmp_path.glob("school.pre-migration-v1-*.db"))) == 1
