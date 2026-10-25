"""Small PyQt views for academic sessions and term setup."""

from __future__ import annotations

from PyQt6.QtCore import QDate, QThreadPool
from PyQt6.QtWidgets import (
    QCheckBox,
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
    QSpinBox,
    QScrollArea,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .table_helpers import configure_table_headers
from .ui_helpers import wrapped_label
from . import database as db
from .workers import BackgroundTask


def _date_edit(value: str | None = None) -> QDateEdit:
    edit = QDateEdit()
    edit.setCalendarPopup(True)
    edit.setDisplayFormat("yyyy-MM-dd")
    edit.setDate(QDate.fromString(value, "yyyy-MM-dd") if value else QDate.currentDate())
    return edit


class SessionDialog(QDialog):
    def __init__(self, session=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit academic session" if session else "Add academic session")
        form = QFormLayout(self)
        self.name = QLineEdit(session["name"] if session else "")
        self.name.setMaxLength(100)
        self.starts_on = _date_edit(session["starts_on"] if session else None)
        self.ends_on = _date_edit(session["ends_on"] if session else None)
        if not session:
            self.ends_on.setDate(self.starts_on.date().addYears(1))
        self.is_current = QCheckBox("Current academic session")
        self.is_current.setChecked(bool(session["is_current"]) if session else False)
        form.addRow("Session name", self.name)
        form.addRow("Start date", self.starts_on)
        form.addRow("End date", self.ends_on)
        form.addRow("Status", self.is_current)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return (
            self.name.text(),
            self.starts_on.date().toString("yyyy-MM-dd"),
            self.ends_on.date().toString("yyyy-MM-dd"),
            self.is_current.isChecked(),
        )


class TermDialog(QDialog):
    def __init__(self, sessions, term=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit term" if term else "Add term")
        form = QFormLayout(self)
        self.session = QComboBox()
        for session in sessions:
            self.session.addItem(
                f"{session['name']} ({session['starts_on']} to {session['ends_on']})",
                int(session["id"]),
            )
        self.name = QLineEdit(term["term_name"] if term else "")
        self.name.setMaxLength(60)
        self.order = QSpinBox()
        self.order.setRange(1, 99)
        self.order.setValue(int(term["term_order"]) if term else 1)
        selected_session = int(term["academic_session_id"]) if term else None
        if selected_session is not None:
            index = self.session.findData(selected_session)
            if index >= 0:
                self.session.setCurrentIndex(index)
        self.starts_on = _date_edit(term["starts_on"] if term else None)
        self.ends_on = _date_edit(term["ends_on"] if term else None)
        if not term and sessions:
            session = next((item for item in sessions if int(item["id"]) == self.session.currentData()), sessions[0])
            start = QDate.fromString(session["starts_on"], "yyyy-MM-dd")
            end = QDate.fromString(session["ends_on"], "yyyy-MM-dd")
            self.starts_on.setDate(start)
            self.ends_on.setDate(min(start.addMonths(4), end))
        form.addRow("Academic session", self.session)
        form.addRow("Term name", self.name)
        form.addRow("Order", self.order)
        form.addRow("Start date", self.starts_on)
        form.addRow("End date", self.ends_on)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return (
            int(self.session.currentData()),
            self.name.text(),
            self.order.value(),
            self.starts_on.date().toString("yyyy-MM-dd"),
            self.ends_on.date().toString("yyyy-MM-dd"),
        )


class AcademicCalendarPage(QWidget):
    def __init__(self, username: str, role: str):
        super().__init__()
        self.username = username
        self.is_admin = role == "admin"
        self._write_id = 0
        self._write_workers = {}
        self._write_messages = {}
        self._thread_pool = QThreadPool.globalInstance()
        self._writing = False
        self._loading = False
        layout = QVBoxLayout(self)
        title = QLabel("Academic calendar")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(QLabel("Configure academic sessions and their terms."))
        self.write_status = QLabel()
        layout.addWidget(self.write_status)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.session_table, self.session_add, self.session_edit, self.session_activate = self._make_table_tab(
            "Academic sessions", "Add session", "Edit selected"
        )
        self.session_table.itemSelectionChanged.connect(self._update_session_actions)
        self.term_tab = QWidget()
        term_layout = QVBoxLayout(self.term_tab)
        selector_row = QHBoxLayout()
        selector_row.addWidget(QLabel("Academic session"))
        self.session_filter = QComboBox()
        self.session_filter.currentIndexChanged.connect(self.refresh_terms)
        selector_row.addWidget(self.session_filter)
        selector_row.addStretch()
        self.term_add = QPushButton("Add term")
        self.term_edit = QPushButton("Edit selected")
        self.term_activate = QPushButton("Set Selected Term as Active")
        selector_row.addWidget(self.term_add)
        selector_row.addWidget(self.term_edit)
        selector_row.addWidget(self.term_activate)
        term_layout.addLayout(selector_row)
        self.term_table = self._new_table(["Term ID", "Academic Term", "Term Number", "Start Date", "End Date", "Current Term"])
        self.term_table.itemSelectionChanged.connect(self._update_term_actions)
        term_layout.addWidget(self.term_table)
        self.tabs.addTab(self.term_tab, "Terms")
        for button in (self.session_add, self.session_edit, self.session_activate,
                       self.term_add, self.term_edit, self.term_activate):
            button.setEnabled(self.is_admin)
        self.session_add.clicked.connect(self.add_session)
        self.session_edit.clicked.connect(self.edit_session)
        self.session_activate.clicked.connect(self.activate_session)
        self.term_add.clicked.connect(self.add_term)
        self.term_edit.clicked.connect(self.edit_term)
        self.term_activate.clicked.connect(self.activate_term)
        self.refresh_sessions()

    def _new_table(self, headers):
        table = QTableWidget()
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        configure_table_headers(table)
        table.setColumnHidden(0, True)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        return table

    def _make_table_tab(self, label, add_label, edit_label):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        row.addStretch()
        add = QPushButton(add_label)
        edit = QPushButton(edit_label)
        activate = QPushButton("Set Selected Session as Active")
        row.addWidget(add)
        row.addWidget(edit)
        row.addWidget(activate)
        layout.addLayout(row)
        table = self._new_table(["Session ID", "Academic Session", "Start Date", "End Date", "Current Session"])
        layout.addWidget(table)
        self.tabs.addTab(tab, "Sessions")
        return table, add, edit, activate

    def _selected_id(self, table):
        row = table.currentRow()
        return int(table.item(row, 0).text()) if row >= 0 and table.item(row, 0) else None

    def refresh_sessions(self):
        sessions = db.list_academic_sessions()
        self.session_table.setRowCount(len(sessions))
        for row_index, session in enumerate(sessions):
            values = (session["id"], session["name"], session["starts_on"], session["ends_on"], "Yes" if session["is_current"] else "No")
            for column, value in enumerate(values):
                self.session_table.setItem(row_index, column, QTableWidgetItem(str(value)))
        self._update_session_actions()
        selected = self.session_filter.currentData()
        self.session_filter.blockSignals(True)
        self.session_filter.clear()
        for session in sessions:
            self.session_filter.addItem(session["name"], int(session["id"]))
        if selected is not None:
            index = self.session_filter.findData(selected)
            if index >= 0:
                self.session_filter.setCurrentIndex(index)
        self.session_filter.blockSignals(False)
        self.refresh_terms()

    def refresh_terms(self):
        session_id = self.session_filter.currentData()
        terms = db.list_terms(int(session_id)) if session_id is not None else []
        self.term_table.setRowCount(len(terms))
        for row_index, term in enumerate(terms):
            values = (term["id"], term["term_name"], term["term_order"], term["starts_on"], term["ends_on"],
                      "Yes" if term["is_current"] else "No")
            for column, value in enumerate(values):
                self.term_table.setItem(row_index, column, QTableWidgetItem(str(value)))
        self.term_add.setEnabled(self.is_admin and session_id is not None)
        self.term_edit.setEnabled(self.is_admin and session_id is not None)
        self._update_term_actions()

    def _update_session_actions(self):
        self.session_activate.setEnabled(
            self.is_admin and not self._writing and self._selected_id(self.session_table) is not None
        )

    def _update_term_actions(self):
        self.term_activate.setEnabled(
            self.is_admin and not self._writing and self._selected_id(self.term_table) is not None
        )

    def add_session(self):
        dialog = SessionDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.values()
            self._start_write(
                lambda: db.save_academic_session(*values, actor=self.username),
                "Could not save session",
            )

    def edit_session(self):
        session_id = self._selected_id(self.session_table)
        if session_id is None:
            QMessageBox.information(self, "Select a session", "Select a session to edit.")
            return
        session = next((item for item in db.list_academic_sessions() if int(item["id"]) == session_id), None)
        if session is None:
            self.refresh_sessions()
            return
        dialog = SessionDialog(session, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.values()
            self._start_write(
                lambda: db.save_academic_session(*values, actor=self.username, session_id=session_id),
                "Could not update session",
            )

    def activate_session(self):
        session_id = self._selected_id(self.session_table)
        if session_id is None:
            QMessageBox.information(self, "Select a session", "Select a session to make active.")
            return
        self._start_write(
            lambda: db.set_current_academic_session(session_id, self.username),
            "Could not set the active session",
        )

    def add_term(self):
        sessions = db.list_academic_sessions()
        if not sessions:
            QMessageBox.information(self, "Create a session first", "Add an academic session before adding terms.")
            return
        dialog = TermDialog(sessions, parent=self)
        session_id = int(self.session_filter.currentData())
        index = dialog.session.findData(session_id)
        if index >= 0:
            dialog.session.setCurrentIndex(index)
        dialog.order.setValue(len(db.list_terms(session_id)) + 1)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.values()
            self._start_write(
                lambda: db.save_term(*values, actor=self.username),
                "Could not save term",
            )

    def edit_term(self):
        term_id = self._selected_id(self.term_table)
        session_id = self.session_filter.currentData()
        if term_id is None or session_id is None:
            QMessageBox.information(self, "Select a term", "Select a term to edit.")
            return
        term = next((item for item in db.list_terms(int(session_id)) if int(item["id"]) == term_id), None)
        if term is None:
            self.refresh_terms()
            return
        dialog = TermDialog(db.list_academic_sessions(), term, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = dialog.values()
            self._start_write(
                lambda: db.save_term(*values, actor=self.username, term_id=term_id),
                "Could not update term",
            )

    def activate_term(self):
        term_id = self._selected_id(self.term_table)
        if term_id is None:
            QMessageBox.information(self, "Select a term", "Select a term to make active.")
            return
        self._start_write(
            lambda: db.set_current_term(term_id, self.username),
            "Could not set the active term",
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
        self.tabs.setEnabled(False)
        self.write_status.setText("Saving changes…")
        self._thread_pool.start(worker)

    def _write_finished(self, write_id, _result, error):
        self._write_workers.pop(write_id, None)
        if write_id != self._write_id:
            return
        error_message = self._write_messages.pop(write_id, "Could not save changes")
        self._writing = False
        self.tabs.setEnabled(True)
        self.write_status.clear()
        if error is not None:
            QMessageBox.warning(self, error_message, str(error))
            return
        self.refresh_sessions()


class ClassStagePage(QWidget):
    """Require an explicit Primary or Secondary stage for every class."""

    def __init__(self, username: str, role: str):
        super().__init__()
        self.username = username
        self.is_admin = role == "admin"
        self._rows = []
        self._load_id = 0
        self._write_id = 0
        self._workers = {}
        self._thread_pool = QThreadPool.globalInstance()
        self._writing = False
        layout = QVBoxLayout(self)
        title = QLabel("Class school stage")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(wrapped_label(
            "Class stage is explicit. Existing class names are never used to guess Primary or Secondary. "
            "Set a stage before assigning subjects or mapping legacy enrollments."
        ))
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Class ID", "Class", "Section", "School Stage"])
        configure_table_headers(self.table)
        self.table.setColumnHidden(0, True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)
        footer = QHBoxLayout()
        self.status = QLabel()
        footer.addWidget(self.status)
        footer.addStretch()
        self.save_button = QPushButton("Save selected class stage")
        self.save_button.setEnabled(self.is_admin)
        self.save_button.clicked.connect(self.save_selected)
        footer.addWidget(self.save_button)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        footer.addWidget(self.refresh_button)
        layout.addLayout(footer)
        self.refresh()

    def refresh(self):
        self._load_id += 1
        load_id = self._load_id
        worker = BackgroundTask(load_id, db.list_class_stages)
        worker.signals.finished.connect(self._loaded)
        self._workers[("load", load_id)] = worker
        self.table.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.status.setText("Loading classes…")
        self._thread_pool.start(worker)

    def _loaded(self, load_id, rows, error):
        self._workers.pop(("load", load_id), None)
        if load_id != self._load_id:
            return
        self.table.setEnabled(True)
        self.refresh_button.setEnabled(True)
        if error is not None:
            self.status.setText("Could not load classes")
            QMessageBox.warning(self, "Could not load classes", str(error))
            return
        self._rows = rows
        self.table.setRowCount(len(rows))
        unassigned = 0
        incompatible = 0
        for row_index, row in enumerate(rows):
            self.table.setItem(row_index, 0, QTableWidgetItem(str(row["id"])))
            self.table.setItem(row_index, 1, QTableWidgetItem(row["class_name"]))
            self.table.setItem(row_index, 2, QTableWidgetItem(row["section"]))
            stage = QComboBox()
            stage.addItem("Needs classification", None)
            school_type = row["school_type"]
            allowed = {school_type} if school_type in db.EDUCATIONAL_STAGES else set(db.EDUCATIONAL_STAGES)
            existing_stage = row["educational_stage"]
            if existing_stage and existing_stage not in allowed:
                stage.setItemText(
                    0,
                    f"Choose allowed stage (currently {db.EDUCATIONAL_STAGES[existing_stage]}; conflicts with School Type)",
                )
                incompatible += 1
            for key, label in db.EDUCATIONAL_STAGES.items():
                item_index = stage.count()
                stage.addItem(label, key)
                if key not in allowed:
                    stage.model().item(item_index).setEnabled(False)
                    stage.model().item(item_index).setToolTip("Change School Type in School Profile to allow this stage.")
            selected = stage.findData(existing_stage) if existing_stage in allowed else 0
            stage.setCurrentIndex(max(0, selected))
            stage.setEnabled(self.is_admin and not self._writing)
            self.table.setCellWidget(row_index, 3, stage)
            unassigned += not bool(row["educational_stage"])
        school_type = rows[0]["school_type"] if rows else ""
        school_label = db.EDUCATIONAL_STAGES.get(school_type, "Primary and Secondary")
        self.status.setText(
            f"School type: {school_label}. {len(rows)} class(es); {unassigned} need a stage. "
            + (f"{incompatible} existing stage(s) conflict with School Type; select an allowed stage to correct." if incompatible else "")
        )

    def save_selected(self):
        row_index = self.table.currentRow()
        if not self.is_admin or self._writing:
            return
        if row_index < 0 or row_index >= len(self._rows):
            QMessageBox.information(self, "Select a class", "Select a class row first.")
            return
        selector = self.table.cellWidget(row_index, 3)
        stage = selector.currentData()
        if stage not in db.EDUCATIONAL_STAGES:
            QMessageBox.information(self, "Choose a stage", "Choose Primary or Secondary first.")
            return
        class_id = int(self._rows[row_index]["id"])
        self._writing = True
        self.save_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.status.setText("Saving stage…")
        self._write_id += 1
        write_id = self._write_id
        worker = BackgroundTask(
            write_id,
            lambda: db.save_class_educational_stage(class_id, stage, self.username),
        )
        worker.signals.finished.connect(self._saved)
        self._workers[("save", write_id)] = worker
        self._thread_pool.start(worker)

    def _saved(self, write_id, _result, error):
        self._workers.pop(("save", write_id), None)
        if write_id != self._write_id:
            return
        self._writing = False
        self.save_button.setEnabled(self.is_admin)
        self.refresh_button.setEnabled(True)
        if error is not None:
            self.status.setText("Stage was not saved")
            QMessageBox.warning(self, "Could not save class stage", str(error))
            return
        self.status.setText("Class stage saved")
        self.refresh()


class ClassSubjectAssignmentDialog(QDialog):
    def __init__(self, sessions, classes, subjects, teachers, assignment=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit class subject assignment" if assignment else "Assign subject to class")
        form = QFormLayout(self)
        self.session = QComboBox()
        for item in sessions:
            self.session.addItem(item["name"], int(item["id"]))
        self.class_section = QComboBox()
        for item in classes:
            stage = db.EDUCATIONAL_STAGES.get(item["educational_stage"], "Stage not set")
            self.class_section.addItem(
                f"{item['class_name']} {item['section']} — {stage}".strip(), int(item["id"])
            )
        self.subject = QComboBox()
        for item in subjects:
            self.subject.addItem(
                f"{item['subject_code']} — {item['subject_name']}", int(item["id"])
            )
        self.teacher = QComboBox()
        self.teacher.addItem("Unassigned", None)
        for item in teachers:
            self.teacher.addItem(
                f"{item['staff_no']} — {item['last_name']}, {item['first_name']}",
                int(item["id"]),
            )
        if assignment:
            for combo, key in (
                (self.session, "academic_session_id"),
                (self.class_section, "class_id"),
                (self.subject, "subject_id"),
                (self.teacher, "teacher_id"),
            ):
                combo.setCurrentIndex(combo.findData(assignment[key]))
        form.addRow("Academic session", self.session)
        form.addRow("Class and section", self.class_section)
        form.addRow("Subject", self.subject)
        form.addRow("Assigned teacher", self.teacher)
        form.addRow(QLabel("A subject can be assigned once per class and session."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _accept_if_valid(self):
        if any(combo.currentData() is None for combo in (self.session, self.class_section, self.subject)):
            QMessageBox.information(
                self, "Complete assignment", "Add an academic session, class, and subject first."
            )
            return
        self.accept()

    def values(self):
        return (
            int(self.session.currentData()),
            int(self.class_section.currentData()),
            int(self.subject.currentData()),
            self.teacher.currentData(),
        )


class BulkClassSubjectDialog(QDialog):
    """Quickly add several subjects to one class in a single step."""

    def __init__(self, sessions, classes, subjects, assignments, teachers, preferred_session_id=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Assign subjects to class")
        self.resize(560, 620)
        self.subjects = subjects
        self.assignments = assignments
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.session = QComboBox()
        for item in sessions:
            self.session.addItem(item["name"], int(item["id"]))
        self.class_section = QComboBox()
        for item in classes:
            stage = db.EDUCATIONAL_STAGES.get(item["educational_stage"], "Stage not set")
            self.class_section.addItem(
                f"{item['class_name']} {item['section']} — {stage}".strip(), int(item["id"])
            )
        form.addRow("Academic session", self.session)
        form.addRow("Class and section", self.class_section)
        self.teacher = QComboBox()
        self.teacher.addItem("Leave unassigned", None)
        for item in teachers:
            self.teacher.addItem(
                f"{item['staff_no']} — {item['last_name']}, {item['first_name']}",
                int(item["id"]),
            )
        form.addRow("Assign selected subjects to", self.teacher)
        layout.addLayout(form)
        layout.addWidget(QLabel("Select every subject this class should take:"))
        subject_tools = QHBoxLayout()
        self.subject_search = QLineEdit()
        self.subject_search.setPlaceholderText("Find by subject code, name, or level…")
        self.select_visible_button = QPushButton("Select visible")
        self.clear_visible_button = QPushButton("Clear visible")
        subject_tools.addWidget(self.subject_search, 1)
        subject_tools.addWidget(self.select_visible_button)
        subject_tools.addWidget(self.clear_visible_button)
        layout.addLayout(subject_tools)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.subject_list = QWidget()
        self.subject_layout = QVBoxLayout(self.subject_list)
        self.subject_layout.setContentsMargins(8, 8, 8, 8)
        self.subject_layout.addStretch()
        scroll.setWidget(self.subject_list)
        layout.addWidget(scroll, 1)
        self.selection_summary = QLabel()
        layout.addWidget(self.selection_summary)
        layout.addWidget(QLabel(
            "Subjects already assigned are marked and locked. The selected teacher is applied only to new subjects. "
            "You can change individual assignments later using Edit selected."
        ))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("Assign selected subjects")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.subject_search.textChanged.connect(self._filter_subject_checks)
        self.select_visible_button.clicked.connect(lambda: self._set_visible_checks(True))
        self.clear_visible_button.clicked.connect(lambda: self._set_visible_checks(False))
        self.session.currentIndexChanged.connect(self._rebuild_subject_checks)
        self.class_section.currentIndexChanged.connect(self._rebuild_subject_checks)
        if preferred_session_id is not None:
            index = self.session.findData(int(preferred_session_id))
            if index >= 0:
                self.session.setCurrentIndex(index)
        self._rebuild_subject_checks()

    def _rebuild_subject_checks(self, *_args):
        while self.subject_layout.count():
            item = self.subject_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.subject_checks = []
        session_id = self.session.currentData()
        class_id = self.class_section.currentData()
        existing_ids = {
            int(row["subject_id"])
            for row in self.assignments
            if row["academic_session_id"] == session_id and row["class_id"] == class_id
        }
        for source_subject in self.subjects:
            subject = dict(source_subject)
            subject_id = int(subject["id"])
            label = f"{subject['subject_code']} — {subject['subject_name']}"
            if subject.get("level"):
                label += f" ({subject['level']})"
            checkbox = QCheckBox(label)
            checkbox.setProperty(
                "subject_search_text",
                " ".join(str(subject.get(key, "")) for key in ("subject_code", "subject_name", "level")).casefold(),
            )
            if subject_id in existing_ids:
                checkbox.setChecked(True)
                checkbox.setEnabled(False)
                checkbox.setToolTip("Already assigned to this class for the selected session.")
            else:
                checkbox.toggled.connect(self._update_selection_summary)
            self.subject_layout.addWidget(checkbox)
            self.subject_checks.append((subject_id, checkbox))
        self.subject_layout.addStretch()
        self._filter_subject_checks()
        self._update_selection_summary()

    def _filter_subject_checks(self, *_args):
        query = self.subject_search.text().strip().casefold()
        for _subject_id, checkbox in self.subject_checks:
            searchable = checkbox.property("subject_search_text") or ""
            checkbox.setVisible(not query or query in searchable)

    def _set_visible_checks(self, checked):
        for _subject_id, checkbox in self.subject_checks:
            if checkbox.isEnabled() and not checkbox.isHidden():
                checkbox.setChecked(checked)

    def _update_selection_summary(self, *_args):
        count = sum(box.isChecked() and box.isEnabled() for _, box in self.subject_checks)
        self.selection_summary.setText(f"{count} new subject(s) selected")
        self.save_button.setEnabled(count > 0)

    def _accept_if_valid(self):
        if self.session.currentData() is None or self.class_section.currentData() is None:
            QMessageBox.information(self, "Complete selection", "Select an academic session and class.")
            return
        if not self.selected_subject_ids():
            QMessageBox.information(self, "Select subjects", "Tick at least one subject to add.")
            return
        self.accept()

    def selected_subject_ids(self):
        return [subject_id for subject_id, box in self.subject_checks if box.isChecked() and box.isEnabled()]

    def values(self):
        return (
            int(self.session.currentData()),
            int(self.class_section.currentData()),
            self.selected_subject_ids(),
            self.teacher.currentData(),
        )


class ClassSubjectAssignmentsPage(QWidget):
    def __init__(self, username: str, role: str):
        super().__init__()
        self.username = username
        self.is_admin = role == "admin"
        self._sessions = []
        self._classes = []
        self._subjects = []
        self._teachers = []
        self._rows = []
        self._all_assignments = []
        self._load_id = 0
        self._write_id = 0
        self._workers = {}
        self._write_workers = {}
        self._thread_pool = QThreadPool.globalInstance()
        self._writing = False
        layout = QVBoxLayout(self)
        title = QLabel("Class subject assignments")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(wrapped_label(
            "Set the subjects offered to each class for a session and optionally assign an active teacher."
        ))
        self.setup_hint = QLabel(
            "Setup order: Main → Subjects (add each subject) · Settings → Academic Calendar (add a session) · "
            "Settings → Class Stages (set Primary or Secondary for each class) · then assign subjects below."
        )
        self.setup_hint.setWordWrap(True)
        self.setup_hint.setObjectName("info")
        layout.addWidget(self.setup_hint)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Academic session"))
        self.session_filter = QComboBox()
        self.session_filter.addItem("All sessions", None)
        self.session_filter.currentIndexChanged.connect(self.refresh)
        controls.addWidget(self.session_filter)
        controls.addStretch()
        self.add_button = QPushButton("Assign subjects")
        self.edit_button = QPushButton("Edit selected")
        self.delete_button = QPushButton("Delete selected")
        for button in (self.add_button, self.edit_button, self.delete_button):
            button.setEnabled(self.is_admin)
            controls.addWidget(button)
        layout.addLayout(controls)
        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels(
            ["Assignment ID", "Academic Session", "Class", "Section", "School Stage", "Subject Code", "Subject Name", "Assigned Teacher"]
        )
        configure_table_headers(self.table)
        self.table.setColumnHidden(0, True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)
        self.status = QLabel()
        layout.addWidget(self.status)
        self.add_button.clicked.connect(self.add_assignment)
        self.edit_button.clicked.connect(self.edit_assignment)
        self.delete_button.clicked.connect(self.delete_assignment)
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.refresh()

    def _selected(self):
        row = self.table.currentRow()
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def _update_actions(self):
        ready = self.is_admin and not self._writing and not self._loading
        selected = self._selected() is not None
        self.add_button.setEnabled(ready)
        self.edit_button.setEnabled(ready and selected)
        self.delete_button.setEnabled(ready and selected)

    def refresh(self, *_args):
        self._loading = True
        self._update_actions()
        self._load_id += 1
        load_id = self._load_id
        session_id = self.session_filter.currentData()
        worker = BackgroundTask(
            load_id,
            lambda: db.load_class_subject_page_data(None),
        )
        worker.signals.finished.connect(self._data_loaded)
        self._workers[load_id] = worker
        self.table.setEnabled(False)
        self.status.setText("Loading assignments…")
        self._thread_pool.start(worker)

    def _data_loaded(self, load_id, data, error):
        self._workers.pop(load_id, None)
        if load_id != self._load_id:
            return
        self._loading = False
        self.table.setEnabled(True)
        if error:
            self.status.setText("Could not load class subject assignments")
            self._update_actions()
            QMessageBox.warning(self, "Could not load assignments", str(error))
            return
        selected_session = self.session_filter.currentData()
        self._sessions = data["sessions"]
        self._classes = data["classes"]
        self._subjects = data["subjects"]
        self._teachers = data["teachers"]
        self.session_filter.blockSignals(True)
        self.session_filter.clear()
        self.session_filter.addItem("All sessions", None)
        for session in self._sessions:
            self.session_filter.addItem(session["name"], int(session["id"]))
        index = self.session_filter.findData(selected_session)
        self.session_filter.setCurrentIndex(max(0, index))
        self.session_filter.blockSignals(False)
        self._all_assignments = data["assignments"]
        rows = self._all_assignments
        if selected_session is not None:
            rows = [row for row in rows if row["academic_session_id"] == int(selected_session)]
        self._rows = rows
        self.table.setRowCount(len(rows))
        for row_index, item in enumerate(rows):
            teacher = (
                f"{item['staff_no']} — {item['teacher_last_name']}, {item['teacher_first_name']}"
                if item["teacher_id"] is not None else "Unassigned"
            )
            values = (
                item["id"], item["session_name"], item["class_name"], item["section"],
                db.EDUCATIONAL_STAGES.get(item["educational_stage"], "Stage not set"),
                item["subject_code"], item["subject_name"], teacher,
            )
            for column, value in enumerate(values):
                self.table.setItem(row_index, column, QTableWidgetItem(str(value)))
        self.status.setText(f"{len(rows)} assignment(s)")
        self._update_actions()

    def add_assignment(self):
        assignable_classes = [item for item in self._classes if item["educational_stage"]]
        missing = []
        if not self._sessions:
            missing.append("create an academic session in Settings → Academic Calendar")
        if not self._subjects:
            missing.append("add at least one subject in Main → Subjects")
        if not assignable_classes:
            missing.append("set a Primary or Secondary stage for a class in Settings → Class Stages")
        if missing:
            QMessageBox.information(
                self, "Setup required",
                "Before assigning a subject, " + "; ".join(missing) + "."
            )
            return
        session_id = self.session_filter.currentData()
        dialog = BulkClassSubjectDialog(
            self._sessions, assignable_classes, self._subjects, self._all_assignments, self._teachers,
            preferred_session_id=session_id, parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            academic_session_id, class_id, subject_ids, teacher_id = dialog.values()
            self._start_write(
                lambda: db.add_class_subject_assignments(
                    academic_session_id, class_id, subject_ids, self.username, teacher_id
                ),
                "Could not assign subjects",
            )

    def edit_assignment(self):
        assignment = self._selected()
        if assignment is None:
            return
        assignable_classes = [item for item in self._classes if item["educational_stage"]]
        dialog = ClassSubjectAssignmentDialog(
            self._sessions, assignable_classes, self._subjects, self._teachers, assignment, self
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._save(dialog.values(), int(assignment["id"]))

    def delete_assignment(self):
        assignment = self._selected()
        if assignment is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete assignment",
            f"Delete {assignment['subject_name']} from {assignment['class_name']} "
            f"{assignment['section']} for {assignment['session_name']}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._start_write(
                lambda: db.delete_class_subject_assignment(int(assignment["id"]), self.username),
                "Could not delete assignment",
            )

    def _save(self, values, assignment_id):
        self._start_write(
            lambda: db.save_class_subject_assignment(*values, actor=self.username, assignment_id=assignment_id),
            "Could not save assignment",
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
        self._write_error_message = error_message
        self._update_actions()
        self.status.setText("Saving assignment…")
        self._thread_pool.start(worker)

    def _write_finished(self, write_id, _result, error):
        self._write_workers.pop(write_id, None)
        if write_id != self._write_id:
            return
        self._writing = False
        if error:
            QMessageBox.warning(self, self._write_error_message, str(error))
        self.refresh()


class GradeBandDialog(QDialog):
    def __init__(self, band=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit grade threshold" if band else "Add grade threshold")
        form = QFormLayout(self)
        self.threshold = QLineEdit(band["threshold"] if band else "")
        self.threshold.setMaxLength(32)
        self.threshold.setPlaceholderText("0 to 100")
        self.grade = QLineEdit(band["grade_label"] if band else "")
        self.grade.setMaxLength(30)
        self.remark = QLineEdit(band["remark"] if band else "")
        self.remark.setMaxLength(120)
        form.addRow("Minimum percentage", self.threshold)
        form.addRow("Grade label", self.grade)
        form.addRow("Remark", self.remark)
        form.addRow(QLabel(
            "Each threshold starts a band that continues up to the next threshold. "
            "The last band continues through 100. The first threshold must be 0."
        ))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return self.threshold.text(), self.grade.text(), self.remark.text()


class GradeBandSetupPage(QWidget):
    """Configure grade thresholds independently for each session and school stage."""

    def __init__(self, username: str, role: str):
        super().__init__()
        self.username = username
        self.is_admin = role == "admin"
        self._sessions = []
        self._rows = []
        self._load_id = 0
        self._write_id = 0
        self._workers = {}
        self._thread_pool = QThreadPool.globalInstance()
        self._loading = False
        self._writing = False
        layout = QVBoxLayout(self)
        title = QLabel("Grade bands by school stage")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(wrapped_label(
            "Configure each school's grading scale separately for Primary and Secondary. "
            "No grades or remarks are prefilled. A threshold starts a band; the last band extends to 100."
        ))
        selectors = QFormLayout()
        self.session = QComboBox()
        self.stage = QComboBox()
        self.stage.addItem("Primary", "primary")
        self.stage.addItem("Secondary", "secondary")
        self.session.currentIndexChanged.connect(self.refresh)
        self.stage.currentIndexChanged.connect(self.refresh)
        selectors.addRow("Academic session", self.session)
        selectors.addRow("School stage", self.stage)
        layout.addLayout(selectors)
        controls = QHBoxLayout()
        controls.addStretch()
        self.add_button = QPushButton("Add threshold")
        self.edit_button = QPushButton("Edit selected")
        self.delete_button = QPushButton("Delete selected")
        for button in (self.add_button, self.edit_button, self.delete_button):
            button.setEnabled(self.is_admin)
            controls.addWidget(button)
        layout.addLayout(controls)
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Grade Band ID", "Minimum Percentage", "Grade", "Grade Remark"])
        configure_table_headers(self.table)
        self.table.setColumnHidden(0, True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)
        self.status = QLabel()
        layout.addWidget(self.status)
        self.add_button.clicked.connect(self.add_band)
        self.edit_button.clicked.connect(self.edit_band)
        self.delete_button.clicked.connect(self.delete_band)
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.refresh()

    def _selected(self):
        row = self.table.currentRow()
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def _update_actions(self):
        ready = self.is_admin and not self._loading and not self._writing
        has_scope = self.session.currentData() is not None and self.stage.currentData() is not None
        selected = self._selected() is not None
        self.add_button.setEnabled(ready and has_scope)
        self.edit_button.setEnabled(ready and has_scope and selected)
        self.delete_button.setEnabled(ready and has_scope and selected)

    def refresh(self, *_args):
        self._loading = True
        self._update_actions()
        self._load_id += 1
        load_id = self._load_id
        session_id = self.session.currentData()
        stage = self.stage.currentData()
        selected_session = int(session_id) if session_id is not None else None
        worker = BackgroundTask(
            load_id,
            lambda: db.load_grade_band_setup_data(selected_session, stage),
        )
        worker.signals.finished.connect(
            lambda request_id, data, error: self._loaded(request_id, data, error, selected_session, stage)
        )
        self._workers[("load", load_id)] = worker
        self.table.setEnabled(False)
        self.status.setText("Loading grade scale…")
        self._thread_pool.start(worker)

    def _loaded(self, load_id, data, error, requested_session, requested_stage):
        self._workers.pop(("load", load_id), None)
        if load_id != self._load_id:
            return
        self._loading = False
        self.table.setEnabled(True)
        if error is not None:
            self.status.setText("Could not load grade scale")
            self._update_actions()
            QMessageBox.warning(self, "Could not load grade scale", str(error))
            return
        self._sessions = data["sessions"]
        self.session.blockSignals(True)
        self.session.clear()
        self.session.addItem("Select an academic session", None)
        for item in self._sessions:
            self.session.addItem(item["name"], int(item["id"]))
        selected = requested_session
        if selected is None:
            current = next((item for item in self._sessions if item["is_current"]), None)
            selected = int(current["id"]) if current else None
        index = self.session.findData(selected)
        self.session.setCurrentIndex(max(0, index))
        self.session.blockSignals(False)
        stage_index = self.stage.findData(requested_stage)
        if stage_index >= 0:
            self.stage.setCurrentIndex(stage_index)
        actual_scope = (self.session.currentData(), self.stage.currentData())
        requested_scope = (requested_session, requested_stage)
        if actual_scope != requested_scope:
            self.refresh()
            return
        self._rows = data["bands"]
        self.table.setRowCount(len(self._rows))
        for row_index, band in enumerate(self._rows):
            for column, value in enumerate((band["id"], band["threshold"], band["grade_label"], band["remark"])):
                self.table.setItem(row_index, column, QTableWidgetItem(str(value)))
        if not self._rows:
            self.status.setText("No scale configured for this session and stage. Add the first band at 0%.")
        else:
            self.status.setText(f"{len(self._rows)} threshold(s); no school-wide defaults applied")
        self._update_actions()

    def add_band(self):
        if self.session.currentData() is None:
            QMessageBox.information(self, "Select a session", "Choose an academic session first.")
            return
        dialog = GradeBandDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._save(dialog.values(), None)

    def edit_band(self):
        band = self._selected()
        if band is None:
            return
        dialog = GradeBandDialog(band, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._save(dialog.values(), int(band["id"]))

    def delete_band(self):
        band = self._selected()
        if band is None:
            return
        answer = QMessageBox.question(
            self, "Delete grade threshold", f"Delete the {band['grade_label']} threshold at {band['threshold']}%?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._start_write(
                lambda: db.delete_assessment_grade_band(int(band["id"]), self.username),
                "Could not delete grade threshold",
            )

    def _save(self, values, band_id):
        session_id = int(self.session.currentData())
        stage = self.stage.currentData()
        self._start_write(
            lambda: db.save_assessment_grade_band(
                session_id, stage, *values, actor=self.username, band_id=band_id
            ),
            "Could not save grade threshold",
        )

    def _start_write(self, operation, error_message):
        if self._writing:
            return
        self._writing = True
        self._update_actions()
        self._write_id += 1
        write_id = self._write_id
        worker = BackgroundTask(write_id, operation)
        worker.signals.finished.connect(self._write_finished)
        self._workers[("save", write_id)] = worker
        self._write_error_message = error_message
        self.status.setText("Saving grade threshold…")
        self._thread_pool.start(worker)

    def _write_finished(self, write_id, _result, error):
        self._workers.pop(("save", write_id), None)
        if write_id != self._write_id:
            return
        self._writing = False
        if error is not None:
            QMessageBox.warning(self, self._write_error_message, str(error))
        self.refresh()


class AssessmentComponentDialog(QDialog):
    def __init__(self, component=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit assessment component" if component else "Add assessment component")
        form = QFormLayout(self)
        self.name = QLineEdit(component["component_name"] if component else "")
        self.name.setMaxLength(80)
        self.order = QSpinBox()
        self.order.setRange(1, 99)
        self.order.setValue(int(component["component_order"]) if component else 1)
        self.max_score = QLineEdit(component["max_score"] if component else "")
        self.max_score.setMaxLength(32)
        self.max_score.setPlaceholderText("For example, 20 or 12.5")
        form.addRow("Component name", self.name)
        form.addRow("Order", self.order)
        form.addRow("Maximum points", self.max_score)
        form.addRow(QLabel(
            "This defines a maximum score only. Weighting, missing-score rules, and grade bands are not set here."
        ))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return self.name.text(), self.order.value(), self.max_score.text()


class AssessmentSetupPage(QWidget):
    def __init__(self, username: str, role: str):
        super().__init__()
        self.username = username
        self.is_admin = role == "admin"
        self._sessions = []
        self._assignments = []
        self._terms = []
        self._rows = []
        self._load_id = 0
        self._write_id = 0
        self._workers = {}
        self._write_workers = {}
        self._thread_pool = QThreadPool.globalInstance()
        self._loading = False
        self._writing = False
        layout = QVBoxLayout(self)
        title = QLabel("Assessment setup")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(wrapped_label(
            "Define ordered assessment components and maximum points for a class subject within a term. "
            "For a CA/exam workflow, add components named CA Score and Exam Score; choose the maxima used by your school."
        ))
        selectors = QFormLayout()
        self.session_filter = QComboBox()
        self.assignment_filter = QComboBox()
        self.term_filter = QComboBox()
        self.session_filter.currentIndexChanged.connect(self._session_changed)
        self.assignment_filter.currentIndexChanged.connect(self.refresh)
        self.term_filter.currentIndexChanged.connect(self.refresh)
        selectors.addRow("Academic session", self.session_filter)
        selectors.addRow("Class and subject", self.assignment_filter)
        selectors.addRow("Term", self.term_filter)
        layout.addLayout(selectors)
        controls = QHBoxLayout()
        controls.addStretch()
        self.add_button = QPushButton("Add component")
        self.edit_button = QPushButton("Edit selected")
        self.delete_button = QPushButton("Delete selected")
        for button in (self.add_button, self.edit_button, self.delete_button):
            button.setEnabled(self.is_admin)
            controls.addWidget(button)
        layout.addLayout(controls)
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Component ID", "Assessment Component", "Component Order", "Maximum Score"])
        configure_table_headers(self.table)
        self.table.setColumnHidden(0, True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)
        self.status = QLabel()
        layout.addWidget(self.status)
        self.add_button.clicked.connect(self.add_component)
        self.edit_button.clicked.connect(self.edit_component)
        self.delete_button.clicked.connect(self.delete_component)
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.refresh()

    def _selected(self):
        row = self.table.currentRow()
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def _session_changed(self, *_args):
        for combo in (self.assignment_filter, self.term_filter):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("Select", None)
            combo.blockSignals(False)
        self.refresh()

    def _update_actions(self):
        ready = self.is_admin and not self._loading and not self._writing
        has_scope = self.assignment_filter.currentData() is not None and self.term_filter.currentData() is not None
        selected = self._selected() is not None
        self.add_button.setEnabled(ready and has_scope)
        self.edit_button.setEnabled(ready and has_scope and selected)
        self.delete_button.setEnabled(ready and has_scope and selected)

    def refresh(self, *_args):
        self._loading = True
        self._update_actions()
        self._load_id += 1
        load_id = self._load_id
        session_id = self.session_filter.currentData()
        assignment_id = self.assignment_filter.currentData()
        term_id = self.term_filter.currentData()
        session_id = int(session_id) if session_id is not None else None
        assignment_id = int(assignment_id) if assignment_id is not None else None
        term_id = int(term_id) if term_id is not None else None
        worker = BackgroundTask(
            load_id,
            lambda: db.load_assessment_setup_data(session_id, assignment_id, term_id),
        )
        worker.signals.finished.connect(
            lambda request_id, data, error: self._data_loaded(
                request_id, data, error, session_id, assignment_id, term_id
            )
        )
        self._workers[load_id] = worker
        self.table.setEnabled(False)
        self.status.setText("Loading assessment setup…")
        self._thread_pool.start(worker)

    def _set_combo_items(self, combo, items, label, selected_id=None):
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(f"Select {label}", None)
        for item in items:
            combo.addItem(item["label"], int(item["id"]))
        index = combo.findData(selected_id) if selected_id is not None else -1
        combo.setCurrentIndex(index if index >= 0 else (1 if combo.count() > 1 else 0))
        combo.blockSignals(False)

    def _data_loaded(self, load_id, data, error, requested_session, requested_assignment, requested_term):
        self._workers.pop(load_id, None)
        if load_id != self._load_id:
            return
        self._loading = False
        self.table.setEnabled(True)
        if error:
            self.status.setText("Could not load assessment setup")
            self._update_actions()
            QMessageBox.warning(self, "Could not load assessment setup", str(error))
            return
        self._sessions = data["sessions"]
        self._assignments = data["assignments"]
        self._terms = data["terms"]
        sessions = [{"id": row["id"], "label": row["name"], **dict(row)} for row in self._sessions]
        selected_session = requested_session
        if selected_session is None:
            current = next((row for row in self._sessions if row["is_current"]), None)
            selected_session = int(current["id"]) if current else (
                int(self._sessions[0]["id"]) if self._sessions else None
            )
        self._set_combo_items(self.session_filter, sessions, "a session", selected_session)
        assignments = []
        for row in self._assignments:
            label = f"{row['class_name']} {row['section']} — {row['subject_code']} {row['subject_name']}".strip()
            assignments.append({"id": row["id"], "label": label})
        self._set_combo_items(self.assignment_filter, assignments, "a class subject", requested_assignment)
        terms = [
            {"id": row["id"], "label": f"{row['term_order']}. {row['term_name']}"}
            for row in self._terms
        ]
        self._set_combo_items(self.term_filter, terms, "a term", requested_term)
        actual_session = self.session_filter.currentData()
        actual_assignment = self.assignment_filter.currentData()
        actual_term = self.term_filter.currentData()
        actual = tuple(int(value) if value is not None else None for value in (
            actual_session, actual_assignment, actual_term
        ))
        requested = (requested_session, requested_assignment, requested_term)
        if actual != requested:
            self.refresh()
            return
        self._rows = data["components"]
        self.table.setRowCount(len(self._rows))
        for row_index, row in enumerate(self._rows):
            for column, value in enumerate((row["id"], row["component_name"], row["component_order"], row["max_score"])):
                self.table.setItem(row_index, column, QTableWidgetItem(str(value)))
        self.status.setText(f"{len(self._rows)} component(s)")
        self._update_actions()

    def add_component(self):
        dialog = AssessmentComponentDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._save(dialog.values(), None)

    def edit_component(self):
        component = self._selected()
        if component is None:
            return
        dialog = AssessmentComponentDialog(component, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._save(dialog.values(), int(component["id"]))

    def delete_component(self):
        component = self._selected()
        if component is None:
            return
        answer = QMessageBox.question(
            self, "Delete component", f"Delete assessment component ‘{component['component_name']}’?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._start_write(
                lambda: db.delete_assessment_component(int(component["id"]), self.username),
                "Could not delete component",
            )

    def _save(self, values, component_id):
        assignment_id = int(self.assignment_filter.currentData())
        term_id = int(self.term_filter.currentData())
        self._start_write(
            lambda: db.save_assessment_component(
                assignment_id, term_id, *values, actor=self.username, component_id=component_id
            ),
            "Could not save component",
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
        self._write_error_message = error_message
        self._update_actions()
        self.status.setText("Saving component…")
        self._thread_pool.start(worker)

    def _write_finished(self, write_id, _result, error):
        self._write_workers.pop(write_id, None)
        if write_id != self._write_id:
            return
        self._writing = False
        if error:
            QMessageBox.warning(self, self._write_error_message, str(error))
        self.refresh()
