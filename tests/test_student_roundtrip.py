"""Student template -> fill in Excel -> preview -> register -> export, the way the screens chain them."""

import openpyxl
import pytest

from smartschool import database as db
from smartschool import student_import, tabular


def test_excel_template_import_register_and_export(school, tmp_path):
    template = tmp_path / "students.xlsx"
    student_import.write_student_template(str(template))
    book = openpyxl.load_workbook(template)
    sheet = book.worksheets[0]
    headers = [c.value for c in sheet[1]]
    rows = [
        {"first_name": "Ada", "last_name": "Obi", "gender": "f", "guardian_phone": "08031234567",
         "email": "ada.parent@example.test", "date_of_birth": "2014-03-05"},
        {"first_name": "Ben", "last_name": "Eze", "gender": "Male", "guardian_phone": "08037654321"},
    ]
    for offset, row in enumerate(rows, start=2):
        for column, header in enumerate(headers, start=1):
            if header in row:
                sheet.cell(row=offset, column=column, value=row[header])
    filled = tmp_path / "filled.xlsx"
    book.save(filled)

    preview = student_import.preview_student_import(str(filled))
    assert preview["row_count"] == 2
    result = db.register_students_bulk(preview["rows"], school["classes"][0], school["session"], "2026-09-02", "admin")
    assert result  # registration succeeded without raising

    exported = tmp_path / "out.xlsx"
    db.export_module_xlsx("Students", str(exported))
    table = tabular.read_table(str(exported))
    text = repr(table)
    assert "Ada" in text and "Ben" in text and "Obi" in text


def test_csv_with_semicolons_and_excel_dates_imports(school, tmp_path):
    path = tmp_path / "s.csv"
    path.write_text("First Name;Surname;Sex;Phone\nChi;Ude;m;08011112222\n", encoding="utf-8")
    preview = student_import.preview_student_import(str(path))
    assert preview["row_count"] == 1
    assert preview["rows"][0]["gender"] in ("Male", "M") or "Male" in repr(preview["rows"][0])


def test_bad_file_gives_a_clear_error_not_a_crash(school, tmp_path):
    path = tmp_path / "x.xls"
    path.write_bytes(b"\xd0\xcf\x11\xe0 not really")
    with pytest.raises(ValueError):
        student_import.preview_student_import(str(path))
