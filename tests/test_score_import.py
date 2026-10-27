import csv

import pytest

from smartschool.score_import import preview_score_import, write_score_template


def _students():
    return [
        {"enrollment_id": 21, "admission_no": "A-001", "first_name": "Amina", "last_name": "One"},
        {"enrollment_id": 22, "admission_no": "A-002", "first_name": "Musa", "last_name": "Two"},
    ]


def _components():
    return [
        {"id": 4, "component_name": "Coursework", "max_score": "40"},
        {"id": 9, "component_name": "End Term Exam", "max_score": "60"},
    ]


def _write_csv(path, rows):
    headers = [
        "admission_no", "first_name", "last_name",
        "SUBJECT_COURSEWORK", "SUBJECT_END_TERM_EXAM",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def test_score_template_uses_admission_number_and_current_roster(tmp_path):
    path = tmp_path / "scores.csv"
    write_score_template(path, _students(), _components())
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["admission_no"] == "A-001"
    assert rows[0]["student_name"] == "Amina One"
    assert rows[0]["SUBJECT_COURSEWORK"] == ""
    assert rows[1]["SUBJECT_END_TERM_EXAM"] == ""


def test_score_template_uses_short_subject_component_codes(tmp_path):
    path = tmp_path / "scores.csv"
    write_score_template(
        path, _students(), [{"id": 4, "component_name": "CA Score", "max_score": "20"}], "MTH"
    )
    with path.open(newline="", encoding="utf-8-sig") as handle:
        assert next(csv.reader(handle)) == ["admission_no", "student_name", "MTH_CA"]


def test_score_import_uses_selected_subject_code_in_short_headers(tmp_path):
    path = tmp_path / "scores.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["admission_no", "first_name", "last_name", "MTH_CA"])
        writer.writerow(["A-001", "Amina", "One", "18"])
    result = preview_score_import(
        path, _students(), [{"id": 4, "component_name": "CA Score", "max_score": "20"}], "MTH"
    )
    assert result["updates"] == {21: {4: "18"}}


def test_score_import_validates_and_returns_partial_scores_without_blank_clears(tmp_path):
    path = tmp_path / "scores.csv"
    _write_csv(path, [
        {"admission_no": "a-001", "first_name": "Wrong name is not the key", "last_name": "", "SUBJECT_COURSEWORK": "12.50", "SUBJECT_END_TERM_EXAM": ""},
        {"admission_no": "A-002", "first_name": "Musa", "last_name": "Two", "SUBJECT_COURSEWORK": "", "SUBJECT_END_TERM_EXAM": "55"},
    ])
    result = preview_score_import(path, _students(), _components())
    assert result == {
        "updates": {21: {4: "12.5"}, 22: {9: "55"}},
        "matched_rows": 2,
        "score_count": 2,
    }


@pytest.mark.parametrize("rows, message", [
    ([{"admission_no": "A-001", "first_name": "Amina", "last_name": "One", "SUBJECT_COURSEWORK": "41", "SUBJECT_END_TERM_EXAM": ""}], "maximum"),
    ([{"admission_no": "A-999", "first_name": "Unknown", "last_name": "Student", "SUBJECT_COURSEWORK": "5", "SUBJECT_END_TERM_EXAM": ""}], "not in the selected class"),
    ([{"admission_no": "A-001", "first_name": "Amina", "last_name": "One", "SUBJECT_COURSEWORK": "5", "SUBJECT_END_TERM_EXAM": ""}, {"admission_no": "a-001", "first_name": "Amina", "last_name": "One", "SUBJECT_COURSEWORK": "6", "SUBJECT_END_TERM_EXAM": ""}], "more than once"),
])
def test_score_import_rejects_invalid_roster_or_scores_without_partial_result(tmp_path, rows, message):
    path = tmp_path / "scores.csv"
    _write_csv(path, rows)
    with pytest.raises(ValueError, match=message):
        preview_score_import(path, _students(), _components())


def test_score_import_rejects_template_from_different_assessment_setup(tmp_path):
    path = tmp_path / "scores.csv"
    _write_csv(path, [{"admission_no": "A-001", "first_name": "Amina", "last_name": "One", "SUBJECT_COURSEWORK": "5", "SUBJECT_END_TERM_EXAM": ""}])
    with pytest.raises(ValueError, match="columns do not match"):
        preview_score_import(path, _students(), [{**_components()[0], "component_name": "Classwork"}, _components()[1]])


def test_score_template_neutralizes_spreadsheet_formulas(tmp_path):
    path = tmp_path / "scores.csv"
    write_score_template(
        path,
        [{"enrollment_id": 1, "admission_no": "=1+1", "first_name": "@SUM(A1:A2)", "last_name": "School"}],
        _components(),
    )
    with path.open(newline="", encoding="utf-8-sig") as handle:
        row = next(csv.DictReader(handle))
    assert row["admission_no"] == "'=1+1"
    assert row["student_name"] == "'@SUM(A1:A2) School"


def test_excel_score_template_uses_id_name_ca_exam_headers_and_imports_scores(tmp_path):
    from smartschool.spreadsheet_files import read_rows, write_rows

    path = tmp_path / "scores.xlsx"
    components = [
        {"id": 4, "component_name": "CA Score", "max_score": "40"},
        {"id": 9, "component_name": "Exam Score", "max_score": "60"},
    ]
    write_score_template(path, _students(), components, "MTH")
    headers, rows = read_rows(path)
    assert headers == ["admission_no", "student_name", "MTH_CA", "MTH_EXAM"]
    assert rows[0]["admission_no"] == "A-001"
    assert rows[0]["student_name"] == "Amina One"
    write_rows(path, headers, [["A-001", "Amina One", "32", "55"]])
    result = preview_score_import(path, _students(), components, "MTH")
    assert result["updates"] == {21: {4: "32", 9: "55"}}
