"""Strict CSV template and preview validation for bulk student registration."""

from __future__ import annotations

import csv
from pathlib import Path
from .student_details import normalize_optional_student_details

MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_IMPORT_ROWS = 2000
HEADERS = ("first_name", "last_name", "gender", "guardian_phone", "nin", "date_of_birth", "email")


def write_student_template(path: str | Path) -> None:
    """Create a blank UTF-8 CSV template; class, session, and date are chosen in the app."""
    with Path(path).open("w", newline="", encoding="utf-8-sig") as handle:
        csv.writer(handle).writerow(HEADERS)


def preview_student_import(path: str | Path) -> dict:
    """Validate all input rows without changing database or application state."""
    path = Path(path)
    try:
        if path.stat().st_size > MAX_IMPORT_BYTES:
            raise ValueError("Student CSV files must be 5 MiB or smaller.")
        with path.open("r", newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            headers = reader.fieldnames
            if not headers:
                raise ValueError("The CSV file is empty or has no header row.")
            normalized_headers = [header.strip().casefold() if header else "" for header in headers]
            if any(not header for header in normalized_headers):
                raise ValueError("Every CSV column must have a heading.")
            if len(normalized_headers) != len(set(normalized_headers)):
                raise ValueError("The CSV file contains duplicate column headings.")
            missing = [header for header in HEADERS[:2] if header not in normalized_headers]
            unknown = [header for header in normalized_headers if header and header not in HEADERS]
            if missing or unknown:
                details = []
                if missing:
                    details.append("missing required columns: " + ", ".join(missing))
                if unknown:
                    details.append("unexpected columns: " + ", ".join(unknown))
                raise ValueError("CSV columns are not recognized (" + "; ".join(details) + ").")

            rows = []
            for line_number, source in enumerate(reader, start=2):
                if None in source:
                    raise ValueError(f"Row {line_number} has more fields than the header.")
                row = {}
                for index, header in enumerate(headers):
                    key = normalized_headers[index]
                    row[key] = (source.get(header) or "").strip()
                if not any(row.values()):
                    continue
                for required in HEADERS[:2]:
                    if not row.get(required):
                        raise ValueError(f"Row {line_number}: {required.replace('_', ' ')} is required.")
                limits = (("first_name", 100), ("last_name", 100), ("gender", 30), ("guardian_phone", 60))
                for field, limit in limits:
                    if len(row.get(field, "")) > limit:
                        raise ValueError(
                            f"Row {line_number}: {field.replace('_', ' ')} must be at most {limit} characters."
                        )
                try:
                    row["nin"], row["date_of_birth"], row["email"] = normalize_optional_student_details(
                        row.get("nin", ""), row.get("date_of_birth", ""), row.get("email", "")
                    )
                except ValueError as exc:
                    raise ValueError(f"Row {line_number}: {exc}") from exc
                rows.append({field: row.get(field, "") for field in HEADERS})
                if len(rows) > MAX_IMPORT_ROWS:
                    raise ValueError(f"A single import can contain at most {MAX_IMPORT_ROWS} students.")
    except UnicodeDecodeError as exc:
        raise ValueError("The student file must be a UTF-8 CSV file.") from exc
    except csv.Error as exc:
        raise ValueError(f"The CSV file could not be read: {exc}") from exc

    if not rows:
        raise ValueError("No student rows were found in this file.")
    return {"rows": rows, "row_count": len(rows)}
