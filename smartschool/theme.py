"""Shared visual design for every page, tab and dialog.

This module is Qt-free (it only builds a stylesheet string) so the design
tokens can be unit-tested without a display. ``ui.run_app`` applies the
stylesheet to the whole application, which means dialogs created before the
main window (setup, sign-in) and message boxes get the same look.

Object names understood by the stylesheet (use them instead of inline styles):
  heading, subheading, muted, primary, danger, nav, navActive, card,
  cardValue, cardLabel, dashboardAction, pageTitle, topBar, statusOk,
  statusWarn, statusError, hint
"""

COLORS = {
    "bg": "#f4f6fa",
    "surface": "#ffffff",
    "surface_alt": "#f8fafc",
    "border": "#d9e0ea",
    "border_strong": "#b8c4d4",
    "text": "#182230",
    "muted": "#5b6b7d",
    "primary": "#155eef",
    "primary_hover": "#004eeb",
    "primary_pressed": "#003fc0",
    "primary_soft": "#e8f0fe",
    "navy": "#102a43",
    "navy_hover": "#1d3d5c",
    "navy_active": "#24547b",
    "navy_text": "#d9e6f2",
    "danger": "#b42318",
    "danger_soft": "#fdecea",
    "ok": "#067647",
    "ok_soft": "#e7f6ee",
    "warn": "#b54708",
    "warn_soft": "#fff4e5",
    "disabled_bg": "#eef1f5",
    "disabled_text": "#98a2b3",
    "row_alt": "#f7f9fc",
    "selection": "#d6e4ff",
}

FONT_FAMILY = '"Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif'


def build_stylesheet(colors: dict | None = None) -> str:
    c = dict(COLORS)
    if colors:
        c.update(colors)
    return f"""
/* ---------- base ---------- */
QWidget {{ font-family: {FONT_FAMILY}; font-size: 10pt; color: {c['text']}; }}
QMainWindow, QDialog, QStackedWidget, QScrollArea#pageScroll, QScrollArea#pageScroll > QWidget > QWidget {{ background: {c['bg']}; }}
QLabel {{ background: transparent; }}
QLabel#heading {{ font-size: 21pt; font-weight: 700; color: {c['navy']}; padding-bottom: 2px; }}
QLabel#subheading {{ font-size: 13pt; font-weight: 700; color: {c['navy']}; }}
QLabel#muted, QLabel#hint {{ color: {c['muted']}; }}
QLabel#hint {{ font-size: 9pt; }}
QLabel#statusOk {{ color: {c['ok']}; font-weight: 600; }}
QLabel#statusWarn {{ color: {c['warn']}; font-weight: 600; }}
QLabel#statusError {{ color: {c['danger']}; font-weight: 600; }}
QToolTip {{ background: {c['navy']}; color: white; border: 0; padding: 6px 8px; }}

/* ---------- buttons ---------- */
QPushButton {{
    padding: 7px 14px; min-height: 20px; border: 1px solid {c['border_strong']};
    border-radius: 6px; background: {c['surface']}; color: {c['text']};
}}
QPushButton:hover {{ background: {c['primary_soft']}; border-color: {c['primary']}; }}
QPushButton:pressed {{ background: {c['selection']}; }}
QPushButton:focus {{ border: 1px solid {c['primary']}; }}
QPushButton:disabled {{ background: {c['disabled_bg']}; color: {c['disabled_text']}; border-color: {c['border']}; }}
QPushButton#primary, QPushButton:default {{
    background: {c['primary']}; color: white; border: 1px solid {c['primary']}; font-weight: 600;
}}
QPushButton#primary:hover, QPushButton:default:hover {{ background: {c['primary_hover']}; border-color: {c['primary_hover']}; }}
QPushButton#primary:pressed, QPushButton:default:pressed {{ background: {c['primary_pressed']}; }}
QPushButton#primary:disabled, QPushButton:default:disabled {{
    background: {c['disabled_bg']}; color: {c['disabled_text']}; border-color: {c['border']};
}}
QPushButton#danger {{ color: {c['danger']}; border-color: {c['danger']}; }}
QPushButton#danger:hover {{ background: {c['danger_soft']}; }}
QPushButton#dashboardAction {{
    text-align: left; padding: 14px 16px; border: 1px solid {c['border']}; border-radius: 10px;
    background: {c['surface']}; font-weight: 600; color: {c['navy']};
}}
QPushButton#dashboardAction:hover {{ background: {c['primary_soft']}; border-color: {c['primary']}; color: {c['primary']}; }}

QPushButton#alertWarn, QPushButton#alertInfo {{
    text-align: left; padding: 9px 14px; border-radius: 8px; font-weight: 600;
}}
QPushButton#alertWarn {{ background: {c['warn_soft']}; color: {c['warn']}; border: 1px solid {c['warn']}; }}
QPushButton#alertInfo {{ background: {c['primary_soft']}; color: {c['primary']}; border: 1px solid {c['primary']}; }}
QPushButton#alertWarn:hover, QPushButton#alertInfo:hover {{ background: {c['surface']}; }}

/* ---------- inputs ---------- */
QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox, QTextEdit, QPlainTextEdit {{
    padding: 6px 9px; min-height: 20px; border: 1px solid {c['border_strong']}; border-radius: 6px;
    background: {c['surface']}; selection-background-color: {c['selection']}; selection-color: {c['text']};
}}
QLineEdit:hover, QComboBox:hover, QDateEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover,
QTextEdit:hover, QPlainTextEdit:hover {{ border-color: {c['primary']}; }}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QTextEdit:focus, QPlainTextEdit:focus {{ border: 2px solid {c['primary']}; padding: 5px 8px; }}
QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QTextEdit:disabled, QPlainTextEdit:disabled {{ background: {c['disabled_bg']}; color: {c['disabled_text']}; }}
QLineEdit:read-only {{ background: {c['surface_alt']}; }}
QComboBox::drop-down {{ border: 0; width: 24px; }}
QComboBox QAbstractItemView {{
    background: {c['surface']}; border: 1px solid {c['border_strong']};
    selection-background-color: {c['primary_soft']}; selection-color: {c['primary']}; outline: 0;
}}
QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
QCheckBox:disabled, QRadioButton:disabled {{ color: {c['disabled_text']}; }}

/* ---------- tables, lists ---------- */
QTableWidget, QTableView, QListWidget, QTreeWidget {{
    background: {c['surface']}; alternate-background-color: {c['row_alt']};
    border: 1px solid {c['border']}; border-radius: 8px; gridline-color: {c['border']};
    selection-background-color: {c['selection']}; selection-color: {c['text']}; outline: 0;
}}
QTableWidget::item, QTableView::item {{ padding: 4px 6px; }}
QTableWidget::item:hover, QTableView::item:hover, QListWidget::item:hover {{ background: {c['primary_soft']}; }}
QTableWidget::item:selected, QTableView::item:selected, QListWidget::item:selected {{
    background: {c['selection']}; color: {c['text']};
}}
QListWidget::item {{ padding: 5px 8px; }}
QHeaderView::section {{
    background: {c['surface_alt']}; color: {c['navy']}; padding: 8px; border: 0;
    border-bottom: 2px solid {c['border']}; font-weight: 600;
}}
QTableCornerButton::section {{ background: {c['surface_alt']}; border: 0; }}

/* ---------- containers ---------- */
QGroupBox {{
    background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 10px;
    margin-top: 14px; padding: 14px 12px 10px 12px; font-weight: 600;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {c['navy']}; background: {c['bg']}; }}
QFrame#card {{ background: {c['surface']}; border: 1px solid {c['border']}; border-radius: 12px; }}
QFrame#card QLabel {{ border: 0; }}
QLabel#cardLabel {{ color: {c['muted']}; font-weight: 600; }}
QLabel#cardValue {{ font-size: 24pt; font-weight: 700; color: {c['primary']}; }}
QFrame#topBar {{ background: {c['surface']}; border-bottom: 1px solid {c['border']}; }}
QLabel#pageTitle {{ font-size: 12pt; font-weight: 700; color: {c['navy']}; }}
QTabWidget::pane {{ border: 1px solid {c['border']}; border-radius: 8px; background: {c['surface']}; top: -1px; }}
QTabBar::tab {{
    padding: 8px 16px; margin-right: 2px; background: {c['disabled_bg']}; color: {c['muted']};
    border: 1px solid {c['border']}; border-bottom: 0; border-top-left-radius: 7px; border-top-right-radius: 7px;
}}
QTabBar::tab:hover {{ background: {c['primary_soft']}; color: {c['primary']}; }}
QTabBar::tab:selected {{ background: {c['surface']}; color: {c['primary']}; font-weight: 600; }}
QProgressBar {{ border: 1px solid {c['border']}; border-radius: 6px; background: {c['surface']}; height: 10px; text-align: center; }}
QProgressBar::chunk {{ background: {c['primary']}; border-radius: 5px; }}

/* ---------- scrollbars ---------- */
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {c['border_strong']}; border-radius: 5px; min-height: 28px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {c['border_strong']}; border-radius: 5px; min-width: 28px; margin: 2px; }}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{ background: {c['muted']}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: 0; width: 0; height: 0; }}

/* ---------- menus, message boxes ---------- */
QMenu {{ background: {c['surface']}; border: 1px solid {c['border']}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 4px; }}
QMenu::item:selected {{ background: {c['primary_soft']}; color: {c['primary']}; }}
QMessageBox {{ background: {c['surface']}; }}
QMessageBox QLabel {{ font-size: 10pt; }}
QDialogButtonBox QPushButton {{ min-width: 84px; }}
QStatusBar {{ background: {c['surface']}; border-top: 1px solid {c['border']}; color: {c['muted']}; }}
QStatusBar::item {{ border: 0; }}

/* ---------- sidebar navigation ---------- */
QFrame#sidebar {{ background: {c['navy']}; }}
QFrame#sidebar QLabel {{ color: {c['navy_text']}; }}
QLabel#brand {{ color: white; font-size: 15pt; font-weight: 800; padding: 18px 10px 6px 10px; }}
QLabel#brandSub {{ color: #8fb0cf; font-size: 8pt; padding: 0 10px 10px 10px; }}
QFrame#sidebar QLineEdit {{
    background: {c['navy_hover']}; color: white; border: 1px solid {c['navy_active']}; border-radius: 6px;
    margin: 0 8px;
}}
QFrame#sidebar QLineEdit:focus {{ border: 2px solid #7fb2ff; padding: 5px 8px; }}
QTabWidget#navTabs::pane {{ border: 0; background: transparent; }}
QTabWidget#navTabs QTabBar::tab {{
    background: {c['navy_hover']}; color: {c['navy_text']}; padding: 8px 14px; border: 0; border-radius: 0; margin: 0;
}}
QTabWidget#navTabs QTabBar::tab:hover {{ background: {c['navy_active']}; color: white; }}
QTabWidget#navTabs QTabBar::tab:selected {{ background: {c['navy_active']}; color: white; font-weight: 600; }}
QScrollArea#navScroll, QScrollArea#navScroll > QWidget > QWidget {{ background: {c['navy']}; border: 0; }}
QPushButton#nav {{
    background: transparent; color: {c['navy_text']}; text-align: left; border: 0;
    border-left: 3px solid transparent; border-radius: 0; padding: 10px 12px;
}}
QPushButton#nav:hover {{ background: {c['navy_hover']}; color: white; }}
QPushButton#navActive {{
    background: {c['navy_active']}; color: white; text-align: left; font-weight: 700; border: 0;
    border-left: 3px solid #7fb2ff; border-radius: 0; padding: 10px 12px;
}}
"""


STYLE = build_stylesheet()
