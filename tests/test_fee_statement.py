import pytest

from smartschool.fee_statement import compare_statement_rows, load_statement_csv


def _statement(day, amount, reference="", description=""):
    from decimal import Decimal
    return {"row_number": 2, "date": day, "amount": Decimal(amount), "amount_text": amount,
            "reference": reference, "description": description}


def _event(day, kind, amount, reference, receipt):
    return {"date": day, "type": kind, "amount_text": amount,
            "reference_or_reason": reference, "receipt_number": receipt,
            "student_name": "Synthetic Student", "invoice_number": "INV-1"}


def test_statement_csv_import_requires_expected_columns_and_valid_rows(tmp_path):
    path = tmp_path / "bank.csv"
    path.write_text("date,amount,reference,description\n2026-10-24,25.50,TX-1,School fees\n", encoding="utf-8")
    rows = load_statement_csv(path)
    assert rows[0]["date"] == "2026-10-24"
    assert str(rows[0]["amount"]) == "25.50"

    path.write_text("date,amount,reference\n2026-10-24,25,TX-1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="columns must be exactly"):
        load_statement_csv(path)


@pytest.mark.parametrize("row,message", [
    ("2026-02-30,5,R1,", "valid YYYY-MM-DD"),
    ("2026-10-24,NaN,R1,", "finite non-zero decimal"),
    ("2026-10-24,0,R1,", "finite non-zero decimal"),
])
def test_statement_csv_rejects_bad_dates_and_amounts(tmp_path, row, message):
    path = tmp_path / "bad.csv"
    path.write_text("date,amount,reference,description\n" + row + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_statement_csv(path)


def test_statement_comparison_matches_exact_receipt_and_reversal_signs():
    events = [
        _event("2026-10-24", "Receipt", "25.5", "BANK-1", "RCP-2026-1"),
        _event("2026-10-24", "Reversal", "5", "Duplicate tender", "RCP-2026-2"),
    ]
    result = compare_statement_rows([
        _statement("2026-10-24", "25.50", "BANK-1"),
        _statement("2026-10-24", "-5", "RCP-2026-2"),
    ], events)
    assert result["matched_count"] == 2
    assert result["ambiguous_count"] == 0
    assert result["statement_only_count"] == 0
    assert result["ledger_only_count"] == 0


def test_statement_comparison_preserves_ambiguous_and_unmatched_rows():
    events = [
        _event("2026-10-24", "Receipt", "10", "", "RCP-1"),
        _event("2026-10-24", "Receipt", "10", "", "RCP-2"),
        _event("2026-10-24", "Receipt", "12", "TX-3", "RCP-3"),
    ]
    result = compare_statement_rows([
        _statement("2026-10-24", "10"),
        _statement("2026-10-24", "12", "WRONG-REF"),
    ], events)
    assert result["matched_count"] == 0
    assert result["ambiguous_count"] == 1
    assert result["statement_only_count"] == 1
    assert result["ledger_only_count"] == 1
    assert [row["status"] for row in result["rows"]] == [
        "Ambiguous", "Statement only", "Ambiguous", "Ambiguous", "Ledger only"
    ]


def test_duplicate_statement_lines_do_not_consume_one_ledger_event_twice():
    event = _event("2026-10-24", "Receipt", "10", "TX-1", "RCP-1")
    result = compare_statement_rows([
        _statement("2026-10-24", "10", "TX-1"),
        _statement("2026-10-24", "10", "TX-1"),
    ], [event])
    assert result["matched_count"] == 0
    assert result["ambiguous_count"] == 2
    assert result["ledger_only_count"] == 0
