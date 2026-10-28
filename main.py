import sys
from PyQt6.QtWidgets import QApplication
from smartschool.ui import run_app
from smartschool.version import __version__
from smartschool.crash_guard import install_crash_guard
from smartschool.ui_polish import install_polish

def main():
    install_crash_guard()
    app = QApplication(sys.argv)
    app.setApplicationName("SmartSchool Manager Rebuild")
    app.setOrganizationName("SmartSchool")
    app.setApplicationVersion(__version__)
    install_polish()
    from smartschool.activation_ui import ensure_activated
    if not ensure_activated():
        sys.exit(0)
    try:
        window = run_app()
        window.showMaximized()
        sys.exit(app.exec())
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Application failed to start")
        raise

if __name__ == "__main__":
    main()
