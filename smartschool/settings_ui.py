"""Administrator-managed school identity used on printed result PDFs."""

from __future__ import annotations

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QThreadPool
from PyQt6.QtGui import QImageReader
from PyQt6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from . import database as db
from .ui_helpers import wrapped_label
from .workers import BackgroundTask


class SchoolProfilePage(QWidget):
    FIELDS = (
        ("school_name", "School name", 120),
        ("school_type", "School type", 16),
        ("address", "Address", 200),
        ("phone", "Phone", 60),
        ("email", "Email", 120),
        ("head_name", "Head of school name", 120),
        ("head_title", "Head of school title", 80),
        ("class_teacher_name", "Class teacher name", 120),
        ("class_teacher_title", "Class teacher title", 80),
    )

    def __init__(self, username: str, role: str):
        super().__init__()
        self.username = username
        self.is_admin = role == "admin"
        self._pool = QThreadPool.globalInstance()
        self._workers = {}
        self._write_id = 0
        self._logo_data = None
        self._logo_name = ""
        layout = QVBoxLayout(self)
        title = QLabel("School profile")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(wrapped_label(
            "These optional details appear on approved report cards and result sheets. "
            "Leaving a field blank omits it from the PDF. Optional PNG/JPEG logos are stored locally."
        ))
        form = QFormLayout()
        self.fields = {}
        for key, label, max_length in self.FIELDS:
            if key == "school_type":
                edit = QComboBox()
                edit.addItem("Select school type", "")
                edit.addItem("Primary", "primary")
                edit.addItem("Secondary", "secondary")
                edit.addItem("Primary and Secondary", "combined")
                edit.setEnabled(self.is_admin)
            else:
                edit = QLineEdit()
                edit.setMaxLength(max_length)
                edit.setReadOnly(not self.is_admin)
            self.fields[key] = edit
            form.addRow(label, edit)
        layout.addLayout(form)
        logo_row = QHBoxLayout()
        self.logo_status = QLabel("No school logo selected")
        self.choose_logo_button = QPushButton("Choose PNG/JPEG logo…")
        self.clear_logo_button = QPushButton("Remove logo")
        self.choose_logo_button.clicked.connect(self.choose_logo)
        self.clear_logo_button.clicked.connect(self.clear_logo)
        self.choose_logo_button.setEnabled(self.is_admin)
        self.clear_logo_button.setEnabled(self.is_admin)
        logo_row.addWidget(self.logo_status, 1)
        logo_row.addWidget(self.choose_logo_button)
        logo_row.addWidget(self.clear_logo_button)
        layout.addLayout(logo_row)
        self.status = QLabel("Loading school profile…")
        layout.addWidget(self.status)
        self.save_button = QPushButton("Save school profile")
        self.save_button.setObjectName("primary")
        self.save_button.setEnabled(self.is_admin)
        self.save_button.clicked.connect(self.save)
        layout.addWidget(self.save_button)
        layout.addStretch()
        self.refresh()

    def refresh(self):
        try:
            profile = db.load_school_profile()
            self._logo_data = db.load_school_logo()
        except Exception as exc:
            self.status.setText("Could not load school profile")
            QMessageBox.warning(self, "Could not load school profile", str(exc))
            return
        for key, value in profile.items():
            if key in self.fields:
                edit = self.fields[key]
                if isinstance(edit, QComboBox):
                    index = edit.findData(value)
                    edit.setCurrentIndex(max(0, index))
                else:
                    edit.setText(value)
        self._logo_name = ""
        self.logo_status.setText("A logo is saved in this local database" if self._logo_data else "No school logo selected")
        self.status.setText("Profile loaded. Details are optional and appear on approved result PDFs.")

    def choose_logo(self):
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, "Choose school logo", "", "Image files (*.png *.jpg *.jpeg)"
        )
        if not path:
            return
        try:
            image_data = open(path, "rb").read()
        except OSError as exc:
            QMessageBox.warning(self, "Could not read image", str(exc))
            return
        if len(image_data) > 1_000_000:
            QMessageBox.warning(self, "Unsupported logo", "Choose a valid PNG or JPEG no larger than 1 MB or 3000 × 1800 pixels.")
            return
        image_buffer = QBuffer(QByteArray(image_data))
        image_buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(image_buffer)
        reader.setDecideFormatFromContent(True)
        size = reader.size()
        if size.isEmpty() or size.width() > 3000 or size.height() > 1800:
            QMessageBox.warning(
                self, "Unsupported logo", "Choose a valid PNG or JPEG no larger than 1 MB or 3000 × 1800 pixels."
            )
            return
        image = reader.read()
        if image.isNull():
            QMessageBox.warning(self, "Unsupported logo", "Choose a readable PNG or JPEG image.")
            return
        self._logo_data = image_data
        self._logo_name = path.replace("\\", "/").rsplit("/", 1)[-1]
        self.logo_status.setText(f"{self._logo_name} selected; save to apply")

    def clear_logo(self):
        self._logo_data = None
        self._logo_name = ""
        self.logo_status.setText("Logo will be removed when you save")

    def save(self):
        if not self.is_admin or not self.save_button.isEnabled():
            return
        profile = {
            key: (edit.currentData() if isinstance(edit, QComboBox) else edit.text())
            for key, edit in self.fields.items()
        }
        self.save_button.setEnabled(False)
        self.status.setText("Saving school profile…")
        self._write_id += 1
        task_id = self._write_id
        logo_data = self._logo_data
        worker = BackgroundTask(task_id, lambda: db.save_school_profile(profile, self.username, logo_data))
        worker.signals.finished.connect(self._saved)
        self._workers[task_id] = worker
        self._pool.start(worker)

    def _saved(self, task_id, _result, error):
        self._workers.pop(task_id, None)
        if task_id != self._write_id:
            return
        self.save_button.setEnabled(self.is_admin)
        if error is not None:
            self.status.setText("School profile was not saved")
            QMessageBox.warning(self, "Could not save school profile", str(error))
            return
        self.logo_status.setText(
            f"{self._logo_name} saved with profile" if self._logo_name else
            ("A logo is saved in this local database" if self._logo_data else "No school logo selected")
        )
        self.status.setText("School profile saved")
