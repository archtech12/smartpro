"""Read-only data for the dashboard and the setup guide.

Nothing here writes. Each function opens one short connection and runs
parameterised queries, so it is safe to call from a background task.
"""

from datetime import date

from . import database as db

# (key, title, why it matters, page to open, required for results/reports?)
SETUP_STEPS = (
    ("profile", "School profile", "School name, head and class-teacher names, logo and signatures appear on report cards.", "School Profile", True),
    ("calendar", "Academic session and current term", "Pick the running session and term. Attendance, results and fees all use them.", "Academic Calendar", True),
    ("classes", "Classes", "Add each class and section your school runs.", "Classes", True),
    ("stages", "Class stages", "Mark each class as Primary or Secondary. Report cards and grade bands use this.", "Class Stages", True),
    ("subjects", "Subjects", "Add the subjects your school teaches.", "Subjects", True),
    ("class_subjects", "Class subjects", "Say which subjects each class takes and who teaches them.", "Class Subjects", True),
    ("bands", "Grade bands", "Define grade boundaries for each stage in the current session.", "Grade Bands", True),
    ("assessment", "Assessment setup", "Set the CA and exam components and their maximum scores.", "Assessment Setup", True),
    ("students", "Students", "Register students or import them from a CSV file.", "Students", True),
    ("enrollments", "Enrollments", "Place students in classes for the current session.", "Enrollments", True),
    ("teachers", "Teacher accounts", "Optional: give teachers their own sign-in for scores and attendance.", "User Accounts", False),
    ("fees", "Fees", "Optional: create fee types and set amounts per class and term.", "Fee Setup", False),
)


def _one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def load_setup_checklist() -> dict:
    """Return each setup step with ``done`` and a short ``detail`` line."""
    with db.connection() as conn:
        session = conn.execute("SELECT id,name FROM academic_sessions WHERE is_current=1").fetchone()
        term = conn.execute("SELECT id,term_name FROM terms WHERE is_current=1").fetchone()
        profile_name = (conn.execute("SELECT school_name FROM school_profile WHERE id=1").fetchone() or [""])[0] or ""
        classes = _one(conn, "SELECT COUNT(*) FROM classes")
        unstaged = _one(conn, "SELECT COUNT(*) FROM classes WHERE educational_stage IS NULL OR educational_stage=''")
        subjects = _one(conn, "SELECT COUNT(*) FROM subjects")
        sid = session["id"] if session else None
        assigned = _one(conn, "SELECT COUNT(*) FROM class_subject_assignments WHERE academic_session_id=?", (sid,)) if sid else 0
        bands = _one(conn, "SELECT COUNT(*) FROM assessment_grade_bands WHERE academic_session_id=?", (sid,)) if sid else 0
        components = _one(
            conn,
            """SELECT COUNT(*) FROM assessment_components c
               JOIN class_subject_assignments a ON a.id=c.class_subject_assignment_id
               WHERE a.academic_session_id=?""", (sid,),
        ) if sid else 0
        students = _one(conn, "SELECT COUNT(*) FROM students WHERE is_active=1")
        enrolled = _one(
            conn, "SELECT COUNT(*) FROM student_enrollments WHERE academic_session_id=? AND status='active'", (sid,),
        ) if sid else 0
        teacher_accounts = _one(conn, "SELECT COUNT(*) FROM users WHERE role='teacher' AND is_active=1")
        fee_schedules = _one(conn, "SELECT COUNT(*) FROM fee_schedules WHERE is_active=1")

    facts = {
        "profile": (bool(profile_name.strip()), profile_name.strip() or "School name not set"),
        "calendar": (bool(session and term), f"{session['name']} — {term['term_name']}" if session and term else "No current session and term"),
        "classes": (classes > 0, f"{classes} class(es)"),
        "stages": (classes > 0 and unstaged == 0, "All classes have a stage" if classes and not unstaged else f"{unstaged} class(es) need a stage"),
        "subjects": (subjects > 0, f"{subjects} subject(s)"),
        "class_subjects": (assigned > 0, f"{assigned} assignment(s) this session"),
        "bands": (bands > 0, f"{bands} band(s) this session"),
        "assessment": (components > 0, f"{components} component(s) this session"),
        "students": (students > 0, f"{students} active student(s)"),
        "enrollments": (enrolled > 0, f"{enrolled} enrolled this session"),
        "teachers": (teacher_accounts > 0, f"{teacher_accounts} teacher account(s)"),
        "fees": (fee_schedules > 0, f"{fee_schedules} active fee schedule(s)"),
    }
    steps = []
    for key, title, why, page, required in SETUP_STEPS:
        done, detail = facts[key]
        steps.append({"key": key, "title": title, "why": why, "page": page,
                      "required": required, "done": done, "detail": detail})
    required_steps = [s for s in steps if s["required"]]
    done_required = sum(1 for s in required_steps if s["done"])
    next_step = next((s for s in steps if s["required"] and not s["done"]), None)
    return {
        "steps": steps,
        "required_total": len(required_steps),
        "required_done": done_required,
        "complete": done_required == len(required_steps),
        "next_step": next_step,
    }


def load_dashboard_overview(today: date | None = None) -> dict:
    """Counts and today's activity for the current session and term."""
    today_text = (today or date.today()).isoformat()
    with db.connection() as conn:
        session = conn.execute("SELECT id,name FROM academic_sessions WHERE is_current=1").fetchone()
        term = conn.execute("SELECT id,term_name FROM terms WHERE is_current=1").fetchone()
        sid = session["id"] if session else None
        enrolled = _one(
            conn, "SELECT COUNT(*) FROM student_enrollments WHERE academic_session_id=? AND status='active'", (sid,),
        ) if sid else 0
        gender = {}
        if sid:
            for row in conn.execute(
                """SELECT COALESCE(NULLIF(TRIM(s.gender),''),'Not set') AS g, COUNT(*) AS n
                   FROM student_enrollments e JOIN students s ON s.id=e.student_id
                   WHERE e.academic_session_id=? AND e.status='active' GROUP BY g ORDER BY g""", (sid,)
            ):
                gender[row["g"]] = row["n"]
        present = absent = registers = 0
        if sid:
            row = conn.execute(
                """SELECT COUNT(DISTINCT r.id) AS registers,
                          COALESCE(SUM(CASE WHEN m.status='present' THEN 1 ELSE 0 END),0) AS present,
                          COALESCE(SUM(CASE WHEN m.status='absent' THEN 1 ELSE 0 END),0) AS absent
                   FROM attendance_registers r LEFT JOIN attendance_marks m ON m.register_id=r.id
                   WHERE r.academic_session_id=? AND r.attendance_date=?""", (sid, today_text),
            ).fetchone()
            registers, present, absent = row["registers"], row["present"], row["absent"]
        class_total = _one(conn, "SELECT COUNT(*) FROM classes")
        classes_with_register = _one(
            conn, "SELECT COUNT(DISTINCT class_id) FROM attendance_registers WHERE academic_session_id=? AND attendance_date=?",
            (sid, today_text),
        ) if sid else 0
        issued = open_drafts = 0
        if sid and term:
            issued = _one(
                conn,
                """SELECT COUNT(*) FROM fee_invoices i JOIN fee_invoice_batches b ON b.id=i.batch_id
                   WHERE b.academic_session_id=? AND b.term_id=?
                     AND EXISTS(SELECT 1 FROM fee_invoice_issues x WHERE x.batch_id=b.id)""", (sid, term["id"]),
            )
            open_drafts = _one(
                conn, "SELECT COUNT(*) FROM fee_invoice_batches WHERE academic_session_id=? AND term_id=? AND status='draft'",
                (sid, term["id"]),
            )
        pending_results = 0
        if sid and term:
            pending_results = _one(
                conn,
                """SELECT COUNT(*) FROM assessment_policy_snapshots s
                   WHERE s.academic_session_id=? AND s.term_id=?
                     AND NOT EXISTS(SELECT 1 FROM assessment_result_approvals a WHERE a.snapshot_id=s.id)""",
                (sid, term["id"]),
            )
    return {
        "session": session["name"] if session else None,
        "term": term["term_name"] if term else None,
        "today": today_text,
        "enrolled": enrolled,
        "gender": gender,
        "attendance": {
            "registers": registers, "present": present, "absent": absent,
            "classes_total": class_total, "classes_taken": classes_with_register,
            "classes_missing": max(class_total - classes_with_register, 0),
        },
        "fees": {"issued_invoices": issued, "draft_batches": open_drafts},
        "results": {"awaiting_approval": pending_results},
    }
