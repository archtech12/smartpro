import sqlite3

import pytest

from smartschool import database as db


@pytest.fixture
def timetable_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    db.create_user("admin", "synthetic-test-hash")
    db.create_user("teacher", "synthetic-test-hash", "teacher")
    class_a = db.add_row("Classes", ["Year 6", "A", ""], actor="admin")
    class_b = db.add_row("Classes", ["Year 6", "B", ""], actor="admin")
    db.save_class_educational_stage(class_a, "primary", "admin")
    db.save_class_educational_stage(class_b, "primary", "admin")
    math_id = db.add_row("Subjects", ["MATH", "Mathematics", "Primary"], actor="admin")
    eng_id = db.add_row("Subjects", ["ENG", "English", "Primary"], actor="admin")
    teacher_a = db.add_row("Teachers", ["T-101", "Taylor", "One", "", "", "Active"], actor="admin")
    teacher_b = db.add_row("Teachers", ["T-102", "Taylor", "Two", "", "", "Active"], actor="admin")
    session_id = db.save_academic_session(
        "2026/2027", "2026-09-01", "2027-07-31", True, "admin"
    )
    assignments = {
        "a_math": db.save_class_subject_assignment(session_id, class_a, math_id, teacher_a, "admin"),
        "a_eng": db.save_class_subject_assignment(session_id, class_a, eng_id, teacher_b, "admin"),
        "b_math": db.save_class_subject_assignment(session_id, class_b, math_id, teacher_a, "admin"),
        "b_eng": db.save_class_subject_assignment(session_id, class_b, eng_id, teacher_b, "admin"),
    }
    return {"session": session_id, "assignments": assignments}


def test_timetable_blocks_class_teacher_and_room_overlaps(timetable_db):
    assignments = timetable_db["assignments"]
    first = db.save_timetable_entry(assignments["a_math"], 1, "09:00", "10:00", "Room 1", "admin")
    # Adjacent slots are allowed; the end boundary is not part of the previous slot.
    adjacent = db.save_timetable_entry(assignments["b_math"], 1, "10:00", "11:00", "Room 1", "admin")
    assert adjacent != first

    with pytest.raises(ValueError, match="class already"):
        db.save_timetable_entry(assignments["a_eng"], 1, "09:30", "10:30", "Room 2", "admin")
    with pytest.raises(ValueError, match="teacher already"):
        db.save_timetable_entry(assignments["b_math"], 1, "09:30", "10:30", "Room 2", "admin")
    with pytest.raises(ValueError, match="room or location"):
        db.save_timetable_entry(assignments["b_eng"], 1, "09:30", "10:30", " room 1 ", "admin")


def test_timetable_validation_updates_deletes_and_lists(timetable_db):
    assignments = timetable_db["assignments"]
    with pytest.raises(ValueError, match="HH:MM"):
        db.save_timetable_entry(assignments["a_math"], 1, "9:00", "10:00", "", "admin")
    with pytest.raises(ValueError, match="later than"):
        db.save_timetable_entry(assignments["a_math"], 1, "10:00", "09:00", "", "admin")
    with pytest.raises(ValueError, match="Monday through Sunday"):
        db.save_timetable_entry(assignments["a_math"], 8, "09:00", "10:00", "", "admin")
    entry_id = db.save_timetable_entry(assignments["a_math"], 2, "09:00", "10:00", "Lab", "admin")
    db.save_timetable_entry(assignments["a_math"], 2, "10:00", "11:00", "Lab", "admin", entry_id)
    rows = db.load_timetable_data(timetable_db["session"])["entries"]
    assert len(rows) == 1
    assert (rows[0]["starts_at"], rows[0]["ends_at"], rows[0]["location"]) == ("10:00", "11:00", "Lab")
    db.delete_timetable_entry(entry_id, "admin")
    assert db.load_timetable_data(timetable_db["session"])["entries"] == []


def test_timetable_print_data_requires_slots_and_includes_school_profile(timetable_db):
    ids = timetable_db
    with pytest.raises(ValueError, match="no timetable slots"):
        db.load_timetable_print_data(ids["session"])
    db.save_timetable_entry(ids["assignments"]["a_math"], 1, "09:00", "10:00", "Room 1", "admin")
    db.save_school_profile({"school_name": "Synthetic School"}, "admin")
    report = db.load_timetable_print_data(ids["session"])
    assert report["session_name"] == "2026/2027"
    assert report["school_profile"]["school_name"] == "Synthetic School"
    assert len(report["entries"]) == 1
    assert report["entries"][0]["subject_code"] == "MATH"


def test_timetable_requires_admin_and_audit_failure_rolls_back(timetable_db, monkeypatch):
    assignment_id = timetable_db["assignments"]["a_math"]
    with pytest.raises(PermissionError):
        db.save_timetable_entry(assignment_id, 1, "09:00", "10:00", "", "teacher")

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic audit failure")

    monkeypatch.setattr(db, "_audit_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="audit failure"):
        db.save_timetable_entry(assignment_id, 1, "09:00", "10:00", "", "admin")
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM timetable_entries").fetchone()[0] == 0


def test_failed_timetable_migration_rolls_back_and_keeps_v8_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    conn = sqlite3.connect(db.DB_PATH)
    try:
        for migration in (
            db._migration_1, db._migration_2, db._migration_3, db._migration_4,
            db._migration_5, db._migration_6, db._migration_7, db._migration_8,
        ):
            conn.execute("BEGIN IMMEDIATE")
            migration(conn)
            conn.commit()
    finally:
        conn.close()

    def fail_after_ddl(conn):
        conn.execute("CREATE TABLE should_rollback(value TEXT)")
        raise RuntimeError("synthetic timetable migration failure")

    monkeypatch.setattr(db, "_migration_9", fail_after_ddl)
    with pytest.raises(RuntimeError, match="timetable migration failure"):
        db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='should_rollback'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='timetable_entries'"
        ).fetchone() is None
    backups = list(tmp_path.glob("school.pre-migration-v8-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
