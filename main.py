import sys
from PyQt6.QtWidgets import QApplication
from smartschool.ui import run_app
from smartschool.version import __version__
from smartschool.crash_guard import install_crash_guard

def main():
    install_crash_guard()
    app = QApplication(sys.argv)
    app.setApplicationName("SmartSchool Manager Rebuild")
    app.setOrganizationName("SmartSchool")
    app.setApplicationVersion(__version__)
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
