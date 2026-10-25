import sqlite3

import pytest

from smartschool import database as db


@pytest.fixture
def grade_band_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    db.create_user("admin", "test-hash", "admin")
    db.create_user("teacher_user", "test-hash", "teacher")
    session_id = db.save_academic_session("2026/2027", "2026-09-01", "2027-07-31", True, "admin")
    return {"session": session_id}


def test_grade_bands_are_decimal_exact_and_scoped_by_session_and_stage(grade_band_db):
    session = grade_band_db["session"]
    zero = db.save_assessment_grade_band(session, "primary", "0.00", "F", "Needs work", "admin")
    middle = db.save_assessment_grade_band(session, "primary", "50.5", "C", "Pass", "admin")
    assert [row["threshold"] for row in db.load_grade_band_setup_data(session, "primary")["bands"]] == ["0", "50.5"]
    with pytest.raises(ValueError, match="Remove the other grade bands"):
        db.save_assessment_grade_band(session, "primary", "1", "F", "Needs work", "admin", zero)
    assert db.load_grade_band_setup_data(session, "secondary")["bands"] == []
    other_session = db.save_academic_session("2027/2028", "2027-09-01", "2028-07-31", False, "admin")
    assert db.load_grade_band_setup_data(other_session, "primary")["bands"] == []
    db.save_assessment_grade_band(session, "primary", "0", "F", "Updated remark", "admin", zero)
    assert db.load_grade_band_setup_data(session, "primary")["bands"][0]["remark"] == "Updated remark"
    db.delete_assessment_grade_band(middle, "admin")
    assert [row["id"] for row in db.load_grade_band_setup_data(session, "primary")["bands"]] == [zero]


def test_grade_bands_require_zero_anchor_unique_threshold_and_label(grade_band_db):
    session = grade_band_db["session"]
    with pytest.raises(ValueError, match="first grade band must start at 0"):
        db.save_assessment_grade_band(session, "secondary", "1", "A", "", "admin")
    zero = db.save_assessment_grade_band(session, "secondary", "0", "F", "", "admin")
    db.save_assessment_grade_band(session, "secondary", "50", "C", "", "admin")
    with pytest.raises(ValueError, match="threshold is already in use"):
        db.save_assessment_grade_band(session, "secondary", "50.0", "B", "", "admin")
    with pytest.raises(ValueError, match="grade label is already in use"):
        db.save_assessment_grade_band(session, "secondary", "75", "c", "", "admin")
    with pytest.raises(ValueError, match="Remove the other grade bands"):
        db.delete_assessment_grade_band(zero, "admin")
    assert len(db.load_grade_band_setup_data(session, "secondary")["bands"]) == 2


def test_grade_bands_reject_bad_input_and_non_admin(grade_band_db):
    session = grade_band_db["session"]
    for invalid in ("-0.01", "100.01", "NaN", "Infinity", "0.1234567890123", 0.1):
        with pytest.raises(ValueError):
            db.save_assessment_grade_band(session, "primary", invalid, "F", "", "admin")
    with pytest.raises(ValueError, match="Select Primary or Secondary"):
        db.save_assessment_grade_band(session, "middle", "0", "F", "", "admin")
    with pytest.raises(PermissionError):
        db.save_assessment_grade_band(session, "primary", "0", "F", "", "teacher_user")
    assert db.load_grade_band_setup_data(session, "primary")["bands"] == []


def test_grade_band_audit_failure_rolls_back(grade_band_db, monkeypatch):
    session = grade_band_db["session"]

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic audit failure")

    monkeypatch.setattr(db, "_audit_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="audit failure"):
        db.save_assessment_grade_band(session, "primary", "0", "F", "", "admin")
    assert db.load_grade_band_setup_data(session, "primary")["bands"] == []


def test_failed_v7_migration_rolls_back_and_keeps_v6_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    conn = sqlite3.connect(db.DB_PATH)
    try:
        for migration in (
            db._migration_1, db._migration_2, db._migration_3,
            db._migration_4, db._migration_5, db._migration_6,
        ):
            conn.execute("BEGIN IMMEDIATE")
            migration(conn)
            conn.commit()
    finally:
        conn.close()

    def fail_after_ddl(conn):
        conn.execute("CREATE TABLE should_rollback(value TEXT)")
        raise RuntimeError("synthetic v7 failure")

    monkeypatch.setattr(db, "_migration_7", fail_after_ddl)
    with pytest.raises(RuntimeError, match="synthetic v7 failure"):
        db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='assessment_grade_bands'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='should_rollback'"
        ).fetchone() is None
    backups = list(tmp_path.glob("school.pre-migration-v6-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 6
