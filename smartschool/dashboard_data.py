"""Read-only data for the dashboard and the setup guide.

Nothing here writes. Each function opens one short connection and runs
parameterised queries, so it is safe to call from a background task.
"""

from datetime import date, timedelta
from decimal import Decimal

from . import communications
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
    ("students", "Students", "Register students or import a CSV/Excel class roster.", "Students", True),
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
        demographics = {"boys": 0, "girls": 0, "other": 0, "not_set": 0}
        for label, count in gender.items():
            normalized = label.strip().casefold()
            if normalized in {"male", "m", "boy", "boys"}:
                demographics["boys"] += count
            elif normalized in {"female", "f", "girl", "girls"}:
                demographics["girls"] += count
            elif normalized in {"", "not set", "unknown"}:
                demographics["not_set"] += count
            else:
                demographics["other"] += count
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
        "demographics": demographics,
        "attendance": {
            "registers": registers, "present": present, "absent": absent,
            "classes_total": class_total, "classes_taken": classes_with_register,
            "classes_missing": max(class_total - classes_with_register, 0),
        },
        "fees": {"issued_invoices": issued, "draft_batches": open_drafts},
        "results": {"awaiting_approval": pending_results},
    }


_ENTITY_LABELS = {
    "students": "a student", "teachers": "a teacher", "classes": "a class", "subjects": "a subject",
    "fee_types": "a fee type", "fee_schedules": "a fee schedule", "fee_payments": "a payment",
    "communication_messages": "a message", "communication_templates": "a message template",
    "academic_sessions": "an academic session", "terms": "a term", "student_enrollments": "an enrollment",
    "users": "a user account", "school_profile": "the school profile",
}
_ACTION_VERBS = {
    "CREATE": "created", "UPDATE": "updated", "DELETE": "deleted", "QUEUE": "queued", "MARK_SENT": "marked sent",
    "CANCEL": "cancelled", "REQUEUE": "requeued", "RESET": "reset", "OPT_OUT": "recorded an opt-out for",
    "OPT_IN": "removed an opt-out for", "LOGIN": "signed in",
}


def _activity_text(row) -> str:
    verb = _ACTION_VERBS.get(row["action"], row["action"].replace("_", " ").lower())
    if row["action"] == "LOGIN":
        return f"{row['username']} signed in"
    entity = _ENTITY_LABELS.get(row["entity"], row["entity"].replace("_", " "))
    return f"{row['username']} {verb} {entity}"


def load_dashboard_analytics(actor: str, today: date | None = None) -> dict:
    """Administrator-only analytics: fees, attendance, class strength, alerts, activity, communications."""
    today = today or date.today()
    today_text = today.isoformat()
    with db.connection() as conn:
        db._require_admin(conn, actor)
        session = conn.execute("SELECT id,name FROM academic_sessions WHERE is_current=1").fetchone()
        term = conn.execute(
            "SELECT id,term_name FROM terms WHERE academic_session_id=? AND is_current=1", (session["id"],)
        ).fetchone() if session else None
        billed = paid = outstanding = Decimal("0")
        owing_students = 0
        if session and term:
            student_ids = [r[0] for r in conn.execute(
                """SELECT DISTINCT e.student_id FROM fee_invoices i JOIN student_enrollments e ON e.id=i.enrollment_id
                   JOIN fee_invoice_batches b ON b.id=i.batch_id JOIN fee_invoice_issues x ON x.batch_id=b.id"""
            )]
            for student_id in student_ids:
                statement = db._fee_statement_in_transaction(conn, student_id)
                billed += Decimal(statement["total_billed"])
                paid += Decimal(statement["total_paid"])
                balance = Decimal(statement["total_outstanding"])
                outstanding += balance
                owing_students += 1 if balance > 0 else 0
        rate = int((paid / billed * 100).to_integral_value(rounding="ROUND_FLOOR")) if billed > 0 else None

        attendance_rate = None
        if session:
            since = (today - timedelta(days=29)).isoformat()
            row = conn.execute(
                """SELECT COALESCE(SUM(CASE WHEN m.status='present' THEN 1 ELSE 0 END),0) p,
                          COALESCE(SUM(CASE WHEN m.status='absent' THEN 1 ELSE 0 END),0) a
                   FROM attendance_registers r JOIN attendance_marks m ON m.register_id=r.id
                   WHERE r.academic_session_id=? AND r.attendance_date BETWEEN ? AND ?""",
                (session["id"], since, today_text),
            ).fetchone()
            if row["p"] + row["a"] > 0:
                attendance_rate = int(row["p"] * 100 // (row["p"] + row["a"]))

        class_strength = []
        if session:
            class_strength = [
                {"class": f"{r['class_name']} {r['section']}".strip(), "students": r["n"]}
                for r in conn.execute(
                    """SELECT c.class_name,c.section,COUNT(e.id) n FROM classes c
                       LEFT JOIN student_enrollments e ON e.class_id=c.id AND e.academic_session_id=? AND e.status='active'
                       GROUP BY c.id ORDER BY c.class_name,c.section""", (session["id"],))
            ]
        activity = [
            {"text": _activity_text(r), "at": r["created_at"]}
            for r in conn.execute("SELECT username,action,entity,created_at FROM audit_log ORDER BY id DESC LIMIT 8")
        ]
        unstaged = conn.execute("SELECT COUNT(*) FROM classes WHERE educational_stage IS NULL OR educational_stage=''").fetchone()[0]
        students = conn.execute("SELECT COUNT(*) FROM students WHERE is_active=1").fetchone()[0]
        pending_results = 0
        if session and term:
            pending_results = conn.execute(
                """SELECT COUNT(*) FROM assessment_policy_snapshots s WHERE s.academic_session_id=? AND s.term_id=?
                   AND NOT EXISTS(SELECT 1 FROM assessment_result_approvals a WHERE a.snapshot_id=s.id)""",
                (session["id"], term["id"]),
            ).fetchone()[0]
        achievers = db._top_academic_achievers_in_transaction(
            conn, int(session["id"]), int(term["id"]), limit=5
        ) if session and term else []

    stats = communications.load_statistics(actor, today)
    birthdays_today = len(communications.list_birthdays(actor, today, 0)["today"])
    reminders_ready = sum(1 for c in communications.list_fee_reminder_candidates(actor, today)
                          if not c["opted_out"] and not c["queued"] and any(c["contacts"].values()))

    alerts = []
    if not (session and term):
        alerts.append({"level": "warn", "message": "No current session or term is set.", "page": "Academic Calendar"})
    if students == 0:
        alerts.append({"level": "info", "message": "No active students yet. Register students to get started.", "page": "Students"})
    if unstaged:
        alerts.append({"level": "warn", "message": f"{unstaged} class(es) have no Primary/Secondary stage.", "page": "Class Stages"})
    if rate is not None and rate < 20:
        alerts.append({"level": "warn", "message": f"Fee collection is low ({rate}% of billed fees). Consider sending reminders.", "page": "Communication Center"})
    if pending_results:
        alerts.append({"level": "info", "message": f"{pending_results} subject result set(s) await approval this term.", "page": "Grade Entry"})
    if stats["pending"]:
        alerts.append({"level": "info", "message": f"{stats['pending']} message(s) are waiting in the outbox.", "page": "Communication Center"})
    if birthdays_today:
        alerts.append({"level": "info", "message": f"{birthdays_today} student birthday(s) today.", "page": "Communication Center"})

    return {
        "fees": {
            "billed": db._decimal_text(billed), "paid": db._decimal_text(paid),
            "outstanding": db._decimal_text(outstanding), "collection_rate": rate,
            "students_owing": owing_students, "through": f"{session['name']} — {term['term_name']}" if session and term else "",
        },
        "attendance_rate_30d": attendance_rate,
        "class_strength": class_strength,
        "top_academic_achievers": achievers,
        "activity": activity,
        "alerts": alerts,
        "communications": {
            "pending": stats["pending"], "sent_today": stats["sent_today"],
            "birthdays_today": birthdays_today, "fee_reminders_ready": reminders_ready,
        },
    }
