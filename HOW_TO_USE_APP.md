# SmartSchool Manager - How to Use the App

This guide covers the current offline-first desktop build. Menu names below match the app. Administrator accounts can manage setup and records; teacher accounts have a limited workspace for assigned grade entry and attendance.

## 1. Run the app

From the project folder, either double-click `app.bat` or open PowerShell in the folder and run:

```powershell
.\.venv\Scripts\python.exe main.py
```

On first launch, create the administrator account. There is no default username or password. The project requirements are listed in `requirements.txt`; the app is designed to run locally without internet once installed.

The normal school database is stored at:

```text
%LOCALAPPDATA%\SmartSchoolManagerRebuild\school.db
```

The program folder and school database are separate. Back up the database before testing, upgrading, or using real school records. `build.bat` builds the Windows installer; it is not needed to run the app from source.

## 2. Set up a school before entering records

Use the Dashboard setup guide if it is shown on a new school. The recommended setup order is:

1. **Settings > School Profile** - enter the school name and other optional details, choose **Primary**, **Secondary**, or **Primary and Secondary**, set the admission ID prefix, and add a logo or signatory details for reports if available.
2. **Settings > Academic Calendar** - create the academic session and its terms, enter dates if known, then explicitly set the active session and the active term within that session.
3. **Main > Classes** - add each class and its section. Section means the stream/group within that class (for example A, B, or Main); choose a suggestion or type the school's own label.
4. **Settings > Class Stages** - mark each class as Primary or Secondary. A class needs a stage before it can receive subject assignments.
5. **Main > Subjects** - add each subject once, with a unique short subject code and a clear name. Subject Level is an optional catalog label (Primary, Secondary, or Both); the class's explicit setting in Class Stages controls where the subject can actually be assigned.
6. **Settings > Class Subjects** - choose the session and class, then use **Assign subjects** to tick the subjects taught by that class. An active teacher can be selected for the newly assigned subjects. Existing assignments are left as they are; edit a selected assignment to change its teacher.
7. **Settings > Grade Bands** - choose the session and Primary/Secondary stage. You may add your school's own scale or use the editable sample A–F thresholds (A 75%, B 65%, C 50%, D 40%, F 0%) drawn from the supplied report card. Confirm those boundaries with the school before use; applying them does not lock them until scores are saved.
8. **Settings > Assessment Setup** - review or change the assessment components and maximum points for the class-subject and term. The default CA/Exam starter setup is **CA Score (40)** and **Exam Score (60)** when an administrator first opens an unconfigured class-subject in Grade Entry.
9. Register students and confirm their class/session enrollments.

### Start the CBT FastAPI server

An administrator can choose **Start CBT Server** on the Dashboard. The service listens on the school computer's network interfaces at port `8765`; students and the host must be on the same school Wi-Fi/LAN. Use a displayed `http://<school-computer-ip>:8765/health` address to confirm it responds. Stop it with **Stop CBT Server** or close the desktop app. This foundation currently serves only a health response: students cannot sign in or sit exams yet. Do not expose port 8765 to the public internet.

The selected active session and term are offered first in academic selectors, but check the selectors before entering results, attendance, or fees.

## 3. Register and manage students

### Register one student

1. Open **Main > Students** and choose **Register student**.
2. Enter the required first and last names. Gender, guardian contact, NIN, date of birth, email, and a student photo are optional.
3. Select the class/section and academic session, then save.

The app generates an admission ID using the prefix in **Settings > School Profile** when no ID is supplied. The student record and enrollment are created together. Changing the prefix affects newly generated IDs, not existing students.

### Import a class roster

1. In **Students**, choose **Excel template** or **CSV template**.
2. Fill the template. Required name columns are `first_name` and `last_name`; `admission_no` is optional. If an existing ID is supplied, the app preserves it; if it is blank, the app generates one. Optional fields include `gender`, `guardian_phone`, `nin`, `date_of_birth`, and `email`.
3. Choose **Import students (Excel/CSV)**, select the destination class, session, and enrollment date.
4. Review the validation preview. Correct any duplicate IDs or invalid rows before confirming registration.

Excel `.xlsx` and CSV are supported; old `.xls` files must be saved as `.xlsx`. Do not import the same students again into a database that already contains their IDs. The importer validates the roster before saving it as one operation.

### Student profile and enrollment changes

- Select a student in the register and open the profile to review enrollment history, approved results, attendance, and invoice balances.
- Use **Enrollments** for transfers, completion, withdrawal, or class-roster promotion. This retains the previous enrollment history.
- Archive or restore a student from the register when appropriate; archived records are retained.
- Student photos are optional PNG/JPEG images and can be used on report cards.

## 4. Assign subjects and teachers

Subjects are created once in **Main > Subjects**. To make a subject available for grade entry, assign it to the class in **Settings > Class Subjects** for the correct academic session. The Grade Entry subject list contains those assignments.

To assign a teacher, first add an active staff record under **Main > Teachers**. In **Settings > Class Subjects**, select the class-subject assignment and choose **Edit selected**, or use **Assign selected subjects to** when assigning several new subjects to one teacher. Then open **Settings > User Accounts** to create or link a teacher login to that active staff record. When the teacher signs in, Grade Entry's Class selector shows classes with at least one subject assigned to them for the selected session; the Subject selector shows only that teacher's assigned subjects in the selected class. Attendance is also limited to assigned classes. Administrators retain result approval and approved-result correction access.

## 5. Enter CA and exam scores

1. Confirm the students are enrolled in the selected session/class and that the subject is assigned to that class.
2. Open **Main > Grade Entry**.
3. Select **Session**, **Term**, **Class**, and **Subject**. The enrolled roster loads automatically.
4. Enter scores in the student rows, or choose **Export Excel template** and fill the component columns in Excel.
5. Choose **Import Scores** to select an `.xlsx` or CSV file. Review the preview before accepting it.
6. **Total** previews immediately as you type. Once all required CA/Exam scores are valid, **Grade** also updates live using `points earned ÷ total available points × 100` and the configured scale. Each component heading shows its maximum points. Choose **Save All Grades** only when you are ready to store the draft scores.
7. An administrator reviews the complete result set and approves it. Approved results are locked. An administrator can correct an approved result with a reason; the change is recorded in history.

The admission number matches each imported row to the selected roster; the student name is for reference. Use the headers exported for the selected subject and term (for example, `MTH_CA` and `MTH_EXAM`). The app checks component maximums. Blank score cells do not clear existing values. Do not type letter grades into the import file: the school-configured grade bands calculate them.

## 6. Generate report cards and result sheets

From **Grade Entry**, open the **Reports** actions. Available exports include an individual student report card, one class PDF with one student per page, a subject result sheet, and a class master sheet.

Report cards require complete approved results for the enrolled subjects. The class PDF also requires the same frozen class roster across those subject results. Cards can include CA/Exam component marks, subject totals and grades, subject positions, overall average, the configured class-position or average-grade result, attendance, psychomotor ratings, affective traits, teacher/principal remarks, and optional student photo. School identity, logo, term dates, and signatory details come from **Settings > School Profile**.

After results are approved, an administrator can select a student in **Grade Entry** and choose **Reports > Optional skills & remarks**. Psychomotor skills are observed abilities (Handwriting, Verbal Fluency, Games & Sports, Drawing & Painting, Musical Skills); affective traits describe conduct (Punctuality, Attendance, Neatness, Honesty, Politeness). Each field is optional and can be left as **Not rated**. Entered ratings are saved for that student and term and appear in individual and class report-card PDFs; if none are entered, the ratings section is omitted. Rating choices are Excellent, Very Good, Good, Fair, and Needs Improvement. These are observations, not marks, and do not affect grades or positions. Check the generated PDF before printing, especially the school name, grading scale, class/term, and signatures.

## 7. Record attendance

1. Open **Main > Attendance** and choose the session, class, and date.
2. Mark students **Present** or **Absent**, then save the register. An unmarked student remains distinct from an absent student.
3. If a saved mark needs correction, enter a reason; the app keeps the correction history.
4. Use **Attendance summary** to select a date range and review enrolled, present, absent, and not-marked counts.

Late and excused statuses are not currently available. Teachers can take attendance only for classes assigned to them.

## 8. Set up fees, invoices, and payments

1. In **Settings > Fee Setup**, define fee types and schedules for the right session and term. A schedule can apply to one class, all classes, or selected classes. Existing schedules are not overwritten by a duplicate save.
2. Open **Main > Fee Invoices**, select the session, then choose one class and term to create a batch. Review the preview, create draft invoices, and issue the batch when it is ready. Review any partial-term enrollment flags; the app does not automatically prorate fees.
3. Select an issued invoice and choose **Record payment**. Enter the amount, method, date, and optional reference. Partial payments are supported; overpayments are blocked. Export the receipt PDF if needed.
4. To correct a payment, select its receipt and record a reversal with a reason. The original transaction stays in the ledger.
5. Use **Transaction History** for date-range receipts/reversals, ledger export, or a read-only comparison with a statement CSV.
6. Use a student's **Statement / lump sum** action to review balances through the active term or allocate one payment to the oldest unpaid invoices first.

Fee amounts currently have no assumed currency label. Confirm the school's billing practices before issuing invoices. Statement comparison is a review aid and does not verify bank settlement.

### What the receipt PDF means

- **Receipt number** is the app's payment ID. Keep it for finding the transaction later.
- **Student and admission number** identify whose account was paid; **invoice number**, **class**, **session**, and **term** identify which billed record received the payment.
- **Received date**, **method**, and optional **reference** record when and how the school received the money.
- **Scheduled fee / amount** lists the fee items saved on that invoice. **Received on this receipt** is the amount paid in this transaction. **Total paid on invoice** includes earlier payments; **invoice balance when exported** is the remaining amount at the time the PDF was made.
- If a receipt was reversed, its PDF also shows the reversal date, amount, and reason. The original receipt remains in the ledger.
- A **lump-sum receipt** shows how one payment was allocated across invoices. Each row includes the term, class, fee, invoice, allocation receipt, amount allocated, amount reversed, and current invoice balance. The total at the bottom is the lump sum received.

The earlier receipt sample uses a clear one-page layout with student/registration details, session and term coverage, payment ID, date/method, fee allocations, balances, and total paid. The rebuild keeps those details and adds invoice-level allocation and reversal history. The sample prints `₦`; the rebuild currently omits currency symbols because a school-configurable currency setting has not been added. Review the currency display before using receipts operationally.

## 9. Timetable and communication tools

- **Main > Timetable** - choose a session and class, then add slots using assigned class-subjects, weekday, start/end times, and optional location. The app blocks class, teacher, and named-location overlaps. Export the session timetable to PDF.
- **Main > Communication Center** - administrators can prepare birthday greetings and fee reminders, edit templates, and manage the local outbox/history. Nothing is sent automatically over a network; the app opens the selected email/WhatsApp workflow or lets the operator copy the message and mark it sent.
- **Question Bank/CBT** is not part of this build.

## 10. Backups, passwords, and safe testing

- In **Settings > Data Backup**, create and verify a backup before significant work. Restore creates a separate database file and will not overwrite the active database.
- **Settings > Database Diagnostics** runs read-only integrity checks. **Settings > Audit Log** lets administrators review recorded actions.
- All users can change their own password from the account menu at the top right: **Change my password**. Administrators manage teacher accounts from **Settings > User Accounts**.
- Use a separate test database for practice. Do not test imports against a live school's database, and do not replace or overwrite an existing database to make a test work.
- The app is offline-first. The Communication Center's manual handoff to email/WhatsApp is not cloud synchronization.

## 11. What the form fields mean

### Main > Classes: **+ Add record**

This creates a class/section label. The first two fields are needed:

| Field | What to enter |
|---|---|
| Class Name | The grade/class, for example `Primary 5`, `JSS 1`, or `SS 2`. |
| Section | The stream or section, for example `A`, `B`, or `Blue`. If the school has one group, enter a consistent value such as `Main`; do not leave it blank. Class Name plus Section identifies the class. |
| Class Teacher | Optional name/reference only. It does not link a staff record, grant access, or set the report-card signatory. Assign teachers under **Settings > Class Subjects**; report signatories are set in **Settings > School Profile**. |

After saving, open **Settings > Class Stages**, select the class row, choose **Primary** or **Secondary**, and save it. The app does not guess the stage from a name like “JSS 1”.

### Main > Subjects: **+ Add record**

| Field | What to enter |
|---|---|
| Subject Code | A short, unique code, such as `MTH`, `ENG`, or `BSCI`. It appears on grade exports and is part of the score-import headers. Keep it stable. |
| Subject Name | The full name pupils and staff recognize, such as `Mathematics` or `Basic Science`. |
| School Level | Optional catalog label such as `Primary` or `Secondary`, shown in subject search. This is a text field; it does not set the class's stage or restrict assignment. Set each class's actual stage separately in **Class Stages**. |

Adding a subject only creates it in the subject catalog. To make it available in grade entry, assign it to each class and session in **Settings > Class Subjects**.

### Main > Teachers: **+ Add record**

| Field | What it means |
|---|---|
| Staff Number | The school's unique staff identifier. |
| First Name / Last Name | The teacher's names. |
| Phone Number / Email | Optional contact details. |
| Staff Status | Enter `Active` for a teacher who should appear in assignment choices; use `Inactive` when they should not be assigned new classes. This is a text field, so use those values exactly. |

This creates a staff record only. A login is created separately under **Settings > User Accounts**, and a teacher must be assigned to class-subjects before their account can access those classes.

### Settings > Academic Calendar: session and term forms

- **Session name**: the school's label for the academic year/session, such as `2026/2027`.
- **Start date / End date**: the full date range of the session. Terms must fit inside it.
- **Current academic session**: mark this only for the session the school is working in now. The app keeps one current session.
- **Academic session** on the term form: which session owns the term.
- **Term name**: e.g. `First Term`, `Second Term`, or `Third Term`.
- **Order**: the term's sequence in that session (1, 2, 3…). This controls the term ordering; it is not a score or position.
- **Term Start / End date**: the term's dates, within the selected session.

After creating a session or term, use the Academic Calendar's **Set Selected Session as Active** or **Set Selected Term as Active** action when that is the current period.

### Settings > Class Subjects: assign subjects

In **Assign subjects**, choose the **Academic session** and **Class and section** first. Tick every subject that class takes. Use search to find a subject in a long list. **Select visible** and **Clear visible** affect the currently displayed search results. **Assign selected subjects to** optionally applies one active teacher to newly selected subjects. Already assigned subjects are locked and are not changed by this bulk action; use **Edit selected** to change one assignment. This is why a subject created under **Main > Subjects** may not appear in Grade Entry until it is assigned here.

### Settings > Grade Bands: add threshold

- **Academic session** and **School stage** choose which grading policy you are editing.
- **Minimum percentage** is where a grade band begins. The first band must begin at `0`; each next threshold begins a new band, and the last one runs through 100.
- **Grade label** is the value printed, such as `A`, `B`, `C`, `D`, or `F`.
- **Remark** is the text associated with that grade, such as `Excellent` or `Pass`.

Use the school's approved boundaries and remarks. No scale is applied automatically; administrators can choose the sample A–F starting scale on the Grade Bands page and edit its thresholds before entering scores. Once score drafts have captured a policy, the scale may be locked for that session/stage to keep results consistent.

### Settings > Assessment Setup: add component

- **Academic session**, **Class and subject**, and **Term** select exactly which results the component belongs to.
- **Component name** is the score column, for example `CA Score` or `Exam Score`.
- **Order** controls the left-to-right order of score columns.
- **Maximum points** is the highest score a student can receive in that component, for example 40 for CA or 60 for an exam.

The component form sets maximum points; it does not set weighting or grade bands. Enter the same maxima the school uses before recording marks. The sum of earned points is compared with the sum of maximum points to calculate the percentage.

### Main > Students: Register student

- **First name / Last name**: enter the student's given name(s) and family name in the matching fields.
- **Gender**: optional; choose Female, Male, or Other if known.
- **Guardian phone**, **NIN**, and **Email**: optional contact/identity details. Only enter verified information.
- **Date of birth**: tick **Enter date of birth** to enable the date picker; otherwise it is left unset.
- **Class and section / Academic session**: the student's initial placement. The app creates the enrollment with the student record.
- **Enrollment date**: the date the student joined that class in that session; the date is limited to the session range.
- **Student photo**: optional PNG/JPEG; use a clear, appropriate image if it should print on reports.
- **Admission ID**: generated on save from the prefix in School Profile. For imports, a supplied admission number is preserved.

### Enrollments: enroll or move a student

For **Enroll student**, type at least two characters of the student's name or admission number, choose the matching student, then choose the class/section, session, and enrollment date. Use this when adding a placement to an existing student; do not create a second student record.

For **Transfer student**, **New class and section** is the destination in the same session and **Transfer date** is when the new placement begins. For **Promote class roster**, choose both the source and destination session/class, set the destination enrollment date, then preview and select eligible students before promoting. The source history is retained.

### Main > Grade Entry: selectors and score cells

- **Session / Term / Class / Subject**: these four selectors define the result set. Subject options come from **Class Subjects** for the chosen session and class.
- **CA / Exam score cells**: enter marks earned, not percentages or grade letters. Stay within each column's displayed maximum.
- **Import Scores**: opens a file picker; the preview shows the matched student rows before values are applied to the table. **Save All Grades** is a separate action that saves them as drafts.
- **More > Approve and lock results**: an administrator uses this only after reviewing a complete result set.
- **More > Correct approved results**: an administrator enters a reason before changing a locked mark.
- **Reports**: exports the selected student's card, all class cards, or class result sheets. Choose a student row before exporting one student's card.

### Main > Attendance and Timetable

- In **Attendance**, **Academic session**, **Class and section**, and **Attendance date** select the register. Set each student's mark to Present or Absent; blank means not marked, not absent. A correction to a saved mark asks for a reason.
- In **Timetable**, **Class and subject** selects an existing assignment, **Day** selects the weekday, **Start time / End time** use 24-hour `HH:MM`, and **Room or location** is optional. The app blocks overlaps for the class, assigned teacher, and matching location.

### Settings > Fee Setup and Main > Fee Invoices

- **Fee type name** names the charge (e.g. Tuition); **Optional description** explains it.
- In a **Class fee schedule**, **Academic session**, **Term**, and **Fee type** specify when and why the fee applies. **Applies to** chooses one class, all classes, or selected classes. **Amount** is the school's fee amount; **Set due date** is optional. Adding a schedule does not issue invoices or record payments.
- In **Record student payment**, the top of the form identifies the student, invoice, and remaining balance. **Payment amount** is the amount received now, **Payment method** is Cash/Bank transfer/Mobile money/Cheque/Other, **Payment date** is when it was received, and **Reference** is an optional receipt or transaction identifier.
- In **Student statement / lump sum**, the amount is allocated to the student's oldest unpaid issued invoices first. Check the preview and active term before saving.

### Other dialogs

- **Create teacher login**: choose the existing active staff record, set a username and password, then confirm the password. Use a password of at least 10 characters. The teacher login must be linked to the staff record assigned to their subjects.
- **Change my password**: open the account menu at the top right; enter the current password, then the new password twice.
- **Communication Center templates/outbox**: templates control the message text; the recipient/outbox steps prepare a local message for manual handoff. The app does not send it automatically.

## 12. Using the supplied JSS 1 import example

The files are in `outputs/jss1-report-import-2026-10-27/`. Import `Students_to_Import.xlsx` first, then import the corresponding subject workbook from Grade Entry. Read the **Read Me** sheet in the files and create the listed subject codes and CA/Exam maxima before importing scores. Use a separate test database and review the student preview before confirming; these are records transcribed from a school report, not synthetic students.

The source report contains 48 student pages while its positions are out of 55. One Business Studies mark is missing and remains blank in the import. Some printed totals differ by 0.5 from the CA and exam values; the rebuild calculates totals and grades from the component marks and configured grade bands.

## Troubleshooting

| What you see | Check |
|---|---|
| No students appear in Grade Entry | Confirm the session, class, and term, then check each student's enrollment and the subject's class assignment. |
| No class or subject is selectable | Create the session/class, set the class stage, add subjects, and assign them under **Settings > Class Subjects**. |
| An import is rejected | Export a fresh template for the selected class/subject/term. Check exact headers, admission numbers, duplicate rows, score maxima, and `.xlsx`/CSV format. |
| Report-card export is unavailable | Confirm all assigned subjects have complete approved results and the class roster matches across subjects. |
| A payment can't be recorded | Confirm the invoice batch was issued, the amount is positive and within the remaining balance, and the invoice belongs to the selected session. |
| The app opens an empty school when expecting a restored copy | Close the app and use the explicit `SMARTSCHOOL_DATA_DIR` instructions in the README to launch the restored database separately. |
