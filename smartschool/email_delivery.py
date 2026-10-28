"""Send queued emails over SMTP (Qt-free; network work is injectable so it can be tested offline).

Settings live in ``communication_settings``. The SMTP password is stored in the local database, like the
old application did; it is never written to the audit log, error text or the settings returned to screens.
"""

import mimetypes
import re
import smtplib
import socket
import ssl
import time
from datetime import date
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from . import credits
from . import database as db

SECURITY = {"ssl": "SSL/TLS (usually port 465)", "starttls": "STARTTLS (usually port 587)", "none": "None (not recommended)"}
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LIMITS = {"daily_limit": (1, 5000, 300), "delay_seconds": (0, 60, 3), "max_retries": (1, 10, 3)}


def _admin(conn, actor):
    return db._require_admin(conn, actor)


def _setting_rows(conn) -> dict:
    return {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM communication_settings")}


def _public(values: dict) -> dict:
    result = {
        "smtp_host": values.get("smtp_host", ""), "smtp_port": int(values.get("smtp_port") or 587),
        "smtp_security": values.get("smtp_security", "starttls"), "smtp_username": values.get("smtp_username", ""),
        "sender_email": values.get("sender_email", ""), "sender_name": values.get("sender_name", ""),
        "has_password": bool(values.get("smtp_password")),
    }
    for key, (_low, _high, default) in _LIMITS.items():
        result[key] = int(values.get(key) or default)
    result["configured"] = bool(result["smtp_host"] and result["sender_email"])
    return result


def load_smtp_settings(actor: str) -> dict:
    """Settings for the screen. The password itself is never returned, only whether one is saved."""
    with db.connection() as conn:
        _admin(conn, actor)
        return _public(_setting_rows(conn))


def save_smtp_settings(actor: str, *, host: str, port: int, security: str, username: str, sender_email: str,
                       sender_name: str = "", password: str | None = None, daily_limit: int = 300,
                       delay_seconds: int = 3, max_retries: int = 3) -> None:
    """Save SMTP settings. ``password=None`` keeps the saved password; ``""`` removes it."""
    host = (host or "").strip()
    username = (username or "").strip()
    sender_email = (sender_email or "").strip()
    sender_name = " ".join((sender_name or "").split())
    if not host or len(host) > 200 or re.search(r"[\s/]", host):
        raise ValueError("Enter the SMTP server name, for example smtp.gmail.com.")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("The port must be a number from 1 to 65535.")
    if security not in SECURITY:
        raise ValueError("Choose a connection security option.")
    if not _EMAIL.match(sender_email) or len(sender_email) > 200:
        raise ValueError("Enter a valid sender email address.")
    if len(username) > 200 or len(sender_name) > 100:
        raise ValueError("The username or sender name is too long.")
    if password is not None and (len(password) > 200 or "\n" in password or "\r" in password):
        raise ValueError("The password is not valid.")
    limits = {"daily_limit": daily_limit, "delay_seconds": delay_seconds, "max_retries": max_retries}
    for key, value in limits.items():
        low, high, _default = _LIMITS[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key.replace('_', ' ').capitalize()} must be between {low} and {high}.")
    values = {"smtp_host": host, "smtp_port": str(port), "smtp_security": security, "smtp_username": username,
              "sender_email": sender_email, "sender_name": sender_name, **{k: str(v) for k, v in limits.items()}}
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        for key, value in values.items():
            conn.execute(
                "INSERT INTO communication_settings(key,value,updated_by) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_by=excluded.updated_by,"
                "updated_at=CURRENT_TIMESTAMP", (key, value, actor),
            )
        if password == "":
            conn.execute("DELETE FROM communication_settings WHERE key='smtp_password'")
        elif password is not None:
            conn.execute(
                "INSERT INTO communication_settings(key,value,updated_by) VALUES('smtp_password',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_by=excluded.updated_by,"
                "updated_at=CURRENT_TIMESTAMP", (password, actor),
            )
        db._audit_in_transaction(conn, actor, "UPDATE", "communication_settings", "smtp")  # never the password


def _load_private(conn) -> dict:
    values = _setting_rows(conn)
    settings = _public(values)
    settings["password"] = values.get("smtp_password", "")
    return settings


def friendly_error(exc: BaseException) -> str:
    """Short, safe text for a delivery failure (never contains the password)."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "The server rejected the username or password (Gmail needs an app password)."
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "The recipient address was refused by the server."
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "The sender address was refused by the server."
    if isinstance(exc, smtplib.SMTPDataError):
        return "The server refused the message."
    if isinstance(exc, (smtplib.SMTPConnectError, ConnectionRefusedError)):
        return "Could not connect to the mail server. Check the server name and port."
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "The mail server took too long to answer."
    if isinstance(exc, socket.gaierror):
        return "The mail server name could not be found. Check the server name and your internet connection."
    if isinstance(exc, ssl.SSLError):
        return "A secure connection could not be made. Try the other security option or port."
    if isinstance(exc, smtplib.SMTPException):
        return "The mail server reported an error."
    if isinstance(exc, FileNotFoundError):
        return "The attached report card file was not found."
    if isinstance(exc, OSError):
        return "A network error occurred. Check your internet connection."
    return "The email could not be sent."


def _connect(settings: dict, timeout: float, smtp_factory=None):
    host, port = settings["smtp_host"], settings["smtp_port"]
    if smtp_factory:
        server = smtp_factory(host, port, settings["smtp_security"], timeout)
    elif settings["smtp_security"] == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(host, port, timeout=timeout)
        if settings["smtp_security"] == "starttls":
            server.starttls(context=ssl.create_default_context())
    if settings["smtp_username"] and settings["password"]:
        server.login(settings["smtp_username"], settings["password"])
    return server


def build_message(settings: dict, row, attachment_dir: Path | None = None) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((settings["sender_name"], settings["sender_email"]))
    message["To"] = row["recipient_contact"]
    message["Subject"] = re.sub(r"[\r\n]+", " ", row["subject"])
    message.set_content(row["body"])
    path = row["attachment_path"]
    if path:
        file = Path(path)
        if not file.is_file():
            raise FileNotFoundError(path)
        if file.stat().st_size > MAX_ATTACHMENT_BYTES:
            raise ValueError("The attachment is larger than 10 MB.")
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        maintype, _, subtype = mime.partition("/")
        message.add_attachment(file.read_bytes(), maintype=maintype, subtype=subtype or "octet-stream",
                               filename=file.name)
    return message


def check_connection(actor: str, smtp_factory=None, timeout: float = 15) -> str:
    """Connect and log in without sending anything. Returns a friendly success text or raises ValueError."""
    with db.connection() as conn:
        _admin(conn, actor)
        settings = _load_private(conn)
    if not settings["configured"]:
        raise ValueError("Save the SMTP settings first.")
    try:
        server = _connect(settings, timeout, smtp_factory)
        try:
            server.noop()
        finally:
            try:
                server.quit()
            except Exception:
                pass
    except Exception as exc:  # noqa: BLE001 - reported as friendly text
        raise ValueError(friendly_error(exc)) from None
    return "Connected successfully."




def sent_today(conn) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM communication_messages WHERE channel='email' AND status='sent' "
        "AND date(handled_at)=date('now')").fetchone()[0])


def process_queue(actor: str, *, smtp_factory=None, sleep=time.sleep, limit: int | None = None,
                  timeout: float = 20, progress=None) -> dict:
    """Send pending emails one at a time. Safe to run in a background worker.

    Stops early on the daily limit or when credits run out. A failed attempt is kept with its error and tried
    again on the next run until ``max_retries`` is reached, after which the message is marked failed.
    """
    with db.connection() as conn:
        actor = _admin(conn, actor)
        settings = _load_private(conn)
        pending = [dict(r) for r in conn.execute(
            "SELECT * FROM communication_messages WHERE channel='email' AND status='pending' ORDER BY id")]
        already_sent = sent_today(conn)
    if not settings["configured"]:
        raise ValueError("Set up the SMTP email account first (Communication Center > Email settings).")
    result = {"sent": 0, "failed": 0, "retrying": 0, "stopped": ""}
    if limit is not None:
        pending = pending[:limit]
    server = None
    try:
        for index, row in enumerate(pending):
            if already_sent + result["sent"] >= settings["daily_limit"]:
                result["stopped"] = "The daily sending limit was reached. Remaining emails stay pending."
                break
            cost = credits.message_cost(row["kind"], "email")
            with db.connection() as conn:
                if cost and credits.balance_in_transaction(conn) < cost:
                    result["stopped"] = "Not enough communication credits. Redeem a code to continue."
                    break
            error = None
            try:
                message = build_message(settings, row)
                if server is None:
                    server = _connect(settings, timeout, smtp_factory)
                server.send_message(message)
            except Exception as exc:  # noqa: BLE001
                error = friendly_error(exc) if not isinstance(exc, ValueError) else str(exc)[:300]
                if server is not None and isinstance(exc, (smtplib.SMTPServerDisconnected, OSError)):
                    server = None
            with db.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if error is None:
                    if cost:
                        credits.deduct_in_transaction(conn, cost, f"{row['kind']} email to {row['recipient_name']}",
                                                      f"msg{row['id']}", actor)
                    conn.execute(
                        "UPDATE communication_messages SET status='sent',handled_by=?,handled_at=CURRENT_TIMESTAMP,"
                        "last_attempt_at=CURRENT_TIMESTAMP,last_error=NULL,credit_cost=? WHERE id=?",
                        (actor, cost, row["id"]))
                    db._audit_in_transaction(conn, actor, "EMAIL_SENT", "communication_messages", row["id"])
                    result["sent"] += 1
                else:
                    retries = row["retry_count"] + 1
                    final = retries >= settings["max_retries"]
                    conn.execute(
                        "UPDATE communication_messages SET retry_count=?,last_error=?,last_attempt_at=CURRENT_TIMESTAMP,"
                        "status=?,handled_by=?,handled_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE handled_at END "
                        "WHERE id=?", (retries, error, "failed" if final else "pending", actor if final else None,
                                       final, row["id"]))
                    db._audit_in_transaction(conn, actor, "EMAIL_FAILED", "communication_messages", row["id"])
                    result["failed" if final else "retrying"] += 1
            if progress:
                progress(index + 1, len(pending))
            if index < len(pending) - 1 and settings["delay_seconds"]:
                sleep(settings["delay_seconds"])
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass
    return result


def retry_failed(message_id: int, actor: str) -> None:
    """Put a failed email back in the queue with a fresh retry count."""
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        raise ValueError("Select a message.")
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        row = conn.execute("SELECT status,channel FROM communication_messages WHERE id=?", (message_id,)).fetchone()
        if row is None or row["channel"] != "email":
            raise ValueError("Select an existing email.")
        if row["status"] != "failed":
            raise ValueError("Only a failed email can be retried.")
        conn.execute("UPDATE communication_messages SET status='pending',retry_count=0,last_error=NULL,"
                     "handled_by=NULL,handled_at=NULL WHERE id=?", (message_id,))
        db._audit_in_transaction(conn, actor, "RETRY", "communication_messages", message_id)


def queue_report_cards(actor: str, pdfs: dict, today: date | None = None) -> dict:
    """Queue report-card emails. ``pdfs`` maps student id to the saved PDF path. Uses each profile's email."""
    from . import communications
    if not pdfs:
        raise ValueError("There are no report cards to email.")
    return communications.queue_messages("report_card", list(pdfs), "email", actor, today=today, attachments=pdfs)
