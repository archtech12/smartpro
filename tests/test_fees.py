import sqlite3
import os
import time
from datetime import date, timedelta

import pytest

from smartschool import database as db


def test_fee_invoice_search_filters_the_loaded_scope(fee_db):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtTest import QTest
    from smartschool.fees_ui import FeeInvoicePage

    _add_invoice_student(fee_db, "A-100")
    _add_invoice_student(fee_db, "B-200")
    fee_type_id = db.create_fee_type("Tuition", "", "admin")
    db.save_fee_schedule(
        fee_db["session"], fee_db["class"], fee_db["term"], fee_type_id,
        "120.50", "", "admin",
    )
    second_class = db.add_row("Classes", ["Grade 5", "B", ""], actor="admin")
    second_student_id = db.add_row("Students", ["C-300", "Synthetic", "Student", "", "", ""], actor="admin")
    db.create_student_enrollment(second_student_id, second_class, fee_db["session"], "2026-09-01", "admin")
    db.save_fee_schedule(
        fee_db["session"], second_class, fee_db["term"], fee_type_id, "120.50", "", "admin"
    )
    batch_id = db.generate_fee_invoice_batch(fee_db["session"], fee_db["class"], fee_db["term"], "admin")
    db.generate_fee_invoice_batch(fee_db["session"], second_class, fee_db["term"], "admin")
    data = db.load_fee_invoice_data(fee_db["session"], fee_db["class"], fee_db["term"])
    first_invoice = next(row for row in data["invoices"] if row["admission_no_snapshot"] == "A-100")
    db.issue_fee_invoice_batch(batch_id, "admin")
    db.record_fee_payment(first_invoice["id"], "20", "2026-09-02", "cash", "test-ref", "admin")

    app = QApplication.instance() or QApplication([])
    page = FeeInvoicePage("admin", "admin")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and "loaded" not in page.status.text().lower():
        app.processEvents()
        QTest.qWait(20)
    assert "select an issued invoice" in page.status.text().lower()
    assert not page.payment_button.isEnabled()
    assert len(page._data["invoices"]) == 3
    assert page.class_section.currentData() is None
    assert page.term.currentData() is None

    page.invoice_search.setText("a-100")
    assert page.invoice_table.rowCount() == 1
    assert page.invoice_table.item(0, 1).text() == "A-100"
    assert "1 invoice · 1 issued · Billed 120.5 · Paid 20 · Due 100.5" in page.invoice_search_status.text()
    page.invoice_table.selectRow(0)
    assert page.payment_button.isEnabled()
    assert page.payment_table.rowCount() == 1
    assert page.class_balance_table.rowCount() == 1
    assert [page.class_balance_table.item(0, column).text() for column in range(7)] == [
        "Grade 4 A", "1", "1", "120.5", "20", "100.5", "0",
    ]
    assert page.fee_views.currentIndex() == 0
    page.fee_view.setCurrentIndex(1)
    assert page.fee_views.currentIndex() == 1
    page.fee_view.setCurrentIndex(2)
    assert page.fee_views.currentIndex() == 2
    page.fee_view.setCurrentIndex(3)
    assert page.fee_views.currentWidget() is page.fee_views.widget(3)
    page.fee_view.setCurrentIndex(0)

    page.invoice_search.setText("synthetic student")
    assert page.invoice_table.rowCount() == 3
    assert page.payment_table.rowCount() == 1
    assert page.class_balance_table.rowCount() == 2

    index = page.class_section.findData(second_class)
    page.class_section.setCurrentIndex(index)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and len(page._data["invoices"]) != 1:
        app.processEvents()
        QTest.qWait(20)
    assert page.invoice_table.rowCount() == 1
    assert page.invoice_table.item(0, 3).text() == "Grade 5 B"
    page.close()


@pytest.fixture
def fee_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    db.create_user("admin", "synthetic-hash")
    db.create_user("teacher", "synthetic-hash", "teacher")
    class_id = db.add_row("Classes", ["Grade 4", "A", ""], actor="admin")
    session_id = db.save_academic_session(
        "2026/2027", "2026-09-01", "2027-07-31", True, "admin"
    )
    term_id = db.save_term(
        session_id, "First Term", 1, "2026-09-01", "2026-12-20", "admin"
    )
    return {"class": class_id, "session": session_id, "term": term_id}


def test_fee_setup_schema_is_migrated_and_empty(fee_db):
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert conn.execute("SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1").fetchone()[0] == db.SCHEMA_VERSION
    data = db.load_fee_setup_data()
    assert data["fee_types"] == []
    assert data["schedules"] == []


def test_fee_type_requires_admin_and_rejects_duplicates(fee_db):
    fee_type_id = db.create_fee_type(" Tuition ", " Core tuition ", "admin")
    assert fee_type_id > 0
    with pytest.raises(PermissionError):
        db.create_fee_type("Books", "", "teacher")
    with pytest.raises(ValueError, match="already exists"):
        db.create_fee_type("TUITION", "Duplicate", "admin")
    data = db.load_fee_setup_data()
    assert data["fee_types"][0]["name"] == "Tuition"
    with db.connection() as conn:
        event = conn.execute(
            "SELECT action,entity,entity_id FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert tuple(event) == ("CREATE", "fee_types", str(fee_type_id))


def test_fee_schedule_uses_exact_amount_and_checks_session_term_scope(fee_db):
    fee_type_id = db.create_fee_type("Tuition", "", "admin")
    schedule_id = db.save_fee_schedule(
        fee_db["session"], fee_db["class"], fee_db["term"], fee_type_id,
        "1250.5000", "2026-10-15", "admin",
    )
    schedule = db.load_fee_setup_data()["schedules"][0]
    assert schedule["id"] == schedule_id
    assert schedule["amount_text"] == "1250.5"
    assert schedule["due_on"] == "2026-10-15"
    assert schedule["is_active"] == 1

    other_session = db.save_academic_session(
        "2027/2028", "2027-09-01", "2028-07-31", False, "admin"
    )
    with pytest.raises(ValueError, match="term from the selected academic session"):
        db.save_fee_schedule(
            other_session, fee_db["class"], fee_db["term"], fee_type_id,
            "10", "", "admin",
        )


@pytest.mark.parametrize("amount,due_on,message", [
    ("0", "", "greater than zero"),
    ("-1", "", "greater than zero"),
    ("NaN", "", "finite"),
    ("100", "15/10/2026", "YYYY-MM-DD"),
])
def test_fee_schedule_rejects_invalid_amount_or_date(fee_db, amount, due_on, message):
    fee_type_id = db.create_fee_type("Tuition", "", "admin")
    with pytest.raises(ValueError, match=message):
        db.save_fee_schedule(
            fee_db["session"], fee_db["class"], fee_db["term"], fee_type_id,
            amount, due_on, "admin",
        )
    assert db.load_fee_setup_data()["schedules"] == []


def test_fee_schedule_is_unique_and_soft_archived(fee_db):
    fee_type_id = db.create_fee_type("Tuition", "", "admin")
    schedule_id = db.save_fee_schedule(
        fee_db["session"], fee_db["class"], fee_db["term"], fee_type_id,
        "100", "", "admin",
    )
    with pytest.raises(ValueError, match="already exists"):
        db.save_fee_schedule(
            fee_db["session"], fee_db["class"], fee_db["term"], fee_type_id,
            "200", "", "admin",
        )
    with pytest.raises(ValueError, match="active schedules"):
        db.set_fee_type_active(fee_type_id, False, "admin")
    db.set_fee_schedule_active(schedule_id, False, "admin")
    db.set_fee_type_active(fee_type_id, False, "admin")
    with pytest.raises(ValueError, match="active fee type"):
        db.save_fee_schedule(
            fee_db["session"], fee_db["class"], fee_db["term"], fee_type_id,
            "200", "", "admin",
        )
    db.set_fee_type_active(fee_type_id, True, "admin")
    db.save_fee_schedule(
        fee_db["session"], fee_db["class"], fee_db["term"], fee_type_id,
        "200", "", "admin", schedule_id=schedule_id,
    )
    assert db.load_fee_setup_data()["schedules"][0]["amount_text"] == "200"


def test_fee_schedule_audit_failure_rolls_back(fee_db, monkeypatch):
    fee_type_id = db.create_fee_type("Tuition", "", "admin")

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic fee audit failure")

    monkeypatch.setattr(db, "_audit_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="fee audit failure"):
        db.save_fee_schedule(
            fee_db["session"], fee_db["class"], fee_db["term"], fee_type_id,
            "100", "", "admin",
        )
    assert db.load_fee_setup_data()["schedules"] == []


def test_fee_migration_failure_rolls_back_ddl(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    monkeypatch.setattr(db, "SCHEMA_VERSION", 13)
    db.initialize()
    monkeypatch.setattr(db, "SCHEMA_VERSION", 14)
    migration = db._migration_14

    def fail_after_fee_ddl(conn):
        migration(conn)
        raise RuntimeError("synthetic fee migration failure")

    monkeypatch.setattr(db, "_migration_14", fail_after_fee_ddl)
    with pytest.raises(RuntimeError, match="fee migration failure"):
        db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='fee_types'").fetchone() is None
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='fee_schedules'").fetchone() is None


def _add_invoice_student(fee_db, admission_no="A-100", enrolled_on="2026-09-01"):
    student_id = db.add_row("Students", [admission_no, "Synthetic", "Student", "", "", ""], actor="admin")
    return db.create_student_enrollment(student_id, fee_db["class"], fee_db["session"], enrolled_on, "admin")


def _add_tuition_schedule(fee_db, amount="120.50"):
    fee_type_id = db.create_fee_type("Tuition", "", "admin")
    db.save_fee_schedule(fee_db["session"], fee_db["class"], fee_db["term"], fee_type_id, amount, "2026-10-01", "admin")


def test_invoice_preview_and_generation_snapshot_roster_and_fees(fee_db):
    _add_invoice_student(fee_db)
    _add_invoice_student(fee_db, "A-101", "2026-10-01")
    _add_tuition_schedule(fee_db)

    preview = db.preview_fee_invoices(fee_db["session"], fee_db["class"], fee_db["term"])
    assert preview["student_count"] == 2
    assert preview["review_count"] == 1
    assert [item["total_amount_text"] for item in preview["students"]] == ["120.5", "120.5"]
    batch_id = db.generate_fee_invoice_batch(fee_db["session"], fee_db["class"], fee_db["term"], "admin")
    data = db.load_fee_invoice_data(fee_db["session"], fee_db["class"])
    assert len(data["batches"]) == 1
    assert data["batches"][0]["id"] == batch_id
    assert data["batches"][0]["invoice_count"] == 2
    assert data["batches"][0]["review_count"] == 1
    with db.connection() as conn:
        invoice = conn.execute("SELECT * FROM fee_invoices WHERE admission_no_snapshot='A-100'").fetchone()
        line = conn.execute("SELECT * FROM fee_invoice_lines WHERE invoice_id=?", (invoice["id"],)).fetchone()
        assert invoice["total_amount_text"] == "120.5" and invoice["review_required"] == 0
        assert line["fee_type_name_snapshot"] == "Tuition" and line["amount_text_snapshot"] == "120.5"
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE fee_invoices SET total_amount_text='0' WHERE id=?", (invoice["id"],))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM fee_invoice_lines WHERE id=?", (line["id"],))


def test_invoice_generation_requires_admin_and_only_one_draft(fee_db):
    _add_invoice_student(fee_db)
    _add_tuition_schedule(fee_db)
    with pytest.raises(PermissionError):
        db.generate_fee_invoice_batch(fee_db["session"], fee_db["class"], fee_db["term"], "teacher")
    db.generate_fee_invoice_batch(fee_db["session"], fee_db["class"], fee_db["term"], "admin")
    with pytest.raises(ValueError, match="already exists"):
        db.generate_fee_invoice_batch(fee_db["session"], fee_db["class"], fee_db["term"], "admin")


def test_invoice_preview_rejects_missing_fees_or_roster(fee_db):
    with pytest.raises(ValueError, match="at least one active fee schedule"):
        db.preview_fee_invoices(fee_db["session"], fee_db["class"], fee_db["term"])
    _add_tuition_schedule(fee_db)
    with pytest.raises(ValueError, match="No active student enrollments"):
        db.generate_fee_invoice_batch(fee_db["session"], fee_db["class"], fee_db["term"], "admin")
    assert db.load_fee_invoice_data(fee_db["session"], fee_db["class"])["batches"] == []


def test_cancel_draft_is_audited_and_allows_regeneration(fee_db):
    _add_invoice_student(fee_db)
    _add_tuition_schedule(fee_db)
    batch_id = db.generate_fee_invoice_batch(fee_db["session"], fee_db["class"], fee_db["term"], "admin")
    with pytest.raises(ValueError, match="between 5 and 300"):
        db.cancel_fee_invoice_batch(batch_id, "no", "admin")
    db.cancel_fee_invoice_batch(batch_id, "Incorrect roster", "admin")
    with pytest.raises(ValueError, match="Only an existing draft"):
        db.cancel_fee_invoice_batch(batch_id, "Again cancelled", "admin")
    replacement = db.generate_fee_invoice_batch(fee_db["session"], fee_db["class"], fee_db["term"], "admin")
    assert replacement != batch_id
    with db.connection() as conn:
        batch = conn.execute("SELECT status,cancel_reason FROM fee_invoice_batches WHERE id=?", (batch_id,)).fetchone()
        assert tuple(batch) == ("cancelled", "Incorrect roster")
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE entity='fee_invoice_batches'").fetchone()[0] == 3


def test_invoice_generation_audit_failure_rolls_back_everything(fee_db, monkeypatch):
    _add_invoice_student(fee_db)
    _add_tuition_schedule(fee_db)
    original = db._audit_in_transaction
    def fail_invoice_audit(conn, actor, action, entity, entity_id):
        if entity == "fee_invoice_batches":
            raise sqlite3.OperationalError("synthetic invoice audit failure")
        return original(conn, actor, action, entity, entity_id)
    monkeypatch.setattr(db, "_audit_in_transaction", fail_invoice_audit)
    with pytest.raises(sqlite3.OperationalError, match="invoice audit failure"):
        db.generate_fee_invoice_batch(fee_db["session"], fee_db["class"], fee_db["term"], "admin")
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM fee_invoice_batches").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM fee_invoices").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM fee_invoice_lines").fetchone()[0] == 0


def test_invoice_migration_failure_rolls_back_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    monkeypatch.setattr(db, "SCHEMA_VERSION", 14)
    db.initialize()
    monkeypatch.setattr(db, "SCHEMA_VERSION", 15)
    migration = db._migration_15
    def fail_after_invoice_ddl(conn):
        migration(conn)
        raise RuntimeError("synthetic invoice migration failure")
    monkeypatch.setattr(db, "_migration_15", fail_after_invoice_ddl)
    with pytest.raises(RuntimeError, match="invoice migration failure"):
        db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 14
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='fee_invoice_batches'").fetchone() is None


def _issued_invoice(fee_db):
    _add_invoice_student(fee_db)
    _add_tuition_schedule(fee_db)
    batch_id = db.generate_fee_invoice_batch(fee_db["session"], fee_db["class"], fee_db["term"], "admin")
    with db.connection() as conn:
        invoice_id = int(conn.execute("SELECT id FROM fee_invoices WHERE batch_id=?", (batch_id,)).fetchone()[0])
    return batch_id, invoice_id


def test_invoice_issue_is_admin_only_and_issued_batches_cannot_be_cancelled(fee_db):
    batch_id, _invoice_id = _issued_invoice(fee_db)
    with pytest.raises(PermissionError):
        db.issue_fee_invoice_batch(batch_id, "teacher")
    db.issue_fee_invoice_batch(batch_id, "admin")
    with pytest.raises(ValueError, match="already been issued"):
        db.issue_fee_invoice_batch(batch_id, "admin")
    with pytest.raises(sqlite3.IntegrityError, match="cannot be cancelled"):
        db.cancel_fee_invoice_batch(batch_id, "Wrong billing", "admin")
    data = db.load_fee_invoice_data(fee_db["session"], fee_db["class"], fee_db["term"])
    assert data["batches"][0]["status"] == "issued"


def test_payments_support_partial_exact_allocations_and_unique_receipts(fee_db):
    _batch_id, invoice_id = _issued_invoice(fee_db)
    with pytest.raises(ValueError, match="Issue the invoice batch"):
        db.record_fee_payment(invoice_id, "1", "2026-10-01", "cash", "", "admin")
    with pytest.raises(PermissionError):
        db.record_fee_payment(invoice_id, "1", "2026-10-01", "cash", "", "teacher")
    db.issue_fee_invoice_batch(_batch_id, "admin")
    first_receipt = db.record_fee_payment(invoice_id, "20.25", "2026-10-01", "cash", "Counter 1", "admin")
    second_receipt = db.record_fee_payment(invoice_id, "100.25", "2026-10-02", "mobile_money", "TX-22", "admin")
    assert first_receipt.startswith("RCP-2026-") and second_receipt != first_receipt
    with pytest.raises(ValueError, match="remaining invoice balance of 0"):
        db.record_fee_payment(invoice_id, "0.01", "2026-10-03", "cash", "", "admin")
    data = db.load_fee_invoice_data(fee_db["session"], fee_db["class"], fee_db["term"])
    invoice = data["invoices"][0]
    assert invoice["paid_amount_text"] == "120.5"
    assert invoice["balance_amount_text"] == "0"
    assert invoice["payment_status"] == "Paid"
    assert len(data["payments"]) == 2
    assert {row["receipt_number"] for row in data["payments"]} == {first_receipt, second_receipt}
    with db.connection() as conn:
        receipt = conn.execute("SELECT * FROM fee_payments WHERE receipt_number=?", (first_receipt,)).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE fee_payments SET amount_text='1' WHERE id=?", (receipt["id"],))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM fee_payments WHERE id=?", (receipt["id"],))


def test_payment_amount_date_and_method_are_validated(fee_db):
    batch_id, invoice_id = _issued_invoice(fee_db)
    db.issue_fee_invoice_batch(batch_id, "admin")
    for amount, day, method, error in [
        ("0", "2026-10-01", "cash", "greater than zero"),
        ("NaN", "2026-10-01", "cash", "finite"),
        ("10", "10/01/2026", "cash", "valid date"),
        ("10", "2026-10-01", "cryptocurrency", "payment method"),
    ]:
        with pytest.raises(ValueError, match=error):
            db.record_fee_payment(invoice_id, amount, day, method, "", "admin")
    assert db.load_fee_invoice_data(fee_db["session"], fee_db["class"], fee_db["term"])["payments"] == []


def test_payment_reversals_are_reasoned_bounded_and_restore_balance(fee_db):
    batch_id, invoice_id = _issued_invoice(fee_db)
    db.issue_fee_invoice_batch(batch_id, "admin")
    db.record_fee_payment(invoice_id, "50", "2026-10-01", "bank_transfer", "REF-1", "admin")
    with db.connection() as conn:
        payment_id = int(conn.execute("SELECT id FROM fee_payments").fetchone()[0])
    with pytest.raises(ValueError, match="between 5 and 300"):
        db.reverse_fee_payment(payment_id, "5", "no", "admin")
    with pytest.raises(ValueError, match="unreversed receipt amount of 50"):
        db.reverse_fee_payment(payment_id, "50.01", "Wrong amount", "admin")
    with pytest.raises(PermissionError):
        db.reverse_fee_payment(payment_id, "10", "Customer refund", "teacher")
    db.reverse_fee_payment(payment_id, "10", "Partial refund", "admin")
    db.record_fee_payment(invoice_id, "80.5", "2026-10-02", "cash", "", "admin")
    data = db.load_fee_invoice_data(fee_db["session"], fee_db["class"], fee_db["term"])
    assert data["invoices"][0]["paid_amount_text"] == "120.5"
    assert data["invoices"][0]["balance_amount_text"] == "0"
    assert sum(float(row["net_amount_text"]) for row in data["payments"]) == 120.5
    with db.connection() as conn:
        reversal = conn.execute("SELECT id FROM fee_payment_reversals").fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM fee_payment_reversals WHERE id=?", (reversal["id"],))


def test_payment_audit_failure_rolls_back_receipt_and_reversal(fee_db, monkeypatch):
    batch_id, invoice_id = _issued_invoice(fee_db)
    db.issue_fee_invoice_batch(batch_id, "admin")
    original = db._audit_in_transaction
    def fail_payment_audit(conn, actor, action, entity, entity_id):
        if entity == "fee_payments":
            raise sqlite3.OperationalError("synthetic payment audit failure")
        return original(conn, actor, action, entity, entity_id)
    monkeypatch.setattr(db, "_audit_in_transaction", fail_payment_audit)
    with pytest.raises(sqlite3.OperationalError, match="payment audit failure"):
        db.record_fee_payment(invoice_id, "10", "2026-10-01", "cash", "", "admin")
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM fee_payments").fetchone()[0] == 0
    monkeypatch.setattr(db, "_audit_in_transaction", original)
    db.record_fee_payment(invoice_id, "10", "2026-10-01", "cash", "", "admin")
    with db.connection() as conn:
        payment_id = int(conn.execute("SELECT id FROM fee_payments").fetchone()[0])
    def fail_reversal_audit(conn, actor, action, entity, entity_id):
        if entity == "fee_payment_reversals":
            raise sqlite3.OperationalError("synthetic reversal audit failure")
        return original(conn, actor, action, entity, entity_id)
    monkeypatch.setattr(db, "_audit_in_transaction", fail_reversal_audit)
    with pytest.raises(sqlite3.OperationalError, match="reversal audit failure"):
        db.reverse_fee_payment(payment_id, "5", "Wrong amount", "admin")
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM fee_payment_reversals").fetchone()[0] == 0


def test_payment_migration_failure_rolls_back_ddl(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    monkeypatch.setattr(db, "SCHEMA_VERSION", 15)
    db.initialize()
    monkeypatch.setattr(db, "SCHEMA_VERSION", 16)
    migration = db._migration_16
    def fail_after_payment_ddl(conn):
        migration(conn)
        raise RuntimeError("synthetic payment migration failure")
    monkeypatch.setattr(db, "_migration_16", fail_after_payment_ddl)
    with pytest.raises(RuntimeError, match="payment migration failure"):
        db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 15
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='fee_payments'").fetchone() is None


def test_fee_reconciliation_matches_receipts_and_reversals_by_event_date(fee_db):
    batch_id, invoice_id = _issued_invoice(fee_db)
    db.issue_fee_invoice_batch(batch_id, "admin")
    with db.connection() as conn:
        event_date = conn.execute("SELECT date('now')").fetchone()[0]
    receipt = db.record_fee_payment(invoice_id, "40.75", event_date, "cash", "Till 2", "admin")
    with db.connection() as conn:
        payment_id = int(conn.execute("SELECT id FROM fee_payments WHERE receipt_number=?", (receipt,)).fetchone()[0])
    db.reverse_fee_payment(payment_id, "5.25", "Duplicate tender", "admin")

    with db.connection() as conn:
        reversal_date = conn.execute(
            "SELECT substr(created_at,1,10) FROM fee_payment_reversals WHERE payment_id=?",
            (payment_id,),
        ).fetchone()[0]
    assert reversal_date == event_date
    report = db.load_fee_reconciliation(event_date, event_date, "admin")
    assert report["receipt_count"] == 1
    assert report["reversal_count"] == 1
    assert report["received_total_text"] == "40.75"
    assert report["reversed_total_text"] == "5.25"
    assert report["net_total_text"] == "35.5"
    assert report["by_method"] == [{"method": "cash", "received_text": "40.75", "reversed_text": "5.25", "net_text": "35.5"}]
    assert [row["type"] for row in report["events"]] == ["Receipt", "Reversal"]
    assert report["events"][1]["reference_or_reason"] == "Duplicate tender"


def test_fee_reconciliation_authorization_and_range_validation(fee_db):
    today = date.today()
    with pytest.raises(PermissionError):
        db.load_fee_reconciliation(today.isoformat(), today.isoformat(), "teacher")
    with pytest.raises(ValueError, match="Start date must be on or before"):
        db.load_fee_reconciliation((today + timedelta(days=1)).isoformat(), today.isoformat(), "admin")
    with pytest.raises(ValueError, match="valid date"):
        db.load_fee_reconciliation("2026-02-30", today.isoformat(), "admin")


def test_receipt_data_includes_snapshotted_invoice_fees_and_reversals(fee_db):
    batch_id, invoice_id = _issued_invoice(fee_db)
    db.issue_fee_invoice_batch(batch_id, "admin")
    today = date.today().isoformat()
    receipt = db.record_fee_payment(invoice_id, "20", today, "bank_transfer", "BANK-1", "admin")
    with db.connection() as conn:
        payment_id = int(conn.execute("SELECT id FROM fee_payments WHERE receipt_number=?", (receipt,)).fetchone()[0])
    db.reverse_fee_payment(payment_id, "2.50", "Correction", "admin")
    data = db.load_fee_receipt_data(payment_id)
    assert data["payment"]["receipt_number"] == receipt
    assert data["payment"]["student_name_snapshot"] == "Synthetic Student"
    assert data["lines"][0]["fee_type_name_snapshot"] == "Tuition"
    assert data["receipt_net_amount_text"] == "17.5"
    assert data["invoice_paid_amount_text"] == "17.5"
    assert data["invoice_balance_text"] == "103"
    assert data["reversals"][0]["reason"] == "Correction"
