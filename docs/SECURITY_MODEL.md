# Security model

## Assets and trust boundaries

The local SQLite database contains account credentials, student/staff records, and future financial and academic data. The application runs under the Windows user's account. The OS account and filesystem ACL are the primary local boundary; SQLite application roles do not defend against someone who can directly edit the DB or run arbitrary code as that Windows user.

## Current controls

- Passwords are salted PBKDF2-HMAC-SHA256 hashes and compared with a constant-time operation. The parser bounds accepted work factors and hash sizes.
- Login failures cause a persisted progressive delay capped at five minutes. This is a local throttling measure, not a defense against direct DB edits or multiple OS accounts.
- Most record and setup mutations require an active administrator. Schema version 18 adds an optional unique link from a teacher login to an active staff record; legacy teacher logins remain unlinked and receive no grade-entry scope until explicitly linked.
- Teacher logins see a dedicated workspace and can load or save draft scores only for class-subject assignments linked to their staff record. They can also read and record attendance for a class when they have a class-subject assignment in the selected academic session; corrections require a reason and append to attendance history. The database rechecks the active account, active staff link, and assignment on scoped reads and writes. Teachers cannot approve or correct approved results; administrator approval and reasoned score-correction rules remain in force. Other school-management workflows remain unavailable to teacher logins.
- Teacher account creation, linking, password reset, and activation changes are administrator-only and audited transactionally. The last active administrator cannot be deactivated.
- Data values use SQLite parameters; dynamic identifiers come from a fixed internal module map.
- SQLite foreign-key enforcement and a bounded busy timeout are enabled and verified per connection.
- An integrity-checked copy is made before every schema upgrade of an existing database.
- Rotating logs are stored under the app-specific local-data folder. Logs must not contain passwords or student record contents.

## Threats not yet addressed

- A local administrator or process running as the school user can read or alter the DB, replace application code, or bypass UI checks.
- Login throttling can be reset by editing the local database; multi-process coordination is basic.
- Audit records are not tamper-evident. Academic session/term, enrollment, and generic CRUD changes write their audit entries in the same transaction. Login events are logged separately from authentication state.
- Database backups contain the same sensitive school and account data as the live database. The backup workflow checks SQLite integrity, foreign-key references, and migration history, but does not encrypt backups; administrators should store them in a restricted location.
- An explicit `SMARTSCHOOL_DATA_DIR` environment setting can point one launched process at a separately restored database directory. The override must be absolute and startup stops if `school.db` is missing; it is not persisted as a Windows setting.
- Legacy `students.class_name` values are never auto-mapped. An administrator can explicitly preview and confirm one exact legacy value for a chosen class/section, session, and enrollment date; inactive students are excluded and any active-enrollment conflict blocks the whole group. The source text remains unchanged, and the generated enrollments plus mapping audit event commit atomically.
- No encryption-at-rest/key-management design, secret store, session expiry, or account recovery workflow exists.
- CSV exports prefix spreadsheet formula-leading values to reduce formula-injection risk. Exports still contain sensitive school data and must be stored carefully.
- Import handling, backup confidentiality, native restore operations, and file permission behavior need dedicated review. Synthetic testing confirms restore to a separate file; the active school database is never replaced by that workflow.

## Operational rules

Use temporary databases and synthetic identities in tests. Never point automated tests at `%LOCALAPPDATA%` school data. Back up and verify before data migrations/imports. Never alter the legacy installation, installer, or production database. Treat backups and exported reports as sensitive school records.
