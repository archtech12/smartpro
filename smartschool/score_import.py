"""Validated CSV templates and previews for draft assessment score entry."""

from __future__ import annotations

import csv
import re
from pathlib import Path

from . import tabular
from .database import validate_assessment_score


def _column_code(value: object) -> str:
    """Convert a subject or component label into a compact spreadsheet column code."""
    return re.sub(r"[^A-Z0-9]+", "_", str(value or "").strip().upper()).strip("_")


def _component_headers(components: list[dict], subject_code: str) -> dict[int, str]:
    subject = _column_code(subject_code) or "SUBJECT"
    headers = {}
    used = set()
    for component in components:
        name = _column_code(component.get("component_name"))
        name = re.sub(r"_(SCORE|MARKS?)$", "", name) or f"COMPONENT_{int(component['id'])}"
        header = f"{subject}_{name}"
        if header in used:
            header = f"{header}_{int(component['id'])}"
        used.add(header)
        headers[int(component["id"])] = header
    return headers


def _safe_spreadsheet_text(value: object) -> str:
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def write_score_template(
    path: str | Path, students: list[dict], components: list[dict], subject_code: str = "SUBJECT"
) -> None:
    """Write the current class roster and any scores currently displayed in the editor."""
    if not components:
        raise ValueError("Assessment components are not configured.")
    component_headers = _component_headers(components, subject_code)
    headers = ["admission_no", "first_name", "last_name"] + list(component_headers.values())
    if Path(path).suffix.lower() == ".xlsx":
        rows = []
        for student in students:
            scores = student.get("scores", {})
            rows.append(
                [student.get("admission_no", ""), student.get("first_name", ""), student.get("last_name", "")]
                + [scores.get(int(component["id"]), "") or "" for component in components]
            )
        tabular.write_workbook(path, [{"name": "Scores", "headers": headers, "rows": rows}])
        return
    with Path(path).open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for student in students:
            row = {
                "admission_no": _safe_spreadsheet_text(student.get("admission_no", "")),
                "first_name": _safe_spreadsheet_text(student.get("first_name", "")),
                "last_name": _safe_spreadsheet_text(student.get("last_name", "")),
            }
            scores = student.get("scores", {})
            for component in components:
                row[component_headers[int(component["id"])]] = scores.get(int(component["id"]), "") or ""
            writer.writerow(row)


def preview_score_import(
    path: str | Path, students: list[dict], components: list[dict], subject_code: str = "SUBJECT"
) -> dict:
    """Validate a whole score file without changing any application or database state.

    Admission number is the only roster key. Blank score cells are omitted from the
    returned updates, so importing a partially filled file cannot erase existing marks.
    """
    if not components:
        raise ValueError("Assessment components are not configured.")
    roster = {}
    for student in students:
        key = str(student["admission_no"]).strip().casefold()
        if not key or key in roster:
            raise ValueError("The selected class roster has missing or duplicate admission numbers.")
        roster[key] = student

    component_headers = _component_headers(components, subject_code)
    expected = ["admission_no", "first_name", "last_name"] + list(component_headers.values())
    component_by_header = {
        component_headers[int(component["id"])]: component for component in components
    }
    table = tabular.read_table(path)
    header_index = next((i for i, row in enumerate(table) if any(row)), None)
    if header_index is None:
        raise ValueError("The file is empty or has no heading row.")
    headers = [h.strip() for h in table[header_index]]
    while headers and not headers[-1]:
        headers.pop()
    lookup = {h.casefold(): i for i, h in enumerate(headers) if h}
    if len(lookup) != len([h for h in headers if h]):
        raise ValueError("The file contains duplicate column headings.")
    missing = [header for header in expected if header.casefold() not in lookup]
    unknown = [h for h in headers if h and h.casefold() not in {e.casefold() for e in expected}]
    if missing or unknown:
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unknown:
            details.append("unexpected: " + ", ".join(unknown))
        raise ValueError("The columns do not match this class, subject, and term (" + "; ".join(details) + "). Export the template again.")

    def cell(cells, header):
        position = lookup[header.casefold()]
        return cells[position].strip() if position < len(cells) else ""

    updates = {}
    seen = set()
    score_count = 0
    matched_rows = 0
    for line_number, cells in enumerate(table[header_index + 1:], start=header_index + 2):
        if not any(c.strip() for c in cells):
            continue
        admission_no = cell(cells, "admission_no")
        key = admission_no.casefold()
        if not key:
            raise ValueError(f"Row {line_number} has scores but no admission number.")
        if key in seen:
            raise ValueError(f"Admission number {admission_no!r} appears more than once.")
        student = roster.get(key)
        if student is None:
            raise ValueError(
                f"Admission number {admission_no!r} on row {line_number} is not in the selected class roster."
            )
        seen.add(key)
        matched_rows += 1
        student_updates = {}
        for header, component in component_by_header.items():
            value = cell(cells, header)
            if value:
                try:
                    student_updates[int(component["id"])] = validate_assessment_score(value, component["max_score"])
                except ValueError as exc:
                    raise ValueError(f"Row {line_number}, {header}: {exc}") from exc
        if student_updates:
            updates[int(student["enrollment_id"])] = student_updates
            score_count += len(student_updates)

    if not updates:
        raise ValueError("No non-blank scores were found. Nothing was imported.")
    return {"updates": updates, "matched_rows": matched_rows, "score_count": score_count}
