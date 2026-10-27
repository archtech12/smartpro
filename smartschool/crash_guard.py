"""Keep an unexpected error in one button from closing the whole application.

PyQt6 aborts the process when a slot raises and Python's default exception hook is in place.
Installing our own hook logs the full traceback to the application log and shows a short
message instead, so work in progress is not lost and the cause can be diagnosed.
"""

from __future__ import annotations

import logging
import sys
import traceback

_installed = False
_showing = False


def install_crash_guard() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    previous = sys.excepthook

    def hook(exc_type, exc, tb):
        global _showing
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            previous(exc_type, exc, tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        logging.getLogger("smartschool.crash").error("Unhandled error:\n%s", text)
        if _showing:
            return
        _showing = True
        try:
            from PyQt6.QtWidgets import QApplication, QMessageBox

            if QApplication.instance() is not None:
                box = QMessageBox(QMessageBox.Icon.Warning, "Something went wrong",
                                  "That action could not be completed, but the application is still running.\n\n"
                                  f"{exc_type.__name__}: {exc}")
                box.setInformativeText("Nothing was lost. Details were saved in smartschool.log in the application data folder.")
                box.exec()
        except Exception:  # never raise from the error handler itself
            pass
        finally:
            _showing = False

    sys.excepthook = hook
