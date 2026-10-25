import sqlite3

import pytest

from smartschool import database as db


@pytest.fixture
def stage_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    db.create_user("admin", "test-hash", "admin")
    db.create_user("teacher_user", "test-hash", "teacher")
    return db.add_row("Classes", ["Year 7", "A", ""], actor="admin")


def test_class_stage_is_explicit_and_audited(stage_db):
    class_id = stage_db
    assert db.list_class_stages()[0]["educational_stage"] == ""

    db.save_class_educational_stage(class_id, "secondary", "admin")
    db.save_class_educational_stage(class_id, "primary", "admin")
    row = db.list_class_stages()[0]
    assert row["educational_stage"] == "primary"
    with db.connection() as conn:
        events = conn.execute(
            "SELECT action,entity_id FROM audit_log WHERE entity='classes' ORDER BY id"
        ).fetchall()
    assert [(event["action"], event["entity_id"]) for event in events] == [
        ("SET_EDUCATIONAL_STAGE", str(class_id)),
        ("SET_EDUCATIONAL_STAGE", str(class_id)),
    ]


def test_class_stage_rejects_invalid_values_missing_class_and_non_admin(stage_db):
    class_id = stage_db
    for invalid in ("", "Primary", "secondary school", "tertiary", None):
        with pytest.raises(ValueError, match="Select Primary or Secondary"):
            db.save_class_educational_stage(class_id, invalid, "admin")
    with pytest.raises(PermissionError):
        db.save_class_educational_stage(class_id, "primary", "teacher_user")
    with pytest.raises(ValueError, match="not found"):
        db.save_class_educational_stage(999, "primary", "admin")
    assert db.list_class_stages()[0]["educational_stage"] == ""


def test_school_profile_type_limits_new_class_stage_assignments(stage_db):
    db.save_class_educational_stage(stage_db, "secondary", "admin")
    db.save_school_profile({"school_type": "primary"}, "admin")
    row = db.list_class_stages()[0]
    assert row["educational_stage"] == "secondary" and row["school_type"] == "primary"
    with pytest.raises(ValueError, match="Primary only"):
        db.save_class_educational_stage(stage_db, "secondary", "admin")
    db.save_class_educational_stage(stage_db, "primary", "admin")

    db.save_school_profile({"school_type": "combined"}, "admin")
    db.save_class_educational_stage(stage_db, "secondary", "admin")
    assert db.list_class_stages()[0]["educational_stage"] == "secondary"


def test_class_stage_audit_failure_rolls_back(stage_db, monkeypatch):
    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic audit failure")

    monkeypatch.setattr(db, "_audit_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="audit failure"):
        db.save_class_educational_stage(stage_db, "primary", "admin")
    assert db.list_class_stages()[0]["educational_stage"] == ""


def test_unclassified_class_cannot_receive_subject_or_enrollment(stage_db):
    class_id = stage_db
    session_id = db.save_academic_session("2026/2027", "2026-09-01", "2027-07-31", True, "admin")
    subject_id = db.add_row("Subjects", ["MATH", "Mathematics", "Primary"], actor="admin")
    with pytest.raises(ValueError, match="educational stage"):
        db.save_class_subject_assignment(session_id, class_id, subject_id, None, "admin")

    student_id = db.add_row("Students", ["S-001", "Test", "Student", "", "Year 7", ""], actor="admin")
    with pytest.raises(ValueError, match="educational stage"):
        db.map_legacy_class_to_enrollments("Year 7", class_id, session_id, "2026-09-01", "admin")
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM student_enrollments WHERE student_id=?", (student_id,)).fetchone()[0] == 0


def test_v5_classes_are_preserved_unclassified_when_migrating_to_v7(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    conn = sqlite3.connect(db.DB_PATH)
    try:
        for migration in (db._migration_1, db._migration_2, db._migration_3, db._migration_4, db._migration_5):
            conn.execute("BEGIN IMMEDIATE")
            migration(conn)
            conn.commit()
        conn.execute("INSERT INTO classes(class_name,section) VALUES('JSS 1','A')")
        conn.commit()
    finally:
        conn.close()

    db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        row = conn.execute("SELECT class_name,section,educational_stage FROM classes").fetchone()
        assert tuple(row) == ("JSS 1", "A", "")
    backups = list(tmp_path.glob("school.pre-migration-v5-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 5


def test_failed_v6_migration_rolls_back_and_keeps_v5_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    conn = sqlite3.connect(db.DB_PATH)
    try:
        for migration in (db._migration_1, db._migration_2, db._migration_3, db._migration_4, db._migration_5):
            conn.execute("BEGIN IMMEDIATE")
            migration(conn)
            conn.commit()
    finally:
        conn.close()

    def fail_after_alter(conn):
        conn.execute(
            "ALTER TABLE classes ADD COLUMN educational_stage TEXT NOT NULL DEFAULT '' "
            "CHECK(educational_stage IN ('','primary','secondary'))"
        )
        conn.execute("CREATE TABLE should_rollback(value TEXT)")
        raise RuntimeError("synthetic v6 failure")

    monkeypatch.setattr(db, "_migration_6", fail_after_alter)
    with pytest.raises(RuntimeError, match="synthetic v6 failure"):
        db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        columns = {row[1] for row in conn.execute("PRAGMA table_info(classes)")}
        assert "educational_stage" not in columns
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='should_rollback'").fetchone() is None
    backups = list(tmp_path.glob("school.pre-migration-v5-*.db"))
    assert len(backups) == 1
