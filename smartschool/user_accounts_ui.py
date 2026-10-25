"""Administrator UI for secure teacher-login linkage and account maintenance."""

from __future__ import annotations

from PyQt6.QtCore import QThreadPool
from PyQt6.QtWidgets import (
    QComboBox,
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
from .ui_helpers import wrapped_label
from . import database as db
from .security import hash_password
from .workers import BackgroundTask


class AccountSetupDialog(QDialog):
    def __init__(self, teachers, accounts, username=None, parent=None):
        super().__init__(parent)
        self._creating = username is None
        self.setWindowTitle("Create teacher login" if self._creating else "Link teacher login")
        form = QFormLayout(self)
        self.username = QLineEdit(username or "")
        if not self._creating:
            self.username.setReadOnly(True)
        self.teacher = QComboBox()
        linked_elsewhere = {int(row["teacher_id"]) for row in accounts if row["teacher_id"] is not None}
        current_teacher_id = next(
            (int(row["teacher_id"]) for row in accounts
             if username and row["username"].casefold() == username.casefold() and row["teacher_id"] is not None),
            None,
        )
        for item in teachers:
            if int(item["id"]) not in linked_elsewhere or int(item["id"]) == current_teacher_id:
                self.teacher.addItem(
                    f"{item['staff_no']} — {item['last_name']}, {item['first_name']}", int(item["id"])
                )
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm = QLineEdit()
        self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
        if self._creating:
            form.addRow("Username", self.username)
        form.addRow("Active teacher record", self.teacher)
        if self._creating:
            form.addRow("Password", self.password)
            form.addRow("Confirm password", self.confirm)
            form.addRow(QLabel("Use at least 10 characters. The teacher will see only their assigned grade-entry subjects."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _validate(self):
        if self.teacher.currentData() is None:
            QMessageBox.information(self, "Choose a teacher", "Select an active teacher record to link.")
            return
        if self._creating:
            username = self.username.text().strip()
            password = self.password.text()
            if len(username) < 3:
                QMessageBox.information(self, "Check username", "Use at least 3 characters for the username.")
                return
            if len(password) < 10:
                QMessageBox.information(self, "Check password", "Use a password of at least 10 characters.")
                return
            if password != self.confirm.text():
                QMessageBox.information(self, "Check password", "The passwords do not match.")
                return
        self.accept()

    def values(self):
        return self.username.text().strip(), int(self.teacher.currentData()), self.password.text()


class PasswordResetDialog(QDialog):
    def __init__(self, username, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Reset password — {username}")
        form = QFormLayout(self)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm = QLineEdit()
        self.confirm.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("New password", self.password)
        form.addRow("Confirm password", self.confirm)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _validate(self):
        if len(self.password.text()) < 10:
            QMessageBox.information(self, "Check password", "Use a password of at least 10 characters.")
            return
        if self.password.text() != self.confirm.text():
            QMessageBox.information(self, "Check password", "The passwords do not match.")
            return
        self.accept()

    def value(self):
        return self.password.text()


class UserAccountsPage(QWidget):
    def __init__(self, username: str, role: str):
        super().__init__()
        self.username = username
        self.is_admin = role == "admin"
        self._accounts = []
        self._teachers = []
        self._loading = False
        self._writing = False
        self._load_id = 0
        self._write_id = 0
        self._workers = {}
        self._thread_pool = QThreadPool.globalInstance()
        layout = QVBoxLayout(self)
        heading = QLabel("User accounts")
        heading.setObjectName("heading")
        layout.addWidget(heading)
        layout.addWidget(wrapped_label(
            "Link each teacher login to one active staff record. Teachers can enter draft scores only for "
            "class-subject assignments assigned to that staff record; approval and corrections stay with administrators."
        ))
        actions = QHBoxLayout()
        self.create_button = QPushButton("Create teacher login…")
        self.link_button = QPushButton("Link selected teacher login…")
        self.reset_button = QPushButton("Reset password…")
        self.status_button = QPushButton("Deactivate account")
        self.refresh_button = QPushButton("Refresh")
        for button in (self.create_button, self.link_button, self.reset_button, self.status_button, self.refresh_button):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Username", "Account Role", "Linked Staff Member", "Account Status", "Account ID"])
        configure_table_headers(self.table)
        self.table.setColumnHidden(4, True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table)
        self.status = QLabel("Loading user accounts…")
        layout.addWidget(self.status)
        self.create_button.clicked.connect(self.create_teacher_login)
        self.link_button.clicked.connect(self.link_selected_teacher)
        self.reset_button.clicked.connect(self.reset_selected_password)
        self.status_button.clicked.connect(self.toggle_selected_status)
        self.refresh_button.clicked.connect(self.refresh)
        self.table.itemSelectionChanged.connect(self._update_actions)
        self._update_actions()
        self.refresh()

    def refresh(self):
        self._load_id += 1
        load_id = self._load_id
        self._loading = True
        self._update_actions()
        worker = BackgroundTask(load_id, lambda: {
            "accounts": db.list_user_accounts(),
            "teachers": db.list_active_teachers(),
        })
        worker.signals.finished.connect(self._loaded)
        self._workers[("load", load_id)] = worker
        self.status.setText("Loading user accounts…")
        self._thread_pool.start(worker)

    def _loaded(self, load_id, result, error):
        self._workers.pop(("load", load_id), None)
        if load_id != self._load_id:
            return
        self._loading = False
        if error:
            self.status.setText("Could not load user accounts")
            QMessageBox.warning(self, "Could not load user accounts", str(error))
            self._update_actions()
            return
        self._accounts = result["accounts"]
        self._teachers = result["teachers"]
        self.table.setRowCount(len(self._accounts))
        for row_index, account in enumerate(self._accounts):
            teacher_name = ""
            if account["teacher_id"] is not None:
                teacher_name = f"{account['staff_no']} — {account['teacher_last_name']}, {account['teacher_first_name']}"
            values = (
                account["username"], account["role"].title(), teacher_name,
                "Active" if account["is_active"] else "Inactive", account["id"],
            )
            for column, value in enumerate(values):
                self.table.setItem(row_index, column, QTableWidgetItem(str(value)))
        self.status.setText(f"{len(self._accounts)} account(s). Teacher access is limited to assigned grade-entry subjects.")
        self._update_actions()

    def _selected(self):
        row = self.table.currentRow()
        return self._accounts[row] if 0 <= row < len(self._accounts) else None

    def _update_actions(self):
        ready = self.is_admin and not self._loading and not self._writing
        selected = self._selected()
        self.create_button.setEnabled(ready and any(
            int(item["id"]) not in {int(a["teacher_id"]) for a in self._accounts if a["teacher_id"] is not None}
            for item in self._teachers
        ))
        linked_to_other_accounts = {
            int(row["teacher_id"])
            for row in self._accounts
            if row["teacher_id"] is not None and (selected is None or row["id"] != selected["id"])
        }
        self.link_button.setEnabled(
            ready and selected is not None and selected["role"] == "teacher"
            and any(int(item["id"]) not in linked_to_other_accounts for item in self._teachers)
        )
        self.reset_button.setEnabled(ready and selected is not None)
        self.status_button.setEnabled(ready and selected is not None)
        self.refresh_button.setEnabled(not self._loading and not self._writing)
        self.status_button.setText(
            "Deactivate account" if selected is None or selected["is_active"] else "Activate account"
        )

    def create_teacher_login(self):
        dialog = AccountSetupDialog(self._teachers, self._accounts, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        username, teacher_id, password = dialog.values()
        self._start_write(
            lambda: db.create_teacher_user(username, hash_password(password), teacher_id, self.username),
            "Teacher login created",
        )

    def link_selected_teacher(self):
        account = self._selected()
        if account is None or account["role"] != "teacher":
            return
        dialog = AccountSetupDialog(self._teachers, self._accounts, account["username"], self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        username, teacher_id, _password = dialog.values()
        self._start_write(
            lambda: db.link_teacher_user(username, teacher_id, self.username),
            "Teacher login linked",
        )

    def reset_selected_password(self):
        account = self._selected()
        if account is None:
            return
        dialog = PasswordResetDialog(account["username"], self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        password = dialog.value()
        self._start_write(
            lambda: db.set_user_password(account["username"], hash_password(password), self.username),
            "Password reset",
        )

    def toggle_selected_status(self):
        account = self._selected()
        if account is None:
            return
        active = not bool(account["is_active"])
        action = "Activate" if active else "Deactivate"
        answer = QMessageBox.question(
            self, f"{action} account", f"{action} login account {account['username']}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._start_write(
                lambda: db.set_user_active(account["username"], active, self.username),
                f"Account {action.lower()}d",
            )

    def _start_write(self, operation, message):
        if self._writing:
            return
        self._writing = True
        self._write_id += 1
        write_id = self._write_id
        worker = BackgroundTask(write_id, operation)
        worker.signals.finished.connect(self._write_finished)
        self._workers[("write", write_id)] = worker
        self._write_message = message
        self._update_actions()
        self.status.setText("Saving account changes…")
        self._thread_pool.start(worker)

    def _write_finished(self, write_id, _result, error):
        self._workers.pop(("write", write_id), None)
        if write_id != self._write_id:
            return
        self._writing = False
        if error:
            self.status.setText("Account change failed")
            QMessageBox.warning(self, "Could not save account changes", str(error))
            self._update_actions()
            return
        self.status.setText(self._write_message)
        self.refresh()
