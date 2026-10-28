"""CBT services: question bank, exams, student attempts, marking, monitoring (synthetic data only)."""

from datetime import datetime, timedelta

import pytest

from smartschool import cbt, cbt_runtime as rt
from smartschool import database as db
from tests.test_communications import _student

T0 = datetime(2026, 10, 27, 9, 0, 0)


def _questions(subject=None):
    return [
        cbt.create_question("admin", "mcq", "Red planet?", ["Venus", "Mars", "Jupiter"], "B", 2, subject_id=subject),
        cbt.create_question("admin", "true_false", "Sun rises in the west.", [], "false", 1),
        cbt.create_question("admin", "short_answer", "Capital of Nigeria?", [], ["Abuja", "FCT Abuja"], 2),
    ]


def _open_exam(school, **kwargs):
    exam = cbt.create_exam("admin", "Maths Test", school["classes"][0], school["session"], 30, **kwargs)
    cbt.set_exam_questions("admin", exam, _questions())
    return exam, cbt.open_exam("admin", exam)


def _answer_all(attempt, student, correct=True, now=T0):
    state = rt.attempt_state(attempt, student, now)
    by_prompt = {q["prompt"]: q for q in state["questions"]}
    mars = next(o["key"] for o in by_prompt["Red planet?"]["options"] if o["text"] == "Mars")
    answers = {"Red planet?": mars, "Sun rises in the west.": "false", "Capital of Nigeria?": "  abuja. "}
    if not correct:
        answers = {"Red planet?": "A" if mars != "A" else "B", "Sun rises in the west.": "true", "Capital of Nigeria?": "Lagos"}
    for prompt, value in answers.items():
        rt.save_answer(attempt, student, by_prompt[prompt]["id"], value, now)


def test_everything_is_admin_only(school):
    for call in (lambda: cbt.list_questions("teacher"), lambda: cbt.create_question("teacher", "true_false", "x", [], "true"),
                 lambda: cbt.list_exams("teacher"), lambda: cbt.create_exam("teacher", "t", 1, 1, 10)):
        with pytest.raises(PermissionError):
            call()


@pytest.mark.parametrize("args,message", [
    (("mcq", "Q", ["a"], "A"), "2 to 5"), (("mcq", "Q", ["a", "A"], "A"), "different"), (("mcq", "Q", ["a", "b"], "C"), "correct"),
    (("true_false", "Q", [], "maybe"), "True or False"), (("short_answer", "Q", [], ""), "accepted"),
    (("essay", "Q", [], "x"), "Choose"), (("mcq", "", ["a", "b"], "A"), "question text"),
])
def test_question_validation(school, args, message):
    with pytest.raises(ValueError, match=message):
        cbt.create_question("admin", *args)


def test_question_edit_archive_search_and_lock(school):
    qid = cbt.create_question("admin", "short_answer", "Say 100%_", [], "a|b")
    assert cbt.list_questions("admin", search="100%_")[0]["accepted"] == ["a", "b"]
    assert cbt.list_questions("admin", search="%")[0]["id"] == qid           # wildcard characters are literal
    assert cbt.list_questions("admin", search="zzz") == []
    cbt.update_question("admin", qid, "short_answer", "Say 100%", [], ["c"])
    cbt.archive_question("admin", qid)
    assert cbt.list_questions("admin") == [] and len(cbt.list_questions("admin", include_archived=True)) == 1
    cbt.archive_question("admin", qid, archived=False)
    exam = cbt.create_exam("admin", "T", school["classes"][0], school["session"], 10)
    cbt.set_exam_questions("admin", exam, [qid])
    cbt.update_question("admin", qid, "short_answer", "Still draft", [], ["c"])      # draft exam: editable
    cbt.open_exam("admin", exam)
    with pytest.raises(ValueError, match="can no longer be changed"):
        cbt.update_question("admin", qid, "short_answer", "Changed", [], ["c"])


def test_import_questions_from_excel_and_csv(school, tmp_path):
    template = tmp_path / "q.xlsx"
    cbt.write_question_template(str(template))
    preview = cbt.preview_question_import(str(template))
    assert preview["row_count"] == 3
    assert cbt.import_questions("admin", preview["rows"]) == 3
    bad = tmp_path / "bad.csv"
    bad.write_text("type,question,option_a,option_b,answer,marks\nmcq,Q1,x,y,Z,1\nmcq,,x,y,A,1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="2 row"):
        cbt.preview_question_import(str(bad))
    assert len(cbt.list_questions("admin")) == 3


def test_exam_lifecycle_rules(school):
    exam = cbt.create_exam("admin", "Draft", school["classes"][0], school["session"], 20)
    with pytest.raises(ValueError, match="Add questions"):
        cbt.open_exam("admin", exam)
    with pytest.raises(ValueError):
        cbt.create_exam("admin", "", school["classes"][0], school["session"], 20)
    with pytest.raises(ValueError):
        cbt.create_exam("admin", "T", school["classes"][0], school["session"], 0)
    cbt.set_exam_questions("admin", exam, _questions())
    code = cbt.open_exam("admin", exam)
    assert len(code) == 6 and cbt.list_exams("admin")[0]["total_marks"] == 5
    with pytest.raises(ValueError, match="draft"):
        cbt.update_exam("admin", exam, "X", school["classes"][0], school["session"], 20)
    with pytest.raises(ValueError, match="draft"):
        cbt.set_exam_questions("admin", exam, [])
    clone = cbt.duplicate_exam("admin", exam)
    assert cbt.exam_questions("admin", clone) and cbt.open_exam("admin", clone) != code
    cbt.close_exam("admin", exam)
    with pytest.raises(ValueError, match="Only a draft"):
        cbt.delete_exam("admin", exam)


def test_full_attempt_marking_and_privacy(school):
    student = _student(school, 1)
    other_class = _student(school, 2, class_index=1)
    exam, code = _open_exam(school, show_score=True)
    with pytest.raises(rt.CBTError):
        rt.identify("ADM-1", "WRONG1")
    with pytest.raises(rt.CBTError):
        rt.identify("ADM-NOPE", code)
    with pytest.raises(rt.CBTError, match="not for your class"):
        rt.identify("ADM-2", code)
    info = rt.identify("adm-1", code.lower())
    assert info["student_id"] == student and info["question_count"] == 3 and info["total_marks"] == 5
    attempt = rt.start_attempt(info["exam_id"], student, T0)
    assert rt.start_attempt(info["exam_id"], student, T0 + timedelta(minutes=5)) == attempt   # resume, same clock
    state = rt.attempt_state(attempt, student, T0 + timedelta(minutes=5))
    assert state["remaining_seconds"] == 25 * 60
    import json
    dumped = json.dumps(state)
    assert '"answer"' not in dumped and "correct" not in dumped and "Abuja" not in dumped
    _answer_all(attempt, student, now=T0 + timedelta(minutes=5))
    result = rt.submit(attempt, student, T0 + timedelta(minutes=6))
    assert result["score"] == 5 and result["max_score"] == 5
    with pytest.raises(rt.CBTError, match="already submitted"):
        rt.start_attempt(info["exam_id"], student, T0 + timedelta(minutes=7))
    with pytest.raises(rt.CBTError):
        rt.save_answer(attempt, student, 1, "A", T0 + timedelta(minutes=7))
    with pytest.raises(rt.CBTError):                       # another student's attempt id is not accessible
        rt.attempt_state(attempt, other_class, T0)


def test_wrong_answers_score_zero_and_hidden_score(school):
    student = _student(school, 3)
    exam, code = _open_exam(school)
    attempt = rt.start_attempt(exam, student, T0)
    _answer_all(attempt, student, correct=False)
    assert rt.submit(attempt, student, T0 + timedelta(minutes=1)) == {"title": "Maths Test", "show_score": False, "auto_submitted": False}
    assert cbt.exam_results("admin", exam)["rows"][0]["score"] == 0


def test_server_clock_auto_submits_and_refuses_late_answers(school):
    student = _student(school, 4)
    exam, _ = _open_exam(school)
    attempt = rt.start_attempt(exam, student, T0)
    _answer_all(attempt, student, now=T0 + timedelta(minutes=1))
    late = T0 + timedelta(minutes=31)
    with pytest.raises(rt.CBTError) as caught:
        rt.save_answer(attempt, student, 1, "A", late)
    assert caught.value.code == "time_up"
    final = rt.attempt_state(attempt, student, late)
    assert final["finished"] and final["auto_submitted"]
    row = cbt.monitor("admin", exam, late)["students"][0]
    assert row["state"] == "submitted" and row["score"] == 5 and row["submitted_at"] == "2026-10-27T09:30:00"


def test_monitor_sweep_close_and_reset(school):
    students = [_student(school, n) for n in (5, 6, 7)]
    exam, code = _open_exam(school)
    a5 = rt.start_attempt(exam, students[0], T0)
    rt.start_attempt(exam, students[1], T0 + timedelta(minutes=10))
    _answer_all(a5, students[0], now=T0 + timedelta(minutes=1))
    rt.record_focus_loss(a5, students[0])
    view = cbt.monitor("admin", exam, T0 + timedelta(minutes=20))
    assert view["counts"] == {"not_started": 1, "in_progress": 2, "submitted": 0}
    assert [s["focus_losses"] for s in view["students"] if s["student_id"] == students[0]] == [1]
    swept = cbt.monitor("admin", exam, T0 + timedelta(minutes=31))     # first attempt timed out, second still running
    assert swept["counts"] == {"not_started": 1, "in_progress": 1, "submitted": 1}
    assert cbt.close_exam("admin", exam, T0 + timedelta(minutes=35)) == 1
    with pytest.raises(rt.CBTError):
        rt.identify("ADM-7", code)                                      # closed exams cannot be entered
    cbt.reset_attempt("admin", exam, students[0])
    assert cbt.exam_results("admin", exam)["stats"]["submitted"] == 1
    with pytest.raises(ValueError, match="no attempt"):
        cbt.reset_attempt("admin", exam, students[2])


def test_results_statistics_analysis_and_export(school, tmp_path):
    first, second = _student(school, 8), _student(school, 9)
    exam, _ = _open_exam(school)
    for student, good in ((first, True), (second, False)):
        attempt = rt.start_attempt(exam, student, T0)
        _answer_all(attempt, student, correct=good)
        rt.submit(attempt, student, T0 + timedelta(minutes=2))
    results = cbt.exam_results("admin", exam)
    assert [r["percent"] for r in results["rows"]] == [100.0, 0.0] and results["stats"]["average"] == 50.0
    assert [q["percent_correct"] for q in cbt.question_analysis("admin", exam)] == [50.0, 50.0, 50.0]
    path = cbt.export_results_xlsx("admin", exam, str(tmp_path / "r.xlsx"))
    import openpyxl
    sheet = openpyxl.load_workbook(path).active
    assert [c.value for c in sheet[1]][:3] == ["Admission No", "Name", "Score"] and sum(1 for r in sheet.iter_rows(values_only=True) if any(r)) == 3


def test_v27_database_upgrades_with_cbt_tables(school):
    with db.connection() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"cbt_questions", "cbt_exams", "cbt_exam_questions", "cbt_attempts", "cbt_answers"} <= tables


def test_form_options_lists_classes_sessions_subjects(school):
    options = cbt.form_options("admin")
    assert len(options["classes"]) == 3 and options["sessions"][0][2] is True
    with pytest.raises(PermissionError):
        cbt.form_options("teacher")
