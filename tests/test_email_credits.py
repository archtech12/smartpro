"""SMTP delivery queue and credit vouchers (fake SMTP server, synthetic data only)."""

import smtplib
import sqlite3
from datetime import date

import pytest

from smartschool import communications as comms
from smartschool import credits, email_delivery as mail
from smartschool import database as db
from tests.test_communications import _owing, _student

TODAY = date(2026, 10, 27)
SECRET = b"unit-test-secret"


class FakeSMTP:
    instances = []

    def __init__(self, fail_with=None):
        self.sent, self.fail_with, self.closed = [], fail_with, False

    def login(self, user, password):
        pass

    def noop(self):
        return (250, b"ok")

    def send_message(self, message):
        if self.fail_with:
            raise self.fail_with
        self.sent.append(message)

    def quit(self):
        self.closed = True


def _factory(server):
    return lambda host, port, security, timeout: server


def _configure(**overrides):
    values = dict(host="smtp.example.test", port=587, security="starttls", username="u", sender_email="school@example.test",
                  sender_name="Syn School", password="secret-pw", delay_seconds=0)
    values.update(overrides)
    mail.save_smtp_settings("admin", **values)


def test_smtp_settings_validate_hide_password_and_are_admin_only(school):
    with pytest.raises(PermissionError):
        mail.load_smtp_settings("teacher")
    for bad in ({"host": "a b"}, {"port": 0}, {"security": "x"}, {"sender_email": "nope"}, {"daily_limit": 0}):
        with pytest.raises(ValueError):
            _configure(**bad)
    _configure()
    loaded = mail.load_smtp_settings("admin")
    assert loaded["configured"] and loaded["has_password"] and "password" not in loaded
    mail.save_smtp_settings("admin", host="smtp.example.test", port=465, security="ssl", username="u",
                            sender_email="school@example.test")  # password=None keeps it
    assert mail.load_smtp_settings("admin")["has_password"]
    mail.save_smtp_settings("admin", host="smtp.example.test", port=465, security="ssl", username="u",
                            sender_email="school@example.test", password="")
    assert not mail.load_smtp_settings("admin")["has_password"]


def test_connection_check_reports_friendly_errors(school):
    with pytest.raises(ValueError, match="Save the SMTP settings"):
        mail.check_connection("admin")
    _configure()
    assert mail.check_connection("admin", smtp_factory=_factory(FakeSMTP())) == "Connected successfully."

    def refuse(*args):
        raise smtplib.SMTPAuthenticationError(535, b"bad")
    with pytest.raises(ValueError, match="username or password"):
        mail.check_connection("admin", smtp_factory=refuse)


def test_queue_sends_email_with_attachment_and_counts_credits(school, tmp_path):
    student = _student(school, 1, email="parent@example.test")
    pdf = tmp_path / "card.pdf"
    pdf.write_bytes(b"%PDF-1.4 synthetic")
    _configure()
    result = mail.queue_report_cards("admin", {student: str(pdf)}, today=TODAY)
    assert result["queued"] == 1
    server = FakeSMTP()
    outcome = mail.process_queue("admin", smtp_factory=_factory(server), sleep=lambda s: None)
    assert outcome["sent"] == 1 and server.closed
    message = server.sent[0]
    assert message["To"] == "parent@example.test" and "report card" in message["Subject"]
    assert [part.get_filename() for part in message.iter_attachments()] == ["card.pdf"]
    assert comms.list_messages("admin", status="sent")


def test_failed_sends_retry_then_fail_and_can_be_retried(school):
    student = _student(school, 2, email="p2@example.test")
    _configure(max_retries=2)
    comms.queue_messages("notice", [student], "email", "admin", today=TODAY)
    server = FakeSMTP(fail_with=smtplib.SMTPRecipientsRefused({}))
    first = mail.process_queue("admin", smtp_factory=_factory(server), sleep=lambda s: None)
    assert first == {"sent": 0, "failed": 0, "retrying": 1, "stopped": ""}
    second = mail.process_queue("admin", smtp_factory=_factory(server), sleep=lambda s: None)
    assert second["failed"] == 1
    failed = comms.list_messages("admin", status="failed")
    assert failed and "refused" in failed[0]["last_error"]
    message_id = failed[0]["id"]
    mail.retry_failed(message_id, "admin")
    ok = mail.process_queue("admin", smtp_factory=_factory(FakeSMTP()), sleep=lambda s: None)
    assert ok["sent"] == 1


def test_daily_limit_and_unconfigured_queue(school):
    students = [_student(school, n, email=f"p{n}@example.test") for n in (3, 4, 5)]
    comms.queue_messages("notice", students, "email", "admin", today=TODAY)
    with pytest.raises(ValueError, match="SMTP"):
        mail.process_queue("admin", smtp_factory=_factory(FakeSMTP()))
    _configure(daily_limit=2)
    outcome = mail.process_queue("admin", smtp_factory=_factory(FakeSMTP()), sleep=lambda s: None)
    assert outcome["sent"] == 2 and "daily" in outcome["stopped"]


def test_fee_reminder_costs_a_credit_and_stops_without_credits(school):
    student = _student(school, 6, email="p6@example.test")
    _owing(school, student)
    _configure()
    comms.queue_messages("fee_reminder", [student], "email", "admin", today=TODAY)
    out = mail.process_queue("admin", smtp_factory=_factory(FakeSMTP()), sleep=lambda s: None)
    assert out["sent"] == 0 and "credits" in out["stopped"]
    code = credits.issue_voucher(SECRET, credits.get_license_id("admin"), 5)
    assert credits.redeem_voucher(code, "admin", credits.HmacVoucherVerifier(SECRET)) == {"added": 5, "balance": 5}
    out = mail.process_queue("admin", smtp_factory=_factory(FakeSMTP()), sleep=lambda s: None)
    assert out["sent"] == 1 and credits.get_balance("admin") == 4
    assert [row["entry_type"] for row in credits.list_ledger("admin")] == ["deduction", "topup"]


def test_vouchers_are_verified_single_use_and_licence_bound(school):
    verifier = credits.HmacVoucherVerifier(SECRET)
    licence = credits.get_license_id("admin")
    assert credits.get_license_id("admin") == licence
    code = credits.issue_voucher(SECRET, licence, 100)
    assert code.startswith("SSM-") and len(code) == 23
    assert credits.redeem_voucher(code.lower(), "admin", verifier)["balance"] == 100
    with pytest.raises(ValueError, match="already been used"):
        credits.redeem_voucher(code, "admin", verifier)
    with pytest.raises(ValueError, match="different school"):
        credits.redeem_voucher(credits.issue_voucher(SECRET, "SCH-OTHER", 10), "admin", verifier)
    with pytest.raises(ValueError, match="not valid"):
        credits.redeem_voucher(credits.issue_voucher(b"wrong-secret", licence, 10), "admin", verifier)
    with pytest.raises(ValueError, match="not a valid"):
        credits.redeem_voucher("garbage", "admin", verifier)
    with pytest.raises(ValueError, match="no voucher secret"):
        credits.redeem_voucher(code, "admin", credits.HmacVoucherVerifier())
    with pytest.raises(PermissionError):
        credits.redeem_voucher(code, "teacher", verifier)
    assert credits.get_balance("admin") == 100


def test_ledger_rejects_overdraft_and_invalid_rows(school):
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="Not enough"):
            credits.deduct_in_transaction(conn, 1, "x", "r", "admin")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO communication_credits(entry_type,amount,description,created_by) VALUES('topup',-5,'x','a')")


def test_v26_messages_survive_the_v27_upgrade(school):
    comms.queue_messages("notice", [_student(school, 7, email="p7@example.test")], "email", "admin", today=TODAY)
    with db.connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM communication_messages").fetchone()[0]
    assert count == 1
    db.initialize()
    assert len(comms.list_messages("admin")) == 1
