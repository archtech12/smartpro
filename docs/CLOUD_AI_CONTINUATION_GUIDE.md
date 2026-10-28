# SmartSchool Manager — Cloud AI Continuation Guide

> Use this file as working instructions when continuing development with a cloud coding assistant. Keep all changes inside this rebuild repository.

## Project boundary

- This is a new, offline-first desktop rebuild built with Python, PyQt6, and SQLite.
- Historical desktop reference repository: <https://github.com/archtech12/SmartSchool.git>. Use it only to understand old workflows/UI when needed; do not copy its whole tree or inherit its behavior without checking it against the user's screenshots and the rebuild's current requirements.
- Destination repository for the new version: <https://github.com/archtech12/smartpro.git>. New development and intentional commits belong there, not in the historical repository.
- Work only in this rebuild repository. The user has allowed read-only inspection of the recovered `SmartSchool-main` desktop source for workflow ideas when specifically useful; never modify it, copy its whole project tree, or use its database. Do not access the installed application or any original school-data database.
- Do not delete redundant project files or databases without first identifying each exact target and confirming it is inside this repository.
- Do not add cloud synchronization, assumed currency, fee proration, or encryption changes unless the user requests them and the design is reviewed.
- Treat user-provided PDFs and screenshots as visual/workflow references. Do not treat their screenshots as proof that every old behavior is correct.

## Git and cleanup rules

- Before staging or committing, inspect `git status --short`, `git diff --stat`, and the exact files that will be included. Stage specific reviewed paths; do not blindly use `git add .`.
- Do not include local virtual environments, Python caches, pytest temp directories, runtime logs, school databases, backups, secrets, build output, or duplicate recovered project trees in commits. Keep generated/local artifacts ignored.
- Remove only clearly identified redundant or generated files that are inside the active `smartpro` checkout. Do not delete source, documentation, test data needed by the project, user files, or databases just to make a commit smaller. If a folder's purpose is unclear, inspect references and leave it until resolved.
- Keep the old `SmartSchool` repository read-only. Never commit rebuild changes there.
- This folder is the `smartpro` Git checkout. Confirm the active branch and remote before every push; current observed branch is `overall-average-results`, with `origin` at `https://github.com/archtech12/smartpro.git`. Do not push to the historical repository.

## Product goals

- Make routine school tasks understandable from the page itself, with short, direct labels and few steps.
- Support Primary schools, Secondary schools, and combined schools. Do not hard-code one school level or one school's grade thresholds.
- Favor responsive layouts, fast interaction, and background work for slow database reads, imports, and exports. Avoid blocking the Qt UI thread.
- Match the supplied SmartSchool screenshots where they clarify the requested workflow, while retaining the rebuild's data validation and audit safeguards.

## Grade entry and results

- The familiar workflow is: choose **Session**, **Term**, **Class**, and **Subject**; the enrolled class roster appears; enter component scores directly in the rows; calculated columns update; save all grades.
- On the first administrator load of a class-subject/term with no assessment components, the current implementation creates **CA Score (40 points)** and **Exam Score (60 points)** automatically. These are starter defaults from the user's reference workflow. The school can edit component names and maxima in **Assessment Setup** before any score is saved. Do not add a setup dialog or extra setup click to the normal grade-entry path.
- Grade-entry headers show the configured component maxima and the visible calculated **Total** and **Grade**. Max Points and Percent remain available internally but are hidden from the entry grid to preserve space. Keep exported spreadsheet headers short and stable.
- **No CGPA/GPA.** A subject result is points earned divided by maximum points, converted to a percentage, then mapped through the school's grade bands for that session and Primary/Secondary stage. Grade bands are school-configured; do not invent or seed a universal scale.
- A blank required component is incomplete, not zero. Reject out-of-range values; never silently clamp imported scores. Use decimal arithmetic and preserve calculation precision.
- Overall average is implemented on report cards and master-sheet rows as the equal-weight mean of subject percentages, only when every assigned subject is complete and approved (never counting missing as zero). It is separate from per-subject grades. Its grade comes from the exact average through bands only when every subject snapshot froze identical bands. Class position uses exact averages with competition ranking (equal averages share a position, next skipped) over the frozen approved roster and appears only when every subject is approved with matching rosters. A School Profile setting chooses whether report cards print position or average grade. Report cards are one A4 page per student with stepwise tightening for many subjects. Position never decides a subject grade; see `docs/ASSESSMENT_POLICY.md` items 8-10.
- Draft scores require explicit saving. Approval locks complete results. Corrections after approval require administrator authority, a reason, and audit history. Teacher reads/writes must remain scoped to active assigned class-subjects.
- Preserve CSV and Excel roster/score import templates and the approved PDF report, subject-sheet, and master-sheet workflows.
- Current usability work: Grade Entry places a compact student search (max 360 px) beside its title, shows a 300 px name column, hides the nonessential Status column, and lays six actions in one row on wide windows. Student Register keeps Import and Excel template actions in its main toolbar. Recheck these at native Windows display scaling and narrower window sizes before release.

## Fees and payments

- Keep fee configuration, invoice batches, student balances, payment history, class balances, and transaction reconciliation clear and contextual.
- Fee Setup can apply one fee to one class, all classes, or selected classes in one atomic save. A student's **Statement / lump sum** (beside Record payment) shows what is owed through the active term, with earlier unpaid terms carried forward, and allocates one lump-sum payment oldest-first across issued invoices under one receipt; it never bypasses invoice issuance, overpayment rejection, immutable receipts, or reasoned reversals.
- The current Fee Invoices page uses a compact view selector. Keep **Record payment** beside **Student Balances** and batch **Issue/Cancel** actions inside **Invoice Batches**.
- Payment is recorded against a selected issued invoice with an outstanding balance. Support partial payments, reject overpayment, retain immutable receipts, and require a reason for reversals. Do not bypass invoice issuance or alter ledger history to simplify the UI.
- Fee schedules are reviewed before draft invoices are created; drafts are reviewed before issue. Partial-term enrollment is flagged for review, never automatically prorated.
- Never assume a currency label or permit a financial action without the existing role checks and transaction/audit handling.

## Security and data safety

- Keep authorization in database/service functions, not only in UI visibility or disabled buttons.
- Use parameterized SQL and short SQLite transactions. Keep each financial/assessment mutation and its audit entry atomic.
- Use only temporary databases and synthetic records for tests. Never point tests at a school's active database or overwrite a database to make a test pass.
- Keep migrations additive and preserve user data. Back up and verify an existing database before migrations; do not replace the original file.
- Keep local app data offline-first and local. Do not introduce network calls or telemetry without explicit approval.

## Remaining work — prioritize in this order

1. **Native Windows usability review (highest priority).** Review Grade Entry, Student Register/import, Fees/Payments, Attendance, report-card dialogs, and the main navigation at common laptop resolutions and Windows scaling settings. Check that the key columns and names are visible at a glance, labels are understandable, buttons are not clipped, keyboard navigation works, dialogs fit, and the app remains responsive while loading. The latest synthetic offscreen check is not a substitute for running the app on Windows.
2. **Assessment policy decisions before implementation.** The school must define assessment weighting, absent/exempt score handling, and an authorized reopen/reapproval lifecycle. Do not implement policy from guesses. Confirm report-card wording and the handling of withdrawn students and cumulative/annual position with the school.
3. **Attendance policy.** Add late/excused states only after the school defines what each state means and how it affects attendance summaries and reports.
4. **Fee workflow decisions and gaps.** Get a school review of collection/reconciliation practices. Design cross-session student-first lookup, annual/session-wide fee schedules, invoice generation on term change, fee statistics, and school-configurable currency display without assuming billing rules or currency. Native XLSX workbook support is already present; avoid duplicating it.
5. **CBT scope.** The removed Question Bank is not part of the current rebuild. The current administrator-started FastAPI component is a LAN health-check foundation only; exam authoring, candidate access, answer persistence/recovery, marking, and duplicate submission handling remain unimplemented. Write the exam and network safety specification before adding those workflows.
6. **Operations and release readiness.** Complete the live operational backup/restore review, validate imports for any remaining general records, run the full tests and performance checks on the target Windows environment, build and exercise the installer, and review accessibility/security before release. Encryption and public/cloud synchronization are deferred; retain offline operation.
7. **Communication Center.** The offline templates/outbox/history/consent workflows exist and do not send messages automatically. Native Windows review remains; any SMTP/WhatsApp delivery adapter needs an explicit product and security decision. Do not describe the center as absent or silently enable network sending.

## Development and verification

1. Read the relevant `docs/` files and inspect current source/tests before editing. Reconcile stale or contradictory documentation with the code and the latest user direction.
2. Use the project-local `.venv`; do not install project dependencies globally. Install only declared dependencies from `requirements.txt` and `requirements-dev.txt` when required.
3. Run focused tests for the changed workflow, then the full suite and syntax compilation where practical:

   ```powershell
   $env:QT_QPA_PLATFORM = 'offscreen'
   .\.venv\Scripts\python.exe -m pytest -q -p no:debugging
   .\.venv\Scripts\python.exe -m compileall -q main.py smartschool tests
   ```

4. If testing UI/database behavior, use pytest temporary paths or a clearly named temporary data directory containing synthetic records. Never use recovered or live school data.
5. Distinguish an offscreen UI test from a native Windows visual review. Report the exact commands, results, files changed, and any remaining limitation. Do not claim an app launch or test that did not happen.
6. Do not add unrelated features or rewrite architecture while addressing a focused usability task.

## Documentation to consult

- `docs/PROJECT_AUDIT.md`
- `docs/ARCHITECTURE.md`
- `docs/FEATURE_MATRIX.md`
- `docs/IMPLEMENTATION_ROADMAP.md`
- `docs/SECURITY_MODEL.md`
- `docs/ASSESSMENT_POLICY.md`
- `docs/PACKAGING.md` (Windows installer build)

## Last verified checkpoint — 2026-10-27

- `.venv\\Scripts\\python.exe -m py_compile smartschool\\assessment_ui.py smartschool\\ui.py smartschool\\theme.py`: passed after the latest UI changes.
- `.venv\\Scripts\\python.exe -m pytest -q -p no:debugging --basetemp .pytest-handoff-20261027`: 312 passed in 153.37 seconds.
- `.venv\\Scripts\\python.exe -m compileall -q main.py smartschool tests`: passed.
- Synthetic offscreen Grade Entry check: passed. Confirmed the compact search is at the title row, student-name width is 300 px, Status is hidden, and all six action controls occupy one row at a 1600 px page width.
- `git diff --check`: passed; Git emitted only existing LF/CRLF working-copy notices.
- The full test suite and a native Windows visual review were not run for this checkpoint. Do not claim either passed. No native app was launched and no school data was read.
- Re-run focused and full tests before release. Keep the CA/Exam first-use defaults, no-CGPA rule, overall-average behavior, and policy-dependent position behavior consistent with `docs/ASSESSMENT_POLICY.md`, tests, and the implementation.
