import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QDate
from PyQt6.QtWidgets import QApplication

from smartschool.ui import StudentRegistrationDialog


def test_optional_student_details_are_off_by_default_and_returned_when_entered():
    app = QApplication.instance() or QApplication([])
    dialog = StudentRegistrationDialog({
        "sessions": [{"id": 1, "name": "2026/2027", "starts_on": "2026-09-01", "ends_on": "2027-07-31"}],
        "classes": [{"id": 2, "class_name": "Class 1", "section": "A", "educational_stage": "primary"}],
    })
    dialog.first_name.setText("Amina")
    dialog.last_name.setText("Okafor")
    assert dialog.values()["nin"] == ""
    assert dialog.values()["date_of_birth"] == ""
    assert dialog.values()["email"] == ""

    dialog.nin.setText("NIN-123")
    dialog.email.setText("amina@example.org")
    dialog.date_of_birth.setDate(QDate(2012, 3, 4))
    dialog.date_of_birth_enabled.setChecked(True)
    values = dialog.values()
    assert values["nin"] == "NIN-123"
    assert values["date_of_birth"] == "2012-03-04"
    assert values["email"] == "amina@example.org"
    dialog.close()
    app.processEvents()
