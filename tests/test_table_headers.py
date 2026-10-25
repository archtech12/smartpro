import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QAbstractItemView, QHeaderView, QTableWidget

from smartschool.table_helpers import configure_table_headers


def test_shared_table_headers_fit_labels_and_stay_resizable():
    app = QApplication.instance() or QApplication([])
    table = QTableWidget(0, 3)
    labels = ["Student Name", "Admission Number", "Reference or Reason"]
    table.setHorizontalHeaderLabels(labels)

    configure_table_headers(table)

    header = table.horizontalHeader()
    assert header.minimumHeight() >= 40
    assert header.minimumSectionSize() == 76
    assert header.stretchLastSection() is False
    assert table.horizontalScrollMode() == QAbstractItemView.ScrollMode.ScrollPerPixel
    for column, label in enumerate(labels):
        item = table.horizontalHeaderItem(column)
        expected = min(280, header.fontMetrics().horizontalAdvance(label) + 28)
        if label == "Student Name":
            expected = max(expected, 150)
        elif label == "Admission Number":
            expected = max(expected, 130)
        assert item.toolTip() == label
        assert table.columnWidth(column) >= expected
        assert header.sectionResizeMode(column) == QHeaderView.ResizeMode.Interactive
    table.close()
    app.processEvents()
