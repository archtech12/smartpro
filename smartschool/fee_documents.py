"""Printable fee documents as plain HTML strings (no Qt, so they can be tested directly)."""

from __future__ import annotations

from html import escape

_STYLE = """<style>
  body { font-family: sans-serif; color: #183247; } .school { text-align: center; color: #102a43; }
  h1 { margin: 8px 0 4px; } .meta { margin: 12px 0; } table { border-collapse: collapse; width: 100%; margin: 10px 0; }
  th { background: #e8f0f6; text-align: left; } th, td { border: 1px solid #9fb3c8; padding: 6px; }
  .num { text-align: right; } .owing { color: #b91c1c; } .total { font-size: 14pt; font-weight: bold; margin-top: 14px; }
  .note { color: #486581; font-size: 9pt; margin-top: 18px; }
</style>"""


def _school_header(profile: dict, has_logo: bool) -> str:
    name = (profile.get("school_name") or "").strip() or "School"
    contact = " · ".join(value for value in (profile.get("address", ""), profile.get("phone", ""), profile.get("email", "")) if value)
    logo = "<img src='school-logo' width='90' height='70'>" if has_logo else ""
    return f"<div class='school'>{logo}<h1>{escape(name)}</h1><div>{escape(contact)}</div></div>"


def _class_label(row: dict) -> str:
    return f"{row['class_name']} {row['section']}".strip()


def statement_html(data: dict) -> str:
    """Statement of account for one student through the active term."""
    student = data["student"]
    rows = "".join(
        "<tr>"
        f"<td>{escape(item['session_name'])} · {item['term_order']}. {escape(item['term_name'])}"
        f"{' <i>(carried forward)</i>' if item['carried_over'] and item['status'] != 'Paid' else ''}</td>"
        f"<td>{escape(_class_label(item))}</td><td>{escape(item['fees'])}</td>"
        f"<td class='num'>{escape(item['total_amount_text'])}</td><td class='num'>{escape(item['paid_amount_text'])}</td>"
        f"<td class='num{' owing' if item['status'] != 'Paid' else ''}'>{escape(item['balance_amount_text'])}</td>"
        f"<td>{escape(item['status'])}</td></tr>"
        for item in data["items"]
    ) or "<tr><td colspan='7'>No issued invoices up to the active term.</td></tr>"
    later = (
        f"<p class='note'>{data['later_term_invoice_count']} issued invoice(s) for later terms are not included.</p>"
        if data.get("later_term_invoice_count") else ""
    )
    return f"""<!doctype html><html><head><meta charset='utf-8'>{_STYLE}</head><body>
      {_school_header(data.get('school_profile', {}), bool(data.get('school_logo')))}
      <h1>Statement of account</h1>
      <div class='meta'><b>Student:</b> {escape(student['last_name'])}, {escape(student['first_name'])} · {escape(student['admission_no'])}<br>
      <b>Active session and term:</b> {escape(data['current_session'])} · {escape(data['current_term'])}</div>
      <table><tr><th>Session / term</th><th>Class</th><th>Fees</th><th>Billed</th><th>Paid</th><th>Balance</th><th>Status</th></tr>{rows}</table>
      <div class='total'>Billed {escape(data['total_billed'])} · Paid {escape(data['total_paid'])} ·
        <span class='owing'>Outstanding {escape(data['total_outstanding'])}</span></div>{later}
      <div class='note'>Outstanding includes unpaid balances from earlier terms. Amounts are shown without a currency symbol.</div>
    </body></html>"""


def lump_sum_receipt_html(data: dict) -> str:
    """One receipt for a lump-sum payment and how it was allocated across invoices."""
    rows = "".join(
        "<tr>"
        f"<td>{escape(item['session_name'])} · {item['term_order']}. {escape(item['term_name'])}</td>"
        f"<td>{escape(_class_label(item))}</td><td>{escape(item['fees'])}</td>"
        f"<td>{escape(item['invoice_number'])}</td><td>{escape(item['receipt_number'])}</td>"
        f"<td class='num'>{escape(item['amount_text'])}</td>"
        f"<td class='num'>{escape(item['reversed_amount_text'])}</td>"
        f"<td class='num'>{escape(item['invoice_balance_text'])}</td></tr>"
        for item in data["allocations"]
    )
    return f"""<!doctype html><html><head><meta charset='utf-8'>{_STYLE}</head><body>
      {_school_header(data.get('school_profile', {}), bool(data.get('school_logo')))}
      <h1>Lump-sum payment receipt</h1>
      <div class='meta'><b>Receipt:</b> {escape(data['lump_sum_number'])}<br>
      <b>Student:</b> {escape(data['student_name'])} · {escape(data['admission_no'])}<br>
      <b>Received on:</b> {escape(data['received_on'])} · <b>Method:</b> {escape(data['method'].replace('_', ' ').title())}<br>
      <b>Reference:</b> {escape(data['reference'] or '—')} · <b>Received by:</b> {escape(data['received_by'])}</div>
      <table><tr><th>Session / term</th><th>Class</th><th>Fees</th><th>Invoice</th><th>Allocation receipt</th>
        <th>Allocated</th><th>Reversed</th><th>Invoice balance now</th></tr>{rows}</table>
      <div class='total'>Total received: {escape(data['total_amount_text'])}</div>
      <p>Remaining after any reversals: {escape(data['net_amount_text'])}</p>
      <div class='note'>The payment was applied to the oldest unpaid invoices first. Keep this receipt for your records.
        Amounts are shown without a currency symbol.</div>
    </body></html>"""
