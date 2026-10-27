# Parity with the previous SmartSchool application

Reference: the old `SmartSchool` repository (read-only) and the October 2026 screenshots PDF.
Status: **Done** = in the rebuild and tested at data level; **Done (new way)** = same job, deliberately different design;
**Planned** = wanted, not built yet; **Excluded** = intentionally not carried over (reason given).

## Main pages
| Old page | Status | Notes |
|---|---|---|
| Dashboard (alerts, activity feed, collection rate, gender split, class strength) | Done | Alerts, fee collection (no currency symbol), students per class, activity feed, communications glance. Top-performer card not carried over (needs a rule for "top"). |
| Attendance | Partial | Present/Absent registers with corrected history. **Planned:** Late / Excused / Sick statuses (needs a verified table rebuild), Mark all present/absent, Copy yesterday, register import/export. |
| Students (add, import/export, profile) | Done | Guided registration, CSV import with preview, CSV export, profile dialog. **Planned:** Excel (.xlsx) import/export. |
| Fees & Payments (settings, record, history, class balances, statistics) | Done (new way) | Fee Setup (one/all/selected classes), invoices with issue controls, payments, lump sum + statement, receipts, transaction history, per-class balances. **Planned:** fee statistics view. Annual whole-session fee needs a school decision. |
| Grade Entry (CA/Exam, master sheet, import) | Done | Plus overall average and class position. **Planned:** psychomotor/affective ratings dialog. |
| Assignments (teacher ↔ class-subject) | Done (new way) | Class Subjects page with bulk assignment. |
| Communication Center (dashboard, birthdays, fee reminders, queue, announcements, templates, history, statistics, settings) | Done (new way) | Offline outbox: messages are opened in the user's own mail/WhatsApp app or copied, then marked sent. **Planned:** announcements to a class or the whole school, report-card attachments. |
| Teachers | Done | Staff records and teacher logins. |
| Timetables (auto-scheduler) | Partial | Manual slot editor with conflict checks and PDF. **Planned:** auto-generator and master/teacher/class views. |
| Exam Manager (CBT) | Excluded | Removed at the owner's request (schema v21). |
| Reports | Done | Report cards, subject sheets, master sheets, one A4 page per student. |
| Promotions & graduation | Partial | Preview-first class promotion keeps history. **Planned:** promotion that carries debts, graduation to alumni, whole-school promotion run. |
| Academic Imports | Partial | Roster CSV and score CSV with validation. **Planned:** Excel templates for scores and skills. |
| Executive Dashboard | Partial | Fee collection and class strength on the dashboard. **Planned:** dedicated page with performance by class. |

## System settings
| Old section | Status | Notes |
|---|---|---|
| School Profile (name, logo, signatures, term closing / next term dates) | Done | Term dates added (schema v26) and printed on report cards. Motto and student-ID prefix: **Planned**. |
| Academic Sessions / terms | Done | Academic Calendar. |
| Classes, Global Subjects, Class Subjects | Done | Classes/Subjects pages, Class Stages, Class Subjects. |
| Grading System (weights, bands) | Done | Grade Bands + Assessment Setup. |
| Psychomotor & Affective | Planned | Setup list, 1–5 ratings per student per term, printed on the card. |
| Attendance statuses | Planned | See Attendance. |
| Security (change password) | Done | "Change my password" for every role. **Planned:** finance PIN, read-only mode. |
| Maintenance & Backup | Done | Verified backup/restore to a separate file. |
| Audit Log | Done | Read-only viewer with filters and CSV export. |
| Diagnostics | Done | |
| Configuration wizard | Done (new way) | Setup guide on the dashboard. |
| License & Plan, Smart Services billing, SmartSync Cloud | Excluded | Online licensing, paid credits and cloud sync conflict with the offline-only rule; revisit only on request. |
| Direct SMTP / WhatsApp sending | Excluded for now | Needs explicit approval because it opens network connections. |
