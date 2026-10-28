import csv

import pytest

from smartschool.student_import import preview_student_import, write_student_template


def _write_csv(path, headers, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def test_student_template_is_blank_and_has_expected_headers(tmp_path):
    path = tmp_path / "students.csv"
    write_student_template(path)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        assert next(csv.reader(handle)) == ["admission_no", "first_name", "last_name", "gender", "guardian_phone", "nin", "date_of_birth", "email"]


def test_student_import_accepts_optional_fields_and_skips_blank_lines(tmp_path):
    path = tmp_path / "students.csv"
    _write_csv(path, [" FIRST_NAME ", "last_name", "gender", "guardian_phone"], [
        {" FIRST_NAME ": " Amina ", "last_name": " Okafor ", "gender": "Female", "guardian_phone": "555-0101"},
        {" FIRST_NAME ": "Musa", "last_name": "Bello", "gender": "", "guardian_phone": ""},
        {" FIRST_NAME ": "", "last_name": "", "gender": "", "guardian_phone": ""},
    ])
    assert preview_student_import(path) == {
        "row_count": 2, "warnings": [],
        "rows": [
            {"admission_no": "", "first_name": "Amina", "last_name": "Okafor", "gender": "Female", "guardian_phone": "555-0101", "nin": "", "date_of_birth": "", "email": ""},
            {"admission_no": "", "first_name": "Musa", "last_name": "Bello", "gender": "", "guardian_phone": "", "nin": "", "date_of_birth": "", "email": ""},
        ],
    }


def test_student_import_accepts_optional_nin_birth_date_and_email(tmp_path):
    path = tmp_path / "students.csv"
    _write_csv(path, ["first_name", "last_name", "nin", "date_of_birth", "email"], [
        {"first_name": "Amina", "last_name": "Okafor", "nin": "NIN-001", "date_of_birth": "2012-03-04", "email": "amina@example.org"},
    ])
    assert preview_student_import(path)["rows"][0] == {
        "admission_no": "", "first_name": "Amina", "last_name": "Okafor", "gender": "", "guardian_phone": "",
        "nin": "NIN-001", "date_of_birth": "2012-03-04", "email": "amina@example.org",
    }


@pytest.mark.parametrize(("headers", "rows", "message"), [
    (["first_name", "gender"], [{"first_name": "Amina", "gender": "Female"}], "missing required"),
    (["first_name", "last_name", "FIRST_NAME"], [{"first_name": "Amina", "last_name": "Okafor", "FIRST_NAME": "A"}], "duplicate column"),
    (["first_name", "last_name"], [{"first_name": "", "last_name": "Okafor"}], "first name is required"),
    (["first_name", "last_name"], [{"first_name": "A" * 101, "last_name": "Okafor"}], "at most 100 characters"),
    (["first_name", "last_name", "email"], [{"first_name": "Amina", "last_name": "Okafor", "email": "not-an-email"}], "valid email"),
    (["first_name", "last_name", "date_of_birth"], [{"first_name": "Amina", "last_name": "Okafor", "date_of_birth": "2012-02-30"}], "YYYY-MM-DD"),
])
def test_student_import_rejects_invalid_files_without_partial_preview(tmp_path, headers, rows, message):
    path = tmp_path / "invalid.csv"
    _write_csv(path, headers, rows)
    with pytest.raises(ValueError, match=message):
        preview_student_import(path)


def test_student_import_supports_full_name_and_preserves_optional_admission_id(tmp_path):
    path = tmp_path / "roster.csv"
    _write_csv(path, ["Student ID", "Student Name", "Gender"], [
        {"Student ID": "SCH-2026-10", "Student Name": "Amina Okafor", "Gender": "F"},
        {"Student ID": "", "Student Name": "Musa Bello", "Gender": "M"},
    ])
    result = preview_student_import(path)
    assert result["rows"] == [
        {"admission_no": "SCH-2026-10", "first_name": "Amina", "last_name": "Okafor", "gender": "Female", "guardian_phone": "", "nin": "", "date_of_birth": "", "email": ""},
        {"admission_no": "", "first_name": "Musa", "last_name": "Bello", "gender": "Male", "guardian_phone": "", "nin": "", "date_of_birth": "", "email": ""},
    ]
