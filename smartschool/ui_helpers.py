"""Small shared helpers for responsive desktop layouts."""

from PyQt6.QtWidgets import QLabel


def wrapped_label(text: str) -> QLabel:
    """Create a readable helper label that adapts to the available page width."""
    label = QLabel(text)
    label.setWordWrap(True)
    return label
