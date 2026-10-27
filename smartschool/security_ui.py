"""Audit log viewer (administrators) and change-own-password dialog (everyone)."""

from __future__ import annotations

import csv
from PyQt6.QtCore import QDate, QThreadPool
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import account_security
from . import database as db
from .table_helpers import configure_table_headers
from .ui_helpers import wrapped_label
from .workers import BackgroundTask


class ChangePasswordDialog(QDialog):
    def __init__(self, username: str, parent=None):
        super().__init__(parent)
        self.username = username
        self.setWindowTitle("Change my password")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.addWidget(wrapped_label(f"Signed in as {username}. Choose a new password of at least 10 characters."))
        form = QFormLayout()
        self.current = QLineEdit()
        self.new = QLineEdit()
        self.confirm = QLineEdit()
        for edit, label in ((self.current, "Current password"), (self.new, "New password"), (self.confirm, "Confirm new password")):
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            form.addRow(label, edit)
        layout.addLayout(form)
        self.show_passwords = QCheckBox("Show passwords")
        self.show_passwords.toggled.connect(self._toggle_visibility)
        layout.addWidget(self.show_passwords)
        self.message = QLabel("")
        self.message.setObjectName("statusError")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.submit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.current.setFocus()

    def _toggle_visibility(self, visible):
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        for edit in (self.current, self.new, self.confirm):
            edit.setEchoMode(mode)

    def submit(self):
        try:
            account_security.change_own_password(self.username, self.current.text(), self.new.text(), self.confirm.text())
        except ValueError as exc:
            self.message.setText(str(exc))
            return
        except Exception as exc:
            self.message.setText(f"The password could not be changed: {exc}")
            return
        QMessageBox.information(self, "Password changed", "Your password was changed. Use it the next time you sign in.")
        self.accept()


class AuditLogPage(QWidget):
    def __init__(self, username: str, role: str):
        super().__init__()
        self.username = username
        self.is_admin = role == "admin"
        self._task_id = 0
        self._workers = {}
        self._pool = QThreadPool.globalInstance()
        self._rows = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        title = QLabel("Audit log")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(wrapped_label(
            "A read-only record of who changed what. Entries cannot be edited or deleted here."))
        filters = QHBoxLayout()
        self.starts = QDateEdit(QDate.currentDate().addDays(-30))
        self.ends = QDateEdit(QDate.currentDate())
        for edit in (self.starts, self.ends):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
        self.action = QComboBox()
        self.action.addItem("All actions", "")
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search user, record type or record number")
        self.search.setClearButtonEnabled(True)
        self.search.returnPressed.connect(self.refresh)
        self.show_button = QPushButton("Search")
        self.show_button.setObjectName("primary")
        self.show_button.clicked.connect(self.refresh)
        self.export_button = QPushButton("Export CSV")
        self.export_button.clicked.connect(self.export_csv)
        for widget in (QLabel("From"), self.starts, QLabel("To"), self.ends, self.action):
            filters.addWidget(widget)
        filters.addWidget(self.search, 1)
        filters.addWidget(self.show_button)
        filters.addWidget(self.export_button)
        layout.addLayout(filters)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Time", "User", "Action", "Record type", "Record no."])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        configure_table_headers(self.table)
        layout.addWidget(self.table, 1)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        layout.addWidget(self.status)
        if not self.is_admin:
            self.show_button.setEnabled(False)
            self.export_button.setEnabled(False)
            self.status.setText("Only an administrator can view the audit log.")
        else:
            self.refresh()

    def _filters(self):
        return {
            "starts_on": self.starts.date().toString("yyyy-MM-dd"), "ends_on": self.ends.date().toString("yyyy-MM-dd"),
            "action": self.action.currentData() or "", "search": self.search.text(),
        }

    def refresh(self, limit=500):
        if not self.is_admin:
            return
        filters = self._filters()
        self._task_id += 1
        task_id = self._task_id
        self.status.setText("Loading…")
        worker = BackgroundTask(task_id, lambda: db.load_audit_log(self.username, limit=limit, **filters))
        worker.signals.finished.connect(self._loaded)
        self._workers[task_id] = worker
        self._pool.start(worker)

    def _loaded(self, task_id, result, error):
        self._workers.pop(task_id, None)
        if task_id != self._task_id:
            return
        if error is not None:
            self.status.setText("Could not load the audit log")
            QMessageBox.warning(self, "Audit log", str(error))
            return
        try:
            current = self.action.currentData()
            self.action.blockSignals(True)
            self.action.clear()
            self.action.addItem("All actions", "")
            for name in result["actions"]:
                self.action.addItem(name, name)
            self.action.setCurrentIndex(max(0, self.action.findData(current)))
            self.action.blockSignals(False)
            self._rows = result["rows"]
            self.table.setRowCount(len(self._rows))
            for r, row in enumerate(self._rows):
                for c, key in enumerate(("created_at", "username", "action", "entity", "entity_id")):
                    self.table.setItem(r, c, QTableWidgetItem(str(row[key])))
            shown = len(self._rows)
            self.status.setText(
                f"Showing {shown} of {result['total']} entries." + (" Narrow the dates or search to see older ones." if result["total"] > shown else "")
            )
        except Exception as exc:
            self.status.setText(f"Could not display the audit log: {exc}")

    def export_csv(self):
        if not self._rows:
            QMessageBox.information(self, "Nothing to export", "There are no entries to export for these filters.")
            return
        path, _selected = QFileDialog.getSaveFileName(self, "Export audit log", "audit-log.csv", "CSV files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as output:
                writer = csv.writer(output)
                writer.writerow(["Time", "User", "Action", "Record type", "Record no."])
                for row in self._rows:
                    writer.writerow([
                        ("'" + str(v) if str(v).startswith(("=", "+", "-", "@")) else v)
                        for v in (row["created_at"], row["username"], row["action"], row["entity"], row["entity_id"])
                    ])
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        QMessageBox.information(self, "Export complete", f"{len(self._rows)} entries exported.")

