from smartschool import fee_documents


def _statement(items, outstanding="3000", later=0):
    return {
        "student": {"first_name": "Amina", "last_name": "O<b>kafor", "admission_no": "S-1"},
        "current_session": "2026", "current_term": "Second Term", "later_term_invoice_count": later,
        "total_billed": "16000", "total_paid": "13000", "total_outstanding": outstanding,
        "school_profile": {"school_name": "Synthetic & Sons", "address": "1 Road"}, "school_logo": None, "items": items,
    }


def _item(term, order, status, balance, carried):
    return {
        "session_name": "2026", "term_name": term, "term_order": order, "class_name": "JSS 1", "section": "A",
        "fees": "School Fees, Exam", "total_amount_text": "8000", "paid_amount_text": "5000",
        "balance_amount_text": balance, "status": status, "carried_over": carried, "invoice_number": "INV-1",
    }


def test_statement_lists_terms_marks_carried_debt_and_escapes_text():
    html = fee_documents.statement_html(_statement(
        [_item("First Term", 1, "Part paid", "3000", True), _item("Second Term", 2, "Paid", "0", False)], later=1
    ))
    assert "Statement of account" in html and "Synthetic &amp; Sons" in html
    assert "O&lt;b&gt;kafor" in html and "<b>kafor" not in html
    assert "1. First Term <i>(carried forward)</i>" in html
    assert "2. Second Term</td>" in html  # a paid earlier-or-current term is not flagged
    assert "Outstanding 3000" in html and "1 issued invoice(s) for later terms" in html


def test_statement_without_invoices_says_so():
    assert "No issued invoices up to the active term" in fee_documents.statement_html(_statement([], "0"))


def test_lump_sum_receipt_shows_allocations_and_totals():
    html = fee_documents.lump_sum_receipt_html({
        "lump_sum_number": "LS-2027-AB12CD34", "student_name": "Amina Okafor", "admission_no": "S-1",
        "received_on": "2027-01-20", "method": "bank_transfer", "reference": "", "received_by": "admin",
        "total_amount_text": "4000", "net_amount_text": "3500", "school_profile": {}, "school_logo": b"x",
        "allocations": [
            {**_item("First Term", 1, "Paid", "0", True), "receipt_number": "RCP-1", "amount_text": "3000",
             "reversed_amount_text": "500", "invoice_balance_text": "500"},
            {**_item("Second Term", 2, "Part paid", "7000", False), "receipt_number": "RCP-2", "amount_text": "1000",
             "reversed_amount_text": "0", "invoice_balance_text": "7000"},
        ],
    })
    assert "LS-2027-AB12CD34" in html and "Bank Transfer" in html and "<img src='school-logo'" in html
    assert html.index("1. First Term") < html.index("2. Second Term")
    assert "Total received: 4000" in html and "Remaining after any reversals: 3500" in html
    assert "RCP-1" in html and "RCP-2" in html and "—" in html
