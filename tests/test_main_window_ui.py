import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from smartschool.ui import Dashboard, MainWindow
from smartschool.ui_polish import install_polish


def test_navigation_highlights_active_page_and_filters(monkeypatch):
    # Navigation coverage must not start database work or access app-data.
    monkeypatch.setattr(Dashboard, "refresh", lambda self: None)
    app = QApplication.instance() or QApplication([])
    install_polish(app)
    window = MainWindow({"username": "tester", "role": "admin"})
    assert window.account_button.text() == "tester · Admin ▾"
    assert window.account_menu.actions()[0].text() == "Change my password"
    assert all(button.text() != "Change my password" for button in window._nav_buttons.values())
    assert window._nav_buttons["Dashboard"].objectName() == "navActive"
    window._filter_nav("fee setup")
    window.show()
    assert not window._nav_buttons["Dashboard"].isVisible()
    assert window._nav_buttons["Fee Setup"].isVisible()
    assert window._nav_tabs.currentIndex() == window._nav_tab_for_page["Fee Setup"]
    window._filter_nav("")
    assert window._nav_buttons["Dashboard"].isVisible()
    assert window._nav_tabs.currentIndex() == window._nav_tab_for_page["Dashboard"]
    window.close()
    app.processEvents()
