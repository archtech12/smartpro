from PyQt6.QtCore import QBuffer, QByteArray, QDate, QIODevice, Qt, QTimer, QThreadPool, QEventLoop, pyqtSignal
from PyQt6.QtGui import QImage, QImageReader, QPixmap
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QComboBox, QTableWidget, QTableWidgetItem, QMessageBox,
    QDialog, QFormLayout, QDialogButtonBox, QStackedWidget, QFrame, QDateEdit,
    QFileDialog, QInputDialog, QProgressBar, QTabWidget, QScrollArea, QCheckBox
)
from .table_helpers import configure_table_headers
from .ui_helpers import wrapped_label
from . import database as db
from . import backups as backup_services
from .academic_ui import (
    AcademicCalendarPage,
    AssessmentSetupPage,
    ClassStagePage,
    ClassSubjectAssignmentsPage,
    GradeBandSetupPage,
)
from .enrollment_ui import EnrollmentPage
from .attendance_ui import AttendancePage
from .assessment_ui import GradeEntryPage
from .timetable_ui import TimetablePage
from .settings_ui import SchoolProfilePage
from .user_accounts_ui import UserAccountsPage
from .diagnostics_ui import DiagnosticsPage
from .fees_ui import FeeInvoicePage, FeeReconciliationPage, FeeSetupPage
from .security import hash_password, verify_password
from .workers import BackgroundTask
from .student_import import write_student_template
from .student_import_ui import StudentImportDialog

STYLE = """
QWidget { font-family: Segoe UI; font-size: 10pt; color: #182230; background: #f5f7fb; }
QLabel#heading { font-size: 22pt; font-weight: 700; color: #102a43; }
QLabel#muted { color: #607080; }
QPushButton { padding: 8px 13px; border: 1px solid #d0d7e2; border-radius: 6px; background: white; }
QPushButton:hover { background: #eaf1fb; }
QPushButton#primary { background: #155eef; color: white; border: 0; font-weight: 600; }
QLineEdit, QComboBox { padding: 8px; border: 1px solid #cbd5e1; border-radius: 5px; background: white; }
QTableWidget { background: white; border: 1px solid #d9e0ea; gridline-color: #e6ebf2; }
QHeaderView::section { background: #edf2f7; padding: 8px; border: 0; font-weight: 600; }
QFrame#sidebar { background: #102a43; }
QTabWidget#navTabs::pane { border: 0; }
QTabWidget#navTabs QTabBar::tab { background: #173b5d; color: #d9e6f2; padding: 8px 12px; border: 0; }
QTabWidget#navTabs QTabBar::tab:selected { background: #24547b; color: white; font-weight: 600; }
QScrollArea#navScroll, QScrollArea#navScroll QWidget { background: #102a43; }
QPushButton#nav { background: transparent; color: white; text-align: left; border: 0; padding: 11px; }
QPushButton#nav:hover { background: #254766; }
"""

class SetupDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Set up SmartSchool Manager")
        self.setMinimumWidth(380)
        layout = QFormLayout(self)
        layout.addRow(QLabel("Create your first administrator account."))
        self.username = QLineEdit()
        self.username.setPlaceholderText("e.g. admin")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm = QLineEdit()
        self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addRow("Username", self.username)
        layout.addRow("Password", self.password)
        layout.addRow("Confirm password", self.confirm)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def validate(self):
        u, p, c = self.username.text().strip(), self.password.text(), self.confirm.text()
        if len(u) < 3:
            QMessageBox.warning(self, "Check username", "Use at least 3 characters for the username.")
            return
        if len(p) < 10:
            QMessageBox.warning(self, "Check password", "Use a password of at least 10 characters.")
            return
        if p != c:
            QMessageBox.warning(self, "Check password", "The passwords do not match.")
            return
        self.credentials = (u, p)
        self.accept()

class LoginDialog(QDialog):
    def __init__(self):
        super().__init__()
        self._auth_id = 0
        self._auth_workers = {}
        self._thread_pool = QThreadPool.globalInstance()
        self.setWindowTitle("SmartSchool Manager — Sign in")
        self.setFixedWidth(430)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 26)
        layout.setSpacing(14)
        brand = QLabel("SMARTSCHOOL MANAGER")
        brand.setObjectName("loginBrand")
        layout.addWidget(brand)
        title = QLabel("Welcome back")
        title.setObjectName("loginTitle")
        layout.addWidget(title)
        subtitle = QLabel("Sign in with your school account to continue.")
        subtitle.setObjectName("loginSubtitle")
        layout.addWidget(subtitle)

        form = QFormLayout()
        form.setContentsMargins(0, 8, 0, 0)
        form.setVerticalSpacing(10)
        self.username = QLineEdit()
        self.username.setPlaceholderText("Enter your username")
        self.username.setClearButtonEnabled(True)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Enter your password")
        form.addRow("Username", self.username)
        form.addRow("Password", self.password)
        layout.addLayout(form)
        self.auth_status = QLabel()
        self.auth_status.setObjectName("loginStatus")
        layout.addWidget(self.auth_status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.sign_in_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.sign_in_button.setText("Sign in")
        self.sign_in_button.setObjectName("primary")
        self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Close")
        self.buttons.accepted.connect(self.validate)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.password.returnPressed.connect(self.validate)
        self.username.setFocus()
        self.setStyleSheet("""
            QDialog { background: #f5f7fb; }
            QLabel#loginBrand { color: #155eef; font-size: 9pt; font-weight: 700; letter-spacing: 1px; }
            QLabel#loginTitle { color: #102a43; font-size: 23pt; font-weight: 700; }
            QLabel#loginSubtitle { color: #607080; font-size: 10pt; }
            QLabel#loginStatus { color: #b42318; min-height: 16px; }
            QLineEdit { min-height: 22px; padding: 9px 10px; border: 1px solid #cbd5e1; border-radius: 6px; background: white; }
            QLineEdit:focus { border: 1px solid #155eef; }
            QPushButton { min-width: 82px; padding: 9px 14px; border: 1px solid #d0d7e2; border-radius: 6px; background: white; }
            QPushButton#primary { background: #155eef; color: white; border: 0; font-weight: 600; }
            QPushButton#primary:hover { background: #004eeb; }
        """)

    def validate(self):
        if self._auth_workers:
            return
        username = self.username.text()
        password = self.password.text()

        def authenticate_and_log():
            user, retry_seconds = db.authenticate(username, password, verify_password)
            if not user:
                return None, retry_seconds
            user = dict(user)
            db.log_action(user["username"], "LOGIN", "users", user["id"])
            return user, 0

        self._auth_id += 1
        auth_id = self._auth_id
        worker = BackgroundTask(auth_id, authenticate_and_log)
        worker.signals.finished.connect(self._authentication_finished)
        self._auth_workers[auth_id] = worker
        self.username.setEnabled(False)
        self.password.setEnabled(False)
        self.buttons.setEnabled(False)
        self.auth_status.setText("Signing in…")
        self._thread_pool.start(worker)

    def _authentication_finished(self, auth_id, result, error):
        self._auth_workers.pop(auth_id, None)
        if auth_id != self._auth_id:
            return
        self.username.setEnabled(True)
        self.password.setEnabled(True)
        self.buttons.setEnabled(True)
        if error is not None:
            self.auth_status.clear()
            QMessageBox.warning(self, "Sign-in failed", str(error))
            return
        user, retry_seconds = result
        if not user:
            suffix = f" Try again in {retry_seconds} seconds." if retry_seconds else ""
            self.auth_status.clear()
            QMessageBox.warning(self, "Sign-in failed", "The username or password is incorrect." + suffix)
            return
        self.user = user
        self.accept()

class AddDialog(QDialog):
    def __init__(self, module_name, values=None):
        super().__init__()
        self.module_name = module_name
        self.is_edit = values is not None
        self.setWindowTitle(("Edit " if self.is_edit else "Add ") + module_name.rstrip("s"))
        self.inputs = []
        layout = QFormLayout(self)
        for index, (field, label) in enumerate(db.MODULES[module_name]["fields"]):
            edit = QLineEdit()
            edit.setPlaceholderText(label)
            if values is not None:
                edit.setText(str(values[index] if values[index] is not None else ""))
                if module_name == "Students" and field in {"admission_no", "class_name"}:
                    edit.setReadOnly(True)
            self.inputs.append(edit)
            layout.addRow(label, edit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def values(self):
        return [w.text() for w in self.inputs]


class StudentProfileDialog(QDialog):
    """Read-only, asynchronously loaded overview of one student's school history."""

    def __init__(self, student_id: int, username: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Student profile")
        self.resize(900, 620)
        layout = QVBoxLayout(self)
        self.heading = QLabel("Loading student profile…")
        self.heading.setObjectName("heading")
        layout.addWidget(self.heading)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.photo_preview = QLabel("No student photo")
        self.photo_preview.setFixedSize(112, 112)
        layout.addWidget(self.photo_preview)
        self.tabs = QTabWidget()
        self.tables = {}
        for key, title, headers in (
            ("enrollments", "Enrollment history", ["Academic Session", "Class", "Enrollment Status", "Enrollment Date", "Exit Date"]),
            ("approved_results", "Approved results", ["Academic Session", "Academic Term", "Class", "Subject", "Percentage", "Grade", "Grade Remark"]),
            ("invoices", "Fee invoices", ["Academic Session", "Academic Term", "Class", "Invoice Number", "Invoice Total", "Amount Paid", "Balance Due", "Invoice Status"]),
        ):
            table = QTableWidget(0, len(headers))
            table.setHorizontalHeaderLabels(headers)
            configure_table_headers(table)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setAlternatingRowColors(True)
            self.tables[key] = table
            self.tabs.addTab(table, title)
        layout.addWidget(self.tabs, 1)
        self._workers = []
        self._pool = QThreadPool.globalInstance()
        worker = BackgroundTask(student_id, lambda: db.load_student_profile_data(student_id, username))
        worker.signals.finished.connect(self._loaded)
        self._workers.append(worker)
        self._pool.start(worker)

    def _loaded(self, _request_id, data, error):
        if error:
            self.heading.setText("Could not load student profile")
            self.summary.setText(str(error))
            return
        student = data["student"]
        self.heading.setText(f"{student['last_name']}, {student['first_name']} — {student['admission_no']}")
        state = "Active" if student["is_active"] else "Archived"
        attendance = data["attendance"]
        photo_note = "Photo on file" if student.get("photo_data") else "No photo"
        if student.get("photo_data"):
            image = QImage.fromData(student["photo_data"])
            if not image.isNull():
                self.photo_preview.setText("")
                self.photo_preview.setPixmap(QPixmap.fromImage(image).scaled(
                    104, 104, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
                ))
        self.summary.setText(
            f"{state} · Gender: {student['gender'] or '—'} · Guardian phone: {student['guardian_phone'] or '—'} · "
            f"NIN: {student['nin'] or '—'} · Date of birth: {student['date_of_birth'] or '—'} · Email: {student['email'] or '—'} · "
            f"{photo_note} · Attendance marks: {attendance['present']} present, {attendance['absent']} absent"
        )
        for key, rows in data.items():
            if key not in self.tables:
                continue
            table = self.tables[key]
            table.setRowCount(len(rows))
            for row_index, row in enumerate(rows):
                if key == "enrollments":
                    values = (row["session_name"], " ".join((row["class_name"], row["section"])).strip(),
                              row["status"].title(), row["enrolled_on"], row["left_on"] or "")
                elif key == "approved_results":
                    values = (row["session"], row["term"], row["class"], row["subject"],
                              row["percent"] or "Incomplete", row["grade"], row["remark"])
                else:
                    values = (row["session_name"], row["term_name"],
                              " ".join((row["class_name"], row["section"])).strip(), row["invoice_number"],
                              row["total_amount_text"], row["paid_amount"], row["balance_amount"],
                              "Issued" if row["issued_at"] else row["status"].title())
                for column, value in enumerate(values):
                    table.setItem(row_index, column, QTableWidgetItem(str(value or "")))
            table.resizeColumnsToContents()


class StudentRegistrationDialog(QDialog):
    """Guided new-student form with class placement and generated admission ID."""
    def __init__(self, options, parent=None):
        super().__init__(parent)
        self._sessions = options["sessions"]
        self.setWindowTitle("Register student")
        self.setMinimumWidth(440)
        form = QFormLayout(self)
        self.first_name = QLineEdit()
        self.first_name.setMaxLength(100)
        self.last_name = QLineEdit()
        self.last_name.setMaxLength(100)
        self.gender = QComboBox()
        self.gender.addItems(["", "Female", "Male", "Other"])
        self.guardian_phone = QLineEdit()
        self.guardian_phone.setMaxLength(60)
        self.nin = QLineEdit()
        self.nin.setMaxLength(60)
        self.nin.setPlaceholderText("Optional")
        self.date_of_birth_enabled = QCheckBox("Enter date of birth")
        self.date_of_birth = QDateEdit()
        self.date_of_birth.setCalendarPopup(True)
        self.date_of_birth.setDisplayFormat("yyyy-MM-dd")
        self.date_of_birth.setMinimumDate(QDate(1900, 1, 1))
        self.date_of_birth.setMaximumDate(QDate.currentDate())
        self.date_of_birth.setEnabled(False)
        self.date_of_birth_enabled.toggled.connect(self.date_of_birth.setEnabled)
        self.email = QLineEdit()
        self.email.setMaxLength(254)
        self.email.setPlaceholderText("Optional")
        self.class_section = QComboBox()
        for item in options["classes"]:
            stage = db.EDUCATIONAL_STAGES.get(item["educational_stage"], "Stage not set")
            name = " ".join(part for part in (item["class_name"], item["section"]) if part)
            self.class_section.addItem(f"{name} — {stage}", int(item["id"]))
        self.session = QComboBox()
        for item in self._sessions:
            self.session.addItem(item["name"], int(item["id"]))
        self.enrolled_on = QDateEdit()
        self.photo_data = None
        self.photo_label = QLabel("Optional: no student photo")
        self.photo_button = QPushButton("Choose photo…")
        self.photo_button.clicked.connect(self.choose_photo)
        self.enrolled_on.setCalendarPopup(True)
        self.enrolled_on.setDisplayFormat("yyyy-MM-dd")
        self.session.currentIndexChanged.connect(self._set_session_date_range)
        form.addRow("First name *", self.first_name)
        form.addRow("Last name *", self.last_name)
        form.addRow("Gender", self.gender)
        form.addRow("Guardian phone", self.guardian_phone)
        form.addRow("NIN (optional)", self.nin)
        birth_row = QHBoxLayout()
        birth_row.addWidget(self.date_of_birth_enabled)
        birth_row.addWidget(self.date_of_birth, 1)
        form.addRow("Date of birth", birth_row)
        form.addRow("Email (optional)", self.email)
        form.addRow("Class and section *", self.class_section)
        form.addRow("Academic session *", self.session)
        form.addRow("Enrollment date", self.enrolled_on)
        photo_row = QHBoxLayout()
        photo_row.addWidget(self.photo_label, 1)
        photo_row.addWidget(self.photo_button)
        form.addRow("Student photo", photo_row)
        form.addRow("Admission ID", QLabel("Generated automatically when saved"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        if not self._sessions:
            self.session.addItem("Create an academic session in Settings first", None)
        if not options["classes"]:
            self.class_section.addItem("Create a class in Settings first", None)
        self._set_session_date_range()

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

    def _accept_if_valid(self):
        if not self.first_name.text().strip() or not self.last_name.text().strip():
            QMessageBox.information(self, "Complete required details", "Enter the student's first and last name.")
            return
        if self.class_section.currentData() is None or self.session.currentData() is None:
            QMessageBox.information(self, "Setup required", "Create a class and academic session before registering a student.")
            return
        self.accept()

    def values(self):
        return {
            "first_name": self.first_name.text(),
            "last_name": self.last_name.text(),
            "gender": self.gender.currentText(),
            "guardian_phone": self.guardian_phone.text(),
            "nin": self.nin.text(),
            "date_of_birth": self.date_of_birth.date().toString("yyyy-MM-dd") if self.date_of_birth_enabled.isChecked() else "",
            "email": self.email.text(),
            "class_id": int(self.class_section.currentData()),
            "academic_session_id": int(self.session.currentData()),
            "enrolled_on": self.enrolled_on.date().toString("yyyy-MM-dd"),
            "photo_data": self.photo_data,
        }

    def choose_photo(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose student photo", "", "Images (*.png *.jpg *.jpeg)")
        if not path:
            return
        try:
            with open(path, "rb") as photo_file:
                image_data = photo_file.read(1_000_001)
        except OSError as exc:
            QMessageBox.warning(self, "Could not read photo", str(exc))
            return
        if not image_data or len(image_data) > 1_000_000:
            QMessageBox.warning(self, "Photo too large", "Choose a PNG or JPEG photo no larger than 1 MB.")
            return
        buffer = QBuffer(QByteArray(image_data))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        reader.setDecideFormatFromContent(True)
        size = reader.size()
        if size.isEmpty() or size.width() > 3000 or size.height() > 1800 or reader.read().isNull():
            QMessageBox.warning(self, "Unsupported photo", "Choose a readable PNG or JPEG no larger than 3000 × 1800 pixels.")
            return
        self.photo_data = image_data
        self.photo_label.setText(path.replace("\\", "/").rsplit("/", 1)[-1])

class ModulePage(QWidget):
    def __init__(self, module_name, username):
        super().__init__()
        self.module_name = module_name
        self.username = username
        self._refresh_id = 0
        self._export_id = 0
        self._write_id = 0
        self._registration_options_id = 0
        self._registration_mode = "register"
        self._workers = {}
        self._export_workers = {}
        self._write_workers = {}
        self._write_messages = {}
        self._writing = False
        self._thread_pool = QThreadPool.globalInstance()
        self._page = 0
        self._page_size = 200
        self._total_rows = 0
        layout = QVBoxLayout(self)
        self.title = QLabel("Student register" if module_name == "Students" else module_name)
        self.title.setObjectName("heading")
        layout.addWidget(self.title)
        header = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search this module…")
        self.search.setMaximumWidth(260)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(250)
        self.search_timer.timeout.connect(self.refresh)
        self.search.textChanged.connect(self._search_changed)
        self.student_status = None
        if module_name == "Students":
            self.student_status = QComboBox()
            self.student_status.addItem("All students", "all")
            self.student_status.addItem("Active only", "active")
            self.student_status.addItem("Archived only", "archived")
            self.student_status.currentIndexChanged.connect(self._student_status_changed)
            header.addWidget(self.student_status)
        header.addWidget(self.search, 1)
        self.add_btn = QPushButton("+ Add record")
        if module_name == "Students":
            self.add_btn.setText("+ Register student")
        self.add_btn.setObjectName("primary")
        self.add_btn.clicked.connect(self.add_record)
        header.addWidget(self.add_btn)
        self.import_btn = None
        self.template_btn = None
        if module_name == "Students":
            self.import_btn = QPushButton("Import students CSV…")
            self.import_btn.clicked.connect(self.import_students_csv)
            self.template_btn = QPushButton("CSV template")
            self.template_btn.clicked.connect(self.export_student_template)
        self.edit_btn = QPushButton("Edit selected")
        if module_name == "Students":
            self.edit_btn.setText("Edit student details")
        self.edit_btn.setEnabled(False)
        self.edit_btn.clicked.connect(self.edit_record)
        header.addWidget(self.edit_btn)
        self.export_btn = QPushButton("Export CSV")
        self.export_btn.clicked.connect(self.export)
        header.addWidget(self.export_btn)
        layout.addLayout(header)
        if self.import_btn is not None:
            roster_tools = QHBoxLayout()
            roster_tools.addWidget(QLabel("Register a class roster from a file:"))
            roster_tools.addWidget(self.template_btn)
            roster_tools.addWidget(self.import_btn)
            roster_tools.addStretch()
            layout.addLayout(roster_tools)
        self.table = QTableWidget()
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.itemSelectionChanged.connect(self._update_selection_actions)
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
        footer_message = wrapped_label(
            "Archive keeps the student and enrollment history; restore makes the record active again."
            if module_name == "Students" else "Select a row to delete a record. Deletion is permanent."
        )
        footer = QHBoxLayout()
        self.delete_btn = QPushButton("Delete selected")
        self.delete_btn.clicked.connect(self.delete_record)
        self.archive_btn = None
        self.photo_btn = None
        self.remove_photo_btn = None
        self.profile_btn = None
        if module_name == "Students":
            layout.addWidget(footer_message)
            self.delete_btn.hide()
            footer.addStretch()
            self.profile_btn = QPushButton("View student profile")
            self.profile_btn.setEnabled(False)
            self.profile_btn.clicked.connect(self.view_student_profile)
            footer.addWidget(self.profile_btn)
            self.archive_btn = QPushButton("Archive student")
            self.archive_btn.setEnabled(False)
            self.archive_btn.clicked.connect(self.toggle_student_archive)
            footer.addWidget(self.archive_btn)
            self.photo_btn = QPushButton("Set student photo")
            self.photo_btn.setEnabled(False)
            self.photo_btn.clicked.connect(self.set_student_photo)
            footer.addWidget(self.photo_btn)
            self.remove_photo_btn = QPushButton("Remove photo")
            self.remove_photo_btn.setEnabled(False)
            self.remove_photo_btn.clicked.connect(self.remove_student_photo)
            footer.addWidget(self.remove_photo_btn)
        else:
            footer.addWidget(footer_message, 1)
            footer.addWidget(self.delete_btn)
        layout.addLayout(footer)
        self.refresh()

    def refresh(self):
        self._refresh_id += 1
        request_id = self._refresh_id
        search_text = self.search.text()
        student_status = self.student_status.currentData() if self.student_status is not None else "all"
        worker = BackgroundTask(
            request_id, lambda: db.list_rows_page(
                self.module_name, search_text, self._page_size, self._page * self._page_size,
                student_status=student_status,
            )
        )
        self.page_status.setText("Loading…")
        self.previous_page.setEnabled(False)
        self.next_page.setEnabled(False)
        self.table.setEnabled(False)
        self.delete_btn.setEnabled(False)
        if self.archive_btn is not None:
            self.archive_btn.setEnabled(False)
        self.edit_btn.setEnabled(False)
        worker.signals.finished.connect(self._rows_loaded)
        self._workers[("summary", request_id)] = worker
        self._thread_pool.start(worker)

    def _rows_loaded(self, request_id, rows, error):
        self._workers.pop(("summary", request_id), None)
        if request_id != self._refresh_id:
            return
        if error is not None:
            self.table.setEnabled(True)
            self._update_selection_actions()
            self.page_status.setText("Could not load records")
            QMessageBox.warning(self, "Could not load records", str(error))
            return
        rows, total_rows = rows
        if not rows and self._page > 0 and total_rows <= self._page * self._page_size:
            self._page = max(0, (total_rows - 1) // self._page_size)
            self.refresh()
            return
        self._total_rows = total_rows
        fields = db.MODULES[self.module_name]["fields"]
        self.table.setUpdatesEnabled(False)
        try:
            extra_status = self.module_name == "Students"
            self.table.setColumnCount(len(fields) + 1 + int(extra_status))
            self.table.setHorizontalHeaderLabels(["Record ID"] + [f[1] for f in fields] + (["Student Status"] if extra_status else []))
            configure_table_headers(self.table)
            self.table.setColumnHidden(0, True)
            self.table.setRowCount(len(rows))
            for r, row in enumerate(rows):
                values = [row["id"]] + [row[f[0]] for f in fields]
                if extra_status:
                    values.append("Active" if row["is_active"] else "Archived")
                for c, value in enumerate(values):
                    self.table.setItem(r, c, QTableWidgetItem(str(value if value is not None else "")))
        finally:
            self.table.setUpdatesEnabled(True)
            self.table.viewport().update()
            self.table.setEnabled(True)
            self._update_selection_actions()
        start = self._page * self._page_size + 1 if total_rows else 0
        end = self._page * self._page_size + len(rows)
        self.page_status.setText(f"Showing {start}–{end} of {total_rows}")
        self.previous_page.setEnabled(self._page > 0)
        self.next_page.setEnabled(end < total_rows)

    def _update_selection_actions(self):
        selected = self.table.isEnabled() and self.table.currentRow() >= 0 and not self._writing
        self.edit_btn.setEnabled(selected)
        self.delete_btn.setEnabled(selected)
        if self.archive_btn is not None:
            status_item = self.table.item(self.table.currentRow(), self.table.columnCount() - 1) if selected else None
            active = bool(status_item and status_item.text() == "Active")
            self.archive_btn.setText("Archive student" if active else "Restore student")
            self.archive_btn.setEnabled(selected)
        if self.photo_btn is not None:
            self.photo_btn.setEnabled(selected)
            self.remove_photo_btn.setEnabled(selected)
            self.profile_btn.setEnabled(selected)

    def view_student_profile(self):
        row = self.table.currentRow()
        if self.module_name != "Students" or row < 0:
            return
        student_id = int(self.table.item(row, 0).text())
        dialog = StudentProfileDialog(student_id, self.username, self)
        dialog.exec()

    def set_student_photo(self):
        row = self.table.currentRow()
        if self.module_name != "Students" or row < 0:
            return
        student_id = int(self.table.item(row, 0).text())
        path, _ = QFileDialog.getOpenFileName(self, "Choose student photo", "", "Images (*.png *.jpg *.jpeg)")
        if not path:
            return
        try:
            with open(path, "rb") as photo_file:
                image_data = photo_file.read(1_000_001)
        except OSError as exc:
            QMessageBox.warning(self, "Could not read photo", str(exc))
            return
        if not image_data or len(image_data) > 1_000_000:
            QMessageBox.warning(self, "Photo too large", "Choose a PNG or JPEG photo no larger than 1 MB.")
            return
        buffer = QBuffer(QByteArray(image_data))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        reader.setDecideFormatFromContent(True)
        size = reader.size()
        if size.isEmpty() or size.width() > 3000 or size.height() > 1800 or reader.read().isNull():
            QMessageBox.warning(self, "Unsupported photo", "Choose a readable PNG or JPEG no larger than 3000 × 1800 pixels.")
            return
        self._start_write(lambda: db.set_student_photo(student_id, image_data, self.username), "Student photo saved")

    def remove_student_photo(self):
        row = self.table.currentRow()
        if self.module_name != "Students" or row < 0:
            return
        student_id = int(self.table.item(row, 0).text())
        answer = QMessageBox.question(self, "Remove student photo", "Remove this student's optional photo?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_write(lambda: db.set_student_photo(student_id, None, self.username), "Student photo removed")

    def _search_changed(self, _text):
        self._page = 0
        self.search_timer.start()

    def _student_status_changed(self, _index):
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

    def add_record(self):
        if self.module_name == "Students":
            self._open_registration_workflow("register")
            return
        dialog = AddDialog(self.module_name)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()

        def save():
            return db.add_row(self.module_name, values, actor=self.username)

        self._start_write(save, "Record saved")

    def _open_registration_workflow(self, mode):
        self._registration_mode = mode
        self._registration_options_id += 1
        request_id = self._registration_options_id
        worker = BackgroundTask(request_id, db.load_student_registration_options)
        worker.signals.finished.connect(self._registration_options_loaded)
        self._workers[("registration_options", request_id)] = worker
        self.add_btn.setEnabled(False)
        if self.import_btn is not None:
            self.import_btn.setEnabled(False)
        self.page_status.setText("Loading classes and sessions…")
        self._thread_pool.start(worker)

    def import_students_csv(self):
        if self.module_name == "Students":
            self._open_registration_workflow("import")

    def export_student_template(self):
        path, _selected_filter = QFileDialog.getSaveFileName(
            self, "Save student CSV template", "student_registration_template.csv", "CSV files (*.csv)"
        )
        if not path:
            return
        if not path.casefold().endswith(".csv"):
            path += ".csv"
        try:
            write_student_template(path)
        except OSError as exc:
            QMessageBox.warning(self, "Could not save template", str(exc))
            return
        self.page_status.setText(f"Student template saved: {path}")
        QMessageBox.information(
            self, "Student template ready",
            "Fill first_name and last_name for each student. NIN, date_of_birth, email, gender, and guardian_phone are optional. "
            "Choose the class, session, and enrollment date in the import screen.",
        )

    def _registration_options_loaded(self, request_id, options, error):
        self._workers.pop(("registration_options", request_id), None)
        if request_id != self._registration_options_id:
            return
        self.add_btn.setEnabled(not self._writing)
        if self.import_btn is not None:
            self.import_btn.setEnabled(not self._writing)
        if error is not None:
            self.page_status.setText("Could not prepare registration")
            QMessageBox.warning(self, "Could not register student", str(error))
            return
        if self._registration_mode == "import":
            dialog = StudentImportDialog(options, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self.page_status.setText("Student import cancelled")
                return
            values = dialog.values()
            self._start_write(
                lambda: db.register_students_bulk(**values, actor=self.username),
                "Student import complete",
            )
        else:
            dialog = StudentRegistrationDialog(options, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                self.page_status.setText("Registration cancelled")
                return
            values = dialog.values()
            self._start_write(
                lambda: db.register_student(**values, actor=self.username),
                "Student registered",
            )

    def edit_record(self):
        row = self.table.currentRow()
        if row < 0 or not self.table.item(row, 0):
            QMessageBox.information(self, "Select a record", "Select a record to edit.")
            return
        row_id = int(self.table.item(row, 0).text())
        values = [
            self.table.item(row, column + 1).text()
            if self.table.item(row, column + 1) is not None else ""
            for column in range(len(db.MODULES[self.module_name]["fields"]))
        ]
        dialog = AddDialog(self.module_name, values)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        self._start_write(
            lambda: db.update_row(self.module_name, row_id, values, actor=self.username),
            "Record updated",
        )

    def delete_record(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Select a record", "Select a row first.")
            return
        row_id = int(self.table.item(row, 0).text())
        answer = QMessageBox.question(self, "Confirm deletion", f"Delete record ID {row_id}?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        def delete():
            db.delete_row(self.module_name, row_id, actor=self.username)

        self._start_write(delete, "Record deleted")

    def toggle_student_archive(self):
        row = self.table.currentRow()
        if self.module_name != "Students" or row < 0:
            return
        row_id = int(self.table.item(row, 0).text())
        status_item = self.table.item(row, self.table.columnCount() - 1)
        restore = bool(status_item and status_item.text() == "Archived")
        action = "restore" if restore else "archive"
        answer = QMessageBox.question(
            self, f"Confirm {action}",
            f"{action.title()} this student? Enrollment and academic history will remain available.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_write(
            lambda: db.set_student_active(row_id, restore, actor=self.username),
            "Student restored" if restore else "Student archived",
        )

    def _start_write(self, operation, success_message):
        if self._writing:
            return
        self._writing = True
        self._write_id += 1
        write_id = self._write_id
        worker = BackgroundTask(write_id, operation)
        worker.signals.finished.connect(self._write_finished)
        self._write_workers[write_id] = worker
        self._write_messages[write_id] = success_message
        self.add_btn.setEnabled(False)
        if self.import_btn is not None:
            self.import_btn.setEnabled(False)
        self.delete_btn.setEnabled(False)
        self.edit_btn.setEnabled(False)
        if self.archive_btn is not None:
            self.archive_btn.setEnabled(False)
        self.page_status.setText("Saving…")
        self._thread_pool.start(worker)

    def _write_finished(self, write_id, result, error):
        self._write_workers.pop(write_id, None)
        if write_id != self._write_id:
            return
        success_message = self._write_messages.pop(write_id, "Save complete")
        self._writing = False
        self.add_btn.setEnabled(True)
        if self.import_btn is not None:
            self.import_btn.setEnabled(True)
        self._update_selection_actions()
        if error is not None:
            self.page_status.setText("Save failed")
            QMessageBox.warning(self, "Could not save record", str(error))
            return
        if isinstance(result, dict) and result.get("admission_no"):
            success_message = f"Registered — admission ID {result['admission_no']}"
        elif isinstance(result, dict) and result.get("admission_numbers"):
            ids = result["admission_numbers"]
            success_message = f"Registered {result['count']} students — IDs {ids[0]} to {ids[-1]}"
        self.page_status.setText(success_message)
        self.refresh()

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", f"{self.module_name.lower()}.csv", "CSV files (*.csv)")
        if not path:
            return
        self._start_export(path)

    def _start_export(self, path):
        self._export_id += 1
        export_id = self._export_id
        worker = BackgroundTask(export_id, lambda: db.export_csv(self.module_name, path))
        worker.signals.finished.connect(self._export_finished)
        self._export_workers[export_id] = worker
        self.export_btn.setEnabled(False)
        self.export_btn.setText("Exporting…")
        self._thread_pool.start(worker)

    def _export_finished(self, export_id, _result, error):
        self._export_workers.pop(export_id, None)
        self.export_btn.setEnabled(True)
        self.export_btn.setText("Export CSV")
        if error is not None:
            QMessageBox.warning(self, "Export failed", str(error))
        else:
            QMessageBox.information(self, "Export complete", "CSV export finished.")

class Dashboard(QWidget):
    navigation_requested = pyqtSignal(str)

    def __init__(self, role="admin"):
        super().__init__()
        self.is_teacher = role == "teacher"
        self._refresh_id = 0
        self._workers = {}
        self._thread_pool = QThreadPool.globalInstance()
        layout = QVBoxLayout(self)
        heading = QLabel("Teacher workspace" if self.is_teacher else "School overview")
        heading.setObjectName("heading")
        layout.addWidget(heading)
        layout.addWidget(QLabel(
            "Enter scores and take attendance for classes assigned to your account." if self.is_teacher
            else "Offline-first school administration • Local database"
        ))
        self.cards = QHBoxLayout()
        layout.addLayout(self.cards)
        actions_heading = QLabel("Quick actions")
        actions_heading.setStyleSheet("font-size:14pt;font-weight:700;color:#102a43;")
        layout.addWidget(actions_heading)
        actions = QHBoxLayout()
        actions_to_show = (
            (("Grade entry", "Grade Entry"), ("Attendance", "Attendance")) if self.is_teacher else (
            ("Students", "Students"),
            ("Classes", "Classes"),
            ("Academic calendar", "Academic Calendar"),
            ("Enrollments", "Enrollments"),
            ("Class stages", "Class Stages"),
            ("Grade bands", "Grade Bands"),
            ("Grade entry", "Grade Entry"),
            ("Data backup", "Data Backup"),
            ("Attendance", "Attendance"),
            ("Timetable", "Timetable"),
            )
        )
        for label, page_name in actions_to_show:
            button = QPushButton(label)
            button.setObjectName("dashboardAction")
            button.clicked.connect(
                lambda checked=False, target=page_name: self.navigation_requested.emit(target)
            )
            actions.addWidget(button)
        layout.addLayout(actions)
        layout.addStretch()
        if not self.is_teacher:
            self.refresh()

    def refresh(self):
        if self.is_teacher:
            return
        self._refresh_id += 1
        request_id = self._refresh_id
        worker = BackgroundTask(request_id, db.counts)
        worker.signals.finished.connect(self._counts_loaded)
        self._workers[request_id] = worker
        self._thread_pool.start(worker)

    def _counts_loaded(self, request_id, counts, error):
        self._workers.pop(request_id, None)
        if request_id != self._refresh_id:
            return
        if error is not None:
            QMessageBox.warning(self, "Could not load dashboard", str(error))
            return
        while self.cards.count():
            item = self.cards.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for label, count in counts.items():
            card = QFrame()
            card.setStyleSheet("QFrame { background:white; border:1px solid #d9e0ea; border-radius:10px; }")
            col = QVBoxLayout(card)
            title = QLabel(label)
            title.setObjectName("muted")
            number = QLabel(str(count))
            number.setStyleSheet("font-size:25pt;font-weight:700;color:#155eef;")
            col.addWidget(title)
            col.addWidget(number)
            self.cards.addWidget(card)


class DataBackupPage(QWidget):
    def __init__(self, role: str):
        super().__init__()
        self.is_admin = role == "admin"
        self._task_id = 0
        self._workers = {}
        self._thread_pool = QThreadPool.globalInstance()
        self._busy = False
        layout = QVBoxLayout(self)
        title = QLabel("Data backup")
        title.setObjectName("heading")
        layout.addWidget(title)
        description = wrapped_label(
            "Create an offline snapshot of the local school database. Each backup is checked "
            "for SQLite integrity and schema history before it is saved. Backups contain sensitive school data. "
            "Restore creates a separate database file and never replaces the database currently in use."
        )
        layout.addWidget(description)
        actions = QHBoxLayout()
        self.create_button = QPushButton("Create verified backup…")
        self.verify_button = QPushButton("Verify existing backup…")
        self.restore_button = QPushButton("Restore to a separate file…")
        self.create_button.setObjectName("primary")
        self.create_button.setEnabled(self.is_admin)
        self.verify_button.setEnabled(self.is_admin)
        self.restore_button.setEnabled(self.is_admin)
        self.create_button.clicked.connect(self.choose_backup_destination)
        self.verify_button.clicked.connect(self.choose_backup_to_verify)
        self.restore_button.clicked.connect(self.choose_restore_source)
        actions.addWidget(self.create_button)
        actions.addWidget(self.verify_button)
        actions.addWidget(self.restore_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.status = QLabel("No backup operation has been run yet.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()

    def choose_backup_destination(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Create verified backup", "school-backup.db", "SQLite database (*.db)"
        )
        if path:
            self._start_task(
                lambda: backup_services.create_backup(path),
                "Creating and verifying backup…",
                "Created and verified backup",
            )

    def choose_backup_to_verify(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Verify database backup", "", "SQLite database (*.db);;All files (*)"
        )
        if path:
            self._start_task(
                lambda: backup_services.verify_backup(path), "Verifying backup…", "Backup verified"
            )

    def choose_restore_source(self):
        source, _ = QFileDialog.getOpenFileName(
            self, "Choose database backup to restore", "", "SQLite database (*.db);;All files (*)"
        )
        if not source:
            return
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "Restore to a separate database file",
            "school-restored.db",
            "SQLite database (*.db)",
        )
        if destination:
            self._start_task(
                lambda: backup_services.restore_to_new_file(source, destination),
                "Verifying backup and restoring to a separate file…",
                "Restored backup to a separate database file",
            )

    def _start_task(self, operation, progress_text, success_label):
        if self._busy:
            return
        self._busy = True
        self._task_id += 1
        task_id = self._task_id
        worker = BackgroundTask(task_id, operation)
        worker.signals.finished.connect(self._task_finished)
        self._workers[task_id] = worker
        self._success_label = success_label
        self.create_button.setEnabled(False)
        self.verify_button.setEnabled(False)
        self.restore_button.setEnabled(False)
        self.status.setText(progress_text)
        self._thread_pool.start(worker)

    def _task_finished(self, task_id, result, error):
        self._workers.pop(task_id, None)
        if task_id != self._task_id:
            return
        self._busy = False
        self.create_button.setEnabled(self.is_admin)
        self.verify_button.setEnabled(self.is_admin)
        self.restore_button.setEnabled(self.is_admin)
        if error is not None:
            self.status.setText("Backup operation failed.")
            QMessageBox.warning(self, "Backup operation failed", str(error))
            return
        self.status.setText(
            f"{self._success_label} — schema {result.schema_version}: {result.path}"
        )


class LegacyClassReviewPage(QWidget):
    """Review legacy class labels and explicitly map a selected group to a session."""

    def __init__(self, username: str, role: str):
        super().__init__()
        self.username = username
        self.is_admin = role == "admin"
        self._request_id = 0
        self._preview_id = 0
        self._mapping_id = 0
        self._mapping_busy = False
        self._workers = {}
        self._thread_pool = QThreadPool.globalInstance()
        self._classes = db.list_class_sections()
        self._sessions = db.list_academic_sessions()
        layout = QVBoxLayout(self)
        heading = QLabel("Legacy class review")
        heading.setObjectName("heading")
        layout.addWidget(heading)
        description = wrapped_label(
            "Review existing student class text. Mapping is explicit: choose one exact legacy value, "
            "a destination class and session, and an enrollment date. Original student text is retained."
        )
        layout.addWidget(description)
        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Legacy Class Name", "Student Count"])
        configure_table_headers(self.table)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table)
        mapping = QFormLayout()
        self.legacy_value = QComboBox()
        self.destination = QComboBox()
        for item in self._classes:
            stage = db.EDUCATIONAL_STAGES.get(item["educational_stage"], "Stage not set")
            self.destination.addItem(
                f"{item['class_name']} {item['section']} — {stage}".strip(), int(item["id"])
            )
        self.session = QComboBox()
        for item in self._sessions:
            self.session.addItem(item["name"], int(item["id"]))
        self.enrolled_on = QDateEdit()
        self.enrolled_on.setCalendarPopup(True)
        self.enrolled_on.setDisplayFormat("yyyy-MM-dd")
        self.session.currentIndexChanged.connect(self._set_date_range)
        self.legacy_value.currentIndexChanged.connect(self.refresh_preview)
        self.session.currentIndexChanged.connect(self.refresh_preview)
        self.destination.currentIndexChanged.connect(self._update_map_button)
        mapping.addRow("Legacy value to map", self.legacy_value)
        mapping.addRow("Destination class and section", self.destination)
        mapping.addRow("Academic session", self.session)
        mapping.addRow("Enrollment date", self.enrolled_on)
        layout.addLayout(mapping)
        self.preview = QLabel("Choose a legacy class value and session to preview the group.")
        layout.addWidget(self.preview)
        footer = QHBoxLayout()
        self.status = QLabel()
        footer.addWidget(self.status)
        footer.addStretch()
        self.map_button = QPushButton("Map active students to enrollments…")
        self.map_button.clicked.connect(self.map_selected_group)
        self.map_button.setEnabled(self.is_admin)
        footer.addWidget(self.map_button)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        footer.addWidget(self.refresh_button)
        layout.addLayout(footer)
        self._set_date_range()
        self.refresh()

    def refresh(self):
        selected_destination = self.destination.currentData()
        self._classes = db.list_class_sections()
        self.destination.blockSignals(True)
        self.destination.clear()
        for item in self._classes:
            stage = db.EDUCATIONAL_STAGES.get(item["educational_stage"], "Stage not set")
            self.destination.addItem(
                f"{item['class_name']} {item['section']} — {stage}".strip(), int(item["id"])
            )
        index = self.destination.findData(selected_destination)
        self.destination.setCurrentIndex(max(0, index))
        self.destination.blockSignals(False)
        self._request_id += 1
        request_id = self._request_id
        worker = BackgroundTask(request_id, db.list_legacy_class_summary)
        worker.signals.finished.connect(self._summary_loaded)
        self._workers[("summary", request_id)] = worker
        self.status.setText("Loading legacy class values…")
        self.table.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self._thread_pool.start(worker)

    def _summary_loaded(self, request_id, rows, error):
        self._workers.pop(("summary", request_id), None)
        if request_id != self._request_id:
            return
        self.table.setEnabled(True)
        self.refresh_button.setEnabled(True)
        if error is not None:
            self.status.setText("Could not load legacy class values")
            QMessageBox.warning(self, "Could not load legacy class values", str(error))
            return
        selected = self.legacy_value.currentData()
        self.legacy_value.blockSignals(True)
        self.legacy_value.clear()
        for row in rows:
            value = row["class_name"]
            count = int(row["student_count"])
            self.legacy_value.addItem(f"{value or '(blank)'} — {count} student(s)", value)
        index = self.legacy_value.findData(selected)
        if index >= 0:
            self.legacy_value.setCurrentIndex(index)
        self.legacy_value.blockSignals(False)
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            class_name = row["class_name"] or "(blank)"
            self.table.setItem(row_index, 0, QTableWidgetItem(class_name))
            self.table.setItem(row_index, 1, QTableWidgetItem(str(row["student_count"])))
        self.status.setText(f"{len(rows)} distinct values")
        self.refresh_preview()

    def _set_date_range(self):
        session_id = self.session.currentData()
        session = next((item for item in self._sessions if int(item["id"]) == session_id), None)
        has_session = session is not None
        self.enrolled_on.setEnabled(has_session)
        if session:
            start = QDate.fromString(session["starts_on"], "yyyy-MM-dd")
            end = QDate.fromString(session["ends_on"], "yyyy-MM-dd")
            self.enrolled_on.setMinimumDate(start)
            self.enrolled_on.setMaximumDate(end)
            self.enrolled_on.setDate(start)
        self._update_map_button()

    def refresh_preview(self, *_args):
        self._preview_id += 1
        preview_id = self._preview_id
        self._preview_result = {}
        class_name = self.legacy_value.currentData()
        session_id = self.session.currentData()
        if class_name is None or session_id is None:
            self.preview.setText("Choose a legacy class value and session to preview the group.")
            self._update_map_button()
            return
        worker = BackgroundTask(
            preview_id,
            lambda: db.preview_legacy_class_mapping(class_name, int(session_id)),
        )
        worker.signals.finished.connect(self._preview_loaded)
        self._workers[("preview", preview_id)] = worker
        self.preview.setText("Checking eligible students…")
        self._update_map_button()
        self._thread_pool.start(worker)

    def _preview_loaded(self, preview_id, result, error):
        self._workers.pop(("preview", preview_id), None)
        if preview_id != self._preview_id:
            return
        if error:
            self.preview.setText("Could not preview this legacy class group.")
            QMessageBox.warning(self, "Could not preview mapping", str(error))
            self._update_map_button()
            return
        self._preview_result = result
        self.preview.setText(
            f"Group: {result['total']} total; {result['active']} active; "
            f"{result['inactive']} inactive excluded; {result['already_enrolled']} already "
            f"enrolled in this session; {result['eligible']} eligible to map."
        )
        self._update_map_button()

    def _update_map_button(self):
        result = getattr(self, "_preview_result", {})
        destination = next(
            (item for item in self._classes if int(item["id"]) == self.destination.currentData()),
            None,
        )
        has_selection = (
            self.is_admin and self.legacy_value.currentData() is not None
            and destination is not None and bool(destination["educational_stage"])
            and self.session.currentData() is not None
        )
        self.map_button.setEnabled(
            has_selection and not self._mapping_busy and result.get("eligible", 0) > 0
            and result.get("already_enrolled", 0) == 0
        )

    def map_selected_group(self):
        if not self.map_button.isEnabled():
            return
        result = self._preview_result
        class_name = self.legacy_value.currentData()
        session_id = int(self.session.currentData())
        destination_id = int(self.destination.currentData())
        session = next(item for item in self._sessions if int(item["id"]) == session_id)
        destination = next(item for item in self._classes if int(item["id"]) == destination_id)
        legacy_label = class_name or "(blank)"
        target_label = (
            f"{destination['class_name']} {destination['section']} — "
            f"{db.EDUCATIONAL_STAGES.get(destination['educational_stage'], 'Stage not set')}"
        ).strip()
        answer = QMessageBox.question(
            self,
            "Confirm legacy class mapping",
            f"Create {result['eligible']} enrollment(s) for active students whose existing "
            f"class value is exactly ‘{legacy_label}’?\n\n"
            f"Destination: {target_label}\nSession: {session['name']}\n"
            f"Enrollment date: {self.enrolled_on.date().toString('yyyy-MM-dd')}\n\n"
            "Inactive students are excluded. The original class text will be preserved. "
            "This action cannot be undone as a group.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._mapping_id += 1
        mapping_id = self._mapping_id
        args = (
            class_name, destination_id, session_id,
            self.enrolled_on.date().toString("yyyy-MM-dd"), self.username,
        )
        worker = BackgroundTask(
            mapping_id, lambda: db.map_legacy_class_to_enrollments(*args)
        )
        worker.signals.finished.connect(self._mapping_finished)
        self._workers[("mapping", mapping_id)] = worker
        self._mapping_busy = True
        self.status.setText("Creating enrollments…")
        self._update_map_button()
        self._thread_pool.start(worker)

    def _mapping_finished(self, mapping_id, count, error):
        self._workers.pop(("mapping", mapping_id), None)
        if mapping_id != self._mapping_id:
            return
        self._mapping_busy = False
        if error:
            self.status.setText("Mapping failed; no partial group was committed")
            QMessageBox.warning(self, "Could not map legacy class", str(error))
            self.refresh_preview()
            return
        self.status.setText(f"Created {count} enrollment(s)")
        QMessageBox.information(self, "Mapping complete", f"Created {count} enrollment(s).")
        self._preview_result = {}
        self.refresh()
        self.refresh_preview()

class MainWindow(QMainWindow):
    def __init__(self, user):
        super().__init__()
        self.user = user
        self.setWindowTitle("SmartSchool Manager — Rebuild")
        self.resize(1280, 780)
        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        nav = QVBoxLayout(sidebar)
        brand = QLabel("SMARTSCHOOL\nMANAGER")
        brand.setStyleSheet("color:white;font-size:15pt;font-weight:800;padding:18px 10px;")
        nav.addWidget(brand)
        self.stack = QStackedWidget()
        self.pages = {}
        self._page_factories = {}
        self._nav_destinations = {}
        dash = Dashboard(self.user["role"])
        self.pages["Dashboard"] = dash
        dash.navigation_requested.connect(self.show_page)
        self.stack.addWidget(dash)
        nav_tabs = QTabWidget()
        nav_tabs.setObjectName("navTabs")
        self._nav_tabs = nav_tabs
        self._nav_tab_for_page = {}
        if self.user["role"] == "teacher":
            tab_specs = [("Main", ["Dashboard", "Grade Entry", "Attendance"])]
        else:
            tab_specs = [
                ("Main", ["Dashboard", *db.MODULES.keys(), "Enrollments", "Grade Entry", "Attendance", "Timetable", "Fee Invoices", "Transaction History"]),
                ("Settings", ["School Profile", "Academic Calendar", "Legacy Class Review", "Class Stages", "Class Subjects", "Grade Bands", "Assessment Setup", "Fee Setup", "User Accounts", "Data Backup", "Database Diagnostics"]),
            ]
        for tab_index, (tab_name, page_names) in enumerate(tab_specs):
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(0, 8, 0, 0)
            page_layout.setSpacing(3)
            for name in page_names:
                button = QPushButton(name)
                button.setObjectName("nav")
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.clicked.connect(lambda checked=False, n=name: self.show_page(n))
                page_layout.addWidget(button)
                self._nav_tab_for_page[name] = tab_index
                if name != "Dashboard":
                    self._page_factories[name] = lambda page_name=name: self._create_page(page_name)
            page_layout.addStretch()
            scroll = QScrollArea()
            scroll.setObjectName("navScroll")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(page)
            nav_tabs.addTab(scroll, tab_name)
        nav.addWidget(nav_tabs, 1)
        nav.addStretch()
        who = QLabel(f"Signed in: {self.user['username']}\nRole: {self.user['role']}")
        who.setWordWrap(True)
        who.setStyleSheet("color:#d9e6f2;background:transparent;padding:12px;")
        nav.addWidget(who)
        root_layout.addWidget(sidebar)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        top = QHBoxLayout()
        top.addWidget(QLabel("SMARTSCHOOL MANAGER  /  OFFLINE"))
        top.addStretch()
        logout = QPushButton("Sign out")
        logout.clicked.connect(self.close)
        top.addWidget(logout)
        content_layout.addLayout(top)
        page_scroll = QScrollArea()
        page_scroll.setObjectName("pageScroll")
        page_scroll.setWidgetResizable(True)
        page_scroll.setFrameShape(QFrame.Shape.NoFrame)
        page_scroll.setWidget(self.stack)
        content_layout.addWidget(page_scroll)
        root_layout.addWidget(content)
        self.setCentralWidget(root)

    def show_page(self, name):
        if self.user["role"] == "teacher" and name not in {"Dashboard", "Grade Entry", "Attendance"}:
            return
        tab_index = self._nav_tab_for_page.get(name)
        if tab_index is not None:
            self._nav_tabs.setCurrentIndex(tab_index)
        page = self.pages.get(name)
        created_now = page is None
        if page is None:
            page = self._page_factories[name]()
            self.pages[name] = page
            self.stack.addWidget(page)
        self.stack.setCurrentWidget(page)
        if name == "Dashboard" and not created_now:
            page.refresh()
        elif name == "Academic Calendar" and not created_now:
            page.refresh_sessions()
        elif name == "Enrollments" and not created_now:
            page.refresh()
        elif name == "Legacy Class Review" and not created_now:
            page.refresh()
        elif name == "Class Stages" and not created_now:
            page.refresh()
        elif name == "Class Subjects" and not created_now:
            page.refresh()
        elif name == "Grade Bands" and not created_now:
            page.refresh()
        elif name == "Assessment Setup" and not created_now:
            page.refresh()
        elif name == "Grade Entry" and not created_now:
            page.refresh()
        elif name == "Attendance" and not created_now:
            page.refresh()
        elif name == "Timetable" and not created_now:
            page.refresh()
        elif name == "School Profile" and not created_now:
            page.refresh()
        elif name == "Fee Setup" and not created_now:
            page.refresh()
        elif name == "Fee Invoices" and not created_now:
            page.refresh()
        elif name == "Transaction History" and not created_now:
            page.refresh()
        elif name == "User Accounts" and not created_now:
            page.refresh()

    def _create_page(self, name):
        if name == "School Profile":
            return SchoolProfilePage(self.user["username"], self.user["role"])
        if name == "User Accounts":
            return UserAccountsPage(self.user["username"], self.user["role"])
        if name == "Database Diagnostics":
            return DiagnosticsPage(self.user["role"])
        if name == "Fee Setup":
            return FeeSetupPage(self.user["username"], self.user["role"])
        if name == "Fee Invoices":
            return FeeInvoicePage(self.user["username"], self.user["role"])
        if name == "Transaction History":
            return FeeReconciliationPage(self.user["username"], self.user["role"])
        if name in db.MODULES:
            return ModulePage(name, self.user["username"])
        if name == "Academic Calendar":
            return AcademicCalendarPage(self.user["username"], self.user["role"])
        if name == "Enrollments":
            return EnrollmentPage(self.user["username"], self.user["role"])
        if name == "Legacy Class Review":
            return LegacyClassReviewPage(self.user["username"], self.user["role"])
        if name == "Class Stages":
            return ClassStagePage(self.user["username"], self.user["role"])
        if name == "Class Subjects":
            return ClassSubjectAssignmentsPage(self.user["username"], self.user["role"])
        if name == "Grade Bands":
            return GradeBandSetupPage(self.user["username"], self.user["role"])
        if name == "Assessment Setup":
            return AssessmentSetupPage(self.user["username"], self.user["role"])
        if name == "Grade Entry":
            return GradeEntryPage(self.user["username"], self.user["role"])
        if name == "Data Backup":
            return DataBackupPage(self.user["role"])
        if name == "Attendance":
            return AttendancePage(self.user["username"], self.user["role"])
        if name == "Timetable":
            return TimetablePage(self.user["username"], self.user["role"])
        raise KeyError(f"Unknown page: {name}")

def run_app():
    import logging
    from logging.handlers import RotatingFileHandler

    db.APP_DIR.mkdir(parents=True, exist_ok=True)
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        handler = RotatingFileHandler(db.APP_DIR / "smartschool.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)
    startup_dialog = QDialog()
    startup_dialog.setWindowTitle("Starting SmartSchool Manager")
    startup_dialog.setModal(True)
    startup_layout = QVBoxLayout(startup_dialog)
    startup_layout.addWidget(QLabel("Preparing the local school database…"))
    progress = QProgressBar()
    progress.setRange(0, 0)
    progress.setTextVisible(False)
    startup_layout.addWidget(progress)

    def prepare_database():
        db.validate_configured_data_dir()
        db.initialize()
        return db.user_count()

    startup_loop = QEventLoop()
    startup_result = {}
    startup_workers = {}
    worker = BackgroundTask(1, prepare_database)
    worker.signals.finished.connect(
        lambda task_id, result, error: (
            startup_result.update(result=result, error=error),
            startup_workers.pop(task_id, None),
            startup_loop.quit(),
        )
    )
    startup_workers[1] = worker
    startup_dialog.show()
    QThreadPool.globalInstance().start(worker)
    startup_loop.exec()
    startup_dialog.close()
    if startup_result.get("error") is not None:
        QMessageBox.critical(None, "Database startup failed", str(startup_result["error"]))
        raise SystemExit(1)

    if startup_result["result"] == 0:
        setup = SetupDialog()
        if setup.exec() != QDialog.DialogCode.Accepted:
            raise SystemExit(0)
        try:
            db.create_user(setup.credentials[0], hash_password(setup.credentials[1]), "admin")
        except Exception as exc:
            QMessageBox.critical(None, "Setup failed", str(exc))
            raise SystemExit(1)
    login = LoginDialog()
    if login.exec() != QDialog.DialogCode.Accepted:
        raise SystemExit(0)
    window = MainWindow(login.user)
    window.setStyleSheet(STYLE)
    return window
