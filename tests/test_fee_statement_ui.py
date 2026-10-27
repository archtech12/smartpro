"""Qt screens for multi-class fees, the student statement with lump sum, and the report settings.

These need PyQt6 and run offscreen; they are skipped where PyQt6 is not installed.
"""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")

from test_fee_statements import _owing_two_terms  # noqa: E402
from test_school_profile import PNG_PIXEL  # noqa: E402

from smartschool import database as db  # noqa: E402

_APP = None


def _app():
    global _APP
    from PyQt6.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication([])
    return _APP


def _wait(condition, seconds=5):
    from PyQt6.QtTest import QTest
    app = _app()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not condition():
        app.processEvents()
        QTest.qWait(20)
    return condition()


def test_statement_dialog_shows_carried_debt_and_allows_lump_sum(school):
    from smartschool.fees_ui import StudentStatementDialog

    _app()
    student = _owing_two_terms(school)
    dialog = StudentStatementDialog(student, "admin")
    assert _wait(lambda: dialog._data is not None)
    assert dialog.table.rowCount() == 2
    assert "(carried forward)" in dialog.table.item(0, 0).text()
    assert dialog.table.item(1, 5).text() == "8000"
    assert "Outstanding 11000" in dialog.summary.text()
    assert dialog.pay_button.isEnabled() and dialog.print_button.isEnabled()
    dialog.close()


def test_statement_dialog_records_a_lump_sum_and_refreshes(school, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    from smartschool.fees_ui import StudentStatementDialog

    _app()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
    student = _owing_two_terms(school)
    dialog = StudentStatementDialog(student, "admin")
    assert _wait(lambda: dialog._data is not None)
    dialog.amount.setText("4000")
    dialog.pay()
    assert _wait(lambda: dialog.changed and not dialog._busy and dialog._data["total_outstanding"] == "7000")
    assert dialog.amount.text() == ""
    dialog.close()


def test_statement_dialog_reports_overpayment_without_changing_the_ledger(school, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    from smartschool.fees_ui import StudentStatementDialog

    _app()
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[2])))
    student = _owing_two_terms(school)
    dialog = StudentStatementDialog(student, "admin")
    assert _wait(lambda: dialog._data is not None)
    dialog.amount.setText("99999")
    dialog.pay()
    assert _wait(lambda: bool(warnings))
    assert "exceeds the outstanding balance" in warnings[0] and not dialog.changed
    assert db.load_student_fee_statement(student, "admin")["total_outstanding"] == "11000"
    dialog.close()


def _fee_setup_page():
    from smartschool.fees_ui import FeeSetupPage
    _app()
    page = FeeSetupPage("admin", "admin")
    assert _wait(lambda: page.status.text().startswith("Loaded"))
    return page


def test_fee_setup_applies_one_fee_to_all_classes(school):
    page = _fee_setup_page()
    page.fee_type.setCurrentIndex(page.fee_type.findData(school["fees"]["School Fees"]))
    page.class_scope.setCurrentIndex(page.class_scope.findData("all"))
    assert page.class_section.isHidden() and page.class_list.isHidden()
    page.amount.setText("7000")
    assert page.save_schedule_button.isEnabled()
    page.save_schedule()
    assert _wait(lambda: len(db.load_fee_setup_data()["schedules"]) == 3)
    page.close()


def test_fee_setup_applies_a_fee_to_ticked_classes_only(school):
    page = _fee_setup_page()
    page.fee_type.setCurrentIndex(page.fee_type.findData(school["fees"]["Exam"]))
    page.class_scope.setCurrentIndex(page.class_scope.findData("selected"))
    assert not page.class_list.isHidden() and page.class_section.isHidden()
    page.amount.setText("1000")
    page.class_list.item(0).setSelected(True)
    page.class_list.item(2).setSelected(True)
    page.save_schedule()
    assert _wait(lambda: len(db.load_fee_setup_data()["schedules"]) == 2)
    assert sorted(row["class_id"] for row in db.load_fee_setup_data()["schedules"]) == sorted(
        [school["classes"][0], school["classes"][2]]
    )
    page.close()


def test_fee_setup_asks_for_a_class_when_none_is_ticked(school, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    shown = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: shown.append(a[1])))
    page = _fee_setup_page()
    page.fee_type.setCurrentIndex(page.fee_type.findData(school["fees"]["Exam"]))
    page.class_scope.setCurrentIndex(page.class_scope.findData("selected"))
    page.amount.setText("1000")
    page.save_schedule()
    assert shown == ["Choose classes"] and db.load_fee_setup_data()["schedules"] == []
    page.close()


def test_school_profile_page_saves_report_style_and_signature(school):
    from smartschool.settings_ui import SchoolProfilePage

    _app()
    page = SchoolProfilePage("admin", "admin")
    assert page.ranking_style.currentData() == "position"
    page.ranking_style.setCurrentIndex(page.ranking_style.findData("average_grade"))
    page._read_image_file = lambda _title: PNG_PIXEL
    page.choose_signature("head_signature", "Head of school signature")
    page.save()
    assert _wait(lambda: db.load_report_settings()["ranking_style"] == "average_grade")
    assert db.load_report_settings()["head_signature"] == PNG_PIXEL
    assert db.load_report_settings()["class_teacher_signature"] is None
    page.close()
