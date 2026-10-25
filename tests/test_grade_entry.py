import sqlite3
import os

import pytest

from smartschool import database as db


@pytest.fixture
def grade_entry_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    db.create_user("admin", "synthetic-hash")
    db.create_user("teacher", "synthetic-hash", "teacher")
    class_id = db.add_row("Classes", ["Grade 5", "A", ""], actor="admin")
    db.save_class_educational_stage(class_id, "primary", "admin")
    subject_id = db.add_row("Subjects", ["ENG", "English", "Primary"], actor="admin")
    session_id = db.save_academic_session("2026/2027", "2026-09-01", "2027-07-31", True, "admin")
    term_id = db.save_term(session_id, "First Term", 1, "2026-09-01", "2026-12-20", "admin")
    assignment_id = db.save_class_subject_assignment(session_id, class_id, subject_id, None, "admin")
    student_id = db.add_row("Students", ["S-501", "Synthetic", "Learner", "", "", ""], actor="admin")
    enrollment_id = db.create_student_enrollment(student_id, class_id, session_id, "2026-09-01", "admin")
    first = db.save_assessment_component(assignment_id, term_id, "Coursework", 1, "40", "admin")
    second = db.save_assessment_component(assignment_id, term_id, "Examination", 2, "60", "admin")
    low = db.save_assessment_grade_band(session_id, "primary", "0", "F", "Needs support", "admin")
    high = db.save_assessment_grade_band(session_id, "primary", "50", "P", "Pass", "admin")
    return {
        "tmp": tmp_path, "class": class_id, "session": session_id, "term": term_id, "assignment": assignment_id,
        "enrollment": enrollment_id, "components": (first, second), "bands": (low, high),
    }


def test_generic_calculation_is_decimal_and_missing_stays_incomplete():
    components = [
        {"id": 1, "max_score": "40"}, {"id": 2, "max_score": "60"},
    ]
    bands = [
        {"threshold": "0", "grade_label": "F", "remark": ""},
        {"threshold": "50", "grade_label": "P", "remark": "Pass"},
    ]
    incomplete = db.calculate_assessment_result(components, {1: "20"}, bands)
    assert incomplete["complete"] is False
    assert incomplete["points_earned"] == "20"
    assert incomplete["percentage"] is None
    result = db.calculate_assessment_result(components, {1: "20", 2: "60"}, bands)
    assert result == {
        "complete": True, "points_earned": "80", "points_possible": "100",
        "percentage": "80", "display_percentage": "80.00", "grade_label": "P", "remark": "Pass",
    }
    thirds = db.calculate_assessment_result(
        [{"id": 1, "max_score": "3"}], {1: "1"}, bands
    )
    assert thirds["display_percentage"] == "33.33"


def test_grade_entry_snapshots_policy_saves_drafts_and_locks_setup(grade_entry_db):
    ids = grade_entry_db
    initial = db.load_grade_entry_data(ids["session"], ids["assignment"], ids["term"])
    assert initial["snapshot_id"] is None
    assert len(initial["students"]) == 1
    snapshot_id, changed = db.save_assessment_scores(
        ids["assignment"], ids["term"],
        {ids["enrollment"]: {ids["components"][0]: "30", ids["components"][1]: None}}, "admin",
    )
    assert changed == 1
    data = db.load_grade_entry_data(ids["session"], ids["assignment"], ids["term"])
    assert data["snapshot_id"] == snapshot_id
    assert data["students"][0]["result"]["complete"] is False
    with pytest.raises(ValueError, match="components are locked"):
        db.save_assessment_component(ids["assignment"], ids["term"], "Quiz", 3, "10", "admin")
    with pytest.raises(ValueError, match="grade scale is locked"):
        db.save_assessment_grade_band(ids["session"], "primary", "75", "G", "", "admin")
    _, changed = db.save_assessment_scores(
        ids["assignment"], ids["term"],
        {ids["enrollment"]: {ids["components"][1]: "50"}}, "admin",
    )
    assert changed == 1
    result = db.load_grade_entry_data(ids["session"], ids["assignment"], ids["term"])["students"][0]["result"]
    assert result["complete"] is True
    assert result["points_earned"] == "80"
    assert result["grade_label"] == "P"
    db.save_assessment_scores(
        ids["assignment"], ids["term"],
        {ids["enrollment"]: {ids["components"][0]: "31"}}, "admin",
    )
    with db.connection() as conn:
        history = conn.execute(
            "SELECT previous_score,new_score FROM assessment_score_history ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in history] == [(None, "30"), (None, "50"), ("30", "31")]
        assert conn.execute("SELECT policy_json FROM assessment_policy_snapshots WHERE id=?", (snapshot_id,)).fetchone()


def test_grade_entry_validates_role_roster_and_rolls_back_policy_on_bad_score(grade_entry_db):
    ids = grade_entry_db
    with pytest.raises(PermissionError):
        db.save_assessment_scores(
            ids["assignment"], ids["term"], {ids["enrollment"]: {ids["components"][0]: "10"}}, "teacher"
        )
    with pytest.raises(ValueError, match="maximum score"):
        db.save_assessment_scores(
            ids["assignment"], ids["term"], {ids["enrollment"]: {ids["components"][0]: "41"}}, "admin"
        )
    with pytest.raises(ValueError, match="not enrolled"):
        db.save_assessment_scores(
            ids["assignment"], ids["term"], {9999: {ids["components"][0]: "10"}}, "admin"
        )
    assert db.load_grade_entry_data(ids["session"], ids["assignment"], ids["term"])["snapshot_id"] is None


def test_ca_exam_setup_is_admin_only_atomic_and_uses_school_maxima(grade_entry_db, monkeypatch):
    ids = grade_entry_db
    class_id = db.add_row("Classes", ["Grade 6", "A", ""], actor="admin")
    db.save_class_educational_stage(class_id, "primary", "admin")
    subject_id = db.add_row("Subjects", ["SCI", "Science", "Primary"], actor="admin")
    assignment_id = db.save_class_subject_assignment(
        ids["session"], class_id, subject_id, None, "admin"
    )

    with pytest.raises(PermissionError):
        db.setup_ca_exam_components(assignment_id, ids["term"], "40", "60", "teacher")

    original_audit = db._audit_in_transaction
    audit_calls = 0

    def fail_second_audit(*args, **kwargs):
        nonlocal audit_calls
        audit_calls += 1
        if audit_calls == 2:
            raise sqlite3.OperationalError("synthetic second component audit failure")
        return original_audit(*args, **kwargs)

    monkeypatch.setattr(db, "_audit_in_transaction", fail_second_audit)
    with pytest.raises(sqlite3.OperationalError, match="second component audit failure"):
        db.setup_ca_exam_components(assignment_id, ids["term"], "25", "75", "admin")
    with db.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM assessment_components WHERE class_subject_assignment_id=? AND term_id=?",
            (assignment_id, ids["term"]),
        ).fetchone()[0] == 0

    monkeypatch.setattr(db, "_audit_in_transaction", original_audit)
    component_ids = db.setup_ca_exam_components(
        assignment_id, ids["term"], "25", "75", "admin"
    )
    data = db.load_grade_entry_data(ids["session"], assignment_id, ids["term"], actor="admin")
    assert component_ids == tuple(item["id"] for item in data["components"])
    assert [(item["component_name"], item["max_score"]) for item in data["components"]] == [
        ("CA Score", "25"), ("Exam Score", "75"),
    ]


def test_linked_teacher_can_load_and_save_only_assigned_draft_scores(grade_entry_db):
    ids = grade_entry_db
    teacher_id = db.add_row("Teachers", ["T-GRADE", "Amina", "Teacher", "", "", "Active"], actor="admin")
    db.link_teacher_user("teacher", teacher_id, "admin")
    db.save_class_subject_assignment(
        ids["session"], ids["class"],
        int(db.list_class_subject_assignments(ids["session"])[0]["subject_id"]),
        teacher_id, "admin", int(ids["assignment"]),
    )
    teacher_data = db.load_grade_entry_data(
        ids["session"], ids["assignment"], ids["term"], actor="teacher"
    )
    assert teacher_data["teacher_linked"] is True
    assert [row["id"] for row in teacher_data["assignments"]] == [ids["assignment"]]
    snapshot_id, changed = db.save_assessment_scores(
        ids["assignment"], ids["term"],
        {ids["enrollment"]: {ids["components"][0]: "30"}}, "teacher",
    )
    assert snapshot_id > 0 and changed == 1

    other_class = db.add_row("Classes", ["Grade 5", "B", ""], actor="admin")
    db.save_class_educational_stage(other_class, "primary", "admin")
    with db.connection() as conn:
        subject_id = int(conn.execute("SELECT subject_id FROM class_subject_assignments WHERE id=?", (ids["assignment"],)).fetchone()[0])
    other_assignment = db.save_class_subject_assignment(
        ids["session"], other_class, subject_id, None, "admin"
    )
    with pytest.raises(PermissionError, match="only open grade entry"):
        db.load_grade_entry_data(
            ids["session"], other_assignment, ids["term"], actor="teacher"
        )
    with pytest.raises(PermissionError, match="only enter scores for subjects assigned"):
        db.save_assessment_scores(
            other_assignment, ids["term"],
            {ids["enrollment"]: {ids["components"][0]: "10"}}, "teacher",
        )


def test_unlinked_teacher_cannot_load_an_assignment_or_save_scores(grade_entry_db):
    ids = grade_entry_db
    data = db.load_grade_entry_data(actor="teacher")
    assert data["teacher_linked"] is False
    assert data["sessions"] == [] and data["assignments"] == []
    with pytest.raises(PermissionError, match="only open grade entry"):
        db.load_grade_entry_data(ids["session"], ids["assignment"], ids["term"], actor="teacher")
    with pytest.raises(PermissionError, match="Link this teacher login"):
        db.save_assessment_scores(
            ids["assignment"], ids["term"],
            {ids["enrollment"]: {ids["components"][0]: "10"}}, "teacher",
        )


def test_grade_entry_headers_show_ca_exam_component_maxima(grade_entry_db, monkeypatch):
    import time
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    from smartschool.assessment_ui import GradeEntryPage

    ids = grade_entry_db
    db.save_assessment_component(ids["assignment"], ids["term"], "CA Score", 1, "25", "admin", ids["components"][0])
    db.save_assessment_component(ids["assignment"], ids["term"], "Exam Score", 2, "75", "admin", ids["components"][1])
    app = QApplication.instance() or QApplication([])
    page = GradeEntryPage("admin", "admin")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and page.table.rowCount() == 0:
        app.processEvents()
        QTest.qWait(20)
    assert page.session_filter.currentData() == ids["session"]
    assert page.class_filter.currentData() == ids["class"]
    assert page.assignment_filter.currentData() == ids["assignment"]
    assert page.term_filter.currentData() == ids["term"]
    assert page.table.rowCount() == 1
    assert page.reports_button.menu().actions()[0].text() == "Student report card PDF"
    assert page.layout().stretch(page.layout().indexOf(page.table)) == 1
    assert page.roster_count.text() == "1 student(s)"
    page.student_search.setText("not-a-student")
    assert page.table.rowCount() == 1  # Filtering keeps score-row indexes stable.
    assert page.table.isRowHidden(0)
    assert page.roster_count.text() == "0 of 1 student(s)"
    page.student_search.setText("S-501")
    assert not page.table.isRowHidden(0)
    page.student_search.clear()
    headers = [page.table.horizontalHeaderItem(index).text() for index in range(page.table.columnCount())]
    assert "CA Score (Max 25)" in headers
    assert "Exam Score (Max 75)" in headers
    assert ["Total", "Max Points", "Percent", "Grade", "Status"] == headers[-5:]
    assert page.table.horizontalHeaderItem(headers.index("CA Score (Max 25)")).toolTip() == "Enter a score from 0 to 25 points."
    assert page.table.horizontalHeaderItem(headers.index("Total")).toolTip() == "Sum of entered component scores. Updates as you type."

    page.table.item(0, headers.index("CA Score (Max 25)")).setText("20")
    page.table.item(0, headers.index("Exam Score (Max 75)")).setText("60")
    result_col = headers.index("Total")
    assert page.table.item(0, result_col).text() == "80"
    assert page.table.item(0, result_col + 1).text() == "100"
    assert page.table.item(0, result_col + 2).text() == "80.00"
    assert page.table.item(0, result_col + 3).text() == "P"
    assert page.table.item(0, result_col + 4).text() == "Complete draft"
    page.close()
    app.processEvents()


def test_grade_entry_auto_loads_default_ca_exam_columns_and_roster(grade_entry_db, monkeypatch):
    import time
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    from smartschool.assessment_ui import GradeEntryPage

    ids = grade_entry_db
    subject_id = db.add_row("Subjects", ["SCI", "Science", "Primary"], actor="admin")
    assignment_id = db.save_class_subject_assignment(
        ids["session"], ids["class"], subject_id, None, "admin"
    )
    app = QApplication.instance() or QApplication([])
    page = GradeEntryPage("admin", "admin")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and page.assignment_filter.findData(assignment_id) < 0:
        app.processEvents()
        QTest.qWait(20)
    index = page.assignment_filter.findData(assignment_id)
    assert index >= 0
    page.assignment_filter.setCurrentIndex(index)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(
        "CA Score (Max 40)" in page.table.horizontalHeaderItem(i).text()
        for i in range(page.table.columnCount())
    ):
        app.processEvents()
        QTest.qWait(20)

    headers = [page.table.horizontalHeaderItem(i).text() for i in range(page.table.columnCount())]
    assert page.table.rowCount() == 1
    assert headers[:2] == ["Admission Number", "Student Name"]
    assert "CA Score (Max 40)" in headers
    assert "Exam Score (Max 60)" in headers
    assert "Total" in headers and "Max Points" in headers and "Percent" in headers and "Grade" in headers
    assert page.table.rowCount() == 1
    configured = db.load_grade_entry_data(ids["session"], assignment_id, ids["term"], actor="admin")
    assert [row["component_name"] for row in configured["components"]] == ["CA Score", "Exam Score"]
    page.close()
    app.processEvents()


def test_linked_teacher_cannot_approve_or_correct_approved_results(grade_entry_db):
    ids = grade_entry_db
    teacher_id = db.add_row("Teachers", ["T-APPROVE", "Amina", "Teacher", "", "", "Active"], actor="admin")
    db.link_teacher_user("teacher", teacher_id, "admin")
    assignment = db.list_class_subject_assignments(ids["session"])[0]
    db.save_class_subject_assignment(
        ids["session"], ids["class"], int(assignment["subject_id"]), teacher_id,
        "admin", ids["assignment"],
    )
    snapshot_id, _ = db.save_assessment_scores(
        ids["assignment"], ids["term"],
        {ids["enrollment"]: {ids["components"][0]: "30", ids["components"][1]: "50"}},
        "teacher",
    )
    with pytest.raises(PermissionError):
        db.approve_assessment_results(snapshot_id, "teacher")
    db.approve_assessment_results(snapshot_id, "admin")
    with pytest.raises(PermissionError, match="draft scores only"):
        db.save_assessment_scores(
            ids["assignment"], ids["term"],
            {ids["enrollment"]: {ids["components"][0]: "31"}}, "teacher", "correction reason",
        )


def test_grade_entry_audit_failure_rolls_back_snapshot_score_and_history(grade_entry_db, monkeypatch):
    ids = grade_entry_db

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic assessment audit failure")

    monkeypatch.setattr(db, "_audit_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="assessment audit failure"):
        db.save_assessment_scores(
            ids["assignment"], ids["term"], {ids["enrollment"]: {ids["components"][0]: "10"}}, "admin"
        )
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM assessment_policy_snapshots").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM assessment_scores").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM assessment_score_history").fetchone()[0] == 0


def test_snapshots_and_score_history_are_immutable(grade_entry_db):
    ids = grade_entry_db
    snapshot_id, _ = db.save_assessment_scores(
        ids["assignment"], ids["term"], {ids["enrollment"]: {ids["components"][0]: "10"}}, "admin"
    )
    with db.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE assessment_policy_snapshots SET policy_json='{}' WHERE id=?", (snapshot_id,))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM assessment_score_history WHERE id=1")


def test_approval_requires_complete_roster_locks_scores_and_allows_reasoned_correction(grade_entry_db):
    ids = grade_entry_db
    snapshot_id, _ = db.save_assessment_scores(
        ids["assignment"], ids["term"],
        {ids["enrollment"]: {ids["components"][0]: "30"}}, "admin",
    )
    with pytest.raises(ValueError, match="incomplete"):
        db.approve_assessment_results(snapshot_id, "admin")
    db.save_assessment_scores(
        ids["assignment"], ids["term"],
        {ids["enrollment"]: {ids["components"][1]: "50"}}, "admin",
    )
    approved_by, approved_at = db.approve_assessment_results(snapshot_id, "admin")
    assert approved_by == "admin" and approved_at
    assert db.load_grade_entry_data(ids["session"], ids["assignment"], ids["term"])["approval"]["approved_by"] == "admin"
    with pytest.raises(ValueError, match="reason is required"):
        db.save_assessment_scores(
            ids["assignment"], ids["term"],
            {ids["enrollment"]: {ids["components"][0]: "31"}}, "admin",
        )
    with pytest.raises(PermissionError):
        db.save_assessment_scores(
            ids["assignment"], ids["term"],
            {ids["enrollment"]: {ids["components"][0]: "31"}}, "teacher", "Verified correction",
        )
    db.save_assessment_scores(
        ids["assignment"], ids["term"],
        {ids["enrollment"]: {ids["components"][0]: "31"}}, "admin", "Verified against signed paper record",
    )
    with db.connection() as conn:
        history = conn.execute(
            "SELECT previous_score,new_score,reason FROM assessment_score_history ORDER BY id"
        ).fetchall()
        assert tuple(history[-1]) == ("30", "31", "Verified against signed paper record")
        with pytest.raises(sqlite3.IntegrityError, match="locked"):
            conn.execute("DELETE FROM assessment_result_approvals WHERE snapshot_id=?", (snapshot_id,))
    with pytest.raises(ValueError, match="already approved"):
        db.approve_assessment_results(snapshot_id, "admin")


def test_result_approval_audit_failure_rolls_back(grade_entry_db, monkeypatch):
    ids = grade_entry_db
    snapshot_id, _ = db.save_assessment_scores(
        ids["assignment"], ids["term"],
        {ids["enrollment"]: {ids["components"][0]: "30", ids["components"][1]: "50"}}, "admin",
    )

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic approval audit failure")

    monkeypatch.setattr(db, "_audit_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="approval audit failure"):
        db.approve_assessment_results(snapshot_id, "admin")
    assert db.load_grade_entry_data(ids["session"], ids["assignment"], ids["term"])["approval"] is None


def test_report_card_requires_approved_complete_results(grade_entry_db):
    ids = grade_entry_db
    db.save_school_profile({"school_name": "Synthetic School"}, "admin")
    with pytest.raises(ValueError, match="not approved"):
        db.load_report_card_data(ids["session"], ids["class"], ids["term"], ids["enrollment"])
    snapshot_id, _ = db.save_assessment_scores(
        ids["assignment"], ids["term"],
        {ids["enrollment"]: {ids["components"][0]: "30", ids["components"][1]: "50"}}, "admin",
    )
    db.approve_assessment_results(snapshot_id, "admin")
    report = db.load_report_card_data(ids["session"], ids["class"], ids["term"], ids["enrollment"])
    assert report["approval_status"] == "Approved"
    assert report["school_profile"]["school_name"] == "Synthetic School"
    assert report["student"]["admission_no"] == "S-501"
    assert report["subjects"] == [{
        "subject_code": "ENG", "subject_name": "English", "complete": True,
        "points_earned": "80", "points_possible": "100", "percentage": "80",
        "display_percentage": "80.00", "grade_label": "P", "remark": "Pass",
    }]


def test_student_profile_loads_history_and_is_admin_only(grade_entry_db):
    ids = grade_entry_db
    with db.connection() as conn:
        student_id = int(conn.execute(
            "SELECT student_id FROM student_enrollments WHERE id=?", (ids["enrollment"],)
        ).fetchone()[0])
    profile = db.load_student_profile_data(student_id, "admin")
    assert profile["student"]["admission_no"] == "S-501"
    assert profile["student"]["nin"] == ""
    assert profile["student"]["date_of_birth"] == ""
    assert profile["student"]["email"] == ""
    assert profile["enrollments"][0]["session_name"] == "2026/2027"
    assert profile["approved_results"] == []
    assert profile["attendance"] == {"present": 0, "absent": 0}
    assert profile["invoices"] == []
    with pytest.raises(PermissionError):
        db.load_student_profile_data(student_id, "teacher")


def test_student_profile_dialog_loads_async(grade_entry_db):
    import os
    import time
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    from smartschool.ui import StudentProfileDialog

    ids = grade_entry_db
    with db.connection() as conn:
        student_id = int(conn.execute(
            "SELECT student_id FROM student_enrollments WHERE id=?", (ids["enrollment"],)
        ).fetchone()[0])
    app = QApplication.instance() or QApplication([])
    dialog = StudentProfileDialog(student_id, "admin")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and dialog.heading.text() == "Loading student profile…":
        app.processEvents()
        QTest.qWait(20)
    assert "S-501" in dialog.heading.text()
    assert dialog.tables["enrollments"].rowCount() == 1
    assert "Attendance marks: 0 present, 0 absent" in dialog.summary.text()
    dialog.close()


def _synthetic_png(path):
    from PyQt6.QtGui import QImage
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(0xFFFFFFFF)
    assert image.save(str(path), "PNG")
    return path.read_bytes()


def test_optional_student_photo_and_class_report_cards_pdf(grade_entry_db):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtPdf import QPdfDocument
    from smartschool.assessment_ui import GradeEntryPage

    ids = grade_entry_db
    app = QApplication.instance() or QApplication([])
    second_student = db.add_row("Students", ["S-502", "Synthetic", "Second", "", "", ""], actor="admin")
    second_enrollment = db.create_student_enrollment(
        second_student, ids["class"], ids["session"], "2026-09-01", "admin"
    )
    first_photo = _synthetic_png(grade_entry_db["tmp"] / "student.png")
    with db.connection() as conn:
        first_student = int(conn.execute(
            "SELECT student_id FROM student_enrollments WHERE id=?", (ids["enrollment"],)
        ).fetchone()[0])
    db.set_student_photo(first_student, first_photo, "admin")
    with pytest.raises(PermissionError):
        db.set_student_photo(second_student, first_photo, "teacher")
    snapshot_id, _ = db.save_assessment_scores(
        ids["assignment"], ids["term"],
        {
            ids["enrollment"]: {ids["components"][0]: "30", ids["components"][1]: "50"},
            second_enrollment: {ids["components"][0]: "25", ids["components"][1]: "45"},
        }, "admin",
    )
    db.approve_assessment_results(snapshot_id, "admin")
    data = db.load_class_report_cards_data(ids["session"], ids["class"], ids["term"])
    assert len(data["report_cards"]) == 2
    assert data["report_cards"][0]["student"]["photo_data"] == first_photo

    path = str(grade_entry_db["tmp"] / "class-report-cards.pdf")
    GradeEntryPage._render_class_report_cards_pdf(data, path)
    pdf = QPdfDocument(None)
    assert pdf.load(path) == QPdfDocument.Error.None_
    assert pdf.pageCount() == 2


def test_class_and_master_sheets_require_approved_results(grade_entry_db):
    ids = grade_entry_db
    with pytest.raises(ValueError, match="not approved"):
        db.load_result_sheet_data(ids["session"], ids["class"], ids["term"], ids["assignment"])
    with pytest.raises(ValueError, match="not approved"):
        db.load_master_sheet_data(ids["session"], ids["class"], ids["term"])
    snapshot_id, _ = db.save_assessment_scores(
        ids["assignment"], ids["term"],
        {ids["enrollment"]: {ids["components"][0]: "30", ids["components"][1]: "50"}}, "admin",
    )
    db.approve_assessment_results(snapshot_id, "admin")
    result = db.load_result_sheet_data(ids["session"], ids["class"], ids["term"], ids["assignment"])
    master = db.load_master_sheet_data(ids["session"], ids["class"], ids["term"])
    assert result["students"][0]["result"]["points_earned"] == "80"
    assert master["subjects"] == [{"id": ids["assignment"], "subject_code": "ENG", "subject_name": "English"}]
    assert master["students"][0]["results"][ids["assignment"]]["grade_label"] == "P"


def test_master_sheet_rejects_different_frozen_rosters(grade_entry_db):
    ids = grade_entry_db
    first_snapshot, _ = db.save_assessment_scores(
        ids["assignment"], ids["term"],
        {ids["enrollment"]: {ids["components"][0]: "30", ids["components"][1]: "50"}}, "admin",
    )
    db.approve_assessment_results(first_snapshot, "admin")
    student_id = db.add_row("Students", ["S-502", "Synthetic", "Joiner", "", "", ""], actor="admin")
    enrollment_id = db.create_student_enrollment(student_id, ids["class"], ids["session"], "2026-09-01", "admin")
    subject_id = db.add_row("Subjects", ["SCI", "Science", "Primary"], actor="admin")
    assignment_id = db.save_class_subject_assignment(ids["session"], ids["class"], subject_id, None, "admin")
    first = db.save_assessment_component(assignment_id, ids["term"], "Coursework", 1, "40", "admin")
    second = db.save_assessment_component(assignment_id, ids["term"], "Examination", 2, "60", "admin")
    snapshot_id, _ = db.save_assessment_scores(
        assignment_id, ids["term"],
        {ids["enrollment"]: {first: "30", second: "50"}, enrollment_id: {first: "30", second: "50"}}, "admin",
    )
    db.approve_assessment_results(snapshot_id, "admin")
    with pytest.raises(ValueError, match="rosters differ"):
        db.load_master_sheet_data(ids["session"], ids["class"], ids["term"])


def test_approved_correction_cannot_make_result_incomplete(grade_entry_db):
    ids = grade_entry_db
    snapshot_id, _ = db.save_assessment_scores(
        ids["assignment"], ids["term"],
        {ids["enrollment"]: {ids["components"][0]: "30", ids["components"][1]: "50"}}, "admin",
    )
    db.approve_assessment_results(snapshot_id, "admin")
    with pytest.raises(ValueError, match="must remain complete"):
        db.save_assessment_scores(
            ids["assignment"], ids["term"],
            {ids["enrollment"]: {ids["components"][1]: None}}, "admin", "Remove invalid mark",
        )


def test_first_score_freezes_the_term_roster(grade_entry_db):
    ids = grade_entry_db
    db.save_assessment_scores(
        ids["assignment"], ids["term"],
        {ids["enrollment"]: {ids["components"][0]: "10"}}, "admin",
    )
    new_student = db.add_row("Students", ["S-502", "Synthetic", "Late Joiner", "", "", ""], actor="admin")
    later_enrollment = db.create_student_enrollment(
        new_student, ids["class"], ids["session"], "2026-10-01", "admin"
    )
    loaded = db.load_grade_entry_data(ids["session"], ids["assignment"], ids["term"])
    assert [row["enrollment_id"] for row in loaded["students"]] == [ids["enrollment"]]
    with pytest.raises(ValueError, match="not enrolled"):
        db.save_assessment_scores(
            ids["assignment"], ids["term"], {later_enrollment: {ids["components"][0]: "10"}}, "admin"
        )


def test_new_grade_entry_roster_excludes_archived_students(grade_entry_db):
    ids = grade_entry_db
    archived_student = db.add_row("Students", ["S-503", "Archived", "Learner", "", "", ""], actor="admin")
    db.create_student_enrollment(archived_student, ids["class"], ids["session"], "2026-09-01", "admin")
    db.set_student_active(archived_student, False, actor="admin")

    data = db.load_grade_entry_data(ids["session"], ids["assignment"], ids["term"])

    assert [student["enrollment_id"] for student in data["students"]] == [ids["enrollment"]]


def test_frozen_grade_roster_keeps_archived_students_for_result_history(grade_entry_db):
    ids = grade_entry_db
    db.save_assessment_scores(
        ids["assignment"], ids["term"],
        {ids["enrollment"]: {ids["components"][0]: "10"}}, "admin",
    )
    with db.connection() as conn:
        student_id = int(conn.execute(
            "SELECT student_id FROM student_enrollments WHERE id=?", (ids["enrollment"],)
        ).fetchone()[0])
    db.set_student_active(student_id, False, actor="admin")

    data = db.load_grade_entry_data(ids["session"], ids["assignment"], ids["term"])

    assert [student["enrollment_id"] for student in data["students"]] == [ids["enrollment"]]


def test_failed_v10_migration_rolls_back_and_keeps_v9_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    conn = sqlite3.connect(db.DB_PATH)
    try:
        for migration in (
            db._migration_1, db._migration_2, db._migration_3, db._migration_4,
            db._migration_5, db._migration_6, db._migration_7, db._migration_8, db._migration_9,
        ):
            conn.execute("BEGIN IMMEDIATE")
            migration(conn)
            conn.commit()
    finally:
        conn.close()

    def fail_after_ddl(conn):
        conn.execute("CREATE TABLE should_rollback(value TEXT)")
        raise RuntimeError("synthetic v10 migration failure")

    monkeypatch.setattr(db, "_migration_10", fail_after_ddl)
    with pytest.raises(RuntimeError, match="v10 migration failure"):
        db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 9
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='should_rollback'").fetchone() is None
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='assessment_policy_snapshots'").fetchone() is None
    backups = list(tmp_path.glob("school.pre-migration-v9-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 9


def test_failed_v11_migration_rolls_back_and_keeps_v10_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    conn = sqlite3.connect(db.DB_PATH)
    try:
        for migration in (
            db._migration_1, db._migration_2, db._migration_3, db._migration_4,
            db._migration_5, db._migration_6, db._migration_7, db._migration_8,
            db._migration_9, db._migration_10,
        ):
            conn.execute("BEGIN IMMEDIATE")
            migration(conn)
            conn.commit()
    finally:
        conn.close()

    def fail_after_ddl(conn):
        conn.execute("CREATE TABLE should_rollback(value TEXT)")
        raise RuntimeError("synthetic v11 migration failure")

    monkeypatch.setattr(db, "_migration_11", fail_after_ddl)
    with pytest.raises(RuntimeError, match="v11 migration failure"):
        db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='should_rollback'").fetchone() is None
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='assessment_result_approvals'").fetchone() is None
        columns = {row[1] for row in conn.execute("PRAGMA table_info(assessment_score_history)")}
        assert "reason" not in columns
    backups = list(tmp_path.glob("school.pre-migration-v10-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 10


def _result(earned, possible, complete=True):
    return {"complete": complete, "points_earned": earned, "points_possible": possible}


BANDS = [
    {"threshold": "0", "grade_label": "F", "remark": "Needs support"},
    {"threshold": "50", "grade_label": "P", "remark": "Pass"},
]


def test_overall_average_is_equal_weight_mean_of_subject_percentages():
    # 50% of 100 and 100% of 10: mean of percentages is 75; pooled points would give ~54.55.
    average = db.calculate_overall_average([_result("50", "100"), _result("10", "10")])
    assert average == {
        "complete": True, "subject_count": 2, "average": "75", "display_average": "75.00",
        "grade_label": None, "remark": None,
    }


def test_overall_average_is_exact_and_rounds_half_up_only_for_display():
    # 80.01% and 80% average exactly 80.005, which must display as 80.01 (half-up, no float drift).
    average = db.calculate_overall_average([_result("8001", "10000"), _result("80", "100")])
    assert average["average"] == "80.005"
    assert average["display_average"] == "80.01"
    thirds = db.calculate_overall_average([_result("1", "3"), _result("1", "3"), _result("1", "3")])
    assert thirds["display_average"] == "33.33"
    assert thirds["average"].startswith("33.3333333333")
    assert db.calculate_overall_average([_result("0", "10")])["display_average"] == "0.00"
    assert db.calculate_overall_average([_result("10", "10")])["display_average"] == "100.00"


def test_overall_average_never_treats_incomplete_subject_as_zero():
    average = db.calculate_overall_average([_result("100", "100"), _result("0", "100", complete=False)], BANDS)
    assert average == {
        "complete": False, "subject_count": 2, "average": None, "display_average": None,
        "grade_label": None, "remark": None,
    }
    with pytest.raises(ValueError, match="at least one subject"):
        db.calculate_overall_average([])


def test_overall_average_grade_uses_exact_average_at_band_boundary():
    # 49.995% displays as 50.00 but is below the 50 threshold, so it must grade F.
    just_below = db.calculate_overall_average([_result("49995", "100000")], BANDS)
    assert just_below["display_average"] == "50.00"
    assert just_below["grade_label"] == "F"
    exact = db.calculate_overall_average([_result("50", "100")], BANDS)
    assert (exact["grade_label"], exact["remark"]) == ("P", "Pass")


def test_common_grade_bands_require_identical_subject_snapshots():
    same = [dict(item) for item in BANDS]
    assert db._common_grade_bands([BANDS, same]) == BANDS
    changed = [dict(BANDS[0]), {"threshold": "45", "grade_label": "P", "remark": "Pass"}]
    assert db._common_grade_bands([BANDS, changed]) is None
    assert db._common_grade_bands([]) is None


def test_ordinal_text_follows_english_suffix_rules():
    expected = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 11: "11th", 12: "12th", 13: "13th",
                21: "21st", 22: "22nd", 23: "23rd", 101: "101st", 111: "111th", 112: "112th"}
    assert {number: db.ordinal_text(number) for number in expected} == expected
    with pytest.raises(ValueError):
        db.ordinal_text(0)


def test_class_positions_use_competition_ranking_on_exact_averages():
    from fractions import Fraction

    positions = db.calculate_class_positions({
        10: Fraction(65), 11: Fraction(130, 2), 12: Fraction(40),
        13: Fraction(100001, 1000), 14: Fraction(100000, 1000),  # 100.001 and 100.000 both beat 65
    })
    assert [positions[key]["position"] for key in (13, 14, 10, 11, 12)] == [1, 2, 3, 3, 5]
    assert positions[10]["tied"] and positions[11]["tied"]
    assert not positions[13]["tied"]
    assert positions[10]["position_text"] == "3rd" and positions[10]["of"] == 5
    # Averages that only look equal after rounding are still ordered.
    close = db.calculate_class_positions({1: Fraction(8001, 100), 2: Fraction(80005, 1000)})
    assert close[1]["position"] == 1 and close[2]["position"] == 2


def _enroll(ids, admission, last):
    student_id = db.add_row("Students", [admission, "Synthetic", last, "", "", ""], actor="admin")
    return db.create_student_enrollment(student_id, ids["class"], ids["session"], "2026-09-01", "admin")


def _two_subject_class(ids):
    """Three students, English (100 points) and Science (50 points), both approved.

    Averages: Learner 65 (80% and 50%), Brown 65 (65% and 65%), Cole 40 (40% and 40%).
    Learner and Brown tie on the average with different subject profiles.
    """
    brown = _enroll(ids, "S-502", "Brown")
    cole = _enroll(ids, "S-503", "Cole")
    learner = ids["enrollment"]
    english = ids["components"]
    snapshot, _ = db.save_assessment_scores(ids["assignment"], ids["term"], {
        learner: {english[0]: "30", english[1]: "50"},
        brown: {english[0]: "25", english[1]: "40"},
        cole: {english[0]: "15", english[1]: "25"},
    }, "admin")
    db.approve_assessment_results(snapshot, "admin")
    subject_id = db.add_row("Subjects", ["SCI", "Science", "Primary"], actor="admin")
    assignment_id = db.save_class_subject_assignment(ids["session"], ids["class"], subject_id, None, "admin")
    first = db.save_assessment_component(assignment_id, ids["term"], "Test", 1, "20", "admin")
    second = db.save_assessment_component(assignment_id, ids["term"], "Exam", 2, "30", "admin")
    science, _ = db.save_assessment_scores(assignment_id, ids["term"], {
        learner: {first: "20", second: "5"},
        brown: {first: "20", second: "12.5"},
        cole: {first: "8", second: "12"},
    }, "admin")
    return {"learner": learner, "brown": brown, "cole": cole, "science_snapshot": science}


def test_master_sheet_and_report_cards_rank_with_ties_and_average_grade(grade_entry_db):
    ids = grade_entry_db
    people = _two_subject_class(ids)
    # While Science is unapproved nothing is averaged or ranked from partial data.
    with pytest.raises(ValueError, match="not approved"):
        db.load_report_card_data(ids["session"], ids["class"], ids["term"], people["learner"])
    with pytest.raises(ValueError, match="not approved"):
        db.load_master_sheet_data(ids["session"], ids["class"], ids["term"])
    db.approve_assessment_results(people["science_snapshot"], "admin")

    master = db.load_master_sheet_data(ids["session"], ids["class"], ids["term"])
    by_id = {int(student["enrollment_id"]): student for student in master["students"]}
    assert by_id[people["learner"]]["overall"]["display_average"] == "65.00"
    assert by_id[people["brown"]]["overall"]["display_average"] == "65.00"
    assert by_id[people["cole"]]["overall"]["display_average"] == "40.00"
    assert by_id[people["learner"]]["overall"]["grade_label"] == "P"
    assert by_id[people["cole"]]["overall"]["grade_label"] == "F"
    positions = {key: (value["position"]["position"], value["position"]["tied"]) for key, value in by_id.items()}
    assert positions == {people["learner"]: (1, True), people["brown"]: (1, True), people["cole"]: (3, False)}
    assert by_id[people["cole"]]["position"]["of"] == 3

    card = db.load_report_card_data(ids["session"], ids["class"], ids["term"], people["cole"])
    assert card["position"]["position_text"] == "3rd" and card["position"]["of"] == 3
    assert card["overall"]["grade_label"] == "F"
    assert [item["grade_label"] for item in card["subjects"]] == ["F", "F"]  # subject grades unchanged
    cards = db.load_class_report_cards_data(ids["session"], ids["class"], ids["term"])["report_cards"]
    assert {c["student"]["admission_no"]: c["position"]["position"] for c in cards} == {
        "S-501": 1, "S-502": 1, "S-503": 3,
    }


def test_report_card_has_no_position_when_class_rosters_are_inconsistent(grade_entry_db):
    ids = grade_entry_db
    snapshot, _ = db.save_assessment_scores(
        ids["assignment"], ids["term"],
        {ids["enrollment"]: {ids["components"][0]: "30", ids["components"][1]: "50"}}, "admin",
    )
    db.approve_assessment_results(snapshot, "admin")
    # A classmate joins before Science is scored, so the two frozen rosters differ.
    joiner = _enroll(ids, "S-502", "Joiner")
    subject_id = db.add_row("Subjects", ["SCI", "Science", "Primary"], actor="admin")
    assignment_id = db.save_class_subject_assignment(ids["session"], ids["class"], subject_id, None, "admin")
    first = db.save_assessment_component(assignment_id, ids["term"], "Test", 1, "20", "admin")
    second = db.save_assessment_component(assignment_id, ids["term"], "Exam", 2, "30", "admin")
    science, _ = db.save_assessment_scores(assignment_id, ids["term"], {
        ids["enrollment"]: {first: "20", second: "5"}, joiner: {first: "10", second: "10"},
    }, "admin")
    db.approve_assessment_results(science, "admin")
    card = db.load_report_card_data(ids["session"], ids["class"], ids["term"], ids["enrollment"])
    assert card["overall"]["display_average"] == "65.00"  # the student's own complete results still average
    assert card["position"] is None  # but the class cannot be ranked from inconsistent rosters
    with pytest.raises(ValueError, match="rosters differ"):
        db.load_master_sheet_data(ids["session"], ids["class"], ids["term"])


def test_single_student_class_has_first_position_of_one(grade_entry_db):
    ids = grade_entry_db
    snapshot, _ = db.save_assessment_scores(
        ids["assignment"], ids["term"],
        {ids["enrollment"]: {ids["components"][0]: "30", ids["components"][1]: "50"}}, "admin",
    )
    db.approve_assessment_results(snapshot, "admin")
    card = db.load_report_card_data(ids["session"], ids["class"], ids["term"], ids["enrollment"])
    assert card["position"] == {"position": 1, "of": 1, "position_text": "1st", "tied": False}
    assert card["overall"]["display_average"] == "80.00" and card["overall"]["grade_label"] == "P"


def test_overall_average_and_position_are_rendered(grade_entry_db, monkeypatch):
    pytest.importorskip("PyQt6")
    from smartschool.assessment_ui import GradeEntryPage

    ids = grade_entry_db
    people = _two_subject_class(ids)
    db.approve_assessment_results(people["science_snapshot"], "admin")
    card = db.load_report_card_data(ids["session"], ids["class"], ids["term"], people["learner"])
    html = GradeEntryPage._report_card_html(card)
    assert "Overall average:</b> 65.00% across 2 subjects" in html
    assert "Grade: <b>P</b> (Pass)" in html
    assert "Class position:</b> 1st of 3 (tied)" in html
    captured = []
    monkeypatch.setattr(
        GradeEntryPage, "_write_pdf", staticmethod(lambda html, path, logo_data=None, student_photo_data=None: captured.append(html))
    )
    GradeEntryPage._render_master_sheet_pdf(
        db.load_master_sheet_data(ids["session"], ids["class"], ids["term"]), "unused.pdf"
    )
    assert "Overall average</b> (2 subjects)" in captured[0]
    assert "<b>65.00%</b>" in captured[0]
    assert "Position 1st of 3 (tied)" in captured[0] and "Position 3rd of 3" in captured[0]
