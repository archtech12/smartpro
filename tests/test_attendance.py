import sqlite3

import pytest

from smartschool import database as db


@pytest.fixture
def attendance_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    db.create_user("admin", "synthetic-test-hash")
    db.create_user("teacher", "synthetic-test-hash", "teacher")
    class_id = db.add_row("Classes", ["Year 5", "A", ""], actor="admin")
    db.save_class_educational_stage(class_id, "primary", "admin")
    other_class_id = db.add_row("Classes", ["Year 5", "B", ""], actor="admin")
    db.save_class_educational_stage(other_class_id, "primary", "admin")
    first_student = db.add_row("Students", ["S-101", "Synthetic", "One", "", "", ""], actor="admin")
    second_student = db.add_row("Students", ["S-102", "Synthetic", "Two", "", "", ""], actor="admin")
    session_id = db.save_academic_session(
        "2026/2027", "2026-09-01", "2027-07-31", True, "admin"
    )
    first_enrollment = db.create_student_enrollment(
        first_student, class_id, session_id, "2026-09-01", "admin"
    )
    second_enrollment = db.create_student_enrollment(
        second_student, class_id, session_id, "2026-09-01", "admin"
    )
    return {
        "class": class_id,
        "other_class": other_class_id,
        "session": session_id,
        "enrollments": (first_enrollment, second_enrollment),
    }


def test_attendance_keeps_unmarked_distinct_and_records_corrections(attendance_db):
    ids = attendance_db
    date = "2026-09-10"
    roster = db.load_attendance_register(ids["session"], ids["class"], date)
    assert roster["register_id"] is None
    assert [row["status"] for row in roster["students"]] == [None, None]

    register_id, changed = db.save_attendance_marks(
        ids["session"], ids["class"], date,
        {ids["enrollments"][0]: "present"}, "admin",
    )
    assert changed == 1
    current = db.load_attendance_register(ids["session"], ids["class"], date)
    assert current["register_id"] == register_id
    assert [row["status"] for row in current["students"]] == ["present", None]

    with pytest.raises(ValueError, match="reason"):
        db.save_attendance_marks(
            ids["session"], ids["class"], date,
            {ids["enrollments"][0]: "absent"}, "admin",
        )
    db.save_attendance_marks(
        ids["session"], ids["class"], date,
        {ids["enrollments"][0]: "absent", ids["enrollments"][1]: "present"},
        "admin", "Corrected after checking the paper register",
    )
    with db.connection() as conn:
        history = conn.execute(
            "SELECT previous_status,new_status,reason,actor FROM attendance_mark_history "
            "WHERE register_id=? ORDER BY id",
            (register_id,),
        ).fetchall()
    assert [tuple(row) for row in history] == [
        (None, "present", "", "admin"),
        ("present", "absent", "Corrected after checking the paper register", "admin"),
        (None, "present", "", "admin"),
    ]


def test_attendance_scope_date_status_and_role_are_validated(attendance_db):
    ids = attendance_db
    enrollment_id = ids["enrollments"][0]
    with pytest.raises(PermissionError):
        db.save_attendance_marks(
            ids["session"], ids["class"], "2026-09-10", {enrollment_id: "present"}, "teacher"
        )
    with pytest.raises(ValueError, match="present or absent"):
        db.save_attendance_marks(
            ids["session"], ids["class"], "2026-09-10", {enrollment_id: "late"}, "admin"
        )
    with pytest.raises(ValueError, match="not enrolled"):
        db.save_attendance_marks(
            ids["session"], ids["other_class"], "2026-09-10", {enrollment_id: "present"}, "admin"
        )
    with pytest.raises(ValueError, match="within the selected academic session"):
        db.load_attendance_register(ids["session"], ids["class"], "2027-08-01")
    assert db.load_attendance_register(ids["session"], ids["class"], "2026-09-10")["register_id"] is None


def _assign_attendance_teacher(ids):
    teacher_id = db.add_row(
        "Teachers", ["T-ATTEND", "Amina", "Teacher", "", "", "Active"], actor="admin"
    )
    db.link_teacher_user("teacher", teacher_id, "admin")
    subject_id = db.add_row("Subjects", ["ATT", "Attendance Scope", "Primary"], actor="admin")
    db.save_class_subject_assignment(
        ids["session"], ids["class"], subject_id, teacher_id, "admin"
    )
    return teacher_id


def test_linked_teacher_attendance_options_and_reads_are_assignment_scoped(attendance_db):
    ids = attendance_db
    _assign_attendance_teacher(ids)
    options = db.load_attendance_options("teacher")
    assert [row["id"] for row in options["sessions"]] == [ids["session"]]
    assert [row["id"] for row in options["classes_by_session"][ids["session"]]] == [ids["class"]]
    assert options["teacher_linked"] is True
    assert len(db.load_attendance_register(
        ids["session"], ids["class"], "2026-09-10", actor="teacher"
    )["students"]) == 2
    with pytest.raises(PermissionError, match="only for classes assigned"):
        db.load_attendance_register(
            ids["session"], ids["other_class"], "2026-09-10", actor="teacher"
        )
    with pytest.raises(PermissionError, match="only for classes assigned"):
        db.load_attendance_summary(
            ids["session"], ids["other_class"], "2026-09-01", "2026-09-30", actor="teacher"
        )


def test_linked_teacher_can_mark_and_correct_only_assigned_class_attendance(attendance_db):
    ids = attendance_db
    _assign_attendance_teacher(ids)
    register_id, changed = db.save_attendance_marks(
        ids["session"], ids["class"], "2026-09-10",
        {ids["enrollments"][0]: "present"}, "teacher",
    )
    assert changed == 1
    with pytest.raises(ValueError, match="reason"):
        db.save_attendance_marks(
            ids["session"], ids["class"], "2026-09-10",
            {ids["enrollments"][0]: "absent"}, "teacher",
        )
    db.save_attendance_marks(
        ids["session"], ids["class"], "2026-09-10",
        {ids["enrollments"][0]: "absent"}, "teacher", "Checked attendance sheet",
    )
    with pytest.raises(PermissionError, match="only for classes assigned"):
        db.save_attendance_marks(
            ids["session"], ids["other_class"], "2026-09-10",
            {ids["enrollments"][0]: "present"}, "teacher",
        )
    with db.connection() as conn:
        history = conn.execute(
            "SELECT previous_status,new_status,reason,actor FROM attendance_mark_history "
            "WHERE register_id=? ORDER BY id", (register_id,),
        ).fetchall()
    assert [(row["previous_status"], row["new_status"], row["reason"], row["actor"]) for row in history] == [
        (None, "present", "", "teacher"),
        ("present", "absent", "Checked attendance sheet", "teacher"),
    ]


def test_unlinked_teacher_has_no_attendance_scope(attendance_db):
    ids = attendance_db
    options = db.load_attendance_options("teacher")
    assert options["teacher_linked"] is False
    assert options["sessions"] == []
    with pytest.raises(PermissionError, match="active teacher record"):
        db.save_attendance_marks(
            ids["session"], ids["class"], "2026-09-10",
            {ids["enrollments"][0]: "present"}, "teacher",
        )


def test_attendance_summary_keeps_unmarked_and_validates_range(attendance_db):
    ids = attendance_db
    db.save_attendance_marks(
        ids["session"], ids["class"], "2026-09-10",
        {ids["enrollments"][0]: "present"}, "admin",
    )
    summary = db.load_attendance_summary(
        ids["session"], ids["class"], "2026-09-01", "2026-09-30"
    )
    assert summary == [{
        "attendance_date": "2026-09-10", "enrolled": 2,
        "present": 1, "absent": 0, "unmarked": 1,
    }]
    with pytest.raises(ValueError, match="not be after"):
        db.load_attendance_summary(
            ids["session"], ids["class"], "2026-09-30", "2026-09-01"
        )
    with pytest.raises(ValueError, match="within the selected academic session"):
        db.load_attendance_summary(
            ids["session"], ids["class"], "2027-08-01", "2027-08-02"
        )


def test_attendance_marks_and_audit_roll_back_together(attendance_db, monkeypatch):
    ids = attendance_db

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic audit failure")

    monkeypatch.setattr(db, "_audit_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="audit failure"):
        db.save_attendance_marks(
            ids["session"], ids["class"], "2026-09-10",
            {ids["enrollments"][0]: "present"}, "admin",
        )
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attendance_registers").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM attendance_marks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM attendance_mark_history").fetchone()[0] == 0


def test_attendance_history_is_append_only(attendance_db):
    ids = attendance_db
    db.save_attendance_marks(
        ids["session"], ids["class"], "2026-09-10",
        {ids["enrollments"][0]: "present"}, "admin",
    )
    with db.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE attendance_mark_history SET reason='changed' WHERE id=1")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM attendance_mark_history WHERE id=1")


def test_failed_attendance_migration_rolls_back_and_keeps_v7_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    conn = sqlite3.connect(db.DB_PATH)
    try:
        for migration in (
            db._migration_1, db._migration_2, db._migration_3, db._migration_4,
            db._migration_5, db._migration_6, db._migration_7,
        ):
            conn.execute("BEGIN IMMEDIATE")
            migration(conn)
            conn.commit()
    finally:
        conn.close()

    def fail_after_ddl(conn):
        conn.execute("CREATE TABLE should_rollback(value TEXT)")
        raise RuntimeError("synthetic attendance migration failure")

    monkeypatch.setattr(db, "_migration_8", fail_after_ddl)
    with pytest.raises(RuntimeError, match="attendance migration failure"):
        db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='should_rollback'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='attendance_registers'"
        ).fetchone() is None
    backups = list(tmp_path.glob("school.pre-migration-v7-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
