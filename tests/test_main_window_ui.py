import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from smartschool.ui import MainWindow
from smartschool.ui_polish import install_polish


def test_navigation_highlights_active_page_and_filters():
    app = QApplication.instance() or QApplication([])
    install_polish(app)
    window = MainWindow({"username": "tester", "role": "admin"})
    assert window._nav_buttons["Dashboard"].objectName() == "navActive"
    window._filter_nav("fee")
    window.show()
    assert not window._nav_buttons["Dashboard"].isVisible()
    assert window._nav_buttons["Fee Setup"].isVisible()
    window._filter_nav("")
    assert window._nav_buttons["Dashboard"].isVisible()
    window.close()
    app.processEvents()
