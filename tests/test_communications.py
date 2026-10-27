"""Offline communication centre: templates, queueing, opt-outs, history (synthetic data only)."""

from datetime import date

import pytest

from smartschool import communications as comms
from smartschool import database as db

TODAY = date(2026, 10, 27)


def _student(school, number, *, dob="", email="", phone="", gender="Female", class_index=0):
    student = db.add_row("Students", [f"ADM-{number}", "Syn", f"Student{number}", gender, "", phone], actor="admin")
    with db.connection() as conn:
        conn.execute("UPDATE students SET date_of_birth=?,email=? WHERE id=?", (dob, email, student))
    db.create_student_enrollment(student, school["classes"][class_index], school["session"], "2026-09-01", "admin")
    return student


def _owing(school, student, amount="5000"):
    db.save_fee_schedule(school["session"], school["classes"][0], school["terms"][0], school["fees"]["School Fees"], amount, "", "admin")
    batch = db.generate_fee_invoice_batch(school["session"], school["classes"][0], school["terms"][0], "admin")
    db.issue_fee_invoice_batch(batch, "admin")


def test_render_replaces_known_placeholders_only_and_supports_old_alias():
    text = comms.render_template("Hi {student_name} from {principal_name} {unknown} {{x}}", {"student_name": "Ada", "head_name": "Mr B"})
    assert text == "Hi Ada from Mr B {unknown} {{x}}"
    assert comms.unknown_placeholders("{student_name} {nope} {principal_name}") == ["nope"]


def test_phone_normalisation_never_assumes_a_country():
    assert comms.normalize_phone("+44 7700 900123") == "447700900123"
    assert comms.normalize_phone("0044 7700 900123") == "447700900123"
    assert comms.normalize_phone("08031234567") is None            # local number, no code known
    assert comms.normalize_phone("08031234567", "234") == "2348031234567"
    assert comms.normalize_phone("123") is None and comms.normalize_phone("") is None


def test_everything_is_administrator_only(school):
    for call in (lambda: comms.load_templates("teacher"), lambda: comms.list_messages("teacher"),
                 lambda: comms.list_birthdays("teacher"), lambda: comms.load_statistics("teacher"),
                 lambda: comms.save_template("notice", "s", "b", "teacher"),
                 lambda: comms.queue_messages("notice", [1], "email", "teacher")):
        with pytest.raises(PermissionError):
            call()


def test_templates_default_customise_validate_and_reset(school):
    assert not comms.load_templates("admin")["birthday"]["customised"]
    with pytest.raises(ValueError, match="Unknown placeholder"):
        comms.save_template("birthday", "Hi", "Dear {guardian}", "admin")
    comms.save_template("birthday", "Happy day {student_name}", "Body {school_name}", "admin")
    saved = comms.load_templates("admin")["birthday"]
    assert saved["customised"] and saved["subject"] == "Happy day {student_name}"
    comms.reset_template("birthday", "admin")
    assert not comms.load_templates("admin")["birthday"]["customised"]


def test_birthday_queue_uses_template_dedupes_and_skips_bad_contacts(school):
    db.save_school_profile({"school_name": "Synthetic School", "head_name": "Mrs Head"}, "admin")
    with_email = _student(school, 1, dob="2014-10-27", email="guardian@example.test")
    no_contact = _student(school, 2, dob="2013-10-27")
    other_day = _student(school, 3, dob="2013-01-02", email="x@example.test")
    listing = comms.list_birthdays("admin", TODAY)
    assert {r["student_id"] for r in listing["today"]} == {with_email, no_contact}
    assert next(r for r in listing["today"] if r["student_id"] == with_email)["age"] == 12

    result = comms.queue_messages("birthday", [with_email, no_contact, other_day], "email", "admin", TODAY)
    assert result["queued"] == 1
    reasons = {s["student_id"]: s["reason"] for s in result["skipped"]}
    assert "valid email" in reasons[no_contact] and "not today" in reasons[other_day]
    message = comms.list_messages("admin", "pending")[0]
    assert message["recipient_contact"] == "guardian@example.test"
    assert "Syn Student1" in message["body"] and "Synthetic School" in message["body"] and "Mrs Head" in message["body"]
    assert "{" not in message["body"]
    again = comms.queue_messages("birthday", [with_email], "email", "admin", TODAY)
    assert again["queued"] == 0 and "Already queued" in again["skipped"][0]["reason"]
    assert [r["queued"] for r in comms.list_birthdays("admin", TODAY)["today"] if r["student_id"] == with_email] == [True]


def test_whatsapp_needs_a_usable_number(school):
    student = _student(school, 1, dob="2014-10-27", phone="08031234567")
    result = comms.queue_messages("birthday", [student], "whatsapp", "admin", TODAY)
    assert result["queued"] == 0 and "country code" in result["skipped"][0]["reason"]
    comms.save_country_code("+234", "admin")
    assert comms.load_settings("admin")["country_code"] == "234"
    assert comms.queue_messages("birthday", [student], "whatsapp", "admin", TODAY)["queued"] == 1
    message = comms.list_messages("admin")[0]
    assert message["recipient_contact"] == "2348031234567"
    assert comms.whatsapp_url(message["recipient_contact"], "Hi there").startswith("https://wa.me/2348031234567?text=Hi%20there")
    assert comms.mailto_url("a@b.test", "S 1", "B\n2") == "mailto:a%40b.test?subject=S%201&body=B%0A2"
    with pytest.raises(ValueError):
        comms.save_country_code("12345", "admin")


def test_fee_reminders_list_only_students_who_owe_and_carry_the_balance(school):
    owing = _student(school, 1, email="owes@example.test")
    _owing(school, owing)
    paid = _student(school, 2, email="paid@example.test", class_index=1)
    candidates = comms.list_fee_reminder_candidates("admin", TODAY)
    assert [c["student_id"] for c in candidates] == [owing]
    assert candidates[0]["balance"] == "5000"
    assert comms.queue_messages("fee_reminder", [paid], "email", "admin", TODAY)["skipped"][0]["reason"] == "No outstanding balance."
    assert comms.queue_messages("fee_reminder", [owing], "email", "admin", TODAY)["queued"] == 1
    body = comms.list_messages("admin")[0]["body"]
    assert "5000" in body and "First Term" in body and "{" not in body


def test_opt_out_blocks_queueing_cancels_pending_and_blocks_resend(school):
    student = _student(school, 1, dob="2014-10-27", email="g@example.test")
    comms.queue_messages("birthday", [student], "email", "admin", TODAY)
    comms.set_opt_out(student, True, "Asked not to be contacted", "admin")
    assert comms.list_messages("admin")[0]["status"] == "cancelled"
    assert comms.list_birthdays("admin", TODAY)["today"][0]["opted_out"]
    result = comms.queue_messages("birthday", [student], "email", "admin", TODAY)
    assert result["queued"] == 0 and "opted out" in result["skipped"][0]["reason"]
    with pytest.raises(ValueError, match="opted out"):
        comms.requeue_message(comms.list_messages("admin")[0]["id"], "admin")
    comms.set_opt_out(student, False, "", "admin")
    assert comms.queue_messages("birthday", [student], "email", "admin", date(2026, 10, 27))["queued"] == 1


def test_message_lifecycle_history_filters_statistics_and_audit(school):
    a = _student(school, 1, dob="2014-10-27", email="a@example.test")
    b = _student(school, 2, dob="2014-10-27", email="b@example.test")
    comms.queue_messages("birthday", [a, b], "email", "admin", TODAY)
    first, second = sorted(comms.list_messages("admin", "pending"), key=lambda m: m["id"])
    comms.mark_sent(first["id"], "admin")
    comms.cancel_message(second["id"], "admin")
    with pytest.raises(ValueError, match="pending"):
        comms.mark_sent(first["id"], "admin")
    copy_id = comms.requeue_message(first["id"], "admin")
    assert comms.list_messages("admin", "pending")[0]["id"] == copy_id
    assert {m["status"] for m in comms.list_messages("admin", kind="birthday")} == {"pending", "sent", "cancelled"}
    assert comms.list_messages("admin", "sent", starts_on="2000-01-01", ends_on="2100-01-01")
    stats = comms.load_statistics("admin", date.today())
    assert (stats["pending"], stats["sent"], stats["cancelled"], stats["total"]) == (1, 1, 1, 3)
    assert stats["by_kind"] == {"birthday": 3} and len(stats["monthly"]) == 6
    with db.connection() as conn:
        actions = {r[0] for r in conn.execute("SELECT action FROM audit_log WHERE entity='communication_messages'")}
    assert {"QUEUE", "MARK_SENT", "CANCEL", "REQUEUE"} <= actions


def test_leap_day_birthday_shows_on_feb_28_in_common_years(school):
    student = _student(school, 1, dob="2016-02-29", email="l@example.test")
    assert comms.list_birthdays("admin", date(2027, 2, 28))["today"][0]["student_id"] == student
    assert not comms.list_birthdays("admin", date(2027, 3, 1))["today"]
    assert comms.list_birthdays("admin", date(2028, 2, 29))["today"][0]["student_id"] == student


def test_history_csv_neutralises_spreadsheet_formulas(school, tmp_path):
    student = _student(school, 1, dob="2014-10-27", email="g@example.test")
    comms.queue_messages("birthday", [student], "email", "admin", TODAY)
    comms.save_template("birthday", "=HYPERLINK(1)", "Body", "admin")
    other = _student(school, 2, dob="2014-10-27", email="h@example.test")
    comms.queue_messages("birthday", [other], "email", "admin", TODAY)
    path = tmp_path / "history.csv"
    assert comms.export_messages_csv("admin", str(path)) == 2
    text = path.read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK(1)" in text and "Subject" in text
    with pytest.raises(PermissionError):
        comms.export_messages_csv("teacher", str(tmp_path / "x.csv"))
