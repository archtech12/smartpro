"""Read-only matching of generic cash/bank statement CSV rows to the local fee ledger."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

MAX_STATEMENT_BYTES = 5 * 1024 * 1024
MAX_STATEMENT_ROWS = 20_000
STATEMENT_COLUMNS = ("date", "amount", "reference", "description")


def load_statement_csv(path: str | Path) -> list[dict]:
    statement_path = Path(path)
    if statement_path.stat().st_size > MAX_STATEMENT_BYTES:
        raise ValueError("Statement CSV is larger than the 5 MiB import limit.")
    try:
        with statement_path.open("r", newline="", encoding="utf-8-sig") as source:
            reader = csv.DictReader(source)
            headers = [str(value or "").strip().casefold() for value in (reader.fieldnames or [])]
            if headers != list(STATEMENT_COLUMNS):
                raise ValueError("CSV columns must be exactly: date,amount,reference,description.")
            rows = []
            for line_number, raw in enumerate(reader, start=2):
                if line_number > MAX_STATEMENT_ROWS + 1:
                    raise ValueError(f"Statement CSV exceeds the {MAX_STATEMENT_ROWS:,}-row import limit.")
                if not any(str(value or "").strip() for value in raw.values()):
                    continue
                try:
                    day_text = str(raw["date"] or "").strip()
                    parsed_day = date.fromisoformat(day_text)
                    if parsed_day.isoformat() != day_text:
                        raise ValueError
                except ValueError as exc:
                    raise ValueError(f"CSV row {line_number}: date must use valid YYYY-MM-DD format.") from exc
                amount_text = str(raw["amount"] or "").strip()
                try:
                    amount = Decimal(amount_text)
                except InvalidOperation as exc:
                    raise ValueError(f"CSV row {line_number}: amount must be a finite non-zero decimal.") from exc
                if not amount.is_finite() or amount == 0 or len(amount_text) > 32:
                    raise ValueError(f"CSV row {line_number}: amount must be a finite non-zero decimal.")
                reference = str(raw["reference"] or "").strip()
                description = str(raw["description"] or "").strip()
                if len(reference) > 100 or len(description) > 240:
                    raise ValueError(f"CSV row {line_number}: reference (100) or description (240) is too long.")
                rows.append({
                    "row_number": line_number,
                    "date": day_text,
                    "amount": amount,
                    "amount_text": amount_text,
                    "reference": reference,
                    "description": description,
                })
    except UnicodeDecodeError as exc:
        raise ValueError("Statement CSV must use UTF-8 encoding.") from exc
    if not rows:
        raise ValueError("Statement CSV contains no transaction rows.")
    return rows


def compare_statement_rows(statement_rows: list[dict], ledger_events: list[dict]) -> dict:
    """Match exact date, signed amount and optional reference, preserving ambiguous cases."""
    candidates: list[list[int]] = []
    parsed_events = []
    for event in ledger_events:
        amount = Decimal(str(event["amount_text"]))
        signed = -amount if event["type"] == "Reversal" else amount
        parsed_events.append((event["date"], signed, event["reference_or_reason"], event["receipt_number"]))
    for statement in statement_rows:
        possible = []
        for index, (event_date, signed, local_reference, receipt_number) in enumerate(parsed_events):
            if statement["date"] != event_date or statement["amount"] != signed:
                continue
            reference = statement["reference"].casefold()
            if reference and reference not in {str(local_reference).casefold(), str(receipt_number).casefold()}:
                continue
            possible.append(index)
        candidates.append(possible)

    candidate_use_count: dict[int, int] = {}
    for possible in candidates:
        for index in possible:
            candidate_use_count[index] = candidate_use_count.get(index, 0) + 1

    matched_events: set[int] = set()
    ambiguous_events: set[int] = set()
    rows = []
    matched_count = ambiguous_count = unmatched_count = 0
    for statement, possible in zip(statement_rows, candidates):
        if len(possible) == 1 and candidate_use_count[possible[0]] == 1:
            event_index = possible[0]
            matched_events.add(event_index)
            event = ledger_events[event_index]
            status = "Matched"
            candidate = f"{event['type']} {event['receipt_number']} · {event['student_name']}"
            matched_count += 1
        elif possible:
            ambiguous_events.update(possible)
            status = "Ambiguous"
            candidate = "; ".join(
                f"{ledger_events[index]['type']} {ledger_events[index]['receipt_number']}"
                for index in possible
            )
            ambiguous_count += 1
        else:
            status, candidate = "Statement only", "No exact date/amount/reference match"
            unmatched_count += 1
        rows.append({
            "status": status,
            "date": statement["date"],
            "amount_text": statement["amount_text"],
            "reference": statement["reference"],
            "description": statement["description"],
            "candidate": candidate,
        })

    ledger_only_count = 0
    for index, event in enumerate(ledger_events):
        if index in matched_events:
            continue
        if index in ambiguous_events:
            status, candidate = "Ambiguous", "Multiple statement lines may refer to this ledger event"
        else:
            status, candidate = "Ledger only", "No exact statement match"
            ledger_only_count += 1
        signed_text = f"-{event['amount_text']}" if event["type"] == "Reversal" else event["amount_text"]
        rows.append({
            "status": status,
            "date": event["date"],
            "amount_text": signed_text,
            "reference": event["receipt_number"],
            "description": f"{event['type']}: {event['student_name']} · {event['invoice_number']}",
            "candidate": event["receipt_number"],
        })
    return {
        "matched_count": matched_count,
        "ambiguous_count": ambiguous_count,
        "statement_only_count": unmatched_count,
        "ledger_only_count": ledger_only_count,
        "rows": rows,
    }
