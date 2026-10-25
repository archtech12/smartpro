import sqlite3

import pytest

from smartschool import database as db


@pytest.fixture
def assignment_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    db.create_user("admin", "test-hash", "admin")
    db.create_user("teacher_user", "test-hash", "teacher")
    class_id = db.add_row("Classes", ["Grade 4", "A", ""], actor="admin")
    db.save_class_educational_stage(class_id, "primary", "admin")
    subject_id = db.add_row("Subjects", ["MATH", "Mathematics", "Primary"], actor="admin")
    teacher_id = db.add_row("Teachers", ["T-001", "Amina", "Yusuf", "", "", "Active"], actor="admin")
    inactive_teacher_id = db.add_row(
        "Teachers", ["T-002", "Musa", "Bello", "", "", "Inactive"], actor="admin"
    )
    session_id = db.save_academic_session("2026/2027", "2026-09-01", "2027-07-31", True, "admin")
    return {
        "class": class_id,
        "subject": subject_id,
        "teacher": teacher_id,
        "inactive_teacher": inactive_teacher_id,
        "session": session_id,
    }


def test_assignment_create_update_and_delete_are_audited(assignment_db):
    ids = assignment_db
    assignment_id = db.save_class_subject_assignment(
        ids["session"], ids["class"], ids["subject"], None, "admin"
    )
    row = db.list_class_subject_assignments(ids["session"])[0]
    assert row["id"] == assignment_id
    assert row["teacher_id"] is None
    assert row["subject_name"] == "Mathematics"

    db.save_class_subject_assignment(
        ids["session"], ids["class"], ids["subject"], ids["teacher"], "admin", assignment_id
    )
    row = db.list_class_subject_assignments(ids["session"])[0]
    assert row["teacher_id"] == ids["teacher"]
    assert row["staff_no"] == "T-001"

    db.delete_class_subject_assignment(assignment_id, "admin")
    assert db.list_class_subject_assignments() == []
    with db.connection() as conn:
        assert [(row["action"], row["entity"]) for row in conn.execute(
            "SELECT action,entity FROM audit_log WHERE entity='class_subject_assignments' ORDER BY id"
        )] == [
            ("CREATE", "class_subject_assignments"),
            ("UPDATE", "class_subject_assignments"),
            ("DELETE", "class_subject_assignments"),
        ]


def test_bulk_subject_dialog_accepts_sqlite_rows(assignment_db, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from smartschool.academic_ui import BulkClassSubjectDialog

    app = QApplication.instance() or QApplication([])
    data = db.load_class_subject_page_data()
    dialog = BulkClassSubjectDialog(
        data["sessions"], data["classes"], data["subjects"], data["assignments"],
        data["teachers"], preferred_session_id=assignment_db["session"],
    )
    assert len(dialog.subject_checks) == 1
    assert dialog.subject_checks[0][1].text().startswith("MATH — Mathematics")
    dialog.close()
    app.processEvents()


def test_class_subject_page_renders_educational_stage(assignment_db, monkeypatch):
    import time

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    from smartschool.academic_ui import ClassSubjectAssignmentsPage

    ids = assignment_db
    db.save_class_subject_assignment(
        ids["session"], ids["class"], ids["subject"], ids["teacher"], "admin"
    )
    app = QApplication.instance() or QApplication([])
    page = ClassSubjectAssignmentsPage("admin", "admin")
    deadline = time.monotonic() + 5
    while page._workers and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(20)
    assert not page._workers
    assert page.table.rowCount() == 1
    assert page.table.item(0, 4).text() == "Primary"
    assert page.table.item(0, 7).text().startswith("T-001 — Yusuf, Amina")
    page.close()
    app.processEvents()


def test_assignment_rejects_duplicates_inactive_teachers_and_non_admin(assignment_db):
    ids = assignment_db
    db.save_class_subject_assignment(
        ids["session"], ids["class"], ids["subject"], None, "admin"
    )
    with pytest.raises(ValueError, match="already assigned"):
        db.save_class_subject_assignment(
            ids["session"], ids["class"], ids["subject"], ids["teacher"], "admin"
        )
    with pytest.raises(ValueError, match="active teacher"):
        db.save_class_subject_assignment(
            ids["session"], ids["class"], ids["subject"], ids["inactive_teacher"], "admin"
        )
    with pytest.raises(PermissionError):
        db.save_class_subject_assignment(
            ids["session"], ids["class"], ids["subject"], ids["teacher"], "teacher_user"
        )
    assert len(db.list_class_subject_assignments()) == 1


def test_bulk_subject_assignment_adds_checked_subjects_and_keeps_existing(assignment_db):
    ids = assignment_db
    second_subject = db.add_row("Subjects", ["ENG", "English", "Primary"], actor="admin")
    third_subject = db.add_row("Subjects", ["SCI", "Science", "Primary"], actor="admin")

    added = db.add_class_subject_assignments(
        ids["session"], ids["class"], [ids["subject"], second_subject], "admin"
    )
    assert added == 2
    assert db.add_class_subject_assignments(
        ids["session"], ids["class"], [second_subject, third_subject], "admin"
    ) == 1
    assert {row["subject_code"] for row in db.list_class_subject_assignments(ids["session"])} == {
        "MATH", "ENG", "SCI"
    }
    with db.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE entity='class_subject_assignments' AND action='CREATE'"
        ).fetchone()[0] == 3


def test_bulk_subject_assignment_can_assign_active_teacher(assignment_db):
    ids = assignment_db
    second_subject = db.add_row("Subjects", ["ENG", "English", "Primary"], actor="admin")
    assert db.add_class_subject_assignments(
        ids["session"], ids["class"], [ids["subject"], second_subject],
        "admin", ids["teacher"],
    ) == 2
    assignments = db.list_class_subject_assignments(ids["session"])
    assert {row["teacher_id"] for row in assignments} == {ids["teacher"]}

    third_subject = db.add_row("Subjects", ["SCI", "Science", "Primary"], actor="admin")
    with pytest.raises(ValueError, match="active teacher"):
        db.add_class_subject_assignments(
            ids["session"], ids["class"], [third_subject],
            "admin", ids["inactive_teacher"],
        )
    assert len(db.list_class_subject_assignments(ids["session"])) == 2


def test_bulk_subject_assignment_is_atomic_and_admin_only(assignment_db, monkeypatch):
    ids = assignment_db
    second_subject = db.add_row("Subjects", ["ENG", "English", "Primary"], actor="admin")
    original_audit = db._audit_in_transaction

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic audit failure")

    monkeypatch.setattr(db, "_audit_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="audit failure"):
        db.add_class_subject_assignments(
            ids["session"], ids["class"], [ids["subject"], second_subject], "admin"
        )
    assert db.list_class_subject_assignments() == []
    monkeypatch.setattr(db, "_audit_in_transaction", original_audit)
    with pytest.raises(PermissionError):
        db.add_class_subject_assignments(
            ids["session"], ids["class"], [ids["subject"]], "teacher_user"
        )
    assert db.list_class_subject_assignments() == []


def test_assignment_write_rolls_back_when_audit_fails(assignment_db, monkeypatch):
    ids = assignment_db

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic audit failure")

    monkeypatch.setattr(db, "_audit_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="audit failure"):
        db.save_class_subject_assignment(
            ids["session"], ids["class"], ids["subject"], ids["teacher"], "admin"
        )
    assert db.list_class_subject_assignments() == []


def test_failed_v4_migration_rolls_back_and_keeps_v3_backup(tmp_path, monkeypatch):
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
    finally:
        conn.close()

    def fail_after_ddl(conn):
        conn.execute("CREATE TABLE should_rollback(value TEXT)")
        raise RuntimeError("synthetic v4 failure")

    monkeypatch.setattr(db, "_migration_4", fail_after_ddl)
    with pytest.raises(RuntimeError, match="synthetic v4 failure"):
        db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='class_subject_assignments'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='should_rollback'"
        ).fetchone() is None
    backups = list(tmp_path.glob("school.pre-migration-v3-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 3
