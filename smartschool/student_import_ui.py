"""Preview-first dialog for bulk student registration from CSV."""

from __future__ import annotations

from PyQt6.QtCore import QDate
from PyQt6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .table_helpers import configure_table_headers
from .ui_helpers import wrapped_label
from . import database as db
from . import tabular
from .student_import import preview_student_import


class StudentImportDialog(QDialog):
    def __init__(self, options, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import student registrations")
        self.resize(980, 640)
        self._sessions = options["sessions"]
        self._rows = []
        self._file_path = ""
        layout = QVBoxLayout(self)
        layout.addWidget(wrapped_label(
            "Choose the class placement, academic session, and enrollment date for this file. "
            "The Excel or CSV file supplies each student's names and optional contact and identity details."
        ))
        form = QFormLayout()
        self.class_section = QComboBox()
        for item in options["classes"]:
            stage = db.EDUCATIONAL_STAGES.get(item["educational_stage"], "Stage not set")
            label = " ".join(part for part in (item["class_name"], item["section"]) if part)
            self.class_section.addItem(f"{label} — {stage}", int(item["id"]))
        self.session = QComboBox()
        for item in self._sessions:
            self.session.addItem(item["name"], int(item["id"]))
        current = next((item for item in self._sessions if item["is_current"]), None)
        if current:
            self.session.setCurrentIndex(self.session.findData(int(current["id"])))
        self.enrolled_on = QDateEdit()
        self.enrolled_on.setCalendarPopup(True)
        self.enrolled_on.setDisplayFormat("yyyy-MM-dd")
        form.addRow("Class and section", self.class_section)
        form.addRow("Academic session", self.session)
        form.addRow("Enrollment date", self.enrolled_on)
        layout.addLayout(form)

        file_row = QHBoxLayout()
        self.choose_file_button = QPushButton("Choose Excel or CSV file…")
        self.file_label = QLabel("No file selected")
        self.file_label.setWordWrap(True)
        file_row.addWidget(self.choose_file_button)
        file_row.addWidget(self.file_label, 1)
        layout.addLayout(file_row)
        self.status = QLabel("Use the template so the column names match. Excel (.xlsx) and CSV files both work.")
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels([
            "First Name", "Last Name", "Gender", "Guardian Phone Number", "NIN", "Date of Birth", "Email"
        ])
        configure_table_headers(self.table)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.import_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.import_button.setText("Register students")
        self.import_button.setEnabled(False)
        buttons.accepted.connect(self._accept_if_ready)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.session.currentIndexChanged.connect(self._set_session_date_range)
        self.choose_file_button.clicked.connect(self._choose_file)
        self._set_session_date_range()
        if not self._sessions or not options["classes"]:
            self.status.setText("Create an academic session and class before importing students.")
            self.choose_file_button.setEnabled(False)

    def _set_session_date_range(self, *_args):
        session_id = self.session.currentData()
        session = next((item for item in self._sessions if int(item["id"]) == session_id), None)
        if not session:
            return
        start = QDate.fromString(session["starts_on"], "yyyy-MM-dd")
        end = QDate.fromString(session["ends_on"], "yyyy-MM-dd")
        self.enrolled_on.setMinimumDate(start)
        self.enrolled_on.setMaximumDate(end)
        today = QDate.currentDate()
        self.enrolled_on.setDate(today if start <= today <= end else start)

    def _choose_file(self):
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, "Choose student file", "", tabular.SPREADSHEET_FILTER
        )
        if not path:
            return
        try:
            self._load_preview(path)
        except Exception as exc:  # a bad file must never close the application
            self._reject_file(str(exc))

    def _reject_file(self, message):
        self._rows = []
        self._file_path = ""
        self.file_label.setText("No valid file selected")
        self.status.setText("The file was not imported.")
        self.table.setRowCount(0)
        self.import_button.setEnabled(False)
        box = QMessageBox(QMessageBox.Icon.Warning, "Student file not accepted", message.split("\n")[0], parent=self)
        if "\n" in message:
            box.setDetailedText(message)
        box.exec()

    def _load_preview(self, path):
        preview = preview_student_import(path)
        self._file_path = path
        self._rows = preview["rows"]
        self.file_label.setText(path)
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(self._rows))
        for row_index, row in enumerate(self._rows):
            for column, key in enumerate(("first_name", "last_name", "gender", "guardian_phone", "nin", "date_of_birth", "email")):
                self.table.setItem(row_index, column, QTableWidgetItem(row[key]))
        self.table.setUpdatesEnabled(True)
        self.table.resizeColumnsToContents()
        notes = " ".join(preview.get("warnings", []))
        self.status.setText(
            f"Preview passed: {len(self._rows)} student(s). No records have been saved yet." + (f"  Note: {notes}" if notes else "")
        )
        self.import_button.setEnabled(self.class_section.currentData() is not None and self.session.currentData() is not None)

    def _accept_if_ready(self):
        if not self._rows or self.class_section.currentData() is None or self.session.currentData() is None:
            QMessageBox.information(self, "Choose import details", "Choose a valid file, class, and academic session.")
            return
        answer = QMessageBox.question(
            self,
            "Confirm student registrations",
            f"Register {len(self._rows)} student(s) in {self.class_section.currentText()} "
            f"for {self.session.currentText()}? The app will generate admission IDs. This saves the entire "
            "file as one transaction; if any record cannot be saved, none will be added.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.accept()

    def values(self):
        return {
            "students": self._rows,
            "class_id": int(self.class_section.currentData()),
            "academic_session_id": int(self.session.currentData()),
            "enrolled_on": self.enrolled_on.date().toString("yyyy-MM-dd"),
        }
