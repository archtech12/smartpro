"""Administrator-only draft score entry using versioned assessment policy snapshots."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from html import escape

from PyQt6.QtCore import QMarginsF, QThreadPool, Qt, QUrl
from PyQt6.QtGui import QAction, QImage, QPageLayout, QPageSize, QPdfWriter, QPainter, QTextDocument
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QMenu,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .table_helpers import configure_table_headers
from .ui_helpers import wrapped_label
from . import database as db
from . import tabular
from .score_import import preview_score_import, write_score_template
from .workers import BackgroundTask


class GradeEntryPage(QWidget):
    def __init__(self, username: str, role: str):
        super().__init__()
        self.username = username
        self.is_admin = role == "admin"
        self.is_teacher = role == "teacher"
        self.can_open_grade_entry = self.is_admin or self.is_teacher
        self._sessions = []
        self._assignments = []
        self._terms = []
        self._components = []
        self._bands = []
        self._students = []
        self._visible_student_indexes = []
        self._snapshot_id = None
        self._approval = None
        self._correction_mode = False
        self._correction_reason = ""
        self._report_path = None
        self._report_kind = "report_card"
        self._load_id = 0
        self._write_id = 0
        self._workers = {}
        self._loading = False
        self._writing = False
        self._rendering = False
        self._thread_pool = QThreadPool.globalInstance()

        layout = QVBoxLayout(self)
        title = QLabel("Grade entry")
        title.setObjectName("heading")
        layout.addWidget(title)
        selectors = QHBoxLayout()
        self.session_filter = QComboBox()
        self.term_filter = QComboBox()
        self.class_filter = QComboBox()
        self.assignment_filter = QComboBox()
        self.session_filter.setAccessibleName("Academic session")
        self.term_filter.setAccessibleName("Academic term")
        self.class_filter.setAccessibleName("Class")
        self.assignment_filter.setAccessibleName("Subject")
        self.session_filter.currentIndexChanged.connect(self._session_changed)
        self.term_filter.currentIndexChanged.connect(self.refresh)
        self.class_filter.currentIndexChanged.connect(self._class_changed)
        self.assignment_filter.currentIndexChanged.connect(self.refresh)
        for label, combo, stretch in (
            ("Session", self.session_filter, 1), ("Term", self.term_filter, 1),
            ("Class", self.class_filter, 1), ("Subject", self.assignment_filter, 2),
        ):
            selectors.addWidget(QLabel(label))
            selectors.addWidget(combo, stretch)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        selectors.addWidget(self.refresh_button)
        layout.addLayout(selectors)

        self.student_search = QLineEdit()
        self.student_search.setClearButtonEnabled(True)
        self.student_search.setPlaceholderText("Search student name or admission number…")
        self.student_search.setAccessibleName("Search students in the selected class roster")
        self.student_search.textChanged.connect(self._filter_students)
        self.roster_count = QLabel("0 students")

        workflow_actions = QToolBar("Grade-entry actions")
        workflow_actions.setMovable(False)
        workflow_actions.setFloatable(False)
        workflow_actions.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.save_button = QPushButton("Save All Grades")
        self.save_button.setObjectName("primary")
        self.save_button.setToolTip("Save all entered scores for this class and subject.")
        self.template_button = QAction("Export Excel template", self)
        self.template_button.setToolTip("Export the roster and score columns as an Excel template for editing.")
        self.import_button = QPushButton("Import Scores")
        self.import_button.setToolTip("Load an Excel or CSV score sheet and review imported values before saving.")
        self.approve_button = QAction("Approve and lock results", self)
        self.correct_button = QAction("Correct approved results", self)
        self.more_button = QToolButton()
        self.more_button.setText("More ▾")
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_menu = QMenu(self.more_button)
        self.more_menu.addAction(self.template_button)
        self.more_menu.addSeparator()
        self.more_menu.addAction(self.approve_button)
        self.more_menu.addAction(self.correct_button)
        self.more_button.setMenu(self.more_menu)
        self.reports_button = QToolButton()
        self.reports_button.setText("Reports ▾")
        self.reports_button.setAccessibleName("Approved report and result exports")
        self.reports_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.reports_menu = QMenu(self.reports_button)
        self.report_button = self.reports_menu.addAction("Student report card PDF")
        self.class_cards_button = self.reports_menu.addAction("Class report cards PDF")
        self.result_sheet_button = QPushButton("Export Subject")
        self.master_sheet_button = QPushButton("Export Master Sheet")
        self.report_button.setToolTip("Export the selected student's approved report card as a PDF.")
        self.class_cards_button.setToolTip("Export one approved report-card page for each student in the class.")
        self.reports_button.setMenu(self.reports_menu)
        self.save_button.clicked.connect(self.save_scores)
        self.template_button.triggered.connect(self.export_score_template)
        self.import_button.clicked.connect(self.import_score_csv)
        self.approve_button.triggered.connect(self.approve_results)
        self.correct_button.triggered.connect(self.toggle_correction)
        self.report_button.triggered.connect(self.export_report_card)
        self.class_cards_button.triggered.connect(self.export_class_report_cards)
        self.result_sheet_button.clicked.connect(self.export_result_sheet)
        self.master_sheet_button.clicked.connect(self.export_master_sheet)
        for button in (
            self.import_button, self.result_sheet_button, self.master_sheet_button,
            self.save_button, self.more_button, self.reports_button,
        ):
            workflow_actions.addWidget(button)
        workflow = QHBoxLayout()
        workflow.addWidget(self.student_search, 1)
        workflow.addWidget(self.roster_count)
        workflow.addWidget(workflow_actions, 3)
        layout.addLayout(workflow)

        self.table = QTableWidget()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.EditKeyPressed
            | QTableWidget.EditTrigger.AnyKeyPressed
        )
        self.table.setToolTip("Select a score cell and type. Press Tab to move through score cells.")
        self.table.itemChanged.connect(self._score_changed)
        self.table.itemSelectionChanged.connect(self._set_actions)
        layout.addWidget(self.table, 1)
        self.status = QLabel("Loading grade-entry setup…")
        layout.addWidget(self.status)
        if self.can_open_grade_entry:
            self.refresh()
        else:
            self.class_filter.setEnabled(False)
            self.session_filter.setEnabled(False)
            self.assignment_filter.setEnabled(False)
            self.term_filter.setEnabled(False)
            self.save_button.setEnabled(False)
            self.refresh_button.setEnabled(False)
            self.status.setText(
                "Grade entry is available to administrators and linked teachers with assigned class-subjects."
            )

    def _session_changed(self, *_args):
        for combo in (self.class_filter, self.assignment_filter, self.term_filter):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("Select", None)
            combo.blockSignals(False)
        self.refresh()

    def _class_changed(self, *_args):
        class_id = self.class_filter.currentData()
        assignments = [
            row for row in self._assignments
            if class_id is not None and int(row["class_id"]) == int(class_id)
        ]
        selected_assignment = self.assignment_filter.currentData()
        if selected_assignment is not None and not any(
            int(row["id"]) == int(selected_assignment) for row in assignments
        ):
            selected_assignment = None
        self._set_items(
            self.assignment_filter,
            [{"id": row["id"], "label": f"{row['subject_code']} — {row['subject_name']}"} for row in assignments],
            "a subject", selected_assignment,
        )
        self.refresh()

    def refresh(self, *_args):
        if not self.can_open_grade_entry or self._writing:
            return
        self._load_id += 1
        load_id = self._load_id
        requested = tuple(
            int(value) if value is not None else None
            for value in (self.session_filter.currentData(), self.assignment_filter.currentData(), self.term_filter.currentData())
        )
        requested_class = (
            int(self.class_filter.currentData()) if self.class_filter.currentData() is not None else None
        )
        self._loading = True
        self._set_actions()
        self.status.setText("Loading assessment setup and enrolled students…")
        worker = BackgroundTask(load_id, lambda: self._load_grade_entry_scope(requested))
        worker.signals.finished.connect(
            lambda request_id, data, error: self._loaded(request_id, data, error, requested, requested_class)
        )
        self._workers[load_id] = worker
        self._thread_pool.start(worker)

    def _load_grade_entry_scope(self, requested):
        data = db.load_grade_entry_data(*requested, actor=self.username)
        if (
            self.is_admin and requested[0] is not None and requested[1] is not None
            and requested[2] is not None and not data["components"]
            and data["snapshot_id"] is None
        ):
            db.setup_ca_exam_components(
                int(requested[1]), int(requested[2]), "40", "60", self.username
            )
            data = db.load_grade_entry_data(*requested, actor=self.username)
        return data

    @staticmethod
    def _set_items(combo, items, label, selected_id):
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(f"Select {label}", None)
        for item in items:
            combo.addItem(item["label"], int(item["id"]))
        index = combo.findData(selected_id) if selected_id is not None else -1
        combo.setCurrentIndex(index if index >= 0 else (1 if combo.count() > 1 else 0))
        combo.blockSignals(False)

    def _loaded(self, load_id, data, error, requested, requested_class=None):
        self._workers.pop(load_id, None)
        if load_id != self._load_id:
            return
        self._loading = False
        if error is not None:
            self._clear_table()
            self.status.setText("Could not load grade-entry data")
            self._set_actions()
            QMessageBox.warning(self, "Could not load grade-entry data", str(error))
            return
        self._sessions = data["sessions"]
        self._assignments = data["assignments"]
        self._terms = data["terms"]
        session_id = requested[0]
        if session_id is None:
            current = next((row for row in self._sessions if row["is_current"]), None)
            session_id = int(current["id"]) if current else (int(self._sessions[0]["id"]) if self._sessions else None)
        sessions = [{"id": row["id"], "label": row["name"]} for row in self._sessions]
        class_items = []
        seen_classes = set()
        for row in self._assignments:
            class_id = int(row["class_id"])
            if class_id not in seen_classes:
                class_items.append({
                    "id": class_id,
                    "label": f"{row['class_name']} {row['section']}".strip(),
                })
                seen_classes.add(class_id)
        term_items = [{"id": row["id"], "label": f"{row['term_order']}. {row['term_name']}"} for row in self._terms]
        self._set_items(self.session_filter, sessions, "a session", session_id)
        selected_class = requested_class
        if selected_class is None and requested[1] is not None:
            selected = next((row for row in self._assignments if int(row["id"]) == requested[1]), None)
            selected_class = int(selected["class_id"]) if selected is not None else None
        self._set_items(self.class_filter, class_items, "a class", selected_class)
        class_id = self.class_filter.currentData()
        assignment_items = [
            {"id": row["id"], "label": f"{row['subject_code']} — {row['subject_name']}"}
            for row in self._assignments
            if class_id is not None and int(row["class_id"]) == int(class_id)
        ]
        selected_assignment = requested[1]
        if selected_assignment is not None and not any(
            int(item["id"]) == selected_assignment for item in assignment_items
        ):
            selected_assignment = None
        self._set_items(self.assignment_filter, assignment_items, "a subject", selected_assignment)
        self._set_items(self.term_filter, term_items, "a term", requested[2])
        actual = tuple(int(value) if value is not None else None for value in (
            self.session_filter.currentData(), self.assignment_filter.currentData(), self.term_filter.currentData()
        ))
        actual_class = int(self.class_filter.currentData()) if self.class_filter.currentData() is not None else None
        if actual != requested or actual_class != requested_class:
            self.refresh()
            return
        self._components = data["components"]
        self._bands = data["bands"]
        self._students = data["students"]
        self._snapshot_id = data["snapshot_id"]
        self._approval = data["approval"]
        self._correction_mode = False
        self._correction_reason = ""
        self._render_table()
        self._filter_students()
        if self.is_teacher and not data.get("teacher_linked"):
            self.status.setText(
                "This teacher login is not linked to an active teacher record. Ask an administrator to link it."
            )
        elif self.is_teacher and not self._assignments:
            self.status.setText(
                "No class subjects are assigned to your teacher account for this session. Ask an administrator to assign them."
            )
        elif not self.is_teacher and actual[0] and not self._assignments:
            self.status.setText(
                "No class-subject assignments exist for this session. In Settings, classify each class as Primary or Secondary, "
                "add subjects, then assign subjects to classes under Class Subjects."
            )
        elif not actual[0] or not actual[1] or not actual[2]:
            self.status.setText("Select a session, term, class, and subject.")
        elif not self._components:
            self.status.setText(
                "CA and exam columns are not set up yet. Ask an administrator to open this subject in Grade Entry."
            )
        elif not self._bands or Decimal(str(self._bands[0]["threshold"])) != 0:
            self.status.setText("Configure a grade scale beginning at 0 for this session and school stage first.")
        elif not self._students:
            self.status.setText("No students are enrolled in this class during the selected term.")
        elif self._approval:
            self.status.setText(
                f"Approved by {self._approval['approved_by']} on {self._approval['approved_at']}. "
                "Results are locked; a reason is required for any correction."
            )
        else:
            snapshot_note = "Policy snapshot is frozen." if self._snapshot_id else "Policy is preview only until the first score is saved."
            self.status.setText(f"{len(self._students)} student(s). Scores are drafts. {snapshot_note}")
        self._set_actions()

    def _clear_table(self):
        self._rendering = True
        self.table.clear()
        self.table.setRowCount(0)
        self.table.setColumnCount(0)
        self._rendering = False

    def _render_table(self):
        self._rendering = True
        headers = ["Admission Number", "Student Name"]
        headers.extend(
            f"{item['component_name']} (Max {item['max_score']})"
            for item in self._components
        )
        headers.extend(["Total", "Max Points", "Percent", "Grade", "Status"])
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        configure_table_headers(self.table)
        total_header = self.table.horizontalHeaderItem(2 + len(self._components))
        total_header.setToolTip("Sum of entered component scores. Updates as you type.")
        self.table.horizontalHeaderItem(3 + len(self._components)).setToolTip(
            "Maximum points available across the selected subject's score components."
        )
        self.table.horizontalHeaderItem(4 + len(self._components)).setToolTip(
            "Total points divided by maximum points, shown as a percentage."
        )
        self.table.horizontalHeaderItem(5 + len(self._components)).setToolTip(
            "Grade calculated from the school's grade scale for this session and school stage."
        )
        for offset, component in enumerate(self._components):
            self.table.horizontalHeaderItem(2 + offset).setToolTip(
                f"Enter a score from 0 to {component['max_score']} points."
            )
        self.table.setRowCount(len(self._students))
        self._visible_student_indexes = list(range(len(self._students)))
        first_score_col = 2
        result_col = first_score_col + len(self._components)
        for row_index, student in enumerate(self._students):
            self.table.setItem(row_index, 0, self._readonly(student["admission_no"]))
            self.table.setItem(row_index, 1, self._readonly(f"{student['last_name']}, {student['first_name']}"))
            scores = student.get("scores", {})
            for offset, component in enumerate(self._components):
                value = scores.get(int(component["id"]))
                score_item = QTableWidgetItem(value or "")
                if self._approval and not self._correction_mode:
                    score_item.setFlags(score_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row_index, first_score_col + offset, score_item)
            result = student.get("result") or self._calculate_preview(scores)
            self._show_result(row_index, result_col, result)
        self.table.resizeColumnsToContents()
        self._rendering = False

    @staticmethod
    def _readonly(value):
        item = QTableWidgetItem(str(value))
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    def _calculate_preview(self, scores):
        if not self._components or not self._bands:
            return None
        try:
            return db.calculate_assessment_result(self._components, scores, self._bands)
        except ValueError:
            return None

    def _show_result(self, row_index, result_col, result):
        if not result:
            values = ("", "", "", "", "Setup required")
        elif result["complete"]:
            values = (
                result["points_earned"], result["points_possible"],
                result["display_percentage"], result["grade_label"] or "No band", "Complete draft",
            )
        else:
            values = (
                result["points_earned"], result["points_possible"], "Incomplete", "—", "Incomplete draft",
            )
        for offset, value in enumerate(values):
            self.table.setItem(row_index, result_col + offset, self._readonly(value))

    def _filter_students(self, *_args):
        if not hasattr(self, "student_search"):
            return
        query = self.student_search.text().strip().casefold()
        visible = 0
        for row_index, student in enumerate(self._students):
            full_name = (
                f"{student['first_name']} {student['last_name']} "
                f"{student['last_name']}, {student['first_name']}"
            ).casefold()
            admission_no = str(student["admission_no"]).casefold()
            matches = not query or query in full_name or query in admission_no
            self.table.setRowHidden(row_index, not matches)
            visible += int(matches)
        self.roster_count.setText(
            f"{visible} student(s)" if not query or visible == len(self._students)
            else f"{visible} of {len(self._students)} student(s)"
        )

    def _score_changed(self, item):
        if self._rendering or item.column() < 2 or item.column() >= 2 + len(self._components):
            return
        component = self._components[item.column() - 2]
        value = item.text().strip()
        if value:
            try:
                db.validate_assessment_score(value, component["max_score"])
            except ValueError:
                item.setBackground(Qt.GlobalColor.red)
            else:
                item.setBackground(Qt.GlobalColor.white)
        else:
            item.setBackground(Qt.GlobalColor.white)
        scores = {}
        for offset, definition in enumerate(self._components):
            cell = self.table.item(item.row(), 2 + offset)
            if cell and cell.text().strip():
                try:
                    scores[int(definition["id"])] = db.validate_assessment_score(cell.text(), definition["max_score"])
                except ValueError:
                    pass
        self._show_result(item.row(), 2 + len(self._components), self._calculate_preview(scores))

    def _set_actions(self):
        ready = (
            self.can_open_grade_entry and not self._loading and not self._writing
            and self.session_filter.currentData() is not None
            and self.class_filter.currentData() is not None
            and self.assignment_filter.currentData() is not None
            and self.term_filter.currentData() is not None
            and bool(self._components) and bool(self._bands)
            and Decimal(str(self._bands[0]["threshold"])) == 0
            and bool(self._students)
            and (self._approval is None or self._correction_mode)
        )
        self.save_button.setEnabled(ready)
        can_export_template = (
            self.can_open_grade_entry and not self._loading and not self._writing
            and self.session_filter.currentData() is not None
            and self.class_filter.currentData() is not None
            and self.assignment_filter.currentData() is not None
            and self.term_filter.currentData() is not None
            and bool(self._components) and bool(self._students)
        )
        self.template_button.setEnabled(can_export_template)
        self.import_button.setEnabled(ready)
        complete = bool(self._students) and all(
            (student.get("result") or self._calculate_preview(student.get("scores", {})))
            and (student.get("result") or self._calculate_preview(student.get("scores", {})))["complete"]
            for student in self._students
        )
        self.approve_button.setEnabled(
            self.is_admin and not self._loading and not self._writing
            and self._snapshot_id is not None and self._approval is None and complete
        )
        self.correct_button.setEnabled(
            self.is_admin and not self._loading and not self._writing and self._approval is not None
        )
        self.correct_button.setText("Cancel correction" if self._correction_mode else "Correct approved results")
        self.report_button.setEnabled(
            self.is_admin and not self._loading and not self._writing
            and self._approval is not None and not self._correction_mode and self.table.currentRow() >= 0
        )
        can_export = self.is_admin and not self._loading and not self._writing and self._approval is not None and not self._correction_mode
        self.class_cards_button.setEnabled(can_export)
        self.result_sheet_button.setEnabled(can_export)
        self.master_sheet_button.setEnabled(can_export)
        self.reports_button.setEnabled(can_export or self.report_button.isEnabled())
        self.refresh_button.setEnabled(not self._loading and not self._writing)
        for combo in (self.session_filter, self.term_filter, self.class_filter, self.assignment_filter):
            combo.setEnabled(not self._writing and self.can_open_grade_entry)
        self.table.setEnabled(not self._loading and not self._writing and self.can_open_grade_entry)

    def export_score_template(self):
        if not self.template_button.isEnabled():
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export score template", "grade_scores_template.xlsx", "Excel workbook (*.xlsx);;CSV files (*.csv)"
        )
        if not path:
            return
        if not path.casefold().endswith((".csv", ".xlsx")):
            path += ".xlsx"
        students = []
        for row_index, student in enumerate(self._students):
            scores = {}
            for offset, component in enumerate(self._components):
                item = self.table.item(row_index, 2 + offset)
                scores[int(component["id"])] = item.text().strip() if item else ""
            students.append({**student, "scores": scores})
        try:
            write_score_template(path, students, self._components, self._selected_subject_code())
        except Exception as exc:
            QMessageBox.warning(self, "Could not export score template", str(exc))
            return
        self.status.setText(f"Score template saved: {path}")
        QMessageBox.information(
            self, "Score template ready",
            "Fill in the score columns and import the file. Admission number identifies each student. "
            "The student name columns are for reference only.",
        )

    def import_score_csv(self):
        if not self.import_button.isEnabled():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import draft scores", "", tabular.SPREADSHEET_FILTER
        )
        if not path:
            return
        try:
            preview = preview_score_import(
                path, self._students, self._components, self._selected_subject_code()
            )
        except Exception as exc:
            QMessageBox.warning(self, "Score file not imported", str(exc))
            self.status.setText("Score file was not imported; no table values were changed")
            return
        answer = QMessageBox.question(
            self, "Review score import",
            f"Validated {preview['matched_rows']} student row(s) and "
            f"{preview['score_count']} score value(s).\n\n"
            "Blank score cells leave current values unchanged. No database changes are made yet. "
            "After reviewing the table, click Save draft scores. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        row_by_enrollment = {
            int(student["enrollment_id"]): row_index
            for row_index, student in enumerate(self._students)
        }
        component_column = {
            int(component["id"]): 2 + offset
            for offset, component in enumerate(self._components)
        }
        changed_items = []
        self.table.blockSignals(True)
        try:
            for enrollment_id, scores in preview["updates"].items():
                row_index = row_by_enrollment[int(enrollment_id)]
                for component_id, score in scores.items():
                    item = self.table.item(row_index, component_column[int(component_id)])
                    if item is None:
                        item = QTableWidgetItem()
                        self.table.setItem(row_index, component_column[int(component_id)], item)
                    item.setText(score)
                    item.setBackground(Qt.GlobalColor.white)
                    changed_items.append(item)
        finally:
            self.table.blockSignals(False)
        for item in changed_items:
            self._score_changed(item)
        self.status.setText(
            f"Imported {preview['score_count']} score value(s) into the table. "
            "Review them and save drafts to commit."
        )

    def _selected_subject_code(self) -> str:
        assignment_id = self.assignment_filter.currentData()
        assignment = next(
            (row for row in self._assignments if int(row["id"]) == int(assignment_id)),
            None,
        ) if assignment_id is not None else None
        return str(assignment["subject_code"]) if assignment else "SUBJECT"

    def approve_results(self):
        if not self.approve_button.isEnabled():
            return
        answer = QMessageBox.question(
            self, "Approve results",
            "Approve and lock all complete student results for this class, subject, and term?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._writing = True
        self._set_actions()
        self.status.setText("Approving and locking result set…")
        self._write_id += 1
        write_id = self._write_id
        worker = BackgroundTask(write_id, lambda: db.approve_assessment_results(self._snapshot_id, self.username))
        worker.signals.finished.connect(self._approval_finished)
        self._workers[("approval", write_id)] = worker
        self._thread_pool.start(worker)

    def _approval_finished(self, write_id, _result, error):
        self._workers.pop(("approval", write_id), None)
        if write_id != self._write_id:
            return
        self._writing = False
        if error is not None:
            self._set_actions()
            self.status.setText("Results were not approved")
            QMessageBox.warning(self, "Could not approve results", str(error))
            return
        self.refresh()

    def toggle_correction(self):
        if not self._approval:
            return
        if self._correction_mode:
            self._correction_mode = False
            self._correction_reason = ""
            self.refresh()
            return
        reason, accepted = QInputDialog.getText(
            self, "Correct approved results", "Reason for this correction (required, max 300 characters):"
        )
        if not accepted:
            return
        reason = reason.strip()
        if not reason or len(reason) > 300:
            QMessageBox.warning(self, "Correction reason required", "Enter a reason of 1 to 300 characters.")
            return
        self._correction_reason = reason
        self._correction_mode = True
        for row in range(self.table.rowCount()):
            for column in range(2, 2 + len(self._components)):
                item = self.table.item(row, column)
                if item:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self.status.setText("Correction mode. Edit scores, then save to retain the reason and previous values.")
        self._set_actions()

    def export_report_card(self):
        row_index = self.table.currentRow()
        if not self._approval or not (0 <= row_index < len(self._students)):
            return
        path, _selected_filter = QFileDialog.getSaveFileName(
            self, "Save approved report card", "report-card.pdf", "PDF files (*.pdf)"
        )
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        self._report_kind = "report_card"
        student = self._students[row_index]
        assignment_id = int(self.assignment_filter.currentData())
        assignment = next((
            item for item in self._assignments if int(item["id"]) == assignment_id
        ), None)
        if assignment is None:
            return
        session_id = int(self.session_filter.currentData())
        term_id = int(self.term_filter.currentData())
        enrollment_id = int(student["enrollment_id"])
        self._report_path = path
        self._writing = True
        self._set_actions()
        self.status.setText("Checking approved results before preparing report card…")
        self._write_id += 1
        write_id = self._write_id
        worker = BackgroundTask(
            write_id,
            lambda: db.load_report_card_data(
                session_id, int(assignment["class_id"]), term_id, enrollment_id
            ),
        )
        worker.signals.finished.connect(self._report_data_loaded)
        self._workers[("report", write_id)] = worker
        self._thread_pool.start(worker)

    def export_class_report_cards(self):
        if not self._approval or self._correction_mode:
            return
        assignment_id = int(self.assignment_filter.currentData())
        assignment = next((item for item in self._assignments if int(item["id"]) == assignment_id), None)
        if assignment is None:
            return
        path, _selected_filter = QFileDialog.getSaveFileName(
            self, "Save class report cards", "class-report-cards.pdf", "PDF files (*.pdf)"
        )
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        self._report_path = path
        self._report_kind = "class_cards"
        self._writing = True
        self._set_actions()
        self.status.setText("Checking all approved class results before preparing student report cards…")
        self._write_id += 1
        write_id = self._write_id
        worker = BackgroundTask(
            write_id,
            lambda: db.load_class_report_cards_data(
                int(self.session_filter.currentData()), int(assignment["class_id"]),
                int(self.term_filter.currentData()),
            ),
        )
        worker.signals.finished.connect(self._report_data_loaded)
        self._workers[("report", write_id)] = worker
        self._thread_pool.start(worker)

    def export_result_sheet(self):
        self._export_class_sheet(master=False)

    def export_master_sheet(self):
        self._export_class_sheet(master=True)

    def _export_class_sheet(self, master: bool):
        if not self._approval or self._correction_mode:
            return
        assignment_id = int(self.assignment_filter.currentData())
        assignment = next((item for item in self._assignments if int(item["id"]) == assignment_id), None)
        if assignment is None:
            return
        label = "master sheet" if master else "class result sheet"
        path, _selected_filter = QFileDialog.getSaveFileName(
            self, f"Save approved {label}", f"{label.replace(' ', '-')}.pdf", "PDF files (*.pdf)"
        )
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        session_id = int(self.session_filter.currentData())
        class_id = int(assignment["class_id"])
        term_id = int(self.term_filter.currentData())
        assignment_id = int(assignment["id"])
        self._report_path = path
        self._report_kind = "master_sheet" if master else "result_sheet"
        self._writing = True
        self._set_actions()
        self.status.setText(f"Checking approved results before preparing {label}…")
        self._write_id += 1
        write_id = self._write_id
        loader = (lambda: db.load_master_sheet_data(session_id, class_id, term_id)) if master else (
            lambda: db.load_result_sheet_data(session_id, class_id, term_id, assignment_id)
        )
        worker = BackgroundTask(write_id, loader)
        worker.signals.finished.connect(self._report_data_loaded)
        self._workers[("report", write_id)] = worker
        self._thread_pool.start(worker)

    def _report_data_loaded(self, write_id, data, error):
        self._workers.pop(("report", write_id), None)
        if write_id != self._write_id:
            return
        self._writing = False
        if error is not None:
            self._set_actions()
            self.status.setText("Approved results PDF was not exported")
            QMessageBox.warning(self, "Could not export results PDF", str(error))
            return
        try:
            renderers = {
                "report_card": self._render_report_card_pdf,
                "class_cards": self._render_class_report_cards_pdf,
                "result_sheet": self._render_result_sheet_pdf,
                "master_sheet": self._render_master_sheet_pdf,
            }
            renderers[self._report_kind](data, self._report_path)
        except Exception as exc:
            self._set_actions()
            self.status.setText("Approved results PDF was not exported")
            QMessageBox.warning(self, "Could not create results PDF", str(exc))
            return
        self._set_actions()
        labels = {"report_card": "report card", "class_cards": "class report cards", "result_sheet": "class result sheet", "master_sheet": "master sheet"}
        label = labels[self._report_kind]
        self.status.setText(f"Approved {label} saved: {self._report_path}")
        QMessageBox.information(self, "Results PDF ready", f"Saved approved {label} to:\n{self._report_path}")

    @staticmethod
    def _write_pdf(html, path, logo_data=None, student_photo_data=None):
        writer = QPdfWriter(path)
        writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        writer.setPageMargins(QMarginsF(14, 14, 14, 14), QPageLayout.Unit.Millimeter)
        document = QTextDocument()
        if logo_data:
            image = QImage.fromData(logo_data)
            if image.isNull():
                raise ValueError("The saved school logo is not a readable image.")
            image = image.scaled(192, 128, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            document.addResource(QTextDocument.ResourceType.ImageResource, QUrl("school-logo"), image)
        if student_photo_data:
            image = QImage.fromData(student_photo_data)
            if image.isNull():
                raise ValueError("The saved student photo is not a readable image.")
            image = image.scaled(96, 96, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            document.addResource(QTextDocument.ResourceType.ImageResource, QUrl("student-photo"), image)
        document.setHtml(html)
        document.print(writer)

    @classmethod
    def _render_result_sheet_pdf(cls, data, path):
        subject = data["subject"]
        rows = "".join(
            "<tr>" + f"<td>{escape(s['admission_no'])}</td><td>{escape(s['last_name'])}, {escape(s['first_name'])}</td>"
            f"<td>{escape(s['result']['points_earned'])}/{escape(s['result']['points_possible'])}</td>"
            f"<td>{escape(s['result']['display_percentage'])}%</td><td>{escape(s['result']['grade_label'] or '')}</td>"
            f"<td>{escape(s['result']['remark'] or '')}</td></tr>" for s in data["students"]
        )
        html = cls._sheet_html(
            "Class Result Sheet", data,
            f"Subject: {escape(subject['subject_code'])} — {escape(subject['subject_name'])}",
            "<th>Admission No.</th><th>Student</th><th>Points</th><th>Percentage</th><th>Grade</th><th>Remark</th>", rows,
        )
        cls._write_pdf(html, path, data.get("school_logo"))

    @classmethod
    def _render_master_sheet_pdf(cls, data, path):
        rows = []
        for student in data["students"]:
            for subject in data["subjects"]:
                result = student["results"][int(subject["id"])]
                rows.append(
                    "<tr>" + f"<td>{escape(student['admission_no'])}</td><td>{escape(student['last_name'])}, {escape(student['first_name'])}</td>"
                    f"<td>{escape(subject['subject_code'])} — {escape(subject['subject_name'])}</td>"
                    f"<td>{escape(result['points_earned'])}/{escape(result['points_possible'])}</td>"
                    f"<td>{escape(result['display_percentage'])}%</td><td>{escape(result['grade_label'] or '')}</td>"
                    f"<td>{escape(result['remark'] or '')}</td></tr>"
                )
            overall = student.get("overall")
            if overall and overall["complete"]:
                rows.append(
                    "<tr>" + f"<td>{escape(student['admission_no'])}</td><td>{escape(student['last_name'])}, {escape(student['first_name'])}</td>"
                    f"<td><b>Overall average</b> ({overall['subject_count']} subjects)</td><td></td>"
                    f"<td><b>{escape(overall['display_average'])}%</b></td><td><b>{escape(overall['grade_label'] or '')}</b></td>"
                    f"<td>{GradeEntryPage._position_html(student.get('position'))}</td></tr>"
                )
        html = cls._sheet_html(
            "Master Result Sheet", data, "All assigned subjects",
            "<th>Admission No.</th><th>Student</th><th>Subject</th><th>Points</th><th>Percentage</th><th>Grade</th><th>Remark</th>",
            "".join(rows),
        )
        cls._write_pdf(html, path, data.get("school_logo"))

    @staticmethod
    def _sheet_html(title, data, subtitle, headings, rows):
        profile = data.get("school_profile", {})
        logo_data = data.get("school_logo")
        school = GradeEntryPage._school_identity_html(profile, bool(logo_data))
        signatures = GradeEntryPage._signature_html(profile)
        return f"""<!doctype html><html><head><meta charset='utf-8'><style>
          body {{ font-family: sans-serif; color: #183247; }} h1 {{ color: #102a43; margin-bottom: 4px; }}
          .meta {{ margin: 4px 0 14px; }} table {{ border-collapse: collapse; width: 100%; }}
          th {{ background: #e8f0f6; text-align: left; }} th,td {{ border: 1px solid #9fb3c8; padding: 5px; }}
          .school {{ text-align:center; margin-bottom:14px; }} .school h2 {{ margin:0; color:#102a43; }}
          .signatures {{ margin-top:36px; }} .signatures td {{ width:50%; border:0; padding:8px 22px; }}
          .signline {{ border-top:1px solid #486581; padding-top:6px; margin-top:30px; }}
        </style></head><body>{school}<h1>{title}</h1><div class='meta'>
          Class: {escape(data['class_name'])} {escape(data['section'])}<br>
          Academic session: {escape(data['academic_session'])} &nbsp; Term: {data['term_order']}. {escape(data['term_name'])}<br>
          {subtitle}</div><table><thead><tr>{headings}</tr></thead><tbody>{rows}</tbody></table>
          <p>Approved results. Grades use the school's saved grading scale. Class position ranks the unrounded overall average; equal averages share a position.</p>{signatures}</body></html>"""

    @staticmethod
    def _position_html(position):
        if not position:
            return ""
        tie = " (tied)" if position["tied"] else ""
        return f"<b>Position {escape(position['position_text'])} of {position['of']}{tie}</b>"

    @staticmethod
    def _school_identity_html(profile, logo_present=False):
        if not logo_present and not any(profile.get(key, "") for key in ("school_name", "address", "phone", "email")):
            return ""
        lines = [escape(profile.get(key, "")) for key in ("address", "phone", "email") if profile.get(key, "")]
        contact = "<br>".join(lines)
        name = escape(profile.get("school_name", ""))
        logo = "<img src='school-logo' width='96'><br>" if logo_present else ""
        return f"<div class='school'>{logo}{f'<h2>{name}</h2>' if name else ''}{contact}</div>"

    @staticmethod
    def _signature_html(profile):
        head = " ".join(part for part in (profile.get("head_name", ""), profile.get("head_title", "")) if part)
        teacher = " ".join(part for part in (profile.get("class_teacher_name", ""), profile.get("class_teacher_title", "")) if part)
        if not head and not teacher:
            return ""
        columns = []
        if head:
            columns.append(f"<td><div class='signline'>{escape(head)}</div></td>")
        if teacher:
            columns.append(f"<td><div class='signline'>{escape(teacher)}</div></td>")
        return "<table class='signatures'><tr>" + "".join(columns) + "</tr></table>"

    # Layout steps tried in order until one student fits on one A4 page:
    # (font size in pt, cell padding in px, show the remark column).
    REPORT_CARD_FIT_STEPS = (
        (9, 4, True), (8, 3, True), (7, 2, True), (7, 2, False), (6, 2, False), (6, 1, False),
    )

    @staticmethod
    def _card_signature_html(profile, settings, pad):
        cells = []
        for key, name_key, title_key, label, source in (
            ("class_teacher_signature", "class_teacher_name", "class_teacher_title", "Class teacher", "teacher-signature"),
            ("head_signature", "head_name", "head_title", "Head of school", "head-signature"),
        ):
            image = settings.get(key)
            text = " ".join(part for part in (profile.get(name_key, ""), profile.get(title_key, "")) if part)
            if not image and not text:
                continue
            picture = f"<img src='{source}'><br>" if image else "<br><br>"
            cells.append(f"<td>{picture}<div class='signline'>{escape(text or label)}</div></td>")
        if not cells:
            return ""
        return f"<table class='signatures'><tr>{''.join(cells)}</tr></table>"

    @staticmethod
    def _term_dates_html(settings):
        parts = []
        for key, label in (("term_closing_date", "Term closes"), ("next_term_begins", "Next term begins")):
            value = (settings or {}).get(key) or ""
            try:
                parts.append(f"{label}: {date.fromisoformat(value).strftime('%d %B %Y').lstrip('0')}")
            except ValueError:
                continue
        return "<br>" + " &nbsp;·&nbsp; ".join(parts) if parts else ""

    @staticmethod
    def _report_card_html(data, font_size=9, pad=4, show_remark=True):
        student = data["student"]
        profile = data.get("school_profile", {})
        settings = data.get("report_settings", {})
        logo_data = data.get("school_logo")
        photo_data = student.get("photo_data")
        rows = "".join(
            "<tr>"
            f"<td>{escape(item['subject_code'])} — {escape(item['subject_name'])}</td>"
            f"<td>{escape(item['points_earned'])} / {escape(item['points_possible'])}</td>"
            f"<td>{escape(item['display_percentage'])}%</td>"
            f"<td><b>{escape(item['grade_label'] or '')}</b></td>"
            + (f"<td>{escape(item['remark'] or '')}</td>" if show_remark else "")
            + "</tr>"
            for item in data["subjects"]
        )
        name = escape(f"{student['last_name']}, {student['first_name']}")
        stage = f" · {escape(data['educational_stage'])}" if data.get("educational_stage") else ""
        overall = data.get("overall")
        position = data.get("position")
        style = settings.get("ranking_style", "position")
        summary = ""
        if overall and overall["complete"]:
            parts = [f"<b>Overall average:</b> {escape(overall['display_average'])}% across {overall['subject_count']} subjects"]
            if style == "average_grade":
                if overall.get("grade_label"):
                    parts.append(f"<b>Average grade:</b> {escape(overall['grade_label'])} ({escape(overall['remark'] or '')})")
            elif position:
                tie = " (tied)" if position["tied"] else ""
                parts.append(f"<b>Class position:</b> {escape(position['position_text'])} of {position['of']}{tie}")
            summary = "<p class='summary'>" + " &nbsp;·&nbsp; ".join(parts) + "</p>"
        key = data.get("grade_key") or []
        key_html = (
            "<p class='key'><b>Grading key:</b> "
            + " &nbsp; ".join(
                f"{escape(item['grade_label'])} {escape(item['from_text'])}+"
                + (f" ({escape(item['remark'])})" if item["remark"] else "")
                for item in key
            ) + "</p>" if key else ""
        )
        shows_position = style != "average_grade" and bool(position) and bool(overall and overall["complete"])
        position_note = " Class position ranks the unrounded overall average; equal averages share a position." if shows_position else ""
        remark_header = "<th>Remark</th>" if show_remark else ""
        term_dates = GradeEntryPage._term_dates_html(settings)
        photo = "<td width='84' align='right'><img src='student-photo'></td>" if photo_data else ""
        html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
          body {{ font-family: sans-serif; color: #183247; font-size:{font_size}pt; }}
          h1 {{ color: #102a43; font-size:13pt; margin:2px 0; }}
          table {{ border-collapse: collapse; width: 100%; }}
          table.info td {{ border:0; padding:1px; }}
          table.grades th {{ background: #e8f0f6; text-align: left; }}
          table.grades th, table.grades td {{ border: 1px solid #9fb3c8; padding: {pad}px; }}
          .summary {{ margin:4px 0 2px; font-size:{font_size + 1}pt; }} .key {{ margin:2px 0; font-size:{max(font_size - 1, 5)}pt; color:#486581; }}
          .approval {{ margin-top: 3px; color: #486581; font-size:{max(font_size - 1, 5)}pt; }}
          .school {{ text-align:center; margin-bottom:4px; }} .school h2 {{ margin:0; color:#102a43; }}
          .signatures {{ width:100%; margin-top:4px; }} .signatures td {{ width:50%; border:0; padding:2px 18px; }}
          .signline {{ border-top:1px solid #486581; padding-top:2px; margin-top:2px; }}
        </style></head><body>
          {GradeEntryPage._school_identity_html(profile, bool(logo_data))}
          <h1>Student Report Card</h1>
          <table class="info"><tr><td><b>{name}</b> &nbsp; Admission No.: {escape(student['admission_no'])}<br>
          Class: {escape(data['class_name'])} {escape(data['section'])}{stage}<br>
          Academic session: {escape(data['academic_session'])} &nbsp; Term: {data['term_order']}. {escape(data['term_name'])}{term_dates}</td>{photo}</tr></table>
          <table class="grades"><thead><tr><th>Subject</th><th>Points</th><th>Percentage</th><th>Grade</th>{remark_header}</tr></thead>
          <tbody>{rows}</tbody></table>
          {summary}{key_html}
          <div class="approval">Approved result card. Grades use the school's saved grading scale.{position_note}</div>
          {GradeEntryPage._card_signature_html(profile, settings, pad)}
        </body></html>"""
        return html

    @classmethod
    def _render_report_card_pdf(cls, data, path):
        cls._render_class_report_cards_pdf({"report_cards": [data]}, path)

    @classmethod
    def _render_class_report_cards_pdf(cls, data, path):
        """Write one A4 page per student; each page shrinks its layout until the whole card fits."""
        cards = data.get("report_cards", [])
        if not cards:
            raise ValueError("The class has no approved student report cards to export.")
        writer = QPdfWriter(path)
        writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        writer.setPageMargins(QMarginsF(10, 10, 10, 10), QPageLayout.Unit.Millimeter)
        page_size = writer.pageLayout().paintRect(QPageLayout.Unit.Point).size()
        painter = QPainter(writer)
        pdf_scale = writer.resolution() / 72.0
        painter.scale(pdf_scale, pdf_scale)
        for index, card in enumerate(cards):
            if index:
                writer.newPage()
            document = QTextDocument()
            document.setPageSize(page_size)
            settings = card.get("report_settings", {})
            sources = {
                "school-logo": (card.get("school_logo"), 192, 128),
                f"student-photo-{index}": (card["student"].get("photo_data"), 80, 80),
                f"teacher-signature-{index}": (settings.get("class_teacher_signature"), 150, 44),
                f"head-signature-{index}": (settings.get("head_signature"), 150, 44),
            }
            for key, (image_data, width, height) in sources.items():
                if image_data:
                    image = QImage.fromData(image_data)
                    if image.isNull():
                        raise ValueError("A saved school logo, signature, or student photo is not a readable image.")
                    image = image.scaled(width, height, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                    document.addResource(QTextDocument.ResourceType.ImageResource, QUrl(key), image)
            fitted = False
            for font_size, pad, show_remark in cls.REPORT_CARD_FIT_STEPS:
                page_html = (
                    cls._report_card_html(card, font_size, pad, show_remark)
                    .replace("src='student-photo'", f"src='student-photo-{index}'")
                    .replace("src='teacher-signature'", f"src='teacher-signature-{index}'")
                    .replace("src='head-signature'", f"src='head-signature-{index}'")
                )
                document.setHtml(page_html)
                if document.documentLayout().documentSize().height() <= page_size.height():
                    fitted = True
                    break
            if not fitted:
                painter.end()
                raise ValueError(
                    f"{card['student']['first_name']} {card['student']['last_name']}'s report card has too many subjects to fit on one page."
                )
            document.drawContents(painter)
        painter.end()

    def save_scores(self):
        if not self.save_button.isEnabled():
            return
        student_scores = {}
        for row_index, student in enumerate(self._students):
            scores = {}
            for offset, component in enumerate(self._components):
                item = self.table.item(row_index, 2 + offset)
                value = item.text().strip() if item else ""
                if value:
                    try:
                        scores[int(component["id"])] = db.validate_assessment_score(value, component["max_score"])
                    except ValueError as exc:
                        QMessageBox.warning(self, "Invalid score", f"{component['component_name']}: {exc}")
                        self.table.setCurrentCell(row_index, 2 + offset)
                        return
                else:
                    scores[int(component["id"])] = None
            if any(value is not None for value in scores.values()) or student.get("scores"):
                student_scores[int(student["enrollment_id"])] = scores
        if not student_scores:
            QMessageBox.information(self, "No scores entered", "Enter at least one score before saving drafts.")
            return
        session_id = int(self.session_filter.currentData())
        assignment_id = int(self.assignment_filter.currentData())
        term_id = int(self.term_filter.currentData())
        self._writing = True
        self._set_actions()
        self.status.setText("Saving draft scores and freezing the policy snapshot…")
        self._write_id += 1
        write_id = self._write_id
        worker = BackgroundTask(
            write_id,
            lambda: db.save_assessment_scores(
                assignment_id, term_id, student_scores, self.username, self._correction_reason
            ),
        )
        worker.signals.finished.connect(self._write_finished)
        self._workers[("write", write_id)] = worker
        self._thread_pool.start(worker)

    def _write_finished(self, write_id, result, error):
        self._workers.pop(("write", write_id), None)
        if write_id != self._write_id:
            return
        self._writing = False
        if error is not None:
            self._set_actions()
            self.status.setText("Draft scores were not saved")
            QMessageBox.warning(self, "Could not save draft scores", str(error))
            return
        self.status.setText(f"Saved draft score fields for {result[1]} component(s). Reloading…")
        self.refresh()
