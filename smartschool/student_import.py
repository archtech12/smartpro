"""Validated CSV/XLSX templates and previews for bulk student registration."""

from __future__ import annotations

from pathlib import Path

from .spreadsheet_files import read_rows, write_rows
from .student_details import normalize_optional_student_details

MAX_IMPORT_ROWS = 2000
HEADERS = ("admission_no", "first_name", "last_name", "gender", "guardian_phone", "nin", "date_of_birth", "email")
_ALIASES = {
    "admission_no": {"admission_no", "admission number", "admission id", "admission_id", "adm no", "adm_no", "student id", "student_id", "id"},
    "first_name": {"first_name", "first name", "given name", "given_name"},
    "last_name": {"last_name", "last name", "surname", "family name", "family_name"},
    "full_name": {"full_name", "full name", "student name", "student_name", "name"},
    "gender": {"gender", "sex"},
    "guardian_phone": {"guardian_phone", "guardian phone", "parent phone", "parent_phone", "phone"},
    "nin": {"nin", "national id", "national_id"},
    "date_of_birth": {"date_of_birth", "date of birth", "dob"},
    "email": {"email", "email address", "email_address"},
}
_HEADER_LOOKUP = {alias.casefold().strip(): canonical for canonical, aliases in _ALIASES.items() for alias in aliases}


def write_student_template(path: str | Path) -> None:
    """Write a CSV/XLSX registration template; class/session/date are selected in-app."""
    write_rows(path, HEADERS, [])


def preview_student_import(path: str | Path) -> dict:
    """Validate every file row without changing database or application state."""
    headers, source_rows = read_rows(path)
    normalized = [_HEADER_LOOKUP.get(header.strip().casefold()) for header in headers]
    if any(not header for header in headers):
        raise ValueError("Every column must have a heading.")
    if any(value is None for value in normalized):
        unknown = [headers[i] for i, value in enumerate(normalized) if value is None]
        raise ValueError("Spreadsheet columns are not recognized: " + ", ".join(unknown))
    if len(normalized) != len(set(normalized)):
        raise ValueError("The file contains duplicate column headings.")
    mapped = dict(zip(headers, normalized))
    if not ({"full_name"} <= set(normalized) or {"first_name", "last_name"} <= set(normalized)):
        raise ValueError("Include a full_name column or both first_name and last_name columns.")

    rows = []
    seen_admission_numbers = set()
    for line_number, source in enumerate(source_rows, start=2):
        row = {mapped[header]: (source.get(header) or "").strip() for header in headers}
        if not any(row.values()):
            continue
        full_name = row.get("full_name", "").strip()
        if full_name:
            name_parts = full_name.split()
            if len(name_parts) < 2:
                raise ValueError(f"Row {line_number}: full name must include at least a first and last name.")
            if row.get("first_name") or row.get("last_name"):
                raise ValueError(f"Row {line_number}: use either full_name or first_name/last_name, not both.")
            row["first_name"], row["last_name"] = name_parts[0], " ".join(name_parts[1:])
        for required in ("first_name", "last_name"):
            if not row.get(required):
                raise ValueError(f"Row {line_number}: {required.replace('_', ' ')} is required.")
        admission_no = row.get("admission_no", "").strip()
        key = admission_no.casefold()
        if key and key in seen_admission_numbers:
            raise ValueError(f"Row {line_number}: admission number {admission_no!r} appears more than once.")
        if key:
            seen_admission_numbers.add(key)
        limits = (("admission_no", 100), ("first_name", 100), ("last_name", 100), ("gender", 30), ("guardian_phone", 60))
        for field, limit in limits:
            if len(row.get(field, "")) > limit:
                raise ValueError(f"Row {line_number}: {field.replace('_', ' ')} must be at most {limit} characters.")
        try:
            row["nin"], row["date_of_birth"], row["email"] = normalize_optional_student_details(
                row.get("nin", ""), row.get("date_of_birth", ""), row.get("email", "")
            )
        except ValueError as exc:
            raise ValueError(f"Row {line_number}: {exc}") from exc
        rows.append({field: row.get(field, "") for field in HEADERS})
        if len(rows) > MAX_IMPORT_ROWS:
            raise ValueError(f"A single import can contain at most {MAX_IMPORT_ROWS} students.")

    if not rows:
        raise ValueError("No student rows were found in this file.")
    return {"rows": rows, "row_count": len(rows)}
