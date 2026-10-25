import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDialogButtonBox

from smartschool.ui import LoginDialog


def test_login_dialog_has_clear_labels_and_sign_in_action():
    app = QApplication.instance() or QApplication([])
    dialog = LoginDialog()

    assert dialog.windowTitle() == "SmartSchool Manager — Sign in"
    assert dialog.username.placeholderText() == "Enter your username"
    assert dialog.password.placeholderText() == "Enter your password"
    assert dialog.sign_in_button.text() == "Sign in"
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel).text() == "Close"
    assert dialog.width() == 430

    dialog.close()
    app.processEvents()
