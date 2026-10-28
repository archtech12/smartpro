"""Student roster import from Excel (.xlsx) or CSV, with an Excel/CSV template.

The whole file is validated first and every problem is reported together, so a school can fix
the spreadsheet once. Nothing is saved here; the caller registers the previewed rows.
"""

from __future__ import annotations

import csv
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import tabular
from .student_details import normalize_optional_student_details

MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ROWS = 2000
MAX_REPORTED_PROBLEMS = 15
HEADERS = ("admission_no", "first_name", "last_name", "gender", "guardian_phone", "nin", "date_of_birth", "email")
LABELS = {
    "first_name": "First name", "last_name": "Last name", "gender": "Gender", "guardian_phone": "Guardian phone",
    "nin": "NIN", "date_of_birth": "Date of birth", "email": "Email",
}
_ALIASES = {
    "admission_no": {"admission_no", "admission_number", "student_id", "student_no", "id"},
    "full_name": {"full_name", "student_name", "name"},
    "first_name": {"first_name", "firstname", "first", "given_name", "forename"},
    "last_name": {"last_name", "lastname", "last", "surname", "family_name"},
    "gender": {"gender", "sex"},
    "guardian_phone": {"guardian_phone", "guardian_phone_number", "phone", "phone_number", "parent_phone",
                       "parent_phone_number", "guardian_contact", "telephone", "mobile", "contact"},
    "nin": {"nin", "national_id", "national_identification_number"},
    "date_of_birth": {"date_of_birth", "dob", "birth_date", "birthday", "date_of_birth_yyyy_mm_dd"},
    "email": {"email", "email_address", "e_mail", "guardian_email", "parent_email"},
}
_CANONICAL = {alias: field for field, names in _ALIASES.items() for alias in names}
_GENDERS = {"m": "Male", "male": "Male", "boy": "Male", "f": "Female", "female": "Female", "girl": "Female"}
_SCIENTIFIC = re.compile(r"^\d+(\.\d+)?[eE]\+?\d+$")


def _heading_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")


def write_student_template(path: str | Path) -> Path:
    """Create the blank template: an Excel workbook for ``.xlsx`` paths, otherwise a UTF-8 CSV."""
    path = Path(path)
    if path.suffix.lower() == ".xlsx":
        return tabular.write_workbook(path, [
            {"name": "Students", "headers": list(HEADERS), "rows": [],
             "text_columns": ["guardian_phone", "nin", "date_of_birth"]},
            {"name": "Instructions", "headers": ["Column", "What to enter"], "rows": [
                ["admission_no", "Optional. Existing ID is preserved; leave blank to generate one."],
                ["full_name", "Required if first_name and last_name are not supplied. The student's full name."],
                ["first_name + last_name", "Alternative to full_name. Both columns are required together."],
                ["gender", "Optional. Male, Female (M or F also work)."],
                ["guardian_phone", "Optional. Keep the leading zero; the column is formatted as text."],
                ["nin", "Optional. National identification number, typed as text."],
                ["date_of_birth", "Optional. Use YYYY-MM-DD, for example 2012-03-04."],
                ["email", "Optional. A valid email address."],
                ["", "The destination class, session and enrolment date are chosen in the app."],
            ]},
        ])
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        csv.writer(handle).writerow(HEADERS)
    return path


def _clean_nin(value: str) -> str:
    """Undo Excel turning a long number into 1.2345678901E+10."""
    if _SCIENTIFIC.match(value):
        try:
            number = Decimal(value)
            if number == number.to_integral_value():
                return format(number, "f").split(".")[0]
        except InvalidOperation:
            pass
    return value


def _clean_date(value: str) -> str:
    """Accept YYYY-MM-DD plus unambiguous day/month/year text; refuse dates that could be read two ways."""
    value = value.strip()
    if not value:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    match = re.fullmatch(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})", value)
    if match:
        first, second, year = int(match[1]), int(match[2]), int(match[3])
        if first > 12 and second <= 12:
            day, month = first, second
        elif second > 12 and first <= 12:
            day, month = second, first
        elif first == second:
            day, month = first, second
        else:
            raise ValueError(f"date of birth {value} could mean day/month or month/day; write it as YYYY-MM-DD")
        try:
            return date(year, month, day).isoformat()
        except ValueError as exc:
            raise ValueError("Date of birth must use YYYY-MM-DD format.") from exc
    for pattern in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    return value  # let the shared validator explain what is wrong


def preview_student_import(path: str | Path) -> dict:
    """Validate every row of an .xlsx/.csv file without changing the database or application state."""
    table = tabular.read_table(path, MAX_IMPORT_BYTES)
    header_index = next((i for i, row in enumerate(table) if any(row)), None)
    if header_index is None:
        raise ValueError("The file is empty or has no heading row.")
    headings = table[header_index]
    keys = [_heading_key(h) for h in headings]
    while keys and not keys[-1]:
        keys.pop()  # trailing empty columns, common in saved spreadsheets
    if any(not key for key in keys):
        raise ValueError("Every column needs a heading. Fill in or delete the empty heading cells.")
    fields = [_CANONICAL.get(key) for key in keys]
    ignored = [headings[i].strip() for i, field in enumerate(fields) if field is None]
    used = [field for field in fields if field]
    if len(used) != len(set(used)):
        raise ValueError("The file contains duplicate column headings.")
    has_full_name = "full_name" in used
    has_name_parts = "first_name" in used and "last_name" in used
    if not has_full_name and not has_name_parts:
        found = ", ".join(h for h in headings if h) or "none"
        raise ValueError(
            "missing required columns: full_name or first_name and last_name" + f". The columns found were: {found}. "
            "Use the template, or name the columns first_name and last_name (First Name / Surname also work)."
        )

    rows, problems = [], []
    for offset, cells in enumerate(table[header_index + 1:], start=header_index + 2):
        row = {field: "" for field in HEADERS}
        row["full_name"] = ""
        for position, field in enumerate(fields):
            if field and position < len(cells):
                row[field] = cells[position].strip()
        if not any(row.values()):
            continue
        if row["full_name"] and not row["first_name"] and not row["last_name"]:
            name_parts = row["full_name"].split(maxsplit=1)
            if len(name_parts) == 2:
                row["first_name"], row["last_name"] = name_parts
        row["gender"] = _GENDERS.get(row["gender"].casefold(), row["gender"])
        row["nin"] = _clean_nin(row["nin"])
        try:
            problem = None
            if not row["first_name"]:
                problem = "first name is required."
            elif not row["last_name"]:
                problem = "last name is required."
            for field, limit in (("first_name", 100), ("last_name", 100), ("gender", 30), ("guardian_phone", 60)):
                if problem is None and len(row[field]) > limit:
                    problem = f"{field.replace('_', ' ')} must be at most {limit} characters."
            if problem:
                raise ValueError(problem)
            row["date_of_birth"] = _clean_date(row["date_of_birth"])
            row["nin"], row["date_of_birth"], row["email"] = normalize_optional_student_details(
                row["nin"], row["date_of_birth"], row["email"]
            )
        except ValueError as exc:
            problems.append(f"Row {offset}: {exc}")
            continue
        if len(row["admission_no"]) > 80:
            problems.append(f"Row {offset}: admission number must be at most 80 characters.")
            continue
        row.pop("full_name", None)
        rows.append(row)
        if len(rows) > MAX_IMPORT_ROWS:
            raise ValueError(f"A single import can contain at most {MAX_IMPORT_ROWS} students.")

    if problems:
        shown = problems[:MAX_REPORTED_PROBLEMS]
        extra = len(problems) - len(shown)
        raise ValueError(
            f"{len(problems)} row(s) need fixing before anything can be imported:\n" + "\n".join(shown)
            + (f"\n…and {extra} more." if extra else "")
        )
    if not rows:
        raise ValueError("No student rows were found in this file.")
    seen, duplicates, seen_ids = set(), [], set()
    for row in rows:
        key = (row["first_name"].casefold(), row["last_name"].casefold(), row["date_of_birth"])
        if key in seen:
            duplicates.append(f"{row['first_name']} {row['last_name']}")
        seen.add(key)
        admission_no = row["admission_no"].casefold()
        if admission_no and admission_no in seen_ids:
            raise ValueError(f"Admission number {row['admission_no']!r} appears more than once in the file.")
        if admission_no:
            seen_ids.add(admission_no)
    warnings = []
    if ignored:
        warnings.append("Ignored columns: " + ", ".join(ignored))
    if duplicates:
        warnings.append("Possible duplicate student(s) in the file: " + ", ".join(duplicates[:5]))
    return {"rows": rows, "row_count": len(rows), "warnings": warnings}
