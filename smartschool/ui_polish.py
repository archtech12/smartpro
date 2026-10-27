"""Qt-side visual polish applied once, app-wide.

An application event filter tidies every table the first time it is shown
(including tables inside dialogs) so each page and dialog looks and behaves
consistently without editing every module. It only changes presentation:
alternating row colours, hidden grid lines, consistent row height, tooltips
for clipped cells. It never changes selection or edit behaviour.
"""

from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtWidgets import QAbstractItemView, QApplication, QTableView, QTableWidget

from .theme import build_stylesheet

_POLISHED = "_ss_polished"


def polish_table(table) -> None:
    if table.property(_POLISHED):
        return
    table.setProperty(_POLISHED, True)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.setWordWrap(False)
    table.verticalHeader().setDefaultSectionSize(max(32, table.verticalHeader().defaultSectionSize()))
    table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    if isinstance(table, QTableWidget):
        table.itemEntered.connect(_tooltip_clipped_cell)
        table.setMouseTracking(True)


def _tooltip_clipped_cell(item) -> None:
    # Show the full text for cells that may be elided and have no tooltip yet.
    if item is not None and not item.toolTip() and item.text():
        item.setToolTip(item.text())


class _PolishFilter(QObject):
    def eventFilter(self, obj, event):  # noqa: N802 (Qt naming)
        if event.type() == QEvent.Type.Show and isinstance(obj, QTableView):
            try:
                polish_table(obj)
            except Exception:  # presentation only; never break a page
                pass
        return False


def install_polish(app: QApplication | None = None) -> None:
    app = app or QApplication.instance()
    if app is None or getattr(app, "_ss_polish_filter", None) is not None:
        return
    app.setStyleSheet(build_stylesheet())
    app._ss_polish_filter = _PolishFilter(app)
    app.installEventFilter(app._ss_polish_filter)
