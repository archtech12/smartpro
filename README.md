# SmartSchool Manager Rebuild — Foundation

This is a **new, separate rebuild project**. It does not modify the original installation or installer.

## Project location
Extract this ZIP to:
`D:\SmartSchoolManager_Rebuild`

## Requirements
- Windows 10/11
- Python 3.12 or newer
- Internet is needed only to install the Python dependency during setup. The app itself is designed to run offline.

## Setup
Open PowerShell in the extracted folder and run:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python main.py
```

For development and tests, install the declared test dependency too:
```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

If PowerShell blocks activation, use:
```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

After setting up the project, Windows users can also double-click `app.bat` to launch it.
The launcher uses the repository's `.venv` and the normal per-user school-data folder.

## First run
The app asks you to create the first administrator account. There is no hard-coded default password.

## Current status
This is an **early offline-first rebuild**, not a completed enterprise release. It includes:
- First-run administrator creation and local login
- Local SQLite database
- Dashboard
- Basic CRUD screens for students, teachers, classes, and subjects
- Guided student registration: enter required names, optional contact details, choose a class/section and academic session, and receive a generated admission ID; the student and enrollment save together
- Preview-first student CSV registration: validate a roster, select one class/session/date, generate admission IDs, and save all student/enrollment records atomically
- Search and CSV export for the currently selected module
- Paginated record and enrollment tables, background search/export, and searchable student lookup
- Background database initialization and migration with a visible startup progress state
- Screens build on demand so startup loads the dashboard first and avoids eagerly querying every workflow
- Administrator-only online database backup creation and SQLite integrity, foreign-key, and schema-history verification
- Restore a verified backup into a separate database file without replacing the active school database
- Open a restored database through an explicit process-scoped `SMARTSCHOOL_DATA_DIR` override; a missing database file stops startup rather than creating an empty one
- Administrator-only read-only database diagnostics run off the UI thread
- PBKDF2 password hashing and parameterized SQL
- Versioned schema initialization with an integrity-checked pre-migration copy
- Progressive delays after failed sign-in attempts
- Administrator checks in data mutation functions
- Rotating local application logs
- Academic sessions and terms with date validation and admin-managed setup
- Explicit student enrollment history tied to sessions and class/sections
- Class transfers, completion, and withdrawal with retained enrollment records
- Preview-first class roster promotion between explicitly selected sessions and classes, with destination conflicts and inactive students identified and source enrollments retained
- Read-only legacy class value review with student counts; no automatic mapping
- Administrator-confirmed legacy class group mapping to explicit session enrollments
- Session-specific class subject assignments with optional active-teacher assignment
- Administrator-managed teacher logins explicitly linked to active staff records; teacher workspace is limited to assigned draft grade entry and assigned-class attendance, while result approvals and approved-result corrections remain administrator-only
- Explicit Primary or Secondary stage per class; existing classes require administrator classification before subject assignments or enrollment mapping
- Configurable grade thresholds and remarks by academic session and stage, with no prefilled or assumed grading scale
- Term-scoped assessment component setup with decimal maximum points
- Administrator-only draft grade entry, complete-result approval/locking, and reasoned corrections with preserved score history
- Grade entry CSV template export and validated draft score import matched by admission number
- Individual report card PDF export from complete approved class-subject results, including the overall average (equal-weight mean of subject percentages) and either the class position (equal averages share a position, next skipped) or the overall average grade from the school's bands, as chosen in **Settings > School Profile > Report card shows**; shown only when every subject is complete and approved, and position only when the whole class is approved with matching rosters. Each student gets exactly one A4 page: the layout tightens step by step (smaller type and padding, then the remark column) so 20 or more subjects still fit, and a card that cannot fit is refused rather than split. The card prints the class's Primary/Secondary stage, a grading key from the school's bands, and optional class-teacher and head-of-school signature images uploaded in the School Profile
- Class report-card PDF export with one page per enrolled student, each containing that student's approved subject results
- Optional student PNG/JPEG photos on report cards; school identity, logo, head and class-teacher signatory details are configured in School Profile
- Read-only Student Profile from Student Register with photo, enrollment history, approved results, attendance counts, and fee invoice balances
- Class result sheet and class master sheet PDF exports from complete approved results; the master sheet requires consistent frozen rosters across subjects
- Admin-managed optional school identity, signatory details, and size-limited PNG/JPEG logo printed on result PDFs
- School Profile school type selection for Primary, Secondary, or Primary and Secondary schools
- Explicit current academic session and current term selection for each session
- Session, class, and date-scoped attendance with explicit Present/Absent marks and reasoned correction history; unmarked is not treated as absent
- Attendance date-range summaries that report enrolled, present, absent, and unmarked counts separately
- Session-scoped timetable slots with editable weekdays and 24-hour start/end times; class, assigned-teacher, and named-location overlaps are blocked without imposing period lengths or a school-week pattern
- Landscape PDF export of the selected session timetable with optional school profile and logo
- Admin-managed fee schedules, immutable draft invoices, issuance, partial receipts, derived balances, reasoned reversals, receipt PDFs, date-scoped reconciliation CSV, and read-only comparison with a generic statement CSV
- Isolated foundation, calendar, enrollment, class-stage, class-subject, assessment, grade-band, attendance, and timetable tests in `tests/`

Still remaining: define a school-authorized reopen and reapproval lifecycle, assessment weighting, absence/exemption policy, attendance late/excused statuses, school review of local fee collection and statement-comparison practices, CBT, email/WhatsApp queues, licensing, encrypted database, general student/record import validation, native Windows visual review, and installer packaging. Legacy class values are never mapped automatically; administrator-confirmed mapping is available. Review `docs/` for the audit, architecture, feature matrix, security model, and roadmap. Review the mapping, class-subject, assessment setup, grade-entry, and report-card flows on synthetic records before real school data use. The app can open a restored copy through a process-scoped data-directory override; replacing the usual active database in place remains unsupported.

### Setting up teacher grade entry

First add the staff member under **Main > Teachers** and make sure their status is **Active**. Then open **Settings > Class Subjects**, choose the academic session and class, and add the subject if it is not already assigned. In the assignments list, select that class-subject row and click **Edit selected**. Choose the staff member in **Assigned teacher**, then save. The teacher selector includes active teacher records; choose **Unassigned** to remove an assignment. Repeat for each subject/class combination the staff member teaches.

To let that teacher use their assignments, open **Settings > User Accounts** and create a teacher login linked to the same active staff record, or link an existing teacher login. The teacher signs in with that account and sees **Grade Entry** and **Attendance** only. They can enter and save draft scores for assigned class-subjects and take attendance for those classes in the selected session. Changing a saved attendance mark requires a reason and is retained in history; administrators retain result approval and correction access. Existing teacher logins are not linked automatically.

### Entering scores with a CSV

Prepare the class before entering results: each student must have an enrollment in the academic session and class, and the subject must be assigned to that class in **Settings > Class Subjects**. Grade Entry uses **Session**, **Term**, **Class**, and **Subject** selectors. On the first administrator load of a class-subject and term with no components, the app creates **CA Score (40)** and **Exam Score (60)** starter columns automatically. The school can edit the component names and maximum points in **Settings > Assessment Setup** before saving scores; additional components can be added there. Set grading bands for the session and school stage in **Settings > Grade Bands**.

Open **Main > Grade Entry**, then select session, term, class, and subject. The app automatically loads students whose active enrollment overlaps that term; you do not need to load each student individually. A newly registered student is enrolled in the class/session selected on the registration form. For an existing student moving classes or sessions, use **Main > Enrollments** so the previous placement stays in history. If a subject or student is missing, check its class-subject assignment and the student's enrollment.

Enter scores directly in the student rows, or choose **Export CSV template** to get the loaded roster and component columns. Fill the score columns and choose **Import Scores**. Admission number identifies each student; the name columns are for reference. The app validates the entire file against the selected roster and each component's maximum before showing a preview. As you type or review imported values, the table recalculates total, maximum points, percentage, and subject grade using the school's configured grade bands. These values are a preview until you click **Save All Grades**; saved scores remain drafts until an administrator approves them. Blank CSV score cells leave existing table values unchanged. Grade labels should not be entered in the CSV.

### Registering a student

Open **Students** from the Main navigation and choose **Register student**. Enter the student's first and last name, add available contact details, and select the class/section and academic session. The app generates the admission ID when saving and creates the student's enrollment in the selected class at the same time. Add the class and academic session in Settings first if either list is empty. To move an existing student to another class, use **Enrollments** so the previous placement remains in the history.

For a roster, choose **CSV template**, fill `first_name` and `last_name` for each student, and optionally fill `gender` and `guardian_phone`. Choose **Import students CSV…**, select the class, session, and enrollment date, then review the validated rows before confirming. Admission IDs are generated automatically. The whole import succeeds together or rolls back together if a row cannot be registered.

To retain a student record while removing the student from active workflows, select the student and choose **Archive student**. The register displays Active or Archived status and can filter to all, active, or archived students. Archived students and their enrollment history remain stored; restore the record from the same screen when needed. Both actions require an administrator and are audited.

### Setting up fees and taking payments

In **Settings > Fee Setup**, define fee types and schedules for an academic session and term. Choose **Applies to**: **One class**, **All classes**, or **Selected classes** (tick the classes). One save creates a schedule for each chosen class; if any chosen class already has that fee for the term, nothing is saved and those classes are named, so existing schedules are never overwritten. Add a schedule for each term where that fee applies. In **Main > Fee Invoices**, choose the session and use **All classes**/**All terms** or select one class and term to review balances. To create invoices, select one specific class and term; preview the enrolled students and fee totals, generate the draft batch, review it, then issue it. Students enrolled for only part of the term are flagged for review; the app does not prorate fees automatically.

After issuing the batch, filter the selected session/class/term by student name, admission number, or invoice number. The same filter narrows the fee-balance rows and receipt list; billed, paid, and due totals count issued invoices only, while draft and cancelled invoices remain labeled separately. Select an issued invoice and choose **Record payment**. Enter the amount, method, date, and optional reference. Partial payments are supported, overpayments are rejected, and a receipt can be exported as PDF. To correct a recorded payment, select its receipt and record a reasoned reversal; the original receipt remains in the ledger. Use **Main > Fee Reconciliation** for date-range receipt and reversal totals and read-only comparison with a statement CSV.

Select any invoice of a student and choose **Statement / lump sum** to see everything that student owes through the **active term** (set in Academic Calendar). Unpaid balances from earlier terms and sessions stay on the statement until they are paid, a fully paid term adds nothing, and invoices for terms after the active one are left out. Enter one amount and it is applied to the oldest unpaid invoices first under a single lump-sum receipt; overpayment is rejected, each allocation keeps its own immutable receipt that can be reversed with a reason, and the statement and the lump-sum receipt can be saved as PDF. Debts carry forward only for invoices that have been issued, so generate and issue each term's invoices after changing the active term. The rebuild does not yet support a whole-session annual fee schedule or fee-statistics tabs; amounts currently have no assumed currency label.

### Promoting a class roster

Open **Main > Enrollments > Promote class roster**. Select the source session/class, destination session/class, and destination enrollment date, then choose **Preview students**. Active students without any enrollment history in the destination session are selected by default; inactive students and students already enrolled in the destination session are shown as skipped. Uncheck any eligible student who should stay behind, then choose **Promote selected students** and confirm. The operation adds destination enrollment records atomically and keeps all source enrollment history. Run a separate promotion for each destination class; no grade-to-class mapping is guessed.

### Assigning subjects to classes

Add each subject in **Main > Subjects**, then create the academic session and set a Primary or Secondary stage for each class in Settings. Open **Settings > Class Subjects > Assign subjects**, choose the session and class, optionally choose an active teacher, and tick the subjects to add. That teacher is assigned to each newly selected subject for that class. Subject codes, names, and levels can be searched. Existing assignments are shown as checked and locked; this action adds subjects without changing existing assignments. Select an assignment in the list and choose **Edit selected** to assign or change its teacher individually.

### Setting school type, active session, and term

In **Settings > School Profile**, select **Primary**, **Secondary**, or **Primary and Secondary** under School type, then save the profile. Class-stage setup follows this selection; if existing classes have a conflicting stage, the screen flags them for correction without changing their records automatically. In **Settings > Academic Calendar > Sessions**, select a session and click **Set Selected Session as Active**. Open **Terms**, choose the session, select a term, and click **Set Selected Term as Active**. Only one academic session can be active; each session can have one active term. The active session and term are prioritized in academic workflow selectors.

## Building the Windows installer

Run `build.bat` (or `packaging\build_windows.ps1`) to build `installer\SmartSchoolManager-Setup-<version>.exe`
(PyInstaller + Inno Setup). It runs the tests first and never touches school data. See
[docs/PACKAGING.md](docs/PACKAGING.md) for requirements, releasing, upgrades and SmartScreen.

## Data location
The database is stored in the current user's local application data directory, not in the program installation folder:
`%LOCALAPPDATA%\SmartSchoolManagerRebuild\school.db`

This avoids requiring administrator permissions to write school records. Back up the database before testing with real school data.

### Open a restored database separately

The restore workflow saves to a separate new file. To open that restored copy without replacing the usual database:

1. Close SmartSchool Manager. In **Settings > Data Backup**, restore the verified backup to a new path such as `D:\SchoolRecovery\SmartSchoolManagerRebuild\school.db`. Create the destination folder first; the destination file must not already exist.
2. In PowerShell, set `SMARTSCHOOL_DATA_DIR` to the folder containing that `school.db` and launch the app from the same PowerShell window:

   ```powershell
   $env:SMARTSCHOOL_DATA_DIR = 'D:\SchoolRecovery\SmartSchoolManagerRebuild'
   if (-not (Test-Path (Join-Path $env:SMARTSCHOOL_DATA_DIR 'school.db') -PathType Leaf)) { throw 'Restored school.db was not found.' }
   .\.venv\Scripts\python.exe main.py
   ```

   For a packaged build, run its executable from that same PowerShell window instead of the Python command. The override applies to that launched process and is not saved as a Windows setting. If the override folder has no `school.db`, startup stops with an error instead of silently creating an empty school database.
3. Sign in and confirm the school profile, academic session, and sample record counts before continuing work. Close the app when finished.

To return to the usual database, start the app normally without `SMARTSCHOOL_DATA_DIR` set (for example, from a new PowerShell window). The usual database and the restored copy remain separate.

## Safety
- Keep the original `SmartSchool_Manager.exe` and `SmartSchool_Manager_Setup.exe` unchanged.
- Do not use real school data until backup/restore and security features have been tested.

## Navigation
The sidebar separates frequently used workflows into **Main** and setup or maintenance screens into **Settings**. Both lists scroll. Dashboard shortcuts switch to Settings automatically when needed.
