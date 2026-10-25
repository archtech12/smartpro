"""Session-scoped timetable editor with service-validated conflict checks."""

from __future__ import annotations

from html import escape

from PyQt6.QtCore import QMarginsF, QThreadPool, Qt, QUrl
from PyQt6.QtGui import QImage, QPageLayout, QPageSize, QPdfWriter, QTextDocument
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .table_helpers import configure_table_headers
from .ui_helpers import wrapped_label
from . import database as db
from .workers import BackgroundTask
from .assessment_ui import GradeEntryPage


WEEKDAYS = [
    (1, "Monday"), (2, "Tuesday"), (3, "Wednesday"), (4, "Thursday"),
    (5, "Friday"), (6, "Saturday"), (7, "Sunday"),
]


class TimetablePage(QWidget):
    def __init__(self, username: str, role: str):
        super().__init__()
        self.username = username
        self.is_admin = role == "admin"
        self._load_id = 0
        self._write_id = 0
        self._workers = {}
        self._assignments = []
        self._entries = []
        self._editing_id = None
        self._loading = False
        self._writing = False
        self._report_path = None
        self._thread_pool = QThreadPool.globalInstance()

        layout = QVBoxLayout(self)
        title = QLabel("Timetable")
        title.setObjectName("heading")
        layout.addWidget(title)
        introduction = wrapped_label(
            "Build a weekly timetable from the school's actual times. No default period length or school week is assumed. "
            "Class, assigned teacher, and named room overlaps are blocked."
        )
        layout.addWidget(introduction)

        self.session_filter = QComboBox()
        self.session_filter.currentIndexChanged.connect(self.refresh)
        layout.addWidget(QLabel("Academic session"))
        layout.addWidget(self.session_filter)

        form = QFormLayout()
        self.assignment = QComboBox()
        self.assignment.currentIndexChanged.connect(self._update_actions)
        self.day = QComboBox()
        for value, label in WEEKDAYS:
            self.day.addItem(label, value)
        self.starts_at = QLineEdit()
        self.starts_at.setPlaceholderText("HH:MM")
        self.ends_at = QLineEdit()
        self.ends_at.setPlaceholderText("HH:MM")
        self.location = QLineEdit()
        self.location.setPlaceholderText("Optional room or location")
        form.addRow("Class and subject", self.assignment)
        form.addRow("Day", self.day)
        form.addRow("Start time (24-hour)", self.starts_at)
        form.addRow("End time (24-hour)", self.ends_at)
        form.addRow("Room or location", self.location)
        layout.addLayout(form)

        actions = QHBoxLayout()
        self.save_button = QPushButton("Add timetable slot")
        self.save_button.setObjectName("primary")
        self.clear_button = QPushButton("Clear form")
        self.delete_button = QPushButton("Delete selected")
        self.export_button = QPushButton("Export session timetable PDF")
        self.save_button.clicked.connect(self.save_entry)
        self.clear_button.clicked.connect(self.clear_form)
        self.delete_button.clicked.connect(self.delete_entry)
        self.export_button.clicked.connect(self.export_pdf)
        actions.addWidget(self.save_button)
        actions.addWidget(self.clear_button)
        actions.addWidget(self.delete_button)
        actions.addWidget(self.export_button)
        actions.addStretch()
        layout.addLayout(actions)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            ["Weekday", "Time Slot", "Class", "Subject Name", "Assigned Teacher", "Room or Location", "Timetable Entry ID"]
        )
        configure_table_headers(self.table)
        self.table.setColumnHidden(6, True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.itemSelectionChanged.connect(self._select_entry)
        layout.addWidget(self.table)
        self.status = QLabel("Loading timetable setup…")
        layout.addWidget(self.status)
        self._update_actions()
        self.refresh()

    def refresh(self, *_args):
        selected = self.session_filter.currentData()
        selected_id = int(selected) if selected is not None else None
        self._load_id += 1
        load_id = self._load_id
        self._loading = True
        self._update_actions()
        self.status.setText("Loading timetable…")
        worker = BackgroundTask(load_id, lambda: db.load_timetable_data(selected_id))
        worker.signals.finished.connect(
            lambda request_id, data, error: self._data_loaded(request_id, data, error, selected_id)
        )
        self._workers[("load", load_id)] = worker
        self._thread_pool.start(worker)

    def _data_loaded(self, load_id, data, error, requested_session):
        self._workers.pop(("load", load_id), None)
        if load_id != self._load_id:
            return
        self._loading = False
        if error is not None:
            self.status.setText("Could not load timetable")
            self._update_actions()
            QMessageBox.warning(self, "Could not load timetable", str(error))
            return
        sessions = data["sessions"]
        self.session_filter.blockSignals(True)
        self.session_filter.clear()
        self.session_filter.addItem("Select session", None)
        for session in sessions:
            self.session_filter.addItem(session["name"], int(session["id"]))
        selected_session = requested_session
        if selected_session is None:
            current = next((item for item in sessions if item["is_current"]), None)
            selected_session = int(current["id"]) if current else (
                int(sessions[0]["id"]) if sessions else None
            )
        index = self.session_filter.findData(selected_session) if selected_session is not None else -1
        self.session_filter.setCurrentIndex(index if index >= 0 else 0)
        self.session_filter.blockSignals(False)
        actual_session = self.session_filter.currentData()
        actual_id = int(actual_session) if actual_session is not None else None
        if actual_id != requested_session:
            self.refresh()
            return

        self._assignments = data["assignments"]
        self.assignment.clear()
        self.assignment.addItem("Select class and subject", None)
        for row in self._assignments:
            label = f"{row['class_name']} {row['section']} — {row['subject_code']} {row['subject_name']}".strip()
            if row["teacher_id"] is not None:
                label += f" · {row['teacher_first_name']} {row['teacher_last_name']}"
            self.assignment.addItem(label, int(row["id"]))
        self._entries = data["entries"]
        self._render_entries()
        self.status.setText(
            f"{len(self._entries)} timetable slot(s)" if actual_id is not None
            else "Create an academic session and class-subject assignments first."
        )
        self._update_actions()

    def _render_entries(self):
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(self._entries))
        for row_index, entry in enumerate(self._entries):
            teacher = " ".join(
                part for part in (entry["teacher_first_name"], entry["teacher_last_name"]) if part
            )
            values = (
                dict(WEEKDAYS)[int(entry["day_of_week"])],
                f"{entry['starts_at']}–{entry['ends_at']}",
                f"{entry['class_name']} {entry['section']}".strip(),
                f"{entry['subject_code']} {entry['subject_name']}",
                teacher,
                entry["location"],
                entry["id"],
            )
            for column, value in enumerate(values):
                self.table.setItem(row_index, column, QTableWidgetItem(str(value)))
        self.table.setUpdatesEnabled(True)
        self.table.viewport().update()
        self._editing_id = None
        self.save_button.setText("Add timetable slot")

    def _selected(self):
        row_index = self.table.currentRow()
        if 0 <= row_index < len(self._entries):
            return self._entries[row_index]
        return None

    def _select_entry(self):
        entry = self._selected()
        if entry is None:
            self._editing_id = None
            self.save_button.setText("Add timetable slot")
            self._update_actions()
            return
        self._editing_id = int(entry["id"])
        self.assignment.setCurrentIndex(self.assignment.findData(int(entry["class_subject_assignment_id"])))
        self.day.setCurrentIndex(self.day.findData(int(entry["day_of_week"])))
        self.starts_at.setText(entry["starts_at"])
        self.ends_at.setText(entry["ends_at"])
        self.location.setText(entry["location"])
        self.save_button.setText("Save timetable slot")
        self._update_actions()

    def _update_actions(self):
        ready = self.is_admin and not self._loading and not self._writing
        has_assignment = self.assignment.currentData() is not None
        self.save_button.setEnabled(ready and has_assignment)
        self.clear_button.setEnabled(not self._loading and not self._writing)
        self.delete_button.setEnabled(ready and self._selected() is not None)
        self.export_button.setEnabled(not self._loading and not self._writing and self.session_filter.currentData() is not None and bool(self._entries))
        self.session_filter.setEnabled(not self._writing)

    def clear_form(self):
        self.table.clearSelection()
        self._editing_id = None
        self.assignment.setCurrentIndex(0)
        self.day.setCurrentIndex(0)
        self.starts_at.clear()
        self.ends_at.clear()
        self.location.clear()
        self.save_button.setText("Add timetable slot")
        self._update_actions()

    def save_entry(self):
        assignment_id = self.assignment.currentData()
        if assignment_id is None or self._loading or self._writing:
            return
        args = (
            int(assignment_id), int(self.day.currentData()),
            self.starts_at.text(), self.ends_at.text(),
            self.location.text(), self.username,
        )
        entry_id = self._editing_id
        self._start_write(
            lambda: db.save_timetable_entry(*args, entry_id=entry_id),
            "Could not save timetable slot",
        )

    def delete_entry(self):
        entry = self._selected()
        if entry is None or self._loading or self._writing:
            return
        answer = QMessageBox.question(
            self, "Delete timetable slot", "Delete the selected timetable slot?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            entry_id = int(entry["id"])
            self._start_write(
                lambda: db.delete_timetable_entry(entry_id, self.username),
                "Could not delete timetable slot",
            )

    def _start_write(self, operation, error_title):
        if self._writing:
            return
        self._writing = True
        self._write_id += 1
        write_id = self._write_id
        worker = BackgroundTask(write_id, operation)
        worker.signals.finished.connect(
            lambda request_id, result, error: self._write_finished(request_id, result, error, error_title)
        )
        self._workers[("write", write_id)] = worker
        self._update_actions()
        self.status.setText("Saving timetable…")
        self._thread_pool.start(worker)

    def _write_finished(self, write_id, _result, error, error_title):
        self._workers.pop(("write", write_id), None)
        if write_id != self._write_id:
            return
        self._writing = False
        if error is not None:
            self.status.setText("Timetable change was not saved")
            self._update_actions()
            QMessageBox.warning(self, error_title, str(error))
            return
        self.clear_form()
        self.refresh()

    def export_pdf(self):
        session_value = self.session_filter.currentData()
        if session_value is None or self._loading or self._writing or not self._entries:
            return
        session_name = self.session_filter.currentText().strip()
        safe_name = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in session_name).strip("-")
        path, _selected_filter = QFileDialog.getSaveFileName(
            self, "Export session timetable", f"timetable-{safe_name or 'session'}.pdf", "PDF files (*.pdf)"
        )
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        self._report_path = path
        self._writing = True
        self._update_actions()
        self.status.setText("Preparing timetable PDF…")
        self._write_id += 1
        task_id = self._write_id
        worker = BackgroundTask(task_id, lambda: db.load_timetable_print_data(int(session_value)))
        worker.signals.finished.connect(self._pdf_data_loaded)
        self._workers[("pdf", task_id)] = worker
        self._thread_pool.start(worker)

    def _pdf_data_loaded(self, task_id, data, error):
        self._workers.pop(("pdf", task_id), None)
        if task_id != self._write_id:
            return
        self._writing = False
        if error is not None:
            self._update_actions()
            self.status.setText("Timetable PDF was not exported")
            QMessageBox.warning(self, "Could not export timetable", str(error))
            return
        try:
            self._render_pdf(data, self._report_path)
        except Exception as exc:
            self._update_actions()
            self.status.setText("Timetable PDF was not exported")
            QMessageBox.warning(self, "Could not create timetable PDF", str(exc))
            return
        self._update_actions()
        self.status.setText(f"Session timetable PDF saved: {self._report_path}")
        QMessageBox.information(self, "Timetable ready", f"Saved timetable to:\n{self._report_path}")

    @staticmethod
    def _render_pdf(data, path):
        entries = data["entries"]
        rows = "".join(
            "<tr>"
            f"<td>{escape(dict(WEEKDAYS)[int(item['day_of_week'])])}</td>"
            f"<td>{escape(item['starts_at'])}–{escape(item['ends_at'])}</td>"
            f"<td>{escape(item['class_name'])} {escape(item['section'])}</td>"
            f"<td>{escape(item['subject_code'])} — {escape(item['subject_name'])}</td>"
            f"<td>{escape(' '.join(part for part in (item['teacher_first_name'], item['teacher_last_name']) if part))}</td>"
            f"<td>{escape(item['location'] or '')}</td></tr>"
            for item in entries
        )
        profile = data.get("school_profile", {})
        logo_data = data.get("school_logo")
        heading = GradeEntryPage._school_identity_html(profile, bool(logo_data))
        html = f"""<!doctype html><html><head><meta charset='utf-8'><style>
          body {{ font-family:sans-serif;color:#183247; }} h1 {{ color:#102a43; }}
          .school {{ text-align:center;margin-bottom:12px; }} .school h2 {{ margin:0; }}
          table {{ border-collapse:collapse;width:100%; }} th {{ background:#e8f0f6;text-align:left; }}
          th,td {{ border:1px solid #9fb3c8;padding:7px; }}
        </style></head><body>{heading}<h1>Weekly Timetable</h1>
          <p>Academic session: {escape(data['session_name'])} · {len(entries)} scheduled slot(s)</p>
          <table><thead><tr><th>Day</th><th>Time</th><th>Class</th><th>Subject</th><th>Teacher</th><th>Room</th></tr></thead>
          <tbody>{rows}</tbody></table><p>Schedule reflects the school's explicitly entered days, times, and locations.</p></body></html>"""
        writer = QPdfWriter(path)
        writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        writer.setPageOrientation(QPageLayout.Orientation.Landscape)
        writer.setPageMargins(QMarginsF(12, 12, 12, 12), QPageLayout.Unit.Millimeter)
        document = QTextDocument()
        if logo_data:
            image = QImage.fromData(logo_data)
            if image.isNull():
                raise ValueError("The saved school logo is not a readable image.")
            image = image.scaled(192, 128, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            document.addResource(QTextDocument.ResourceType.ImageResource, QUrl("school-logo"), image)
        document.setHtml(html)
        document.print(writer)
