"""Validated CSV/XLSX templates and previews for draft assessment score entry."""

from __future__ import annotations

import re
from pathlib import Path

from .database import validate_assessment_score
from .spreadsheet_files import read_rows, write_rows


def _column_code(value: object) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(value or "").strip().upper()).strip("_")


def _component_headers(components: list[dict], subject_code: str) -> dict[int, str]:
    subject = _column_code(subject_code) or "SUBJECT"
    headers, used = {}, set()
    for component in components:
        name = _column_code(component.get("component_name"))
        name = re.sub(r"_(SCORE|MARKS?)$", "", name) or f"COMPONENT_{int(component['id'])}"
        header = f"{subject}_{name}"
        if header in used:
            header = f"{header}_{int(component['id'])}"
        used.add(header)
        headers[int(component["id"])] = header
    return headers


def write_score_template(
    path: str | Path, students: list[dict], components: list[dict], subject_code: str = "SUBJECT"
) -> None:
    """Write roster IDs, readable names and current component scores to CSV/XLSX."""
    if not components:
        raise ValueError("Assessment components are not configured.")
    component_headers = _component_headers(components, subject_code)
    headers = ["admission_no", "student_name"] + list(component_headers.values())
    rows = []
    for student in students:
        name = " ".join(str(student.get(field, "")).strip() for field in ("first_name", "last_name") if student.get(field))
        scores = student.get("scores", {})
        rows.append([student.get("admission_no", ""), name] + [scores.get(component_id, "") or "" for component_id in component_headers])
    write_rows(path, headers, rows)


def preview_score_import(
    path: str | Path, students: list[dict], components: list[dict], subject_code: str = "SUBJECT"
) -> dict:
    """Validate all scores before changing draft values; admission number is the roster key."""
    if not components:
        raise ValueError("Assessment components are not configured.")
    roster = {}
    for student in students:
        key = str(student["admission_no"]).strip().casefold()
        if not key or key in roster:
            raise ValueError("The selected class roster has missing or duplicate admission numbers.")
        roster[key] = student

    headers, source_rows = read_rows(path)
    normalized = [str(header or "").strip().casefold() for header in headers]
    aliases = {"student_id": "admission_no", "adm_no": "admission_no", "student name": "student_name", "name": "student_name"}
    normalized = [aliases.get(header, header) for header in normalized]
    if len(normalized) != len(set(normalized)):
        raise ValueError("The score file contains duplicate column headings.")
    component_headers = _component_headers(components, subject_code)
    normalized_component_headers = {header.casefold(): component_id for component_id, header in component_headers.items()}
    required = {"admission_no", *normalized_component_headers}
    if "student_name" not in normalized and not {"first_name", "last_name"} <= set(normalized):
        raise ValueError("The score file must include student_name or first_name and last_name columns.")
    missing = required - set(normalized)
    expected = required | {"student_name", "first_name", "last_name"}
    unknown = set(normalized) - expected
    if missing or unknown:
        details = []
        if missing:
            details.append("missing: " + ", ".join(sorted(missing)))
        if unknown:
            details.append("unexpected: " + ", ".join(sorted(unknown)))
        raise ValueError("Score columns do not match this class, subject, and term (" + "; ".join(details) + ").")

    column_headers = dict(zip(headers, normalized))
    component_by_header = {header.casefold(): next(item for item in components if int(item["id"]) == component_id)
                           for header, component_id in normalized_component_headers.items()}
    updates, seen = {}, set()
    score_count = matched_rows = 0
    for line_number, source in enumerate(source_rows, start=2):
        row = {column_headers[header]: (source.get(header) or "").strip() for header in headers}
        if not any(row.values()):
            continue
        admission_no = row.get("admission_no", "").strip()
        key = admission_no.casefold()
        if not key:
            raise ValueError(f"Row {line_number} has scores but no student ID/admission number.")
        if key in seen:
            raise ValueError(f"Admission number {admission_no!r} appears more than once.")
        student = roster.get(key)
        if student is None:
            raise ValueError(f"Admission number {admission_no!r} on row {line_number} is not in the selected class roster.")
        seen.add(key)
        matched_rows += 1
        student_updates = {}
        for header, component in component_by_header.items():
            value = row.get(header, "").strip()
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
