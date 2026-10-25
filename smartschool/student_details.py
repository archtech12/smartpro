"""Validation shared by guided registration and student roster imports."""

from __future__ import annotations

import re
from datetime import date


def normalize_optional_student_details(
    nin: str = "", date_of_birth: str = "", email: str = ""
) -> tuple[str, str, str]:
    """Normalize optional identifiers/contact details without imposing country-specific NIN rules."""
    if any(not isinstance(value, str) for value in (nin, date_of_birth, email)):
        raise ValueError("NIN, date of birth, and email must be text.")
    nin, date_of_birth, email = nin.strip(), date_of_birth.strip(), email.strip()
    if len(nin) > 60:
        raise ValueError("NIN must be at most 60 characters.")
    if len(email) > 254:
        raise ValueError("Email must be at most 254 characters.")
    if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("Email must be a valid email address.")
    if date_of_birth:
        try:
            parsed = date.fromisoformat(date_of_birth)
        except ValueError as exc:
            raise ValueError("Date of birth must use YYYY-MM-DD format.") from exc
        if parsed.isoformat() != date_of_birth:
            raise ValueError("Date of birth must use YYYY-MM-DD format.")
        if parsed > date.today():
            raise ValueError("Date of birth cannot be in the future.")
    return nin, date_of_birth, email
