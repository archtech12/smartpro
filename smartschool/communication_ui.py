"""Communication Center: birthdays, fee reminders, outbox, templates, history, statistics, settings.

Offline by design: this screen never sends anything over a network. A person delivers each
message (copy it, or open it in their own mail / WhatsApp app) and then marks it as sent.
"""

from __future__ import annotations

from datetime import date

from PyQt6.QtCore import Qt, QThreadPool, QUrl
from PyQt6.QtGui import QDesktopServices, QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDateEdit, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from . import communications as comms
from .table_helpers import configure_table_headers
from .ui_helpers import wrapped_label
from .workers import BackgroundTask


class _Tab(QWidget):
    """Shared background-task plumbing for the tabs."""

    def __init__(self, username: str):
        super().__init__()
        self.username = username
        self._task_id = 0
        self._workers = {}
        self._pool = QThreadPool.globalInstance()

    def run(self, operation, done):
        self._task_id += 1
        task_id = self._task_id
        worker = BackgroundTask(task_id, operation)

        def finished(finished_id, result, error):
            self._workers.pop(finished_id, None)
            if finished_id != self._task_id:
                return  # a newer request replaced this one
            if error is not None:
                QMessageBox.warning(self, "Communication Center", str(error))
                return
            try:
                done(result)
            except Exception as exc:  # a display problem must never close the app
                QMessageBox.warning(self, "Communication Center", f"Could not display the result: {exc}")

        worker.signals.finished.connect(finished)
        self._workers[task_id] = worker
        self._pool.start(worker)

    def refresh(self):  # pragma: no cover - overridden
        pass


def _table(headers, multi=False):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(
        QAbstractItemView.SelectionMode.ExtendedSelection if multi else QAbstractItemView.SelectionMode.SingleSelection
    )
    configure_table_headers(table)
    return table


def _fill(table, rows, ids=None):
    table.setRowCount(len(rows))
    for r, values in enumerate(rows):
        for c, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            if c == 0 and ids is not None:
                item.setData(Qt.ItemDataRole.UserRole, ids[r])
            table.setItem(r, c, item)


def _contact_text(contacts):
    parts = [name for name, ok in (("Email", contacts["email"]), ("WhatsApp", contacts["whatsapp"])) if ok]
    return " · ".join(parts) if parts else "No usable contact"


class RecipientsTab(_Tab):
    """Birthdays today / fee reminders: choose students, choose a channel, queue messages."""

    def __init__(self, username, kind, title, subtitle):
        super().__init__(username)
        self.kind = kind
        self._rows = []
        layout = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setObjectName("subheading")
        layout.addWidget(heading)
        layout.addWidget(wrapped_label(subtitle))
        self.status = QLabel("")
        self.status.setObjectName("muted")
        layout.addWidget(self.status)
        headers = ["Student", "Class", "Birthday" if kind == "birthday" else "Balance", "Can be reached by", "Status"]
        self.table = _table(headers, multi=True)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.channel = QComboBox()
        for key, label in comms.CHANNELS.items():
            self.channel.addItem(label, key)
        self.queue_selected = QPushButton("Queue selected")
        self.queue_selected.setObjectName("primary")
        self.queue_all = QPushButton("Queue everyone who can be reached")
        self.opt_toggle = QPushButton("Guardian opted out / back in")
        self.opt_toggle.setToolTip("Toggle: opted-out guardians are never queued and their pending messages are cancelled")
        self.queue_selected.clicked.connect(lambda: self._queue(False))
        self.queue_all.clicked.connect(lambda: self._queue(True))
        self.opt_toggle.clicked.connect(self._toggle_opt_out)
        actions.addWidget(QLabel("Send by"))
        actions.addWidget(self.channel)
        actions.addWidget(self.queue_selected)
        actions.addWidget(self.queue_all)
        actions.addStretch()
        actions.addWidget(self.opt_toggle)
        layout.addLayout(actions)

    def refresh(self):
        if self.kind == "birthday":
            self.run(lambda: comms.list_birthdays(self.username), self._loaded_birthdays)
        else:
            self.run(lambda: comms.list_fee_reminder_candidates(self.username), self._loaded_fees)

    def _status_text(self, row):
        if row["opted_out"]:
            return "Opted out"
        return "Queued today" if row["queued"] else "Ready"

    def _loaded_birthdays(self, data):
        self._rows = data["today"]
        _fill(self.table, [
            (r["name"], r["class_name"], f"Turns {r['age']}", _contact_text(r["contacts"]), self._status_text(r))
            for r in self._rows], [r["student_id"] for r in self._rows])
        upcoming = ", ".join(f"{r['name']} ({r['date'][5:]})" for r in data["upcoming"][:6])
        self.status.setText(
            f"{len(self._rows)} birthday(s) today." + (f"  Coming this week: {upcoming}" if upcoming else "")
            if self._rows or upcoming else "No birthdays today or this week. (Dates of birth come from student records.)")

    def _loaded_fees(self, rows):
        self._rows = rows
        _fill(self.table, [
            (r["name"], r["class_name"], r["balance"], _contact_text(r["contacts"]), self._status_text(r)) for r in rows
        ], [r["student_id"] for r in rows])
        self.status.setText(
            f"{len(rows)} student(s) owe fees through the active term." if rows
            else "No outstanding balances on issued invoices. Reminders appear here once invoices are issued.")

    def _selected_ids(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self._rows[r]["student_id"] for r in rows if r < len(self._rows)]

    def _queue(self, everyone):
        try:
            channel = self.channel.currentData()
            ids = [r["student_id"] for r in self._rows
                   if not r["opted_out"] and not r["queued"] and r["contacts"][channel]] if everyone else self._selected_ids()
            if not ids:
                QMessageBox.information(self, "Nothing to queue", "No one is ready for this channel. Select students or choose another channel.")
                return
            self.run(lambda: comms.queue_messages(self.kind, ids, channel, self.username), self._queued)
        except Exception as exc:
            QMessageBox.warning(self, "Communication Center", str(exc))

    def _queued(self, result):
        text = f"{result['queued']} message(s) added to the Outbox."
        if result["skipped"]:
            text += "\n\nSkipped:\n" + "\n".join(f"• {s['name'] or s['student_id']}: {s['reason']}" for s in result["skipped"][:12])
        QMessageBox.information(self, "Queued", text)
        self.refresh()

    def _toggle_opt_out(self):
        ids = self._selected_ids()
        if len(ids) != 1:
            QMessageBox.information(self, "Select one student", "Select exactly one student first.")
            return
        row = next(r for r in self._rows if r["student_id"] == ids[0])
        opting_out = not row["opted_out"]
        if opting_out and QMessageBox.question(
            self, "Guardian opt-out",
            f"Stop all messages to the guardian of {row['name']}? Pending messages will be cancelled.",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.run(lambda: comms.set_opt_out(ids[0], opting_out, "Recorded in Communication Center", self.username), lambda _r: self.refresh())


class OutboxTab(_Tab):
    def __init__(self, username):
        super().__init__(username)
        self._rows = []
        layout = QVBoxLayout(self)
        heading = QLabel("Outbox")
        heading.setObjectName("subheading")
        layout.addWidget(heading)
        layout.addWidget(wrapped_label(
            "Messages waiting to be delivered. This app does not send anything itself: open the message in "
            "your own mail or WhatsApp app (or copy it), send it, then press Mark as sent."))
        body = QHBoxLayout()
        self.table = _table(["To", "Channel", "Type", "Subject", "Created"])
        self.table.itemSelectionChanged.connect(self._show_preview)
        body.addWidget(self.table, 3)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("Select a message to preview it here.")
        body.addWidget(self.preview, 2)
        layout.addLayout(body, 1)
        actions = QHBoxLayout()
        self.open_button = QPushButton("Open in mail / WhatsApp")
        self.open_button.setObjectName("primary")
        self.copy_button = QPushButton("Copy message")
        self.sent_button = QPushButton("Mark as sent")
        self.cancel_button = QPushButton("Cancel message")
        self.cancel_button.setObjectName("danger")
        self.open_button.clicked.connect(self._open_external)
        self.copy_button.clicked.connect(self._copy)
        self.sent_button.clicked.connect(lambda: self._handle(comms.mark_sent))
        self.cancel_button.clicked.connect(self._cancel)
        for button in (self.open_button, self.copy_button, self.sent_button):
            actions.addWidget(button)
        actions.addStretch()
        actions.addWidget(self.cancel_button)
        layout.addLayout(actions)

    def refresh(self):
        self.run(lambda: comms.list_messages(self.username, "pending"), self._loaded)

    def _loaded(self, rows):
        self._rows = list(reversed(rows))  # oldest first
        _fill(self.table, [
            (r["recipient_name"], comms.CHANNELS[r["channel"]], comms.KINDS[r["kind"]], r["subject"], r["created_at"][:16])
            for r in self._rows], [r["id"] for r in self._rows])
        self.preview.clear()

    def _current(self):
        row = self.table.currentRow()
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def _show_preview(self):
        message = self._current()
        if message:
            self.preview.setPlainText(
                f"To: {message['recipient_name']} <{message['recipient_contact']}>\nSubject: {message['subject']}\n\n{message['body']}")

    def _open_external(self):
        message = self._current()
        if not message:
            return
        url = (comms.mailto_url(message["recipient_contact"], message["subject"], message["body"])
               if message["channel"] == "email" else comms.whatsapp_url(message["recipient_contact"], message["body"]))
        if not QDesktopServices.openUrl(QUrl(url)):
            QMessageBox.information(self, "Could not open", "No app handled the link. Use Copy message instead.")

    def _copy(self):
        message = self._current()
        if message:
            QGuiApplication.clipboard().setText(f"{message['subject']}\n\n{message['body']}")

    def _handle(self, function):
        message = self._current()
        if message:
            self.run(lambda: function(message["id"], self.username), lambda _r: self._after())

    def _cancel(self):
        message = self._current()
        if message and QMessageBox.question(self, "Cancel message", f"Cancel the message to {message['recipient_name']}?") == QMessageBox.StandardButton.Yes:
            self._handle(comms.cancel_message)

    def _after(self):
        self.refresh()


class TemplatesTab(_Tab):
    def __init__(self, username):
        super().__init__(username)
        self._templates = {}
        layout = QVBoxLayout(self)
        heading = QLabel("Message templates")
        heading.setObjectName("subheading")
        layout.addWidget(heading)
        layout.addWidget(wrapped_label("Edit the wording of each message type. Placeholders in braces are filled in for each student."))
        self.kind = QComboBox()
        for key, label in comms.KINDS.items():
            self.kind.addItem(label, key)
        self.kind.currentIndexChanged.connect(self._show)
        form = QFormLayout()
        form.addRow("Message type", self.kind)
        self.subject = QLineEdit()
        form.addRow("Subject", self.subject)
        layout.addLayout(form)
        self.body = QPlainTextEdit()
        layout.addWidget(self.body, 1)
        hint = QLabel("Placeholders: " + "  ".join("{" + p + "}" for p in comms.PLACEHOLDERS))
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.state = QLabel("")
        self.state.setObjectName("muted")
        layout.addWidget(self.state)
        actions = QHBoxLayout()
        save = QPushButton("Save template")
        save.setObjectName("primary")
        reset = QPushButton("Restore default wording")
        save.clicked.connect(self._save)
        reset.clicked.connect(self._reset)
        actions.addWidget(save)
        actions.addWidget(reset)
        actions.addStretch()
        layout.addLayout(actions)

    def refresh(self):
        self.run(lambda: comms.load_templates(self.username), self._loaded)

    def _loaded(self, templates):
        self._templates = templates
        self._show()

    def _show(self):
        template = self._templates.get(self.kind.currentData())
        if template:
            self.subject.setText(template["subject"])
            self.body.setPlainText(template["body"])
            self.state.setText("Customised by your school." if template["customised"] else "Using the default wording.")

    def _save(self):
        kind, subject, body = self.kind.currentData(), self.subject.text(), self.body.toPlainText()
        self.run(lambda: comms.save_template(kind, subject, body, self.username), lambda _r: self.refresh())

    def _reset(self):
        kind = self.kind.currentData()
        if QMessageBox.question(self, "Restore default", "Replace this template with the default wording?") == QMessageBox.StandardButton.Yes:
            self.run(lambda: comms.reset_template(kind, self.username), lambda _r: self.refresh())


class HistoryTab(_Tab):
    def __init__(self, username):
        super().__init__(username)
        self._rows = []
        layout = QVBoxLayout(self)
        heading = QLabel("Communication history")
        heading.setObjectName("subheading")
        layout.addWidget(heading)
        filters = QHBoxLayout()
        self.status = QComboBox()
        self.status.addItem("Every status", None)
        for key in ("pending", "sent", "cancelled"):
            self.status.addItem(key.capitalize(), key)
        self.kind = QComboBox()
        self.kind.addItem("Every type", None)
        for key, label in comms.KINDS.items():
            self.kind.addItem(label, key)
        self.use_dates = QComboBox()
        self.use_dates.addItem("Any date", False)
        self.use_dates.addItem("Between dates", True)
        self.starts = QDateEdit(date.today().replace(day=1))
        self.ends = QDateEdit(date.today())
        for edit in (self.starts, self.ends):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
        for widget in (self.status, self.kind, self.use_dates, self.starts, self.ends):
            filters.addWidget(widget)
        apply = QPushButton("Show")
        apply.clicked.connect(self.refresh)
        filters.addWidget(apply)
        filters.addStretch()
        layout.addLayout(filters)
        self.table = _table(["Created", "To", "Channel", "Type", "Subject", "Status", "Handled by"])
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.requeue = QPushButton("Send again (re-queue)")
        self.export = QPushButton("Export CSV")
        self.requeue.clicked.connect(self._requeue)
        self.export.clicked.connect(self._export)
        actions.addWidget(self.requeue)
        actions.addWidget(self.export)
        actions.addStretch()
        layout.addLayout(actions)

    def _filters(self):
        filters = {"status": self.status.currentData(), "kind": self.kind.currentData()}
        if self.use_dates.currentData():
            filters["starts_on"] = self.starts.date().toString("yyyy-MM-dd")
            filters["ends_on"] = self.ends.date().toString("yyyy-MM-dd")
        return filters

    def refresh(self):
        filters = self._filters()
        self.run(lambda: comms.list_messages(self.username, **filters), self._loaded)

    def _loaded(self, rows):
        self._rows = rows
        _fill(self.table, [
            (r["created_at"][:16], r["recipient_name"], comms.CHANNELS[r["channel"]], comms.KINDS[r["kind"]],
             r["subject"], r["status"].capitalize(), r["handled_by"] or "") for r in rows], [r["id"] for r in rows])

    def _requeue(self):
        row = self.table.currentRow()
        if 0 <= row < len(self._rows):
            message_id = self._rows[row]["id"]
            self.run(lambda: comms.requeue_message(message_id, self.username), self._requeued)

    def _requeued(self, _result):
        QMessageBox.information(self, "Re-queued", "A new copy was added to the Outbox.")
        self.refresh()

    def _export(self):
        path, _f = QFileDialog.getSaveFileName(self, "Export history", "communication-history.csv", "CSV files (*.csv)")
        if path:
            filters = self._filters()
            self.run(lambda: comms.export_messages_csv(self.username, path, **filters),
                     lambda count: QMessageBox.information(self, "Export complete", f"{count} row(s) exported."))


class StatisticsTab(_Tab):
    def __init__(self, username):
        super().__init__(username)
        layout = QVBoxLayout(self)
        heading = QLabel("Communication statistics")
        heading.setObjectName("subheading")
        layout.addWidget(heading)
        self.cards = QGridLayout()
        layout.addLayout(self.cards)
        self.bars = QVBoxLayout()
        layout.addLayout(self.bars)
        layout.addStretch()

    def refresh(self):
        self.run(lambda: comms.load_statistics(self.username), self._loaded)

    @staticmethod
    def _clear(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _loaded(self, stats):
        self._clear(self.cards)
        self._clear(self.bars)
        for column, (label, value) in enumerate((
            ("Pending", stats["pending"]), ("Sent", stats["sent"]), ("Cancelled", stats["cancelled"]),
            ("Sent today", stats["sent_today"]), ("Guardians opted out", stats["opted_out"]),
        )):
            card = QFrame()
            card.setObjectName("card")
            box = QVBoxLayout(card)
            title = QLabel(label)
            title.setObjectName("cardLabel")
            number = QLabel(str(value))
            number.setObjectName("cardValue")
            box.addWidget(title)
            box.addWidget(number)
            self.cards.addWidget(card, 0, column)
        self._add_bars("Messages by type", {comms.KINDS[k]: v for k, v in stats["by_kind"].items()})
        self._add_bars("Messages by channel", {comms.CHANNELS[k]: v for k, v in stats["by_channel"].items()})
        self._add_bars("Last 6 months", stats["monthly"])

    def _add_bars(self, title, values):
        heading = QLabel(title)
        heading.setObjectName("subheading")
        self.bars.addWidget(heading)
        top = max(values.values(), default=0)
        if top == 0:
            empty = QLabel("Nothing to show yet.")
            empty.setObjectName("muted")
            self.bars.addWidget(empty)
            return
        for label, value in values.items():
            row = QHBoxLayout()
            name = QLabel(label)
            name.setMinimumWidth(150)
            bar = QProgressBar()
            bar.setRange(0, top)
            bar.setValue(value)
            bar.setFormat(str(value))
            row.addWidget(name)
            row.addWidget(bar, 1)
            holder = QWidget()
            holder.setLayout(row)
            self.bars.addWidget(holder)


class SettingsTab(_Tab):
    def __init__(self, username):
        super().__init__(username)
        layout = QVBoxLayout(self)
        heading = QLabel("Communication settings")
        heading.setObjectName("subheading")
        layout.addWidget(heading)
        layout.addWidget(wrapped_label(
            "SmartSchool never sends email or WhatsApp messages by itself and uses no network connection for "
            "communication. Messages are prepared here; you deliver them from your own mail or WhatsApp app."))
        form = QFormLayout()
        self.code = QLineEdit()
        self.code.setPlaceholderText("Digits only, e.g. your country's calling code")
        self.code.setMaximumWidth(260)
        form.addRow("Country calling code", self.code)
        layout.addLayout(form)
        layout.addWidget(wrapped_label(
            "Used only to turn local phone numbers that start with 0 into international WhatsApp numbers. "
            "Numbers already written as +… or 00… are used as they are. Leave empty if all numbers include their code."))
        save = QPushButton("Save settings")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        layout.addWidget(save, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()

    def refresh(self):
        self.run(lambda: comms.load_settings(self.username), lambda s: self.code.setText(s["country_code"]))

    def _save(self):
        code = self.code.text()
        self.run(lambda: comms.save_country_code(code, self.username),
                 lambda _r: QMessageBox.information(self, "Saved", "Communication settings saved."))


class CommunicationCenterPage(QWidget):
    def __init__(self, username: str, role: str):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        title = QLabel("Communication Center")
        title.setObjectName("heading")
        layout.addWidget(title)
        if role != "admin":
            layout.addWidget(wrapped_label("Only an administrator can use the Communication Center."))
            self.tabs = QTabWidget()
            self._tabs = []
            return
        layout.addWidget(wrapped_label("Prepare birthday greetings, fee reminders and notices for guardians. Everything stays on this computer."))
        self.tabs = QTabWidget()
        self.birthdays = RecipientsTab(username, "birthday", "Birthday greetings", "Students whose birthday is today. Choose who to greet and how.")
        self.fees = RecipientsTab(username, "fee_reminder", "Fee reminders", "Students who still owe fees on issued invoices through the active term. Amounts carry no currency symbol; add one in the template if you want it.")
        self.outbox = OutboxTab(username)
        self.templates = TemplatesTab(username)
        self.history = HistoryTab(username)
        self.statistics = StatisticsTab(username)
        self.settings = SettingsTab(username)
        self._tabs = [self.birthdays, self.fees, self.outbox, self.templates, self.history, self.statistics, self.settings]
        for tab, name in zip(self._tabs, ("Birthdays", "Fee Reminders", "Outbox", "Templates", "History", "Statistics", "Settings")):
            self.tabs.addTab(tab, name)
        self.tabs.currentChanged.connect(lambda index: self._tabs[index].refresh())
        layout.addWidget(self.tabs, 1)
        self.refresh()

    def refresh(self):
        if self._tabs:
            self._tabs[self.tabs.currentIndex()].refresh()
