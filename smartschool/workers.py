"""Small Qt worker wrapper for operations that should not block the UI thread."""

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal


class TaskSignals(QObject):
    finished = pyqtSignal(int, object, object)


class BackgroundTask(QRunnable):
    def __init__(self, task_id: int, operation):
        super().__init__()
        self.task_id = task_id
        self.operation = operation
        self.signals = TaskSignals()

    def run(self):
        try:
            self.signals.finished.emit(self.task_id, self.operation(), None)
        except Exception as exc:
            self.signals.finished.emit(self.task_id, None, exc)
