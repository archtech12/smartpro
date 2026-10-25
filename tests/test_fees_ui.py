import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QDate
from PyQt6.QtWidgets import QApplication

from smartschool.fees_ui import RecordPaymentDialog


def test_record_payment_dialog_collects_invoice_scoped_details():
    app = QApplication.instance() or QApplication([])
    invoice = {
        "student_name_snapshot": "Amina Okafor", "admission_no_snapshot": "ADM-2026-00001",
        "invoice_number": "INV-001", "balance_amount_text": "125.50",
    }
    dialog = RecordPaymentDialog(invoice)
    dialog.amount.setText("25.50")
    dialog.method.setCurrentIndex(dialog.method.findData("mobile_money"))
    dialog.reference.setText("MOBILE-REF-1")
    dialog.received_on.setDate(QDate.currentDate())
    assert dialog.values() == ("25.50", "mobile_money", "MOBILE-REF-1", QDate.currentDate().toString("yyyy-MM-dd"))
    dialog.close()
    app.processEvents()
