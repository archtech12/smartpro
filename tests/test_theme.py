from smartschool import theme


def test_stylesheet_is_balanced_and_covers_core_widgets():
    css = theme.build_stylesheet()
    assert css.count("{") == css.count("}")
    for selector in ("QPushButton:default", "QPushButton#primary", "QPushButton#navActive",
                     "QTableWidget", "QDialog", "QMessageBox", "QTabBar::tab",
                     "QLabel#cardValue", "QLineEdit:focus", "QStatusBar"):
        assert selector in css


def test_every_colour_token_is_a_valid_hex_value():
    for name, value in theme.COLORS.items():
        assert len(value) == 7 and value.startswith("#"), name
        int(value[1:], 16)


def test_colour_override_is_applied():
    assert "#123456" in theme.build_stylesheet({"primary": "#123456"})
