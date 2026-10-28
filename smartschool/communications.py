"""Offline communication centre services (Qt-free, testable).

Messages are composed from editable templates and stored in a local outbox.
Nothing here opens a network connection: delivery is a person's own action
(copy the text, or open it in their own mail / WhatsApp app) followed by
marking the message as sent. Opted-out guardians are never queued.
"""

import re
from datetime import date, timedelta
from decimal import Decimal

from . import database as db

KINDS = {
    "birthday": "Birthday greeting",
    "fee_reminder": "Fee reminder",
    "welcome": "Welcome",
    "notice": "General notice",
    "report_card": "Report card",
}
CHANNELS = {"email": "Email", "whatsapp": "WhatsApp"}
PLACEHOLDERS = (
    "student_name", "class_name", "school_name", "head_name", "date",
    "balance", "term_name", "session_name", "age",
)
_ALIASES = {"principal_name": "head_name"}  # placeholders used by the old application

DEFAULT_TEMPLATES = {
    "birthday": (
        "Happy Birthday, {student_name}!",
        "Dear Parent/Guardian of {student_name},\n\nOn behalf of the staff and management of {school_name}, "
        "we warmly wish {student_name} a very Happy Birthday!\n\nMay this special day be filled with joy.\n\n"
        "Best regards,\n{head_name}\n{school_name}",
    ),
    "fee_reminder": (
        "School fee reminder – {school_name}",
        "Dear Parent/Guardian of {student_name},\n\nThis is a gentle reminder that a school fee balance of "
        "{balance} remains unpaid for {student_name} ({class_name}) through {term_name}, {session_name}.\n\n"
        "Please settle this balance at your earliest convenience. If you have already paid, kindly ignore "
        "this message.\n\nBest regards,\n{head_name}\n{school_name}",
    ),
    "welcome": (
        "Welcome to {school_name}",
        "Dear Parent/Guardian of {student_name},\n\nWe are delighted to welcome {student_name} to "
        "{school_name} ({class_name}).\n\nBest regards,\n{head_name}\n{school_name}",
    ),
    "notice": (
        "Notice from {school_name}",
        "Dear Parent/Guardian of {student_name},\n\n[Write your notice here]\n\n"
        "Best regards,\n{head_name}\n{school_name}",
    ),
    "report_card": (
        "{student_name} – {term_name} report card, {school_name}",
        "Dear Parent/Guardian of {student_name},\n\nPlease find attached the {term_name} ({session_name}) report "
        "card for {student_name} ({class_name}).\n\nBest regards,\n{head_name}\n{school_name}",
    ),
}

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def render_template(text: str, variables: dict[str, str]) -> str:
    """Replace known ``{placeholders}`` only; unknown braces are left untouched."""
    def swap(match):
        key = _ALIASES.get(match.group(1), match.group(1))
        return str(variables[key]) if key in variables else match.group(0)
    return _PLACEHOLDER.sub(swap, text)


def unknown_placeholders(text: str) -> list[str]:
    return sorted({m for m in _PLACEHOLDER.findall(text) if _ALIASES.get(m, m) not in PLACEHOLDERS})


def _admin(conn, actor):
    return db._require_admin(conn, actor)


def _kind(kind):
    if kind not in KINDS:
        raise ValueError("Choose a valid message type.")
    return kind


# ---------------------------------------------------------------- settings / templates

def normalize_phone(phone: str, country_code: str = "") -> str | None:
    """Digits-only international number for a WhatsApp link, or ``None`` when it cannot be known.

    A number starting with ``+`` or ``00`` is already international. A local number
    starting with ``0`` needs the school's configured country code; no code is assumed.
    """
    raw = (phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if raw.startswith("+"):
        result = digits
    elif digits.startswith("00"):
        result = digits[2:]
    elif digits.startswith("0"):
        code = re.sub(r"\D", "", country_code or "")
        if not code:
            return None
        result = code + digits[1:]
    else:
        result = digits
    return result if 8 <= len(result) <= 15 else None


def load_settings(actor: str) -> dict:
    with db.connection() as conn:
        _admin(conn, actor)
        row = conn.execute("SELECT value FROM communication_settings WHERE key='country_code'").fetchone()
        return {"country_code": row["value"] if row else ""}


def save_country_code(country_code: str, actor: str) -> None:
    code = re.sub(r"\D", "", country_code or "")
    if country_code.strip() and not 1 <= len(code) <= 4:
        raise ValueError("Enter the country calling code with 1 to 4 digits (without + or 0).")
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        if code:
            conn.execute(
                "INSERT INTO communication_settings(key,value,updated_by) VALUES('country_code',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_by=excluded.updated_by,updated_at=CURRENT_TIMESTAMP",
                (code, actor),
            )
        else:
            conn.execute("DELETE FROM communication_settings WHERE key='country_code'")
        db._audit_in_transaction(conn, actor, "UPDATE", "communication_settings", "country_code")


def load_templates(actor: str) -> dict:
    with db.connection() as conn:
        _admin(conn, actor)
        saved = {row["kind"]: row for row in conn.execute("SELECT kind,subject,body FROM communication_templates")}
    result = {}
    for kind, label in KINDS.items():
        subject, body = DEFAULT_TEMPLATES[kind]
        custom = kind in saved
        if custom:
            subject, body = saved[kind]["subject"], saved[kind]["body"]
        result[kind] = {"label": label, "subject": subject, "body": body, "customised": custom}
    return result


def save_template(kind: str, subject: str, body: str, actor: str) -> None:
    _kind(kind)
    if not isinstance(subject, str) or not 1 <= len(subject.strip()) <= 200:
        raise ValueError("The subject must contain between 1 and 200 characters.")
    if not isinstance(body, str) or not 1 <= len(body.strip()) <= 4000:
        raise ValueError("The message must contain between 1 and 4000 characters.")
    bad = unknown_placeholders(subject) + unknown_placeholders(body)
    if bad:
        raise ValueError("Unknown placeholder(s): " + ", ".join("{" + b + "}" for b in sorted(set(bad))))
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        conn.execute(
            "INSERT INTO communication_templates(kind,subject,body,updated_by) VALUES(?,?,?,?) "
            "ON CONFLICT(kind) DO UPDATE SET subject=excluded.subject,body=excluded.body,"
            "updated_by=excluded.updated_by,updated_at=CURRENT_TIMESTAMP",
            (kind, subject.strip(), body.strip(), actor),
        )
        db._audit_in_transaction(conn, actor, "UPDATE", "communication_templates", kind)


def reset_template(kind: str, actor: str) -> None:
    _kind(kind)
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        conn.execute("DELETE FROM communication_templates WHERE kind=?", (kind,))
        db._audit_in_transaction(conn, actor, "RESET", "communication_templates", kind)


# ---------------------------------------------------------------- opt-outs

def set_opt_out(student_id: int, opted_out: bool, reason: str, actor: str) -> None:
    if isinstance(student_id, bool) or not isinstance(student_id, int) or student_id <= 0:
        raise ValueError("Select a valid student.")
    reason = (reason or "").strip()
    if len(reason) > 240:
        raise ValueError("The reason must contain at most 240 characters.")
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        if conn.execute("SELECT 1 FROM students WHERE id=?", (student_id,)).fetchone() is None:
            raise ValueError("Select an existing student.")
        if opted_out:
            conn.execute(
                "INSERT INTO communication_opt_outs(student_id,reason,created_by) VALUES(?,?,?) "
                "ON CONFLICT(student_id) DO UPDATE SET reason=excluded.reason",
                (student_id, reason, actor),
            )
            # a guardian who asked not to be contacted must not keep pending messages
            conn.execute(
                "UPDATE communication_messages SET status='cancelled',handled_by=?,handled_at=CURRENT_TIMESTAMP "
                "WHERE student_id=? AND status='pending'", (actor, student_id),
            )
        else:
            conn.execute("DELETE FROM communication_opt_outs WHERE student_id=?", (student_id,))
        db._audit_in_transaction(conn, actor, "OPT_OUT" if opted_out else "OPT_IN", "students", student_id)


# ---------------------------------------------------------------- recipients

def _school_context(conn) -> dict:
    profile = conn.execute("SELECT school_name,head_name FROM school_profile WHERE id=1").fetchone()
    session = conn.execute("SELECT id,name FROM academic_sessions WHERE is_current=1").fetchone()
    term = None
    if session:
        term = conn.execute(
            "SELECT term_name FROM terms WHERE academic_session_id=? AND is_current=1", (session["id"],)
        ).fetchone()
    return {
        "school_name": (profile["school_name"] if profile else "") or "the school",
        "head_name": (profile["head_name"] if profile else "") or "The Principal",
        "session_name": session["name"] if session else "",
        "term_name": term["term_name"] if term else "",
        "session_id": session["id"] if session else None,
    }


def _class_label(conn, student_id: int, session_id, legacy: str) -> str:
    if session_id:
        row = conn.execute(
            """SELECT c.class_name,c.section FROM student_enrollments e JOIN classes c ON c.id=e.class_id
               WHERE e.student_id=? AND e.academic_session_id=? AND e.status='active'""", (student_id, session_id),
        ).fetchone()
        if row:
            return f"{row['class_name']} {row['section']}".strip()
    return legacy or ""


def _birthday_matches(dob: str, day: date) -> bool:
    try:
        born = date.fromisoformat(dob)
    except (TypeError, ValueError):
        return False
    if born.month == 2 and born.day == 29 and not _is_leap(day.year):
        return day.month == 2 and day.day == 28
    return (born.month, born.day) == (day.month, day.day)


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _contact_flags(row, country_code):
    email = (row["email"] or "").strip()
    return {
        "email": email if _EMAIL.match(email) else "",
        "whatsapp": normalize_phone(row["guardian_phone"] or "", country_code) or "",
    }


def list_birthdays(actor: str, today: date | None = None, days_ahead: int = 7) -> dict:
    today = today or date.today()
    with db.connection() as conn:
        _admin(conn, actor)
        ctx = _school_context(conn)
        code = (conn.execute("SELECT value FROM communication_settings WHERE key='country_code'").fetchone() or [""])[0]
        students = conn.execute(
            """SELECT s.id,s.admission_no,s.first_name,s.last_name,s.class_name,s.email,s.guardian_phone,
                      s.date_of_birth,o.student_id IS NOT NULL AS opted_out
               FROM students s LEFT JOIN communication_opt_outs o ON o.student_id=s.id
               WHERE s.is_active=1 AND s.date_of_birth IS NOT NULL AND s.date_of_birth<>''
               ORDER BY s.last_name,s.first_name"""
        ).fetchall()
        queued = {r[0] for r in conn.execute(
            "SELECT dedupe_key FROM communication_messages WHERE status IN ('pending','sent','failed') AND kind='birthday'"
        )}
        def entry(row, day):
            born = date.fromisoformat(row["date_of_birth"])
            return {
                "student_id": int(row["id"]), "admission_no": row["admission_no"],
                "name": f"{row['first_name']} {row['last_name']}".strip(),
                "class_name": _class_label(conn, int(row["id"]), ctx["session_id"], row["class_name"]),
                "date": day.isoformat(), "age": day.year - born.year, "opted_out": bool(row["opted_out"]),
                "contacts": _contact_flags(row, code),
                "queued": f"birthday:{row['id']}:{day.isoformat()}" in queued,
            }
        todays = [entry(r, today) for r in students if _birthday_matches(r["date_of_birth"], today)]
        upcoming = []
        for offset in range(1, max(1, days_ahead) + 1):
            day = today + timedelta(days=offset)
            upcoming += [entry(r, day) for r in students if _birthday_matches(r["date_of_birth"], day)]
    return {"today": todays, "upcoming": upcoming}


def list_fee_reminder_candidates(actor: str, today: date | None = None) -> list[dict]:
    today = today or date.today()
    with db.connection() as conn:
        _admin(conn, actor)
        ctx = _school_context(conn)
        if not ctx["session_id"] or not ctx["term_name"]:
            return []
        code = (conn.execute("SELECT value FROM communication_settings WHERE key='country_code'").fetchone() or [""])[0]
        ids = [r[0] for r in conn.execute(
            """SELECT DISTINCT e.student_id FROM fee_invoices i JOIN student_enrollments e ON e.id=i.enrollment_id
               JOIN fee_invoice_batches b ON b.id=i.batch_id
               JOIN fee_invoice_issues x ON x.batch_id=b.id"""
        )]
        queued = {r[0] for r in conn.execute(
            "SELECT dedupe_key FROM communication_messages WHERE status IN ('pending','sent','failed') AND kind='fee_reminder'"
        )}
        result = []
        for student_id in ids:
            student = conn.execute(
                """SELECT s.id,s.admission_no,s.first_name,s.last_name,s.class_name,s.email,s.guardian_phone,s.is_active,
                          o.student_id IS NOT NULL AS opted_out
                   FROM students s LEFT JOIN communication_opt_outs o ON o.student_id=s.id WHERE s.id=?""",
                (student_id,),
            ).fetchone()
            if student is None or not student["is_active"]:
                continue
            statement = db._fee_statement_in_transaction(conn, student_id)
            balance = Decimal(statement["total_outstanding"])
            if balance <= 0:
                continue
            result.append({
                "student_id": student_id, "admission_no": student["admission_no"],
                "name": f"{student['first_name']} {student['last_name']}".strip(),
                "class_name": _class_label(conn, student_id, ctx["session_id"], student["class_name"]),
                "balance": db._decimal_text(balance), "opted_out": bool(student["opted_out"]),
                "contacts": _contact_flags(student, code),
                "queued": f"fee_reminder:{student_id}:{today.isoformat()}" in queued,
            })
    return sorted(result, key=lambda r: (-Decimal(r["balance"]), r["name"]))


# ---------------------------------------------------------------- outbox

def queue_messages(kind: str, student_ids: list[int], channel: str, actor: str, today: date | None = None,
                   attachments: dict | None = None) -> dict:
    """Compose and store pending messages. Returns ``{"queued": n, "skipped": [{student_id,name,reason}]}``."""
    _kind(kind)
    if channel not in CHANNELS:
        raise ValueError("Choose Email or WhatsApp.")
    if not isinstance(student_ids, (list, tuple)) or not student_ids or len(set(student_ids)) > 500:
        raise ValueError("Select between 1 and 500 students.")
    if any(isinstance(i, bool) or not isinstance(i, int) or i <= 0 for i in student_ids):
        raise ValueError("Select valid students.")
    today = today or date.today()
    attachments = dict(attachments or {})
    if any(not isinstance(v, str) or not v or len(v) > 500 for v in attachments.values()):
        raise ValueError("Attachment paths must be short, non-empty text.")
    queued, skipped = 0, []
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        ctx = _school_context(conn)
        code = (conn.execute("SELECT value FROM communication_settings WHERE key='country_code'").fetchone() or [""])[0]
        saved = conn.execute("SELECT subject,body FROM communication_templates WHERE kind=?", (kind,)).fetchone()
        subject_t, body_t = (saved["subject"], saved["body"]) if saved else DEFAULT_TEMPLATES[kind]
        for student_id in dict.fromkeys(student_ids):
            student = conn.execute(
                """SELECT s.id,s.first_name,s.last_name,s.class_name,s.email,s.guardian_phone,s.date_of_birth,s.is_active,
                          o.student_id IS NOT NULL AS opted_out
                   FROM students s LEFT JOIN communication_opt_outs o ON o.student_id=s.id WHERE s.id=?""",
                (student_id,),
            ).fetchone()
            if student is None or not student["is_active"]:
                skipped.append({"student_id": student_id, "name": "", "reason": "Student not found or inactive."})
                continue
            name = f"{student['first_name']} {student['last_name']}".strip()
            if student["opted_out"]:
                skipped.append({"student_id": student_id, "name": name, "reason": "Guardian has opted out."})
                continue
            contact = _contact_flags(student, code)[channel]
            if not contact:
                reason = ("No valid email address." if channel == "email"
                          else "No usable phone number (add a country code in Communication settings for local numbers).")
                skipped.append({"student_id": student_id, "name": name, "reason": reason})
                continue
            variables = {
                "student_name": name, "class_name": _class_label(conn, student_id, ctx["session_id"], student["class_name"]),
                "school_name": ctx["school_name"], "head_name": ctx["head_name"], "date": today.strftime("%d %B %Y"),
                "term_name": ctx["term_name"], "session_name": ctx["session_name"], "balance": "", "age": "",
            }
            if kind == "fee_reminder":
                balance = Decimal(db._fee_statement_in_transaction(conn, student_id)["total_outstanding"])
                if balance <= 0:
                    skipped.append({"student_id": student_id, "name": name, "reason": "No outstanding balance."})
                    continue
                variables["balance"] = db._decimal_text(balance)
            if kind == "birthday":
                if not _birthday_matches(student["date_of_birth"] or "", today):
                    skipped.append({"student_id": student_id, "name": name, "reason": "Birthday is not today."})
                    continue
                variables["age"] = str(today.year - date.fromisoformat(student["date_of_birth"]).year)
            attachment = attachments.get(student_id)
            if kind == "report_card" and not attachment:
                skipped.append({"student_id": student_id, "name": name, "reason": "No report-card PDF was saved."})
                continue
            key = (f"{kind}:{student_id}:{today.isoformat()}"
                   if kind in ("birthday", "fee_reminder", "report_card") else None)
            if key and conn.execute(
                "SELECT 1 FROM communication_messages WHERE dedupe_key=? AND status IN ('pending','sent','failed')", (key,)
            ).fetchone():
                skipped.append({"student_id": student_id, "name": name, "reason": "Already queued today."})
                continue
            cursor = conn.execute(
                """INSERT INTO communication_messages(kind,channel,student_id,recipient_name,recipient_contact,
                                                      subject,body,dedupe_key,created_by,attachment_path)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (kind, channel, student_id, name, contact, render_template(subject_t, variables),
                 render_template(body_t, variables), key, actor, attachment),
            )
            db._audit_in_transaction(conn, actor, "QUEUE", "communication_messages", cursor.lastrowid)
            queued += 1
    return {"queued": queued, "skipped": skipped}


def list_messages(actor: str, status: str | None = None, kind: str | None = None,
                  starts_on: str | None = None, ends_on: str | None = None, limit: int = 500) -> list[dict]:
    if status not in (None, "pending", "sent", "cancelled", "failed"):
        raise ValueError("Choose a valid status.")
    if kind is not None:
        _kind(kind)
    where, params = [], []
    for column, value in (("status", status), ("kind", kind)):
        if value:
            where.append(f"{column}=?")
            params.append(value)
    if starts_on:
        where.append("date(created_at)>=?")
        params.append(db._iso_date(starts_on, "Start date").isoformat())
    if ends_on:
        where.append("date(created_at)<=?")
        params.append(db._iso_date(ends_on, "End date").isoformat())
    limit = max(1, min(int(limit), 5000))
    with db.connection() as conn:
        _admin(conn, actor)
        rows = conn.execute(
            "SELECT id,kind,channel,student_id,recipient_name,recipient_contact,subject,body,status,created_by,"
            "created_at,handled_by,handled_at,attachment_path,retry_count,last_error FROM communication_messages"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY id DESC LIMIT ?", (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def _handle(message_id: int, new_status: str, actor: str, action: str) -> None:
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        raise ValueError("Select a message.")
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        row = conn.execute("SELECT status FROM communication_messages WHERE id=?", (message_id,)).fetchone()
        if row is None:
            raise ValueError("Select an existing message.")
        if row["status"] != "pending":
            raise ValueError("Only a pending message can be changed.")
        conn.execute(
            "UPDATE communication_messages SET status=?,handled_by=?,handled_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_status, actor, message_id),
        )
        db._audit_in_transaction(conn, actor, action, "communication_messages", message_id)


def mark_sent(message_id: int, actor: str) -> None:
    """Record that a person delivered the message (the app itself never sends it)."""
    _handle(message_id, "sent", actor, "MARK_SENT")


def cancel_message(message_id: int, actor: str) -> None:
    _handle(message_id, "cancelled", actor, "CANCEL")


def requeue_message(message_id: int, actor: str) -> int:
    """Create a fresh pending copy of a sent or cancelled message (for a resend)."""
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        raise ValueError("Select a message.")
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        row = conn.execute("SELECT * FROM communication_messages WHERE id=?", (message_id,)).fetchone()
        if row is None:
            raise ValueError("Select an existing message.")
        if row["status"] == "pending":
            raise ValueError("This message is already pending.")
        if row["student_id"] and conn.execute(
            "SELECT 1 FROM communication_opt_outs WHERE student_id=?", (row["student_id"],)
        ).fetchone():
            raise ValueError("The guardian has opted out of communications.")
        cursor = conn.execute(
            """INSERT INTO communication_messages(kind,channel,student_id,recipient_name,recipient_contact,
                                                  subject,body,created_by,attachment_path) VALUES(?,?,?,?,?,?,?,?,?)""",
            (row["kind"], row["channel"], row["student_id"], row["recipient_name"], row["recipient_contact"],
             row["subject"], row["body"], actor, row["attachment_path"]),
        )
        db._audit_in_transaction(conn, actor, "REQUEUE", "communication_messages", cursor.lastrowid)
        return int(cursor.lastrowid)


# ---------------------------------------------------------------- delivery links & statistics

def mailto_url(contact: str, subject: str, body: str) -> str:
    from urllib.parse import quote
    return f"mailto:{quote(contact)}?subject={quote(subject)}&body={quote(body)}"


def whatsapp_url(contact: str, body: str) -> str:
    from urllib.parse import quote
    return f"https://wa.me/{re.sub(r'[^0-9]', '', contact)}?text={quote(body)}"


def load_statistics(actor: str, today: date | None = None) -> dict:
    today = today or date.today()
    with db.connection() as conn:
        _admin(conn, actor)
        by_status = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status,COUNT(*) n FROM communication_messages GROUP BY status")}
        by_kind = {r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind,COUNT(*) n FROM communication_messages GROUP BY kind")}
        by_channel = {r["channel"]: r["n"] for r in conn.execute(
            "SELECT channel,COUNT(*) n FROM communication_messages GROUP BY channel")}
        months = []
        year, month = today.year, today.month
        for _ in range(6):
            months.append(f"{year:04d}-{month:02d}")
            month -= 1
            if month == 0:
                year, month = year - 1, 12
        months.reverse()
        monthly = {m: 0 for m in months}
        for r in conn.execute(
            "SELECT strftime('%Y-%m',created_at) m,COUNT(*) n FROM communication_messages GROUP BY m"
        ):
            if r["m"] in monthly:
                monthly[r["m"]] = r["n"]
        sent_today = conn.execute(
            "SELECT COUNT(*) FROM communication_messages WHERE status='sent' AND date(handled_at)=?", (today.isoformat(),)
        ).fetchone()[0]
        opted_out = conn.execute("SELECT COUNT(*) FROM communication_opt_outs").fetchone()[0]
    return {
        "pending": by_status.get("pending", 0), "sent": by_status.get("sent", 0),
        "cancelled": by_status.get("cancelled", 0), "total": sum(by_status.values()),
        "sent_today": sent_today, "opted_out": opted_out,
        "by_kind": by_kind, "by_channel": by_channel, "monthly": monthly,
    }



def export_messages_csv(actor: str, path: str, **filters) -> int:
    """Write the (filtered) outbox/history to CSV; returns the number of rows written."""
    import csv

    rows = list_messages(actor, limit=5000, **filters)

    def safe(value):
        return "'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value

    with open(path, "w", newline="", encoding="utf-8-sig") as output:
        writer = csv.writer(output)
        writer.writerow(["Created", "Type", "Channel", "To", "Contact", "Subject", "Status", "Handled by", "Handled at"])
        for row in rows:
            writer.writerow([safe(v) for v in (
                row["created_at"], KINDS[row["kind"]], CHANNELS[row["channel"]], row["recipient_name"],
                row["recipient_contact"], row["subject"], row["status"], row["handled_by"] or "", row["handled_at"] or "",
            )])
    return len(rows)
