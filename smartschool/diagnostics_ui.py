"""Administrator-only asynchronous database health diagnostics screen."""

from __future__ import annotations

from PyQt6.QtCore import QThreadPool
from PyQt6.QtWidgets import QLabel, QMessageBox, QPushButton, QPlainTextEdit, QVBoxLayout, QWidget

from .diagnostics import collect_diagnostics
from .ui_helpers import wrapped_label
from .workers import BackgroundTask


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{amount:.1f} TB"


class DiagnosticsPage(QWidget):
    def __init__(self, role: str):
        super().__init__()
        self.is_admin = role == "admin"
        self._pool = QThreadPool.globalInstance()
        self._workers = {}
        self._task_id = 0

        layout = QVBoxLayout(self)
        heading = QLabel("Database diagnostics")
        heading.setObjectName("heading")
        layout.addWidget(heading)
        layout.addWidget(wrapped_label(
            "Run read-only checks on the local school database. Integrity checking can take a little time "
            "for a large file, but it runs in the background so the window remains responsive."
        ))
        self.run_button = QPushButton("Run diagnostics")
        self.run_button.setObjectName("primary")
        self.run_button.setEnabled(self.is_admin)
        self.run_button.clicked.connect(self.run_diagnostics)
        layout.addWidget(self.run_button)
        self.status = QLabel("Diagnostics have not been run yet.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("Run diagnostics to view database health, schema, disk space, and record counts.")
        layout.addWidget(self.output, 1)
        if not self.is_admin:
            self.status.setText("Database diagnostics are restricted to administrators.")

    def run_diagnostics(self):
        if not self.is_admin or not self.run_button.isEnabled():
            return
        self._task_id += 1
        task_id = self._task_id
        self.run_button.setEnabled(False)
        self.status.setText("Checking the local database in the background…")
        worker = BackgroundTask(task_id, collect_diagnostics)
        worker.signals.finished.connect(self._finished)
        self._workers[task_id] = worker
        self._pool.start(worker)

    def _finished(self, task_id, result, error):
        self._workers.pop(task_id, None)
        if task_id != self._task_id:
            return
        self.run_button.setEnabled(self.is_admin)
        if error is not None:
            self.status.setText("Diagnostics could not be completed.")
            QMessageBox.warning(self, "Diagnostics failed", str(error))
            return
        counts = "\n".join(f"  {label}: {count:,}" for label, count in result["record_counts"].items())
        lines = [
            f"Overall status: {'HEALTHY' if result['overall_ok'] else 'REVIEW REQUIRED'}",
            f"SQLite quick check: {'; '.join(result['quick_check'])}",
            f"Foreign-key violations: {result['foreign_key_violations']}",
            f"Schema version: {result['schema_version']} / supported {result['supported_schema_version']}",
            f"Migration ledger complete: {'Yes' if result['migration_ledger_ok'] else 'No'}",
            f"Database size: {_format_bytes(result['database_size_bytes'])}",
            f"Free space on database drive: {_format_bytes(result['free_disk_bytes'])}",
            f"Database file: {result['database_path']}",
            "",
            "Record counts (informational):",
            counts or "  No supported data tables found.",
        ]
        self.output.setPlainText("\n".join(lines))
        self.status.setText("Diagnostics complete. No database records were changed.")
