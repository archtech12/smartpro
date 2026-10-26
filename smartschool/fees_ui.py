"""Administrator fee-type and fee-schedule setup."""

from __future__ import annotations

import csv
from decimal import Decimal
from html import escape
from pathlib import Path

from PyQt6.QtCore import QDate, QMarginsF, QThreadPool, QUrl, Qt
from PyQt6.QtGui import QImage, QPageLayout, QPageSize, QPdfWriter, QTextDocument
from PyQt6.QtWidgets import (
    QComboBox,
    QCheckBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QInputDialog,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .table_helpers import configure_table_headers
from .ui_helpers import wrapped_label
from . import database as db
from .fee_statement import compare_statement_rows, load_statement_csv
from . import fee_documents
from .workers import BackgroundTask


def _display_amount(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


class RecordPaymentDialog(QDialog):
    """Collect one payment against the selected issued invoice in a single form."""

    def __init__(self, invoice: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Record student payment")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        student = QLabel(
            f"{invoice['student_name_snapshot']} · {invoice['admission_no_snapshot']}\n"
            f"Invoice {invoice['invoice_number']} · Balance due: {invoice['balance_amount_text']}"
        )
        student.setWordWrap(True)
        student.setObjectName("info")
        layout.addWidget(student)
        form = QFormLayout()
        self.amount = QLineEdit()
        self.amount.setPlaceholderText("Enter amount in the school's local currency")
        self.method = QComboBox()
        for code, label in (
            ("cash", "Cash"), ("bank_transfer", "Bank transfer"),
            ("mobile_money", "Mobile money"), ("cheque", "Cheque"), ("other", "Other"),
        ):
            self.method.addItem(label, code)
        self.reference = QLineEdit()
        self.reference.setMaxLength(100)
        self.reference.setPlaceholderText("Optional transaction reference")
        self.received_on = QDateEdit(QDate.currentDate())
        self.received_on.setCalendarPopup(True)
        self.received_on.setDisplayFormat("yyyy-MM-dd")
        form.addRow("Payment amount", self.amount)
        form.addRow("Payment method", self.method)
        form.addRow("Payment date", self.received_on)
        form.addRow("Reference", self.reference)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("Record payment")
        self.save_button.setObjectName("primary")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_if_valid(self):
        if not self.amount.text().strip():
            QMessageBox.information(self, "Enter payment amount", "Enter the amount received before saving.")
            return
        self.accept()

    def values(self):
        return (
            self.amount.text().strip(), self.method.currentData(), self.reference.text().strip(),
            self.received_on.date().toString("yyyy-MM-dd"),
        )


def _write_html_pdf(html: str, path: str, logo_data=None) -> str:
    """Write an HTML document to an A4 PDF, embedding the optional school logo."""
    writer = QPdfWriter(path)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageMargins(QMarginsF(15, 15, 15, 15), QPageLayout.Unit.Millimeter)
    document = QTextDocument()
    if logo_data:
        image = QImage.fromData(logo_data)
        if image.isNull():
            raise ValueError("The saved school logo is not a readable image.")
        image = image.scaled(180, 140, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        document.addResource(QTextDocument.ResourceType.ImageResource, QUrl("school-logo"), image)
    document.setHtml(html)
    document.print(writer)
    return path


class StudentStatementDialog(QDialog):
    """A student's statement through the active term, with lump-sum payment and PDF printing.

    Unpaid balances from earlier terms stay on the statement until paid, and a lump-sum
    payment is applied to the oldest unpaid invoices first under one receipt.
    """

    def __init__(self, student_id: int, username: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Student fee statement")
        self.setMinimumSize(820, 560)
        self.student_id = student_id
        self.username = username
        self.changed = False
        self._data = None
        self._busy = False
        self._request = 0
        self._workers = {}
        self._pool = QThreadPool.globalInstance()
        layout = QVBoxLayout(self)
        self.heading = QLabel("Loading statement…")
        self.heading.setObjectName("heading")
        layout.addWidget(self.heading)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Session / Term", "Class", "Fees", "Billed", "Paid", "Balance", "Status"])
        configure_table_headers(self.table)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)
        form = QFormLayout()
        self.amount = QLineEdit()
        self.amount.setPlaceholderText("Amount received; applied to the oldest unpaid fees first")
        self.method = QComboBox()
        for code, label in (
            ("cash", "Cash"), ("bank_transfer", "Bank transfer"),
            ("mobile_money", "Mobile money"), ("cheque", "Cheque"), ("other", "Other"),
        ):
            self.method.addItem(label, code)
        self.received_on = QDateEdit(QDate.currentDate())
        self.received_on.setCalendarPopup(True)
        self.received_on.setDisplayFormat("yyyy-MM-dd")
        self.reference = QLineEdit()
        self.reference.setMaxLength(100)
        self.reference.setPlaceholderText("Optional transaction reference")
        form.addRow("Lump-sum amount", self.amount)
        form.addRow("Payment method", self.method)
        form.addRow("Payment date", self.received_on)
        form.addRow("Reference", self.reference)
        layout.addLayout(form)
        actions = QHBoxLayout()
        self.pay_button = QPushButton("Record lump-sum payment")
        self.pay_button.setObjectName("primary")
        self.pay_button.clicked.connect(self.pay)
        self.print_button = QPushButton("Print statement (PDF)")
        self.print_button.clicked.connect(self.print_statement)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.accept)
        actions.addWidget(self.pay_button)
        actions.addWidget(self.print_button)
        actions.addStretch()
        actions.addWidget(self.close_button)
        layout.addLayout(actions)
        self.status = QLabel("")
        layout.addWidget(self.status)
        self._set_actions()
        self.refresh()

    def _set_actions(self):
        has_data = self._data is not None
        owing = has_data and Decimal(self._data["total_outstanding"]) > 0
        self.pay_button.setEnabled(not self._busy and owing)
        self.amount.setEnabled(not self._busy and owing)
        self.print_button.setEnabled(not self._busy and has_data)

    def _run(self, operation, callback):
        if self._busy:
            return
        self._busy = True
        self._set_actions()
        self._request += 1
        request = self._request
        worker = BackgroundTask(request, operation)
        worker.signals.finished.connect(callback)
        self._workers[request] = worker
        self._pool.start(worker)

    def refresh(self):
        self.status.setText("Loading statement…")
        self._run(lambda: db.load_student_fee_statement(self.student_id, self.username), self._loaded)

    def _loaded(self, request, data, error):
        self._workers.pop(request, None)
        self._busy = False
        if error is not None:
            self.heading.setText("Could not load the statement")
            self.status.setText(str(error))
            self._set_actions()
            return
        self._data = data
        student = data["student"]
        self.heading.setText(f"{student['last_name']}, {student['first_name']} · {student['admission_no']}")
        self.summary.setText(
            f"Active term: {data['current_session']} · {data['current_term']}.  Billed {data['total_billed']}  ·  "
            f"Paid {data['total_paid']}  ·  Outstanding {data['total_outstanding']}.  "
            "Unpaid balances from earlier terms stay here until they are paid."
        )
        self.table.setRowCount(0)
        for row, item in enumerate(data["items"]):
            self.table.insertRow(row)
            term = f"{item['session_name']} · {item['term_order']}. {item['term_name']}"
            if item["carried_over"] and item["status"] != "Paid":
                term += " (carried forward)"
            values = (
                term, f"{item['class_name']} {item['section']}".strip(), item["fees"],
                item["total_amount_text"], item["paid_amount_text"], item["balance_amount_text"], item["status"],
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.status.setText(
            "No issued invoices up to the active term." if not data["items"] else
            (f"{data['later_term_invoice_count']} issued invoice(s) for later terms are not included."
             if data["later_term_invoice_count"] else "")
        )
        self._set_actions()

    def pay(self):
        amount = self.amount.text().strip()
        if not amount:
            QMessageBox.information(self, "Enter payment amount", "Enter the amount received before saving.")
            return
        received_on = self.received_on.date().toString("yyyy-MM-dd")
        method, reference = self.method.currentData(), self.reference.text().strip()
        self.status.setText("Recording payment…")
        self._run(
            lambda: db.record_fee_lump_sum_payment(
                self.student_id, amount, received_on, method, reference, self.username
            ),
            self._paid,
        )

    def _paid(self, request, result, error):
        self._workers.pop(request, None)
        self._busy = False
        if error is not None:
            self.status.setText("Payment was not recorded")
            self._set_actions()
            QMessageBox.warning(self, "Could not record payment", str(error))
            return
        self.changed = True
        self.amount.clear()
        self.reference.clear()
        number = result["lump_sum_number"]
        answer = QMessageBox.question(
            self, "Payment recorded",
            f"Lump-sum receipt {number} was recorded for {result['total_amount_text']}.\n\nSave the receipt as a PDF now?",
        )
        if answer == QMessageBox.StandardButton.Yes and self._export_receipt(number):
            return
        self.refresh()

    def _ask_pdf_path(self, title: str, suggested: str):
        path, _selected_filter = QFileDialog.getSaveFileName(self, title, suggested, "PDF files (*.pdf)")
        if not path:
            return None
        return path if path.casefold().endswith(".pdf") else path + ".pdf"

    def _export_receipt(self, number: str) -> bool:
        path = self._ask_pdf_path("Save lump-sum receipt", f"{number}.pdf")
        if path is None:
            return False

        def build():
            data = db.load_fee_lump_sum_receipt_data(number, self.username)
            return _write_html_pdf(fee_documents.lump_sum_receipt_html(data), path, data.get("school_logo"))

        self.status.setText("Preparing receipt PDF…")
        self._run(build, self._pdf_saved)
        return True

    def print_statement(self):
        if self._data is None:
            return
        student = self._data["student"]
        path = self._ask_pdf_path("Save statement of account", f"statement-{student['admission_no']}.pdf")
        if path is None:
            return
        data = self._data
        self.status.setText("Preparing statement PDF…")
        self._run(
            lambda: _write_html_pdf(fee_documents.statement_html(data), path, data.get("school_logo")),
            self._pdf_saved,
        )

    def _pdf_saved(self, request, path, error):
        self._workers.pop(request, None)
        self._busy = False
        if error is not None:
            self.status.setText("The PDF was not saved")
            self._set_actions()
            QMessageBox.warning(self, "Could not save PDF", str(error))
            return
        self.status.setText(f"Saved {path}")
        self.refresh()


class FeeInvoicePage(QWidget):
    """Manage draft and issued invoices, payments, receipts, and reversals."""

    def __init__(self, username: str, role: str):
        super().__init__()
        self.username = username
        self.is_admin = role == "admin"
        self._data = {"sessions": [], "classes": [], "terms": [], "batches": [], "invoices": [], "payments": []}
        self._workers = {}
        self._operations = {}
        self._pool = QThreadPool.globalInstance()
        self._request = 0
        self._busy = False
        self._selected_batch = None
        self._selected_invoice = None
        self._selected_payment = None
        self._did_initial_scope_refresh = False
        layout = QVBoxLayout(self)
        title = QLabel("Fees and invoices")
        title.setObjectName("heading")
        layout.addWidget(title)
        introduction = wrapped_label(
            "Choose a session and class, create draft invoices, review them, then issue. Record payments from Student Balances."
        )
        layout.addWidget(introduction)
        selectors = QHBoxLayout()
        self.session = QComboBox()
        self.class_section = QComboBox()
        self.term = QComboBox()
        for label, combo in (("Session", self.session), ("Class", self.class_section), ("Term", self.term)):
            selectors.addWidget(QLabel(label))
            selectors.addWidget(combo, 1)
        layout.addLayout(selectors)
        actions = QToolBar("Fee actions")
        actions.setMovable(False)
        actions.setFloatable(False)
        actions.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.preview_button = QPushButton("Preview invoices")
        self.preview_button.setToolTip("Check which students and charges will be included before creating invoices.")
        self.preview_button.clicked.connect(self.preview)
        self.generate_button = QPushButton("Create draft invoices")
        self.generate_button.setObjectName("primary")
        self.generate_button.setToolTip("Create reviewable drafts for the selected session, class, and term.")
        self.generate_button.clicked.connect(self.generate)
        self.cancel_button = QPushButton("Cancel selected draft")
        self.cancel_button.setToolTip("Select a draft under Invoice Batches before cancelling it.")
        self.cancel_button.clicked.connect(self.cancel_batch)
        self.issue_button = QPushButton("Issue selected batch")
        self.issue_button.setToolTip("Select and review a draft under Invoice Batches before issuing it.")
        self.issue_button.clicked.connect(self.issue_batch)
        self.payment_button = QPushButton("Record payment")
        self.payment_button.setToolTip(
            "Select an issued invoice with a balance in Student Balances before recording a payment."
        )
        self.payment_button.clicked.connect(self.record_payment)
        self.statement_button = QPushButton("Statement / lump sum")
        self.statement_button.setToolTip(
            "Select a student's invoice to see everything owed through the active term, pay it in one lump sum, or print the statement."
        )
        self.statement_button.clicked.connect(self.open_statement)
        for button in (self.preview_button, self.generate_button):
            actions.addWidget(button)
        layout.addWidget(actions)
        batch_tab = QWidget()
        batch_layout = QVBoxLayout(batch_tab)
        batch_heading = QHBoxLayout()
        batch_heading.addWidget(QLabel("Select a draft below to issue or cancel it."), 1)
        batch_heading.addWidget(self.issue_button)
        batch_heading.addWidget(self.cancel_button)
        batch_layout.addLayout(batch_heading)
        self.batch_table = QTableWidget(0, 7)
        self.batch_table.setHorizontalHeaderLabels(["Created On", "Academic Session", "Class", "Academic Term", "Batch Status", "Invoice Count", "Invoices Requiring Review"])
        configure_table_headers(self.batch_table)
        self.batch_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.batch_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.batch_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.batch_table.itemSelectionChanged.connect(self._batch_selected)
        self.batch_table.setAlternatingRowColors(True)
        batch_layout.addWidget(self.batch_table, 1)
        view_picker = QHBoxLayout()
        view_picker.addWidget(QLabel("Fee view"))
        self.fee_view = QComboBox()
        self.fee_view.addItems(["Student Balances", "Payment History", "Class Balances", "Invoice Batches"])
        self.fee_view.setAccessibleName("Choose a fee workflow view")
        self.fee_view.setToolTip("Choose student balances, receipts, class totals, or invoice batches.")
        view_picker.addWidget(self.fee_view, 1)
        view_picker.addStretch(2)
        layout.addLayout(view_picker)
        self.fee_views = QStackedWidget()
        self.fee_view.currentIndexChanged.connect(self.fee_views.setCurrentIndex)
        student_tab = QWidget()
        student_layout = QVBoxLayout(student_tab)
        invoice_heading = QHBoxLayout()
        invoice_heading.addWidget(QLabel("Student fee balances"))
        invoice_heading.addStretch()
        self.invoice_search = QLineEdit()
        self.invoice_search.setClearButtonEnabled(True)
        self.invoice_search.setPlaceholderText("Filter by student, admission number, or invoice")
        self.invoice_search.setAccessibleName("Filter fee balances and receipts by student or invoice")
        self.invoice_search.setMaximumWidth(360)
        self.invoice_search.textChanged.connect(self._render_scope)
        invoice_heading.addWidget(self.invoice_search)
        self.invoice_search_status = QLabel("")
        invoice_heading.addWidget(self.invoice_search_status)
        invoice_heading.addWidget(self.payment_button)
        invoice_heading.addWidget(self.statement_button)
        student_layout.addLayout(invoice_heading)
        self.invoice_table = QTableWidget(0, 9)
        self.invoice_table.setHorizontalHeaderLabels(["Invoice Number", "Admission Number", "Student Name", "Class", "Academic Term", "Invoice Amount", "Amount Paid", "Balance Due", "Invoice Status"])
        configure_table_headers(self.invoice_table)
        self.invoice_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.invoice_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.invoice_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.invoice_table.itemSelectionChanged.connect(self._invoice_selected)
        student_layout.addWidget(self.invoice_table, 1)
        self.fee_views.addWidget(student_tab)

        payments_tab = QWidget()
        payments_layout = QVBoxLayout(payments_tab)
        payments_layout.addWidget(wrapped_label(
            "Receipts for the selected filters. For a date-range ledger or reversals, open Transaction History."
        ))
        self.payment_table = QTableWidget(0, 6)
        self.payment_table.setHorizontalHeaderLabels(["Receipt Number", "Invoice Number", "Received Date", "Amount Received", "Payment Method", "Net Amount After Reversals"])
        configure_table_headers(self.payment_table)
        self.payment_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.payment_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.payment_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.payment_table.itemSelectionChanged.connect(self._payment_selected)
        payments_layout.addWidget(self.payment_table, 1)
        self.reverse_button = QPushButton("Reverse selected payment")
        self.reverse_button.clicked.connect(self.reverse_payment)
        payment_actions = QHBoxLayout()
        payment_actions.addWidget(self.reverse_button)
        self.export_receipt_button = QPushButton("Export selected receipt PDF")
        self.export_receipt_button.clicked.connect(self.export_receipt)
        payment_actions.addWidget(self.export_receipt_button)
        payment_actions.addStretch()
        payments_layout.addLayout(payment_actions)
        self.fee_views.addWidget(payments_tab)

        class_tab = QWidget()
        class_layout = QVBoxLayout(class_tab)
        class_layout.addWidget(wrapped_label(
            "Issued invoice totals by class. Draft invoices are counted separately."
        ))
        self.class_balance_table = QTableWidget(0, 7)
        self.class_balance_table.setHorizontalHeaderLabels([
            "Class and Section", "Students with Invoices", "Issued Invoices", "Total Billed",
            "Amount Paid", "Balance Due", "Draft Invoices",
        ])
        configure_table_headers(self.class_balance_table)
        self.class_balance_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.class_balance_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.class_balance_table.setAlternatingRowColors(True)
        class_layout.addWidget(self.class_balance_table, 1)
        self.fee_views.addWidget(class_tab)
        self.fee_views.addWidget(batch_tab)
        layout.addWidget(self.fee_views, 1)
        self.status = QLabel("Loading invoice data…")
        layout.addWidget(self.status)
        if not self.is_admin:
            self.generate_button.setEnabled(False)
            self.cancel_button.setEnabled(False)
            self.issue_button.setEnabled(False)
            self.payment_button.setEnabled(False)
            self.statement_button.setEnabled(False)
            self.reverse_button.setEnabled(False)
            self.export_receipt_button.setEnabled(False)
        self.session.currentIndexChanged.connect(self._session_changed)
        self.class_section.currentIndexChanged.connect(self.refresh)
        self.term.currentIndexChanged.connect(self.refresh)
        self.refresh()

    @staticmethod
    def _fill(combo, prompt, rows, label, selected=None, all_option=False):
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(prompt, None)
        for row in rows:
            combo.addItem(label(row), int(row["id"]))
        index = combo.findData(selected) if selected is not None else (0 if all_option else -1)
        if index < 0 and combo.count() > 1:
            index = 1
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def refresh(self, *_args):
        self._request += 1
        request = self._request
        session_id, class_id, term_id = self.session.currentData(), self.class_section.currentData(), self.term.currentData()
        self._busy = True
        self._actions()
        worker = BackgroundTask(request, lambda: db.load_fee_invoice_data(session_id, class_id, term_id))
        worker.signals.finished.connect(self._loaded)
        self._workers[request] = worker
        self._pool.start(worker)

    def _loaded(self, request, data, error):
        self._workers.pop(request, None)
        if request != self._request:
            return
        self._busy = False
        if error:
            self.status.setText("Could not load fee invoices")
            QMessageBox.warning(self, "Could not load fee invoices", str(error))
            self._actions()
            return
        old_session, old_class, old_term = self.session.currentData(), self.class_section.currentData(), self.term.currentData()
        self._data = data
        self._fill(self.session, "Select session", data["sessions"], lambda x: x["name"], old_session)
        self._fill(self.class_section, "All classes", data["classes"], lambda x: f"{x['class_name']} {x['section']}".strip(), old_class, all_option=True)
        self._fill(self.term, "All terms", [x for x in data["terms"] if x["academic_session_id"] == self.session.currentData()], lambda x: f"{x['term_order']}. {x['term_name']}", old_term, all_option=True)
        if not self._did_initial_scope_refresh and self.session.currentData() is not None:
            self._did_initial_scope_refresh = True
            self.refresh()
            return
        self._render_batches()
        self._render_invoices()
        self._render_payments()
        has_balance_due = any(
            invoice["is_issued"] and Decimal(invoice["balance_amount_text"]) > 0
            for invoice in data["invoices"]
        )
        self.status.setText(
            "Balances loaded. To record a payment, select an issued invoice with a balance under Student Balances, then click Record payment."
            if has_balance_due else
            "Balances loaded. No issued invoices with an outstanding balance match these filters."
        )
        self._actions()

    def _session_changed(self, *_args):
        self._fill(self.term, "All terms", [x for x in self._data["terms"] if x["academic_session_id"] == self.session.currentData()], lambda x: f"{x['term_order']}. {x['term_name']}", all_option=True)
        self.refresh()

    def _render_batches(self):
        self.batch_table.setRowCount(len(self._data["batches"]))
        for ri, row in enumerate(self._data["batches"]):
            vals = (row["created_at"], row["session_name"], f"{row['class_name']} {row['section']}".strip(), row["term_name"], row["status"].title(), row["invoice_count"], row["review_count"])
            for ci, value in enumerate(vals):
                item = QTableWidgetItem(str(value))
                if ci == 0:
                    item.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
                self.batch_table.setItem(ri, ci, item)
        self.batch_table.resizeColumnsToContents()

    def _render_invoices(self, *_args):
        batch_ids = {int(row["id"]) for row in self._data["batches"]}
        batches_by_id = {int(row["id"]): row for row in self._data["batches"]}
        visible = [row for row in self._data["invoices"] if row["batch_id"] in batch_ids]
        query = self.invoice_search.text().strip().casefold()
        if query:
            visible = [
                row for row in visible
                if query in " ".join((
                    str(row["invoice_number"]),
                    str(row["admission_no_snapshot"]),
                    str(row["student_name_snapshot"]),
                )).casefold()
            ]
        self._selected_invoice = None
        self.invoice_table.setRowCount(len(visible))
        for ri, row in enumerate(visible):
            batch = batches_by_id[int(row["batch_id"])]
            if batch["status"] == "cancelled":
                state = "Cancelled"
            elif row["is_issued"]:
                state = row["payment_status"]
            else:
                state = "Draft; review" if row["review_required"] else "Draft"
            class_label = f"{batch['class_name']} {batch['section']}".strip()
            vals = (
                row["invoice_number"], row["admission_no_snapshot"], row["student_name_snapshot"],
                class_label, batch["term_name"], row["total_amount_text"], row["paid_amount_text"],
                row["balance_amount_text"], state,
            )
            for ci, value in enumerate(vals):
                item = QTableWidgetItem(str(value))
                if ci == 0:
                    item.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
                self.invoice_table.setItem(ri, ci, item)
        self.invoice_table.resizeColumnsToContents()
        issued = [row for row in visible if row["is_issued"]]
        expected = sum((Decimal(row["total_amount_text"]) for row in issued), Decimal("0"))
        paid = sum((Decimal(row["paid_amount_text"]) for row in issued), Decimal("0"))
        due = sum((Decimal(row["balance_amount_text"]) for row in issued), Decimal("0"))
        invoice_label = "invoice" if len(visible) == 1 else "invoices"
        self.invoice_search_status.setText(
            f"{len(visible)} {invoice_label} · {len(issued)} issued · Billed {expected} · Paid {paid} · Due {due}"
        )
        self._render_class_balances(visible, batches_by_id)
        self._actions()

    def _render_class_balances(self, invoices, batches_by_id):
        grouped = {}
        for invoice in invoices:
            batch = batches_by_id.get(int(invoice["batch_id"]))
            if batch is None or batch["status"] == "cancelled":
                continue
            class_label = " ".join((str(batch["class_name"]), str(batch["section"]))).strip()
            entry = grouped.setdefault(class_label, {
                "students": set(), "issued": 0, "billed": Decimal("0"),
                "paid": Decimal("0"), "due": Decimal("0"), "drafts": 0,
            })
            if invoice["is_issued"]:
                entry["students"].add(str(invoice["admission_no_snapshot"]))
                entry["issued"] += 1
                entry["billed"] += Decimal(invoice["total_amount_text"])
                entry["paid"] += Decimal(invoice["paid_amount_text"])
                entry["due"] += Decimal(invoice["balance_amount_text"])
            elif batch["status"] == "draft":
                entry["drafts"] += 1
        self.class_balance_table.setRowCount(len(grouped))
        for row_index, (class_label, data) in enumerate(sorted(grouped.items(), key=lambda item: item[0].casefold())):
            values = (
                class_label, len(data["students"]), data["issued"],
                _display_amount(data["billed"]), _display_amount(data["paid"]),
                _display_amount(data["due"]), data["drafts"],
            )
            for column, value in enumerate(values):
                self.class_balance_table.setItem(row_index, column, QTableWidgetItem(str(value)))
        self.class_balance_table.resizeColumnsToContents()

    def _render_payments(self):
        query = self.invoice_search.text().strip().casefold()
        self._selected_payment = None
        visible = [
            row for row in self._data["payments"]
            if not query or query in " ".join((
                str(row["receipt_number"]), str(row["invoice_number"]),
                str(row["admission_no_snapshot"]), str(row["student_name_snapshot"]),
            )).casefold()
        ]
        self.payment_table.setRowCount(len(visible))
        for ri, row in enumerate(visible):
            values = (row["receipt_number"], row["invoice_number"], row["received_on"], row["amount_text"], row["method"].replace("_", " ").title(), row["net_amount_text"])
            for ci, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if ci == 0:
                    item.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
                self.payment_table.setItem(ri, ci, item)
        self.payment_table.resizeColumnsToContents()
        self._actions()

    def _render_scope(self, *_args):
        self._render_invoices()
        self._render_payments()

    def _batch_selected(self):
        row = self.batch_table.currentRow()
        item = self.batch_table.item(row, 0) if row >= 0 else None
        self._selected_batch = item.data(Qt.ItemDataRole.UserRole) if item else None
        self._actions()

    def _invoice_selected(self):
        row = self.invoice_table.currentRow()
        item = self.invoice_table.item(row, 0) if row >= 0 else None
        self._selected_invoice = item.data(Qt.ItemDataRole.UserRole) if item else None
        self._actions()

    def _payment_selected(self):
        row = self.payment_table.currentRow()
        item = self.payment_table.item(row, 0) if row >= 0 else None
        self._selected_payment = item.data(Qt.ItemDataRole.UserRole) if item else None
        self._actions()

    def _scope(self):
        values = (self.session.currentData(), self.class_section.currentData(), self.term.currentData())
        if any(value is None for value in values):
            raise ValueError("Select an academic session, class, and term.")
        return tuple(int(value) for value in values)

    def _actions(self):
        enabled = self.is_admin and not self._busy
        self.preview_button.setEnabled(not self._busy and self.session.currentData() is not None and self.class_section.currentData() is not None and self.term.currentData() is not None)
        self.generate_button.setEnabled(enabled and self.preview_button.isEnabled())
        batch = next((x for x in self._data["batches"] if x["id"] == self._selected_batch), None)
        self.cancel_button.setEnabled(enabled and bool(batch and batch["status"] == "draft"))
        self.issue_button.setEnabled(enabled and bool(batch and batch["status"] == "draft"))
        invoice = next((x for x in self._data["invoices"] if x["id"] == self._selected_invoice), None)
        self.payment_button.setEnabled(enabled and bool(invoice and invoice["is_issued"] and invoice["balance_amount_text"] != "0"))
        self.statement_button.setEnabled(enabled and invoice is not None)
        payment = next((x for x in self._data["payments"] if x["id"] == self._selected_payment), None)
        self.reverse_button.setEnabled(enabled and bool(payment and payment["net_amount_text"] != "0"))
        self.export_receipt_button.setEnabled(not self._busy and payment is not None)

    def preview(self):
        try:
            scope = self._scope()
            self._start_operation("preview", lambda: db.preview_fee_invoices(*scope), "Preparing invoice preview…")
        except Exception as exc:
            QMessageBox.warning(self, "Cannot preview invoices", str(exc))

    def generate(self):
        try:
            scope = self._scope()
            self._pending_scope = scope
            self._start_operation("generate_preview", lambda: db.preview_fee_invoices(*scope), "Checking roster and fee schedules…")
        except Exception as exc:
            QMessageBox.warning(self, "Could not generate invoices", str(exc))

    def _start_operation(self, operation, callback, message):
        self._busy = True
        self._actions()
        self.status.setText(message)
        self._request += 1
        request = self._request
        worker = BackgroundTask(request, callback)
        worker.signals.finished.connect(self._operation_finished)
        self._workers[request] = worker
        self._operations[request] = operation
        self._pool.start(worker)

    def _operation_finished(self, request, result, error):
        self._workers.pop(request, None)
        operation = self._operations.pop(request, None)
        if request != self._request:
            return
        self._busy = False
        if error:
            self.status.setText("Invoice operation did not complete")
            QMessageBox.warning(self, "Fee invoice", str(error))
            self._actions()
            return
        if operation in {"preview", "generate_preview"}:
            summary = f"{result['student_count']} student(s), {len(result['schedules'])} active fee type(s), {result['review_count']} enrollment(s) requiring review.\n\nDraft total per student: {result['students'][0]['total_amount_text']}\n\nNo fees are prorated. Review before creating drafts."
            if operation == "preview":
                QMessageBox.information(self, "Invoice preview", summary)
                self.status.setText("Preview ready. No data was changed.")
                self._actions()
                return
            answer = QMessageBox.question(self, "Generate draft invoices", f"{summary}\n\nCreate the draft batch? No payments or balances will change.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                scope = self._pending_scope
                self._start_operation("generate", lambda: db.generate_fee_invoice_batch(*scope, self.username), "Generating draft invoices…")
            else:
                self.status.setText("Draft batch was not created.")
                self._actions()
            return
        if operation == "generate":
            self.status.setText(f"Draft batch {result} created. Reloading…")
            self.refresh()
        elif operation == "cancel":
            self.status.setText("Draft batch cancelled. Reloading…")
            self.refresh()
        elif operation == "issue":
            self.status.setText("Invoice batch issued. Reloading…")
            self.refresh()
        elif operation == "payment":
            QMessageBox.information(self, "Payment recorded", f"Receipt number: {result}")
            self.status.setText("Payment recorded. Reloading balances…")
            self.refresh()
        elif operation == "reverse":
            self.status.setText("Payment reversal recorded. Reloading balances…")
            self.refresh()
        elif operation == "receipt_export":
            self.status.setText(f"Receipt PDF saved: {result}")
            QMessageBox.information(self, "Receipt exported", f"Receipt PDF saved to:\n{result}")

    def issue_batch(self):
        batch = next((x for x in self._data["batches"] if x["id"] == self._selected_batch), None)
        if not batch or batch["status"] != "draft":
            return
        answer = QMessageBox.question(
            self, "Issue invoices", "Issue all invoices in this batch? Issued invoices can accept payments and cannot be cancelled.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._start_operation("issue", lambda: db.issue_fee_invoice_batch(int(batch["id"]), self.username), "Issuing invoices…")

    def record_payment(self):
        invoice = next((x for x in self._data["invoices"] if x["id"] == self._selected_invoice), None)
        if not invoice or not invoice["is_issued"]:
            return
        dialog = RecordPaymentDialog(invoice, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        amount, method, reference, received_on = dialog.values()
        self._start_operation(
            "payment",
            lambda: db.record_fee_payment(int(invoice["id"]), amount, received_on, method, reference, self.username),
            "Recording payment…",
        )

    def open_statement(self):
        invoice = next((x for x in self._data["invoices"] if x["id"] == self._selected_invoice), None)
        if invoice is None or not self.is_admin:
            return
        try:
            dialog = StudentStatementDialog(int(invoice["student_id"]), self.username, self)
            dialog.exec()
        except Exception as exc:  # a dialog problem must not close the application
            QMessageBox.warning(self, "Could not open the statement", str(exc))
            return
        if dialog.changed:
            self.refresh()

    def reverse_payment(self):
        payment = next((x for x in self._data["payments"] if x["id"] == self._selected_payment), None)
        if not payment or payment["net_amount_text"] == "0":
            return
        amount, accepted = QInputDialog.getText(
            self, "Reverse payment", f"Amount (unreversed receipt amount: {payment['net_amount_text']}):"
        )
        if not accepted:
            return
        reason, accepted = QInputDialog.getText(self, "Reversal reason", "Reason (5–300 characters):")
        if not accepted:
            return
        self._start_operation(
            "reverse",
            lambda: db.reverse_fee_payment(int(payment["id"]), amount, reason, self.username),
            "Recording payment reversal…",
        )

    def export_receipt(self):
        payment = next((x for x in self._data["payments"] if x["id"] == self._selected_payment), None)
        if payment is None:
            return
        suggested = f"{payment['receipt_number']}.pdf"
        path, _selected_filter = QFileDialog.getSaveFileName(
            self, "Export payment receipt", suggested, "PDF files (*.pdf)"
        )
        if not path:
            return
        if not path.casefold().endswith(".pdf"):
            path += ".pdf"
        payment_id = int(payment["id"])
        self._start_operation(
            "receipt_export",
            lambda: self._write_receipt_pdf(db.load_fee_receipt_data(payment_id), path),
            "Preparing receipt PDF…",
        )

    @staticmethod
    def _write_receipt_pdf(data: dict, path: str) -> str:
        payment = data["payment"]
        profile = data["school_profile"]
        name = profile.get("school_name", "").strip() or "School"
        contact = " · ".join(value for value in (profile.get("address", ""), profile.get("phone", ""), profile.get("email", "")) if value)
        fee_rows = "".join(
            f"<tr><td>{escape(row['fee_type_name_snapshot'])}</td><td>{escape(row['amount_text_snapshot'])}</td></tr>"
            for row in data["lines"]
        )
        reversal_rows = "".join(
            f"<tr><td>{escape(row['created_at'])}</td><td>{escape(row['amount_text'])}</td><td>{escape(row['reason'])}</td></tr>"
            for row in data["reversals"]
        )
        reverse_section = (
            "<h2>Reversals recorded against this receipt</h2><table><tr><th>Date</th><th>Amount</th><th>Reason</th></tr>" + reversal_rows + "</table>"
            if reversal_rows else ""
        )
        logo = "<img src='school-logo' width='90' height='70'>" if data.get("school_logo") else ""
        html = f"""<!doctype html><html><head><meta charset='utf-8'><style>
          body {{ font-family:sans-serif;color:#183247; }} .school {{ text-align:center;color:#102a43; }}
          h1 {{ margin:8px 0 4px; }} .meta {{ margin:12px 0; }} table {{ border-collapse:collapse;width:100%;margin:10px 0; }}
          th {{ background:#e8f0f6;text-align:left; }} th,td {{ border:1px solid #9fb3c8;padding:7px; }}
          .total {{ font-size:16pt;font-weight:bold;margin-top:18px; }} .note {{ color:#486581;font-size:9pt;margin-top:20px; }}
        </style></head><body><div class='school'>{logo}<h1>{escape(name)}</h1><div>{escape(contact)}</div></div>
        <h1>Payment receipt</h1><div class='meta'><b>Receipt:</b> {escape(payment['receipt_number'])}<br>
        <b>Received on:</b> {escape(payment['received_on'])}<br><b>Invoice:</b> {escape(payment['invoice_number'])}<br>
        <b>Student:</b> {escape(payment['student_name_snapshot'])} · {escape(payment['admission_no_snapshot'])}<br>
        <b>Class:</b> {escape(payment['class_name'])} {escape(payment['section'])}<br>
        <b>Session/term:</b> {escape(payment['session_name'])} · {payment['term_order']}. {escape(payment['term_name'])}<br>
        <b>Method:</b> {escape(payment['method'].replace('_',' ').title())}<br>
        <b>Reference:</b> {escape(payment['reference'] or '—')}</div>
        <table><tr><th>Scheduled fee</th><th>Amount</th></tr>{fee_rows}</table>
        <div class='total'>Received on this receipt: {escape(payment['amount_text'])}</div>
        <p>Remaining amount on this receipt after reversals: {escape(data['receipt_net_amount_text'])}<br>
        Total paid on invoice: {escape(data['invoice_paid_amount_text'])}<br>
        Invoice balance when exported: {escape(data['invoice_balance_text'])}</p>
        {reverse_section}<div class='note'>Keep this receipt for your records. Amounts are shown without a currency symbol.</div></body></html>"""
        writer = QPdfWriter(path)
        writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        writer.setPageMargins(QMarginsF(15, 15, 15, 15), QPageLayout.Unit.Millimeter)
        document = QTextDocument()
        logo_data = data.get("school_logo")
        if logo_data:
            image = QImage.fromData(logo_data)
            if image.isNull():
                raise ValueError("The saved school logo is not a readable image.")
            image = image.scaled(180, 140, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            document.addResource(QTextDocument.ResourceType.ImageResource, QUrl("school-logo"), image)
        document.setHtml(html)
        document.print(writer)
        return path

    def cancel_batch(self):
        batch = next((x for x in self._data["batches"] if x["id"] == self._selected_batch), None)
        if not batch or batch["status"] != "draft":
            return
        reason, accepted = QInputDialog.getText(self, "Cancel draft batch", "Cancellation reason (5–300 characters):")
        if not accepted:
            return
        self._start_operation("cancel", lambda: db.cancel_fee_invoice_batch(int(batch["id"]), reason, self.username), "Cancelling draft batch…")


class FeeReconciliationPage(QWidget):
    """Date-scoped receipt and reversal reconciliation with exportable ledger events."""

    def __init__(self, username: str, role: str):
        super().__init__()
        self.username = username
        self.is_admin = role == "admin"
        self._pool = QThreadPool.globalInstance()
        self._request = 0
        self._worker = None
        self._data = None
        self._comparison = None
        layout = QVBoxLayout(self)
        title = QLabel("Transaction history and reconciliation")
        title.setObjectName("heading")
        layout.addWidget(title)
        introduction = wrapped_label(
            "Compare recorded receipts and reversals by date and method, or match a statement CSV using "
            "date, amount, reference, description. Credits are positive; reversals are negative. "
            "This is a read-only comparison, not bank settlement confirmation."
        )
        layout.addWidget(introduction)
        filters = QHBoxLayout()
        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)
        today = QDate.currentDate()
        self.start_date.setDate(today.addDays(1 - today.day()))
        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)
        self.end_date.setDate(today)
        filters.addWidget(QLabel("From"))
        filters.addWidget(self.start_date)
        filters.addWidget(QLabel("Through"))
        filters.addWidget(self.end_date)
        filters.addStretch()
        layout.addLayout(filters)
        actions = QHBoxLayout()
        self.run_button = QPushButton("Run reconciliation")
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(self.refresh)
        self.export_button = QPushButton("Export ledger CSV")
        self.export_button.clicked.connect(self.export_csv)
        self.template_button = QPushButton("Save statement template")
        self.template_button.clicked.connect(self.save_statement_template)
        self.compare_button = QPushButton("Compare statement CSV")
        self.compare_button.clicked.connect(self.compare_statement)
        self.export_comparison_button = QPushButton("Export comparison CSV")
        self.export_comparison_button.clicked.connect(self.export_comparison)
        self.export_comparison_button.setEnabled(False)
        actions.addWidget(self.run_button)
        actions.addWidget(self.export_button)
        actions.addWidget(self.template_button)
        actions.addWidget(self.compare_button)
        actions.addWidget(self.export_comparison_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.summary = QLabel("Choose a date range and run the report.")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        layout.addWidget(QLabel("Totals by payment method"))
        self.method_table = QTableWidget(0, 4)
        self.method_table.setHorizontalHeaderLabels(["Payment Method", "Amount Received", "Amount Reversed", "Net Amount Received"])
        configure_table_headers(self.method_table)
        self.method_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.method_table.setMaximumHeight(180)
        layout.addWidget(self.method_table)
        layout.addWidget(QLabel("Transaction history (first 500 shown; CSV export includes all events)"))
        self.event_table = QTableWidget(0, 9)
        self.event_table.setHorizontalHeaderLabels(["Transaction Date", "Transaction Type", "Receipt Number", "Invoice Number", "Admission Number", "Student Name", "Payment Method", "Transaction Amount", "Reference or Reason"])
        configure_table_headers(self.event_table)
        self.event_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.event_table, 1)
        self.status = QLabel("Ready")
        layout.addWidget(self.status)
        if not self.is_admin:
            self.run_button.setEnabled(False)
            self.export_button.setEnabled(False)
            self.template_button.setEnabled(False)
            self.compare_button.setEnabled(False)
            self.export_comparison_button.setEnabled(False)
            self.status.setText("Fee reconciliation requires an active administrator account.")
        else:
            self.refresh()

    def refresh(self, *_args):
        if not self.is_admin:
            return
        self._request += 1
        request = self._request
        start = self.start_date.date().toString("yyyy-MM-dd")
        end = self.end_date.date().toString("yyyy-MM-dd")
        self.run_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.compare_button.setEnabled(False)
        self.export_comparison_button.setEnabled(False)
        self.status.setText("Calculating receipt and reversal totals…")
        worker = BackgroundTask(request, lambda: db.load_fee_reconciliation(start, end, self.username))
        worker.signals.finished.connect(self._loaded)
        self._worker = worker
        self._pool.start(worker)

    def _loaded(self, request, data, error):
        if request != self._request:
            return
        self._worker = None
        self.run_button.setEnabled(True)
        self.export_button.setEnabled(error is None and bool(data and data["events"]))
        if error:
            self._data = None
            self.compare_button.setEnabled(self.is_admin)
            self.export_comparison_button.setEnabled(False)
            self.summary.setText("No reconciliation report was produced.")
            self.status.setText(str(error))
            return
        self._data = data
        self._comparison = None
        self.export_comparison_button.setEnabled(False)
        self.compare_button.setEnabled(self.is_admin)
        self.event_table.setColumnCount(9)
        self.event_table.setHorizontalHeaderLabels(["Transaction Date", "Transaction Type", "Receipt Number", "Invoice Number", "Admission Number", "Student Name", "Payment Method", "Transaction Amount", "Reference or Reason"])
        configure_table_headers(self.event_table)
        self.summary.setText(
            f"{data['start_on']} through {data['end_on']}  ·  "
            f"{data['receipt_count']} receipt(s) totaling {data['received_total_text']}  ·  "
            f"{data['reversal_count']} reversal(s) totaling {data['reversed_total_text']}  ·  "
            f"Net ledger activity: {data['net_total_text']}"
        )
        self.method_table.setRowCount(len(data["by_method"]))
        for ri, row in enumerate(data["by_method"]):
            for ci, value in enumerate((row["method"].replace("_", " ").title(), row["received_text"], row["reversed_text"], row["net_text"])):
                self.method_table.setItem(ri, ci, QTableWidgetItem(str(value)))
        self.method_table.resizeColumnsToContents()
        events = data["events"][:500]
        self.event_table.setRowCount(len(events))
        for ri, row in enumerate(events):
            values = (row["date"], row["type"], row["receipt_number"], row["invoice_number"], row["admission_no"], row["student_name"], row["method"].replace("_", " ").title(), row["amount_text"], row["reference_or_reason"])
            for ci, value in enumerate(values):
                self.event_table.setItem(ri, ci, QTableWidgetItem(str(value)))
        self.event_table.resizeColumnsToContents()
        self.status.setText(
            f"Loaded {len(data['events'])} ledger event(s). This report does not verify external cash or bank settlement."
        )

    @staticmethod
    def _write_csv(data: dict, path: str) -> str:
        with Path(path).open("w", newline="", encoding="utf-8-sig") as output:
            writer = csv.writer(output)
            writer.writerow(["Fee reconciliation", data["start_on"], data["end_on"]])
            writer.writerow(["Receipt count", data["receipt_count"], "Receipt total", data["received_total_text"]])
            writer.writerow(["Reversal count", data["reversal_count"], "Reversal total", data["reversed_total_text"]])
            writer.writerow(["Net ledger activity", data["net_total_text"]])
            writer.writerow([])
            writer.writerow(["Date", "Type", "Receipt", "Invoice", "Admission No.", "Student", "Method", "Amount", "Reference / reason"])
            for row in data["events"]:
                values = [row[key] for key in ("date", "type", "receipt_number", "invoice_number", "admission_no", "student_name", "method", "amount_text", "reference_or_reason")]
                writer.writerow([("'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value) for value in values])
        return path

    def export_csv(self):
        if not self._data or not self._data["events"]:
            return
        path, _selected_filter = QFileDialog.getSaveFileName(
            self, "Export fee reconciliation", f"fee-reconciliation-{self._data['start_on']}-to-{self._data['end_on']}.csv", "CSV files (*.csv)"
        )
        if not path:
            return
        if not path.casefold().endswith(".csv"):
            path += ".csv"
        self.export_button.setEnabled(False)
        self.status.setText("Writing reconciliation CSV…")
        self._request += 1
        request = self._request
        worker = BackgroundTask(request, lambda: self._write_csv(self._data, path))
        worker.signals.finished.connect(self._exported)
        self._worker = worker
        self._pool.start(worker)

    def save_statement_template(self):
        path, _selected_filter = QFileDialog.getSaveFileName(
            self, "Save statement CSV template", "settlement-statement-template.csv", "CSV files (*.csv)"
        )
        if not path:
            return
        if not path.casefold().endswith(".csv"):
            path += ".csv"
        try:
            with Path(path).open("w", newline="", encoding="utf-8-sig") as output:
                csv.writer(output).writerow(["date", "amount", "reference", "description"])
        except OSError as exc:
            QMessageBox.warning(self, "Could not save statement template", str(exc))
            return
        self.status.setText(f"Statement template saved: {path}")

    def compare_statement(self):
        path, _selected_filter = QFileDialog.getOpenFileName(
            self, "Select cash/bank statement CSV", "", "CSV files (*.csv)"
        )
        if not path:
            return
        self.run_button.setEnabled(False)
        self.compare_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.export_comparison_button.setEnabled(False)
        self.status.setText("Validating statement and matching ledger events…")
        self._request += 1
        request = self._request

        def compare_file():
            rows = load_statement_csv(path)
            start = min(row["date"] for row in rows)
            end = max(row["date"] for row in rows)
            ledger = db.load_fee_reconciliation(start, end, self.username)
            return compare_statement_rows(rows, ledger["events"]), start, end

        worker = BackgroundTask(request, compare_file)
        worker.signals.finished.connect(self._statement_compared)
        self._worker = worker
        self._pool.start(worker)

    def _statement_compared(self, request, result, error):
        if request != self._request:
            return
        self._worker = None
        self.run_button.setEnabled(True)
        self.compare_button.setEnabled(self.is_admin)
        self.export_button.setEnabled(self._data is not None and bool(self._data["events"]))
        if error:
            self._comparison = None
            self.export_comparison_button.setEnabled(False)
            self.status.setText("Statement comparison failed; the ledger was not changed.")
            QMessageBox.warning(self, "Could not compare statement", str(error))
            return
        comparison, start, end = result
        self._comparison = comparison
        self.export_comparison_button.setEnabled(True)
        self.event_table.setColumnCount(6)
        self.event_table.setHorizontalHeaderLabels(["Match Status", "Statement Date", "Statement Amount", "Statement Reference", "Statement Description", "Possible Matching Ledger Entry"])
        configure_table_headers(self.event_table)
        self.summary.setText(
            f"Statement {start} through {end}  ·  {comparison['matched_count']} exact match(es)  ·  "
            f"{comparison['ambiguous_count']} ambiguous  ·  {comparison['statement_only_count']} statement-only  ·  "
            f"{comparison['ledger_only_count']} ledger-only"
        )
        rows = comparison["rows"][:500]
        self.event_table.setRowCount(len(rows))
        for ri, row in enumerate(rows):
            values = (row["status"], row["date"], row["amount_text"], row["reference"], row["description"], row["candidate"])
            for ci, value in enumerate(values):
                self.event_table.setItem(ri, ci, QTableWidgetItem(str(value)))
        self.event_table.resizeColumnsToContents()
        self.status.setText("Read-only comparison complete. Review ambiguous and unmatched lines; no ledger data was changed.")

    @staticmethod
    def _write_comparison_csv(comparison: dict, path: str) -> str:
        with Path(path).open("w", newline="", encoding="utf-8-sig") as output:
            writer = csv.writer(output)
            writer.writerow(["Status", "Date", "Amount", "Reference", "Description", "Ledger candidate"])
            for row in comparison["rows"]:
                values = [row[key] for key in ("status", "date", "amount_text", "reference", "description", "candidate")]
                writer.writerow([("'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value) for value in values])
        return path

    def export_comparison(self):
        if self._comparison is None:
            return
        path, _selected_filter = QFileDialog.getSaveFileName(
            self, "Export statement comparison", "settlement-comparison.csv", "CSV files (*.csv)"
        )
        if not path:
            return
        if not path.casefold().endswith(".csv"):
            path += ".csv"
        comparison = self._comparison
        self.export_comparison_button.setEnabled(False)
        self.status.setText("Writing statement comparison CSV…")
        self._request += 1
        request = self._request
        worker = BackgroundTask(request, lambda: self._write_comparison_csv(comparison, path))
        worker.signals.finished.connect(self._comparison_exported)
        self._worker = worker
        self._pool.start(worker)

    def _comparison_exported(self, request, path, error):
        if request != self._request:
            return
        self._worker = None
        self.export_comparison_button.setEnabled(self._comparison is not None)
        if error:
            QMessageBox.warning(self, "Could not export comparison", str(error))
            self.status.setText("Statement comparison CSV was not exported")
            return
        self.status.setText(f"Statement comparison saved: {path}")
        QMessageBox.information(self, "Comparison exported", f"Saved to:\n{path}")

    def _exported(self, request, path, error):
        if request != self._request:
            return
        self._worker = None
        self.export_button.setEnabled(self._data is not None and bool(self._data["events"]))
        if error:
            QMessageBox.warning(self, "Could not export reconciliation", str(error))
            self.status.setText("Reconciliation CSV was not exported")
            return
        self.status.setText(f"Reconciliation CSV saved: {path}")
        QMessageBox.information(self, "Reconciliation exported", f"Saved to:\n{path}")


class FeeSetupPage(QWidget):
    """Configure fee categories and term amounts without recording payments."""

    def __init__(self, username: str, role: str):
        super().__init__()
        self.username = username
        self.is_admin = role == "admin"
        self._data = {"sessions": [], "classes": [], "terms": [], "fee_types": [], "schedules": []}
        self._selected_schedule_id = None
        self._selected_fee_type_id = None
        self._loading = False
        self._writing = False
        self._load_id = 0
        self._write_id = 0
        self._workers = {}
        self._pool = QThreadPool.globalInstance()

        layout = QVBoxLayout(self)
        title = QLabel("Fee setup")
        title.setObjectName("heading")
        layout.addWidget(title)
        layout.addWidget(wrapped_label(
            "Create fee types, then set exact amounts for a class, academic session, and term. "
            "These schedules do not create student balances or record payments."
        ))

        layout.addWidget(QLabel("Fee types"))
        type_form = QHBoxLayout()
        self.type_name = QLineEdit()
        self.type_name.setPlaceholderText("Fee type name, e.g. Tuition")
        self.type_name.setMaxLength(80)
        self.type_description = QLineEdit()
        self.type_description.setPlaceholderText("Optional description")
        self.type_description.setMaxLength(240)
        self.add_type_button = QPushButton("Add fee type")
        self.add_type_button.clicked.connect(self.add_fee_type)
        self.type_status_button = QPushButton("Archive selected type")
        self.type_status_button.clicked.connect(self.toggle_fee_type)
        type_form.addWidget(self.type_name, 1)
        type_form.addWidget(self.type_description, 2)
        type_form.addWidget(self.add_type_button)
        type_form.addWidget(self.type_status_button)
        layout.addLayout(type_form)
        self.type_table = QTableWidget(0, 3)
        self.type_table.setHorizontalHeaderLabels(["Fee Type", "Fee Description", "Fee Type Status"])
        configure_table_headers(self.type_table)
        self.type_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.type_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.type_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.type_table.setMaximumHeight(150)
        self.type_table.itemSelectionChanged.connect(self._fee_type_selected)
        layout.addWidget(self.type_table)

        layout.addWidget(QLabel("Class fee schedules"))
        form = QFormLayout()
        self.session = QComboBox()
        self.class_section = QComboBox()
        self.term = QComboBox()
        self.fee_type = QComboBox()
        self.amount = QLineEdit()
        self.amount.setPlaceholderText("Enter amount in the school's local currency")
        self.due_date_enabled = QCheckBox("Set due date")
        self.due_on = QDateEdit(QDate.currentDate())
        self.due_on.setCalendarPopup(True)
        self.due_on.setDisplayFormat("yyyy-MM-dd")
        self.due_on.setEnabled(False)
        self.due_date_enabled.toggled.connect(self.due_on.setEnabled)
        form.addRow("Academic session", self.session)
        self.class_scope = QComboBox()
        self.class_scope.addItem("One class", "one")
        self.class_scope.addItem("All classes", "all")
        self.class_scope.addItem("Selected classes", "selected")
        self.class_scope.setToolTip("Apply a new fee to one class, every class, or only the classes you tick.")
        self.class_list = QListWidget()
        self.class_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self.class_list.setMaximumHeight(130)
        self.class_label = QLabel("Class and section")
        self.class_list_label = QLabel("Choose classes")
        form.addRow("Applies to", self.class_scope)
        form.addRow(self.class_label, self.class_section)
        form.addRow(self.class_list_label, self.class_list)
        form.addRow("Term", self.term)
        form.addRow("Fee type", self.fee_type)
        form.addRow("Amount", self.amount)
        due_date_row = QHBoxLayout()
        due_date_row.addWidget(self.due_date_enabled)
        due_date_row.addWidget(self.due_on, 1)
        form.addRow("Due date (optional)", due_date_row)
        layout.addLayout(form)
        self.session.currentIndexChanged.connect(self._session_changed)
        self.class_scope.currentIndexChanged.connect(self._scope_changed)
        self._scope_changed()

        schedule_actions = QHBoxLayout()
        self.save_schedule_button = QPushButton("Add fee schedule")
        self.save_schedule_button.setObjectName("primary")
        self.save_schedule_button.clicked.connect(self.save_schedule)
        self.clear_schedule_button = QPushButton("New schedule")
        self.clear_schedule_button.clicked.connect(self.clear_schedule_form)
        self.schedule_status_button = QPushButton("Archive selected schedule")
        self.schedule_status_button.clicked.connect(self.toggle_schedule)
        schedule_actions.addWidget(self.save_schedule_button)
        schedule_actions.addWidget(self.clear_schedule_button)
        schedule_actions.addWidget(self.schedule_status_button)
        schedule_actions.addStretch()
        layout.addLayout(schedule_actions)

        self.schedule_table = QTableWidget(0, 7)
        self.schedule_table.setHorizontalHeaderLabels(
            ["Academic Session", "Class", "Academic Term", "Fee Type", "Fee Amount", "Due Date", "Schedule Status"]
        )
        configure_table_headers(self.schedule_table)
        self.schedule_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.schedule_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.schedule_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.schedule_table.itemSelectionChanged.connect(self._schedule_selected)
        layout.addWidget(self.schedule_table, 1)
        self.status = QLabel("Loading fee setup…")
        layout.addWidget(self.status)

        if not self.is_admin:
            for widget in (
                self.type_name, self.type_description, self.add_type_button, self.type_status_button,
                self.session, self.class_scope, self.class_list, self.class_section, self.term, self.fee_type, self.amount,
                self.due_date_enabled, self.due_on,
                self.save_schedule_button, self.clear_schedule_button, self.schedule_status_button,
            ):
                widget.setEnabled(False)
        self.refresh()

    def refresh(self):
        self._load_id += 1
        load_id = self._load_id
        self._loading = True
        self._set_actions()
        worker = BackgroundTask(load_id, db.load_fee_setup_data)
        worker.signals.finished.connect(self._loaded)
        self._workers[("load", load_id)] = worker
        self._pool.start(worker)

    @staticmethod
    def _fill_combo(combo, prompt, rows, label_fn, selected_id=None):
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(prompt, None)
        for row in rows:
            combo.addItem(label_fn(row), int(row["id"]))
        index = combo.findData(selected_id) if selected_id is not None else -1
        if index < 0 and combo.count() > 1:
            index = 1
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.blockSignals(False)

    def _loaded(self, load_id, data, error):
        self._workers.pop(("load", load_id), None)
        if load_id != self._load_id:
            return
        self._loading = False
        if error is not None:
            self.status.setText("Could not load fee setup")
            self._set_actions()
            QMessageBox.warning(self, "Could not load fee setup", str(error))
            return
        old_session = self.session.currentData()
        old_class = self.class_section.currentData()
        old_term = self.term.currentData()
        old_type = self.fee_type.currentData()
        self._data = data
        self._fill_combo(
            self.session, "Select session", data["sessions"],
            lambda row: f"{row['name']}  (Current)" if row["is_current"] else row["name"],
            int(old_session) if old_session is not None else None,
        )
        self._fill_combo(
            self.class_section, "Select class", data["classes"],
            lambda row: f"{row['class_name']} {row['section']}".strip(),
            int(old_class) if old_class is not None else None,
        )
        self._fill_combo(
            self.fee_type, "Select fee type", data["fee_types"],
            lambda row: row["name"] if row["is_active"] else f"{row['name']} (archived)",
            int(old_type) if old_type is not None else None,
        )
        ticked = set(self._selected_class_ids())
        self.class_list.clear()
        for row in data["classes"]:
            item = QListWidgetItem(f"{row['class_name']} {row['section']}".strip())
            item.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
            self.class_list.addItem(item)
            item.setSelected(int(row["id"]) in ticked)
        self._fill_terms(int(old_term) if old_term is not None else None)
        self._render_types()
        self._render_schedules()
        self.status.setText(
            f"Loaded {len(data['fee_types'])} fee type(s) and {len(data['schedules'])} schedule(s)."
            if self.is_admin else "Fee setup is view-only for this account."
        )
        self._set_actions()

    def _selected_class_ids(self):
        return [
            int(item.data(Qt.ItemDataRole.UserRole))
            for item in self.class_list.selectedItems()
        ]

    def _scope_changed(self, *_args):
        scope = self.class_scope.currentData()
        self.class_label.setVisible(scope == "one")
        self.class_section.setVisible(scope == "one")
        self.class_list_label.setVisible(scope == "selected")
        self.class_list.setVisible(scope == "selected")

    def _fill_terms(self, selected_id=None):
        session_id = self.session.currentData()
        terms = [row for row in self._data["terms"] if row["academic_session_id"] == session_id]
        self._fill_combo(
            self.term, "Select term", terms,
            lambda row: f"{row['term_order']}. {row['term_name']}", selected_id,
        )

    def _session_changed(self, *_args):
        self._fill_terms()

    def _render_types(self):
        self.type_table.setRowCount(len(self._data["fee_types"]))
        for row_index, fee_type in enumerate(self._data["fee_types"]):
            values = (fee_type["name"], fee_type["description"], "Active" if fee_type["is_active"] else "Archived")
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, int(fee_type["id"]))
                self.type_table.setItem(row_index, column, item)
        self.type_table.resizeColumnsToContents()

    def _render_schedules(self):
        self.schedule_table.setRowCount(len(self._data["schedules"]))
        for row_index, schedule in enumerate(self._data["schedules"]):
            class_label = f"{schedule['class_name']} {schedule['section']}".strip()
            values = (
                schedule["session_name"], class_label,
                f"{schedule['term_order']}. {schedule['term_name']}",
                schedule["fee_type_name"], schedule["amount_text"], schedule["due_on"] or "—",
                "Active" if schedule["is_active"] else "Archived",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, int(schedule["id"]))
                self.schedule_table.setItem(row_index, column, item)
        self.schedule_table.resizeColumnsToContents()

    def _fee_type_selected(self):
        row = self.type_table.currentRow()
        if row < 0:
            self._selected_fee_type_id = None
            return
        item = self.type_table.item(row, 0)
        self._selected_fee_type_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        fee_type = next(
            (item for item in self._data["fee_types"] if item["id"] == self._selected_fee_type_id), None
        )
        if fee_type:
            self.type_status_button.setText("Archive selected type" if fee_type["is_active"] else "Activate selected type")
        self._set_actions()

    def _schedule_selected(self):
        row = self.schedule_table.currentRow()
        if row < 0:
            self._selected_schedule_id = None
            self._set_actions()
            return
        item = self.schedule_table.item(row, 0)
        self._selected_schedule_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        schedule = next(
            (item for item in self._data["schedules"] if item["id"] == self._selected_schedule_id), None
        )
        if schedule:
            self.class_scope.setCurrentIndex(0)
            self._fill_combo(self.session, "Select session", self._data["sessions"],
                lambda item: f"{item['name']}  (Current)" if item["is_current"] else item["name"],
                schedule["academic_session_id"])
            self._fill_combo(self.class_section, "Select class", self._data["classes"],
                lambda item: f"{item['class_name']} {item['section']}".strip(), schedule["class_id"])
            self._fill_combo(self.fee_type, "Select fee type", self._data["fee_types"],
                lambda item: item["name"] if item["is_active"] else f"{item['name']} (archived)",
                schedule["fee_type_id"])
            self._fill_terms(schedule["term_id"])
            self.amount.setText(schedule["amount_text"])
            self.due_date_enabled.setChecked(bool(schedule["due_on"]))
            if schedule["due_on"]:
                self.due_on.setDate(QDate.fromString(schedule["due_on"], "yyyy-MM-dd"))
            self.save_schedule_button.setText("Update fee schedule")
            self.schedule_status_button.setText(
                "Archive selected schedule" if schedule["is_active"] else "Reactivate selected schedule"
            )
        self._set_actions()

    def clear_schedule_form(self):
        self._selected_schedule_id = None
        self.schedule_table.clearSelection()
        self.save_schedule_button.setText("Add fee schedule")
        self.schedule_status_button.setText("Archive selected schedule")
        self.amount.clear()
        self.due_date_enabled.setChecked(False)
        self.due_on.setDate(QDate.currentDate())
        self._set_actions()

    def _set_actions(self):
        editable = self.is_admin and not self._loading and not self._writing
        self.add_type_button.setEnabled(editable)
        self.type_name.setEnabled(editable)
        self.type_description.setEnabled(editable)
        selected_type = next(
            (item for item in self._data["fee_types"] if item["id"] == self._selected_fee_type_id), None
        )
        self.type_status_button.setEnabled(editable and selected_type is not None)
        self.session.setEnabled(editable)
        self.class_section.setEnabled(editable)
        self.class_scope.setEnabled(editable and self._selected_schedule_id is None)
        self.class_list.setEnabled(editable)
        self.term.setEnabled(editable)
        self.fee_type.setEnabled(editable)
        self.amount.setEnabled(editable)
        self.due_date_enabled.setEnabled(editable)
        self.due_on.setEnabled(editable and self.due_date_enabled.isChecked())
        multi_class = self._selected_schedule_id is None and self.class_scope.currentData() != "one"
        classes_available = self.class_list.count() > 0 if multi_class else self.class_section.count() > 1
        self.save_schedule_button.setEnabled(
            editable and self.session.count() > 1 and classes_available
            and self.term.count() > 1 and self.fee_type.count() > 1
        )
        self.clear_schedule_button.setEnabled(editable)
        selected_schedule = next(
            (item for item in self._data["schedules"] if item["id"] == self._selected_schedule_id), None
        )
        self.schedule_status_button.setEnabled(editable and selected_schedule is not None)

    def _start_write(self, operation, description):
        if not self.is_admin or self._writing:
            return
        self._writing = True
        self.status.setText(description)
        self._set_actions()
        self._write_id += 1
        write_id = self._write_id
        worker = BackgroundTask(write_id, operation)
        worker.signals.finished.connect(self._write_finished)
        self._workers[("write", write_id)] = worker
        self._pool.start(worker)

    def _write_finished(self, write_id, _result, error):
        self._workers.pop(("write", write_id), None)
        if write_id != self._write_id:
            return
        self._writing = False
        if error is not None:
            self.status.setText("Fee setup was not saved")
            self._set_actions()
            QMessageBox.warning(self, "Could not save fee setup", str(error))
            return
        self.status.setText("Fee setup saved. Reloading…")
        self.refresh()

    def add_fee_type(self):
        name, description = self.type_name.text(), self.type_description.text()
        self._start_write(
            lambda: db.create_fee_type(name, description, self.username), "Adding fee type…"
        )

    def toggle_fee_type(self):
        fee_type = next(
            (item for item in self._data["fee_types"] if item["id"] == self._selected_fee_type_id), None
        )
        if fee_type is None:
            return
        desired = not bool(fee_type["is_active"])
        action = "activate" if desired else "archive"
        answer = QMessageBox.question(
            self, f"{action.title()} fee type",
            f"{action.title()} the fee type ‘{fee_type['name']}’?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        fee_type_id = int(fee_type["id"])
        self._start_write(
            lambda: db.set_fee_type_active(fee_type_id, desired, self.username),
            f"{action.title()}ing fee type…",
        )

    def save_schedule(self):
        scope = self.class_scope.currentData()
        if self._selected_schedule_id is None and scope != "one":
            class_ids = None if scope == "all" else self._selected_class_ids()
            if scope == "selected" and not class_ids:
                QMessageBox.information(self, "Choose classes", "Tick at least one class, or choose All classes.")
                return
            several = (
                self.session.currentData(), self.term.currentData(), self.fee_type.currentData(), class_ids,
                self.amount.text(),
                self.due_on.date().toString("yyyy-MM-dd") if self.due_date_enabled.isChecked() else "",
            )
            self._start_write(
                lambda: db.save_fee_schedules_for_classes(*several, self.username),
                "Saving fee schedules for the chosen classes…",
            )
            return
        values = (
            self.session.currentData(), self.class_section.currentData(), self.term.currentData(),
            self.fee_type.currentData(), self.amount.text(),
            self.due_on.date().toString("yyyy-MM-dd") if self.due_date_enabled.isChecked() else "",
        )
        schedule_id = self._selected_schedule_id
        self._start_write(
            lambda: db.save_fee_schedule(*values, self.username, schedule_id=schedule_id),
            "Saving fee schedule…",
        )

    def toggle_schedule(self):
        schedule = next(
            (item for item in self._data["schedules"] if item["id"] == self._selected_schedule_id), None
        )
        if schedule is None:
            return
        desired = not bool(schedule["is_active"])
        action = "reactivate" if desired else "archive"
        answer = QMessageBox.question(
            self, f"{action.title()} fee schedule",
            f"{action.title()} {schedule['fee_type_name']} for {schedule['class_name']} "
            f"{schedule['section']} in {schedule['session_name']}, {schedule['term_name']}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        schedule_id = int(schedule["id"])
        self._start_write(
            lambda: db.set_fee_schedule_active(schedule_id, desired, self.username),
            f"{action.title()}ing fee schedule…",
        )
