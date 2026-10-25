"""Assigned-class attendance register with explicit marks and corrections."""

from __future__ import annotations

from PyQt6.QtCore import QDate, QThreadPool
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDateEdit,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
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


class AttendancePage(QWidget):
    def __init__(self, username: str, role: str):
        super().__init__()
        self.username = username
        self.is_admin = role == "admin"
        self.can_use = role in {"admin", "teacher"}
        self._scope_id = 0
        self._load_id = 0
        self._save_id = 0
        self._workers = {}
        self._thread_pool = QThreadPool.globalInstance()
        self._sessions = []
        self._classes = []
        self._classes_by_session = {}
        self._teacher_linked = False
        self._students = []
        self._loading = False
        self._saving = False

        layout = QVBoxLayout(self)
        title = QLabel("Daily attendance")
        title.setObjectName("heading")
        layout.addWidget(title)
        introduction = wrapped_label(
            "Record explicit Present or Absent marks for enrolled students. Unmarked rows are not counted as absent. "
            "Teachers can take attendance for classes assigned to them. Changes to saved marks require a reason and are retained in history."
        )
        layout.addWidget(introduction)

        selectors = QFormLayout()
        self.session_filter = QComboBox()
        self.class_filter = QComboBox()
        self.attendance_date = QDateEdit(QDate.currentDate())
        self.attendance_date.setCalendarPopup(True)
        self.attendance_date.setDisplayFormat("yyyy-MM-dd")
        self.session_filter.currentIndexChanged.connect(self._session_changed)
        selectors.addRow("Academic session", self.session_filter)
        selectors.addRow("Class and section", self.class_filter)
        selectors.addRow("Attendance date", self.attendance_date)
        layout.addLayout(selectors)

        actions = QHBoxLayout()
        self.load_button = QPushButton("Load register")
        self.present_button = QPushButton("Mark all present")
        self.absent_button = QPushButton("Mark all absent")
        self.save_button = QPushButton("Save attendance")
        self.summary_button = QPushButton("Attendance summary")
        self.save_button.setObjectName("primary")
        for button in (self.load_button, self.present_button, self.absent_button, self.save_button, self.summary_button):
            button.setEnabled(False)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Admission Number", "Student Name", "Attendance Mark", "Enrollment Record ID"])
        configure_table_headers(self.table)
        self.table.setColumnHidden(3, True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)
        self.status = QLabel("Loading attendance setup…")
        layout.addWidget(self.status)

        self.load_button.clicked.connect(self.load_register)
        self.present_button.clicked.connect(lambda: self._mark_all("present"))
        self.absent_button.clicked.connect(lambda: self._mark_all("absent"))
        self.save_button.clicked.connect(self.save_register)
        self.summary_button.clicked.connect(self.show_summary)
        if self.can_use:
            self._load_scope()
        else:
            self.status.setText(
                "Attendance is currently available to administrators only; teacher class scopes are not configured yet."
            )

    def _set_busy(self, busy: bool):
        self._loading = busy
        self.table.setEnabled(not busy and not self._saving)
        has_scope = self.session_filter.currentData() is not None and self.class_filter.currentData() is not None
        self.load_button.setEnabled(not busy and not self._saving and has_scope)
        self.summary_button.setEnabled(not busy and not self._saving and has_scope)
        markable = self.can_use and not busy and not self._saving and bool(self._students)
        self.present_button.setEnabled(markable)
        self.absent_button.setEnabled(markable)
        self.save_button.setEnabled(markable)

    def _load_scope(self):
        self._scope_id += 1
        scope_id = self._scope_id
        self._set_busy(True)
        self.status.setText("Loading sessions and classes…")
        worker = BackgroundTask(
            scope_id,
            lambda: db.load_attendance_options(self.username),
        )
        worker.signals.finished.connect(self._scope_loaded)
        self._workers[("scope", scope_id)] = worker
        self._thread_pool.start(worker)

    def _scope_loaded(self, scope_id, result, error):
        self._workers.pop(("scope", scope_id), None)
        if scope_id != self._scope_id:
            return
        if error is not None:
            self.status.setText("Could not load attendance setup")
            self._set_busy(False)
            QMessageBox.warning(self, "Could not load attendance setup", str(error))
            return
        self._sessions = result["sessions"]
        self._classes_by_session = result["classes_by_session"]
        self._teacher_linked = result["teacher_linked"]
        self.session_filter.blockSignals(True)
        self.session_filter.clear()
        self.session_filter.addItem("Select session", None)
        for row in self._sessions:
            self.session_filter.addItem(row["name"], int(row["id"]))
        current = next((row for row in self._sessions if row["is_current"]), None)
        if current:
            self.session_filter.setCurrentIndex(self.session_filter.findData(int(current["id"])))
        elif self._sessions:
            self.session_filter.setCurrentIndex(1)
        self.session_filter.blockSignals(False)
        self._populate_classes()
        self._set_busy(False)
        if self.session_filter.currentData() is not None and self.class_filter.currentData() is not None:
            self.load_register()
        else:
            if self.is_teacher and not self._teacher_linked:
                self.status.setText("Ask an administrator to link this login to an active teacher record.")
            elif self.is_teacher:
                self.status.setText("No class-subject assignments are linked to this teacher for attendance.")
            else:
                self.status.setText("Create an academic session and class before taking attendance.")

    def _session_changed(self, *_args):
        if not self._sessions:
            return
        self._populate_classes()
        if self.session_filter.currentData() is not None and self.class_filter.currentData() is not None:
            self.load_register()

    def _populate_classes(self):
        session_id = self.session_filter.currentData()
        self._classes = self._classes_by_session.get(int(session_id), []) if session_id is not None else []
        self.class_filter.blockSignals(True)
        self.class_filter.clear()
        self.class_filter.addItem("Select class", None)
        for row in self._classes:
            stage = db.EDUCATIONAL_STAGES.get(row["educational_stage"], "Stage not set")
            label = f"{row['class_name']} {row['section']} — {stage}".strip()
            self.class_filter.addItem(label, int(row["id"]))
        if self._classes:
            self.class_filter.setCurrentIndex(1)
        self.class_filter.blockSignals(False)

    def load_register(self):
        session_id = self.session_filter.currentData()
        class_id = self.class_filter.currentData()
        if session_id is None or class_id is None:
            self.status.setText("Select an academic session and class.")
            return
        session_id, class_id = int(session_id), int(class_id)
        attendance_date = self.attendance_date.date().toString("yyyy-MM-dd")
        self._load_id += 1
        load_id = self._load_id
        self._set_busy(True)
        self._students = []
        self.status.setText("Loading enrolled students…")
        worker = BackgroundTask(
            load_id,
            lambda: db.load_attendance_register(session_id, class_id, attendance_date, self.username),
        )
        worker.signals.finished.connect(self._register_loaded)
        self._workers[("load", load_id)] = worker
        self._thread_pool.start(worker)

    def _register_loaded(self, load_id, result, error):
        self._workers.pop(("load", load_id), None)
        if load_id != self._load_id:
            return
        if error is not None:
            self.table.setRowCount(0)
            self._students = []
            self._set_busy(False)
            self.status.setText("Could not load attendance register")
            QMessageBox.warning(self, "Could not load attendance register", str(error))
            return
        self._students = result["students"]
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(self._students))
        for row_index, student in enumerate(self._students):
            self.table.setItem(row_index, 0, QTableWidgetItem(student["admission_no"]))
            self.table.setItem(
                row_index, 1,
                QTableWidgetItem(f"{student['last_name']}, {student['first_name']}"),
            )
            self.table.setItem(row_index, 3, QTableWidgetItem(str(student["enrollment_id"])))
            status = QComboBox()
            if student["status"] is None:
                status.addItem("Unmarked", None)
            status.addItem("Present", "present")
            status.addItem("Absent", "absent")
            if student["status"]:
                status.setCurrentIndex(status.findData(student["status"]))
            status.setEnabled(self.can_use)
            self.table.setCellWidget(row_index, 2, status)
        self.table.setUpdatesEnabled(True)
        self.table.viewport().update()
        self._set_busy(False)
        marked = sum(student["status"] is not None for student in self._students)
        self.status.setText(
            f"{len(self._students)} student(s); {marked} explicitly marked. "
            "Unmarked students are not treated as absent."
        )

    def _mark_all(self, status: str):
        for row_index in range(self.table.rowCount()):
            combo = self.table.cellWidget(row_index, 2)
            if combo is not None:
                combo.setCurrentIndex(combo.findData(status))

    def save_register(self):
        if not self.can_use or self._loading or self._saving:
            return
        marks = {}
        changed_existing = False
        for row_index, student in enumerate(self._students):
            combo = self.table.cellWidget(row_index, 2)
            status = combo.currentData() if combo is not None else None
            if status is None:
                continue
            marks[int(student["enrollment_id"])] = status
            if student["status"] is not None and student["status"] != status:
                changed_existing = True
        if not marks:
            QMessageBox.information(self, "No marks to save", "Mark at least one student present or absent.")
            return
        reason = ""
        if changed_existing:
            reason, accepted = QInputDialog.getText(
                self, "Attendance correction", "Reason for changing saved attendance marks:"
            )
            if not accepted:
                return
        args = (
            int(self.session_filter.currentData()),
            int(self.class_filter.currentData()),
            self.attendance_date.date().toString("yyyy-MM-dd"),
            marks,
            self.username,
            reason,
        )
        self._saving = True
        self._set_busy(True)
        self.status.setText("Saving attendance…")
        self._save_id += 1
        save_id = self._save_id
        worker = BackgroundTask(save_id, lambda: db.save_attendance_marks(*args))
        worker.signals.finished.connect(self._save_finished)
        self._workers[("save", save_id)] = worker
        self._thread_pool.start(worker)

    def _save_finished(self, save_id, result, error):
        self._workers.pop(("save", save_id), None)
        if save_id != self._save_id:
            return
        self._saving = False
        if error is not None:
            self._set_busy(False)
            self.status.setText("Attendance was not saved")
            QMessageBox.warning(self, "Could not save attendance", str(error))
            return
        _register_id, changed = result
        self.status.setText(f"Saved {changed} attendance mark(s). Loading the register…")
        self.load_register()

    def refresh(self):
        if self._sessions and self._classes:
            self.load_register()
        else:
            self._load_scope()

    def show_summary(self):
        session_id = self.session_filter.currentData()
        class_id = self.class_filter.currentData()
        if session_id is None or class_id is None:
            return
        dialog = AttendanceSummaryDialog(
            int(session_id), int(class_id), self.attendance_date.date(), self.username, self
        )
        dialog.exec()


class AttendanceSummaryDialog(QDialog):
    def __init__(self, session_id: int, class_id: int, selected_date: QDate, actor: str, parent=None):
        super().__init__(parent)
        self.actor = actor
        self.setWindowTitle("Attendance summary")
        self.resize(760, 480)
        self._load_id = 0
        self._workers = {}
        self._thread_pool = QThreadPool.globalInstance()
        layout = QVBoxLayout(self)
        filters = QFormLayout()
        self.starts_on = QDateEdit(selected_date)
        self.ends_on = QDateEdit(selected_date)
        for control in (self.starts_on, self.ends_on):
            control.setCalendarPopup(True)
            control.setDisplayFormat("yyyy-MM-dd")
        filters.addRow("From", self.starts_on)
        filters.addRow("Through", self.ends_on)
        layout.addLayout(filters)
        self.load_button = QPushButton("Load summary")
        layout.addWidget(self.load_button)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Attendance Date", "Enrolled Students", "Present Students", "Absent Students", "Students Not Marked"])
        configure_table_headers(self.table)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)
        self.status = QLabel("Choose a date range to summarize saved registers.")
        layout.addWidget(self.status)
        self.load_button.clicked.connect(lambda: self.load(session_id, class_id))

    def load(self, session_id: int, class_id: int):
        self._load_id += 1
        load_id = self._load_id
        starts_on = self.starts_on.date().toString("yyyy-MM-dd")
        ends_on = self.ends_on.date().toString("yyyy-MM-dd")
        self.load_button.setEnabled(False)
        self.status.setText("Loading attendance summary…")
        worker = BackgroundTask(
            load_id,
            lambda: db.load_attendance_summary(session_id, class_id, starts_on, ends_on, self.actor),
        )
        worker.signals.finished.connect(self._loaded)
        self._workers[load_id] = worker
        self._thread_pool.start(worker)

    def _loaded(self, load_id, rows, error):
        self._workers.pop(load_id, None)
        if load_id != self._load_id:
            return
        self.load_button.setEnabled(True)
        if error is not None:
            self.status.setText("Could not load attendance summary")
            QMessageBox.warning(self, "Could not load attendance summary", str(error))
            return
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = (row["attendance_date"], row["enrolled"], row["present"], row["absent"], row["unmarked"])
            for column, value in enumerate(values):
                self.table.setItem(index, column, QTableWidgetItem(str(value)))
        if rows:
            self.status.setText(
                f"{len(rows)} saved register(s). Unmarked students are shown separately, not counted as absent."
            )
        else:
            self.status.setText("No saved attendance registers in this date range.")
