"""Dashboard overview and setup checklist (read-only, synthetic data)."""

from smartschool import dashboard_data as dd
from smartschool import database as db


def _step(checklist, key):
    return next(step for step in checklist["steps"] if step["key"] == key)


def test_empty_school_checklist_points_at_first_missing_step(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    checklist = dd.load_setup_checklist()
    assert not checklist["complete"]
    assert checklist["required_done"] == 0
    assert checklist["next_step"]["key"] == "profile"
    assert all(not step["done"] for step in checklist["steps"])
    overview = dd.load_dashboard_overview()
    assert overview["session"] is None and overview["enrolled"] == 0


def test_checklist_tracks_progress_without_writing(school):
    before = dd.load_setup_checklist()
    assert _step(before, "calendar")["done"]
    assert _step(before, "classes")["done"]
    assert not _step(before, "stages")["done"]
    assert "need a stage" in _step(before, "stages")["detail"]
    for class_id in school["classes"]:
        db.save_class_educational_stage(class_id, "primary", "admin")
    after = dd.load_setup_checklist()
    assert _step(after, "stages")["done"]
    assert after["required_done"] == before["required_done"] + 1
    assert after["next_step"]["key"] != "stages"


def test_overview_counts_enrolment_gender_and_attendance(school):
    for number, gender in enumerate(("Female", "Male", "")):
        student = db.add_row("Students", [f"ADM-{number}", "Syn", str(number), gender, "", ""], actor="admin")
        db.create_student_enrollment(student, school["classes"][0], school["session"], "2026-09-01", "admin")
    overview = dd.load_dashboard_overview()
    assert overview["session"] == "2026" and overview["term"] == "First Term"
    assert overview["enrolled"] == 3
    assert overview["gender"] == {"Female": 1, "Male": 1, "Not set": 1}
    assert overview["attendance"]["classes_total"] == 3
    assert overview["attendance"]["classes_missing"] == 3
    assert overview["fees"] == {"issued_invoices": 0, "draft_batches": 0}


def test_analytics_are_admin_only_and_report_fees_alerts_and_activity(school):
    import pytest
    with pytest.raises(PermissionError):
        dd.load_dashboard_analytics("teacher")
    student = db.add_row("Students", ["A-1", "Syn", "One", "Female", "", ""], actor="admin")
    db.create_student_enrollment(student, school["classes"][0], school["session"], "2026-09-01", "admin")
    db.save_fee_schedule(school["session"], school["classes"][0], school["terms"][0], school["fees"]["School Fees"], "10000", "", "admin")
    batch = db.generate_fee_invoice_batch(school["session"], school["classes"][0], school["terms"][0], "admin")
    db.issue_fee_invoice_batch(batch, "admin")
    data = dd.load_dashboard_analytics("admin")
    assert data["fees"]["billed"] == "10000" and data["fees"]["paid"] == "0"
    assert data["fees"]["outstanding"] == "10000" and data["fees"]["collection_rate"] == 0
    assert data["fees"]["students_owing"] == 1
    assert any("Fee collection is low" in a["message"] for a in data["alerts"])
    assert any("no Primary/Secondary stage" in a["message"] for a in data["alerts"])
    assert data["class_strength"][0] == {"class": "JSS 1 A", "students": 1}
    assert data["activity"] and "admin" in data["activity"][0]["text"]
    assert data["attendance_rate_30d"] is None
    assert data["communications"]["fee_reminders_ready"] == 0  # owed, but no usable contact: not "ready" to send
