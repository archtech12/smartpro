"""CBT Center: question bank, exams, the browser server, live monitoring and results (administrators)."""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)

from . import cbt, tabular
from . import database as db
from .cbt_server import CBTServer
from .communication_ui import _fill, _Tab, _table
from .ui_helpers import wrapped_label

_SERVER: CBTServer | None = None  # one server per running app; it is stopped from the Server tab or when the app exits


def _short(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _mmss(seconds) -> str:
    if seconds is None:
        return ""
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _combo(items, include_blank=None) -> QComboBox:
    box = QComboBox()
    if include_blank:
        box.addItem(include_blank, None)
    for value, label in items:
        box.addItem(label, value)
    return box


def _select(box: QComboBox, value) -> None:
    index = box.findData(value)
    if index >= 0:
        box.setCurrentIndex(index)


# ------------------------------------------------------------------ dialogs
class QuestionDialog(QDialog):
    def __init__(self, options: dict, question: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit question" if question else "New question")
        self.setMinimumWidth(560)
        self.question = question
        form = QFormLayout(self)
        self.qtype = _combo(list(cbt.QTYPES.items()))
        self.subject = _combo(options["subjects"], "Any subject")
        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("Type the question here")
        self.prompt.setFixedHeight(90)
        self.marks = QSpinBox()
        self.marks.setRange(1, 100)
        form.addRow("Question type", self.qtype)
        form.addRow("Subject", self.subject)
        form.addRow("Question", self.prompt)

        self.mcq_box = QWidget()
        mcq = QFormLayout(self.mcq_box)
        mcq.setContentsMargins(0, 0, 0, 0)
        self.option_edits = []
        for letter in cbt.OPTION_LETTERS:
            edit = QLineEdit()
            edit.setMaxLength(300)
            edit.setPlaceholderText("Optional" if letter in "CDE" else "Required")
            self.option_edits.append(edit)
            mcq.addRow(f"Option {letter}", edit)
        self.correct = QComboBox()
        for letter in cbt.OPTION_LETTERS:
            self.correct.addItem(letter, letter)
        mcq.addRow("Correct option", self.correct)
        form.addRow(self.mcq_box)

        self.tf_box = QWidget()
        tf = QFormLayout(self.tf_box)
        tf.setContentsMargins(0, 0, 0, 0)
        self.truth = _combo([("true", "True"), ("false", "False")])
        tf.addRow("The statement is", self.truth)
        form.addRow(self.tf_box)

        self.short_box = QWidget()
        short = QFormLayout(self.short_box)
        short.setContentsMargins(0, 0, 0, 0)
        self.accepted = QPlainTextEdit()
        self.accepted.setPlaceholderText("One accepted answer per line. Capital letters, spaces and a final full stop are ignored.")
        self.accepted.setFixedHeight(70)
        short.addRow("Accepted answers", self.accepted)
        form.addRow(self.short_box)

        form.addRow("Marks", self.marks)
        self.explanation = QLineEdit()
        self.explanation.setMaxLength(1000)
        self.explanation.setPlaceholderText("Optional note for staff")
        form.addRow("Explanation", self.explanation)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.qtype.currentIndexChanged.connect(self._switch)
        if question:
            _select(self.qtype, question["qtype"])
            _select(self.subject, question["subject_id"])
            self.prompt.setPlainText(question["prompt"])
            self.marks.setValue(question["marks"])
            self.explanation.setText(question["explanation"])
            for edit, text in zip(self.option_edits, question["options"]):
                edit.setText(text)
            if question["qtype"] == "mcq":
                _select(self.correct, question["answer"])
            elif question["qtype"] == "true_false":
                _select(self.truth, question["answer"])
            else:
                self.accepted.setPlainText("\n".join(question["accepted"]))
        self._switch()

    def _switch(self):
        kind = self.qtype.currentData()
        self.mcq_box.setVisible(kind == "mcq")
        self.tf_box.setVisible(kind == "true_false")
        self.short_box.setVisible(kind == "short_answer")

    def values(self) -> dict:
        kind = self.qtype.currentData()
        if kind == "mcq":
            options = [e.text() for e in self.option_edits]
            # keep the chosen letter pointing at the same option when blanks are skipped
            letter = self.correct.currentData()
            chosen = options["ABCDE".index(letter)].strip()
            kept = [o.strip() for o in options if o.strip()]
            answer = "ABCDE"[kept.index(chosen)] if chosen in kept else ""
            options, answer_value = kept, answer
        elif kind == "true_false":
            options, answer_value = [], self.truth.currentData()
        else:
            options, answer_value = [], [line for line in self.accepted.toPlainText().splitlines() if line.strip()]
        return {"qtype": kind, "prompt": self.prompt.toPlainText(), "options": options, "answer": answer_value,
                "marks": self.marks.value(), "explanation": self.explanation.text(), "subject_id": self.subject.currentData()}

    def _save(self):
        v = self.values()
        try:
            cbt.clean_question(v["qtype"], v["prompt"], v["options"], v["answer"], v["marks"], v["explanation"])
        except ValueError as exc:
            QMessageBox.warning(self, "Question", str(exc))
            return
        self.accept()


class ExamDialog(QDialog):
    def __init__(self, options: dict, exam: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit exam" if exam else "New exam")
        self.setMinimumWidth(480)
        form = QFormLayout(self)
        self.title = QLineEdit()
        self.title.setMaxLength(120)
        self.title.setPlaceholderText("For example: Basic Science – First Term Test")
        self.class_box = _combo(options["classes"])
        self.session_box = _combo([(i, n + (" (current)" if cur else "")) for i, n, cur in options["sessions"]])
        for i, _name, cur in options["sessions"]:
            if cur:
                _select(self.session_box, i)
        self.subject = _combo(options["subjects"], "Not tied to a subject")
        self.duration = QSpinBox()
        self.duration.setRange(1, 300)
        self.duration.setValue(30)
        self.duration.setSuffix(" minutes")
        self.shuffle_q = QCheckBox("Shuffle question order for each student")
        self.shuffle_o = QCheckBox("Shuffle answer options for each student")
        self.show_score = QCheckBox("Show students their score after they submit")
        self.shuffle_q.setChecked(True)
        self.shuffle_o.setChecked(True)
        self.instructions = QPlainTextEdit()
        self.instructions.setPlaceholderText("Optional instructions shown before the exam starts")
        self.instructions.setFixedHeight(80)
        for label, widget in (("Title", self.title), ("Class", self.class_box), ("Session", self.session_box),
                              ("Subject", self.subject), ("Duration", self.duration), ("", self.shuffle_q),
                              ("", self.shuffle_o), ("", self.show_score), ("Instructions", self.instructions)):
            form.addRow(label, widget)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        if exam:
            self.title.setText(exam["title"])
            _select(self.class_box, exam["class_id"])
            _select(self.session_box, exam["academic_session_id"])
            _select(self.subject, exam["subject_id"])
            self.duration.setValue(exam["duration_minutes"])
            self.shuffle_q.setChecked(bool(exam["shuffle_questions"]))
            self.shuffle_o.setChecked(bool(exam["shuffle_options"]))
            self.show_score.setChecked(bool(exam["show_score"]))
            self.instructions.setPlainText(exam["instructions"])

    def values(self) -> dict:
        return {"title": self.title.text(), "class_id": self.class_box.currentData(),
                "academic_session_id": self.session_box.currentData(), "duration_minutes": self.duration.value(),
                "subject_id": self.subject.currentData(), "shuffle_questions": self.shuffle_q.isChecked(),
                "shuffle_options": self.shuffle_o.isChecked(), "show_score": self.show_score.isChecked(),
                "instructions": self.instructions.toPlainText()}


class QuestionPickerDialog(QDialog):
    def __init__(self, questions: list[dict], selected: list[int], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose exam questions")
        self.setMinimumSize(640, 480)
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search questions…")
        layout.addWidget(self.search)
        self.list = QListWidget()
        chosen = set(selected)
        # chosen questions first (in exam order), then the rest
        order = {q: i for i, q in enumerate(selected)}
        for q in sorted(questions, key=lambda q: (order.get(q["id"], len(order)), -q["id"])):
            item = QListWidgetItem(f"{_short(q['prompt'], 110)}   [{cbt.QTYPES[q['qtype']]} · {q['marks']} mark{'s' if q['marks'] != 1 else ''}]")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if q["id"] in chosen else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, q["id"])
            item.setData(Qt.ItemDataRole.UserRole + 1, q["prompt"].lower())
            self.list.addItem(item)
        layout.addWidget(self.list, 1)
        self.count = QLabel("")
        layout.addWidget(self.count)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.search.textChanged.connect(self._filter)
        self.list.itemChanged.connect(lambda _item: self._count())
        self._count()

    def _filter(self, text):
        needle = text.strip().lower()
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setHidden(bool(needle) and needle not in item.data(Qt.ItemDataRole.UserRole + 1))

    def _count(self):
        self.count.setText(f"{len(self.selected_ids())} selected")

    def selected_ids(self) -> list[int]:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]


# --------------------------------------------------------------------- tabs
class _CBTTab(_Tab):
    """Background loading plus a guarded way to run quick writes."""

    def guard(self, action, success=None) -> bool:
        try:
            result = action()
        except (ValueError, PermissionError, OSError) as exc:
            QMessageBox.warning(self, "CBT Center", str(exc))
            return False
        except Exception as exc:  # a failure must never close the app
            QMessageBox.warning(self, "CBT Center", f"Something went wrong: {exc}")
            return False
        if success:
            success(result)
        return True

    def selected_id(self, table):
        row = table.currentRow()
        item = table.item(row, 0) if row >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item else None


class QuestionsTab(_CBTTab):
    HEADERS = ["Question", "Type", "Marks", "Subject", "Correct answer", "Used in exams"]

    def __init__(self, username):
        super().__init__(username)
        self._questions: dict[int, dict] = {}
        layout = QVBoxLayout(self)
        layout.addWidget(wrapped_label("Build a bank of questions once and reuse them across exams. Multiple-choice, true/false and "
                                       "short-answer questions are marked automatically."))
        filters = QHBoxLayout()
        self.kind = _combo(list(cbt.QTYPES.items()), "Every type")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search questions…")
        self.archived = QCheckBox("Show archived")
        for widget in (self.kind, self.search, self.archived):
            filters.addWidget(widget, 1 if widget is self.search else 0)
        layout.addLayout(filters)
        self.table = _table(self.HEADERS)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.new_button = QPushButton("New question")
        self.new_button.setObjectName("primary")
        self.edit_button = QPushButton("Edit")
        self.archive_button = QPushButton("Archive / restore")
        self.import_button = QPushButton("Import Excel/CSV…")
        self.template_button = QPushButton("Excel template")
        for b in (self.new_button, self.edit_button, self.archive_button, self.import_button, self.template_button):
            actions.addWidget(b)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.new_button.clicked.connect(self.new_question)
        self.edit_button.clicked.connect(self.edit_question)
        self.archive_button.clicked.connect(self.toggle_archive)
        self.import_button.clicked.connect(self.import_questions)
        self.template_button.clicked.connect(self.save_template)
        self.table.doubleClicked.connect(lambda _i: self.edit_question())
        self.kind.currentIndexChanged.connect(lambda _i: self.refresh())
        self.archived.stateChanged.connect(lambda _s: self.refresh())
        self.search.returnPressed.connect(self.refresh)

    def refresh(self):
        kind, search, archived = self.kind.currentData(), self.search.text(), self.archived.isChecked()
        self.run(lambda: cbt.list_questions(self.username, qtype=kind, search=search, include_archived=archived), self._show)

    def _show(self, questions):
        self._questions = {q["id"]: q for q in questions}
        rows = [[_short(q["prompt"]) + ("  (archived)" if q["is_archived"] else ""), cbt.QTYPES[q["qtype"]], q["marks"],
                 q["subject_name"] or "", q["answer_text"], q["used_in"]] for q in questions]
        _fill(self.table, rows, [q["id"] for q in questions])

    def _options(self):
        return cbt.form_options(self.username)

    def new_question(self):
        try:
            options = self._options()
        except Exception as exc:
            QMessageBox.warning(self, "CBT Center", str(exc))
            return
        dialog = QuestionDialog(options, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            v = dialog.values()
            if self.guard(lambda: cbt.create_question(self.username, v["qtype"], v["prompt"], v["options"], v["answer"],
                                                      v["marks"], v["explanation"], v["subject_id"])):
                self.refresh()

    def edit_question(self):
        question = self._questions.get(self.selected_id(self.table))
        if not question:
            QMessageBox.information(self, "CBT Center", "Select a question first.")
            return
        dialog = QuestionDialog(self._options(), question, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            v = dialog.values()
            if self.guard(lambda: cbt.update_question(self.username, question["id"], v["qtype"], v["prompt"], v["options"],
                                                      v["answer"], v["marks"], v["explanation"], v["subject_id"])):
                self.refresh()

    def toggle_archive(self):
        question = self._questions.get(self.selected_id(self.table))
        if not question:
            QMessageBox.information(self, "CBT Center", "Select a question first.")
            return
        if self.guard(lambda: cbt.archive_question(self.username, question["id"], not question["is_archived"])):
            self.refresh()

    def import_questions(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import questions", "", tabular.SPREADSHEET_FILTER)
        if not path:
            return
        try:
            preview = cbt.preview_question_import(path)
        except Exception as exc:
            QMessageBox.warning(self, "Import questions", str(exc))
            return
        if QMessageBox.question(self, "Import questions", f"Import {preview['row_count']} question(s)?") != QMessageBox.StandardButton.Yes:
            return
        if self.guard(lambda: cbt.import_questions(self.username, preview["rows"]),
                      lambda n: QMessageBox.information(self, "Import questions", f"{n} question(s) imported.")):
            self.refresh()

    def save_template(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save question template", "cbt_questions_template.xlsx",
                                              "Excel workbook (*.xlsx);;CSV (*.csv)")
        if path:
            self.guard(lambda: cbt.write_question_template(path),
                       lambda _r: QMessageBox.information(self, "Template", "Template saved. Fill it in and import it."))


class ExamsTab(_CBTTab):
    HEADERS = ["Title", "Class", "Subject", "Questions", "Marks", "Minutes", "Status", "Access code", "Submitted", "Writing now"]

    def __init__(self, username, on_change=None):
        super().__init__(username)
        self._exams: dict[int, dict] = {}
        self._on_change = on_change
        layout = QVBoxLayout(self)
        layout.addWidget(wrapped_label("Create an exam, choose its questions, then open it. Students sign in from a browser with their "
                                       "admission number and the exam's access code."))
        self.table = _table(self.HEADERS)
        layout.addWidget(self.table, 1)
        row1, row2 = QHBoxLayout(), QHBoxLayout()
        self.buttons = {}
        for name, row in (("New exam", row1), ("Edit", row1), ("Choose questions…", row1), ("Duplicate", row1), ("Delete", row1),
                          ("Open exam", row2), ("Close exam", row2), ("New access code", row2)):
            button = QPushButton(name)
            self.buttons[name] = button
            row.addWidget(button)
        self.buttons["New exam"].setObjectName("primary")
        self.buttons["Open exam"].setObjectName("primary")
        row1.addStretch(1)
        row2.addStretch(1)
        layout.addLayout(row1)
        layout.addLayout(row2)
        self.buttons["New exam"].clicked.connect(self.new_exam)
        self.buttons["Edit"].clicked.connect(self.edit_exam)
        self.buttons["Choose questions…"].clicked.connect(self.choose_questions)
        self.buttons["Duplicate"].clicked.connect(lambda: self._simple(cbt.duplicate_exam))
        self.buttons["Delete"].clicked.connect(self.delete_exam)
        self.buttons["Open exam"].clicked.connect(self.open_exam)
        self.buttons["Close exam"].clicked.connect(self.close_exam)
        self.buttons["New access code"].clicked.connect(lambda: self._simple(cbt.regenerate_code))
        self.table.itemSelectionChanged.connect(self._buttons)
        self._buttons()

    def refresh(self):
        self.run(lambda: cbt.list_exams(self.username), self._show)

    def _show(self, exams):
        self._exams = {e["id"]: e for e in exams}
        labels = {"draft": "Draft", "open": "Open", "closed": "Closed"}
        rows = [[e["title"], e["class_label"], e["subject_name"] or "", e["question_count"], e["total_marks"], e["duration_minutes"],
                 labels[e["status"]], e["access_code"] if e["status"] == "open" else "", e["submitted"], e["in_progress"]] for e in exams]
        _fill(self.table, rows, [e["id"] for e in exams])
        self._buttons()
        if self._on_change:
            self._on_change(exams)

    def current(self):
        return self._exams.get(self.selected_id(self.table))

    def _buttons(self):
        exam = self.current()
        status = exam["status"] if exam else None
        enabled = {"New exam": True, "Edit": status == "draft", "Choose questions…": status == "draft", "Duplicate": exam is not None,
                   "Delete": status == "draft", "Open exam": status in ("draft", "closed"), "Close exam": status == "open",
                   "New access code": status in ("draft", "open")}
        for name, button in self.buttons.items():
            button.setEnabled(enabled[name])

    def _simple(self, function):
        exam = self.current()
        if exam and self.guard(lambda: function(self.username, exam["id"])):
            self.refresh()

    def new_exam(self):
        try:
            options = cbt.form_options(self.username)
        except Exception as exc:
            QMessageBox.warning(self, "CBT Center", str(exc))
            return
        if not options["classes"] or not options["sessions"]:
            QMessageBox.information(self, "CBT Center", "Add a class and an academic session first (Setup guide).")
            return
        dialog = ExamDialog(options, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            v = dialog.values()
            if self.guard(lambda: cbt.create_exam(self.username, **v),
                          lambda _id: QMessageBox.information(self, "CBT Center", "Exam created. Now choose its questions.")):
                self.refresh()

    def edit_exam(self):
        exam = self.current()
        if not exam:
            return
        dialog = ExamDialog(cbt.form_options(self.username), exam, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            v = dialog.values()
            if self.guard(lambda: cbt.update_exam(self.username, exam["id"], **v)):
                self.refresh()

    def choose_questions(self):
        exam = self.current()
        if not exam:
            return
        try:
            questions = cbt.list_questions(self.username)
            chosen = [q["id"] for q in cbt.exam_questions(self.username, exam["id"])]
        except Exception as exc:
            QMessageBox.warning(self, "CBT Center", str(exc))
            return
        if not questions:
            QMessageBox.information(self, "CBT Center", "The question bank is empty. Add questions on the Question Bank tab first.")
            return
        dialog = QuestionPickerDialog(questions, chosen, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            ids = dialog.selected_ids()
            if self.guard(lambda: cbt.set_exam_questions(self.username, exam["id"], ids)):
                self.refresh()

    def delete_exam(self):
        exam = self.current()
        if exam and QMessageBox.question(self, "Delete exam", f"Delete the draft exam “{exam['title']}”?") == QMessageBox.StandardButton.Yes:
            self._simple(cbt.delete_exam)

    def open_exam(self):
        exam = self.current()
        if not exam:
            return
        if self.guard(lambda: cbt.open_exam(self.username, exam["id"]),
                      lambda code: QMessageBox.information(
                          self, "Exam open", f"The exam is open.\n\nAccess code: {code}\n\nStart the exam server on the "
                                             "“Server & Live Monitor” tab and give students the web address and this code.")):
            self.refresh()

    def close_exam(self):
        exam = self.current()
        if not exam:
            return
        if QMessageBox.question(self, "Close exam", "Close this exam? Students still writing will have their answers submitted now."
                                ) != QMessageBox.StandardButton.Yes:
            return
        if self.guard(lambda: cbt.close_exam(self.username, exam["id"]),
                      lambda n: QMessageBox.information(self, "Exam closed", f"Exam closed. {n} unfinished attempt(s) were submitted.")):
            self.refresh()


class ServerTab(_CBTTab):
    HEADERS = ["Admission no", "Student", "Status", "Answered", "Time left", "Tab switches", "Score"]

    def __init__(self, username):
        super().__init__(username)
        self._students: dict[int, dict] = {}
        layout = QVBoxLayout(self)
        layout.addWidget(wrapped_label("Start the exam server so students can open the exam in a web browser on this network. "
                                       "Keep this window open while students are writing."))
        row = QHBoxLayout()
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(8080)
        self.lan = QCheckBox("Let other computers on the network connect")
        self.lan.setChecked(True)
        self.start_button = QPushButton("Start server")
        self.start_button.setObjectName("primary")
        self.stop_button = QPushButton("Stop server")
        for widget in (QLabel("Port"), self.port, self.lan, self.start_button, self.stop_button):
            row.addWidget(widget)
        row.addStretch(1)
        layout.addLayout(row)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        layout.addWidget(self.status)
        urls = QHBoxLayout()
        self.urls = QLabel("")
        self.urls.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.copy_button = QPushButton("Copy address")
        urls.addWidget(self.urls, 1)
        urls.addWidget(self.copy_button)
        layout.addLayout(urls)
        layout.addWidget(wrapped_label("Security: connections use plain HTTP, so use a school-only network or an isolated lab Wi-Fi, "
                                       "never public Wi-Fi. Students sign in with their admission number and the exam code."))
        picker = QHBoxLayout()
        self.exam_box = QComboBox()
        self.code_label = QLabel("")
        self.code_label.setObjectName("cardValue")
        picker.addWidget(QLabel("Monitor exam"))
        picker.addWidget(self.exam_box, 1)
        picker.addWidget(QLabel("Access code"))
        picker.addWidget(self.code_label)
        layout.addLayout(picker)
        self.summary = QLabel("")
        layout.addWidget(self.summary)
        self.table = _table(self.HEADERS)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.reset_button = QPushButton("Let student retake (reset)")
        self.close_button = QPushButton("Close exam now")
        self.refresh_button = QPushButton("Refresh")
        for b in (self.reset_button, self.close_button, self.refresh_button):
            actions.addWidget(b)
        actions.addStretch(1)
        layout.addLayout(actions)
        self._exams: dict[int, dict] = {}
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self._load_monitor)
        self.start_button.clicked.connect(self.start_server)
        self.stop_button.clicked.connect(self.stop_server)
        self.copy_button.clicked.connect(self.copy_address)
        self.exam_box.currentIndexChanged.connect(lambda _i: self._load_monitor())
        self.refresh_button.clicked.connect(self._load_monitor)
        self.reset_button.clicked.connect(self.reset_student)
        self.close_button.clicked.connect(self.close_exam)
        self._sync_server()

    # server control
    def _sync_server(self):
        running = bool(_SERVER and _SERVER.running)
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.port.setEnabled(not running)
        self.lan.setEnabled(not running)
        self.copy_button.setEnabled(running)
        if running:
            self.status.setText(f"Server running · {_SERVER.active_sessions} student(s) signed in")
            self.urls.setText("Students open:  " + "   or   ".join(_SERVER.urls()))
        else:
            self.status.setText("Server stopped")
            self.urls.setText("")

    def start_server(self):
        global _SERVER
        server = CBTServer("0.0.0.0" if self.lan.isChecked() else "127.0.0.1", self.port.value())
        if not self.guard(server.start):
            return
        _SERVER = server
        self._sync_server()

    def stop_server(self):
        global _SERVER
        if _SERVER and QMessageBox.question(self, "Stop server", "Stop the exam server? Students will not be able to continue until "
                                            "it is started again. Their saved answers are kept."
                                            ) != QMessageBox.StandardButton.Yes:
            return
        if _SERVER:
            _SERVER.stop()
            _SERVER = None
        self._sync_server()

    def copy_address(self):
        if _SERVER and _SERVER.running:
            QGuiApplication.clipboard().setText(_SERVER.urls()[0])
            self.status.setText("Address copied")

    # monitor
    def set_exams(self, exams):
        live = [e for e in exams if e["status"] != "draft"]
        self._exams = {e["id"]: e for e in live}
        current = self.exam_box.currentData()
        self.exam_box.blockSignals(True)
        self.exam_box.clear()
        for e in live:
            self.exam_box.addItem(f"{e['title']} — {e['class_label']} ({'open' if e['status'] == 'open' else 'closed'})", e["id"])
        _select(self.exam_box, current)
        self.exam_box.blockSignals(False)

    def refresh(self):
        self._sync_server()
        self.run(lambda: cbt.list_exams(self.username), lambda exams: (self.set_exams(exams), self._load_monitor()))

    def _load_monitor(self):
        exam_id = self.exam_box.currentData()
        self._sync_server()
        if exam_id is None:
            self.table.setRowCount(0)
            self.summary.setText("Open an exam on the Exams tab to monitor it here.")
            self.code_label.setText("")
            return
        self.run(lambda: cbt.monitor(self.username, exam_id), self._show_monitor)

    def _show_monitor(self, data):
        exam, counts = data["exam"], data["counts"]
        self.code_label.setText(exam["access_code"] if exam["status"] == "open" else "—")
        self.summary.setText(f"Not started: {counts['not_started']}   ·   Writing: {counts['in_progress']}   ·   Submitted: {counts['submitted']}")
        labels = {"not_started": "Not started", "in_progress": "Writing", "submitted": "Submitted"}
        self._students = {s["student_id"]: s for s in data["students"]}
        rows = []
        for s in data["students"]:
            answered = f"{s['answered']} / {s['total_questions']}" if s["state"] != "not_started" else ""
            score = f"{s['score']} / {s['max_score']}" if s["state"] == "submitted" else ""
            note = " (auto)" if s["auto_submitted"] and s["state"] == "submitted" else ""
            rows.append([s["admission_no"], s["name"], labels[s["state"]] + note, answered, _mmss(s["remaining_seconds"]),
                         s["focus_losses"] if s["state"] != "not_started" else "", score])
        _fill(self.table, rows, [s["student_id"] for s in data["students"]])

    def reset_student(self):
        student = self._students.get(self.selected_id(self.table))
        exam_id = self.exam_box.currentData()
        if not student or exam_id is None or student["state"] == "not_started":
            QMessageBox.information(self, "CBT Center", "Select a student who has started the exam.")
            return
        if QMessageBox.question(self, "Reset attempt", f"Reset {student['name']}'s attempt? Their answers are discarded and they can "
                                "start again. The old attempt stays in the record.") != QMessageBox.StandardButton.Yes:
            return
        if self.guard(lambda: cbt.reset_attempt(self.username, exam_id, student["student_id"])):
            self._load_monitor()

    def close_exam(self):
        exam_id = self.exam_box.currentData()
        exam = self._exams.get(exam_id)
        if not exam or exam["status"] != "open":
            QMessageBox.information(self, "CBT Center", "This exam is not open.")
            return
        if QMessageBox.question(self, "Close exam", "Close the exam now? Students still writing are submitted immediately."
                                ) != QMessageBox.StandardButton.Yes:
            return
        if self.guard(lambda: cbt.close_exam(self.username, exam_id)):
            self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)


class ResultsTab(_CBTTab):
    HEADERS = ["Position", "Admission no", "Student", "Score", "Percent", "Tab switches", "Submitted"]

    def __init__(self, username):
        super().__init__(username)
        layout = QVBoxLayout(self)
        layout.addWidget(wrapped_label("Results are marked automatically when a student submits or time runs out. Export to Excel to "
                                       "copy scores into Grade Entry."))
        row = QHBoxLayout()
        self.exam_box = QComboBox()
        self.export_button = QPushButton("Export results (Excel)…")
        row.addWidget(QLabel("Exam"))
        row.addWidget(self.exam_box, 1)
        row.addWidget(self.export_button)
        layout.addLayout(row)
        self.stats = QLabel("")
        layout.addWidget(self.stats)
        self.table = _table(self.HEADERS)
        layout.addWidget(self.table, 2)
        layout.addWidget(QLabel("How each question went (percent of students who got it right)"))
        self.analysis = _table(["Question", "Answered", "Correct", "Percent correct"])
        layout.addWidget(self.analysis, 1)
        self.exam_box.currentIndexChanged.connect(lambda _i: self._load())
        self.export_button.clicked.connect(self.export)

    def refresh(self):
        def show(exams):
            current = self.exam_box.currentData()
            self.exam_box.blockSignals(True)
            self.exam_box.clear()
            for e in exams:
                if e["status"] != "draft":
                    self.exam_box.addItem(f"{e['title']} — {e['class_label']}", e["id"])
            _select(self.exam_box, current)
            self.exam_box.blockSignals(False)
            self._load()
        self.run(lambda: cbt.list_exams(self.username), show)

    def _load(self):
        exam_id = self.exam_box.currentData()
        if exam_id is None:
            self.table.setRowCount(0)
            self.analysis.setRowCount(0)
            self.stats.setText("No exam has been opened yet.")
            return
        self.run(lambda: (cbt.exam_results(self.username, exam_id), cbt.question_analysis(self.username, exam_id)), self._show)

    def _show(self, payload):
        results, analysis = payload
        stats = results["stats"]
        if stats["average"] is None:
            self.stats.setText(f"No submissions yet · {stats['not_submitted']} student(s) have not submitted")
        else:
            self.stats.setText(f"Submitted {stats['submitted']} · Not submitted {stats['not_submitted']} · Average {stats['average']}% · "
                               f"Highest {stats['highest']}% · Lowest {stats['lowest']}%")
        rows = [[i, r["admission_no"], r["name"], f"{r['score']} / {r['max_score']}", f"{r['percent']}%", r["focus_losses"],
                 r["submitted_at"].replace("T", " ") + " UTC" + (" (auto)" if r["auto_submitted"] else "")]
                for i, r in enumerate(results["rows"], start=1)]
        _fill(self.table, rows)
        _fill(self.analysis, [[_short(a["prompt"], 80), a["answered"], a["correct"],
                               "" if a["percent_correct"] is None else f"{a['percent_correct']}%"] for a in analysis])

    def export(self):
        exam_id = self.exam_box.currentData()
        if exam_id is None:
            QMessageBox.information(self, "CBT Center", "Choose an exam first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export results", "cbt_results.xlsx", "Excel workbook (*.xlsx)")
        if path:
            self.guard(lambda: cbt.export_results_xlsx(self.username, exam_id, path),
                       lambda p: QMessageBox.information(self, "Export results", f"Saved to {p}"))


class CBTCenterPage(QWidget):
    def __init__(self, username: str, role: str):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        title = QLabel("CBT Center")
        title.setObjectName("heading")
        layout.addWidget(title)
        self.tabs = QTabWidget()
        self._tabs: list[_CBTTab] = []
        if role != "admin":
            layout.addWidget(wrapped_label("Only an administrator can use the CBT Center."))
            return
        layout.addWidget(wrapped_label("Computer-based tests: students answer on any browser; marking and results are automatic."))
        self.server = ServerTab(username)
        self.results = ResultsTab(username)
        self.exams = ExamsTab(username, on_change=self.server.set_exams)
        self.questions = QuestionsTab(username)
        self._tabs = [self.exams, self.questions, self.server, self.results]
        for tab, name in zip(self._tabs, ("Exams", "Question Bank", "Server & Live Monitor", "Results")):
            self.tabs.addTab(tab, name)
        self.tabs.currentChanged.connect(lambda index: self._tabs[index].refresh())
        layout.addWidget(self.tabs, 1)
        self.refresh()

    def refresh(self):
        if self._tabs:
            self._tabs[self.tabs.currentIndex()].refresh()
