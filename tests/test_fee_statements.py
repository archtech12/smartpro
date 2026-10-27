"""Multi-class fee schedules, student fee statements through the active term, and lump-sum payments."""

import pytest

from smartschool import database as db


def _student(school, admission_no, class_index=0):
    student_id = db.add_row("Students", [admission_no, "Synthetic", admission_no, "", "", ""], actor="admin")
    db.create_student_enrollment(student_id, school["classes"][class_index], school["session"], "2026-09-01", "admin")
    return student_id


def _bill_term(school, term_index, class_index=0, school_fee="7000", exam="1000"):
    term = school["terms"][term_index]
    for name, amount in (("School Fees", school_fee), ("Exam", exam)):
        if amount:
            db.save_fee_schedule(school["session"], school["classes"][class_index], term, school["fees"][name], amount, "", "admin")
    batch = db.generate_fee_invoice_batch(school["session"], school["classes"][class_index], term, "admin")
    db.issue_fee_invoice_batch(batch, "admin")


def _invoice_for(school, term_index, admission_no, class_index=0):
    data = db.load_fee_invoice_data(school["session"], school["classes"][class_index], school["terms"][term_index])
    return next(row for row in data["invoices"] if row["admission_no_snapshot"] == admission_no)


# --- one fee for all classes, or for selected classes ---------------------------------------------

def test_fee_can_be_set_for_all_classes_in_one_step(school):
    created = db.save_fee_schedules_for_classes(
        school["session"], school["terms"][0], school["fees"]["School Fees"], None, "7000", "", "admin"
    )
    assert len(created) == 3
    data = db.load_fee_setup_data()
    assert sorted(row["class_id"] for row in data["schedules"]) == sorted(school["classes"])
    assert {row["amount_text"] for row in data["schedules"]} == {"7000"}


def test_fee_can_be_set_for_selected_classes_only(school):
    selected = [school["classes"][0], school["classes"][2], school["classes"][0]]  # duplicate is ignored
    created = db.save_fee_schedules_for_classes(
        school["session"], school["terms"][1], school["fees"]["Exam"], selected, "1,000.50".replace(",", ""), "2027-01-31", "admin"
    )
    assert len(created) == 2
    schedules = db.load_fee_setup_data()["schedules"]
    assert sorted(row["class_id"] for row in schedules) == sorted([school["classes"][0], school["classes"][2]])
    assert {row["due_on"] for row in schedules} == {"2027-01-31"}


def test_multi_class_fee_is_all_or_nothing_and_names_existing_schedules(school):
    db.save_fee_schedule(
        school["session"], school["classes"][1], school["terms"][0], school["fees"]["School Fees"], "6000", "", "admin"
    )
    with pytest.raises(ValueError, match="JSS 2"):
        db.save_fee_schedules_for_classes(
            school["session"], school["terms"][0], school["fees"]["School Fees"], None, "7000", "", "admin"
        )
    schedules = db.load_fee_setup_data()["schedules"]
    assert len(schedules) == 1 and schedules[0]["amount_text"] == "6000"  # nothing was overwritten or added


def test_multi_class_fee_validates_inputs_and_requires_an_administrator(school):
    args = (school["session"], school["terms"][0], school["fees"]["School Fees"])
    with pytest.raises(PermissionError):
        db.save_fee_schedules_for_classes(*args, None, "7000", "", "teacher")
    for bad in ([], [0], [999], "all"):
        with pytest.raises(ValueError):
            db.save_fee_schedules_for_classes(*args, bad, "7000", "", "admin")
    with pytest.raises(ValueError, match="greater than zero"):
        db.save_fee_schedules_for_classes(*args, None, "0", "", "admin")
    with pytest.raises(ValueError, match="Due date"):
        db.save_fee_schedules_for_classes(*args, None, "5", "01/02/2026", "admin")
    other_session = db.save_academic_session("2027", "2027-09-01", "2028-07-31", False, "admin")
    other_term = db.save_term(other_session, "First Term", 1, "2027-09-01", "2027-12-20", "admin")
    with pytest.raises(ValueError, match="term from the selected"):
        db.save_fee_schedules_for_classes(school["session"], other_term, school["fees"]["Exam"], None, "5", "", "admin")
    assert db.load_fee_setup_data()["schedules"] == []


# --- a debt follows the student; a paid term does not add to it --------------------------------------

def test_unpaid_earlier_term_is_carried_into_the_active_term_and_paid_terms_are_not(school):
    owing, settled = _student(school, "A-001"), _student(school, "A-002")
    _bill_term(school, 0)  # First Term: 7000 + 1000 = 8000 each
    db.record_fee_payment(_invoice_for(school, 0, "A-001")["id"], "5000", "2026-09-10", "cash", "", "admin")
    db.record_fee_payment(_invoice_for(school, 0, "A-002")["id"], "8000", "2026-09-10", "cash", "", "admin")

    first_term = db.load_student_fee_statement(owing, "admin")
    assert first_term["current_term"] == "First Term"
    assert first_term["total_outstanding"] == "3000"
    assert [item["carried_over"] for item in first_term["items"]] == [False]

    db.set_current_term(school["terms"][1], "admin")
    _bill_term(school, 1)  # Second Term: another 8000 each
    debtor = db.load_student_fee_statement(owing, "admin")
    assert [(i["term_name"], i["balance_amount_text"], i["carried_over"]) for i in debtor["items"]] == [
        ("First Term", "3000", True), ("Second Term", "8000", False),
    ]
    assert debtor["total_billed"] == "16000" and debtor["total_paid"] == "5000"
    assert debtor["total_outstanding"] == "11000"
    paid_up = db.load_student_fee_statement(settled, "admin")
    assert [(i["term_name"], i["status"]) for i in paid_up["items"]] == [("First Term", "Paid"), ("Second Term", "Unpaid")]
    assert paid_up["total_outstanding"] == "8000"  # the paid first term adds nothing to the second


def test_statement_ignores_later_terms_and_needs_a_current_term(school):
    student = _student(school, "A-001")
    _bill_term(school, 0)
    _bill_term(school, 2)  # Third Term is issued early but is after the active term
    statement = db.load_student_fee_statement(student, "admin")
    assert [item["term_name"] for item in statement["items"]] == ["First Term"]
    assert statement["later_term_invoice_count"] == 1
    assert statement["total_outstanding"] == "8000"
    with db.connection() as conn:
        conn.execute("UPDATE terms SET is_current=0")
    with pytest.raises(ValueError, match="current academic session and term"):
        db.load_student_fee_statement(student, "admin")


def test_unpaid_balance_from_an_earlier_session_still_appears(school):
    student = _student(school, "A-001")
    _bill_term(school, 0)
    next_session = db.save_academic_session("2027", "2027-09-01", "2028-07-31", True, "admin")
    next_term = db.save_term(next_session, "First Term", 1, "2027-09-01", "2027-12-20", "admin")
    db.set_current_term(next_term, "admin")
    statement = db.load_student_fee_statement(student, "admin")
    assert statement["current_session"] == "2027"
    assert [(i["session_name"], i["carried_over"]) for i in statement["items"]] == [("2026", True)]
    assert statement["total_outstanding"] == "8000"


def test_statement_requires_an_administrator_and_an_existing_student(school):
    student = _student(school, "A-001")
    with pytest.raises(PermissionError):
        db.load_student_fee_statement(student, "teacher")
    with pytest.raises(ValueError, match="existing student"):
        db.load_student_fee_statement(9999, "admin")


# --- lump sum: one payment, allocated oldest first, one receipt ------------------------------------

def _owing_two_terms(school):
    student = _student(school, "A-001")
    _bill_term(school, 0)
    db.record_fee_payment(_invoice_for(school, 0, "A-001")["id"], "5000", "2026-09-10", "cash", "", "admin")
    db.set_current_term(school["terms"][1], "admin")
    _bill_term(school, 1)
    return student


def test_lump_sum_pays_oldest_first_under_one_receipt(school):
    student = _owing_two_terms(school)  # First Term owes 3000, Second Term owes 8000
    result = db.record_fee_lump_sum_payment(student, "4000", "2027-01-20", "bank_transfer", "TRX-9", "admin")
    assert result["lump_sum_number"].startswith("LS-2027-")
    assert [(a["term_name"], a["amount_text"]) for a in result["allocations"]] == [("First Term", "3000"), ("Second Term", "1000")]
    statement = db.load_student_fee_statement(student, "admin")
    assert [(i["term_name"], i["balance_amount_text"], i["status"]) for i in statement["items"]] == [
        ("First Term", "0", "Paid"), ("Second Term", "7000", "Part paid"),
    ]
    assert statement["total_outstanding"] == "7000"
    receipt = db.load_fee_lump_sum_receipt_data(result["lump_sum_number"], "admin")
    assert receipt["total_amount_text"] == "4000" and receipt["net_amount_text"] == "4000"
    assert receipt["student_name"].endswith("A-001") and receipt["admission_no"] == "A-001"
    assert (receipt["method"], receipt["reference"], receipt["received_on"]) == ("bank_transfer", "TRX-9", "2027-01-20")
    assert [(a["fees"], a["amount_text"], a["invoice_balance_text"]) for a in receipt["allocations"]] == [
        ("Exam, School Fees", "3000", "0"), ("Exam, School Fees", "1000", "7000"),
    ]
    assert len({a["receipt_number"] for a in receipt["allocations"]}) == 2  # each allocation keeps its own immutable receipt


def test_lump_sum_can_clear_everything_and_then_nothing_is_outstanding(school):
    student = _owing_two_terms(school)
    db.record_fee_lump_sum_payment(student, "11000", "2027-01-20", "cash", "", "admin")
    assert db.load_student_fee_statement(student, "admin")["total_outstanding"] == "0"
    with pytest.raises(ValueError, match="no outstanding balance"):
        db.record_fee_lump_sum_payment(student, "1", "2027-01-21", "cash", "", "admin")


def test_lump_sum_rejects_overpayment_without_recording_anything(school):
    student = _owing_two_terms(school)
    with pytest.raises(ValueError, match="exceeds the outstanding balance of 11000"):
        db.record_fee_lump_sum_payment(student, "11000.01", "2027-01-20", "cash", "", "admin")
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM fee_payments WHERE lump_sum_number IS NOT NULL").fetchone()[0] == 0
    assert db.load_student_fee_statement(student, "admin")["total_outstanding"] == "11000"


def test_lump_sum_never_pays_a_term_after_the_active_term(school):
    student = _student(school, "A-001")
    _bill_term(school, 0)
    _bill_term(school, 2)
    with pytest.raises(ValueError, match="exceeds the outstanding balance of 8000"):
        db.record_fee_lump_sum_payment(student, "9000", "2026-09-10", "cash", "", "admin")


def test_lump_sum_validates_inputs_and_requires_an_administrator(school):
    student = _owing_two_terms(school)
    with pytest.raises(PermissionError):
        db.record_fee_lump_sum_payment(student, "10", "2027-01-20", "cash", "", "teacher")
    for kwargs in ({"amount": "0"}, {"amount": "-5"}, {"amount": "abc"}, {"received_on": "20/01/2027"},
                   {"method": "barter"}, {"reference": "x" * 101}):
        values = {"amount": "10", "received_on": "2027-01-20", "method": "cash", "reference": ""} | kwargs
        with pytest.raises(ValueError):
            db.record_fee_lump_sum_payment(student, values["amount"], values["received_on"], values["method"], values["reference"], "admin")
    with pytest.raises(ValueError, match="existing student"):
        db.record_fee_lump_sum_payment(9999, "10", "2027-01-20", "cash", "", "admin")


def test_lump_sum_allocation_can_be_reversed_with_a_reason_and_reopens_the_debt(school):
    student = _owing_two_terms(school)
    result = db.record_fee_lump_sum_payment(student, "3000", "2027-01-20", "cash", "", "admin")
    allocation = result["allocations"][0]
    db.reverse_fee_payment(allocation["payment_id"], "1000", "Counted twice at the till", "admin")
    statement = db.load_student_fee_statement(student, "admin")
    assert statement["items"][0]["balance_amount_text"] == "1000"
    receipt = db.load_fee_lump_sum_receipt_data(result["lump_sum_number"], "admin")
    assert receipt["total_amount_text"] == "3000" and receipt["net_amount_text"] == "2000"
    assert receipt["allocations"][0]["reversed_amount_text"] == "1000"


def test_lump_sum_payments_are_audited_and_receipts_are_admin_only(school):
    student = _owing_two_terms(school)
    result = db.record_fee_lump_sum_payment(student, "4000", "2027-01-20", "cash", "", "admin")
    with db.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action='RECEIVE' AND entity='fee_payments'"
        ).fetchone()[0]
    assert count >= 3  # one earlier single payment plus two lump-sum allocations
    with pytest.raises(PermissionError):
        db.load_fee_lump_sum_receipt_data(result["lump_sum_number"], "teacher")
    with pytest.raises(ValueError, match="not found"):
        db.load_fee_lump_sum_receipt_data("LS-0000-NONE", "admin")


def test_invoice_loader_exposes_student_and_lump_sum_links(school):
    student = _owing_two_terms(school)
    result = db.record_fee_lump_sum_payment(student, "100", "2027-01-20", "cash", "", "admin")
    data = db.load_fee_invoice_data(school["session"], school["classes"][0], school["terms"][0])
    assert {row["student_id"] for row in data["invoices"]} == {student}
    assert [p["lump_sum_number"] for p in data["payments"] if p["lump_sum_number"]] == [result["lump_sum_number"]]


def test_whole_session_can_be_paid_ahead_across_issued_later_terms(school):
    student = _student(school, "ADM-AHEAD")
    for term_index in (0, 1, 2):          # the school issues the whole session's invoices up front
        _bill_term(school, term_index, school_fee="30000", exam="")
    db.set_current_term(school["terms"][0], "admin")
    statement = db.load_student_fee_statement(student, "admin")
    assert statement["total_outstanding"] == "30000" and statement["later_outstanding"] == "60000"
    assert [i["term_name"] for i in statement["later_items"]] == ["Second Term", "Third Term"]

    with pytest.raises(ValueError, match="exceeds the outstanding balance of 30000"):
        db.record_fee_lump_sum_payment(student, "90000", "2026-09-10", "cash", "", "admin")   # default scope: active term only
    result = db.record_fee_lump_sum_payment(student, "90000", "2026-09-10", "cash", "", "admin", include_later_terms=True)
    assert [a["term_name"] for a in result["allocations"]] == ["First Term", "Second Term", "Third Term"]
    assert [a["amount_text"] for a in result["allocations"]] == ["30000", "30000", "30000"]
    after = db.load_student_fee_statement(student, "admin")
    assert after["total_outstanding"] == "0" and after["later_outstanding"] == "0"
    with pytest.raises(ValueError, match="no outstanding balance"):
        db.record_fee_lump_sum_payment(student, "1", "2026-09-11", "cash", "", "admin", include_later_terms=True)


def test_paying_ahead_partially_fills_oldest_first_and_never_overpays(school):
    student = _student(school, "ADM-PART")
    for term_index in (0, 1):
        _bill_term(school, term_index, school_fee="30000", exam="")
    result = db.record_fee_lump_sum_payment(student, "40000", "2026-09-10", "cash", "", "admin", include_later_terms=True)
    assert [a["amount_text"] for a in result["allocations"]] == ["30000", "10000"]
    statement = db.load_student_fee_statement(student, "admin")
    assert statement["total_outstanding"] == "0" and statement["later_outstanding"] == "20000"
    with pytest.raises(ValueError, match="exceeds the outstanding balance of 20000"):
        db.record_fee_lump_sum_payment(student, "20001", "2026-09-11", "cash", "", "admin", include_later_terms=True)
