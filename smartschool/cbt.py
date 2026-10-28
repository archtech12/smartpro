"""Computer-based tests, staff side (Qt-free): question bank, exams, monitoring, results.

Students never use this module; the browser server uses ``cbt_runtime``. Only administrators author exams.
Correct answers are stored here and are never sent to a student's browser.
"""

import json
import re
import secrets
from datetime import datetime, timezone

from . import database as db
from . import tabular

QTYPES = {"mcq": "Multiple choice", "true_false": "True / False", "short_answer": "Short answer"}
OPTION_LETTERS = "ABCDE"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid mix-ups on a lab screen
MAX_QUESTIONS_PER_EXAM = 300
IMPORT_HEADERS = ("type", "question", "option_a", "option_b", "option_c", "option_d", "option_e", "answer", "marks", "explanation")


def _admin(conn, actor):
    return db._require_admin(conn, actor)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)


def iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat(timespec="seconds")


def normalise_short(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().casefold()).strip(" .,;:!?")


def _int(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be a whole number from {low} to {high}.")
    return value


# ------------------------------------------------------------------ questions
def clean_question(qtype, prompt, options, answer, marks=1, explanation="") -> dict:
    """Validate a question and return the values to store."""
    if qtype not in QTYPES:
        raise ValueError("Choose Multiple choice, True / False or Short answer.")
    prompt = (prompt or "").strip()
    if not 1 <= len(prompt) <= 2000:
        raise ValueError("Enter the question text (up to 2000 characters).")
    marks = _int(marks, "Marks", 1, 100)
    explanation = (explanation or "").strip()
    if len(explanation) > 1000:
        raise ValueError("The explanation is limited to 1000 characters.")
    stored_options: list[str] = []
    if qtype == "mcq":
        stored_options = [str(o).strip() for o in (options or []) if str(o).strip()]
        if not 2 <= len(stored_options) <= 5:
            raise ValueError("A multiple-choice question needs 2 to 5 answer options.")
        if any(len(o) > 300 for o in stored_options):
            raise ValueError("Each option is limited to 300 characters.")
        if len({o.casefold() for o in stored_options}) != len(stored_options):
            raise ValueError("Answer options must all be different.")
        letter = str(answer or "").strip().upper()
        if letter not in OPTION_LETTERS[: len(stored_options)]:
            raise ValueError("Select which option is correct.")
        stored_answer = letter
    elif qtype == "true_false":
        value = str(answer or "").strip().lower()
        if value in ("t", "true", "yes"):
            stored_answer = "true"
        elif value in ("f", "false", "no"):
            stored_answer = "false"
        else:
            raise ValueError("Select whether the statement is True or False.")
    else:
        accepted = answer if isinstance(answer, (list, tuple)) else re.split(r"[|\n]", str(answer or ""))
        cleaned = []
        for item in accepted:
            text = re.sub(r"\s+", " ", str(item).strip())
            if text and normalise_short(text) not in {normalise_short(c) for c in cleaned}:
                cleaned.append(text)
        if not 1 <= len(cleaned) <= 10 or any(len(c) > 100 for c in cleaned):
            raise ValueError("Enter 1 to 10 accepted answers (each up to 100 characters).")
        stored_answer = json.dumps(cleaned, ensure_ascii=False)
    return {"qtype": qtype, "prompt": prompt, "options_json": json.dumps(stored_options, ensure_ascii=False),
            "answer": stored_answer, "marks": marks, "explanation": explanation}


def _question_dict(row) -> dict:
    result = dict(row)
    result["options"] = json.loads(result.pop("options_json") or "[]")
    result["accepted"] = json.loads(result["answer"]) if result["qtype"] == "short_answer" else []
    result["answer_text"] = " | ".join(result["accepted"]) if result["accepted"] else result["answer"]
    return result


def _check_subject(conn, subject_id):
    if subject_id is None:
        return None
    if isinstance(subject_id, bool) or not isinstance(subject_id, int) or not conn.execute(
            "SELECT 1 FROM subjects WHERE id=?", (subject_id,)).fetchone():
        raise ValueError("Select an existing subject.")
    return subject_id


def create_question(actor, qtype, prompt, options, answer, marks=1, explanation="", subject_id=None) -> int:
    values = clean_question(qtype, prompt, options, answer, marks, explanation)
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        subject_id = _check_subject(conn, subject_id)
        cursor = conn.execute(
            "INSERT INTO cbt_questions(subject_id,qtype,prompt,options_json,answer,marks,explanation,created_by) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (subject_id, values["qtype"], values["prompt"], values["options_json"], values["answer"], values["marks"],
             values["explanation"], actor))
        db._audit_in_transaction(conn, actor, "CREATE", "cbt_questions", cursor.lastrowid)
        return int(cursor.lastrowid)


def _locked_by_live_exam(conn, question_id) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM cbt_exam_questions q JOIN cbt_exams e ON e.id=q.exam_id WHERE q.question_id=? AND e.status<>'draft'",
        (question_id,)).fetchone())


def update_question(actor, question_id, qtype, prompt, options, answer, marks=1, explanation="", subject_id=None) -> None:
    values = clean_question(qtype, prompt, options, answer, marks, explanation)
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        if not conn.execute("SELECT 1 FROM cbt_questions WHERE id=?", (question_id,)).fetchone():
            raise ValueError("Select an existing question.")
        if _locked_by_live_exam(conn, question_id):
            raise ValueError("This question is in an exam that has been opened, so it can no longer be changed. "
                             "Archive it and add a corrected copy instead.")
        subject_id = _check_subject(conn, subject_id)
        conn.execute(
            "UPDATE cbt_questions SET subject_id=?,qtype=?,prompt=?,options_json=?,answer=?,marks=?,explanation=?,"
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (subject_id, values["qtype"], values["prompt"], values["options_json"], values["answer"], values["marks"],
             values["explanation"], question_id))
        db._audit_in_transaction(conn, actor, "UPDATE", "cbt_questions", question_id)


def archive_question(actor, question_id, archived=True) -> None:
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        if conn.execute("UPDATE cbt_questions SET is_archived=? WHERE id=?", (1 if archived else 0, question_id)).rowcount != 1:
            raise ValueError("Select an existing question.")
        db._audit_in_transaction(conn, actor, "ARCHIVE" if archived else "RESTORE", "cbt_questions", question_id)


def list_questions(actor, subject_id=None, qtype=None, search="", include_archived=False, limit=1000) -> list[dict]:
    where, params = [], []
    if not include_archived:
        where.append("q.is_archived=0")
    if subject_id is not None:
        where.append("q.subject_id=?")
        params.append(subject_id)
    if qtype:
        if qtype not in QTYPES:
            raise ValueError("Choose a valid question type.")
        where.append("q.qtype=?")
        params.append(qtype)
    if (search or "").strip():
        where.append("q.prompt LIKE ? ESCAPE '\\'")
        params.append("%" + search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    limit = max(1, min(int(limit), 5000))
    with db.connection() as conn:
        _admin(conn, actor)
        rows = conn.execute(
            "SELECT q.*,s.subject_name,(SELECT COUNT(*) FROM cbt_exam_questions x WHERE x.question_id=q.id) AS used_in "
            "FROM cbt_questions q LEFT JOIN subjects s ON s.id=q.subject_id"
            + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY q.id DESC LIMIT ?", (*params, limit)).fetchall()
    return [_question_dict(r) for r in rows]


def write_question_template(path: str) -> str:
    sample = [IMPORT_HEADERS,
              ("mcq", "Which planet is known as the Red Planet?", "Venus", "Mars", "Jupiter", "Saturn", "", "B", "1", "Mars looks red."),
              ("true_false", "The sun rises in the west.", "", "", "", "", "", "false", "1", ""),
              ("short_answer", "What is the capital of Nigeria?", "", "", "", "", "", "Abuja", "2", "")]
    instructions = [("Column", "What to enter"),
                    ("type", "mcq, true_false or short_answer"),
                    ("question", "The question text (required)"),
                    ("option_a … option_e", "Answer options for mcq (2 to 5)"),
                    ("answer", "mcq: the correct letter A-E. true_false: true or false. short_answer: accepted answers separated by |"),
                    ("marks", "Whole number 1-100 (default 1)"),
                    ("explanation", "Optional note shown to staff")]
    if str(path).lower().endswith(".csv"):
        import csv
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            csv.writer(handle).writerows(sample)
        return str(path)
    tabular.write_workbook(path, [
        {"name": "Questions", "headers": list(IMPORT_HEADERS), "rows": [list(r) for r in sample[1:]],
         "text_columns": list(IMPORT_HEADERS)},
        {"name": "Instructions", "headers": list(instructions[0]), "rows": [list(r) for r in instructions[1:]]}])
    return str(path)


def preview_question_import(path: str) -> dict:
    """Read and validate a spreadsheet of questions. Returns ``{"rows": [...], "row_count": n}`` or raises ValueError listing problems."""
    table = tabular.read_table(path)
    start = next((i for i, row in enumerate(table) if any(str(c or "").strip() for c in row)), None)
    if start is None:
        raise ValueError("The file is empty or has no heading row.")
    header_row, data = table[start], table[start + 1:]
    index = {re.sub(r"[^a-z_]", "", str(h).strip().lower().replace(" ", "_")): i for i, h in enumerate(header_row)}
    aliases = {"q": "question", "text": "question", "prompt": "question", "correct": "answer", "mark": "marks", "points": "marks"}
    for alias, real in aliases.items():
        if alias in index and real not in index:
            index[real] = index[alias]
    if "question" not in index or "answer" not in index:
        raise ValueError("The file needs at least 'question' and 'answer' columns. Download the Excel template for the layout.")

    def cell(row, name):
        i = index.get(name)
        return str(row[i]).strip() if i is not None and i < len(row) and row[i] is not None else ""

    rows, problems = [], []
    for number, row in enumerate(data, start=2):
        if not any(str(c or "").strip() for c in row):
            continue
        kind = cell(row, "type").lower().replace(" ", "_").replace("/", "_").replace("-", "_") or "mcq"
        kind = {"multiple_choice": "mcq", "objective": "mcq", "tf": "true_false", "truefalse": "true_false",
                "short": "short_answer", "fill": "short_answer"}.get(kind, kind)
        options = [cell(row, f"option_{c}") for c in "abcde"]
        try:
            marks_text = cell(row, "marks") or "1"
            marks = int(float(marks_text))
            values = clean_question(kind, cell(row, "question"), options, cell(row, "answer"), marks, cell(row, "explanation"))
            rows.append({**values, "row": number})
        except ValueError as exc:
            problems.append(f"Row {number}: {exc}")
    if len(rows) + len(problems) > 1000:
        raise ValueError("A single import can contain at most 1000 questions.")
    if problems:
        shown = problems[:15]
        raise ValueError(f"{len(problems)} row(s) need fixing before anything is imported:\n" + "\n".join(shown)
                         + (f"\n…and {len(problems) - 15} more." if len(problems) > 15 else ""))
    if not rows:
        raise ValueError("No questions were found in this file.")
    return {"rows": rows, "row_count": len(rows)}


def import_questions(actor, rows: list[dict], subject_id=None) -> int:
    if not rows or len(rows) > 1000:
        raise ValueError("Import between 1 and 1000 questions.")
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        subject_id = _check_subject(conn, subject_id)
        for row in rows:
            values = clean_question(row["qtype"], row["prompt"], json.loads(row["options_json"]),
                                    json.loads(row["answer"]) if row["qtype"] == "short_answer" else row["answer"],
                                    row["marks"], row.get("explanation", ""))
            conn.execute(
                "INSERT INTO cbt_questions(subject_id,qtype,prompt,options_json,answer,marks,explanation,created_by) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (subject_id, values["qtype"], values["prompt"], values["options_json"], values["answer"], values["marks"],
                 values["explanation"], actor))
        db._audit_in_transaction(conn, actor, "IMPORT", "cbt_questions", f"{len(rows)} rows")
    return len(rows)


# ---------------------------------------------------------------------- exams
def _new_code(conn) -> str:
    for _ in range(50):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if not conn.execute("SELECT 1 FROM cbt_exams WHERE status='open' AND access_code=? COLLATE NOCASE", (code,)).fetchone():
            return code
    raise RuntimeError("Could not create a unique access code.")


def _exam_row(conn, exam_id):
    row = conn.execute("SELECT * FROM cbt_exams WHERE id=?", (exam_id,)).fetchone() if isinstance(exam_id, int) and not isinstance(exam_id, bool) else None
    if row is None:
        raise ValueError("Select an existing exam.")
    return row


def _validate_exam_fields(conn, title, class_id, academic_session_id, duration_minutes, subject_id, instructions):
    title = " ".join((title or "").split())
    if not 1 <= len(title) <= 120:
        raise ValueError("Enter an exam title (up to 120 characters).")
    _int(duration_minutes, "Duration (minutes)", 1, 300)
    if not conn.execute("SELECT 1 FROM classes WHERE id=?", (class_id,)).fetchone():
        raise ValueError("Select an existing class.")
    if not conn.execute("SELECT 1 FROM academic_sessions WHERE id=?", (academic_session_id,)).fetchone():
        raise ValueError("Select an existing academic session.")
    _check_subject(conn, subject_id)
    instructions = (instructions or "").strip()
    if len(instructions) > 1000:
        raise ValueError("Instructions are limited to 1000 characters.")
    return title, instructions


def create_exam(actor, title, class_id, academic_session_id, duration_minutes, subject_id=None, shuffle_questions=True,
                shuffle_options=True, show_score=False, instructions="") -> int:
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        title, instructions = _validate_exam_fields(conn, title, class_id, academic_session_id, duration_minutes,
                                                    subject_id, instructions)
        cursor = conn.execute(
            "INSERT INTO cbt_exams(title,class_id,subject_id,academic_session_id,duration_minutes,access_code,"
            "shuffle_questions,shuffle_options,show_score,instructions,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (title, class_id, subject_id, academic_session_id, duration_minutes, _new_code(conn),
             int(bool(shuffle_questions)), int(bool(shuffle_options)), int(bool(show_score)), instructions, actor))
        db._audit_in_transaction(conn, actor, "CREATE", "cbt_exams", cursor.lastrowid)
        return int(cursor.lastrowid)


def update_exam(actor, exam_id, title, class_id, academic_session_id, duration_minutes, subject_id=None,
                shuffle_questions=True, shuffle_options=True, show_score=False, instructions="") -> None:
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        if _exam_row(conn, exam_id)["status"] != "draft":
            raise ValueError("Only a draft exam can be edited. Close and duplicate it to change it.")
        title, instructions = _validate_exam_fields(conn, title, class_id, academic_session_id, duration_minutes,
                                                    subject_id, instructions)
        conn.execute(
            "UPDATE cbt_exams SET title=?,class_id=?,subject_id=?,academic_session_id=?,duration_minutes=?,"
            "shuffle_questions=?,shuffle_options=?,show_score=?,instructions=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (title, class_id, subject_id, academic_session_id, duration_minutes, int(bool(shuffle_questions)),
             int(bool(shuffle_options)), int(bool(show_score)), instructions, exam_id))
        db._audit_in_transaction(conn, actor, "UPDATE", "cbt_exams", exam_id)


def set_exam_questions(actor, exam_id, question_ids: list[int]) -> None:
    if not isinstance(question_ids, (list, tuple)) or len(question_ids) > MAX_QUESTIONS_PER_EXAM:
        raise ValueError(f"An exam can have up to {MAX_QUESTIONS_PER_EXAM} questions.")
    ids = list(dict.fromkeys(question_ids))
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        if _exam_row(conn, exam_id)["status"] != "draft":
            raise ValueError("Questions can only be changed while the exam is a draft.")
        for question_id in ids:
            row = conn.execute("SELECT is_archived FROM cbt_questions WHERE id=?", (question_id,)).fetchone() \
                if isinstance(question_id, int) and not isinstance(question_id, bool) else None
            if row is None or row["is_archived"]:
                raise ValueError("Every selected question must exist and not be archived.")
        conn.execute("DELETE FROM cbt_exam_questions WHERE exam_id=?", (exam_id,))
        conn.executemany("INSERT INTO cbt_exam_questions(exam_id,question_id,position) VALUES(?,?,?)",
                         [(exam_id, q, i) for i, q in enumerate(ids, start=1)])
        db._audit_in_transaction(conn, actor, "SET_QUESTIONS", "cbt_exams", exam_id)


def delete_exam(actor, exam_id) -> None:
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        if _exam_row(conn, exam_id)["status"] != "draft":
            raise ValueError("Only a draft exam can be deleted.")
        conn.execute("DELETE FROM cbt_exams WHERE id=?", (exam_id,))
        db._audit_in_transaction(conn, actor, "DELETE", "cbt_exams", exam_id)


def duplicate_exam(actor, exam_id) -> int:
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        exam = _exam_row(conn, exam_id)
        cursor = conn.execute(
            "INSERT INTO cbt_exams(title,class_id,subject_id,academic_session_id,duration_minutes,access_code,shuffle_questions,"
            "shuffle_options,show_score,instructions,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f"{exam['title']} (copy)"[:120], exam["class_id"], exam["subject_id"], exam["academic_session_id"],
             exam["duration_minutes"], _new_code(conn), exam["shuffle_questions"], exam["shuffle_options"],
             exam["show_score"], exam["instructions"], actor))
        conn.execute("INSERT INTO cbt_exam_questions(exam_id,question_id,position) "
                     "SELECT ?,question_id,position FROM cbt_exam_questions WHERE exam_id=?", (cursor.lastrowid, exam_id))
        db._audit_in_transaction(conn, actor, "DUPLICATE", "cbt_exams", cursor.lastrowid)
        return int(cursor.lastrowid)


def open_exam(actor, exam_id) -> str:
    """Make the exam available to students. Returns its access code."""
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        exam = _exam_row(conn, exam_id)
        if exam["status"] == "open":
            return exam["access_code"]
        if not conn.execute("SELECT 1 FROM cbt_exam_questions WHERE exam_id=?", (exam_id,)).fetchone():
            raise ValueError("Add questions to the exam before opening it.")
        code = exam["access_code"]
        if conn.execute("SELECT 1 FROM cbt_exams WHERE status='open' AND access_code=? COLLATE NOCASE", (code,)).fetchone():
            code = _new_code(conn)
        conn.execute("UPDATE cbt_exams SET status='open',access_code=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (code, exam_id))
        db._audit_in_transaction(conn, actor, "OPEN", "cbt_exams", exam_id)
        return code


def close_exam(actor, exam_id, now=None) -> int:
    """Close the exam and submit every attempt still in progress. Returns how many were submitted."""
    from . import cbt_runtime
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        if _exam_row(conn, exam_id)["status"] != "open":
            raise ValueError("Only an open exam can be closed.")
        conn.execute("UPDATE cbt_exams SET status='closed',updated_at=CURRENT_TIMESTAMP WHERE id=?", (exam_id,))
        ids = [r[0] for r in conn.execute("SELECT id FROM cbt_attempts WHERE exam_id=? AND status='in_progress'", (exam_id,))]
        for attempt_id in ids:
            cbt_runtime.finish_attempt_in_transaction(conn, attempt_id, now or utc_now(), auto=True)
        db._audit_in_transaction(conn, actor, "CLOSE", "cbt_exams", exam_id)
        return len(ids)


def regenerate_code(actor, exam_id) -> str:
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        if _exam_row(conn, exam_id)["status"] == "closed":
            raise ValueError("A closed exam has no access code to change.")
        code = _new_code(conn)
        conn.execute("UPDATE cbt_exams SET access_code=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (code, exam_id))
        db._audit_in_transaction(conn, actor, "NEW_CODE", "cbt_exams", exam_id)
        return code


def list_exams(actor) -> list[dict]:
    with db.connection() as conn:
        _admin(conn, actor)
        rows = conn.execute(
            """SELECT e.*,c.class_name||CASE WHEN c.section<>'' THEN ' '||c.section ELSE '' END AS class_label,
                      s.subject_name,
                      (SELECT COUNT(*) FROM cbt_exam_questions x WHERE x.exam_id=e.id) AS question_count,
                      (SELECT COALESCE(SUM(q.marks),0) FROM cbt_exam_questions x JOIN cbt_questions q ON q.id=x.question_id
                        WHERE x.exam_id=e.id) AS total_marks,
                      (SELECT COUNT(*) FROM cbt_attempts a WHERE a.exam_id=e.id AND a.status='submitted') AS submitted,
                      (SELECT COUNT(*) FROM cbt_attempts a WHERE a.exam_id=e.id AND a.status='in_progress') AS in_progress
               FROM cbt_exams e JOIN classes c ON c.id=e.class_id LEFT JOIN subjects s ON s.id=e.subject_id
               ORDER BY e.id DESC""").fetchall()
    return [dict(r) for r in rows]


def exam_questions(actor, exam_id) -> list[dict]:
    with db.connection() as conn:
        _admin(conn, actor)
        _exam_row(conn, exam_id)
        rows = conn.execute(
            "SELECT q.*,x.position,NULL AS subject_name,0 AS used_in FROM cbt_exam_questions x "
            "JOIN cbt_questions q ON q.id=x.question_id WHERE x.exam_id=? ORDER BY x.position", (exam_id,)).fetchall()
    return [_question_dict(r) for r in rows]


# -------------------------------------------------------- monitor and results
def monitor(actor, exam_id, now=None) -> dict:
    """Live view: every active student of the class with their attempt state. Also submits timed-out attempts."""
    from . import cbt_runtime
    now = now or utc_now()
    cbt_runtime.finalize_expired(now)
    with db.connection() as conn:
        _admin(conn, actor)
        exam = _exam_row(conn, exam_id)
        total_questions = conn.execute("SELECT COUNT(*) FROM cbt_exam_questions WHERE exam_id=?", (exam_id,)).fetchone()[0]
        rows = conn.execute(
            """SELECT s.id AS student_id,s.admission_no,s.first_name||' '||s.last_name AS name,
                      a.id AS attempt_id,a.status,a.started_at,a.deadline_at,a.submitted_at,a.score,a.max_score,
                      a.focus_losses,a.auto_submitted,
                      (SELECT COUNT(*) FROM cbt_answers w WHERE w.attempt_id=a.id) AS answered
               FROM student_enrollments en JOIN students s ON s.id=en.student_id
               LEFT JOIN cbt_attempts a ON a.exam_id=? AND a.student_id=s.id AND a.status<>'voided'
               WHERE en.class_id=? AND en.academic_session_id=? AND en.status='active' AND s.is_active=1
               ORDER BY s.last_name,s.first_name""",
            (exam_id, exam["class_id"], exam["academic_session_id"])).fetchall()
    students = []
    for row in rows:
        item = dict(row)
        item["state"] = item["status"] or "not_started"
        remaining = None
        if item["status"] == "in_progress":
            remaining = max(0, int((datetime.fromisoformat(item["deadline_at"]) - now).total_seconds()))
        item["remaining_seconds"] = remaining
        item["total_questions"] = total_questions
        students.append(item)
    counts = {"not_started": 0, "in_progress": 0, "submitted": 0}
    for item in students:
        counts[item["state"]] += 1
    return {"exam": dict(exam), "students": students, "counts": counts}


def reset_attempt(actor, exam_id, student_id) -> None:
    """Void a student's attempt (kept for the record) so they can sit the exam again."""
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        _exam_row(conn, exam_id)
        changed = conn.execute("UPDATE cbt_attempts SET status='voided' WHERE exam_id=? AND student_id=? AND status<>'voided'",
                               (exam_id, student_id)).rowcount
        if not changed:
            raise ValueError("This student has no attempt to reset.")
        db._audit_in_transaction(conn, actor, "RESET_ATTEMPT", "cbt_exams", f"{exam_id}/{student_id}")


def exam_results(actor, exam_id) -> dict:
    data = monitor(actor, exam_id)
    rows = []
    for item in data["students"]:
        if item["state"] != "submitted":
            continue
        percent = round(100 * item["score"] / item["max_score"], 1) if item["max_score"] else 0.0
        rows.append({"admission_no": item["admission_no"], "name": item["name"], "score": item["score"],
                     "max_score": item["max_score"], "percent": percent, "focus_losses": item["focus_losses"],
                     "submitted_at": item["submitted_at"], "auto_submitted": bool(item["auto_submitted"])})
    percents = [r["percent"] for r in rows]
    stats = {"submitted": len(rows), "not_submitted": len(data["students"]) - len(rows),
             "average": round(sum(percents) / len(percents), 1) if percents else None,
             "highest": max(percents) if percents else None, "lowest": min(percents) if percents else None}
    return {"exam": data["exam"], "rows": sorted(rows, key=lambda r: (-r["percent"], r["name"])), "stats": stats}


def question_analysis(actor, exam_id) -> list[dict]:
    """Per-question difficulty: how many submitted students got each question right."""
    with db.connection() as conn:
        _admin(conn, actor)
        _exam_row(conn, exam_id)
        questions = conn.execute(
            "SELECT q.id,q.qtype,q.prompt,q.options_json,q.answer,q.marks FROM cbt_exam_questions x "
            "JOIN cbt_questions q ON q.id=x.question_id WHERE x.exam_id=? ORDER BY x.position", (exam_id,)).fetchall()
        submitted = conn.execute("SELECT COUNT(*) FROM cbt_attempts WHERE exam_id=? AND status='submitted'", (exam_id,)).fetchone()[0]
        result = []
        for q in questions:
            answers = [r[0] for r in conn.execute(
                "SELECT w.answer FROM cbt_answers w JOIN cbt_attempts a ON a.id=w.attempt_id "
                "WHERE a.exam_id=? AND a.status='submitted' AND w.question_id=?", (exam_id, q["id"]))]
            correct = sum(1 for a in answers if is_correct(q["qtype"], q["answer"], a))
            result.append({"question_id": q["id"], "prompt": q["prompt"], "answered": len(answers), "correct": correct,
                           "submitted": submitted, "percent_correct": round(100 * correct / submitted, 1) if submitted else None})
    return result


def is_correct(qtype: str, stored_answer: str, given: str) -> bool:
    given = str(given or "")
    if qtype == "mcq":
        return given.strip().upper() == stored_answer
    if qtype == "true_false":
        return given.strip().lower() == stored_answer
    return normalise_short(given) in {normalise_short(a) for a in json.loads(stored_answer)}


def export_results_xlsx(actor, exam_id, path: str) -> str:
    data = exam_results(actor, exam_id)
    rows = [["Admission No", "Name", "Score", "Out of", "Percent", "Tab switches", "Submitted at", "Auto-submitted"]]
    for r in data["rows"]:
        rows.append([r["admission_no"], r["name"], r["score"], r["max_score"], r["percent"], r["focus_losses"],
                     r["submitted_at"], "Yes" if r["auto_submitted"] else "No"])
    tabular.write_workbook(path, [{"name": "Results", "headers": rows[0], "rows": rows[1:],
                                   "text_columns": ["Admission No"]}])
    return str(path)


def form_options(actor) -> dict:
    """Choices for the exam and question forms."""
    with db.connection() as conn:
        _admin(conn, actor)
        classes = [(r["id"], r["label"]) for r in conn.execute(
            "SELECT id,class_name||CASE WHEN section<>'' THEN ' '||section ELSE '' END AS label FROM classes ORDER BY class_name,section")]
        sessions = [(r["id"], r["name"], bool(r["is_current"])) for r in conn.execute(
            "SELECT id,name,is_current FROM academic_sessions ORDER BY starts_on DESC")]
        subjects = [(r["id"], r["subject_name"]) for r in conn.execute("SELECT id,subject_name FROM subjects ORDER BY subject_name")]
    return {"classes": classes, "sessions": sessions, "subjects": subjects}
