"""First-run activation window: shows this computer's machine code and accepts the activation key."""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
                             QVBoxLayout)

from . import licensing


class ActivationDialog(QDialog):
    """Blocks until a valid key is saved. Closing the window means the program does not start."""

    def __init__(self, state: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Activate SmartSchool Manager")
        self.setModal(True)
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        title = QLabel("Activate SmartSchool Manager")
        title.setObjectName("heading")
        layout.addWidget(title)
        reason = (state or {}).get("message") or "Enter your activation key to start using the program."
        self.reason = QLabel(reason)
        self.reason.setWordWrap(True)
        layout.addWidget(self.reason)

        layout.addWidget(QLabel("1. Send this machine code to your SmartSchool provider:"))
        row = QHBoxLayout()
        self.machine = QLineEdit(licensing.machine_code())
        self.machine.setReadOnly(True)
        self.machine.setAccessibleName("Machine code")
        copy = QPushButton("Copy")
        copy.clicked.connect(self.copy_machine_code)
        row.addWidget(self.machine, 1)
        row.addWidget(copy)
        layout.addLayout(row)

        layout.addWidget(QLabel("2. Paste the activation key you receive:"))
        self.key = QPlainTextEdit()
        self.key.setAccessibleName("Activation key")
        self.key.setPlaceholderText("SSA1-XXXXX-XXXXX-…")
        self.key.setFixedHeight(90)
        layout.addWidget(self.key)
        self.message = QLabel("")
        self.message.setObjectName("statusError")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        quit_button = QPushButton("Exit")
        quit_button.clicked.connect(self.reject)
        self.activate_button = QPushButton("Activate")
        self.activate_button.setObjectName("primary")
        self.activate_button.setDefault(True)
        self.activate_button.clicked.connect(self.activate)
        buttons.addWidget(quit_button)
        buttons.addWidget(self.activate_button)
        layout.addLayout(buttons)
        self.result_state = None

    def copy_machine_code(self):
        QGuiApplication.clipboard().setText(self.machine.text())
        self.message.setText("")

    def activate(self):
        try:
            self.result_state = licensing.activate(self.key.toPlainText())
        except ValueError as exc:
            self.message.setText(str(exc))
            return
        except OSError:
            self.message.setText("The key is valid but could not be saved. Check that the data folder is writable.")
            return
        QMessageBox.information(self, "Activated", self.result_state["message"])
        self.accept()


def ensure_activated(parent=None) -> bool:
    """Return True when the program may start; shows the activation window when needed."""
    state = licensing.status()
    if licensing.is_usable(state):
        return True
    return ActivationDialog(state, parent).exec() == QDialog.DialogCode.Accepted
