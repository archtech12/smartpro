import sqlite3

import pytest

from smartschool import database as db


@pytest.fixture
def assessment_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    db.create_user("admin", "test-hash", "admin")
    db.create_user("teacher_user", "test-hash", "teacher")
    class_id = db.add_row("Classes", ["Grade 5", "A", ""], actor="admin")
    db.save_class_educational_stage(class_id, "primary", "admin")
    subject_id = db.add_row("Subjects", ["ENG", "English", "Primary"], actor="admin")
    session_id = db.save_academic_session("2026/2027", "2026-09-01", "2027-07-31", True, "admin")
    term_id = db.save_term(session_id, "First Term", 1, "2026-09-01", "2026-12-20", "admin")
    assignment_id = db.save_class_subject_assignment(
        session_id, class_id, subject_id, None, "admin"
    )
    return {"session": session_id, "term": term_id, "assignment": assignment_id}


def test_assessment_components_are_term_scoped_and_decimal_exact(assessment_db):
    ids = assessment_db
    first = db.save_assessment_component(
        ids["assignment"], ids["term"], "Classwork", 1, "10.500", "admin"
    )
    second = db.save_assessment_component(
        ids["assignment"], ids["term"], "Examination", 2, "89.5", "admin"
    )
    data = db.load_assessment_setup_data(ids["session"], ids["assignment"], ids["term"])
    assert [row["id"] for row in data["components"]] == [first, second]
    assert [row["max_score"] for row in data["components"]] == ["10.5", "89.5"]
    assert db.validate_assessment_score("0.1", "0.3") == "0.1"
    assert db.validate_assessment_score("0.3", "0.3") == "0.3"


def test_assessment_components_reject_bad_bounds_duplicate_name_and_order(assessment_db):
    ids = assessment_db
    component_id = db.save_assessment_component(
        ids["assignment"], ids["term"], "Classwork", 1, "10", "admin"
    )
    with pytest.raises(ValueError, match="component order"):
        db.save_assessment_component(ids["assignment"], ids["term"], "Test", 1, "10", "admin")
    with pytest.raises(ValueError, match="component name"):
        db.save_assessment_component(ids["assignment"], ids["term"], "classwork", 2, "10", "admin")
    for invalid in ("0", "-1", "1000000.01", "NaN", "Infinity", "0.1234567890123", "not-a-number"):
        with pytest.raises(ValueError):
            db.save_assessment_component(
                ids["assignment"], ids["term"], f"Invalid {invalid}", 3, invalid, "admin"
            )
    with pytest.raises(ValueError, match="between zero"):
        db.validate_assessment_score("10.01", "10")
    with pytest.raises(ValueError, match="between zero"):
        db.validate_assessment_score("-0.01", "10")
    with pytest.raises(ValueError, match="decimal number"):
        db.validate_assessment_score(0.1, "1")
    assert len(db.load_assessment_setup_data(ids["session"], ids["assignment"], ids["term"])["components"]) == 1
    db.delete_assessment_component(component_id, "admin")


def test_assessment_components_require_matching_session_and_admin(assessment_db):
    ids = assessment_db
    other_session = db.save_academic_session("2027/2028", "2027-09-01", "2028-07-31", False, "admin")
    other_term = db.save_term(other_session, "First Term", 1, "2027-09-01", "2027-12-20", "admin")
    with db.connection() as conn:
        class_id = conn.execute("SELECT class_id FROM class_subject_assignments WHERE id=?", (ids["assignment"],)).fetchone()[0]
        subject_id = conn.execute("SELECT subject_id FROM class_subject_assignments WHERE id=?", (ids["assignment"],)).fetchone()[0]
    other_assignment = db.save_class_subject_assignment(
        other_session, class_id, subject_id, None, "admin"
    )
    with pytest.raises(ValueError, match="term from the assignment's session"):
        db.save_assessment_component(other_assignment, ids["term"], "Test", 1, "10", "admin")
    with pytest.raises(PermissionError):
        db.save_assessment_component(ids["assignment"], ids["term"], "Blocked", 1, "10", "teacher_user")
    assert db.load_assessment_setup_data(ids["session"], ids["assignment"], ids["term"])["components"] == []


def test_assessment_component_audit_failure_rolls_back(assessment_db, monkeypatch):
    ids = assessment_db

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic audit failure")

    monkeypatch.setattr(db, "_audit_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="audit failure"):
        db.save_assessment_component(ids["assignment"], ids["term"], "Classwork", 1, "10", "admin")
    assert db.load_assessment_setup_data(ids["session"], ids["assignment"], ids["term"])["components"] == []


def test_failed_v5_migration_rolls_back_and_keeps_v4_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    conn = sqlite3.connect(db.DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")
        db._migration_1(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        db._migration_2(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        db._migration_3(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        db._migration_4(conn)
        conn.commit()
    finally:
        conn.close()

    def fail_after_ddl(conn):
        conn.execute("CREATE TABLE should_rollback(value TEXT)")
        raise RuntimeError("synthetic v5 failure")

    monkeypatch.setattr(db, "_migration_5", fail_after_ddl)
    with pytest.raises(RuntimeError, match="synthetic v5 failure"):
        db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='assessment_components'").fetchone() is None
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='should_rollback'").fetchone() is None
    backups = list(tmp_path.glob("school.pre-migration-v4-*.db"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        backup.close()
