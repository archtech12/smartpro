"""Step-by-step school setup guide (a read-only checklist that opens each page)."""

from PyQt6.QtCore import Qt, QThreadPool
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from . import dashboard_data
from .ui_helpers import wrapped_label
from .workers import BackgroundTask


def step_marker(step: dict) -> str:
    if step["done"]:
        return "✓"
    return "○" if step["required"] else "·"


class SetupGuideDialog(QDialog):
    """Shows setup progress. ``selected_page`` is set when the user opens a step."""

    def __init__(self, parent=None, checklist: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("School setup guide")
        self.setMinimumSize(720, 460)
        self.selected_page = None
        self._steps = []
        self._task_id = 0
        self._workers = {}
        self._thread_pool = QThreadPool.globalInstance()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        title = QLabel("Set up your school")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(wrapped_label(
            "Work through the steps in order. Each step opens the page where you do the work; "
            "come back here any time from the dashboard. Optional steps are marked with a dot."
        ))
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        layout.addWidget(self.progress)

        body = QHBoxLayout()
        body.setSpacing(14)
        self.step_list = QListWidget()
        self.step_list.setFixedWidth(250)
        self.step_list.currentRowChanged.connect(self._show_step)
        body.addWidget(self.step_list)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        self.step_title = QLabel()
        self.step_title.setObjectName("subheading")
        self.step_state = QLabel()
        self.step_why = wrapped_label("")
        self.step_detail = wrapped_label("")
        self.step_detail.setObjectName("muted")
        self.open_button = QPushButton("Open this page")
        self.open_button.setObjectName("primary")
        self.open_button.clicked.connect(self._open_current)
        for widget in (self.step_title, self.step_state, self.step_why, self.step_detail):
            detail_layout.addWidget(widget)
        detail_layout.addSpacing(8)
        detail_layout.addWidget(self.open_button, 0, Qt.AlignmentFlag.AlignLeft)
        detail_layout.addStretch()
        body.addWidget(detail, 1)
        layout.addLayout(body, 1)

        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("Check again")
        self.refresh_button.setToolTip("Re-read the school's current setup")
        self.refresh_button.clicked.connect(self.reload)
        self.next_button = QPushButton("Go to next unfinished step")
        self.next_button.clicked.connect(self._go_next_unfinished)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.next_button)
        buttons.addStretch()
        buttons.addWidget(close)
        layout.addLayout(buttons)

        if checklist is not None:
            self.set_checklist(checklist)
        else:
            self.reload()

    # -- data ---------------------------------------------------------------
    def reload(self):
        self._task_id += 1
        task_id = self._task_id
        self.refresh_button.setEnabled(False)
        worker = BackgroundTask(task_id, dashboard_data.load_setup_checklist)
        worker.signals.finished.connect(self._loaded)
        self._workers[task_id] = worker
        self._thread_pool.start(worker)

    def _loaded(self, task_id, checklist, error):
        self._workers.pop(task_id, None)
        if task_id != self._task_id:
            return
        self.refresh_button.setEnabled(True)
        if error is not None:
            QMessageBox.warning(self, "Could not read setup status", str(error))
            return
        self.set_checklist(checklist)

    def set_checklist(self, checklist: dict):
        keep = max(self.step_list.currentRow(), 0)
        self._steps = checklist["steps"]
        self.step_list.blockSignals(True)
        self.step_list.clear()
        for step in self._steps:
            self.step_list.addItem(QListWidgetItem(f"{step_marker(step)}  {step['title']}"))
        self.step_list.blockSignals(False)
        self.progress.setRange(0, checklist["required_total"])
        self.progress.setValue(checklist["required_done"])
        self.progress.setFormat(f"{checklist['required_done']} of {checklist['required_total']} required steps done")
        self.next_button.setEnabled(checklist["next_step"] is not None)
        first = next((i for i, s in enumerate(self._steps) if s["required"] and not s["done"]), keep)
        self.step_list.setCurrentRow(first if checklist["next_step"] else keep)
        self._show_step(self.step_list.currentRow())

    # -- interaction --------------------------------------------------------
    def _show_step(self, row):
        if not 0 <= row < len(self._steps):
            return
        step = self._steps[row]
        self.step_title.setText(step["title"])
        self.step_state.setText("Done" if step["done"] else ("To do" if step["required"] else "Optional"))
        self.step_state.setObjectName("statusOk" if step["done"] else ("statusWarn" if step["required"] else "muted"))
        self.step_state.style().unpolish(self.step_state)
        self.step_state.style().polish(self.step_state)
        self.step_why.setText(step["why"])
        self.step_detail.setText(f"Current status: {step['detail']}")
        self.open_button.setText("Review this page" if step["done"] else "Open this page")

    def _go_next_unfinished(self):
        for index, step in enumerate(self._steps):
            if step["required"] and not step["done"]:
                self.step_list.setCurrentRow(index)
                return

    def _open_current(self):
        row = self.step_list.currentRow()
        if 0 <= row < len(self._steps):
            self.selected_page = self._steps[row]["page"]
            self.accept()
