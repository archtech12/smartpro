"""Administrator-managed school identity used on printed result PDFs."""

from __future__ import annotations

from PyQt6.QtCore import QBuffer, QByteArray, QDate, QIODevice, QThreadPool
from PyQt6.QtGui import QImageReader
from PyQt6.QtWidgets import (
    QComboBox, QDateEdit, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from . import database as db
from .ui_helpers import wrapped_label
from .workers import BackgroundTask


class SchoolProfilePage(QWidget):
    FIELDS = (
        ("school_name", "School name", 120),
        ("admission_id_prefix", "Student admission ID prefix", 16),
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
        self._signatures = {"class_teacher_signature": None, "head_signature": None}
        self._signature_status = {}
        layout = QVBoxLayout(self)
        title = QLabel("School profile")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(wrapped_label(
            "These details appear on approved reports. New student IDs use the prefix, year, and a unique sequence "
            "(for example, ADM-2026-00001); changing the prefix does not change existing IDs."
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
                if key == "admission_id_prefix":
                    edit.setPlaceholderText("ADM")
                    edit.setToolTip("1–16 letters or numbers; single hyphens are allowed. Applies to newly generated student IDs.")
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
        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("Report card shows"))
        self.ranking_style = QComboBox()
        for value, label in db.REPORT_RANKING_STYLES.items():
            self.ranking_style.addItem(label, value)
        self.ranking_style.setEnabled(self.is_admin)
        style_row.addWidget(self.ranking_style, 1)
        layout.addLayout(style_row)
        term_row = QFormLayout()
        self.term_dates = {}
        for key, label in (("term_closing_date", "Term closes on"), ("next_term_begins", "Next term begins on")):
            edit = QDateEdit()
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("dd MMM yyyy")
            edit.setMinimumDate(QDate(2000, 1, 1))   # the minimum date means "not set"
            edit.setSpecialValueText("Not set")
            edit.setDate(edit.minimumDate())
            edit.setEnabled(self.is_admin)
            edit.setToolTip("Printed on report cards. Pick the minimum date (or press Delete) to leave it blank.")
            self.term_dates[key] = edit
            term_row.addRow(label, edit)
        layout.addLayout(term_row)
        for key, label in (("class_teacher_signature", "Class teacher signature"), ("head_signature", "Head of school signature")):
            row = QHBoxLayout()
            status = QLabel(f"No {label.lower()} selected")
            choose = QPushButton(f"Choose {label.lower()}…")
            clear = QPushButton("Remove")
            choose.clicked.connect(lambda _checked=False, k=key, l=label: self.choose_signature(k, l))
            clear.clicked.connect(lambda _checked=False, k=key, l=label: self.clear_signature(k, l))
            choose.setEnabled(self.is_admin)
            clear.setEnabled(self.is_admin)
            row.addWidget(status, 1)
            row.addWidget(choose)
            row.addWidget(clear)
            layout.addLayout(row)
            self._signature_status[key] = status
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
            report_settings = db.load_report_settings()
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
        self.ranking_style.setCurrentIndex(max(0, self.ranking_style.findData(report_settings["ranking_style"])))
        for key, edit in self.term_dates.items():
            parsed = QDate.fromString(report_settings.get(key, ""), "yyyy-MM-dd")
            edit.setDate(parsed if parsed.isValid() else edit.minimumDate())
        for key, label in (("class_teacher_signature", "class teacher signature"), ("head_signature", "head of school signature")):
            self._signatures[key] = report_settings[key]
            self._signature_status[key].setText(
                f"A {label} is saved in this local database" if report_settings[key] else f"No {label} selected"
            )
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

    def _read_image_file(self, title: str):
        """Return validated PNG/JPEG bytes chosen by the user, or None if cancelled or rejected."""
        path, _selected_filter = QFileDialog.getOpenFileName(self, title, "", "Image files (*.png *.jpg *.jpeg)")
        if not path:
            return None
        try:
            with open(path, "rb") as source:
                image_data = source.read()
        except OSError as exc:
            QMessageBox.warning(self, "Could not read image", str(exc))
            return None
        reader = None
        if len(image_data) <= 1_000_000:
            image_buffer = QBuffer(QByteArray(image_data))
            image_buffer.open(QIODevice.OpenModeFlag.ReadOnly)
            reader = QImageReader(image_buffer)
            reader.setDecideFormatFromContent(True)
        size = reader.size() if reader is not None else None
        if size is None or size.isEmpty() or size.width() > 3000 or size.height() > 1800 or reader.read().isNull():
            QMessageBox.warning(self, "Unsupported image", "Choose a readable PNG or JPEG no larger than 1 MB or 3000 × 1800 pixels.")
            return None
        return image_data

    def choose_signature(self, key: str, label: str):
        try:
            image_data = self._read_image_file(f"Choose {label.lower()}")
        except Exception as exc:  # keep an unexpected image error from closing the application
            QMessageBox.warning(self, "Could not use image", str(exc))
            return
        if image_data is None:
            return
        self._signatures[key] = image_data
        self._signature_status[key].setText(f"{label} selected; save to apply")

    def clear_signature(self, key: str, label: str):
        self._signatures[key] = None
        self._signature_status[key].setText(f"{label} will be removed when you save")

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
        ranking_style = self.ranking_style.currentData()
        signatures = dict(self._signatures)
        term_dates = {
            key: ("" if edit.date() == edit.minimumDate() else edit.date().toString("yyyy-MM-dd"))
            for key, edit in self.term_dates.items()
        }
        worker = BackgroundTask(
            task_id,
            lambda: db.save_school_profile(
                profile, self.username, logo_data, report_ranking_style=ranking_style,
                head_signature=signatures["head_signature"],
                class_teacher_signature=signatures["class_teacher_signature"],
                term_closing_date=term_dates["term_closing_date"], next_term_begins=term_dates["next_term_begins"],
            ),
        )
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
