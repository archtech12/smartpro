"""Computer-based tests, student side (Qt-free). Used by the browser server; no staff login is involved.

Everything a student can do is here and is limited to their own attempt. The server owns the clock:
``deadline_at`` is fixed when the attempt starts and answers after it are refused.
"""

import json
import random
from datetime import datetime, timedelta

from . import cbt
from . import database as db


class CBTError(ValueError):
    """A problem to show to the student. ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _exam_by_code(conn, code):
    return conn.execute("SELECT * FROM cbt_exams WHERE status='open' AND access_code=? COLLATE NOCASE",
                        ((code or "").strip(),)).fetchone()


def identify(admission_no: str, code: str) -> dict:
    """Check the admission number and exam code. Raises ``CBTError`` with a deliberately vague message on mismatch."""
    generic = CBTError("invalid", "Those details were not recognised. Check your admission number and the exam code.")
    admission_no = (admission_no or "").strip()
    if not admission_no or len(admission_no) > 40 or not (code or "").strip() or len(code) > 20:
        raise generic
    with db.connection() as conn:
        exam = _exam_by_code(conn, code)
        student = conn.execute(
            "SELECT id,first_name,last_name,is_active FROM students WHERE admission_no=? COLLATE NOCASE", (admission_no,)).fetchone()
        if exam is None or student is None or not student["is_active"]:
            raise generic
        if not conn.execute(
                "SELECT 1 FROM student_enrollments WHERE student_id=? AND class_id=? AND academic_session_id=? AND status='active'",
                (student["id"], exam["class_id"], exam["academic_session_id"])).fetchone():
            raise CBTError("not_in_class", "This exam is not for your class.")
        total = conn.execute(
            "SELECT COUNT(*),COALESCE(SUM(q.marks),0) FROM cbt_exam_questions x JOIN cbt_questions q ON q.id=x.question_id "
            "WHERE x.exam_id=?", (exam["id"],)).fetchone()
        attempt = conn.execute("SELECT status FROM cbt_attempts WHERE exam_id=? AND student_id=? AND status<>'voided'",
                               (exam["id"], student["id"])).fetchone()
        return {"exam_id": exam["id"], "student_id": student["id"],
                "student_name": f"{student['first_name']} {student['last_name']}".strip(),
                "title": exam["title"], "duration_minutes": exam["duration_minutes"], "instructions": exam["instructions"],
                "question_count": total[0], "total_marks": total[1], "attempt_status": attempt["status"] if attempt else "none"}


def start_attempt(exam_id: int, student_id: int, now: datetime | None = None, client_ip: str = "") -> int:
    """Start the timer, or resume an attempt already in progress."""
    now = now or cbt.utc_now()
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        exam = conn.execute("SELECT * FROM cbt_exams WHERE id=?", (exam_id,)).fetchone()
        if exam is None or exam["status"] != "open":
            raise CBTError("closed", "This exam is not open.")
        existing = conn.execute("SELECT id,status,deadline_at FROM cbt_attempts WHERE exam_id=? AND student_id=? AND status<>'voided'",
                                (exam_id, student_id)).fetchone()
        if existing:
            if existing["status"] == "submitted":
                raise CBTError("already_submitted", "You have already submitted this exam.")
            return int(existing["id"])
        seed = random.SystemRandom().randrange(1, 2**31)
        deadline = now + timedelta(minutes=exam["duration_minutes"])
        cursor = conn.execute(
            "INSERT INTO cbt_attempts(exam_id,student_id,seed,started_at,deadline_at,client_ip) VALUES(?,?,?,?,?,?)",
            (exam_id, student_id, seed, cbt.iso(now), cbt.iso(deadline), (client_ip or "")[:60]))
        return int(cursor.lastrowid)


def _ordered_questions(conn, attempt) -> list:
    exam = conn.execute("SELECT * FROM cbt_exams WHERE id=?", (attempt["exam_id"],)).fetchone()
    rows = conn.execute(
        "SELECT q.* FROM cbt_exam_questions x JOIN cbt_questions q ON q.id=x.question_id WHERE x.exam_id=? ORDER BY x.position",
        (attempt["exam_id"],)).fetchall()
    rows = list(rows)
    if exam["shuffle_questions"]:
        random.Random(attempt["seed"]).shuffle(rows)
    return rows


def _student_options(question, attempt, shuffle: bool) -> list[dict]:
    options = [{"key": "ABCDE"[i], "text": text} for i, text in enumerate(json.loads(question["options_json"]))]
    if shuffle:
        random.Random(attempt["seed"] * 1_000_003 + question["id"]).shuffle(options)
    return options


def _attempt(conn, attempt_id, student_id):
    attempt = conn.execute("SELECT * FROM cbt_attempts WHERE id=? AND student_id=?", (attempt_id, student_id)).fetchone()
    if attempt is None or attempt["status"] == "voided":
        raise CBTError("no_attempt", "Your exam session is no longer valid. Sign in again.")
    return attempt


def attempt_state(attempt_id: int, student_id: int, now: datetime | None = None) -> dict:
    """Everything the browser needs to show the exam: questions (without answers), saved answers, time left."""
    now = now or cbt.utc_now()
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        attempt = _attempt(conn, attempt_id, student_id)
        if attempt["status"] == "in_progress" and now >= datetime.fromisoformat(attempt["deadline_at"]):
            finish_attempt_in_transaction(conn, attempt_id, now, auto=True)
            attempt = _attempt(conn, attempt_id, student_id)
        exam = conn.execute("SELECT * FROM cbt_exams WHERE id=?", (attempt["exam_id"],)).fetchone()
        if attempt["status"] == "submitted":
            return {"finished": True, **_result_view(exam, attempt)}
        saved = {r["question_id"]: r["answer"] for r in conn.execute(
            "SELECT question_id,answer FROM cbt_answers WHERE attempt_id=?", (attempt_id,))}
        questions = []
        for q in _ordered_questions(conn, attempt):
            item = {"id": q["id"], "type": q["qtype"], "prompt": q["prompt"], "marks": q["marks"]}
            if q["qtype"] == "mcq":
                item["options"] = _student_options(q, attempt, bool(exam["shuffle_options"]))
            questions.append(item)
        remaining = int((datetime.fromisoformat(attempt["deadline_at"]) - now).total_seconds())
        return {"finished": False, "title": exam["title"], "questions": questions, "answers": saved,
                "remaining_seconds": max(0, remaining)}


def save_answer(attempt_id: int, student_id: int, question_id: int, answer: str, now: datetime | None = None) -> int:
    """Save one answer. Returns the seconds left. Raises ``CBTError('time_up')`` after the deadline."""
    now = now or cbt.utc_now()
    if isinstance(question_id, bool) or not isinstance(question_id, int):
        raise CBTError("bad_request", "Invalid question.")
    answer = str(answer if answer is not None else "")
    if len(answer) > 500:
        raise CBTError("bad_request", "That answer is too long.")
    time_up = False
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        attempt = _attempt(conn, attempt_id, student_id)
        if attempt["status"] != "in_progress":
            raise CBTError("already_submitted", "You have already submitted this exam.")
        deadline = datetime.fromisoformat(attempt["deadline_at"])
        if now >= deadline:
            finish_attempt_in_transaction(conn, attempt_id, now, auto=True)
            time_up = True  # raised after the transaction commits, otherwise the submission would roll back
        elif not conn.execute("SELECT 1 FROM cbt_exam_questions WHERE exam_id=? AND question_id=?",
                            (attempt["exam_id"], question_id)).fetchone():
            raise CBTError("bad_request", "Invalid question.")
        elif answer.strip() == "":
            conn.execute("DELETE FROM cbt_answers WHERE attempt_id=? AND question_id=?", (attempt_id, question_id))
        else:
            conn.execute(
                "INSERT INTO cbt_answers(attempt_id,question_id,answer,answered_at) VALUES(?,?,?,?) "
                "ON CONFLICT(attempt_id,question_id) DO UPDATE SET answer=excluded.answer,answered_at=excluded.answered_at",
                (attempt_id, question_id, answer, cbt.iso(now)))
    if time_up:
        raise CBTError("time_up", "Time is up. Your answers were submitted.")
    return int((deadline - now).total_seconds())


def record_focus_loss(attempt_id: int, student_id: int) -> None:
    with db.connection() as conn:
        conn.execute("UPDATE cbt_attempts SET focus_losses=MIN(focus_losses+1,999) WHERE id=? AND student_id=? AND status='in_progress'",
                     (attempt_id, student_id))


def _result_view(exam, attempt) -> dict:
    view = {"title": exam["title"], "show_score": bool(exam["show_score"]), "auto_submitted": bool(attempt["auto_submitted"])}
    if exam["show_score"]:
        view.update(score=attempt["score"], max_score=attempt["max_score"])
    return view


def finish_attempt_in_transaction(conn, attempt_id: int, now: datetime, auto: bool = False) -> None:
    """Mark the attempt and store the score. The caller owns the transaction."""
    attempt = conn.execute("SELECT * FROM cbt_attempts WHERE id=?", (attempt_id,)).fetchone()
    if attempt is None or attempt["status"] != "in_progress":
        return
    questions = conn.execute(
        "SELECT q.id,q.qtype,q.answer,q.marks FROM cbt_exam_questions x JOIN cbt_questions q ON q.id=x.question_id WHERE x.exam_id=?",
        (attempt["exam_id"],)).fetchall()
    given = {r["question_id"]: r["answer"] for r in conn.execute(
        "SELECT question_id,answer FROM cbt_answers WHERE attempt_id=?", (attempt_id,))}
    score = sum(q["marks"] for q in questions if q["id"] in given and cbt.is_correct(q["qtype"], q["answer"], given[q["id"]]))
    maximum = sum(q["marks"] for q in questions)
    conn.execute(
        "UPDATE cbt_attempts SET status='submitted',submitted_at=?,score=?,max_score=?,auto_submitted=? WHERE id=?",
        (cbt.iso(min(now, datetime.fromisoformat(attempt["deadline_at"])) if auto else now), score, maximum, 1 if auto else 0, attempt_id))


def submit(attempt_id: int, student_id: int, now: datetime | None = None) -> dict:
    now = now or cbt.utc_now()
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        attempt = _attempt(conn, attempt_id, student_id)
        if attempt["status"] == "in_progress":
            late = now >= datetime.fromisoformat(attempt["deadline_at"])
            finish_attempt_in_transaction(conn, attempt_id, now, auto=late)
            attempt = _attempt(conn, attempt_id, student_id)
        exam = conn.execute("SELECT * FROM cbt_exams WHERE id=?", (attempt["exam_id"],)).fetchone()
        return _result_view(exam, attempt)


def finalize_expired(now: datetime | None = None) -> int:
    """Submit every attempt whose time ran out (students who closed their browser). Returns how many."""
    now = now or cbt.utc_now()
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        ids = [r[0] for r in conn.execute("SELECT id FROM cbt_attempts WHERE status='in_progress' AND deadline_at<=?", (cbt.iso(now),))]
        for attempt_id in ids:
            finish_attempt_in_transaction(conn, attempt_id, now, auto=True)
        return len(ids)
