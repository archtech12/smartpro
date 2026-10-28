"""Shared table header sizing for a readable desktop layout."""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QAbstractItemView, QHeaderView, QSizePolicy


def configure_table_headers(table):
    """Size each header to its label and expose the full label as a tooltip.

    Sections remain interactive so users can widen data-heavy columns. Long
    tables keep their own horizontal scrollbar instead of forcing the window
    or screen to grow.
    """
    header = table.horizontalHeader()
    header.setMinimumSectionSize(76)
    header.setMaximumSectionSize(280)
    header.setMinimumHeight(40)
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    header.setSectionsMovable(False)
    header.setStretchLastSection(False)
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    table.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)

    metrics = header.fontMetrics()
    for column in range(table.columnCount()):
        item = table.horizontalHeaderItem(column)
        if item is None:
            continue
        label = item.text()
        item.setToolTip(label)
        desired = metrics.horizontalAdvance(label) + 28
        if label.casefold() in {"student", "student name"}:
            desired = max(desired, 150)
        elif label.casefold() in {"admission no.", "admission number"}:
            desired = max(desired, 130)
        table.setColumnWidth(column, max(76, min(280, desired)))
