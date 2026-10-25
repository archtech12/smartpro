"""Administrator UI for explicit student enrollment and class-transfer history."""

from __future__ import annotations

from PyQt6.QtCore import QDate, QThreadPool, QTimer, Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
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
from . import database as db
from .workers import BackgroundTask


class EnrollmentDialog(QDialog):
    def __init__(self, classes, sessions, session_id=None, parent=None):
        super().__init__(parent)
        self._sessions = sessions
        self._student_search_id = 0
        self._student_workers = {}
        self._thread_pool = QThreadPool.globalInstance()
        self.setWindowTitle("Enroll student")
        form = QFormLayout(self)
        self.student = QComboBox()
        self.student.addItem("Search for a student below", None)
        self.student_search = QLineEdit()
        self.student_search.setPlaceholderText("Type at least 2 characters of name or admission number")
        self.student_search_timer = QTimer(self)
        self.student_search_timer.setSingleShot(True)
        self.student_search_timer.setInterval(250)
        self.student_search_timer.timeout.connect(self._load_student_matches)
        self.student_search.textChanged.connect(self._student_search_changed)
        self.student_matches = QLabel("Enter at least 2 characters to search active students.")
        self.class_section = QComboBox()
        for item in classes:
            stage = db.EDUCATIONAL_STAGES.get(item["educational_stage"], "Stage not set")
            label = f"{item['class_name']} {item['section']} — {stage}".strip()
            self.class_section.addItem(label, int(item["id"]))
        self.session = QComboBox()
        for item in sessions:
            self.session.addItem(item["name"], int(item["id"]))
        if session_id is not None:
            index = self.session.findData(int(session_id))
            if index >= 0:
                self.session.setCurrentIndex(index)
        self.enrolled_on = QDateEdit()
        self.enrolled_on.setCalendarPopup(True)
        self.enrolled_on.setDisplayFormat("yyyy-MM-dd")
        self.session.currentIndexChanged.connect(self._set_session_date_range)
        self._set_session_date_range()
        form.addRow("Find student", self.student_search)
        form.addRow("Matching student", self.student)
        form.addRow("Search results", self.student_matches)
        form.addRow("Class and section", self.class_section)
        form.addRow("Academic session", self.session)
        form.addRow("Enrollment date", self.enrolled_on)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _set_session_date_range(self):
        session_id = self.session.currentData()
        session = next((item for item in self._sessions if int(item["id"]) == session_id), None)
        if session:
            start = QDate.fromString(session["starts_on"], "yyyy-MM-dd")
            end = QDate.fromString(session["ends_on"], "yyyy-MM-dd")
            self.enrolled_on.setMinimumDate(start)
            self.enrolled_on.setMaximumDate(end)
            today = QDate.currentDate()
            self.enrolled_on.setDate(today if start <= today <= end else start)

    def _load_student_matches(self):
        search = self.student_search.text().strip()
        search_id = self._student_search_id
        if len(search) < 2:
            self.student_matches.setText("Enter at least 2 characters to search active students.")
            return
        worker = BackgroundTask(search_id, lambda: db.search_active_students(search, 100))
        worker.signals.finished.connect(self._student_matches_loaded)
        self._student_workers[search_id] = worker
        self.student_matches.setText("Searching…")
        self._thread_pool.start(worker)

    def _student_search_changed(self, _text):
        self._student_search_id += 1
        self.student.clear()
        self.student.addItem("Select a matching student", None)
        self.student_matches.setText("Searching…")
        self.student_search_timer.start()

    def _student_matches_loaded(self, search_id, result, error):
        self._student_workers.pop(search_id, None)
        if search_id != self._student_search_id:
            return
        if error is not None:
            self.student_matches.setText("Student search failed.")
            QMessageBox.warning(self, "Student search failed", str(error))
            return
        rows, total = result
        self.student.clear()
        self.student.addItem("Select a matching student", None)
        for item in rows:
            label = f"{item['admission_no']} — {item['last_name']}, {item['first_name']}"
            self.student.addItem(label, int(item["id"]))
        if total == 0:
            self.student_matches.setText("No active students matched.")
        elif total > len(rows):
            self.student_matches.setText(f"Showing {len(rows)} of {total}; refine the search to narrow results.")
        else:
            self.student_matches.setText(f"{total} matching student(s).")

    def _accept_if_valid(self):
        if self.student.currentData() is None:
            QMessageBox.information(self, "Select a student", "Search for and select an active student first.")
            return
        self.accept()

    def values(self):
        return (
            int(self.student.currentData()),
            int(self.class_section.currentData()),
            int(self.session.currentData()),
            self.enrolled_on.date().toString("yyyy-MM-dd"),
        )


class EnrollmentDateDialog(QDialog):
    def __init__(self, title, prompt, session, min_date=None, default_date=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        form = QFormLayout(self)
        form.addRow(QLabel(prompt))
        self.date = QDateEdit()
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("yyyy-MM-dd")
        session_start = QDate.fromString(session["starts_on"], "yyyy-MM-dd")
        self.date.setMinimumDate(max(session_start, min_date) if min_date else session_start)
        self.date.setMaximumDate(QDate.fromString(session["ends_on"], "yyyy-MM-dd"))
        self.date.setDate(default_date or self.date.minimumDate())
        form.addRow("Date", self.date)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def value(self):
        return self.date.date().toString("yyyy-MM-dd")


class TransferDialog(QDialog):
    def __init__(self, classes, session, current_class_id, enrolled_on, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Transfer student")
        form = QFormLayout(self)
        self.class_section = QComboBox()
        for item in classes:
            class_id = int(item["id"])
            if class_id != current_class_id:
                self.class_section.addItem(
                    f"{item['class_name']} {item['section']} — "
                    f"{db.EDUCATIONAL_STAGES.get(item['educational_stage'], 'Stage not set')}".strip(),
                    class_id,
                )
        self.date = QDateEdit()
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("yyyy-MM-dd")
        session_start = QDate.fromString(session["starts_on"], "yyyy-MM-dd")
        self.date.setMinimumDate(max(session_start, QDate.fromString(enrolled_on, "yyyy-MM-dd")))
        self.date.setMaximumDate(QDate.fromString(session["ends_on"], "yyyy-MM-dd"))
        self.date.setDate(self.date.minimumDate())
        form.addRow("New class and section", self.class_section)
        form.addRow("Transfer date", self.date)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return int(self.class_section.currentData()), self.date.date().toString("yyyy-MM-dd")


class ClassPromotionDialog(QDialog):
    def __init__(self, classes, sessions, actor: str, parent=None):
        super().__init__(parent)
        self.actor = actor
        self._classes = classes
        self._sessions = sessions
        self._preview_id = 0
        self._write_id = 0
        self._workers = {}
        self._thread_pool = QThreadPool.globalInstance()
        self._candidates = []
        self._busy = False
        self.setWindowTitle("Promote class roster")
        self.resize(850, 580)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Choose the source and destination explicitly. The preview includes active enrollments only; "
            "source records are retained in their original session."
        ))
        form = QFormLayout()
        self.source_session = QComboBox()
        self.destination_session = QComboBox()
        self.source_class = QComboBox()
        self.destination_class = QComboBox()
        for control in (self.source_session, self.destination_session):
            control.addItem("Select academic session", None)
            for session in sessions:
                control.addItem(session["name"], int(session["id"]))
        for control in (self.source_class, self.destination_class):
            control.addItem("Select class and section", None)
            for item in classes:
                stage = db.EDUCATIONAL_STAGES.get(item["educational_stage"], "Stage not set")
                label = f"{item['class_name']} {item['section']} — {stage}".strip()
                control.addItem(label, int(item["id"]))
        form.addRow("From session", self.source_session)
        form.addRow("From class and section", self.source_class)
        form.addRow("To session", self.destination_session)
        form.addRow("To class and section", self.destination_class)
        self.promotion_date = QDateEdit(QDate.currentDate())
        self.promotion_date.setCalendarPopup(True)
        self.promotion_date.setDisplayFormat("yyyy-MM-dd")
        form.addRow("Enrollment date", self.promotion_date)
        layout.addLayout(form)

        self.preview_button = QPushButton("Preview students")
        self.promote_button = QPushButton("Promote selected students")
        self.promote_button.setObjectName("primary")
        self.promote_button.setEnabled(False)
        actions = QHBoxLayout()
        actions.addWidget(self.preview_button)
        actions.addWidget(self.promote_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Select Student", "Admission Number", "Student Name", "Enrollment Eligibility"])
        configure_table_headers(self.table)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)
        self.status = QLabel("Preview the roster before creating destination enrollments.")
        layout.addWidget(self.status)
        self.table.itemChanged.connect(self._update_promote_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.reject)
        layout.addWidget(buttons)

        for control in (self.source_session, self.destination_session, self.source_class, self.destination_class):
            control.currentIndexChanged.connect(self._invalidate_preview)
        self.destination_session.currentIndexChanged.connect(self._set_promotion_date_range)
        self.promotion_date.dateChanged.connect(self._invalidate_preview)
        self.preview_button.clicked.connect(self.preview)
        self.promote_button.clicked.connect(self.promote_selected)

    def _selection(self):
        values = (
            self.source_session.currentData(), self.source_class.currentData(),
            self.destination_session.currentData(), self.destination_class.currentData(),
        )
        if any(value is None for value in values):
            return None
        return tuple(int(value) for value in values)

    def _invalidate_preview(self, *_args):
        if self._busy:
            return
        self._preview_id += 1
        self._candidates = []
        self.table.setRowCount(0)
        self.promote_button.setEnabled(False)
        if not self._busy:
            self.status.setText("Preview the roster before creating destination enrollments.")

    def _set_promotion_date_range(self, *_args):
        session_id = self.destination_session.currentData()
        session = next((row for row in self._sessions if int(row["id"]) == session_id), None)
        if not session:
            return
        start = QDate.fromString(session["starts_on"], "yyyy-MM-dd")
        end = QDate.fromString(session["ends_on"], "yyyy-MM-dd")
        self.promotion_date.setMinimumDate(start)
        self.promotion_date.setMaximumDate(end)
        today = QDate.currentDate()
        self.promotion_date.setDate(today if start <= today <= end else start)

    def preview(self):
        selection = self._selection()
        if selection is None:
            self.status.setText("Select both sessions and both classes first.")
            return
        source_session, source_class, destination_session, destination_class = selection
        if source_session == destination_session:
            self.status.setText("Choose a different destination session.")
            return
        self._preview_id += 1
        preview_id = self._preview_id
        enrolled_on = self.promotion_date.date().toString("yyyy-MM-dd")
        worker = BackgroundTask(preview_id, lambda: db.preview_student_promotion(
            source_session, source_class, destination_session, destination_class, enrolled_on, self.actor
        ))
        worker.signals.finished.connect(self._preview_finished)
        self._workers[("preview", preview_id)] = worker
        self._busy = True
        self.preview_button.setEnabled(False)
        self.promote_button.setEnabled(False)
        for control in (self.source_session, self.source_class, self.destination_session, self.destination_class, self.promotion_date):
            control.setEnabled(False)
        self.status.setText("Checking the selected class roster…")
        self._thread_pool.start(worker)

    def _preview_finished(self, preview_id, result, error):
        self._workers.pop(("preview", preview_id), None)
        if preview_id != self._preview_id:
            return
        self._busy = False
        self.preview_button.setEnabled(True)
        for control in (self.source_session, self.source_class, self.destination_session, self.destination_class, self.promotion_date):
            control.setEnabled(True)
        if error is not None:
            self._candidates = []
            self.status.setText("Could not prepare the promotion preview.")
            QMessageBox.warning(self, "Promotion preview failed", str(error))
            return
        self._candidates = result["students"]
        self.table.setRowCount(len(self._candidates))
        for index, student in enumerate(self._candidates):
            choice = QTableWidgetItem()
            choice.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
            choice.setCheckState(Qt.CheckState.Checked if student["eligible"] else Qt.CheckState.Unchecked)
            choice.setData(Qt.ItemDataRole.UserRole, int(student["student_id"]))
            if not student["eligible"]:
                choice.setFlags(choice.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            self.table.setItem(index, 0, choice)
            self.table.setItem(index, 1, QTableWidgetItem(student["admission_no"]))
            self.table.setItem(index, 2, QTableWidgetItem(f"{student['last_name']}, {student['first_name']}"))
            self.table.setItem(index, 3, QTableWidgetItem("Eligible" if student["eligible"] else student["reason"]))
        self.status.setText(
            f"{result['eligible_count']} eligible; {result['skipped_count']} skipped. "
            "Uncheck any eligible students you do not want to promote."
        )
        self._update_promote_button()

    def _selected_student_ids(self):
        ids = []
        for index in range(self.table.rowCount()):
            choice = self.table.item(index, 0)
            if choice and choice.checkState() == Qt.CheckState.Checked:
                ids.append(int(choice.data(Qt.ItemDataRole.UserRole)))
        return ids

    def _update_promote_button(self, *_args):
        self.promote_button.setEnabled(
            not self._busy and bool(self._candidates) and bool(self._selected_student_ids())
        )

    def promote_selected(self):
        student_ids = self._selected_student_ids()
        selection = self._selection()
        if not student_ids or selection is None or self._busy:
            return
        source_session, source_class, destination_session, destination_class = selection
        destination_name = self.destination_session.currentText()
        answer = QMessageBox.question(
            self,
            "Confirm class promotion",
            f"Create {len(student_ids)} enrollment(s) in {destination_name}? "
            "The source-session enrollments will be retained.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._write_id += 1
        write_id = self._write_id
        enrolled_on = self.promotion_date.date().toString("yyyy-MM-dd")
        worker = BackgroundTask(write_id, lambda: db.promote_class_students(
            source_session, source_class, destination_session, destination_class,
            enrolled_on, student_ids, self.actor,
        ))
        worker.signals.finished.connect(self._promotion_finished)
        self._workers[("write", write_id)] = worker
        self._busy = True
        self._candidates = []
        self.table.setRowCount(0)
        for control in (self.source_session, self.source_class, self.destination_session, self.destination_class, self.promotion_date):
            control.setEnabled(False)
        self.preview_button.setEnabled(False)
        self.promote_button.setEnabled(False)
        self.status.setText("Creating destination enrollments…")
        self._thread_pool.start(worker)

    def _promotion_finished(self, write_id, count, error):
        self._workers.pop(("write", write_id), None)
        self._busy = False
        self.preview_button.setEnabled(True)
        for control in (self.source_session, self.source_class, self.destination_session, self.destination_class, self.promotion_date):
            control.setEnabled(True)
        if error is not None:
            self._invalidate_preview()
            self.status.setText("Promotion was not completed.")
            QMessageBox.warning(self, "Promotion failed", str(error))
            return
        QMessageBox.information(self, "Promotion complete", f"Created {count} destination enrollment(s).")
        self.accept()


class EnrollmentPage(QWidget):
    def __init__(self, username: str, role: str):
        super().__init__()
        self.username = username
        self.is_admin = role == "admin"
        self._rows = []
        self._classes = []
        self._sessions = []
        self._can_enroll = False
        self._load_id = 0
        self._page = 0
        self._page_size = 200
        self._total_rows = 0
        self._loading = False
        self._writing = False
        self._write_id = 0
        self._write_workers = {}
        self._write_messages = {}
        self._workers = {}
        self._thread_pool = QThreadPool.globalInstance()
        layout = QVBoxLayout(self)
        title = QLabel("Student enrollments")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(QLabel("Record session-specific class placement and retain transfer history."))
        filters = QHBoxLayout()
        filters.addWidget(QLabel("Academic session"))
        self.session_filter = QComboBox()
        self.session_filter.addItem("All sessions", None)
        for session in db.list_academic_sessions():
            self.session_filter.addItem(session["name"], int(session["id"]))
        self.session_filter.currentIndexChanged.connect(lambda _index: self._filter_changed())
        self.session_filter.setMaximumWidth(320)
        filters.addWidget(self.session_filter)
        filters.addStretch()
        layout.addLayout(filters)
        actions = QHBoxLayout()
        self.enroll_button = QPushButton("Enroll student")
        self.promote_button = QPushButton("Promote class roster")
        self.transfer_button = QPushButton("Transfer selected")
        self.complete_button = QPushButton("Complete selected")
        self.withdraw_button = QPushButton("Withdraw selected")
        for button in (self.enroll_button, self.promote_button, self.transfer_button, self.complete_button, self.withdraw_button):
            button.setEnabled(self.is_admin)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.table = QTableWidget()
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels(
            ["Enrollment ID", "Admission Number", "Student Name", "Class", "Section", "Academic Session", "Enrollment Date", "Exit Date", "Enrollment Status"]
        )
        configure_table_headers(self.table)
        self.table.setColumnHidden(0, True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)
        navigation = QHBoxLayout()
        self.page_status = QLabel()
        self.previous_page = QPushButton("Previous")
        self.next_page = QPushButton("Next")
        self.previous_page.clicked.connect(self.go_previous_page)
        self.next_page.clicked.connect(self.go_next_page)
        navigation.addWidget(self.page_status)
        navigation.addStretch()
        navigation.addWidget(self.previous_page)
        navigation.addWidget(self.next_page)
        layout.addLayout(navigation)
        self.enroll_button.clicked.connect(self.enroll_student)
        self.promote_button.clicked.connect(self.promote_class_roster)
        self.transfer_button.clicked.connect(self.transfer_selected)
        self.complete_button.clicked.connect(lambda: self.close_selected("completed"))
        self.withdraw_button.clicked.connect(lambda: self.close_selected("withdrawn"))
        self.table.itemSelectionChanged.connect(self._update_action_state)
        self.refresh()

    def refresh(self):
        sessions = db.list_academic_sessions()
        self._sessions = sessions
        selected_session = self.session_filter.currentData()
        self.session_filter.blockSignals(True)
        self.session_filter.clear()
        self.session_filter.addItem("All sessions", None)
        for session in sessions:
            self.session_filter.addItem(session["name"], int(session["id"]))
        selected_index = self.session_filter.findData(selected_session)
        self.session_filter.setCurrentIndex(max(0, selected_index))
        self.session_filter.blockSignals(False)
        self._classes = db.list_class_sections()
        self._can_enroll = bool(sessions and self._classes and db.has_active_students())
        session_id = self.session_filter.currentData()
        self._load_id += 1
        self._loading = True
        load_id = self._load_id
        selected_session_id = int(session_id) if session_id is not None else None
        worker = BackgroundTask(
            load_id,
            lambda: db.list_student_enrollments_page(
                selected_session_id, self._page_size, self._page * self._page_size
            ),
        )
        worker.signals.finished.connect(self._rows_loaded)
        self._workers[load_id] = worker
        self.page_status.setText("Loading…")
        self.previous_page.setEnabled(False)
        self.next_page.setEnabled(False)
        self.table.setEnabled(False)
        self._update_action_state()
        self._thread_pool.start(worker)

    def _rows_loaded(self, load_id, result, error):
        self._workers.pop(load_id, None)
        if load_id != self._load_id:
            return
        if error is not None:
            self._loading = False
            self.table.setEnabled(True)
            QMessageBox.warning(self, "Could not load enrollments", str(error))
            self.page_status.setText("Could not load enrollments")
            self._update_action_state()
            return
        self._rows, self._total_rows = result
        if not self._rows and self._page > 0 and self._total_rows <= self._page * self._page_size:
            self._page = max(0, (self._total_rows - 1) // self._page_size)
            self.refresh()
            return
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(self._rows))
            for row_index, enrollment in enumerate(self._rows):
                values = (
                    enrollment["id"],
                    enrollment["admission_no"],
                    f"{enrollment['last_name']}, {enrollment['first_name']}",
                    enrollment["class_name"],
                    enrollment["section"],
                    enrollment["session_name"],
                    enrollment["enrolled_on"],
                    enrollment["left_on"] or "",
                    enrollment["status"].title(),
                )
                for column, value in enumerate(values):
                    self.table.setItem(row_index, column, QTableWidgetItem(str(value)))
        finally:
            self.table.setUpdatesEnabled(True)
            self.table.viewport().update()
            self.table.setEnabled(True)
        self._loading = False
        start = self._page * self._page_size + 1 if self._total_rows else 0
        end = self._page * self._page_size + len(self._rows)
        self.page_status.setText(f"Showing {start}–{end} of {self._total_rows}")
        self.previous_page.setEnabled(self._page > 0)
        self.next_page.setEnabled(end < self._total_rows)
        self._update_action_state()

    def _filter_changed(self):
        self._page = 0
        self.refresh()

    def go_previous_page(self):
        if self._page > 0:
            self._page -= 1
            self.refresh()

    def go_next_page(self):
        if (self._page + 1) * self._page_size < self._total_rows:
            self._page += 1
            self.refresh()

    def _selected(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self._rows):
            return None
        return self._rows[row]

    def _update_action_state(self):
        enrollment = self._selected()
        active = bool(enrollment and enrollment["status"] == "active")
        ready = not self._loading and not self._writing
        self.enroll_button.setEnabled(self.is_admin and self._can_enroll and ready)
        self.promote_button.setEnabled(self.is_admin and bool(self._classes) and len(self._sessions) >= 2 and ready)
        self.transfer_button.setEnabled(self.is_admin and active and len(self._classes) > 1 and ready)
        self.complete_button.setEnabled(self.is_admin and active and ready)
        self.withdraw_button.setEnabled(self.is_admin and active and ready)

    def enroll_student(self):
        sessions = db.list_academic_sessions()
        if not self._can_enroll or not self._classes or not sessions:
            QMessageBox.information(
                self,
                "Setup required",
                "Add an active student, a class and section, and an academic session before enrolling students.",
            )
            return
        selected_session = self.session_filter.currentData()
        dialog = EnrollmentDialog(self._classes, sessions, selected_session, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.values()
            self._start_write(
                lambda: db.create_student_enrollment(*values, actor=self.username),
                "Could not enroll student",
            )

    def promote_class_roster(self):
        if not self.is_admin or self._loading or self._writing:
            return
        sessions = [dict(row) for row in db.list_academic_sessions()]
        if len(sessions) < 2 or len(self._classes) < 1:
            QMessageBox.information(
                self,
                "Setup required",
                "Add at least two academic sessions and one class and section before promoting students.",
            )
            return
        dialog = ClassPromotionDialog(self._classes, sessions, self.username, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._page = 0
            self.refresh()

    def transfer_selected(self):
        enrollment = self._selected()
        if not enrollment or enrollment["status"] != "active":
            QMessageBox.information(self, "Select an active enrollment", "Select an active enrollment to transfer.")
            return
        classes = self._classes
        sessions = db.list_academic_sessions()
        session = next((item for item in sessions if int(item["id"]) == enrollment["academic_session_id"]), None)
        if not session:
            QMessageBox.warning(self, "Session unavailable", "The enrollment's academic session could not be found.")
            return
        dialog = TransferDialog(classes, session, int(enrollment["class_id"]), enrollment["enrolled_on"], self)
        if dialog.class_section.count() == 0:
            QMessageBox.information(self, "No destination class", "Add another class and section before transferring.")
            return
        if dialog.exec() == QDialog.DialogCode.Accepted:
            class_id, transfer_on = dialog.values()
            enrollment_id = int(enrollment["id"])
            self._start_write(
                lambda: db.transfer_student_enrollment(
                    enrollment_id, class_id, transfer_on, self.username
                ),
                "Could not transfer student",
            )

    def close_selected(self, status: str):
        enrollment = self._selected()
        if not enrollment or enrollment["status"] != "active":
            QMessageBox.information(self, "Select an active enrollment", "Select an active enrollment first.")
            return
        sessions = db.list_academic_sessions()
        session = next((item for item in sessions if int(item["id"]) == enrollment["academic_session_id"]), None)
        if not session:
            QMessageBox.warning(self, "Session unavailable", "The enrollment's academic session could not be found.")
            return
        label = "Complete" if status == "completed" else "Withdraw"
        dialog = EnrollmentDateDialog(
            f"{label} student enrollment",
            f"Record the {status} date for this enrollment.",
            session,
            min_date=QDate.fromString(enrollment["enrolled_on"], "yyyy-MM-dd"),
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            enrollment_id = int(enrollment["id"])
            left_on = dialog.value()
            self._start_write(
                lambda: db.close_student_enrollment(
                    enrollment_id, status, left_on, self.username
                ),
                "Could not update enrollment",
            )

    def _start_write(self, operation, error_message):
        if self._writing:
            return
        self._writing = True
        self._write_id += 1
        write_id = self._write_id
        worker = BackgroundTask(write_id, operation)
        worker.signals.finished.connect(self._write_finished)
        self._write_workers[write_id] = worker
        self._write_messages[write_id] = error_message
        self._update_action_state()
        self.page_status.setText("Saving…")
        self._thread_pool.start(worker)

    def _write_finished(self, write_id, _result, error):
        self._write_workers.pop(write_id, None)
        if write_id != self._write_id:
            return
        error_message = self._write_messages.pop(write_id, "Could not save enrollment")
        self._writing = False
        if error is not None:
            QMessageBox.warning(self, error_message, str(error))
            self._update_action_state()
            return
        self.refresh()
