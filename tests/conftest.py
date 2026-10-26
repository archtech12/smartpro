"""Shared synthetic fixtures."""

import pytest

from smartschool import database as db


@pytest.fixture
def school(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    db.create_user("admin", "synthetic-hash")
    db.create_user("teacher", "synthetic-hash", "teacher")
    session = db.save_academic_session("2026", "2026-09-01", "2027-07-31", True, "admin")
    terms = [
        db.save_term(session, "First Term", 1, "2026-09-01", "2026-12-20", "admin"),
        db.save_term(session, "Second Term", 2, "2027-01-05", "2027-04-10", "admin"),
        db.save_term(session, "Third Term", 3, "2027-04-25", "2027-07-20", "admin"),
    ]
    db.set_current_term(terms[0], "admin")
    classes = [db.add_row("Classes", [name, "A", ""], actor="admin") for name in ("JSS 1", "JSS 2", "SS 1")]
    fees = {name: db.create_fee_type(name, "", "admin") for name in ("School Fees", "Exam")}
    return {"session": session, "terms": terms, "classes": classes, "fees": fees}
