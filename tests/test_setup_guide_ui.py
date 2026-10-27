import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from smartschool import dashboard_data
from smartschool.setup_guide_ui import SetupGuideDialog


def test_guide_lists_steps_and_opens_selected_page(school):
    app = QApplication.instance() or QApplication([])
    checklist = dashboard_data.load_setup_checklist()
    dialog = SetupGuideDialog(None, checklist)
    assert dialog.step_list.count() == len(checklist["steps"])
    assert dialog.progress.maximum() == checklist["required_total"]
    # the guide starts on the first unfinished required step
    assert dialog._steps[dialog.step_list.currentRow()]["key"] == checklist["next_step"]["key"]
    dialog._open_current()
    assert dialog.selected_page == checklist["next_step"]["page"]
    dialog.close()
    app.processEvents()
