"""SQLite persistence and additive schema migrations for SmartSchool Manager."""

from __future__ import annotations

import csv
import json
import logging
import os
import shutil
import secrets
import sqlite3
from contextlib import closing, contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from fractions import Fraction
from pathlib import Path
from typing import Iterable
from .student_details import normalize_optional_student_details

def configured_data_dir() -> Path:
    """Return the optional explicit app-data override or the Windows user-local default."""
    override = os.environ.get("SMARTSCHOOL_DATA_DIR", "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ValueError("SMARTSCHOOL_DATA_DIR must be an absolute folder path.")
        return path
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SmartSchoolManagerRebuild"


APP_DIR = configured_data_dir()
DB_PATH = APP_DIR / "school.db"
SCHEMA_VERSION = 27
# Bound foreground-worker waits when another process holds a long SQLite lock.
SQLITE_BUSY_TIMEOUT_SECONDS = 2
SQLITE_BUSY_TIMEOUT_MS = SQLITE_BUSY_TIMEOUT_SECONDS * 1000
logger = logging.getLogger(__name__)

MODULES = {
    "Students": {"table": "students", "fields": [("admission_no", "Admission Number"), ("first_name", "First Name"), ("last_name", "Last Name"), ("gender", "Gender"), ("class_name", "Class"), ("guardian_phone", "Guardian Phone Number"), ("nin", "NIN"), ("date_of_birth", "Date of Birth"), ("email", "Email")]},
    "Teachers": {"table": "teachers", "fields": [("staff_no", "Staff Number"), ("first_name", "First Name"), ("last_name", "Last Name"), ("phone", "Phone Number"), ("email", "Email"), ("status", "Staff Status")]},
    "Classes": {"table": "classes", "fields": [("class_name", "Class Name"), ("section", "Section"), ("class_teacher", "Class Teacher")]},
    "Subjects": {"table": "subjects", "fields": [("subject_code", "Subject Code"), ("subject_name", "Subject Name"), ("level", "School Level")]},
}

EDUCATIONAL_STAGES = {"primary": "Primary", "secondary": "Secondary"}


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        conn.close()
        raise sqlite3.DatabaseError("SQLite foreign-key enforcement could not be enabled")
    return conn


def validate_configured_data_dir() -> None:
    """Prevent a mistyped explicit data folder from silently creating an empty school DB."""
    if os.environ.get("SMARTSCHOOL_DATA_DIR", "").strip() and not DB_PATH.is_file():
        raise FileNotFoundError(
            "SMARTSCHOOL_DATA_DIR is set, but no school.db exists in that folder. "
            "Check the restored database path before starting the app."
        )


@contextmanager
def connection():
    """Commit on success, roll back on failure, and always close the connection."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _backup_existing_database(version: int) -> Path | None:
    """Back up an existing database and verify its integrity before migration."""
    if not DB_PATH.exists() or DB_PATH.stat().st_size == 0:
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = DB_PATH.with_name(f"{DB_PATH.stem}.pre-migration-v{version}-{stamp}{DB_PATH.suffix}")
    try:
        shutil.copy2(DB_PATH, backup)
        with closing(sqlite3.connect(backup)) as check:
            with check:
                result = check.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise sqlite3.DatabaseError(f"Pre-migration backup failed integrity check: {result}")
    except Exception:
        backup.unlink(missing_ok=True)
        raise
    logger.info("Created verified pre-migration backup at %s", backup)
    return backup


def _migration_1(conn: sqlite3.Connection) -> None:
    migration_sql = """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('admin','principal','teacher')),
        is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        failed_login_count INTEGER NOT NULL DEFAULT 0,
        locked_until TEXT
    );
    CREATE TABLE IF NOT EXISTS students (
        id INTEGER PRIMARY KEY AUTOINCREMENT, admission_no TEXT NOT NULL UNIQUE,
        first_name TEXT NOT NULL, last_name TEXT NOT NULL, gender TEXT NOT NULL DEFAULT '',
        class_name TEXT NOT NULL DEFAULT '', guardian_phone TEXT NOT NULL DEFAULT '',
        is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS teachers (
        id INTEGER PRIMARY KEY AUTOINCREMENT, staff_no TEXT NOT NULL UNIQUE,
        first_name TEXT NOT NULL, last_name TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'Active',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS classes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, class_name TEXT NOT NULL,
        section TEXT NOT NULL DEFAULT '', class_teacher TEXT NOT NULL DEFAULT '',
        UNIQUE(class_name, section)
    );
    CREATE TABLE IF NOT EXISTS subjects (
        id INTEGER PRIMARY KEY AUTOINCREMENT, subject_code TEXT NOT NULL UNIQUE,
        subject_name TEXT NOT NULL, level TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL, action TEXT NOT NULL,
        entity TEXT NOT NULL, entity_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS login_attempts (
        username_key TEXT PRIMARY KEY, failure_count INTEGER NOT NULL DEFAULT 0,
        locked_until TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_students_active_name ON students(is_active,last_name,first_name);
    CREATE INDEX IF NOT EXISTS idx_teachers_status_name ON teachers(status,last_name,first_name);
    CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_log(created_at);
    """
    # sqlite3.executescript() issues an implicit COMMIT in legacy transaction
    # mode, so execute this static, semicolon-delimited migration within the
    # explicit transaction opened by initialize().
    for statement in migration_sql.split(";"):
        if statement.strip():
            conn.execute(statement)
    # Upgrade databases created by the initial foundation without discarding data.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "failed_login_count" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN failed_login_count INTEGER NOT NULL DEFAULT 0")
    if "locked_until" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN locked_until TEXT")
    conn.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(1)")
    conn.execute("PRAGMA user_version = 1")


def _migration_2(conn: sqlite3.Connection) -> None:
    """Add academic sessions and terms without altering existing records."""
    statements = (
        """CREATE TABLE academic_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE CHECK(length(trim(name)) BETWEEN 1 AND 100),
            starts_on TEXT NOT NULL,
            ends_on TEXT NOT NULL,
            is_current INTEGER NOT NULL DEFAULT 0 CHECK(is_current IN (0,1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(starts_on < ends_on)
        )""",
        "CREATE UNIQUE INDEX idx_academic_sessions_one_current ON academic_sessions(is_current) WHERE is_current=1",
        """CREATE TABLE terms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            academic_session_id INTEGER NOT NULL REFERENCES academic_sessions(id) ON DELETE RESTRICT,
            term_name TEXT NOT NULL CHECK(length(trim(term_name)) BETWEEN 1 AND 60),
            term_order INTEGER NOT NULL CHECK(term_order > 0),
            starts_on TEXT NOT NULL,
            ends_on TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(starts_on < ends_on),
            UNIQUE(academic_session_id, term_name),
            UNIQUE(academic_session_id, term_order)
        )""",
        "CREATE INDEX idx_terms_session_order ON terms(academic_session_id, term_order)",
        "INSERT INTO schema_migrations(version) VALUES(2)",
        "PRAGMA user_version = 2",
    )
    for statement in statements:
        conn.execute(statement)


def _migration_3(conn: sqlite3.Connection) -> None:
    """Add explicit student enrollment history; leave legacy class text intact."""
    statements = (
        """CREATE TABLE student_enrollments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE RESTRICT,
            class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE RESTRICT,
            academic_session_id INTEGER NOT NULL REFERENCES academic_sessions(id) ON DELETE RESTRICT,
            status TEXT NOT NULL CHECK(status IN ('active','completed','withdrawn','transferred')),
            enrolled_on TEXT NOT NULL,
            left_on TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(enrolled_on <= coalesce(left_on,'9999-12-31')),
            CHECK((status='active' AND left_on IS NULL) OR (status<>'active' AND left_on IS NOT NULL))
        )""",
        "CREATE UNIQUE INDEX idx_student_enrollments_one_active ON student_enrollments(student_id,academic_session_id) WHERE status='active'",
        "CREATE INDEX idx_student_enrollments_session_class ON student_enrollments(academic_session_id,class_id,status)",
        "CREATE INDEX idx_student_enrollments_student_history ON student_enrollments(student_id,academic_session_id,enrolled_on)",
        "INSERT INTO schema_migrations(version) VALUES(3)",
        "PRAGMA user_version = 3",
    )
    for statement in statements:
        conn.execute(statement)


def _migration_4(conn: sqlite3.Connection) -> None:
    """Add session-specific class, subject and optional teacher assignments."""
    statements = (
        """CREATE TABLE class_subject_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            academic_session_id INTEGER NOT NULL REFERENCES academic_sessions(id) ON DELETE RESTRICT,
            class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE RESTRICT,
            subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE RESTRICT,
            teacher_id INTEGER REFERENCES teachers(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(academic_session_id,class_id,subject_id)
        )""",
        "CREATE INDEX idx_class_subject_assignments_session ON class_subject_assignments(academic_session_id,class_id)",
        "INSERT INTO schema_migrations(version) VALUES(4)",
        "PRAGMA user_version = 4",
    )
    for statement in statements:
        conn.execute(statement)


def _migration_5(conn: sqlite3.Connection) -> None:
    """Add term-scoped assessment component definitions without score records."""
    statements = (
        """CREATE TABLE assessment_components (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_subject_assignment_id INTEGER NOT NULL REFERENCES class_subject_assignments(id) ON DELETE RESTRICT,
            term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE RESTRICT,
            component_name TEXT NOT NULL CHECK(length(trim(component_name)) BETWEEN 1 AND 80),
            component_order INTEGER NOT NULL CHECK(component_order BETWEEN 1 AND 99),
            max_score TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(class_subject_assignment_id,term_id,component_name COLLATE NOCASE),
            UNIQUE(class_subject_assignment_id,term_id,component_order)
        )""",
        "CREATE INDEX idx_assessment_components_term ON assessment_components(term_id,class_subject_assignment_id,component_order)",
        "INSERT INTO schema_migrations(version) VALUES(5)",
        "PRAGMA user_version = 5",
    )
    for statement in statements:
        conn.execute(statement)


def _migration_6(conn: sqlite3.Connection) -> None:
    """Add explicit primary/secondary stage to classes; do not infer legacy values."""
    conn.execute(
        "ALTER TABLE classes ADD COLUMN educational_stage TEXT NOT NULL DEFAULT '' "
        "CHECK(educational_stage IN ('','primary','secondary'))"
    )
    conn.execute("INSERT INTO schema_migrations(version) VALUES(6)")
    conn.execute("PRAGMA user_version = 6")


def _migration_7(conn: sqlite3.Connection) -> None:
    """Add configurable grade-band thresholds per academic session and school stage."""
    conn.execute(
        """CREATE TABLE assessment_grade_bands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            academic_session_id INTEGER NOT NULL REFERENCES academic_sessions(id) ON DELETE RESTRICT,
            educational_stage TEXT NOT NULL CHECK(educational_stage IN ('primary','secondary')),
            threshold TEXT NOT NULL,
            grade_label TEXT NOT NULL CHECK(length(trim(grade_label)) BETWEEN 1 AND 30),
            remark TEXT NOT NULL DEFAULT '' CHECK(length(remark) <= 120),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(academic_session_id,educational_stage,threshold),
            UNIQUE(academic_session_id,educational_stage,grade_label COLLATE NOCASE)
        )"""
    )
    conn.execute(
        "CREATE INDEX idx_assessment_grade_bands_scope ON "
        "assessment_grade_bands(academic_session_id,educational_stage)"
    )
    conn.execute("INSERT INTO schema_migrations(version) VALUES(7)")
    conn.execute("PRAGMA user_version = 7")


def _migration_8(conn: sqlite3.Connection) -> None:
    """Add explicitly marked attendance registers and append-only mark history."""
    statements = (
        """CREATE TABLE attendance_registers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            academic_session_id INTEGER NOT NULL REFERENCES academic_sessions(id) ON DELETE RESTRICT,
            class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE RESTRICT,
            attendance_date TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_by TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(academic_session_id,class_id,attendance_date)
        )""",
        """CREATE TABLE attendance_marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            register_id INTEGER NOT NULL REFERENCES attendance_registers(id) ON DELETE RESTRICT,
            enrollment_id INTEGER NOT NULL REFERENCES student_enrollments(id) ON DELETE RESTRICT,
            status TEXT NOT NULL CHECK(status IN ('present','absent')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(register_id,enrollment_id)
        )""",
        """CREATE TABLE attendance_mark_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            register_id INTEGER NOT NULL REFERENCES attendance_registers(id) ON DELETE RESTRICT,
            enrollment_id INTEGER NOT NULL REFERENCES student_enrollments(id) ON DELETE RESTRICT,
            previous_status TEXT CHECK(previous_status IS NULL OR previous_status IN ('present','absent')),
            new_status TEXT NOT NULL CHECK(new_status IN ('present','absent')),
            reason TEXT NOT NULL DEFAULT '' CHECK(length(reason) <= 300),
            actor TEXT NOT NULL,
            changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX idx_attendance_registers_date ON attendance_registers(academic_session_id,attendance_date,class_id)",
        "CREATE INDEX idx_attendance_marks_register ON attendance_marks(register_id,status)",
        "CREATE INDEX idx_attendance_history_register ON attendance_mark_history(register_id,enrollment_id,changed_at)",
        "CREATE TRIGGER attendance_history_no_update BEFORE UPDATE ON attendance_mark_history BEGIN SELECT RAISE(ABORT,'attendance history is append-only'); END",
        "CREATE TRIGGER attendance_history_no_delete BEFORE DELETE ON attendance_mark_history BEGIN SELECT RAISE(ABORT,'attendance history is append-only'); END",
        "INSERT INTO schema_migrations(version) VALUES(8)",
        "PRAGMA user_version = 8",
    )
    for statement in statements:
        conn.execute(statement)


def _migration_9(conn: sqlite3.Connection) -> None:
    """Add session-scoped timetable slots for existing class-subject assignments."""
    statements = (
        """CREATE TABLE timetable_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_subject_assignment_id INTEGER NOT NULL REFERENCES class_subject_assignments(id) ON DELETE RESTRICT,
            day_of_week INTEGER NOT NULL CHECK(day_of_week BETWEEN 1 AND 7),
            starts_at TEXT NOT NULL,
            ends_at TEXT NOT NULL,
            location TEXT NOT NULL DEFAULT '' CHECK(length(location) <= 100),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_by TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK(starts_at < ends_at)
        )""",
        "CREATE INDEX idx_timetable_entries_day ON timetable_entries(day_of_week,starts_at,ends_at)",
        "CREATE INDEX idx_timetable_entries_assignment ON timetable_entries(class_subject_assignment_id,day_of_week)",
        "INSERT INTO schema_migrations(version) VALUES(9)",
        "PRAGMA user_version = 9",
    )
    for statement in statements:
        conn.execute(statement)


def _migration_10(conn: sqlite3.Connection) -> None:
    """Add immutable assessment policy snapshots and editable draft score history."""
    statements = (
        """CREATE TABLE assessment_policy_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_subject_assignment_id INTEGER NOT NULL REFERENCES class_subject_assignments(id) ON DELETE RESTRICT,
            term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE RESTRICT,
            academic_session_id INTEGER NOT NULL REFERENCES academic_sessions(id) ON DELETE RESTRICT,
            educational_stage TEXT NOT NULL CHECK(educational_stage IN ('primary','secondary')),
            policy_json TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(class_subject_assignment_id,term_id)
        )""",
        """CREATE TABLE assessment_scores (
            snapshot_id INTEGER NOT NULL REFERENCES assessment_policy_snapshots(id) ON DELETE RESTRICT,
            enrollment_id INTEGER NOT NULL REFERENCES student_enrollments(id) ON DELETE RESTRICT,
            component_id INTEGER NOT NULL REFERENCES assessment_components(id) ON DELETE RESTRICT,
            score_text TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(snapshot_id,enrollment_id,component_id)
        )""",
        """CREATE TABLE assessment_score_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL REFERENCES assessment_policy_snapshots(id) ON DELETE RESTRICT,
            enrollment_id INTEGER NOT NULL REFERENCES student_enrollments(id) ON DELETE RESTRICT,
            component_id INTEGER NOT NULL REFERENCES assessment_components(id) ON DELETE RESTRICT,
            previous_score TEXT,
            new_score TEXT,
            actor TEXT NOT NULL,
            changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX idx_assessment_scores_enrollment ON assessment_scores(enrollment_id,snapshot_id)",
        "CREATE INDEX idx_assessment_snapshots_scope ON assessment_policy_snapshots(academic_session_id,educational_stage)",
        "CREATE TRIGGER assessment_policy_snapshot_no_update BEFORE UPDATE ON assessment_policy_snapshots BEGIN SELECT RAISE(ABORT,'assessment policy snapshots are immutable'); END",
        "CREATE TRIGGER assessment_policy_snapshot_no_delete BEFORE DELETE ON assessment_policy_snapshots BEGIN SELECT RAISE(ABORT,'assessment policy snapshots are immutable'); END",
        "CREATE TRIGGER assessment_score_history_no_update BEFORE UPDATE ON assessment_score_history BEGIN SELECT RAISE(ABORT,'assessment score history is append-only'); END",
        "CREATE TRIGGER assessment_score_history_no_delete BEFORE DELETE ON assessment_score_history BEGIN SELECT RAISE(ABORT,'assessment score history is append-only'); END",
        "INSERT INTO schema_migrations(version) VALUES(10)",
        "PRAGMA user_version = 10",
    )
    for statement in statements:
        conn.execute(statement)


def _migration_11(conn: sqlite3.Connection) -> None:
    """Add result approval locks and reasoned post-approval score corrections."""
    conn.execute(
        "ALTER TABLE assessment_score_history ADD COLUMN reason TEXT NOT NULL DEFAULT '' CHECK(length(reason)<=300)"
    )
    conn.execute(
        """CREATE TABLE assessment_result_approvals (
            snapshot_id INTEGER PRIMARY KEY REFERENCES assessment_policy_snapshots(id) ON DELETE RESTRICT,
            approved_by TEXT NOT NULL,
            approved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        "CREATE TRIGGER assessment_result_approval_no_update BEFORE UPDATE ON assessment_result_approvals "
        "BEGIN SELECT RAISE(ABORT,'approved assessment results are locked'); END"
    )
    conn.execute(
        "CREATE TRIGGER assessment_result_approval_no_delete BEFORE DELETE ON assessment_result_approvals "
        "BEGIN SELECT RAISE(ABORT,'approved assessment results are locked'); END"
    )
    conn.execute("INSERT INTO schema_migrations(version) VALUES(11)")
    conn.execute("PRAGMA user_version = 11")


def _migration_12(conn: sqlite3.Connection) -> None:
    """Add administrator-configured school identity and report signatories."""
    conn.execute(
        """CREATE TABLE school_profile (
            id INTEGER PRIMARY KEY CHECK(id=1),
            school_name TEXT NOT NULL DEFAULT '' CHECK(length(school_name)<=120),
            address TEXT NOT NULL DEFAULT '' CHECK(length(address)<=200),
            phone TEXT NOT NULL DEFAULT '' CHECK(length(phone)<=60),
            email TEXT NOT NULL DEFAULT '' CHECK(length(email)<=120),
            head_name TEXT NOT NULL DEFAULT '' CHECK(length(head_name)<=120),
            head_title TEXT NOT NULL DEFAULT '' CHECK(length(head_title)<=80),
            class_teacher_name TEXT NOT NULL DEFAULT '' CHECK(length(class_teacher_name)<=120),
            class_teacher_title TEXT NOT NULL DEFAULT '' CHECK(length(class_teacher_title)<=80),
            updated_by TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute("INSERT INTO school_profile(id) VALUES(1)")
    conn.execute("INSERT INTO schema_migrations(version) VALUES(12)")
    conn.execute("PRAGMA user_version = 12")


def _migration_13(conn: sqlite3.Connection) -> None:
    """Store an optional school logo inside the offline database and backups."""
    conn.execute("ALTER TABLE school_profile ADD COLUMN logo_data BLOB")
    conn.execute("INSERT INTO schema_migrations(version) VALUES(13)")
    conn.execute("PRAGMA user_version = 13")


def _migration_14(conn: sqlite3.Connection) -> None:
    """Add configurable fee types and class/session/term schedules."""
    statements = (
        """CREATE TABLE fee_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE
                CHECK(length(trim(name)) BETWEEN 1 AND 80),
            description TEXT NOT NULL DEFAULT '' CHECK(length(description)<=240),
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE fee_schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            academic_session_id INTEGER NOT NULL REFERENCES academic_sessions(id) ON DELETE RESTRICT,
            class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE RESTRICT,
            term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE RESTRICT,
            fee_type_id INTEGER NOT NULL REFERENCES fee_types(id) ON DELETE RESTRICT,
            amount_text TEXT NOT NULL CHECK(length(amount_text) BETWEEN 1 AND 32),
            due_on TEXT CHECK(due_on IS NULL OR length(due_on)=10),
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
            updated_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(academic_session_id,class_id,term_id,fee_type_id)
        )""",
        "CREATE INDEX idx_fee_schedules_scope ON fee_schedules(academic_session_id,class_id,term_id,is_active)",
        "INSERT INTO schema_migrations(version) VALUES(14)",
        "PRAGMA user_version = 14",
    )
    for statement in statements:
        conn.execute(statement)


def _migration_15(conn: sqlite3.Connection) -> None:
    """Add draft fee invoice batches with immutable fee and roster snapshots."""
    statements = (
        """CREATE TABLE fee_invoice_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            academic_session_id INTEGER NOT NULL REFERENCES academic_sessions(id) ON DELETE RESTRICT,
            class_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE RESTRICT,
            term_id INTEGER NOT NULL REFERENCES terms(id) ON DELETE RESTRICT,
            status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','cancelled')),
            generated_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            cancelled_by TEXT,
            cancelled_at TEXT,
            cancel_reason TEXT NOT NULL DEFAULT '' CHECK(length(cancel_reason)<=300),
            UNIQUE(id,academic_session_id,class_id,term_id)
        )""",
        "CREATE UNIQUE INDEX idx_fee_invoice_batches_one_draft ON fee_invoice_batches(academic_session_id,class_id,term_id) WHERE status='draft'",
        """CREATE TABLE fee_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL REFERENCES fee_invoice_batches(id) ON DELETE RESTRICT,
            enrollment_id INTEGER NOT NULL REFERENCES student_enrollments(id) ON DELETE RESTRICT,
            invoice_number TEXT NOT NULL UNIQUE CHECK(length(invoice_number)<=40),
            admission_no_snapshot TEXT NOT NULL,
            student_name_snapshot TEXT NOT NULL,
            enrolled_on_snapshot TEXT NOT NULL,
            left_on_snapshot TEXT,
            total_amount_text TEXT NOT NULL CHECK(length(total_amount_text) BETWEEN 1 AND 32),
            review_required INTEGER NOT NULL CHECK(review_required IN (0,1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(batch_id,enrollment_id)
        )""",
        """CREATE TABLE fee_invoice_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL REFERENCES fee_invoices(id) ON DELETE RESTRICT,
            fee_schedule_id INTEGER NOT NULL REFERENCES fee_schedules(id) ON DELETE RESTRICT,
            fee_type_name_snapshot TEXT NOT NULL,
            amount_text_snapshot TEXT NOT NULL CHECK(length(amount_text_snapshot) BETWEEN 1 AND 32),
            due_on_snapshot TEXT,
            UNIQUE(invoice_id,fee_schedule_id)
        )""",
        "CREATE INDEX idx_fee_invoices_batch ON fee_invoices(batch_id,admission_no_snapshot)",
        "CREATE TRIGGER fee_invoices_no_update BEFORE UPDATE ON fee_invoices BEGIN SELECT RAISE(ABORT,'fee invoice snapshots are immutable'); END",
        "CREATE TRIGGER fee_invoices_no_delete BEFORE DELETE ON fee_invoices BEGIN SELECT RAISE(ABORT,'fee invoice snapshots are immutable'); END",
        "CREATE TRIGGER fee_invoice_lines_no_update BEFORE UPDATE ON fee_invoice_lines BEGIN SELECT RAISE(ABORT,'fee invoice lines are immutable'); END",
        "CREATE TRIGGER fee_invoice_lines_no_delete BEFORE DELETE ON fee_invoice_lines BEGIN SELECT RAISE(ABORT,'fee invoice lines are immutable'); END",
        "INSERT INTO schema_migrations(version) VALUES(15)",
        "PRAGMA user_version = 15",
    )
    for statement in statements:
        conn.execute(statement)


def _migration_16(conn: sqlite3.Connection) -> None:
    """Add explicit invoice issue, payment receipt, allocation and reversal ledgers."""
    statements = (
        """CREATE TABLE fee_invoice_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id INTEGER NOT NULL UNIQUE REFERENCES fee_invoice_batches(id) ON DELETE RESTRICT,
            issued_by TEXT NOT NULL,
            issued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE fee_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL REFERENCES fee_invoices(id) ON DELETE RESTRICT,
            receipt_number TEXT NOT NULL UNIQUE CHECK(length(receipt_number)<=40),
            amount_text TEXT NOT NULL CHECK(length(amount_text) BETWEEN 1 AND 32),
            received_on TEXT NOT NULL CHECK(length(received_on)=10),
            method TEXT NOT NULL CHECK(method IN ('cash','bank_transfer','mobile_money','cheque','other')),
            reference TEXT NOT NULL DEFAULT '' CHECK(length(reference)<=100),
            received_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE fee_payment_reversals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id INTEGER NOT NULL REFERENCES fee_payments(id) ON DELETE RESTRICT,
            amount_text TEXT NOT NULL CHECK(length(amount_text) BETWEEN 1 AND 32),
            reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 5 AND 300),
            reversed_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX idx_fee_payments_invoice ON fee_payments(invoice_id,id)",
        "CREATE INDEX idx_fee_reversals_payment ON fee_payment_reversals(payment_id,id)",
        "CREATE TRIGGER fee_issue_no_update BEFORE UPDATE ON fee_invoice_issues BEGIN SELECT RAISE(ABORT,'invoice issue records are immutable'); END",
        "CREATE TRIGGER fee_issue_no_delete BEFORE DELETE ON fee_invoice_issues BEGIN SELECT RAISE(ABORT,'invoice issue records are immutable'); END",
        "CREATE TRIGGER fee_payment_no_update BEFORE UPDATE ON fee_payments BEGIN SELECT RAISE(ABORT,'payment receipts are immutable'); END",
        "CREATE TRIGGER fee_payment_no_delete BEFORE DELETE ON fee_payments BEGIN SELECT RAISE(ABORT,'payment receipts are immutable'); END",
        "CREATE TRIGGER fee_reversal_no_update BEFORE UPDATE ON fee_payment_reversals BEGIN SELECT RAISE(ABORT,'payment reversal records are immutable'); END",
        "CREATE TRIGGER fee_reversal_no_delete BEFORE DELETE ON fee_payment_reversals BEGIN SELECT RAISE(ABORT,'payment reversal records are immutable'); END",
        """CREATE TRIGGER fee_batch_issued_no_cancel BEFORE UPDATE OF status ON fee_invoice_batches
           WHEN NEW.status='cancelled' AND EXISTS(SELECT 1 FROM fee_invoice_issues WHERE batch_id=OLD.id)
           BEGIN SELECT RAISE(ABORT,'issued invoice batches cannot be cancelled'); END""",
        "INSERT INTO schema_migrations(version) VALUES(16)",
        "PRAGMA user_version = 16",
    )
    for statement in statements:
        conn.execute(statement)


def _migration_17(conn: sqlite3.Connection) -> None:
    """Add the school type setting and a current term per academic session."""
    conn.execute(
        "ALTER TABLE school_profile ADD COLUMN school_type TEXT NOT NULL DEFAULT '' "
        "CHECK(school_type IN ('','primary','secondary','combined'))"
    )
    conn.execute(
        "ALTER TABLE terms ADD COLUMN is_current INTEGER NOT NULL DEFAULT 0 CHECK(is_current IN (0,1))"
    )
    conn.execute(
        "CREATE UNIQUE INDEX idx_terms_one_current_per_session "
        "ON terms(academic_session_id) WHERE is_current=1"
    )
    conn.execute("INSERT INTO schema_migrations(version) VALUES(17)")
    conn.execute("PRAGMA user_version = 17")


def _migration_18(conn: sqlite3.Connection) -> None:
    """Link teacher logins to one staff record without changing existing accounts."""
    conn.execute(
        "ALTER TABLE users ADD COLUMN teacher_id INTEGER REFERENCES teachers(id) ON DELETE RESTRICT"
    )
    conn.execute(
        "CREATE UNIQUE INDEX idx_users_one_account_per_teacher "
        "ON users(teacher_id) WHERE teacher_id IS NOT NULL"
    )
    conn.execute(
        """CREATE TRIGGER users_teacher_link_insert_guard
           BEFORE INSERT ON users
           WHEN NEW.teacher_id IS NOT NULL AND NEW.role<>'teacher'
           BEGIN SELECT RAISE(ABORT,'Only teacher accounts can link to staff records.'); END"""
    )
    conn.execute(
        """CREATE TRIGGER users_teacher_link_update_guard
           BEFORE UPDATE OF teacher_id,role ON users
           WHEN NEW.teacher_id IS NOT NULL AND NEW.role<>'teacher'
           BEGIN SELECT RAISE(ABORT,'Only teacher accounts can link to staff records.'); END"""
    )
    conn.execute("INSERT INTO schema_migrations(version) VALUES(18)")
    conn.execute("PRAGMA user_version = 18")


def _migration_19(conn: sqlite3.Connection) -> None:
    """Store an optional student portrait in the offline school database."""
    conn.execute(
        "ALTER TABLE students ADD COLUMN photo_data BLOB "
        "CHECK(photo_data IS NULL OR length(photo_data)<=1000000)"
    )
    conn.execute("INSERT INTO schema_migrations(version) VALUES(19)")
    conn.execute("PRAGMA user_version = 19")


def _migration_20(conn: sqlite3.Connection) -> None:
    """Retain the v20 migration slot without installing the removed module."""
    conn.execute("INSERT INTO schema_migrations(version) VALUES(20)")
    conn.execute("PRAGMA user_version = 20")


def _migration_21(conn: sqlite3.Connection) -> None:
    """Remove the deferred question-bank module and its live records."""
    conn.execute("DROP TABLE IF EXISTS question_bank_items")
    conn.execute("INSERT INTO schema_migrations(version) VALUES(21)")
    conn.execute("PRAGMA user_version = 21")


def _migration_22(conn: sqlite3.Connection) -> None:
    """Add optional student identity and contact fields without changing existing records."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(students)")}
    for name in ("nin", "date_of_birth", "email"):
        if name not in columns:
            conn.execute(f"ALTER TABLE students ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    conn.execute("INSERT INTO schema_migrations(version) VALUES(22)")
    conn.execute("PRAGMA user_version = 22")


def _migration_23(conn: sqlite3.Connection) -> None:
    """Add the report ranking style and optional report signature images to the school profile."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(school_profile)")}
    if "report_ranking_style" not in columns:
        conn.execute(
            "ALTER TABLE school_profile ADD COLUMN report_ranking_style TEXT NOT NULL DEFAULT 'position' "
            "CHECK(report_ranking_style IN ('position','average_grade'))"
        )
    for name in ("head_signature", "class_teacher_signature"):
        if name not in columns:
            conn.execute(f"ALTER TABLE school_profile ADD COLUMN {name} BLOB")
    conn.execute("INSERT INTO schema_migrations(version) VALUES(23)")
    conn.execute("PRAGMA user_version = 23")


def _migration_24(conn: sqlite3.Connection) -> None:
    """Group the per-invoice payments of one lump-sum receipt without touching existing payments."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(fee_payments)")}
    if "lump_sum_number" not in columns:
        conn.execute("ALTER TABLE fee_payments ADD COLUMN lump_sum_number TEXT CHECK(lump_sum_number IS NULL OR length(lump_sum_number)<=40)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fee_payments_lump_sum ON fee_payments(lump_sum_number) WHERE lump_sum_number IS NOT NULL")
    conn.execute("INSERT INTO schema_migrations(version) VALUES(24)")
    conn.execute("PRAGMA user_version = 24")


def _migration_25(conn: sqlite3.Connection) -> None:
    """Offline communication centre: templates, an outbox/history, guardian opt-outs and settings."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS communication_templates(
            kind TEXT PRIMARY KEY CHECK(kind IN ('birthday','fee_reminder','welcome','notice')),
            subject TEXT NOT NULL CHECK(length(subject) BETWEEN 1 AND 200),
            body TEXT NOT NULL CHECK(length(body) BETWEEN 1 AND 4000),
            updated_by TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS communication_messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK(kind IN ('birthday','fee_reminder','welcome','notice')),
            channel TEXT NOT NULL CHECK(channel IN ('email','whatsapp')),
            student_id INTEGER REFERENCES students(id),
            recipient_name TEXT NOT NULL,
            recipient_contact TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent','cancelled')),
            dedupe_key TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            handled_by TEXT,
            handled_at TEXT)"""
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_comm_messages_dedupe ON communication_messages(dedupe_key) "
        "WHERE dedupe_key IS NOT NULL AND status IN ('pending','sent')"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_comm_messages_status ON communication_messages(status,created_at)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS communication_opt_outs(
            student_id INTEGER PRIMARY KEY REFERENCES students(id),
            reason TEXT NOT NULL DEFAULT '' CHECK(length(reason)<=240),
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS communication_settings(
            key TEXT PRIMARY KEY CHECK(key IN ('country_code')),
            value TEXT NOT NULL CHECK(length(value)<=8),
            updated_by TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"""
    )
    conn.execute("INSERT INTO schema_migrations(version) VALUES(25)")
    conn.execute("PRAGMA user_version = 25")


def _migration_26(conn: sqlite3.Connection) -> None:
    """Printable term closing and next-term dates for report cards."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(school_profile)")}
    for column in ("term_closing_date", "next_term_begins"):
        if column not in columns:
            conn.execute(
                f"ALTER TABLE school_profile ADD COLUMN {column} TEXT NOT NULL DEFAULT '' CHECK(length({column})<=10)"
            )
    conn.execute("INSERT INTO schema_migrations(version) VALUES(26)")
    conn.execute("PRAGMA user_version = 26")


def _migration_27(conn: sqlite3.Connection) -> None:
    """Add the configurable prefix used for newly generated student admission IDs."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(school_profile)")}
    if "admission_id_prefix" not in columns:
        conn.execute(
            "ALTER TABLE school_profile ADD COLUMN admission_id_prefix TEXT NOT NULL DEFAULT 'ADM' "
            "CHECK(length(admission_id_prefix) BETWEEN 1 AND 16)"
        )
    conn.execute("INSERT INTO schema_migrations(version) VALUES(27)")
    conn.execute("PRAGMA user_version = 27")


def initialize() -> None:
    """Apply known migrations; an existing DB is backed up before its first migration."""
    existed = DB_PATH.exists() and DB_PATH.stat().st_size > 0
    with connection() as conn:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise sqlite3.DatabaseError(f"Database schema {version} is newer than supported schema {SCHEMA_VERSION}")
        if version < SCHEMA_VERSION and existed:
            _backup_existing_database(version)
        while version < SCHEMA_VERSION:
            conn.execute("BEGIN IMMEDIATE")
            next_version = version + 1
            if next_version == 1:
                _migration_1(conn)
            elif next_version == 2:
                _migration_2(conn)
            elif next_version == 3:
                _migration_3(conn)
            elif next_version == 4:
                _migration_4(conn)
            elif next_version == 5:
                _migration_5(conn)
            elif next_version == 6:
                _migration_6(conn)
            elif next_version == 7:
                _migration_7(conn)
            elif next_version == 8:
                _migration_8(conn)
            elif next_version == 9:
                _migration_9(conn)
            elif next_version == 10:
                _migration_10(conn)
            elif next_version == 11:
                _migration_11(conn)
            elif next_version == 12:
                _migration_12(conn)
            elif next_version == 13:
                _migration_13(conn)
            elif next_version == 14:
                _migration_14(conn)
            elif next_version == 15:
                _migration_15(conn)
            elif next_version == 16:
                _migration_16(conn)
            elif next_version == 17:
                _migration_17(conn)
            elif next_version == 18:
                _migration_18(conn)
            elif next_version == 19:
                _migration_19(conn)
            elif next_version == 20:
                _migration_20(conn)
            elif next_version == 21:
                _migration_21(conn)
            elif next_version == 22:
                _migration_22(conn)
            elif next_version == 23:
                _migration_23(conn)
            elif next_version == 24:
                _migration_24(conn)
            elif next_version == 25:
                _migration_25(conn)
            elif next_version == 26:
                _migration_26(conn)
            elif next_version == 27:
                _migration_27(conn)
            else:
                raise sqlite3.DatabaseError(f"No migration is defined for schema {next_version}")
            conn.commit()
            version = next_version
    logger.info("Database ready at %s (schema %s)", DB_PATH, SCHEMA_VERSION)


def user_count() -> int:
    with connection() as db:
        return int(db.execute("SELECT COUNT(*) FROM users").fetchone()[0])


SCHOOL_PROFILE_FIELDS = (
    ("school_name", 120), ("address", 200), ("phone", 60), ("email", 120),
    ("head_name", 120), ("head_title", 80),
    ("class_teacher_name", 120), ("class_teacher_title", 80),
    ("school_type", 16), ("admission_id_prefix", 16),
)


def load_school_profile() -> dict[str, str]:
    with connection() as conn:
        row = conn.execute(
            "SELECT school_name,address,phone,email,head_name,head_title,class_teacher_name,class_teacher_title,school_type,admission_id_prefix "
            "FROM school_profile WHERE id=1"
        ).fetchone()
        return {key: str(row[key] or "") for key, _limit in SCHOOL_PROFILE_FIELDS}


def load_school_logo() -> bytes | None:
    with connection() as conn:
        row = conn.execute("SELECT logo_data FROM school_profile WHERE id=1").fetchone()
        return bytes(row["logo_data"]) if row and row["logo_data"] else None


def _school_profile_in_transaction(conn: sqlite3.Connection) -> dict[str, str]:
    row = conn.execute(
        "SELECT school_name,address,phone,email,head_name,head_title,class_teacher_name,class_teacher_title,school_type,admission_id_prefix "
        "FROM school_profile WHERE id=1"
    ).fetchone()
    return {key: str(row[key] or "") for key, _limit in SCHOOL_PROFILE_FIELDS}


def _school_logo_in_transaction(conn: sqlite3.Connection) -> bytes | None:
    row = conn.execute("SELECT logo_data FROM school_profile WHERE id=1").fetchone()
    return bytes(row["logo_data"]) if row and row["logo_data"] else None


_LOGO_UNCHANGED = object()
REPORT_RANKING_STYLES = {
    "position": "Show class position (1st, 2nd…)",
    "average_grade": "Show overall average grade",
}


def _report_settings_in_transaction(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT report_ranking_style,head_signature,class_teacher_signature,term_closing_date,next_term_begins "
        "FROM school_profile WHERE id=1"
    ).fetchone()
    return {
        "ranking_style": row["report_ranking_style"],
        "term_closing_date": row["term_closing_date"],
        "next_term_begins": row["next_term_begins"],
        "head_signature": bytes(row["head_signature"]) if row["head_signature"] else None,
        "class_teacher_signature": bytes(row["class_teacher_signature"]) if row["class_teacher_signature"] else None,
    }


def load_report_settings() -> dict:
    """Return the report ranking style and the optional head / class-teacher signature images."""
    with connection() as conn:
        return _report_settings_in_transaction(conn)


def _validated_image(data, label: str):
    if data is None:
        return None
    if not isinstance(data, bytes):
        raise ValueError(f"{label} must be a validated PNG or JPEG image.")
    if len(data) > 1_000_000:
        raise ValueError(f"{label} must be 1 MB or smaller.")
    if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")):
        raise ValueError(f"{label} must be a PNG or JPEG image.")
    return data


def save_school_profile(
    profile: dict[str, str], actor: str, logo_data=_LOGO_UNCHANGED, *,
    report_ranking_style=_LOGO_UNCHANGED, head_signature=_LOGO_UNCHANGED,
    class_teacher_signature=_LOGO_UNCHANGED, term_closing_date=_LOGO_UNCHANGED,
    next_term_begins=_LOGO_UNCHANGED,
) -> None:
    term_dates = {}
    for column, value, label in (
        ("term_closing_date", term_closing_date, "Term closing date"),
        ("next_term_begins", next_term_begins, "Next term begins"),
    ):
        if value is not _LOGO_UNCHANGED:
            term_dates[column] = _iso_date(value, label).isoformat() if value else ""
    values = {}
    for field, max_length in SCHOOL_PROFILE_FIELDS:
        if field == "admission_id_prefix" and field not in profile:
            values[field] = None  # preserve the saved prefix for partial profile updates
            continue
        value = profile.get(field, "")
        if not isinstance(value, str):
            raise ValueError(f"{field.replace('_', ' ').capitalize()} must be text.")
        value = value.strip()
        if len(value) > max_length:
            raise ValueError(f"{field.replace('_', ' ').capitalize()} must be {max_length} characters or fewer.")
        if field == "admission_id_prefix":
            value = value.upper()
            compact = value.replace("-", "")
            if (not value or not value.isascii() or not compact.isalnum()
                    or value.startswith("-") or value.endswith("-") or "--" in value):
                raise ValueError("Student ID prefix must use letters and numbers, with single hyphens only.")
        values[field] = value
    if values["email"] and ("@" not in values["email"] or values["email"].startswith("@") or values["email"].endswith("@")):
        raise ValueError("Enter a valid email address, or leave it blank.")
    if values["school_type"] not in {"", "primary", "secondary", "combined"}:
        raise ValueError("Choose Primary, Secondary, or Primary and Secondary for the school type.")
    optional_images = (
        ("logo_data", logo_data, "School logo"),
        ("head_signature", head_signature, "Head of school signature"),
        ("class_teacher_signature", class_teacher_signature, "Class teacher signature"),
    )
    for _column, image, label in optional_images:
        if image is not _LOGO_UNCHANGED:
            _validated_image(image, label)
    if report_ranking_style is not _LOGO_UNCHANGED and report_ranking_style not in REPORT_RANKING_STYLES:
        raise ValueError("Choose class position or overall average grade for the report ranking style.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        if values["admission_id_prefix"] is None:
            saved_prefix = conn.execute(
                "SELECT admission_id_prefix FROM school_profile WHERE id=1"
            ).fetchone()
            values["admission_id_prefix"] = saved_prefix[0] if saved_prefix else "ADM"
        columns = ",".join(f"{field}=?" for field, _limit in SCHOOL_PROFILE_FIELDS)
        params = [values[field] for field, _limit in SCHOOL_PROFILE_FIELDS]
        extra_clause = ""
        for column, image, _label in optional_images:
            if image is not _LOGO_UNCHANGED:
                extra_clause += f",{column}=?"
                params.append(image)
        if report_ranking_style is not _LOGO_UNCHANGED:
            extra_clause += ",report_ranking_style=?"
            params.append(report_ranking_style)
        for column, value in term_dates.items():
            extra_clause += f",{column}=?"
            params.append(value)
        params.append(actor)
        conn.execute(
            f"UPDATE school_profile SET {columns}{extra_clause},updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE id=1",
            params,
        )
        _audit_in_transaction(conn, actor, "UPDATE", "school_profile", 1)


def load_audit_log(actor: str, starts_on: str = "", ends_on: str = "", action: str = "", search: str = "", limit: int = 500) -> dict:
    """Administrator-only, read-only view of the audit trail (newest first)."""
    where, params = [], []
    if starts_on:
        where.append("date(created_at)>=?")
        params.append(_iso_date(starts_on, "Start date").isoformat())
    if ends_on:
        where.append("date(created_at)<=?")
        params.append(_iso_date(ends_on, "End date").isoformat())
    if action:
        where.append("action=?")
        params.append(str(action))
    if search and search.strip():
        like = "%" + search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(username LIKE ? ESCAPE '\\' OR entity LIKE ? ESCAPE '\\' OR entity_id LIKE ? ESCAPE '\\')")
        params += [like, like, like]
    limit = max(1, min(int(limit), 2000))
    clause = " WHERE " + " AND ".join(where) if where else ""
    with connection() as conn:
        _require_admin(conn, actor)
        total = conn.execute(f"SELECT COUNT(*) FROM audit_log{clause}", params).fetchone()[0]
        rows = [dict(r) for r in conn.execute(
            f"SELECT id,created_at,username,action,entity,entity_id FROM audit_log{clause} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        )]
        actions = [r[0] for r in conn.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")]
    return {"rows": rows, "total": int(total), "actions": actions, "limit": limit}


def create_user(username: str, password_hash: str, role: str = "admin") -> None:
    username = username.strip()
    if len(username) < 3:
        raise ValueError("Username must contain at least 3 characters.")
    if role not in {"admin", "principal", "teacher"}:
        raise ValueError("Invalid user role.")
    with connection() as db:
        db.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)", (username, password_hash, role))


def list_user_accounts() -> list[dict]:
    with connection() as conn:
        return [dict(row) for row in conn.execute(
            """SELECT u.id,u.username,u.role,u.is_active,u.teacher_id,
                      t.staff_no,t.first_name AS teacher_first_name,t.last_name AS teacher_last_name
               FROM users u LEFT JOIN teachers t ON t.id=u.teacher_id
               ORDER BY u.username COLLATE NOCASE"""
        )]


def create_teacher_user(username: str, password_hash: str, teacher_id: int, actor: str) -> None:
    username = username.strip() if isinstance(username, str) else ""
    if len(username) < 3 or len(username) > 256:
        raise ValueError("Username must contain between 3 and 256 characters.")
    if not isinstance(password_hash, str) or not password_hash:
        raise ValueError("A valid password hash is required.")
    if isinstance(teacher_id, bool) or not isinstance(teacher_id, int) or teacher_id <= 0:
        raise ValueError("Select an active teacher record.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        teacher = conn.execute(
            "SELECT id FROM teachers WHERE id=? AND status COLLATE NOCASE='Active'", (teacher_id,)
        ).fetchone()
        if teacher is None:
            raise ValueError("Select an active teacher record.")
        try:
            cursor = conn.execute(
                "INSERT INTO users(username,password_hash,role,teacher_id) VALUES(?,?,?,?)",
                (username, password_hash, "teacher", teacher_id),
            )
        except sqlite3.IntegrityError as exc:
            if "idx_users_one_account_per_teacher" in str(exc) or "users.teacher_id" in str(exc):
                    raise ValueError("This teacher record is already linked to a login account.") from exc
            if "users.username" in str(exc):
                raise ValueError("That username is already in use.") from exc
            raise
        _audit_in_transaction(conn, actor, "CREATE", "users", int(cursor.lastrowid))


def link_teacher_user(username: str, teacher_id: int, actor: str) -> None:
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        user = conn.execute(
            "SELECT id,role FROM users WHERE username=? COLLATE NOCASE", (username.strip(),)
        ).fetchone()
        if user is None or user["role"] != "teacher":
            raise ValueError("Select an existing teacher login account.")
        teacher = conn.execute(
            "SELECT id FROM teachers WHERE id=? AND status COLLATE NOCASE='Active'", (teacher_id,)
        ).fetchone()
        if teacher is None:
            raise ValueError("Select an active teacher record.")
        existing = conn.execute(
            "SELECT id FROM users WHERE teacher_id=? AND id<>?", (teacher_id, int(user["id"]))
        ).fetchone()
        if existing:
            raise ValueError("This teacher record is already linked to another login account.")
        conn.execute("UPDATE users SET teacher_id=? WHERE id=?", (teacher_id, int(user["id"])))
        _audit_in_transaction(conn, actor, "UPDATE", "users", int(user["id"]))


def set_user_password(username: str, password_hash: str, actor: str) -> None:
    if not isinstance(password_hash, str) or not password_hash:
        raise ValueError("A valid password hash is required.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        user = conn.execute(
            "SELECT id FROM users WHERE username=? COLLATE NOCASE", (username.strip(),)
        ).fetchone()
        if user is None:
            raise ValueError("User account was not found.")
        conn.execute(
            "UPDATE users SET password_hash=?,failed_login_count=0,locked_until=NULL WHERE id=?",
            (password_hash, int(user["id"])),
        )
        _audit_in_transaction(conn, actor, "RESET_PASSWORD", "users", int(user["id"]))


def set_user_active(username: str, active: bool, actor: str) -> None:
    if not isinstance(active, bool):
        raise ValueError("Account status must be active or inactive.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        user = conn.execute(
            "SELECT id,role,is_active FROM users WHERE username=? COLLATE NOCASE", (username.strip(),)
        ).fetchone()
        if user is None:
            raise ValueError("User account was not found.")
        if not active and user["role"] == "admin" and user["is_active"]:
            active_admins = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1"
            ).fetchone()[0]
            if active_admins <= 1:
                raise ValueError("At least one active administrator account must remain.")
        conn.execute("UPDATE users SET is_active=? WHERE id=?", (int(active), int(user["id"])))
        _audit_in_transaction(conn, actor, "ACTIVATE" if active else "DEACTIVATE", "users", int(user["id"]))


def get_user(username: str):
    with connection() as db:
        return db.execute("SELECT id,username,password_hash,role,is_active,failed_login_count,locked_until,teacher_id FROM users WHERE username=? COLLATE NOCASE", (username.strip(),)).fetchone()


def authenticate(username: str, password: str, verifier) -> tuple[sqlite3.Row | None, int]:
    """Verify a login and persist progressive lockout. Return (user, retry_seconds)."""
    key = username.strip().casefold()[:256]
    now = datetime.now(timezone.utc)
    with connection() as conn:
        row = conn.execute("SELECT id,username,password_hash,role,is_active,failed_login_count,locked_until,teacher_id FROM users WHERE username=? COLLATE NOCASE", (username.strip(),)).fetchone()
        if row and row["locked_until"]:
            until = datetime.fromisoformat(row["locked_until"])
            if until > now:
                return None, max(1, int((until - now).total_seconds()))
        if row and row["is_active"] and verifier(password, row["password_hash"]):
            conn.execute("DELETE FROM login_attempts WHERE username_key=?", (key,))
            conn.execute("UPDATE users SET failed_login_count=0,locked_until=NULL WHERE id=?", (row["id"],))
            return row, 0
        attempts = conn.execute("SELECT failure_count FROM login_attempts WHERE username_key=?", (key,)).fetchone()
        count = (attempts[0] if attempts else 0) + 1
        delay = min(300, 2 ** min(count - 1, 8))
        locked = (now + timedelta(seconds=delay)).isoformat(timespec="seconds")
        conn.execute("INSERT INTO login_attempts(username_key,failure_count,locked_until) VALUES(?,?,?) ON CONFLICT(username_key) DO UPDATE SET failure_count=excluded.failure_count,locked_until=excluded.locked_until", (key, count, locked))
        if row:
            conn.execute("UPDATE users SET failed_login_count=?,locked_until=? WHERE id=?", (count, locked, row["id"]))
        return None, delay


def log_action(username: str, action: str, entity: str, entity_id: str = "") -> None:
    with connection() as db:
        db.execute("INSERT INTO audit_log(username,action,entity,entity_id) VALUES(?,?,?,?)", (username, action, entity, str(entity_id)))


def _authorized(module_name: str, actor: str | None, write: bool = False) -> None:
    if module_name not in MODULES:
        raise ValueError("Unknown module.")
    if not write:
        return
    if actor is None:
        raise PermissionError("An authenticated administrator is required.")
    with connection() as conn:
        row = conn.execute("SELECT role,is_active FROM users WHERE username=? COLLATE NOCASE", (actor.strip(),)).fetchone()
    if not row or not row["is_active"] or row["role"] != "admin":
        raise PermissionError("Only an active administrator may change records.")


def _require_admin(conn: sqlite3.Connection, actor: str | None) -> str:
    if not actor:
        raise PermissionError("An authenticated administrator is required.")
    row = conn.execute("SELECT role,is_active FROM users WHERE username=? COLLATE NOCASE", (actor.strip(),)).fetchone()
    if not row or not row["is_active"] or row["role"] != "admin":
        raise PermissionError("Only an active administrator may change records.")
    return actor.strip()


def _audit_in_transaction(
    conn: sqlite3.Connection, username: str, action: str, entity: str, entity_id: int | str
) -> None:
    conn.execute(
        "INSERT INTO audit_log(username,action,entity,entity_id) VALUES(?,?,?,?)",
        (username, action, entity, str(entity_id)),
    )


def _iso_date(value: str, field_name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a date in YYYY-MM-DD format.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid date in YYYY-MM-DD format.") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field_name} must use YYYY-MM-DD format.")
    return parsed


def _date_range(starts_on: str, ends_on: str, label: str) -> tuple[str, str]:
    start = _iso_date(starts_on, f"{label} start date")
    end = _iso_date(ends_on, f"{label} end date")
    if start >= end:
        raise ValueError(f"{label} start date must be earlier than its end date.")
    return start.isoformat(), end.isoformat()


def _decimal_value(value, field_name: str) -> Decimal:
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"{field_name} must be entered as a decimal number.")
    raw = str(value).strip()
    if not raw or len(raw) > 32:
        raise ValueError(f"{field_name} must be a finite decimal with at most 32 characters.")
    try:
        result = value if isinstance(value, Decimal) else Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid decimal number.") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal number.")
    if result.as_tuple().exponent < -12:
        raise ValueError(f"{field_name} may have no more than 12 decimal places.")
    return result


def _decimal_text(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def validate_assessment_score(score, max_score) -> str:
    """Validate a score without binary floating-point conversion and return canonical text."""
    maximum = _decimal_value(max_score, "Maximum score")
    value = _decimal_value(score, "Score")
    if maximum <= 0 or maximum > Decimal("1000000"):
        raise ValueError("Maximum score must be greater than zero and no more than 1000000.")
    if value < 0 or value > maximum:
        raise ValueError("Score must be between zero and the maximum score, inclusive.")
    return _decimal_text(value)


def validate_grade_threshold(threshold) -> str:
    """Validate an inclusive 0–100 lower threshold and return canonical decimal text."""
    value = _decimal_value(threshold, "Grade threshold")
    if value < 0 or value > 100:
        raise ValueError("Grade threshold must be between 0 and 100, inclusive.")
    return _decimal_text(value)


def calculate_assessment_result(components: list[dict], scores: dict[int, str], bands: list[dict]) -> dict:
    """Calculate the generic points-based result without treating missing scores as zero."""
    if not components:
        raise ValueError("Define assessment components before entering scores.")
    ordered_bands = sorted(bands, key=lambda item: Decimal(str(item["threshold"])))
    if not ordered_bands or Decimal(str(ordered_bands[0]["threshold"])) != 0:
        raise ValueError("Configure a grade scale beginning at 0 before entering scores.")
    maximum = sum((_decimal_value(item["max_score"], "Maximum score") for item in components), Decimal(0))
    earned = Decimal(0)
    missing = False
    for component in components:
        component_id = int(component["id"])
        value = scores.get(component_id)
        if value is None:
            missing = True
            continue
        earned += _decimal_value(validate_assessment_score(value, component["max_score"]), "Score")
    if missing:
        return {
            "complete": False, "points_earned": _decimal_text(earned),
            "points_possible": _decimal_text(maximum), "percentage": None,
            "display_percentage": None, "grade_label": None, "remark": None,
        }
    percentage = earned * Decimal(100) / maximum
    matching = [band for band in ordered_bands if Decimal(str(band["threshold"])) <= percentage]
    grade = matching[-1] if matching else None
    return {
        "complete": True,
        "points_earned": _decimal_text(earned),
        "points_possible": _decimal_text(maximum),
        "percentage": _decimal_text(percentage),
        "display_percentage": str(percentage.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        "grade_label": grade["grade_label"] if grade else None,
        "remark": grade.get("remark", "") if grade else None,
    }


def _overall_average_fraction(subject_results: list[dict]) -> Fraction | None:
    """Exact equal-weight mean of subject percentages, or None if any subject is incomplete."""
    if not subject_results:
        raise ValueError("Provide at least one subject result to calculate an overall average.")
    if any(not item["complete"] for item in subject_results):
        return None
    total = sum(
        (
            Fraction(100) * Fraction(Decimal(item["points_earned"])) / Fraction(Decimal(item["points_possible"]))
            for item in subject_results
        ),
        Fraction(0),
    )
    return total / len(subject_results)


def _common_grade_bands(band_sets: list[list[dict]]) -> list[dict] | None:
    """Return the grade bands shared by every subject snapshot, or None if they differ."""
    def normal(bands):
        return sorted(
            (Decimal(str(band["threshold"])).normalize(), band["grade_label"], band.get("remark", "") or "")
            for band in bands
        )
    if not band_sets or any(not bands for bands in band_sets):
        return None
    first = normal(band_sets[0])
    return band_sets[0] if all(normal(bands) == first for bands in band_sets[1:]) else None


def grade_key_entries(bands: list[dict] | None) -> list[dict]:
    """Return the school's grade bands, highest first, for the printed grading key."""
    if not bands:
        return []
    ordered = sorted(bands, key=lambda item: Decimal(str(item["threshold"])), reverse=True)
    return [
        {
            "grade_label": band["grade_label"], "remark": band.get("remark", "") or "",
            "from_text": _decimal_text(Decimal(str(band["threshold"]))),
        }
        for band in ordered
    ]


def calculate_overall_average(subject_results: list[dict], bands: list[dict] | None = None) -> dict:
    """Return a student's overall average: the equal-weight mean of subject percentages.

    Each subject contributes its own points percentage (earned / possible x 100), so a
    subject with a larger maximum does not count more than one with a smaller maximum.
    The mean is computed exactly (no float or intermediate rounding) and rounded
    half-up to two places only for display. A missing subject score is never treated
    as zero: if any subject result is incomplete, no average is produced. When the
    caller supplies one shared set of grade bands, the exact (unrounded) average is
    also mapped to a grade; otherwise the grade is left empty rather than guessed.
    """
    average = _overall_average_fraction(subject_results)
    count = len(subject_results)
    if average is None:
        return {
            "complete": False, "subject_count": count, "average": None, "display_average": None,
            "grade_label": None, "remark": None,
        }
    cents = (average * 100 + Fraction(1, 2)).__floor__()
    grade_label = remark = None
    if bands:
        matching = [
            band for band in sorted(bands, key=lambda item: Decimal(str(item["threshold"])))
            if Fraction(Decimal(str(band["threshold"]))) <= average
        ]
        if matching:
            grade_label, remark = matching[-1]["grade_label"], matching[-1].get("remark", "") or ""
    return {
        "complete": True,
        "subject_count": count,
        "average": _decimal_text(Decimal(average.numerator) / Decimal(average.denominator)),
        "display_average": f"{cents // 100}.{cents % 100:02d}",
        "grade_label": grade_label,
        "remark": remark,
    }


def ordinal_text(number: int) -> str:
    """Return 1st, 2nd, 3rd, 4th, 11th, 12th, 13th, 21st, ... for a positive integer."""
    if number < 1:
        raise ValueError("A position must be a positive whole number.")
    if 11 <= number % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def calculate_class_positions(averages: dict[int, Fraction]) -> dict[int, dict]:
    """Rank students by exact overall average using standard competition ranking.

    Students with exactly equal averages share a position and the next position is
    skipped (1, 2, 2, 4). Ranking uses the unrounded average, so two students whose
    averages only look equal after rounding are still ordered. Every student in the
    supplied roster counts toward "of"; callers pass only complete, approved results.
    """
    ordered = sorted(averages.values(), reverse=True)
    first_place: dict[Fraction, int] = {}
    counts: dict[Fraction, int] = {}
    for index, value in enumerate(ordered):
        first_place.setdefault(value, index + 1)
        counts[value] = counts.get(value, 0) + 1
    total = len(ordered)
    return {
        key: {
            "position": first_place[value], "of": total,
            "position_text": ordinal_text(first_place[value]), "tied": counts[value] > 1,
        }
        for key, value in averages.items()
    }


def list_academic_sessions():
    with connection() as conn:
        return conn.execute(
            "SELECT id,name,starts_on,ends_on,is_current FROM academic_sessions "
            "ORDER BY is_current DESC,starts_on DESC,id DESC"
        ).fetchall()


def load_student_registration_options() -> dict:
    """Return the class and session choices needed by the guided registration form."""
    with connection() as conn:
        sessions = [dict(row) for row in conn.execute(
            "SELECT id,name,starts_on,ends_on,is_current FROM academic_sessions "
            "ORDER BY is_current DESC,starts_on DESC,id DESC"
        )]
        classes = [dict(row) for row in conn.execute(
            "SELECT id,class_name,section,educational_stage FROM classes "
            "ORDER BY class_name COLLATE NOCASE,section COLLATE NOCASE,id"
        )]
    return {"sessions": sessions, "classes": classes}


def save_academic_session(
    name: str, starts_on: str, ends_on: str, is_current: bool, actor: str, session_id: int | None = None
) -> int:
    name = name.strip()
    if not name or len(name) > 100:
        raise ValueError("Session name must contain between 1 and 100 characters.")
    starts_on, ends_on = _date_range(starts_on, ends_on, "Session")
    if not isinstance(is_current, bool):
        raise ValueError("Current session selection must be true or false.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        if session_id is not None:
            term_outside = conn.execute(
                "SELECT 1 FROM terms WHERE academic_session_id=? AND (starts_on<? OR ends_on>?) LIMIT 1",
                (session_id, starts_on, ends_on),
            ).fetchone()
            if term_outside:
                raise ValueError("Session dates must include all dates of its existing terms.")
        if is_current:
            conn.execute("UPDATE academic_sessions SET is_current=0,updated_at=CURRENT_TIMESTAMP WHERE is_current=1")
        if session_id is None:
            cursor = conn.execute(
                "INSERT INTO academic_sessions(name,starts_on,ends_on,is_current) VALUES(?,?,?,?)",
                (name, starts_on, ends_on, int(is_current)),
            )
            session_id = int(cursor.lastrowid)
            action = "CREATE"
        else:
            cursor = conn.execute(
                "UPDATE academic_sessions SET name=?,starts_on=?,ends_on=?,is_current=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (name, starts_on, ends_on, int(is_current), session_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Academic session was not found.")
            action = "UPDATE"
        _audit_in_transaction(conn, actor, action, "academic_sessions", session_id)
        return session_id


def set_current_academic_session(session_id: int, actor: str) -> None:
    if isinstance(session_id, bool) or not isinstance(session_id, int) or session_id <= 0:
        raise ValueError("Select an academic session to make active.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        if not conn.execute("SELECT 1 FROM academic_sessions WHERE id=?", (session_id,)).fetchone():
            raise ValueError("Academic session was not found.")
        conn.execute(
            "UPDATE academic_sessions SET is_current=0,updated_at=CURRENT_TIMESTAMP WHERE is_current=1"
        )
        conn.execute(
            "UPDATE academic_sessions SET is_current=1,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (session_id,),
        )
        _audit_in_transaction(conn, actor, "SET_CURRENT", "academic_sessions", session_id)


def list_terms(academic_session_id: int):
    with connection() as conn:
        return conn.execute(
            "SELECT id,academic_session_id,term_name,term_order,starts_on,ends_on,is_current "
            "FROM terms WHERE academic_session_id=? ORDER BY is_current DESC,term_order,id",
            (academic_session_id,),
        ).fetchall()


def set_current_term(term_id: int, actor: str) -> None:
    if isinstance(term_id, bool) or not isinstance(term_id, int) or term_id <= 0:
        raise ValueError("Select a term to make active.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        row = conn.execute(
            "SELECT academic_session_id FROM terms WHERE id=?", (term_id,)
        ).fetchone()
        if not row:
            raise ValueError("Term was not found.")
        session_id = int(row["academic_session_id"])
        conn.execute(
            "UPDATE terms SET is_current=0,updated_at=CURRENT_TIMESTAMP "
            "WHERE academic_session_id=? AND is_current=1",
            (session_id,),
        )
        conn.execute(
            "UPDATE terms SET is_current=1,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (term_id,),
        )
        _audit_in_transaction(conn, actor, "SET_CURRENT", "terms", term_id)


def list_student_enrollments(academic_session_id: int | None = None):
    sql = """SELECT e.id,e.student_id,s.admission_no,s.first_name,s.last_name,
                     e.class_id,c.class_name,c.section,e.academic_session_id,a.name AS session_name,
                     e.status,e.enrolled_on,e.left_on
              FROM student_enrollments e
              JOIN students s ON s.id=e.student_id
              JOIN classes c ON c.id=e.class_id
              JOIN academic_sessions a ON a.id=e.academic_session_id"""
    params: tuple[object, ...] = ()
    if academic_session_id is not None:
        sql += " WHERE e.academic_session_id=?"
        params = (academic_session_id,)
    sql += " ORDER BY a.starts_on DESC,s.last_name,s.first_name,e.enrolled_on,e.id"
    with connection() as conn:
        return conn.execute(sql, params).fetchall()


def list_student_enrollments_page(
    academic_session_id: int | None = None, limit: int = 200, offset: int = 0
):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("Page size must be between 1 and 500.")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("Page offset must be a non-negative whole number.")
    where = " WHERE academic_session_id=?" if academic_session_id is not None else ""
    params = (academic_session_id,) if academic_session_id is not None else ()
    with connection() as conn:
        conn.execute("BEGIN")
        total = conn.execute(
            "SELECT COUNT(*) FROM student_enrollments" + where, params
        ).fetchone()[0]
        rows = conn.execute(
            """SELECT e.id,e.student_id,s.admission_no,s.first_name,s.last_name,
                      e.class_id,c.class_name,c.section,e.academic_session_id,a.name AS session_name,
                      e.status,e.enrolled_on,e.left_on
               FROM student_enrollments e
               JOIN students s ON s.id=e.student_id
               JOIN classes c ON c.id=e.class_id
               JOIN academic_sessions a ON a.id=e.academic_session_id"""
            + (" WHERE e.academic_session_id=?" if academic_session_id is not None else "")
            + " ORDER BY a.starts_on DESC,s.last_name,s.first_name,e.enrolled_on,e.id LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return rows, int(total)


def list_active_students():
    with connection() as conn:
        return conn.execute(
            "SELECT id,admission_no,first_name,last_name FROM students WHERE is_active=1 ORDER BY last_name,first_name,admission_no"
        ).fetchall()


def search_active_students(search: str, limit: int = 100):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("Student search limit must be between 1 and 500.")
    term = search.strip()
    if len(term) < 2:
        return [], 0
    pattern = f"%{term}%"
    where = "is_active=1 AND (admission_no LIKE ? OR first_name LIKE ? OR last_name LIKE ?)"
    with connection() as conn:
        conn.execute("BEGIN")
        total = conn.execute(
            "SELECT COUNT(*) FROM students WHERE " + where,
            (pattern, pattern, pattern),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT id,admission_no,first_name,last_name FROM students WHERE " + where
            + " ORDER BY last_name,first_name,admission_no LIMIT ?",
            (pattern, pattern, pattern, limit),
        ).fetchall()
    return rows, int(total)


def has_active_students() -> bool:
    with connection() as conn:
        return conn.execute("SELECT 1 FROM students WHERE is_active=1 LIMIT 1").fetchone() is not None


def list_class_sections():
    with connection() as conn:
        return conn.execute(
            "SELECT id,class_name,section,educational_stage FROM classes ORDER BY class_name,section"
        ).fetchall()


def list_class_stages():
    """List classes, including legacy classes that still need an explicit stage."""
    with connection() as conn:
        return conn.execute(
            "SELECT c.id,c.class_name,c.section,c.educational_stage,p.school_type "
            "FROM classes c CROSS JOIN school_profile p WHERE p.id=1 "
            "ORDER BY c.class_name,c.section"
        ).fetchall()


def save_class_educational_stage(class_id: int, educational_stage: str, actor: str) -> None:
    """Set a class's explicit educational stage and audit it atomically."""
    if educational_stage not in EDUCATIONAL_STAGES:
        raise ValueError("Select Primary or Secondary for this class.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        school_type = conn.execute("SELECT school_type FROM school_profile WHERE id=1").fetchone()[0]
        if school_type in EDUCATIONAL_STAGES and educational_stage != school_type:
            school_label = EDUCATIONAL_STAGES[school_type]
            raise ValueError(
                f"School Profile is set to {school_label} only. Set this class to {school_label}, "
                "or change the school type to Primary and Secondary."
            )
        cursor = conn.execute(
            "UPDATE classes SET educational_stage=? WHERE id=?",
            (educational_stage, class_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Class was not found.")
        _audit_in_transaction(conn, actor, "SET_EDUCATIONAL_STAGE", "classes", class_id)


def list_subjects():
    with connection() as conn:
        return conn.execute(
            "SELECT id,subject_code,subject_name,level FROM subjects ORDER BY subject_name,subject_code"
        ).fetchall()


def list_active_teachers():
    with connection() as conn:
        return conn.execute(
            "SELECT id,staff_no,first_name,last_name FROM teachers WHERE status COLLATE NOCASE='Active' "
            "ORDER BY last_name,first_name,staff_no"
        ).fetchall()


def list_class_subject_assignments(academic_session_id: int | None = None):
    sql = """SELECT x.id,x.academic_session_id,a.name AS session_name,x.class_id,
                     c.class_name,c.section,x.subject_id,s.subject_code,s.subject_name,
                     x.teacher_id,t.staff_no,t.first_name AS teacher_first_name,
                     t.last_name AS teacher_last_name
              FROM class_subject_assignments x
              JOIN academic_sessions a ON a.id=x.academic_session_id
              JOIN classes c ON c.id=x.class_id
              JOIN subjects s ON s.id=x.subject_id
              LEFT JOIN teachers t ON t.id=x.teacher_id"""
    params: tuple[object, ...] = ()
    if academic_session_id is not None:
        sql += " WHERE x.academic_session_id=?"
        params = (academic_session_id,)
    sql += " ORDER BY c.class_name,c.section,s.subject_name,s.subject_code"
    with connection() as conn:
        return conn.execute(sql, params).fetchall()


def load_class_subject_page_data(academic_session_id: int | None = None) -> dict[str, list]:
    """Load assignment catalogs and rows together for the asynchronous UI page."""
    with connection() as conn:
        conn.execute("BEGIN")
        sessions = conn.execute(
            "SELECT id,name,starts_on,ends_on,is_current FROM academic_sessions ORDER BY is_current DESC,starts_on DESC,id DESC"
        ).fetchall()
        classes = conn.execute(
            "SELECT id,class_name,section,educational_stage FROM classes ORDER BY class_name,section"
        ).fetchall()
        subjects = conn.execute(
            "SELECT id,subject_code,subject_name,level FROM subjects ORDER BY subject_name,subject_code"
        ).fetchall()
        teachers = conn.execute(
            "SELECT id,staff_no,first_name,last_name FROM teachers "
            "WHERE status COLLATE NOCASE='Active' ORDER BY last_name,first_name,staff_no"
        ).fetchall()
        sql = """SELECT x.id,x.academic_session_id,a.name AS session_name,x.class_id,
                         c.class_name,c.section,c.educational_stage,x.subject_id,
                         s.subject_code,s.subject_name,
                         x.teacher_id,t.staff_no,t.first_name AS teacher_first_name,
                         t.last_name AS teacher_last_name
                  FROM class_subject_assignments x
                  JOIN academic_sessions a ON a.id=x.academic_session_id
                  JOIN classes c ON c.id=x.class_id
                  JOIN subjects s ON s.id=x.subject_id
                  LEFT JOIN teachers t ON t.id=x.teacher_id"""
        params: tuple[object, ...] = ()
        if academic_session_id is not None:
            sql += " WHERE x.academic_session_id=?"
            params = (academic_session_id,)
        assignments = conn.execute(
            sql + " ORDER BY c.class_name,c.section,s.subject_name,s.subject_code", params
        ).fetchall()
    return {
        "sessions": sessions,
        "classes": classes,
        "subjects": subjects,
        "teachers": teachers,
        "assignments": assignments,
    }


def save_class_subject_assignment(
    academic_session_id: int,
    class_id: int,
    subject_id: int,
    teacher_id: int | None,
    actor: str,
    assignment_id: int | None = None,
) -> int:
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        if not conn.execute("SELECT 1 FROM academic_sessions WHERE id=?", (academic_session_id,)).fetchone():
            raise ValueError("Select an existing academic session.")
        class_row = conn.execute(
            "SELECT educational_stage FROM classes WHERE id=?", (class_id,)
        ).fetchone()
        if not class_row:
            raise ValueError("Select an existing class and section.")
        if not class_row["educational_stage"]:
            raise ValueError("Set this class's educational stage before assigning subjects.")
        if not conn.execute("SELECT 1 FROM subjects WHERE id=?", (subject_id,)).fetchone():
            raise ValueError("Select an existing subject.")
        if teacher_id is not None and not conn.execute(
            "SELECT 1 FROM teachers WHERE id=? AND status COLLATE NOCASE='Active'", (teacher_id,)
        ).fetchone():
            raise ValueError("Select an active teacher, or leave the assignment unassigned.")
        duplicate = conn.execute(
            "SELECT id FROM class_subject_assignments "
            "WHERE academic_session_id=? AND class_id=? AND subject_id=? "
            "AND (? IS NULL OR id<>?)",
            (academic_session_id, class_id, subject_id, assignment_id, assignment_id),
        ).fetchone()
        if duplicate:
            raise ValueError("This subject is already assigned to that class in the selected session.")
        if assignment_id is None:
            cursor = conn.execute(
                "INSERT INTO class_subject_assignments "
                "(academic_session_id,class_id,subject_id,teacher_id) VALUES(?,?,?,?)",
                (academic_session_id, class_id, subject_id, teacher_id),
            )
            assignment_id = int(cursor.lastrowid)
            action = "CREATE"
        else:
            cursor = conn.execute(
                "UPDATE class_subject_assignments SET academic_session_id=?,class_id=?,subject_id=?,"
                "teacher_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (academic_session_id, class_id, subject_id, teacher_id, assignment_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Class subject assignment was not found.")
            action = "UPDATE"
        _audit_in_transaction(conn, actor, action, "class_subject_assignments", assignment_id)
        return assignment_id


def add_class_subject_assignments(
    academic_session_id: int, class_id: int, subject_ids: list[int], actor: str,
    teacher_id: int | None = None,
) -> int:
    """Add a checked set of subjects for one class atomically, keeping existing mappings."""
    normalized_subject_ids = sorted({int(subject_id) for subject_id in subject_ids})
    if not normalized_subject_ids:
        raise ValueError("Select at least one subject to add.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        if teacher_id is not None and not conn.execute(
            "SELECT 1 FROM teachers WHERE id=? AND status COLLATE NOCASE='Active'", (teacher_id,)
        ).fetchone():
            raise ValueError("Select an active teacher, or leave the assignment unassigned.")
        if not conn.execute("SELECT 1 FROM academic_sessions WHERE id=?", (academic_session_id,)).fetchone():
            raise ValueError("Select an existing academic session.")
        class_row = conn.execute(
            "SELECT educational_stage FROM classes WHERE id=?", (class_id,)
        ).fetchone()
        if not class_row:
            raise ValueError("Select an existing class and section.")
        if not class_row["educational_stage"]:
            raise ValueError("Set this class's educational stage before assigning subjects.")
        for subject_id in normalized_subject_ids:
            if not conn.execute("SELECT 1 FROM subjects WHERE id=?", (subject_id,)).fetchone():
                raise ValueError("One of the selected subjects no longer exists. Refresh and try again.")
        inserted = 0
        for subject_id in normalized_subject_ids:
            existing = conn.execute(
                "SELECT id FROM class_subject_assignments "
                "WHERE academic_session_id=? AND class_id=? AND subject_id=?",
                (academic_session_id, class_id, subject_id),
            ).fetchone()
            if existing:
                continue
            cursor = conn.execute(
                "INSERT INTO class_subject_assignments "
                "(academic_session_id,class_id,subject_id,teacher_id) VALUES(?,?,?,?)",
                (academic_session_id, class_id, subject_id, teacher_id),
            )
            _audit_in_transaction(conn, actor, "CREATE", "class_subject_assignments", int(cursor.lastrowid))
            inserted += 1
        return inserted


def delete_class_subject_assignment(assignment_id: int, actor: str) -> None:
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        cursor = conn.execute("DELETE FROM class_subject_assignments WHERE id=?", (assignment_id,))
        if cursor.rowcount != 1:
            raise ValueError("Class subject assignment was not found.")
        _audit_in_transaction(conn, actor, "DELETE", "class_subject_assignments", assignment_id)


def load_assessment_setup_data(
    academic_session_id: int | None = None,
    class_subject_assignment_id: int | None = None,
    term_id: int | None = None,
) -> dict[str, list]:
    """Load assessment setup selectors and components from one read snapshot."""
    with connection() as conn:
        conn.execute("BEGIN")
        sessions = conn.execute(
            "SELECT id,name,starts_on,ends_on,is_current FROM academic_sessions ORDER BY is_current DESC,starts_on DESC,id DESC"
        ).fetchall()
        assignments = []
        terms = []
        components = []
        if academic_session_id is not None:
            assignments = conn.execute(
            """SELECT x.id,x.academic_session_id,c.class_name,c.section,c.educational_stage,
                          s.subject_code,s.subject_name
                   FROM class_subject_assignments x
                   JOIN classes c ON c.id=x.class_id
                   JOIN subjects s ON s.id=x.subject_id
                   WHERE x.academic_session_id=?
                   ORDER BY c.class_name,c.section,s.subject_name,s.subject_code""",
                (academic_session_id,),
            ).fetchall()
            terms = conn.execute(
                "SELECT id,academic_session_id,term_name,term_order,starts_on,ends_on,is_current "
                "FROM terms WHERE academic_session_id=? ORDER BY is_current DESC,term_order,id",
                (academic_session_id,),
            ).fetchall()
        if class_subject_assignment_id is not None and term_id is not None:
            pair = conn.execute(
                """SELECT x.academic_session_id AS assignment_session,
                          t.academic_session_id AS term_session
                   FROM class_subject_assignments x JOIN terms t ON t.id=? WHERE x.id=?""",
                (term_id, class_subject_assignment_id),
            ).fetchone()
            if pair is None:
                raise ValueError("Select an existing class-subject assignment and term.")
            if pair["assignment_session"] != pair["term_session"]:
                raise ValueError("Assessment components must use a term from the assignment's session.")
            if academic_session_id is not None and pair["assignment_session"] != academic_session_id:
                raise ValueError("Select a class-subject assignment and term from the selected session.")
            components = conn.execute(
                """SELECT id,class_subject_assignment_id,term_id,component_name,
                          component_order,max_score
                   FROM assessment_components
                   WHERE class_subject_assignment_id=? AND term_id=?
                   ORDER BY component_order,id""",
                (class_subject_assignment_id, term_id),
            ).fetchall()
    return {
        "sessions": sessions,
        "assignments": assignments,
        "terms": terms,
        "components": components,
    }


def load_grade_band_setup_data(
    academic_session_id: int | None = None, educational_stage: str | None = None
) -> dict[str, list]:
    """Load sessions and one session/stage grade scale for the setup screen."""
    if educational_stage is not None and educational_stage not in EDUCATIONAL_STAGES:
        raise ValueError("Select Primary or Secondary.")
    with connection() as conn:
        sessions = conn.execute(
            "SELECT id,name,starts_on,ends_on,is_current FROM academic_sessions ORDER BY is_current DESC,starts_on DESC,id DESC"
        ).fetchall()
        bands = []
        if academic_session_id is not None and educational_stage is not None:
            bands = conn.execute(
                """SELECT id,academic_session_id,educational_stage,threshold,grade_label,remark
                   FROM assessment_grade_bands
                   WHERE academic_session_id=? AND educational_stage=?
                   ORDER BY id""",
                (academic_session_id, educational_stage),
            ).fetchall()
        bands = sorted(bands, key=lambda row: (Decimal(row["threshold"]), int(row["id"])))
    return {"sessions": sessions, "bands": bands}


def save_assessment_grade_band(
    academic_session_id: int,
    educational_stage: str,
    threshold,
    grade_label: str,
    remark: str,
    actor: str,
    band_id: int | None = None,
) -> int:
    """Save one grade threshold; threshold zero anchors a complete 0–100 scale."""
    if educational_stage not in EDUCATIONAL_STAGES:
        raise ValueError("Select Primary or Secondary.")
    threshold_text = validate_grade_threshold(threshold)
    grade_label = grade_label.strip()
    remark = remark.strip()
    if not grade_label or len(grade_label) > 30:
        raise ValueError("Grade label must contain between 1 and 30 characters.")
    if len(remark) > 120:
        raise ValueError("Grade remark may contain at most 120 characters.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        if not conn.execute(
            "SELECT 1 FROM academic_sessions WHERE id=?", (academic_session_id,)
        ).fetchone():
            raise ValueError("Select an existing academic session.")
        if conn.execute(
            "SELECT 1 FROM assessment_policy_snapshots WHERE academic_session_id=? AND educational_stage=? LIMIT 1",
            (academic_session_id, educational_stage),
        ).fetchone():
            raise ValueError("This grade scale is locked because draft scores use its saved policy snapshot.")
        current_band = None if band_id is None else conn.execute(
            "SELECT academic_session_id,educational_stage,threshold "
            "FROM assessment_grade_bands WHERE id=?", (band_id,)
        ).fetchone()
        if band_id is not None and current_band is None:
            raise ValueError("Grade band was not found.")
        existing = conn.execute(
            """SELECT id,threshold FROM assessment_grade_bands
               WHERE academic_session_id=? AND educational_stage=?
                 AND (? IS NULL OR id<>?)""",
            (academic_session_id, educational_stage, band_id, band_id),
        ).fetchall()
        owns_zero = bool(
            current_band
            and current_band["academic_session_id"] == academic_session_id
            and current_band["educational_stage"] == educational_stage
            and current_band["threshold"] == "0"
        )
        if not existing and threshold_text != "0":
            raise ValueError("The first grade band must start at 0.")
        if owns_zero and threshold_text != "0" and existing:
            raise ValueError("Remove the other grade bands before changing the 0 threshold.")
        if existing and threshold_text == "0" and not owns_zero:
            raise ValueError("This scale already has its 0 threshold.")
        if any(validate_grade_threshold(row["threshold"]) == threshold_text for row in existing):
            raise ValueError("That threshold is already in use for this session and school stage.")
        duplicate_label = conn.execute(
            """SELECT id FROM assessment_grade_bands
               WHERE academic_session_id=? AND educational_stage=?
                 AND grade_label=? COLLATE NOCASE AND (? IS NULL OR id<>?)""",
            (academic_session_id, educational_stage, grade_label, band_id, band_id),
        ).fetchone()
        if duplicate_label:
            raise ValueError("That grade label is already in use for this session and school stage.")
        if band_id is None:
            cursor = conn.execute(
                "INSERT INTO assessment_grade_bands "
                "(academic_session_id,educational_stage,threshold,grade_label,remark) VALUES(?,?,?,?,?)",
                (academic_session_id, educational_stage, threshold_text, grade_label, remark),
            )
            band_id = int(cursor.lastrowid)
            action = "CREATE"
        else:
            cursor = conn.execute(
                "UPDATE assessment_grade_bands SET academic_session_id=?,educational_stage=?,"
                "threshold=?,grade_label=?,remark=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (academic_session_id, educational_stage, threshold_text, grade_label, remark, band_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Grade band was not found.")
            action = "UPDATE"
        _audit_in_transaction(conn, actor, action, "assessment_grade_bands", band_id)
        return band_id


def delete_assessment_grade_band(band_id: int, actor: str) -> None:
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        band = conn.execute(
            "SELECT academic_session_id,educational_stage,threshold "
            "FROM assessment_grade_bands WHERE id=?",
            (band_id,),
        ).fetchone()
        if band is None:
            raise ValueError("Grade band was not found.")
        if conn.execute(
            "SELECT 1 FROM assessment_policy_snapshots WHERE academic_session_id=? AND educational_stage=? LIMIT 1",
            (band["academic_session_id"], band["educational_stage"]),
        ).fetchone():
            raise ValueError("This grade scale is locked because draft scores use its saved policy snapshot.")
        if band["threshold"] == "0":
            count = conn.execute(
                "SELECT COUNT(*) FROM assessment_grade_bands "
                "WHERE academic_session_id=? AND educational_stage=? AND id<>?",
                (band["academic_session_id"], band["educational_stage"], band_id),
            ).fetchone()[0]
            if count:
                raise ValueError("Remove the other grade bands before removing the 0 threshold.")
        conn.execute("DELETE FROM assessment_grade_bands WHERE id=?", (band_id,))
        _audit_in_transaction(conn, actor, "DELETE", "assessment_grade_bands", band_id)


def save_assessment_component(
    class_subject_assignment_id: int,
    term_id: int,
    component_name: str,
    component_order: int,
    max_score,
    actor: str,
    component_id: int | None = None,
) -> int:
    component_name = component_name.strip()
    if not component_name or len(component_name) > 80:
        raise ValueError("Component name must contain between 1 and 80 characters.")
    if isinstance(component_order, bool) or not isinstance(component_order, int) or not 1 <= component_order <= 99:
        raise ValueError("Component order must be a whole number from 1 to 99.")
    max_value = _decimal_value(max_score, "Maximum score")
    if max_value <= 0 or max_value > Decimal("1000000"):
        raise ValueError("Maximum score must be greater than zero and no more than 1000000.")
    max_score_text = _decimal_text(max_value)
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        pair = conn.execute(
            """SELECT x.academic_session_id AS assignment_session,
                      t.academic_session_id AS term_session
               FROM class_subject_assignments x JOIN terms t ON t.id=? WHERE x.id=?""",
            (term_id, class_subject_assignment_id),
        ).fetchone()
        if pair is None:
            raise ValueError("Select an existing class-subject assignment and term.")
        if pair["assignment_session"] != pair["term_session"]:
            raise ValueError("Assessment components must use a term from the assignment's session.")
        if conn.execute(
            "SELECT 1 FROM assessment_policy_snapshots WHERE class_subject_assignment_id=? AND term_id=?",
            (class_subject_assignment_id, term_id),
        ).fetchone():
            raise ValueError("These components are locked because draft scores use their saved policy snapshot.")
        duplicate = conn.execute(
            """SELECT id FROM assessment_components
               WHERE class_subject_assignment_id=? AND term_id=?
                 AND component_order=? AND (? IS NULL OR id<>?)""",
            (class_subject_assignment_id, term_id, component_order, component_id, component_id),
        ).fetchone()
        if duplicate:
            raise ValueError("That component order is already in use for this term and subject.")
        duplicate_name = conn.execute(
            """SELECT id FROM assessment_components
               WHERE class_subject_assignment_id=? AND term_id=?
                 AND component_name=? COLLATE NOCASE AND (? IS NULL OR id<>?)""",
            (class_subject_assignment_id, term_id, component_name, component_id, component_id),
        ).fetchone()
        if duplicate_name:
            raise ValueError("That component name is already in use for this term and subject.")
        if component_id is None:
            cursor = conn.execute(
                "INSERT INTO assessment_components "
                "(class_subject_assignment_id,term_id,component_name,component_order,max_score) "
                "VALUES(?,?,?,?,?)",
                (class_subject_assignment_id, term_id, component_name, component_order, max_score_text),
            )
            component_id = int(cursor.lastrowid)
            action = "CREATE"
        else:
            cursor = conn.execute(
                "UPDATE assessment_components SET class_subject_assignment_id=?,term_id=?,"
                "component_name=?,component_order=?,max_score=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (class_subject_assignment_id, term_id, component_name, component_order, max_score_text, component_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Assessment component was not found.")
            action = "UPDATE"
        _audit_in_transaction(conn, actor, action, "assessment_components", component_id)
        return component_id


def setup_ca_exam_components(
    class_subject_assignment_id: int,
    term_id: int,
    ca_max_score,
    exam_max_score,
    actor: str,
) -> tuple[int, int]:
    """Create the standard CA and exam score columns atomically for one class subject and term."""
    maxima = (
        _decimal_value(ca_max_score, "CA maximum score"),
        _decimal_value(exam_max_score, "Exam maximum score"),
    )
    if any(value <= 0 or value > Decimal("1000000") for value in maxima):
        raise ValueError("CA and exam maxima must be greater than zero and no more than 1000000.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        pair = conn.execute(
            """SELECT x.academic_session_id AS assignment_session,
                      x.id,t.academic_session_id AS term_session,c.educational_stage
               FROM class_subject_assignments x
               JOIN classes c ON c.id=x.class_id JOIN terms t ON t.id=?
               WHERE x.id=?""",
            (term_id, class_subject_assignment_id),
        ).fetchone()
        if pair is None:
            raise ValueError("Select an existing class subject and term.")
        if pair["assignment_session"] != pair["term_session"]:
            raise ValueError("Assessment components must use a term from the assignment's session.")
        if not pair["educational_stage"]:
            raise ValueError("Set the class stage to Primary or Secondary before configuring scores.")
        if conn.execute(
            "SELECT 1 FROM assessment_policy_snapshots WHERE class_subject_assignment_id=? AND term_id=?",
            (class_subject_assignment_id, term_id),
        ).fetchone():
            raise ValueError("These scores are locked by an existing draft result snapshot.")
        existing = conn.execute(
            """SELECT id,component_name,max_score FROM assessment_components
               WHERE class_subject_assignment_id=? AND term_id=? ORDER BY component_order,id""",
            (class_subject_assignment_id, term_id),
        ).fetchall()
        if existing:
            expected = [
                ("CA Score", _decimal_text(maxima[0])),
                ("Exam Score", _decimal_text(maxima[1])),
            ]
            actual = [(row["component_name"], row["max_score"]) for row in existing]
            if actual == expected:
                return tuple(int(row["id"]) for row in existing)
            raise ValueError("Score columns already exist. Edit them in Assessment Setup.")
        component_ids = []
        for order, (name, maximum) in enumerate(zip(("CA Score", "Exam Score"), maxima), start=1):
            cursor = conn.execute(
                """INSERT INTO assessment_components
                   (class_subject_assignment_id,term_id,component_name,component_order,max_score)
                   VALUES(?,?,?,?,?)""",
                (class_subject_assignment_id, term_id, name, order, _decimal_text(maximum)),
            )
            component_id = int(cursor.lastrowid)
            component_ids.append(component_id)
            _audit_in_transaction(conn, actor, "CREATE", "assessment_components", component_id)
        return component_ids[0], component_ids[1]


def delete_assessment_component(component_id: int, actor: str) -> None:
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        scope = conn.execute(
            "SELECT class_subject_assignment_id,term_id FROM assessment_components WHERE id=?",
            (component_id,),
        ).fetchone()
        if scope is None:
            raise ValueError("Assessment component was not found.")
        if conn.execute(
            "SELECT 1 FROM assessment_policy_snapshots WHERE class_subject_assignment_id=? AND term_id=?",
            tuple(scope),
        ).fetchone():
            raise ValueError("This component is locked because draft scores use its saved policy snapshot.")
        cursor = conn.execute("DELETE FROM assessment_components WHERE id=?", (component_id,))
        if cursor.rowcount != 1:
            raise ValueError("Assessment component was not found.")
        _audit_in_transaction(conn, actor, "DELETE", "assessment_components", component_id)


def load_grade_entry_data(
    academic_session_id: int | None = None,
    class_subject_assignment_id: int | None = None,
    term_id: int | None = None,
    actor: str | None = None,
) -> dict[str, list | dict | None]:
    """Load score-entry selectors, roster, policy, saved drafts, and calculated previews."""
    with connection() as conn:
        conn.execute("BEGIN")
        teacher_scope_id = None
        teacher_linked = None
        account_role = None
        if actor is not None:
            account = conn.execute(
                "SELECT role,is_active,teacher_id FROM users WHERE username=? COLLATE NOCASE",
                (actor.strip(),),
            ).fetchone()
            if account is None or not account["is_active"]:
                raise PermissionError("An active user account is required to open grade entry.")
            account_role = account["role"]
            if account_role == "teacher":
                teacher_id = account["teacher_id"]
                teacher_linked = bool(teacher_id and conn.execute(
                    "SELECT 1 FROM teachers WHERE id=? AND status COLLATE NOCASE='Active'", (teacher_id,)
                ).fetchone())
                if teacher_linked:
                    teacher_scope_id = int(teacher_id)
            elif account_role != "admin":
                raise PermissionError("Only administrators and linked teachers may open grade entry.")
        if account_role == "teacher":
            sessions = conn.execute(
                """SELECT a.id,a.name,a.starts_on,a.ends_on,a.is_current
                   FROM academic_sessions a
                   WHERE EXISTS (SELECT 1 FROM class_subject_assignments x
                                 WHERE x.academic_session_id=a.id AND x.teacher_id=?)
                   ORDER BY a.is_current DESC,a.starts_on DESC,a.id DESC""",
                (teacher_scope_id or -1,),
            ).fetchall()
        else:
            sessions = conn.execute(
                "SELECT id,name,starts_on,ends_on,is_current FROM academic_sessions ORDER BY is_current DESC,starts_on DESC,id DESC"
            ).fetchall()
        assignments = []
        terms = []
        components = []
        bands = []
        students = []
        snapshot_id = None
        approval = None
        if academic_session_id is not None:
            if account_role == "teacher":
                assignments = conn.execute(
                    """SELECT x.id,x.academic_session_id,x.class_id,c.class_name,c.section,
                              c.educational_stage,s.subject_code,s.subject_name
                       FROM class_subject_assignments x
                       JOIN classes c ON c.id=x.class_id
                       JOIN subjects s ON s.id=x.subject_id
                       WHERE x.academic_session_id=? AND x.teacher_id=?
                       ORDER BY c.class_name,c.section,s.subject_name,s.subject_code""",
                    (academic_session_id, teacher_scope_id or -1),
                ).fetchall()
            else:
                assignments = conn.execute(
                    """SELECT x.id,x.academic_session_id,x.class_id,c.class_name,c.section,
                              c.educational_stage,s.subject_code,s.subject_name
                       FROM class_subject_assignments x
                       JOIN classes c ON c.id=x.class_id
                       JOIN subjects s ON s.id=x.subject_id
                       WHERE x.academic_session_id=?
                       ORDER BY c.class_name,c.section,s.subject_name,s.subject_code""",
                    (academic_session_id,),
                ).fetchall()
            terms = conn.execute(
                "SELECT id,academic_session_id,term_name,term_order,starts_on,ends_on,is_current "
                "FROM terms WHERE academic_session_id=? ORDER BY is_current DESC,term_order,id",
                (academic_session_id,),
            ).fetchall()
        if class_subject_assignment_id is not None and term_id is not None:
            assignment = conn.execute(
                """SELECT x.id,x.academic_session_id,x.class_id,x.teacher_id,c.educational_stage
                   FROM class_subject_assignments x JOIN classes c ON c.id=x.class_id WHERE x.id=?""",
                (class_subject_assignment_id,),
            ).fetchone()
            term = conn.execute(
                "SELECT id,academic_session_id,starts_on,ends_on FROM terms WHERE id=?", (term_id,)
            ).fetchone()
            if assignment is None or term is None:
                raise ValueError("Select an existing class-subject assignment and term.")
            if account_role == "teacher" and (
                teacher_scope_id is None or assignment["teacher_id"] != teacher_scope_id
            ):
                raise PermissionError("You can only open grade entry for subjects assigned to your teacher account.")
            if assignment["academic_session_id"] != term["academic_session_id"]:
                raise ValueError("Assessment assignment and term must belong to the same academic session.")
            if academic_session_id is not None and assignment["academic_session_id"] != academic_session_id:
                raise ValueError("Assessment assignment is outside the selected session.")
            if not assignment["educational_stage"]:
                raise ValueError("Assign this class to Primary or Secondary before entering scores.")
            snapshot = conn.execute(
                "SELECT id,policy_json FROM assessment_policy_snapshots "
                "WHERE class_subject_assignment_id=? AND term_id=?",
                (class_subject_assignment_id, term_id),
            ).fetchone()
            if snapshot:
                snapshot_id = int(snapshot["id"])
                policy = json.loads(snapshot["policy_json"])
                components = policy["components"]
                bands = policy["bands"]
                approval_row = conn.execute(
                    "SELECT approved_by,approved_at FROM assessment_result_approvals WHERE snapshot_id=?",
                    (snapshot_id,),
                ).fetchone()
                approval = dict(approval_row) if approval_row else None
            else:
                components = [dict(row) for row in conn.execute(
                    """SELECT id,component_name,component_order,max_score FROM assessment_components
                       WHERE class_subject_assignment_id=? AND term_id=? ORDER BY component_order,id""",
                    (class_subject_assignment_id, term_id),
                )]
                bands = [dict(row) for row in conn.execute(
                    """SELECT threshold,grade_label,remark FROM assessment_grade_bands
                       WHERE academic_session_id=? AND educational_stage=? ORDER BY id""",
                    (assignment["academic_session_id"], assignment["educational_stage"]),
                )]
                bands.sort(key=lambda item: Decimal(item["threshold"]))
            if snapshot_id is not None and "roster_enrollment_ids" in policy:
                roster_ids = [int(value) for value in policy["roster_enrollment_ids"]]
                if roster_ids:
                    placeholders = ",".join("?" for _ in roster_ids)
                    students = [dict(row) for row in conn.execute(
                        f"""SELECT e.id AS enrollment_id,s.admission_no,s.first_name,s.last_name
                            FROM student_enrollments e JOIN students s ON s.id=e.student_id
                            WHERE e.id IN ({placeholders})
                            ORDER BY s.last_name COLLATE NOCASE,s.first_name COLLATE NOCASE,s.id,e.id""",
                        roster_ids,
                    )]
            else:
                students = [dict(row) for row in conn.execute(
                    """SELECT e.id AS enrollment_id,s.admission_no,s.first_name,s.last_name
                       FROM student_enrollments e JOIN students s ON s.id=e.student_id
                       WHERE e.academic_session_id=? AND e.class_id=?
                         AND e.enrolled_on<=? AND (e.left_on IS NULL OR e.left_on>=?)
                         AND s.is_active=1
                       ORDER BY s.last_name COLLATE NOCASE,s.first_name COLLATE NOCASE,s.id,e.id""",
                    (assignment["academic_session_id"], assignment["class_id"], term["ends_on"], term["starts_on"]),
                )]
            if snapshot_id is not None:
                by_enrollment = {}
                for row in conn.execute(
                    "SELECT enrollment_id,component_id,score_text FROM assessment_scores WHERE snapshot_id=?",
                    (snapshot_id,),
                ):
                    by_enrollment.setdefault(int(row["enrollment_id"]), {})[int(row["component_id"])] = row["score_text"]
                for student in students:
                    score_map = by_enrollment.get(int(student["enrollment_id"]), {})
                    student["scores"] = score_map
                    student["result"] = calculate_assessment_result(components, score_map, bands) if components else None
            else:
                for student in students:
                    student["scores"] = {}
                    student["result"] = None
    return {
        "sessions": sessions, "assignments": assignments, "terms": terms,
        "components": components, "bands": bands, "students": students,
        "snapshot_id": snapshot_id, "approval": approval,
        "teacher_linked": teacher_linked,
    }


def save_assessment_scores(
    class_subject_assignment_id: int,
    term_id: int,
    student_scores: dict[int, dict[int, str | None]],
    actor: str,
    correction_reason: str = "",
) -> tuple[int, int]:
    """Save editable draft marks, freezing the configured components and grade bands on first save."""
    if not isinstance(student_scores, dict) or not student_scores:
        raise ValueError("Enter at least one assessment score before saving.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute(
            "SELECT username,role,is_active,teacher_id FROM users WHERE username=? COLLATE NOCASE",
            (actor.strip() if isinstance(actor, str) else "",),
        ).fetchone()
        if user is None or not user["is_active"]:
            raise PermissionError("An active user account is required to save assessment scores.")
        is_admin = user["role"] == "admin"
        if is_admin:
            actor = _require_admin(conn, user["username"])
        elif user["role"] == "teacher":
            actor = user["username"]
            if user["teacher_id"] is None or not conn.execute(
                "SELECT 1 FROM teachers WHERE id=? AND status COLLATE NOCASE='Active'",
                (user["teacher_id"],),
            ).fetchone():
                raise PermissionError("Link this teacher login to an active teacher record before entering scores.")
        else:
            raise PermissionError("Only administrators and assigned teachers may save draft scores.")
        assignment = conn.execute(
            """SELECT x.academic_session_id,x.class_id,x.teacher_id,c.educational_stage
               FROM class_subject_assignments x JOIN classes c ON c.id=x.class_id WHERE x.id=?""",
            (class_subject_assignment_id,),
        ).fetchone()
        term = conn.execute(
            "SELECT academic_session_id,starts_on,ends_on FROM terms WHERE id=?", (term_id,)
        ).fetchone()
        if assignment is None or term is None or assignment["academic_session_id"] != term["academic_session_id"]:
            raise ValueError("Select a class-subject assignment and term from the same session.")
        if not is_admin and assignment["teacher_id"] != user["teacher_id"]:
            raise PermissionError("You can only enter scores for subjects assigned to your teacher account.")
        if not assignment["educational_stage"]:
            raise ValueError("Assign this class to Primary or Secondary before entering scores.")
        snapshot = conn.execute(
            "SELECT id,policy_json FROM assessment_policy_snapshots "
            "WHERE class_subject_assignment_id=? AND term_id=?",
            (class_subject_assignment_id, term_id),
        ).fetchone()
        snapshot_created = snapshot is None
        approval = None if snapshot is None else conn.execute(
            "SELECT approved_by FROM assessment_result_approvals WHERE snapshot_id=?",
            (int(snapshot["id"]),),
        ).fetchone()
        correction_reason = correction_reason.strip()
        if not is_admin and (approval or correction_reason):
            raise PermissionError("Teachers may enter draft scores only; approved results require administrator correction.")
        if approval and not correction_reason:
            raise ValueError("A reason is required to correct approved results.")
        if len(correction_reason) > 300:
            raise ValueError("Correction reason must contain at most 300 characters.")
        if snapshot is None:
            components = [dict(row) for row in conn.execute(
                """SELECT id,component_name,component_order,max_score FROM assessment_components
                   WHERE class_subject_assignment_id=? AND term_id=? ORDER BY component_order,id""",
                (class_subject_assignment_id, term_id),
            )]
            bands = [dict(row) for row in conn.execute(
                """SELECT threshold,grade_label,remark FROM assessment_grade_bands
                   WHERE academic_session_id=? AND educational_stage=? ORDER BY id""",
                (assignment["academic_session_id"], assignment["educational_stage"]),
            )]
            bands.sort(key=lambda item: Decimal(item["threshold"]))
            if not components:
                raise ValueError("Define assessment components before entering scores.")
            if not bands or Decimal(str(bands[0]["threshold"])) != 0:
                raise ValueError("Configure a grade scale beginning at 0 before entering scores.")
            roster = [int(row["id"]) for row in conn.execute(
                """SELECT id FROM student_enrollments
                   WHERE academic_session_id=? AND class_id=?
                     AND enrolled_on<=? AND (left_on IS NULL OR left_on>=?)
                   ORDER BY id""",
                (assignment["academic_session_id"], assignment["class_id"], term["ends_on"], term["starts_on"]),
            )]
            if not roster:
                raise ValueError("No students are enrolled in this class during the selected term.")
            # Validate grade labels and the component maximum data before freezing the policy.
            for item in components:
                _decimal_value(item["max_score"], "Maximum score")
            policy = {
                "calculation": "points_ratio_v1",
                "missing": "incomplete",
                "display_rounding": "2_decimal_half_up",
                "grade_thresholds_use": "unrounded_percentage",
                "components": components,
                "bands": bands,
                "roster_enrollment_ids": roster,
            }
            cursor = conn.execute(
                """INSERT INTO assessment_policy_snapshots
                   (class_subject_assignment_id,term_id,academic_session_id,educational_stage,policy_json,created_by)
                   VALUES(?,?,?,?,?,?)""",
                (class_subject_assignment_id, term_id, assignment["academic_session_id"],
                 assignment["educational_stage"], json.dumps(policy, separators=(",", ":")), actor),
            )
            snapshot_id = int(cursor.lastrowid)
            _audit_in_transaction(conn, actor, "SNAPSHOT", "assessment_policy_snapshots", snapshot_id)
        else:
            snapshot_id = int(snapshot["id"])
            policy = json.loads(snapshot["policy_json"])
            components = policy["components"]
        component_by_id = {int(item["id"]): item for item in components}
        if not component_by_id:
            raise ValueError("The saved policy snapshot has no assessment components.")
        roster = set(int(value) for value in policy.get("roster_enrollment_ids", ()))
        if not roster:
            roster = {
                int(row["id"])
                for row in conn.execute(
                    """SELECT id FROM student_enrollments
                       WHERE academic_session_id=? AND class_id=?
                         AND enrolled_on<=? AND (left_on IS NULL OR left_on>=?)""",
                    (assignment["academic_session_id"], assignment["class_id"], term["ends_on"], term["starts_on"]),
                )
            }
        changed_fields = 0
        for enrollment_id, scores in student_scores.items():
            if isinstance(enrollment_id, bool) or not isinstance(enrollment_id, int) or enrollment_id not in roster:
                raise ValueError("A selected student is not enrolled in this class during the term.")
            if not isinstance(scores, dict):
                raise ValueError("Assessment scores must be provided by component.")
            for component_id, value in scores.items():
                if isinstance(component_id, bool) or not isinstance(component_id, int) or component_id not in component_by_id:
                    raise ValueError("A selected assessment component is outside this saved policy.")
                previous = conn.execute(
                    "SELECT score_text FROM assessment_scores WHERE snapshot_id=? AND enrollment_id=? AND component_id=?",
                    (snapshot_id, enrollment_id, component_id),
                ).fetchone()
                previous_value = previous["score_text"] if previous else None
                if value is None or (isinstance(value, str) and not value.strip()):
                    if previous is None:
                        continue
                    conn.execute(
                        "DELETE FROM assessment_scores WHERE snapshot_id=? AND enrollment_id=? AND component_id=?",
                        (snapshot_id, enrollment_id, component_id),
                    )
                    new_value = None
                else:
                    new_value = validate_assessment_score(value, component_by_id[component_id]["max_score"])
                    if previous_value == new_value:
                        continue
                    conn.execute(
                        """INSERT INTO assessment_scores(snapshot_id,enrollment_id,component_id,score_text,updated_by)
                           VALUES(?,?,?,?,?) ON CONFLICT(snapshot_id,enrollment_id,component_id)
                           DO UPDATE SET score_text=excluded.score_text,updated_by=excluded.updated_by,
                             updated_at=CURRENT_TIMESTAMP""",
                        (snapshot_id, enrollment_id, component_id, new_value, actor),
                    )
                conn.execute(
                    """INSERT INTO assessment_score_history
                       (snapshot_id,enrollment_id,component_id,previous_score,new_score,actor,reason)
                       VALUES(?,?,?,?,?,?,?)""",
                    (snapshot_id, enrollment_id, component_id, previous_value, new_value, actor,
                     correction_reason if approval else ""),
                )
                changed_fields += 1
        if snapshot_created and conn.execute(
            "SELECT 1 FROM assessment_scores WHERE snapshot_id=? LIMIT 1", (snapshot_id,)
        ).fetchone() is None:
            raise ValueError("Enter at least one assessment score before saving.")
        if approval:
            current_marks: dict[int, dict[int, str]] = {}
            for row in conn.execute(
                "SELECT enrollment_id,component_id,score_text FROM assessment_scores WHERE snapshot_id=?",
                (snapshot_id,),
            ):
                current_marks.setdefault(int(row["enrollment_id"]), {})[int(row["component_id"])] = row["score_text"]
            incomplete = [
                enrollment_id for enrollment_id in policy.get("roster_enrollment_ids", [])
                if not component_by_id.keys() <= current_marks.get(int(enrollment_id), {}).keys()
            ]
            if incomplete:
                raise ValueError("Approved results must remain complete; this correction would leave a missing score.")
        _audit_in_transaction(conn, actor, "SAVE", "assessment_scores", snapshot_id)
        return snapshot_id, changed_fields


def approve_assessment_results(snapshot_id: int, actor: str) -> tuple[str, str]:
    """Approve and lock one complete class-subject/term result set."""
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        snapshot = conn.execute(
            "SELECT policy_json FROM assessment_policy_snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()
        if snapshot is None:
            raise ValueError("Assessment policy snapshot was not found.")
        if conn.execute(
            "SELECT 1 FROM assessment_result_approvals WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone():
            raise ValueError("These assessment results are already approved and locked.")
        policy = json.loads(snapshot["policy_json"])
        component_ids = {int(item["id"]) for item in policy["components"]}
        roster = [int(value) for value in policy.get("roster_enrollment_ids", [])]
        if not component_ids or not roster:
            raise ValueError("The saved result set has no components or enrolled students to approve.")
        marks: dict[int, dict[int, str]] = {}
        for row in conn.execute(
            "SELECT enrollment_id,component_id,score_text FROM assessment_scores WHERE snapshot_id=?",
            (snapshot_id,),
        ):
            marks.setdefault(int(row["enrollment_id"]), {})[int(row["component_id"])] = row["score_text"]
        incomplete = []
        for enrollment_id in roster:
            current = marks.get(enrollment_id, {})
            if not component_ids.issubset(current):
                incomplete.append(enrollment_id)
        if incomplete:
            raise ValueError(f"Cannot approve results: {len(incomplete)} student result(s) are incomplete.")
        conn.execute(
            "INSERT INTO assessment_result_approvals(snapshot_id,approved_by) VALUES(?,?)",
            (snapshot_id, actor),
        )
        _audit_in_transaction(conn, actor, "APPROVE", "assessment_policy_snapshots", snapshot_id)
        row = conn.execute(
            "SELECT approved_by,approved_at FROM assessment_result_approvals WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        return row["approved_by"], row["approved_at"]


def load_report_card_data(
    academic_session_id: int, class_id: int, term_id: int, enrollment_id: int,
    class_positions: dict[int, dict] | None = None,
) -> dict:
    """Load an individual report card only when every class subject is approved and complete.

    The card also carries the student's overall average and, when the whole class has
    complete approved results with one consistent roster, the class position. Pass
    ``class_positions`` (enrollment id -> position) to reuse positions already computed
    for the class; otherwise they are derived here.
    """
    with connection() as conn:
        assignment_rows = conn.execute(
            """SELECT x.id,s.subject_code,s.subject_name
               FROM class_subject_assignments x JOIN subjects s ON s.id=x.subject_id
               WHERE x.academic_session_id=? AND x.class_id=? ORDER BY s.subject_name,s.subject_code""",
            (academic_session_id, class_id),
        ).fetchall()
        term = conn.execute(
            "SELECT academic_session_id,term_name,term_order FROM terms WHERE id=?", (term_id,)
        ).fetchone()
        session = conn.execute(
            "SELECT name FROM academic_sessions WHERE id=?", (academic_session_id,)
        ).fetchone()
        class_row = conn.execute(
            "SELECT class_name,section,educational_stage FROM classes WHERE id=?", (class_id,)
        ).fetchone()
        if not term or not session or not class_row or term["academic_session_id"] != academic_session_id:
            raise ValueError("Select a valid session, class, and term.")
        if not assignment_rows:
            raise ValueError("No class subjects are assigned for this academic session.")
        enrollment = conn.execute(
            """SELECT e.id,s.admission_no,s.first_name,s.last_name,s.photo_data
               FROM student_enrollments e JOIN students s ON s.id=e.student_id
               WHERE e.id=? AND e.academic_session_id=? AND e.class_id=?""",
            (enrollment_id, academic_session_id, class_id),
        ).fetchone()
        if enrollment is None:
            raise ValueError("Select a student enrolled in this class and academic session.")
        subject_results = []
        subject_bands = []
        for assignment in assignment_rows:
            snapshot = conn.execute(
                """SELECT p.id,p.policy_json,a.approved_by,a.approved_at
                   FROM assessment_policy_snapshots p
                   LEFT JOIN assessment_result_approvals a ON a.snapshot_id=p.id
                   WHERE p.class_subject_assignment_id=? AND p.term_id=?""",
                (assignment["id"], term_id),
            ).fetchone()
            if snapshot is None or snapshot["approved_by"] is None:
                raise ValueError(f"{assignment['subject_name']} results are not approved yet.")
            policy = json.loads(snapshot["policy_json"])
            if enrollment_id not in [int(value) for value in policy.get("roster_enrollment_ids", [])]:
                raise ValueError(f"{assignment['subject_name']} has no result for this student.")
            scores = {
                int(row["component_id"]): row["score_text"]
                for row in conn.execute(
                    "SELECT component_id,score_text FROM assessment_scores WHERE snapshot_id=? AND enrollment_id=?",
                    (snapshot["id"], enrollment_id),
                )
            }
            result = calculate_assessment_result(policy["components"], scores, policy["bands"])
            if not result["complete"]:
                raise ValueError(f"{assignment['subject_name']} result is incomplete and cannot be reported.")
            subject_bands.append(policy["bands"])
            subject_results.append({
                "subject_code": assignment["subject_code"],
                "subject_name": assignment["subject_name"],
                **result,
            })
        card = {
            "student": dict(enrollment),
            "academic_session": session["name"],
            "term_name": term["term_name"],
            "term_order": int(term["term_order"]),
            "class_name": class_row["class_name"],
            "section": class_row["section"],
            "subjects": subject_results,
            "overall": calculate_overall_average(subject_results, _common_grade_bands(subject_bands)),
            "approval_status": "Approved",
            "educational_stage": EDUCATIONAL_STAGES.get(class_row["educational_stage"], ""),
            "grade_key": grade_key_entries(_common_grade_bands(subject_bands)),
            "report_settings": _report_settings_in_transaction(conn),
            "school_profile": _school_profile_in_transaction(conn),
            "school_logo": _school_logo_in_transaction(conn),
        }
    if class_positions is None:
        try:
            master = load_master_sheet_data(academic_session_id, class_id, term_id)
            class_positions = {int(item["enrollment_id"]): item["position"] for item in master["students"]}
        except ValueError:
            class_positions = {}  # The class is not fully approved and consistent: no position is shown.
    card["position"] = class_positions.get(int(enrollment_id))
    return card


def load_class_report_cards_data(academic_session_id: int, class_id: int, term_id: int) -> dict:
    """Load approved individual report cards for one class, requiring a consistent frozen roster."""
    master = load_master_sheet_data(academic_session_id, class_id, term_id)
    positions = {int(student["enrollment_id"]): student["position"] for student in master["students"]}
    cards = [
        load_report_card_data(
            academic_session_id, class_id, term_id, int(student["enrollment_id"]), class_positions=positions
        )
        for student in master["students"]
    ]
    return {"report_cards": cards}


def load_student_profile_data(student_id: int, actor: str) -> dict:
    """Return a read-only student overview with enrollment, approved results, attendance, and fees."""
    with connection() as conn:
        conn.execute("BEGIN")
        _require_admin(conn, actor)
        student = conn.execute(
            "SELECT id,admission_no,first_name,last_name,gender,guardian_phone,nin,date_of_birth,email,is_active,photo_data,created_at FROM students WHERE id=?",
            (student_id,),
        ).fetchone()
        if student is None:
            raise ValueError("Select an existing student.")
        enrollments = [dict(row) for row in conn.execute(
            """SELECT e.id,e.status,e.enrolled_on,e.left_on,c.class_name,c.section,a.name AS session_name
               FROM student_enrollments e JOIN classes c ON c.id=e.class_id
               JOIN academic_sessions a ON a.id=e.academic_session_id
               WHERE e.student_id=? ORDER BY a.starts_on DESC,e.id DESC""", (student_id,)
        )]
        enrollment_ids = {int(row["id"]) for row in enrollments}
        results = []
        if enrollment_ids:
            snapshots = conn.execute(
                """SELECT p.id,p.policy_json,p.academic_session_id,t.term_name,t.term_order,a.name AS session_name,
                          c.class_name,c.section,s.subject_code,s.subject_name
                   FROM assessment_policy_snapshots p
                   JOIN assessment_result_approvals ap ON ap.snapshot_id=p.id
                   JOIN terms t ON t.id=p.term_id JOIN academic_sessions a ON a.id=p.academic_session_id
                   JOIN class_subject_assignments x ON x.id=p.class_subject_assignment_id
                   JOIN classes c ON c.id=x.class_id JOIN subjects s ON s.id=x.subject_id
                   ORDER BY a.starts_on DESC,t.term_order DESC,s.subject_name"""
            ).fetchall()
            for snapshot in snapshots:
                policy = json.loads(snapshot["policy_json"])
                roster = enrollment_ids.intersection(int(value) for value in policy.get("roster_enrollment_ids", []))
                for enrollment_id in roster:
                    scores = {
                        int(row["component_id"]): row["score_text"]
                        for row in conn.execute(
                            "SELECT component_id,score_text FROM assessment_scores WHERE snapshot_id=? AND enrollment_id=?",
                            (snapshot["id"], enrollment_id),
                        )
                    }
                    result = calculate_assessment_result(policy["components"], scores, policy["bands"])
                    results.append({
                        "session": snapshot["session_name"], "term": snapshot["term_name"],
                        "class": " ".join((snapshot["class_name"], snapshot["section"])).strip(),
                        "subject": f"{snapshot['subject_code']} — {snapshot['subject_name']}",
                        "complete": result["complete"], "percent": result["display_percentage"],
                        "grade": result["grade_label"] or "", "remark": result["remark"] or "",
                    })
        attendance = {"present": 0, "absent": 0}
        invoices = []
        if enrollment_ids:
            placeholders = ",".join("?" for _ in enrollment_ids)
            params = tuple(sorted(enrollment_ids))
            for row in conn.execute(
                f"""SELECT m.status,COUNT(*) AS count FROM attendance_marks m
                    WHERE m.enrollment_id IN ({placeholders}) GROUP BY m.status""", params
            ):
                attendance[row["status"]] = int(row["count"])
            invoices = [dict(row) for row in conn.execute(
                f"""SELECT i.id,i.invoice_number,i.admission_no_snapshot,i.total_amount_text,
                           b.status,issue.issued_at,t.term_name,a.name AS session_name,c.class_name,c.section
                    FROM fee_invoices i JOIN fee_invoice_batches b ON b.id=i.batch_id
                    LEFT JOIN fee_invoice_issues issue ON issue.batch_id=b.id
                    JOIN terms t ON t.id=b.term_id JOIN academic_sessions a ON a.id=b.academic_session_id
                    JOIN classes c ON c.id=b.class_id WHERE i.enrollment_id IN ({placeholders})
                    ORDER BY i.id DESC""", params
            )]
            for invoice in invoices:
                gross = sum((Decimal(row[0]) for row in conn.execute(
                    "SELECT amount_text FROM fee_payments WHERE invoice_id=?", (invoice["id"],)
                )), Decimal("0"))
                reversed_amount = sum((Decimal(row[0]) for row in conn.execute(
                    """SELECT r.amount_text FROM fee_payment_reversals r
                       JOIN fee_payments p ON p.id=r.payment_id WHERE p.invoice_id=?""", (invoice["id"],)
                )), Decimal("0"))
                invoice["paid_amount"] = _decimal_text(gross - reversed_amount)
                invoice["balance_amount"] = _decimal_text(
                    Decimal(invoice["total_amount_text"]) - Decimal(invoice["paid_amount"])
                )
        return {
            "student": dict(student), "enrollments": enrollments,
            "approved_results": results, "attendance": attendance, "invoices": invoices,
        }


def _load_approved_subject_results(conn: sqlite3.Connection, assignment_id: int, term_id: int) -> dict:
    assignment = conn.execute(
        """SELECT x.id,x.academic_session_id,x.class_id,s.subject_code,s.subject_name
           FROM class_subject_assignments x JOIN subjects s ON s.id=x.subject_id WHERE x.id=?""",
        (assignment_id,),
    ).fetchone()
    snapshot = conn.execute(
        """SELECT p.id,p.policy_json,a.approved_by,a.approved_at
           FROM assessment_policy_snapshots p
           LEFT JOIN assessment_result_approvals a ON a.snapshot_id=p.id
           WHERE p.class_subject_assignment_id=? AND p.term_id=?""",
        (assignment_id, term_id),
    ).fetchone()
    if assignment is None or snapshot is None or snapshot["approved_by"] is None:
        raise ValueError("This class-subject result set is not approved.")
    policy = json.loads(snapshot["policy_json"])
    roster_ids = [int(value) for value in policy.get("roster_enrollment_ids", [])]
    if not roster_ids:
        raise ValueError("The approved result set has no saved student roster.")
    # Bound the lookup by the assignment scope, then apply the immutable roster in
    # Python. Avoid a variable-length IN clause, which hits SQLite's bind limit in
    # large classes and could accidentally accept a corrupted cross-class roster.
    scoped_students = conn.execute(
        """SELECT e.id AS enrollment_id,s.admission_no,s.first_name,s.last_name
           FROM student_enrollments e JOIN students s ON s.id=e.student_id
           WHERE e.academic_session_id=? AND e.class_id=?
           ORDER BY s.last_name COLLATE NOCASE,s.first_name COLLATE NOCASE,s.id,e.id""",
        (assignment["academic_session_id"], assignment["class_id"]),
    ).fetchall()
    roster_set = set(roster_ids)
    students = [dict(row) for row in scoped_students if int(row["enrollment_id"]) in roster_set]
    if len(students) != len(set(roster_ids)):
        raise ValueError("The approved result roster contains a missing enrollment.")
    marks: dict[int, dict[int, str]] = {}
    for row in conn.execute(
        "SELECT enrollment_id,component_id,score_text FROM assessment_scores WHERE snapshot_id=?",
        (snapshot["id"],),
    ):
        marks.setdefault(int(row["enrollment_id"]), {})[int(row["component_id"])] = row["score_text"]
    for student in students:
        result = calculate_assessment_result(
            policy["components"], marks.get(int(student["enrollment_id"]), {}), policy["bands"]
        )
        if not result["complete"]:
            raise ValueError("An approved result set contains an incomplete student result.")
        student["result"] = result
    return {
        "assignment": dict(assignment),
        "snapshot_id": int(snapshot["id"]),
        "approval": {"approved_by": snapshot["approved_by"], "approved_at": snapshot["approved_at"]},
        "roster_ids": set(roster_ids),
        "bands": policy["bands"],
        "students": students,
    }


def load_result_sheet_data(academic_session_id: int, class_id: int, term_id: int, assignment_id: int) -> dict:
    """Load a complete approved class result sheet for one subject and term."""
    with connection() as conn:
        term = conn.execute(
            "SELECT academic_session_id,term_name,term_order FROM terms WHERE id=?", (term_id,)
        ).fetchone()
        session = conn.execute("SELECT name FROM academic_sessions WHERE id=?", (academic_session_id,)).fetchone()
        class_row = conn.execute("SELECT class_name,section FROM classes WHERE id=?", (class_id,)).fetchone()
        assignment = conn.execute(
            "SELECT academic_session_id,class_id FROM class_subject_assignments WHERE id=?", (assignment_id,)
        ).fetchone()
        if not term or not session or not class_row or not assignment:
            raise ValueError("Select a valid session, class, subject, and term.")
        if term["academic_session_id"] != academic_session_id or assignment["academic_session_id"] != academic_session_id or assignment["class_id"] != class_id:
            raise ValueError("The selected subject and term must belong to the selected class and session.")
        result_set = _load_approved_subject_results(conn, assignment_id, term_id)
        return {
            "academic_session": session["name"], "term_name": term["term_name"],
            "term_order": int(term["term_order"]), "class_name": class_row["class_name"],
            "section": class_row["section"], "subject": result_set["assignment"],
            "students": result_set["students"], "approved_by": result_set["approval"]["approved_by"],
            "approved_at": result_set["approval"]["approved_at"],
            "school_profile": _school_profile_in_transaction(conn),
            "school_logo": _school_logo_in_transaction(conn),
        }


def load_master_sheet_data(academic_session_id: int, class_id: int, term_id: int) -> dict:
    """Load all complete approved subject results, requiring one consistent frozen roster."""
    with connection() as conn:
        return _load_master_sheet_in_transaction(conn, academic_session_id, class_id, term_id)


def _load_master_sheet_in_transaction(
    conn, academic_session_id: int, class_id: int, term_id: int, include_school_assets: bool = True
) -> dict:
    term = conn.execute(
        "SELECT academic_session_id,term_name,term_order FROM terms WHERE id=?", (term_id,)
    ).fetchone()
    session = conn.execute("SELECT name FROM academic_sessions WHERE id=?", (academic_session_id,)).fetchone()
    class_row = conn.execute("SELECT class_name,section FROM classes WHERE id=?", (class_id,)).fetchone()
    if not term or not session or not class_row or term["academic_session_id"] != academic_session_id:
        raise ValueError("Select a valid session, class, and term.")
    assignments = conn.execute(
        "SELECT id FROM class_subject_assignments WHERE academic_session_id=? AND class_id=? ORDER BY id",
        (academic_session_id, class_id),
    ).fetchall()
    if not assignments:
        raise ValueError("No class subjects are assigned for this academic session.")
    subject_sets = [
        _load_approved_subject_results(conn, int(item["id"]), term_id)
        for item in assignments
    ]
    expected_roster = subject_sets[0]["roster_ids"]
    if any(item["roster_ids"] != expected_roster for item in subject_sets[1:]):
        raise ValueError("Subject result rosters differ. Resolve enrollment or score-entry rosters before creating a master sheet.")
    by_enrollment = {
        int(student["enrollment_id"]): dict(student)
        for student in subject_sets[0]["students"]
    }
    subjects = []
    for result_set in subject_sets:
        assignment = result_set["assignment"]
        subjects.append({"id": int(assignment["id"]), "subject_code": assignment["subject_code"], "subject_name": assignment["subject_name"]})
        for student in result_set["students"]:
            by_enrollment[int(student["enrollment_id"])].setdefault("results", {})[
                int(assignment["id"])
            ] = student["result"]
    common_bands = _common_grade_bands([item["bands"] for item in subject_sets])
    exact_averages = {}
    for enrollment_id, student in by_enrollment.items():
        student_results = list(student["results"].values())
        student["overall"] = calculate_overall_average(student_results, common_bands)
        exact_averages[enrollment_id] = _overall_average_fraction(student_results)
    positions = calculate_class_positions(exact_averages)
    for enrollment_id, student in by_enrollment.items():
        student["position"] = positions[enrollment_id]
    students = sorted(
        by_enrollment.values(),
        key=lambda item: (item["last_name"].casefold(), item["first_name"].casefold(), int(item["enrollment_id"])),
    )
    return {
        "academic_session": session["name"], "term_name": term["term_name"],
        "term_order": int(term["term_order"]), "class_name": class_row["class_name"],
        "section": class_row["section"], "subjects": subjects, "students": students,
        "approval_status": "Approved",
        "school_profile": _school_profile_in_transaction(conn) if include_school_assets else {},
        "school_logo": _school_logo_in_transaction(conn) if include_school_assets else None,
    }


def _top_academic_achievers_in_transaction(conn, academic_session_id: int, term_id: int, limit: int = 5) -> list[dict]:
    """Return leaders only from complete, approved, roster-consistent class results."""
    ranked = []
    classes = conn.execute("SELECT id,class_name,section FROM classes ORDER BY class_name,section,id").fetchall()
    for class_row in classes:
        try:
            master = _load_master_sheet_in_transaction(
                conn, academic_session_id, int(class_row["id"]), term_id, include_school_assets=False
            )
        except ValueError:
            continue
        class_name = f"{class_row['class_name']} {class_row['section']}".strip()
        for student in master["students"]:
            ranked.append({
                "name": f"{student['first_name']} {student['last_name']}".strip(),
                "admission_no": student.get("admission_no", ""),
                "class": class_name,
                "average": student["overall"]["average"],
            })
    ranked.sort(key=lambda item: (-float(item["average"]), item["name"].casefold(), item["class"].casefold()))
    return ranked[:max(0, min(int(limit), 20))]


def _enrollment_context(conn: sqlite3.Connection, student_id: int, class_id: int, session_id: int):
    student = conn.execute(
        "SELECT is_active FROM students WHERE id=?", (student_id,)
    ).fetchone()
    if not student or not student["is_active"]:
        raise ValueError("Select an existing active student.")
    if not conn.execute("SELECT 1 FROM classes WHERE id=?", (class_id,)).fetchone():
        raise ValueError("Select an existing class and section.")
    session = conn.execute(
        "SELECT starts_on,ends_on FROM academic_sessions WHERE id=?", (session_id,)
    ).fetchone()
    if not session:
        raise ValueError("Select an existing academic session.")
    return session


def _validate_enrollment_date(value: str, session, field_name: str) -> str:
    normalized = _iso_date(value, field_name).isoformat()
    if normalized < session["starts_on"] or normalized > session["ends_on"]:
        raise ValueError(f"{field_name} must fall within the academic session dates.")
    return normalized


def _generate_admission_number(conn: sqlite3.Connection, year: str, next_number: int, reserved: set[str] | None = None):
    """Generate a unique, configured-prefix admission ID inside the caller's write transaction."""
    row = conn.execute("SELECT admission_id_prefix FROM school_profile WHERE id=1").fetchone()
    prefix = str(row[0] if row else "ADM")
    reserved = reserved or set()
    while True:
        admission_no = f"{prefix}-{year}-{next_number:05d}"
        if (admission_no.casefold() not in reserved
                and not conn.execute(
                    "SELECT 1 FROM students WHERE admission_no=? COLLATE NOCASE", (admission_no,)
                ).fetchone()):
            return admission_no, next_number + 1
        next_number += 1


def register_student(
    first_name: str,
    last_name: str,
    gender: str,
    guardian_phone: str,
    class_id: int,
    academic_session_id: int,
    enrolled_on: str,
    actor: str,
    photo_data: bytes | None = None,
    nin: str = "",
    date_of_birth: str = "",
    email: str = "",
) -> dict:
    """Create a student and their first class placement as one atomic action.

    Admission numbers are generated inside the write transaction so two
    registrations cannot receive the same value. The visible class field is
    retained for compatibility; the session-scoped enrollment is authoritative.
    """
    fields = {
        "First name": first_name,
        "Last name": last_name,
        "Gender": gender,
        "Guardian phone": guardian_phone,
    }
    for label, value in fields.items():
        if not isinstance(value, str):
            raise ValueError(f"{label} must be text.")
    first_name, last_name = first_name.strip(), last_name.strip()
    gender, guardian_phone = gender.strip(), guardian_phone.strip()
    nin, date_of_birth, email = normalize_optional_student_details(nin, date_of_birth, email)
    if not first_name or len(first_name) > 100:
        raise ValueError("First name is required and must be at most 100 characters.")
    if not last_name or len(last_name) > 100:
        raise ValueError("Last name is required and must be at most 100 characters.")
    if len(gender) > 30:
        raise ValueError("Gender must be at most 30 characters.")
    if len(guardian_phone) > 60:
        raise ValueError("Guardian phone must be at most 60 characters.")
    photo_data = _validate_student_photo(photo_data)
    if isinstance(class_id, bool) or not isinstance(class_id, int) or class_id <= 0:
        raise ValueError("Select a class and section.")
    if isinstance(academic_session_id, bool) or not isinstance(academic_session_id, int) or academic_session_id <= 0:
        raise ValueError("Select an academic session.")
    enrolled_on = _iso_date(enrolled_on, "Enrollment date").isoformat()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        session = conn.execute(
            "SELECT starts_on,ends_on FROM academic_sessions WHERE id=?", (academic_session_id,)
        ).fetchone()
        if not session:
            raise ValueError("Select an existing academic session.")
        enrolled_on = _validate_enrollment_date(enrolled_on, session, "Enrollment date")
        class_row = conn.execute(
            "SELECT class_name,section FROM classes WHERE id=?", (class_id,)
        ).fetchone()
        if not class_row:
            raise ValueError("Select an existing class and section.")
        class_label = " ".join(part for part in (class_row["class_name"], class_row["section"]) if part)
        year = enrolled_on[:4]
        next_number = int(conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM students").fetchone()[0])
        admission_no, _next_number = _generate_admission_number(conn, year, next_number)
        cursor = conn.execute(
            "INSERT INTO students(admission_no,first_name,last_name,gender,class_name,guardian_phone,photo_data,nin,date_of_birth,email) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (admission_no, first_name, last_name, gender, class_label, guardian_phone,
             photo_data, nin, date_of_birth, email),
        )
        student_id = int(cursor.lastrowid)
        _audit_in_transaction(conn, actor, "CREATE", "Students", student_id)
        enrollment_cursor = conn.execute(
            "INSERT INTO student_enrollments(student_id,class_id,academic_session_id,status,enrolled_on) "
            "VALUES(?,?,?,'active',?)",
            (student_id, class_id, academic_session_id, enrolled_on),
        )
        enrollment_id = int(enrollment_cursor.lastrowid)
        _audit_in_transaction(conn, actor, "ENROLL", "student_enrollments", enrollment_id)
        return {
            "student_id": student_id,
            "admission_no": admission_no,
            "enrollment_id": enrollment_id,
        }


def _validate_student_photo(photo_data: bytes | None) -> bytes | None:
    if photo_data is None:
        return None
    if not isinstance(photo_data, bytes) or not photo_data or len(photo_data) > 1_000_000:
        raise ValueError("Student photo must be a readable PNG or JPEG no larger than 1 MB.")
    if not (photo_data.startswith(b"\x89PNG\r\n\x1a\n") or photo_data.startswith(b"\xff\xd8\xff")):
        raise ValueError("Student photo must be a PNG or JPEG image.")
    return photo_data


def set_student_photo(student_id: int, photo_data: bytes | None, actor: str) -> None:
    photo_data = _validate_student_photo(photo_data)
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        row = conn.execute("SELECT id FROM students WHERE id=?", (student_id,)).fetchone()
        if row is None:
            raise ValueError("Select an existing student.")
        conn.execute("UPDATE students SET photo_data=? WHERE id=?", (photo_data, student_id))
        _audit_in_transaction(conn, actor, "UPDATE", "student_photo", student_id)


def register_students_bulk(
    students: list[dict], class_id: int, academic_session_id: int, enrolled_on: str, actor: str
) -> dict:
    """Register a validated class roster as one atomic database transaction."""
    if not isinstance(students, list) or not students or len(students) > 2000:
        raise ValueError("Select between 1 and 2,000 students to register.")
    if isinstance(class_id, bool) or not isinstance(class_id, int) or class_id <= 0:
        raise ValueError("Select a class and section.")
    if isinstance(academic_session_id, bool) or not isinstance(academic_session_id, int) or academic_session_id <= 0:
        raise ValueError("Select an academic session.")
    normalized_students = []
    for index, student in enumerate(students, start=1):
        if not isinstance(student, dict):
            raise ValueError(f"Student row {index} is invalid.")
        values = {}
        for field, limit in (("admission_no", 100), ("first_name", 100), ("last_name", 100), ("gender", 30), ("guardian_phone", 60)):
            value = student.get(field, "")
            if not isinstance(value, str):
                raise ValueError(f"Student row {index}: {field.replace('_', ' ')} must be text.")
            value = value.strip()
            if len(value) > limit:
                raise ValueError(f"Student row {index}: {field.replace('_', ' ')} must be at most {limit} characters.")
            values[field] = value
        try:
            values["nin"], values["date_of_birth"], values["email"] = normalize_optional_student_details(
                student.get("nin", ""), student.get("date_of_birth", ""), student.get("email", "")
            )
        except ValueError as exc:
            raise ValueError(f"Student row {index}: {exc}") from exc
        if not values["first_name"] or not values["last_name"]:
            raise ValueError(f"Student row {index}: first and last name are required.")
        normalized_students.append(values)
    supplied_numbers = [row["admission_no"].casefold() for row in normalized_students if row["admission_no"]]
    if len(supplied_numbers) != len(set(supplied_numbers)):
        raise ValueError("The import contains duplicate admission numbers.")
    enrolled_on = _iso_date(enrolled_on, "Enrollment date").isoformat()

    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        session = conn.execute(
            "SELECT starts_on,ends_on FROM academic_sessions WHERE id=?", (academic_session_id,)
        ).fetchone()
        if not session:
            raise ValueError("Select an existing academic session.")
        enrolled_on = _validate_enrollment_date(enrolled_on, session, "Enrollment date")
        class_row = conn.execute(
            "SELECT class_name,section FROM classes WHERE id=?", (class_id,)
        ).fetchone()
        if not class_row:
            raise ValueError("Select an existing class and section.")
        class_label = " ".join(part for part in (class_row["class_name"], class_row["section"]) if part)
        next_number = int(conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM students").fetchone()[0])
        for admission_no in (row["admission_no"] for row in normalized_students if row["admission_no"]):
            if conn.execute("SELECT 1 FROM students WHERE admission_no=? COLLATE NOCASE", (admission_no,)).fetchone():
                raise ValueError(f"Admission number {admission_no!r} is already in use.")
        admission_numbers = []
        reserved_numbers = set(supplied_numbers)
        for student in normalized_students:
            year = enrolled_on[:4]
            admission_no = student["admission_no"]
            if not admission_no:
                admission_no, next_number = _generate_admission_number(
                    conn, year, next_number, reserved_numbers
                )
                reserved_numbers.add(admission_no.casefold())
            cursor = conn.execute(
                "INSERT INTO students(admission_no,first_name,last_name,gender,class_name,guardian_phone,nin,date_of_birth,email) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (admission_no, student["first_name"], student["last_name"], student["gender"],
                 class_label, student["guardian_phone"], student["nin"],
                 student["date_of_birth"], student["email"]),
            )
            student_id = int(cursor.lastrowid)
            _audit_in_transaction(conn, actor, "CREATE", "Students", student_id)
            enrollment_cursor = conn.execute(
                "INSERT INTO student_enrollments(student_id,class_id,academic_session_id,status,enrolled_on) "
                "VALUES(?,?,?,'active',?)",
                (student_id, class_id, academic_session_id, enrolled_on),
            )
            _audit_in_transaction(conn, actor, "ENROLL", "student_enrollments", int(enrollment_cursor.lastrowid))
            admission_numbers.append(admission_no)
        return {"count": len(admission_numbers), "admission_numbers": admission_numbers}


def _promotion_candidates(
    conn: sqlite3.Connection,
    source_session_id: int,
    source_class_id: int,
    destination_session_id: int,
) -> list[dict]:
    if source_session_id == destination_session_id:
        raise ValueError("Choose a different destination academic session.")
    if not conn.execute("SELECT 1 FROM academic_sessions WHERE id=?", (source_session_id,)).fetchone():
        raise ValueError("Select an existing source academic session.")
    if not conn.execute("SELECT 1 FROM academic_sessions WHERE id=?", (destination_session_id,)).fetchone():
        raise ValueError("Select an existing destination academic session.")
    if not conn.execute("SELECT 1 FROM classes WHERE id=?", (source_class_id,)).fetchone():
        raise ValueError("Select an existing source class and section.")
    candidates = conn.execute(
        """SELECT e.student_id,s.admission_no,s.first_name,s.last_name,s.is_active,
                  (SELECT x.status FROM student_enrollments x
                   WHERE x.student_id=e.student_id AND x.academic_session_id=?
                   ORDER BY x.id DESC LIMIT 1) AS destination_status
           FROM student_enrollments e JOIN students s ON s.id=e.student_id
           WHERE e.academic_session_id=? AND e.class_id=? AND e.status='active'
           ORDER BY s.last_name COLLATE NOCASE,s.first_name COLLATE NOCASE,s.admission_no""",
        (destination_session_id, source_session_id, source_class_id),
    ).fetchall()
    result = []
    for row in candidates:
        reason = ""
        if not row["is_active"]:
            reason = "Student record is inactive"
        elif row["destination_status"] is not None:
            reason = f"Already has a {row['destination_status']} enrollment in the destination session"
        result.append({
            "student_id": int(row["student_id"]),
            "admission_no": row["admission_no"],
            "first_name": row["first_name"],
            "last_name": row["last_name"],
            "eligible": not reason,
            "reason": reason,
        })
    return result


def preview_student_promotion(
    source_session_id: int,
    source_class_id: int,
    destination_session_id: int,
    destination_class_id: int,
    enrolled_on: str,
    actor: str,
) -> dict:
    """Preview explicit class-to-class promotion without changing enrollments."""
    if source_session_id == destination_session_id:
        raise ValueError("Choose a different destination academic session.")
    enrolled_on = _iso_date(enrolled_on, "Promotion date").isoformat()
    with connection() as conn:
        _require_admin(conn, actor)
        if not conn.execute("SELECT 1 FROM classes WHERE id=?", (destination_class_id,)).fetchone():
            raise ValueError("Select an existing destination class and section.")
        destination_session = conn.execute(
            "SELECT starts_on,ends_on FROM academic_sessions WHERE id=?", (destination_session_id,)
        ).fetchone()
        if not destination_session:
            raise ValueError("Select an existing destination academic session.")
        _validate_enrollment_date(enrolled_on, destination_session, "Promotion date")
        candidates = _promotion_candidates(
            conn, source_session_id, source_class_id, destination_session_id
        )
    return {
        "students": candidates,
        "eligible_count": sum(item["eligible"] for item in candidates),
        "skipped_count": sum(not item["eligible"] for item in candidates),
    }


def promote_class_students(
    source_session_id: int,
    source_class_id: int,
    destination_session_id: int,
    destination_class_id: int,
    enrolled_on: str,
    student_ids: list[int],
    actor: str,
) -> int:
    """Create explicit destination enrollments atomically; retain every source row."""
    if source_session_id == destination_session_id:
        raise ValueError("Choose a different destination academic session.")
    enrolled_on = _iso_date(enrolled_on, "Promotion date").isoformat()
    if not isinstance(student_ids, list) or not student_ids or len(student_ids) > 2000:
        raise ValueError("Select between 1 and 2,000 eligible students to promote.")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in student_ids):
        raise ValueError("Promotion contains an invalid student selection.")
    if len(set(student_ids)) != len(student_ids):
        raise ValueError("A student can only be selected once for promotion.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        destination_session = conn.execute(
            "SELECT starts_on,ends_on FROM academic_sessions WHERE id=?", (destination_session_id,)
        ).fetchone()
        if not destination_session:
            raise ValueError("Select an existing destination academic session.")
        enrolled_on = _validate_enrollment_date(enrolled_on, destination_session, "Promotion date")
        if not conn.execute("SELECT 1 FROM classes WHERE id=?", (destination_class_id,)).fetchone():
            raise ValueError("Select an existing destination class and section.")
        candidates = _promotion_candidates(
            conn, source_session_id, source_class_id, destination_session_id
        )
        by_id = {item["student_id"]: item for item in candidates}
        if any(student_id not in by_id or not by_id[student_id]["eligible"] for student_id in student_ids):
            raise ValueError("The promotion selection changed or includes an ineligible student. Review the preview again.")
        for student_id in student_ids:
            cursor = conn.execute(
                """INSERT INTO student_enrollments
                   (student_id,class_id,academic_session_id,status,enrolled_on)
                   VALUES(?,?,?,'active',?)""",
                (student_id, destination_class_id, destination_session_id, enrolled_on),
            )
            _audit_in_transaction(conn, actor, "PROMOTE_IN", "student_enrollments", int(cursor.lastrowid))
        _audit_in_transaction(
            conn,
            actor,
            f"PROMOTE_CLASS ({len(student_ids)} students)",
            "student_enrollments",
            f"{source_session_id}:{source_class_id}->{destination_session_id}:{destination_class_id}",
        )
        return len(student_ids)


def create_student_enrollment(
    student_id: int, class_id: int, academic_session_id: int, enrolled_on: str, actor: str
) -> int:
    enrolled_on = _iso_date(enrolled_on, "Enrollment date").isoformat()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        session = _enrollment_context(conn, student_id, class_id, academic_session_id)
        enrolled_on = _validate_enrollment_date(enrolled_on, session, "Enrollment date")
        if conn.execute(
            "SELECT 1 FROM student_enrollments WHERE student_id=? AND academic_session_id=? AND status='active'",
            (student_id, academic_session_id),
        ).fetchone():
            raise ValueError("This student already has an active enrollment in that session.")
        cursor = conn.execute(
            "INSERT INTO student_enrollments(student_id,class_id,academic_session_id,status,enrolled_on) VALUES(?,?,?,'active',?)",
            (student_id, class_id, academic_session_id, enrolled_on),
        )
        enrollment_id = int(cursor.lastrowid)
        _audit_in_transaction(conn, actor, "ENROLL", "student_enrollments", enrollment_id)
        return enrollment_id


def close_student_enrollment(enrollment_id: int, status: str, left_on: str, actor: str) -> None:
    if status not in {"completed", "withdrawn"}:
        raise ValueError("Enrollment can only be closed as completed or withdrawn.")
    left_on = _iso_date(left_on, "Leaving date").isoformat()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        row = conn.execute(
            """SELECT e.student_id,e.status,e.enrolled_on,e.academic_session_id,
                      a.starts_on,a.ends_on
               FROM student_enrollments e JOIN academic_sessions a ON a.id=e.academic_session_id
               WHERE e.id=?""",
            (enrollment_id,),
        ).fetchone()
        if not row:
            raise ValueError("Student enrollment was not found.")
        if row["status"] != "active":
            raise ValueError("Only an active enrollment can be closed.")
        left_on = _validate_enrollment_date(left_on, row, "Leaving date")
        if left_on < row["enrolled_on"]:
            raise ValueError("Leaving date cannot be earlier than the enrollment date.")
        conn.execute(
            "UPDATE student_enrollments SET status=?,left_on=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, left_on, enrollment_id),
        )
        _audit_in_transaction(conn, actor, status.upper(), "student_enrollments", enrollment_id)


def transfer_student_enrollment(
    enrollment_id: int, destination_class_id: int, transfer_on: str, actor: str
) -> int:
    transfer_on = _iso_date(transfer_on, "Transfer date").isoformat()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        row = conn.execute(
            """SELECT e.student_id,e.class_id,e.academic_session_id,e.status,e.enrolled_on,
                      a.starts_on,a.ends_on,s.is_active
               FROM student_enrollments e
               JOIN academic_sessions a ON a.id=e.academic_session_id
               JOIN students s ON s.id=e.student_id
               WHERE e.id=?""",
            (enrollment_id,),
        ).fetchone()
        if not row:
            raise ValueError("Student enrollment was not found.")
        if row["status"] != "active":
            raise ValueError("Only an active enrollment can be transferred.")
        if not row["is_active"]:
            raise ValueError("An inactive student cannot be transferred.")
        if row["class_id"] == destination_class_id:
            raise ValueError("Choose a different destination class and section.")
        if not conn.execute("SELECT 1 FROM classes WHERE id=?", (destination_class_id,)).fetchone():
            raise ValueError("Select an existing destination class and section.")
        transfer_on = _validate_enrollment_date(transfer_on, row, "Transfer date")
        if transfer_on < row["enrolled_on"]:
            raise ValueError("Transfer date cannot be earlier than the enrollment date.")
        conn.execute(
            "UPDATE student_enrollments SET status='transferred',left_on=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (transfer_on, enrollment_id),
        )
        cursor = conn.execute(
            """INSERT INTO student_enrollments
               (student_id,class_id,academic_session_id,status,enrolled_on)
               VALUES(?,?,?,'active',?)""",
            (row["student_id"], destination_class_id, row["academic_session_id"], transfer_on),
        )
        new_id = int(cursor.lastrowid)
        _audit_in_transaction(conn, actor, "TRANSFERRED", "student_enrollments", enrollment_id)
        _audit_in_transaction(conn, actor, "TRANSFER_IN", "student_enrollments", new_id)
        return new_id


def save_term(
    academic_session_id: int,
    term_name: str,
    term_order: int,
    starts_on: str,
    ends_on: str,
    actor: str,
    term_id: int | None = None,
) -> int:
    term_name = term_name.strip()
    if not term_name or len(term_name) > 60:
        raise ValueError("Term name must contain between 1 and 60 characters.")
    if isinstance(term_order, bool) or not isinstance(term_order, int) or term_order < 1:
        raise ValueError("Term order must be a positive whole number.")
    starts_on, ends_on = _date_range(starts_on, ends_on, "Term")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        session = conn.execute(
            "SELECT starts_on,ends_on FROM academic_sessions WHERE id=?", (academic_session_id,)
        ).fetchone()
        if not session:
            raise ValueError("Select an existing academic session.")
        if starts_on < session["starts_on"] or ends_on > session["ends_on"]:
            raise ValueError("Term dates must fall within the selected academic session.")
        overlap_sql = (
            "SELECT 1 FROM terms WHERE academic_session_id=? AND starts_on<=? AND ends_on>=?"
        )
        overlap_params: tuple[object, ...] = (academic_session_id, ends_on, starts_on)
        if term_id is not None:
            overlap_sql += " AND id<>?"
            overlap_params += (term_id,)
        if conn.execute(overlap_sql + " LIMIT 1", overlap_params).fetchone():
            raise ValueError("Term dates cannot overlap another term in the same session.")
        keep_current = False
        if term_id is not None:
            existing = conn.execute(
                "SELECT academic_session_id,is_current FROM terms WHERE id=?", (term_id,)
            ).fetchone()
            if not existing:
                raise ValueError("Term was not found.")
            keep_current = bool(existing["is_current"] and int(existing["academic_session_id"]) == academic_session_id)
        if term_id is None:
            cursor = conn.execute(
                "INSERT INTO terms(academic_session_id,term_name,term_order,starts_on,ends_on) VALUES(?,?,?,?,?)",
                (academic_session_id, term_name, term_order, starts_on, ends_on),
            )
            term_id = int(cursor.lastrowid)
            action = "CREATE"
        else:
            cursor = conn.execute(
                "UPDATE terms SET academic_session_id=?,term_name=?,term_order=?,starts_on=?,ends_on=?,is_current=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (academic_session_id, term_name, term_order, starts_on, ends_on, int(keep_current), term_id),
            )
            action = "UPDATE"
        _audit_in_transaction(conn, actor, action, "terms", term_id)
        return term_id


def list_rows(module_name: str, search: str = ""):
    _authorized(module_name, None)
    spec = MODULES[module_name]
    columns = [field[0] for field in spec["fields"]]
    sql = f"SELECT id, {', '.join(columns)} FROM {spec['table']}"
    params = []
    if search.strip():
        sql += " WHERE " + " OR ".join(f"CAST({col} AS TEXT) LIKE ?" for col in columns)
        params = [f"%{search.strip()}%"] * len(columns)
    sql += " ORDER BY id DESC LIMIT 1000"
    with connection() as db:
        return db.execute(sql, params).fetchall()


def list_legacy_class_summary():
    """Return legacy student class labels and counts without changing records."""
    with connection() as conn:
        return conn.execute(
            "SELECT class_name,COUNT(*) AS student_count FROM students "
            "GROUP BY class_name ORDER BY class_name COLLATE NOCASE, class_name"
        ).fetchall()


def preview_legacy_class_mapping(class_name: str, academic_session_id: int) -> dict[str, int]:
    """Count total, active, inactive and already-enrolled students for an exact label."""
    if not isinstance(class_name, str):
        raise ValueError("Select a legacy class value.")
    with connection() as conn:
        if not conn.execute(
            "SELECT 1 FROM academic_sessions WHERE id=?", (academic_session_id,)
        ).fetchone():
            raise ValueError("Select an existing academic session.")
        row = conn.execute(
            """SELECT COUNT(*) AS total,
                      COALESCE(SUM(CASE WHEN s.is_active=1 THEN 1 ELSE 0 END),0) AS active,
                      COALESCE(SUM(CASE WHEN s.is_active=0 THEN 1 ELSE 0 END),0) AS inactive,
                      COALESCE(SUM(CASE WHEN s.is_active=1 AND EXISTS (
                          SELECT 1 FROM student_enrollments e
                          WHERE e.student_id=s.id AND e.academic_session_id=? AND e.status='active'
                      ) THEN 1 ELSE 0 END),0) AS already_enrolled
               FROM students s WHERE s.class_name=?""",
            (academic_session_id, class_name),
        ).fetchone()
        result = dict(row)
        result["eligible"] = result["active"] - result["already_enrolled"]
        return result


def map_legacy_class_to_enrollments(
    class_name: str, class_id: int, academic_session_id: int, enrolled_on: str, actor: str
) -> int:
    """Explicitly enroll active students with an exact legacy label in one transaction."""
    if not isinstance(class_name, str):
        raise ValueError("Select a legacy class value.")
    enrolled_on = _iso_date(enrolled_on, "Enrollment date").isoformat()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        destination = conn.execute(
            "SELECT educational_stage FROM classes WHERE id=?", (class_id,)
        ).fetchone()
        if not destination:
            raise ValueError("Select an existing class and section.")
        if not destination["educational_stage"]:
            raise ValueError("Set the destination class's educational stage before mapping enrollments.")
        session = conn.execute(
            "SELECT starts_on,ends_on FROM academic_sessions WHERE id=?", (academic_session_id,)
        ).fetchone()
        if not session:
            raise ValueError("Select an existing academic session.")
        enrolled_on = _validate_enrollment_date(enrolled_on, session, "Enrollment date")
        active_count = conn.execute(
            "SELECT COUNT(*) FROM students WHERE class_name=? AND is_active=1",
            (class_name,),
        ).fetchone()[0]
        if not active_count:
            raise ValueError("This legacy class value has no active students to map.")
        conflicts = conn.execute(
            """SELECT COUNT(*) FROM student_enrollments e
               JOIN students s ON s.id=e.student_id
               WHERE e.academic_session_id=? AND e.status='active'
                 AND s.is_active=1 AND s.class_name=?""",
            (academic_session_id, class_name),
        ).fetchone()[0]
        if conflicts:
            raise ValueError(
                f"{conflicts} active student(s) already have an enrollment in this session. "
                "No records were changed; review the group and resolve the conflicts first."
            )
        cursor = conn.execute(
            """INSERT INTO student_enrollments
               (student_id,class_id,academic_session_id,status,enrolled_on)
               SELECT id,?,?,'active',? FROM students WHERE class_name=? AND is_active=1""",
            (class_id, academic_session_id, enrolled_on, class_name),
        )
        mapped_count = cursor.rowcount
        _audit_in_transaction(
            conn, actor, f"MAP_LEGACY_CLASS ({mapped_count} students)",
            "legacy_student_class", class_name or "(blank)",
        )
        return mapped_count


def list_rows_page(
    module_name: str, search: str = "", limit: int = 200, offset: int = 0,
    student_status: str = "all",
):
    _authorized(module_name, None)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("Page size must be between 1 and 500.")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("Page offset must be a non-negative whole number.")
    spec = MODULES[module_name]
    columns = [field[0] for field in spec["fields"]]
    if module_name == "Students" and student_status not in {"all", "active", "archived"}:
        raise ValueError("Choose all students, active students, or archived students.")
    if module_name != "Students" and student_status != "all":
        raise ValueError("Student status filtering is available only for the student register.")
    conditions = []
    params: list[object] = []
    if search.strip():
        conditions.append("(" + " OR ".join(f"CAST({col} AS TEXT) LIKE ?" for col in columns) + ")")
        params = [f"%{search.strip()}%"] * len(columns)
    if module_name == "Students" and student_status in {"active", "archived"}:
        conditions.append("is_active=?")
        params.append(1 if student_status == "active" else 0)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with connection() as conn:
        conn.execute("BEGIN")
        total = conn.execute(f"SELECT COUNT(*) FROM {spec['table']}" + where, params).fetchone()[0]
        status_column = ", is_active" if module_name == "Students" else ""
        rows = conn.execute(
            f"SELECT id, {', '.join(columns)}{status_column} FROM {spec['table']}" + where
            + " ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return rows, int(total)


def set_student_active(student_id: int, is_active: bool, actor: str | None = None) -> None:
    """Archive or restore a student while preserving enrollment and academic history."""
    if isinstance(student_id, bool) or not isinstance(student_id, int) or student_id <= 0:
        raise ValueError("Select a valid student record.")
    if not isinstance(is_active, bool):
        raise ValueError("Student status must be active or archived.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        row = conn.execute("SELECT is_active FROM students WHERE id=?", (student_id,)).fetchone()
        if not row:
            raise ValueError("The selected student no longer exists.")
        current = bool(row["is_active"])
        if current == is_active:
            raise ValueError("The student is already " + ("active." if is_active else "archived."))
        conn.execute("UPDATE students SET is_active=? WHERE id=?", (int(is_active), student_id))
        _audit_in_transaction(conn, actor, "RESTORE" if is_active else "ARCHIVE", "Students", student_id)


def add_row(module_name: str, values: Iterable[str], actor: str | None = None):
    _authorized(module_name, None)
    spec = MODULES[module_name]
    columns = [field[0] for field in spec["fields"]]
    values = [str(value).strip() for value in values]
    if module_name == "Students" and len(values) == len(columns) - 3:
        values.extend(("", "", ""))
    if len(values) != len(columns):
        raise ValueError("The number of values does not match the form fields.")
    if not all(values[:2]):
        raise ValueError("Please complete the required first two fields.")
    if module_name == "Students":
        values[-3:] = normalize_optional_student_details(*values[-3:])
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        cur = conn.execute(f"INSERT INTO {spec['table']} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values)
        row_id = int(cur.lastrowid)
        _audit_in_transaction(conn, actor, "CREATE", module_name, row_id)
        return row_id


def update_row(module_name: str, row_id: int, values: Iterable[str], actor: str | None = None) -> None:
    _authorized(module_name, None)
    spec = MODULES[module_name]
    columns = [field[0] for field in spec["fields"]]
    values = [str(value).strip() for value in values]
    if module_name == "Students" and len(values) == len(columns) - 3:
        values.extend(("", "", ""))
    if len(values) != len(columns):
        raise ValueError("The number of values does not match the form fields.")
    if not all(values[:2]):
        raise ValueError("Please complete the required first two fields.")
    if module_name == "Students":
        values[-3:] = normalize_optional_student_details(*values[-3:])
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        cursor = conn.execute(
            f"UPDATE {spec['table']} SET {','.join(f'{column}=?' for column in columns)} WHERE id=?",
            (*values, row_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("The selected record no longer exists.")
        _audit_in_transaction(conn, actor, "UPDATE", module_name, row_id)


def delete_row(module_name: str, row_id: int, actor: str | None = None) -> None:
    _authorized(module_name, None)
    spec = MODULES[module_name]
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        cursor = conn.execute(f"DELETE FROM {spec['table']} WHERE id=?", (row_id,))
        if cursor.rowcount != 1:
            raise ValueError("The selected record no longer exists.")
        _audit_in_transaction(conn, actor, "DELETE", module_name, row_id)


def export_csv(module_name: str, path: str) -> None:
    _authorized(module_name, None)
    spec = MODULES[module_name]
    columns = [field[0] for field in spec["fields"]]
    # Machine-friendly field names keep exported CSV headers short and stable;
    # the desktop UI continues to use the full, readable labels from MODULES.
    labels = [field[0] for field in spec["fields"]]

    def spreadsheet_safe(value):
        if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
            return "'" + value
        return value

    from .spreadsheet_files import write_rows
    with connection() as db:
        rows = db.execute(f"SELECT {','.join(columns)} FROM {spec['table']} ORDER BY id")
        write_rows(path, labels, ([spreadsheet_safe(row[column]) for column in columns] for row in rows))


def _attendance_roster(conn: sqlite3.Connection, session_id: int, class_id: int, attendance_date: str):
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (session_id, class_id)):
        raise ValueError("Select a valid academic session and class.")
    session = conn.execute(
        "SELECT starts_on,ends_on FROM academic_sessions WHERE id=?", (session_id,)
    ).fetchone()
    if not session:
        raise ValueError("Select an existing academic session.")
    if not conn.execute("SELECT 1 FROM classes WHERE id=?", (class_id,)).fetchone():
        raise ValueError("Select an existing class and section.")
    if not session["starts_on"] <= attendance_date <= session["ends_on"]:
        raise ValueError("Attendance date must fall within the selected academic session.")
    return conn.execute(
        """SELECT e.id AS enrollment_id,s.id AS student_id,s.admission_no,
                  s.first_name,s.last_name
           FROM student_enrollments e
           JOIN students s ON s.id=e.student_id
           WHERE e.academic_session_id=? AND e.class_id=? AND e.enrolled_on<=?
             AND (e.left_on IS NULL OR e.left_on>=?)
             AND NOT EXISTS (
                 SELECT 1 FROM student_enrollments newer
                 WHERE newer.student_id=e.student_id
                   AND newer.academic_session_id=e.academic_session_id
                   AND newer.enrolled_on<=?
                   AND (newer.left_on IS NULL OR newer.left_on>=?)
                   AND (newer.enrolled_on>e.enrolled_on
                        OR (newer.enrolled_on=e.enrolled_on AND newer.id>e.id))
             )
           ORDER BY s.last_name COLLATE NOCASE,s.first_name COLLATE NOCASE,s.id""",
        (session_id, class_id, attendance_date, attendance_date, attendance_date, attendance_date),
    ).fetchall()


def _require_attendance_class_scope(
    conn: sqlite3.Connection, actor: str, session_id: int, class_id: int
) -> str:
    if not actor:
        raise PermissionError("An active administrator or assigned teacher is required.")
    user = conn.execute(
        "SELECT role,is_active,teacher_id FROM users WHERE username=? COLLATE NOCASE",
        (actor.strip(),),
    ).fetchone()
    if not user or not user["is_active"]:
        raise PermissionError("An active administrator or assigned teacher is required.")
    if user["role"] == "admin":
        return actor.strip()
    if user["role"] != "teacher":
        raise PermissionError("Only an administrator or assigned teacher may access attendance.")
    if user["teacher_id"] is None:
        raise PermissionError("Link this teacher login to an active teacher record before accessing attendance.")
    teacher = conn.execute(
        "SELECT status FROM teachers WHERE id=?", (int(user["teacher_id"]),)
    ).fetchone()
    if not teacher or teacher["status"] != "Active":
        raise PermissionError("Link this login to an active teacher record before accessing attendance.")
    assigned = conn.execute(
        """SELECT 1 FROM class_subject_assignments
           WHERE academic_session_id=? AND class_id=? AND teacher_id=? LIMIT 1""",
        (session_id, class_id, int(user["teacher_id"])),
    ).fetchone()
    if not assigned:
        raise PermissionError("Teachers may access attendance only for classes assigned to them.")
    return actor.strip()


def load_attendance_options(actor: str) -> dict:
    """Load sessions and class choices limited to the signed-in attendance user."""
    with connection() as conn:
        user = conn.execute(
            "SELECT role,is_active,teacher_id FROM users WHERE username=? COLLATE NOCASE",
            (actor.strip(),),
        ).fetchone() if actor else None
        if not user or not user["is_active"]:
            raise PermissionError("An active administrator or assigned teacher is required.")
        if user["role"] == "admin":
            sessions = [dict(row) for row in conn.execute(
                "SELECT id,name,starts_on,ends_on,is_current FROM academic_sessions "
                "ORDER BY is_current DESC,starts_on DESC,id DESC"
            )]
            classes = [dict(row) for row in conn.execute(
                "SELECT id,class_name,section,educational_stage FROM classes "
                "ORDER BY class_name COLLATE NOCASE,section COLLATE NOCASE,id"
            )]
            return {
                "sessions": sessions,
                "classes_by_session": {int(item["id"]): classes for item in sessions},
                "teacher_linked": False,
            }
        if user["role"] != "teacher":
            raise PermissionError("Only an administrator or assigned teacher may access attendance.")
        if user["teacher_id"] is None:
            return {"sessions": [], "classes_by_session": {}, "teacher_linked": False}
        teacher = conn.execute(
            "SELECT status FROM teachers WHERE id=?", (int(user["teacher_id"]),)
        ).fetchone()
        if not teacher or teacher["status"] != "Active":
            return {"sessions": [], "classes_by_session": {}, "teacher_linked": False}
        pairs = conn.execute(
            """SELECT DISTINCT a.academic_session_id AS session_id,s.name,s.starts_on,s.ends_on,
                      s.is_current,c.id AS class_id,c.class_name,c.section,c.educational_stage
               FROM class_subject_assignments a
               JOIN academic_sessions s ON s.id=a.academic_session_id
               JOIN classes c ON c.id=a.class_id
               WHERE a.teacher_id=?
               ORDER BY s.is_current DESC,s.starts_on DESC,s.id DESC,
                        c.class_name COLLATE NOCASE,c.section COLLATE NOCASE,c.id""",
            (int(user["teacher_id"]),),
        ).fetchall()
        sessions = []
        seen_sessions = set()
        classes_by_session: dict[int, list[dict]] = {}
        for row in pairs:
            session_id = int(row["session_id"])
            if session_id not in seen_sessions:
                sessions.append({
                    "id": session_id, "name": row["name"], "starts_on": row["starts_on"],
                    "ends_on": row["ends_on"], "is_current": row["is_current"],
                })
                seen_sessions.add(session_id)
            classes_by_session.setdefault(session_id, []).append({
                "id": int(row["class_id"]), "class_name": row["class_name"],
                "section": row["section"], "educational_stage": row["educational_stage"],
            })
        return {"sessions": sessions, "classes_by_session": classes_by_session, "teacher_linked": True}


def load_attendance_register(
    session_id: int, class_id: int, attendance_date: str, actor: str | None = None
) -> dict:
    """Load the enrollment roster and explicit marks; missing marks remain unmarked."""
    attendance_date = _iso_date(attendance_date, "Attendance date").isoformat()
    with connection() as conn:
        if actor is not None:
            _require_attendance_class_scope(conn, actor, session_id, class_id)
        roster = _attendance_roster(conn, session_id, class_id, attendance_date)
        register = conn.execute(
            "SELECT id FROM attendance_registers WHERE academic_session_id=? AND class_id=? AND attendance_date=?",
            (session_id, class_id, attendance_date),
        ).fetchone()
        register_id = int(register["id"]) if register else None
        marks = {}
        if register_id is not None:
            marks = {
                int(row["enrollment_id"]): row["status"]
                for row in conn.execute(
                    "SELECT enrollment_id,status FROM attendance_marks WHERE register_id=?",
                    (register_id,),
                )
            }
        return {
            "register_id": register_id,
            "students": [dict(row, status=marks.get(int(row["enrollment_id"]))) for row in roster],
        }


def load_attendance_summary(
    session_id: int, class_id: int, starts_on: str, ends_on: str, actor: str | None = None
) -> list[dict]:
    """Summarize saved registers; unmarked counts are derived from date-specific enrollment."""
    starts_on = _iso_date(starts_on, "Summary start date").isoformat()
    ends_on = _iso_date(ends_on, "Summary end date").isoformat()
    if starts_on > ends_on:
        raise ValueError("Summary start date must not be after its end date.")
    with connection() as conn:
        if actor is not None:
            _require_attendance_class_scope(conn, actor, session_id, class_id)
        session = conn.execute(
            "SELECT starts_on,ends_on FROM academic_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if not session or not conn.execute("SELECT 1 FROM classes WHERE id=?", (class_id,)).fetchone():
            raise ValueError("Select an existing academic session and class.")
        if starts_on < session["starts_on"] or ends_on > session["ends_on"]:
            raise ValueError("Summary dates must fall within the selected academic session.")
        rows = conn.execute(
            """SELECT r.attendance_date,
                      (SELECT COUNT(*) FROM student_enrollments e
                       WHERE e.academic_session_id=r.academic_session_id AND e.class_id=r.class_id
                         AND e.enrolled_on<=r.attendance_date
                         AND (e.left_on IS NULL OR e.left_on>=r.attendance_date)
                         AND NOT EXISTS (
                           SELECT 1 FROM student_enrollments newer
                           WHERE newer.student_id=e.student_id
                             AND newer.academic_session_id=e.academic_session_id
                             AND newer.enrolled_on<=r.attendance_date
                             AND (newer.left_on IS NULL OR newer.left_on>=r.attendance_date)
                             AND (newer.enrolled_on>e.enrolled_on
                               OR (newer.enrolled_on=e.enrolled_on AND newer.id>e.id))
                         )) AS enrolled,
                      SUM(CASE WHEN m.status='present' THEN 1 ELSE 0 END) AS present,
                      SUM(CASE WHEN m.status='absent' THEN 1 ELSE 0 END) AS absent
               FROM attendance_registers r
               LEFT JOIN attendance_marks m ON m.register_id=r.id
               WHERE r.academic_session_id=? AND r.class_id=?
                 AND r.attendance_date BETWEEN ? AND ?
               GROUP BY r.id
               ORDER BY r.attendance_date""",
            (session_id, class_id, starts_on, ends_on),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["present"] = int(item["present"] or 0)
            item["absent"] = int(item["absent"] or 0)
            item["unmarked"] = max(0, int(item["enrolled"]) - item["present"] - item["absent"])
            result.append(item)
        return result


def save_attendance_marks(
    session_id: int,
    class_id: int,
    attendance_date: str,
    marks: dict[int, str],
    actor: str,
    reason: str = "",
) -> tuple[int, int]:
    """Save explicit present/absent marks and append every change to history atomically."""
    attendance_date = _iso_date(attendance_date, "Attendance date").isoformat()
    if not isinstance(marks, dict) or not marks:
        raise ValueError("Mark at least one student before saving attendance.")
    normalized: dict[int, str] = {}
    for enrollment_id, status in marks.items():
        if isinstance(enrollment_id, bool) or not isinstance(enrollment_id, int) or enrollment_id <= 0:
            raise ValueError("Invalid student enrollment in attendance marks.")
        if status not in ("present", "absent"):
            raise ValueError("Attendance must be explicitly marked present or absent.")
        normalized[enrollment_id] = status
    if not isinstance(reason, str):
        raise ValueError("Attendance correction reason must be text.")
    reason = reason.strip()
    if len(reason) > 300:
        raise ValueError("The attendance correction reason must be 300 characters or fewer.")

    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_attendance_class_scope(conn, actor, session_id, class_id)
        roster = _attendance_roster(conn, session_id, class_id, attendance_date)
        allowed_ids = {int(row["enrollment_id"]) for row in roster}
        if not set(normalized).issubset(allowed_ids):
            raise ValueError("Attendance includes a student who is not enrolled in this class on that date.")
        existing_register = conn.execute(
            "SELECT id FROM attendance_registers WHERE academic_session_id=? AND class_id=? AND attendance_date=?",
            (session_id, class_id, attendance_date),
        ).fetchone()
        register_id = int(existing_register["id"]) if existing_register else None
        existing_marks = {}
        if register_id is not None:
            existing_marks = {
                int(row["enrollment_id"]): row["status"]
                for row in conn.execute(
                    "SELECT enrollment_id,status FROM attendance_marks WHERE register_id=?",
                    (register_id,),
                )
            }
        has_correction = any(
            enrollment_id in existing_marks and existing_marks[enrollment_id] != status
            for enrollment_id, status in normalized.items()
        )
        if has_correction and not reason:
            raise ValueError("Enter a reason when changing a previously saved attendance mark.")
        if register_id is None:
            cursor = conn.execute(
                "INSERT INTO attendance_registers(academic_session_id,class_id,attendance_date,created_by,updated_by) "
                "VALUES(?,?,?,?,?)",
                (session_id, class_id, attendance_date, actor, actor),
            )
            register_id = int(cursor.lastrowid)

        changed = 0
        for enrollment_id, status in normalized.items():
            previous = existing_marks.get(enrollment_id)
            if previous == status:
                continue
            if previous is None:
                conn.execute(
                    "INSERT INTO attendance_marks(register_id,enrollment_id,status) VALUES(?,?,?)",
                    (register_id, enrollment_id, status),
                )
            else:
                conn.execute(
                    "UPDATE attendance_marks SET status=?,updated_at=CURRENT_TIMESTAMP WHERE register_id=? AND enrollment_id=?",
                    (status, register_id, enrollment_id),
                )
            conn.execute(
                """INSERT INTO attendance_mark_history
                   (register_id,enrollment_id,previous_status,new_status,reason,actor)
                   VALUES(?,?,?,?,?,?)""",
                (register_id, enrollment_id, previous, status, reason if previous is not None else "", actor),
            )
            changed += 1
        if changed:
            conn.execute(
                "UPDATE attendance_registers SET updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (actor, register_id),
            )
            _audit_in_transaction(conn, actor, "SAVE", "attendance_registers", register_id)
        return register_id, changed


def _canonical_clock_time(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must use 24-hour HH:MM time.")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must use 24-hour HH:MM time.") from exc
    canonical = parsed.strftime("%H:%M")
    if canonical != value:
        raise ValueError(f"{label} must use 24-hour HH:MM time.")
    return canonical


def load_timetable_data(academic_session_id: int | None = None) -> dict[str, list]:
    """Load timetable selectors and entries for the chosen academic session."""
    with connection() as conn:
        conn.execute("BEGIN")
        sessions = conn.execute(
            "SELECT id,name,is_current FROM academic_sessions ORDER BY is_current DESC,starts_on DESC,id DESC"
        ).fetchall()
        assignments = []
        entries = []
        if academic_session_id is not None:
            assignments = conn.execute(
                """SELECT x.id,x.academic_session_id,x.class_id,c.class_name,c.section,
                          x.subject_id,s.subject_code,s.subject_name,x.teacher_id,
                          t.staff_no,t.first_name AS teacher_first_name,t.last_name AS teacher_last_name
                   FROM class_subject_assignments x
                   JOIN classes c ON c.id=x.class_id
                   JOIN subjects s ON s.id=x.subject_id
                   LEFT JOIN teachers t ON t.id=x.teacher_id
                   WHERE x.academic_session_id=?
                   ORDER BY c.class_name,c.section,s.subject_name,s.subject_code""",
                (academic_session_id,),
            ).fetchall()
            entries = conn.execute(
                """SELECT e.id,e.class_subject_assignment_id,e.day_of_week,e.starts_at,e.ends_at,
                          e.location,x.class_id,x.teacher_id,c.class_name,c.section,
                          s.subject_code,s.subject_name,t.first_name AS teacher_first_name,
                          t.last_name AS teacher_last_name
                   FROM timetable_entries e
                   JOIN class_subject_assignments x ON x.id=e.class_subject_assignment_id
                   JOIN classes c ON c.id=x.class_id
                   JOIN subjects s ON s.id=x.subject_id
                   LEFT JOIN teachers t ON t.id=x.teacher_id
                   WHERE x.academic_session_id=?
                   ORDER BY e.day_of_week,e.starts_at,c.class_name,c.section,s.subject_name""",
                (academic_session_id,),
            ).fetchall()
    return {"sessions": sessions, "assignments": assignments, "entries": entries}


def load_timetable_print_data(academic_session_id: int) -> dict:
    """Load an explicitly selected session's timetable and optional school branding."""
    timetable = load_timetable_data(academic_session_id)
    session = next(
        (item for item in timetable["sessions"] if int(item["id"]) == int(academic_session_id)),
        None,
    )
    if session is None:
        raise ValueError("Select an existing academic session.")
    if not timetable["entries"]:
        raise ValueError("There are no timetable slots to print for this session.")
    with connection() as conn:
        profile = _school_profile_in_transaction(conn)
        logo = _school_logo_in_transaction(conn)
    return {
        "session_name": session["name"],
        "entries": timetable["entries"],
        "school_profile": profile,
        "school_logo": logo,
    }


def save_timetable_entry(
    class_subject_assignment_id: int,
    day_of_week: int,
    starts_at: str,
    ends_at: str,
    location: str,
    actor: str,
    entry_id: int | None = None,
) -> int:
    if isinstance(day_of_week, bool) or not isinstance(day_of_week, int) or not 1 <= day_of_week <= 7:
        raise ValueError("Choose a weekday from Monday through Sunday.")
    starts_at = _canonical_clock_time(starts_at, "Start time")
    ends_at = _canonical_clock_time(ends_at, "End time")
    if starts_at >= ends_at:
        raise ValueError("End time must be later than start time.")
    if not isinstance(location, str):
        raise ValueError("Room or location must be text.")
    location = location.strip()
    if len(location) > 100:
        raise ValueError("Room or location must be 100 characters or fewer.")

    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        assignment = conn.execute(
            """SELECT x.academic_session_id,x.class_id,x.teacher_id,t.status AS teacher_status
               FROM class_subject_assignments x LEFT JOIN teachers t ON t.id=x.teacher_id
               WHERE x.id=?""",
            (class_subject_assignment_id,),
        ).fetchone()
        if not assignment:
            raise ValueError("Select an existing class-subject assignment.")
        if assignment["teacher_id"] is not None and (assignment["teacher_status"] or "").casefold() != "active":
            raise ValueError("The assigned teacher is no longer active; update the class-subject assignment first.")
        if entry_id is not None and not conn.execute(
            "SELECT 1 FROM timetable_entries WHERE id=?", (entry_id,)
        ).fetchone():
            raise ValueError("Timetable slot was not found.")

        overlaps = conn.execute(
            """SELECT e.id,e.location,x.class_id,x.teacher_id
               FROM timetable_entries e
               JOIN class_subject_assignments x ON x.id=e.class_subject_assignment_id
               WHERE x.academic_session_id=? AND e.day_of_week=?
                 AND e.starts_at<? AND e.ends_at>?
                 AND (? IS NULL OR e.id<>?)""",
            (assignment["academic_session_id"], day_of_week, ends_at, starts_at, entry_id, entry_id),
        ).fetchall()
        for existing in overlaps:
            if int(existing["class_id"]) == int(assignment["class_id"]):
                raise ValueError("This class already has a timetable slot during that time.")
            if assignment["teacher_id"] is not None and existing["teacher_id"] == assignment["teacher_id"]:
                raise ValueError("The assigned teacher already has a timetable slot during that time.")
            if location and existing["location"].strip().casefold() == location.casefold():
                raise ValueError("This room or location is already booked during that time.")

        if entry_id is None:
            cursor = conn.execute(
                """INSERT INTO timetable_entries
                   (class_subject_assignment_id,day_of_week,starts_at,ends_at,location,created_by,updated_by)
                   VALUES(?,?,?,?,?,?,?)""",
                (class_subject_assignment_id, day_of_week, starts_at, ends_at, location, actor, actor),
            )
            entry_id = int(cursor.lastrowid)
            action = "CREATE"
        else:
            cursor = conn.execute(
                """UPDATE timetable_entries SET class_subject_assignment_id=?,day_of_week=?,
                   starts_at=?,ends_at=?,location=?,updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (class_subject_assignment_id, day_of_week, starts_at, ends_at, location, actor, entry_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Timetable slot was not found.")
            action = "UPDATE"
        _audit_in_transaction(conn, actor, action, "timetable_entries", entry_id)
        return entry_id


def delete_timetable_entry(entry_id: int, actor: str) -> None:
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        cursor = conn.execute("DELETE FROM timetable_entries WHERE id=?", (entry_id,))
        if cursor.rowcount != 1:
            raise ValueError("Timetable slot was not found.")
        _audit_in_transaction(conn, actor, "DELETE", "timetable_entries", entry_id)


def counts():
    with connection() as db:
        return {"Students": db.execute("SELECT COUNT(*) FROM students WHERE is_active=1").fetchone()[0], "Teachers": db.execute("SELECT COUNT(*) FROM teachers WHERE status='Active'").fetchone()[0], "Classes": db.execute("SELECT COUNT(*) FROM classes").fetchone()[0], "Subjects": db.execute("SELECT COUNT(*) FROM subjects").fetchone()[0]}


def load_fee_setup_data() -> dict:
    with connection() as conn:
        sessions = [dict(row) for row in conn.execute(
            "SELECT id,name,starts_on,ends_on,is_current FROM academic_sessions ORDER BY is_current DESC,starts_on DESC,id DESC"
        )]
        classes = [dict(row) for row in conn.execute(
            "SELECT id,class_name,section,educational_stage FROM classes ORDER BY class_name,section,id"
        )]
        terms = [dict(row) for row in conn.execute(
            "SELECT id,academic_session_id,term_name,term_order,starts_on,ends_on,is_current FROM terms ORDER BY academic_session_id DESC,is_current DESC,term_order,id"
        )]
        fee_types = [dict(row) for row in conn.execute(
            "SELECT id,name,description,is_active FROM fee_types ORDER BY is_active DESC,name COLLATE NOCASE,id"
        )]
        schedules = [dict(row) for row in conn.execute(
            """SELECT f.id,f.academic_session_id,f.class_id,f.term_id,f.fee_type_id,
                      f.amount_text,f.due_on,f.is_active,f.updated_by,
                      a.name AS session_name,c.class_name,c.section,t.term_name,t.term_order,
                      ft.name AS fee_type_name
               FROM fee_schedules f
               JOIN academic_sessions a ON a.id=f.academic_session_id
               JOIN classes c ON c.id=f.class_id
               JOIN terms t ON t.id=f.term_id
               JOIN fee_types ft ON ft.id=f.fee_type_id
               ORDER BY a.starts_on DESC,c.class_name,c.section,t.term_order,ft.name COLLATE NOCASE"""
        )]
    return {"sessions": sessions, "classes": classes, "terms": terms, "fee_types": fee_types, "schedules": schedules}


def create_fee_type(name: str, description: str, actor: str) -> int:
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("Fee type name must contain between 1 and 80 characters.")
    if not isinstance(description, str) or len(description.strip()) > 240:
        raise ValueError("Fee type description must contain at most 240 characters.")
    name, description = name.strip(), description.strip()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        try:
            cursor = conn.execute(
                "INSERT INTO fee_types(name,description,created_by) VALUES(?,?,?)",
                (name, description, actor),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("A fee type with this name already exists.") from exc
        fee_type_id = int(cursor.lastrowid)
        _audit_in_transaction(conn, actor, "CREATE", "fee_types", fee_type_id)
        return fee_type_id


def set_fee_type_active(fee_type_id: int, is_active: bool, actor: str) -> None:
    if isinstance(fee_type_id, bool) or not isinstance(fee_type_id, int) or fee_type_id <= 0:
        raise ValueError("Select a valid fee type.")
    if not isinstance(is_active, bool):
        raise ValueError("Fee type status must be active or archived.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        row = conn.execute("SELECT is_active FROM fee_types WHERE id=?", (fee_type_id,)).fetchone()
        if row is None:
            raise ValueError("Fee type was not found.")
        if not is_active and conn.execute(
            "SELECT 1 FROM fee_schedules WHERE fee_type_id=? AND is_active=1 LIMIT 1", (fee_type_id,)
        ).fetchone():
            raise ValueError("Archive this fee type's active schedules before archiving the fee type.")
        if bool(row["is_active"]) == is_active:
            return
        conn.execute(
            "UPDATE fee_types SET is_active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(is_active), fee_type_id),
        )
        _audit_in_transaction(conn, actor, "ACTIVATE" if is_active else "ARCHIVE", "fee_types", fee_type_id)


def _validated_fee_amount_and_due(amount, due_on) -> tuple[str, str | None]:
    amount_value = _decimal_value(amount, "Fee amount")
    if amount_value <= 0:
        raise ValueError("Fee amount must be greater than zero.")
    if not isinstance(due_on, str):
        raise ValueError("Due date must be blank or a valid YYYY-MM-DD date.")
    due_on = due_on.strip()
    if due_on:
        try:
            if date.fromisoformat(due_on).isoformat() != due_on:
                raise ValueError
        except ValueError as exc:
            raise ValueError("Due date must be blank or a valid YYYY-MM-DD date.") from exc
    return _decimal_text(amount_value), (due_on or None)


def save_fee_schedules_for_classes(
    academic_session_id: int, term_id: int, fee_type_id: int, class_ids,
    amount, due_on: str, actor: str,
) -> list[int]:
    """Create one fee schedule per class in a single transaction.

    ``class_ids=None`` means every class in the school. The whole request is rejected,
    with the affected classes named, if any selected class already has a schedule for
    this fee type, session and term, so existing schedules are never overwritten.
    """
    ids = (academic_session_id, term_id, fee_type_id)
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in ids):
        raise ValueError("Select an academic session, term, and active fee type.")
    amount_text, due_text = _validated_fee_amount_and_due(amount, due_on)
    if class_ids is not None:
        if isinstance(class_ids, (str, bytes)) or not hasattr(class_ids, "__iter__"):
            raise ValueError("Select at least one class, or choose all classes.")
        class_ids = list(dict.fromkeys(class_ids))
        if not class_ids or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in class_ids):
            raise ValueError("Select at least one class, or choose all classes.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        if not conn.execute("SELECT 1 FROM academic_sessions WHERE id=?", (academic_session_id,)).fetchone():
            raise ValueError("Academic session was not found.")
        term = conn.execute("SELECT academic_session_id FROM terms WHERE id=?", (term_id,)).fetchone()
        if term is None or int(term["academic_session_id"]) != academic_session_id:
            raise ValueError("Select a term from the selected academic session.")
        fee_type = conn.execute("SELECT is_active FROM fee_types WHERE id=?", (fee_type_id,)).fetchone()
        if fee_type is None or not fee_type["is_active"]:
            raise ValueError("Select an active fee type.")
        if class_ids is None:
            class_ids = [int(row[0]) for row in conn.execute("SELECT id FROM classes ORDER BY class_name,section,id")]
            if not class_ids:
                raise ValueError("Add at least one class before setting a fee for all classes.")
        labels = {}
        for class_id in class_ids:
            row = conn.execute("SELECT class_name,section FROM classes WHERE id=?", (class_id,)).fetchone()
            if row is None:
                raise ValueError("A selected class was not found.")
            labels[class_id] = f"{row['class_name']} {row['section']}".strip()
        taken = [
            labels[class_id] for class_id in class_ids
            if conn.execute(
                "SELECT 1 FROM fee_schedules WHERE academic_session_id=? AND class_id=? AND term_id=? AND fee_type_id=?",
                (academic_session_id, class_id, term_id, fee_type_id),
            ).fetchone()
        ]
        if taken:
            raise ValueError(
                "A schedule for this fee type, session, and term already exists for: "
                + ", ".join(taken) + ". Deselect those classes or edit the existing schedules."
            )
        created = []
        for class_id in class_ids:
            cursor = conn.execute(
                """INSERT INTO fee_schedules
                   (academic_session_id,class_id,term_id,fee_type_id,amount_text,due_on,updated_by)
                   VALUES(?,?,?,?,?,?,?)""",
                (academic_session_id, class_id, term_id, fee_type_id, amount_text, due_text, actor),
            )
            created.append(int(cursor.lastrowid))
            _audit_in_transaction(conn, actor, "CREATE", "fee_schedules", created[-1])
        return created


def save_fee_schedule(
    academic_session_id: int,
    class_id: int,
    term_id: int,
    fee_type_id: int,
    amount,
    due_on: str,
    actor: str,
    schedule_id: int | None = None,
) -> int:
    ids = (academic_session_id, class_id, term_id, fee_type_id)
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in ids):
        raise ValueError("Select an academic session, class, term, and active fee type.")
    amount_value = _decimal_value(amount, "Fee amount")
    if amount_value <= 0:
        raise ValueError("Fee amount must be greater than zero.")
    amount_text = _decimal_text(amount_value)
    if not isinstance(due_on, str):
        raise ValueError("Due date must be blank or a valid YYYY-MM-DD date.")
    due_on = due_on.strip()
    if due_on:
        try:
            if date.fromisoformat(due_on).isoformat() != due_on:
                raise ValueError
        except ValueError as exc:
            raise ValueError("Due date must be blank or a valid YYYY-MM-DD date.") from exc
    else:
        due_on = None
    if schedule_id is not None and (
        isinstance(schedule_id, bool) or not isinstance(schedule_id, int) or schedule_id <= 0
    ):
        raise ValueError("Select a valid fee schedule.")

    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        if not conn.execute("SELECT 1 FROM academic_sessions WHERE id=?", (academic_session_id,)).fetchone():
            raise ValueError("Academic session was not found.")
        if not conn.execute("SELECT 1 FROM classes WHERE id=?", (class_id,)).fetchone():
            raise ValueError("Class was not found.")
        term = conn.execute(
            "SELECT academic_session_id FROM terms WHERE id=?", (term_id,)
        ).fetchone()
        if term is None or int(term["academic_session_id"]) != academic_session_id:
            raise ValueError("Select a term from the selected academic session.")
        fee_type = conn.execute(
            "SELECT is_active FROM fee_types WHERE id=?", (fee_type_id,)
        ).fetchone()
        if fee_type is None or not fee_type["is_active"]:
            raise ValueError("Select an active fee type.")
        try:
            if schedule_id is None:
                cursor = conn.execute(
                    """INSERT INTO fee_schedules
                       (academic_session_id,class_id,term_id,fee_type_id,amount_text,due_on,updated_by)
                       VALUES(?,?,?,?,?,?,?)""",
                    (*ids, amount_text, due_on, actor),
                )
                schedule_id = int(cursor.lastrowid)
                action = "CREATE"
            else:
                cursor = conn.execute(
                    """UPDATE fee_schedules SET academic_session_id=?,class_id=?,term_id=?,fee_type_id=?,
                       amount_text=?,due_on=?,updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (*ids, amount_text, due_on, actor, schedule_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Fee schedule was not found.")
                action = "UPDATE"
        except sqlite3.IntegrityError as exc:
            raise ValueError("A schedule already exists for this fee type, class, session, and term.") from exc
        _audit_in_transaction(conn, actor, action, "fee_schedules", schedule_id)
        return schedule_id


def set_fee_schedule_active(schedule_id: int, is_active: bool, actor: str) -> None:
    if isinstance(schedule_id, bool) or not isinstance(schedule_id, int) or schedule_id <= 0:
        raise ValueError("Select a valid fee schedule.")
    if not isinstance(is_active, bool):
        raise ValueError("Fee schedule status must be active or archived.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        row = conn.execute("SELECT is_active FROM fee_schedules WHERE id=?", (schedule_id,)).fetchone()
        if row is None:
            raise ValueError("Fee schedule was not found.")
        if bool(row["is_active"]) == is_active:
            return
        conn.execute(
            "UPDATE fee_schedules SET is_active=?,updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(is_active), actor, schedule_id),
        )
        _audit_in_transaction(conn, actor, "ACTIVATE" if is_active else "ARCHIVE", "fee_schedules", schedule_id)


def _fee_invoice_preview_in_transaction(conn: sqlite3.Connection, session_id: int, class_id: int, term_id: int) -> dict:
    term = conn.execute(
        "SELECT academic_session_id,term_name,starts_on,ends_on FROM terms WHERE id=?", (term_id,)
    ).fetchone()
    if term is None or int(term["academic_session_id"]) != session_id:
        raise ValueError("Select a term from the selected academic session.")
    if not conn.execute("SELECT 1 FROM classes WHERE id=?", (class_id,)).fetchone():
        raise ValueError("Select an existing class and section.")
    schedules = conn.execute(
        """SELECT f.id,f.amount_text,f.due_on,ft.name AS fee_type_name
           FROM fee_schedules f JOIN fee_types ft ON ft.id=f.fee_type_id
           WHERE f.academic_session_id=? AND f.class_id=? AND f.term_id=?
             AND f.is_active=1 AND ft.is_active=1
           ORDER BY ft.name COLLATE NOCASE,f.id""",
        (session_id, class_id, term_id),
    ).fetchall()
    if not schedules:
        raise ValueError("Add at least one active fee schedule before generating invoices.")
    roster = conn.execute(
        """SELECT e.id AS enrollment_id,e.student_id,e.enrolled_on,e.left_on,
                  s.admission_no,s.first_name,s.last_name
           FROM student_enrollments e JOIN students s ON s.id=e.student_id
           WHERE e.academic_session_id=? AND e.class_id=?
             AND e.enrolled_on<=? AND coalesce(e.left_on,'9999-12-31')>=?
             AND s.is_active=1
           ORDER BY s.admission_no COLLATE NOCASE,e.id""",
        (session_id, class_id, term["ends_on"], term["starts_on"]),
    ).fetchall()
    if not roster:
        raise ValueError("No active student enrollments overlap the selected term.")
    amount_total = sum((_decimal_value(row["amount_text"], "Fee amount") for row in schedules), Decimal("0"))
    students = []
    for row in roster:
        review = row["enrolled_on"] > term["starts_on"] or (
            row["left_on"] is not None and row["left_on"] < term["ends_on"]
        )
        students.append({
            "enrollment_id": int(row["enrollment_id"]),
            "admission_no": row["admission_no"],
            "student_name": " ".join(part for part in (row["first_name"], row["last_name"]) if part),
            "enrolled_on": row["enrolled_on"],
            "left_on": row["left_on"],
            "total_amount_text": _decimal_text(amount_total),
            "review_required": bool(review),
        })
    return {
        "academic_session_id": session_id,
        "class_id": class_id,
        "term_id": term_id,
        "term_name": term["term_name"],
        "term_starts_on": term["starts_on"],
        "term_ends_on": term["ends_on"],
        "schedules": [dict(row) for row in schedules],
        "students": students,
        "student_count": len(students),
        "review_count": sum(item["review_required"] for item in students),
    }


def preview_fee_invoices(academic_session_id: int, class_id: int, term_id: int) -> dict:
    with connection() as conn:
        return _fee_invoice_preview_in_transaction(conn, academic_session_id, class_id, term_id)


def generate_fee_invoice_batch(academic_session_id: int, class_id: int, term_id: int, actor: str) -> int:
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
           for value in (academic_session_id, class_id, term_id)):
        raise ValueError("Select an academic session, class, and term.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        preview = _fee_invoice_preview_in_transaction(conn, academic_session_id, class_id, term_id)
        try:
            cursor = conn.execute(
                "INSERT INTO fee_invoice_batches(academic_session_id,class_id,term_id,generated_by) VALUES(?,?,?,?)",
                (academic_session_id, class_id, term_id, actor),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("A draft invoice batch already exists for this class, session, and term.") from exc
        batch_id = int(cursor.lastrowid)
        for student in preview["students"]:
            invoice_number = f"INV-{preview['term_ends_on'][:4]}-{secrets.token_hex(5).upper()}"
            invoice = conn.execute(
                """INSERT INTO fee_invoices
                   (batch_id,enrollment_id,invoice_number,admission_no_snapshot,student_name_snapshot,
                    enrolled_on_snapshot,left_on_snapshot,total_amount_text,review_required)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (batch_id, student["enrollment_id"], invoice_number, student["admission_no"],
                 student["student_name"], student["enrolled_on"], student["left_on"],
                 student["total_amount_text"], int(student["review_required"])),
            )
            invoice_id = int(invoice.lastrowid)
            for schedule in preview["schedules"]:
                conn.execute(
                    """INSERT INTO fee_invoice_lines
                       (invoice_id,fee_schedule_id,fee_type_name_snapshot,amount_text_snapshot,due_on_snapshot)
                       SELECT ?,f.id,ft.name,f.amount_text,f.due_on
                       FROM fee_schedules f JOIN fee_types ft ON ft.id=f.fee_type_id WHERE f.id=?""",
                    (invoice_id, schedule["id"]),
                )
        _audit_in_transaction(conn, actor, "CREATE_DRAFT", "fee_invoice_batches", batch_id)
        return batch_id


def cancel_fee_invoice_batch(batch_id: int, reason: str, actor: str) -> None:
    if isinstance(batch_id, bool) or not isinstance(batch_id, int) or batch_id <= 0:
        raise ValueError("Select a valid invoice batch.")
    if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 300:
        raise ValueError("Enter a cancellation reason between 5 and 300 characters.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        cursor = conn.execute(
            """UPDATE fee_invoice_batches SET status='cancelled',cancelled_by=?,
               cancelled_at=CURRENT_TIMESTAMP,cancel_reason=? WHERE id=? AND status='draft'""",
            (actor, reason.strip(), batch_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Only an existing draft invoice batch can be cancelled.")
        _audit_in_transaction(conn, actor, "CANCEL_DRAFT", "fee_invoice_batches", batch_id)


def load_fee_invoice_data(
    academic_session_id: int | None = None, class_id: int | None = None, term_id: int | None = None
) -> dict:
    with connection() as conn:
        sessions = [dict(row) for row in conn.execute(
            "SELECT id,name,starts_on,ends_on,is_current FROM academic_sessions ORDER BY is_current DESC,starts_on DESC,id DESC"
        )]
        classes = [dict(row) for row in conn.execute(
            "SELECT id,class_name,section FROM classes ORDER BY class_name,section,id"
        )]
        terms = [dict(row) for row in conn.execute(
            "SELECT id,academic_session_id,term_name,term_order,starts_on,ends_on,is_current FROM terms ORDER BY academic_session_id DESC,is_current DESC,term_order,id"
        )]
        batches = []
        invoices = []
        payments = []
        if academic_session_id is not None:
            conditions, params = ["b.academic_session_id=?"], [academic_session_id]
            if class_id is not None:
                conditions.append("b.class_id=?")
                params.append(class_id)
            if term_id is not None:
                conditions.append("b.term_id=?")
                params.append(term_id)
            batches = [dict(row) for row in conn.execute(
                """SELECT b.id,b.academic_session_id,b.class_id,b.term_id,
                          CASE WHEN EXISTS(SELECT 1 FROM fee_invoice_issues x WHERE x.batch_id=b.id)
                               THEN 'issued' ELSE b.status END AS status,b.created_at,
                          b.generated_by,b.cancel_reason,a.name AS session_name,c.class_name,c.section,t.term_name,
                          (SELECT COUNT(*) FROM fee_invoices i WHERE i.batch_id=b.id) AS invoice_count,
                          (SELECT COUNT(*) FROM fee_invoices i WHERE i.batch_id=b.id AND i.review_required=1) AS review_count
                   FROM fee_invoice_batches b JOIN academic_sessions a ON a.id=b.academic_session_id
                   JOIN classes c ON c.id=b.class_id JOIN terms t ON t.id=b.term_id
                   WHERE """ + " AND ".join(conditions) + " ORDER BY b.id DESC", params
            )]
            if batches:
                batch_ids = [int(row["id"]) for row in batches]
                placeholders = ",".join("?" for _ in batch_ids)
                invoices = [dict(row) for row in conn.execute(
                    f"""SELECT i.id,i.batch_id,i.invoice_number,i.admission_no_snapshot,i.student_name_snapshot,
                               i.total_amount_text,i.review_required,i.created_at,e.student_id,
                               CASE WHEN x.id IS NULL THEN 0 ELSE 1 END AS is_issued
                        FROM fee_invoices i JOIN fee_invoice_batches b ON b.id=i.batch_id
                        JOIN student_enrollments e ON e.id=i.enrollment_id
                        LEFT JOIN fee_invoice_issues x ON x.batch_id=i.batch_id
                        WHERE i.batch_id IN ({placeholders})
                        ORDER BY i.batch_id DESC,i.admission_no_snapshot COLLATE NOCASE""", batch_ids
                )]
                scope_clause = " AND ".join(conditions)
                payments = [dict(row) for row in conn.execute(
                    """SELECT p.id,p.invoice_id,p.receipt_number,p.amount_text,p.received_on,p.method,
                              p.reference,p.received_by,p.created_at,p.lump_sum_number,i.invoice_number,
                              i.admission_no_snapshot,i.student_name_snapshot
                       FROM fee_payments p JOIN fee_invoices i ON i.id=p.invoice_id
                       JOIN fee_invoice_batches b ON b.id=i.batch_id
                       WHERE """ + scope_clause + " ORDER BY p.id DESC", params
                )]
                reversed_by_payment: dict[int, Decimal] = {}
                for row in conn.execute(
                    """SELECT r.payment_id,r.amount_text FROM fee_payment_reversals r
                       JOIN fee_payments p ON p.id=r.payment_id
                       JOIN fee_invoices i ON i.id=p.invoice_id
                       JOIN fee_invoice_batches b ON b.id=i.batch_id WHERE """ + scope_clause,
                    params,
                ):
                    payment_id = int(row["payment_id"])
                    reversed_by_payment[payment_id] = reversed_by_payment.get(payment_id, Decimal("0")) + _decimal_value(row["amount_text"], "Reversal amount")
                paid_by_invoice: dict[int, Decimal] = {}
                reversed_by_invoice: dict[int, Decimal] = {}
                for payment in payments:
                    payment_id, invoice_id = int(payment["id"]), int(payment["invoice_id"])
                    gross = _decimal_value(payment["amount_text"], "Payment amount")
                    reversed = reversed_by_payment.get(payment_id, Decimal("0"))
                    paid_by_invoice[invoice_id] = paid_by_invoice.get(invoice_id, Decimal("0")) + gross
                    reversed_by_invoice[invoice_id] = reversed_by_invoice.get(invoice_id, Decimal("0")) + reversed
                    payment["reversed_amount_text"] = _decimal_text(reversed)
                    payment["net_amount_text"] = _decimal_text(gross - reversed)
                for invoice in invoices:
                    invoice_id = int(invoice["id"])
                    gross = paid_by_invoice.get(invoice_id, Decimal("0"))
                    reversed = reversed_by_invoice.get(invoice_id, Decimal("0"))
                    invoice["paid_amount_text"] = _decimal_text(gross - reversed)
                    invoice["balance_amount_text"] = _decimal_text(_decimal_value(invoice["total_amount_text"], "Invoice total") - gross + reversed)
                    invoice["payment_status"] = "Paid" if invoice["balance_amount_text"] == "0" else ("Part paid" if gross > reversed else "Unpaid")
    return {"sessions": sessions, "classes": classes, "terms": terms, "batches": batches, "invoices": invoices, "payments": payments}


PAYMENT_METHODS = {"cash", "bank_transfer", "mobile_money", "cheque", "other"}


def issue_fee_invoice_batch(batch_id: int, actor: str) -> None:
    if isinstance(batch_id, bool) or not isinstance(batch_id, int) or batch_id <= 0:
        raise ValueError("Select a valid invoice batch.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        batch = conn.execute("SELECT status FROM fee_invoice_batches WHERE id=?", (batch_id,)).fetchone()
        if batch is None or batch["status"] != "draft":
            raise ValueError("Select an existing draft invoice batch to issue.")
        if conn.execute("SELECT 1 FROM fee_invoice_issues WHERE batch_id=?", (batch_id,)).fetchone():
            raise ValueError("This invoice batch has already been issued.")
        if not conn.execute("SELECT 1 FROM fee_invoices WHERE batch_id=? LIMIT 1", (batch_id,)).fetchone():
            raise ValueError("This invoice batch has no invoices to issue.")
        cursor = conn.execute("INSERT INTO fee_invoice_issues(batch_id,issued_by) VALUES(?,?)", (batch_id, actor))
        _audit_in_transaction(conn, actor, "ISSUE", "fee_invoice_batches", batch_id)


def record_fee_payment(
    invoice_id: int, amount, received_on: str, method: str, reference: str, actor: str
) -> str:
    if isinstance(invoice_id, bool) or not isinstance(invoice_id, int) or invoice_id <= 0:
        raise ValueError("Select a valid student invoice.")
    value = _decimal_value(amount, "Payment amount")
    if value <= 0:
        raise ValueError("Payment amount must be greater than zero.")
    if not isinstance(received_on, str):
        raise ValueError("Payment date must use YYYY-MM-DD format.")
    received_on = _iso_date(received_on, "Payment date").isoformat()
    if not isinstance(method, str) or method not in PAYMENT_METHODS:
        raise ValueError("Choose a supported payment method.")
    if not isinstance(reference, str) or len(reference.strip()) > 100:
        raise ValueError("Payment reference must be 100 characters or fewer.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        invoice = conn.execute(
            """SELECT i.total_amount_text,b.id AS batch_id
               FROM fee_invoices i JOIN fee_invoice_batches b ON b.id=i.batch_id WHERE i.id=?""",
            (invoice_id,),
        ).fetchone()
        if invoice is None:
            raise ValueError("Student invoice was not found.")
        if not conn.execute("SELECT 1 FROM fee_invoice_issues WHERE batch_id=?", (invoice["batch_id"],)).fetchone():
            raise ValueError("Issue the invoice batch before recording a payment.")
        payments = conn.execute("SELECT id,amount_text FROM fee_payments WHERE invoice_id=?", (invoice_id,)).fetchall()
        gross = sum((_decimal_value(row["amount_text"], "Payment amount") for row in payments), Decimal("0"))
        reversals = Decimal("0")
        if payments:
            payment_ids = [int(row["id"]) for row in payments]
            placeholders = ",".join("?" for _ in payment_ids)
            reversals = sum((_decimal_value(row[0], "Reversal amount") for row in conn.execute(
                f"SELECT amount_text FROM fee_payment_reversals WHERE payment_id IN ({placeholders})", payment_ids
            )), Decimal("0"))
        balance = _decimal_value(invoice["total_amount_text"], "Invoice total") - gross + reversals
        if value > balance:
            raise ValueError(f"Payment exceeds the remaining invoice balance of {_decimal_text(balance)}.")
        receipt_number = f"RCP-{received_on[:4]}-{secrets.token_hex(5).upper()}"
        cursor = conn.execute(
            """INSERT INTO fee_payments
               (invoice_id,receipt_number,amount_text,received_on,method,reference,received_by)
               VALUES(?,?,?,?,?,?,?)""",
            (invoice_id, receipt_number, _decimal_text(value), received_on, method, reference.strip(), actor),
        )
        _audit_in_transaction(conn, actor, "RECEIVE", "fee_payments", int(cursor.lastrowid))
        return receipt_number


def _invoice_net_paid(conn: sqlite3.Connection, invoice_id: int) -> Decimal:
    gross = sum((_decimal_value(row[0], "Payment amount") for row in conn.execute(
        "SELECT amount_text FROM fee_payments WHERE invoice_id=?", (invoice_id,)
    )), Decimal("0"))
    reversed_total = sum((_decimal_value(row[0], "Reversal amount") for row in conn.execute(
        """SELECT r.amount_text FROM fee_payment_reversals r
           JOIN fee_payments p ON p.id=r.payment_id WHERE p.invoice_id=?""", (invoice_id,)
    )), Decimal("0"))
    return gross - reversed_total


def _fee_statement_in_transaction(conn: sqlite3.Connection, student_id: int) -> dict:
    """Issued invoices for one student through the active term, oldest first, with balances.

    Earlier unpaid terms (and sessions) stay on the statement until paid, so a debt is
    carried forward while a fully paid term adds nothing. Invoices for terms after the
    active term are counted but not included in what is outstanding now.
    """
    student = conn.execute(
        "SELECT id,admission_no,first_name,last_name,is_active FROM students WHERE id=?", (student_id,)
    ).fetchone()
    if student is None:
        raise ValueError("Select an existing student.")
    session = conn.execute("SELECT id,name,starts_on FROM academic_sessions WHERE is_current=1").fetchone()
    term = None
    if session is not None:
        term = conn.execute(
            "SELECT id,term_name,term_order FROM terms WHERE academic_session_id=? AND is_current=1", (session["id"],)
        ).fetchone()
    if session is None or term is None:
        raise ValueError("Set the current academic session and term in Academic Calendar before using fee statements.")
    rows = conn.execute(
        """SELECT i.id,i.invoice_number,i.total_amount_text,b.academic_session_id,
                  a.name AS session_name,a.starts_on,t.term_name,t.term_order,c.class_name,c.section
           FROM fee_invoices i JOIN student_enrollments e ON e.id=i.enrollment_id
           JOIN fee_invoice_batches b ON b.id=i.batch_id
           JOIN fee_invoice_issues x ON x.batch_id=b.id
           JOIN academic_sessions a ON a.id=b.academic_session_id
           JOIN terms t ON t.id=b.term_id JOIN classes c ON c.id=b.class_id
           WHERE e.student_id=? ORDER BY a.starts_on,t.term_order,i.id""",
        (student_id,),
    ).fetchall()
    items, later = [], 0
    billed = paid_total = outstanding = Decimal("0")
    for row in rows:
        same_session = int(row["academic_session_id"]) == int(session["id"])
        if (not same_session and row["starts_on"] > session["starts_on"]) or (
            same_session and int(row["term_order"]) > int(term["term_order"])
        ):
            later += 1
            continue
        total = _decimal_value(row["total_amount_text"], "Invoice total")
        paid = _invoice_net_paid(conn, int(row["id"]))
        balance = total - paid
        fees = ", ".join(line[0] for line in conn.execute(
            "SELECT fee_type_name_snapshot FROM fee_invoice_lines WHERE invoice_id=? ORDER BY id", (row["id"],)
        ))
        items.append({
            "invoice_id": int(row["id"]), "invoice_number": row["invoice_number"],
            "session_name": row["session_name"], "term_name": row["term_name"],
            "term_order": int(row["term_order"]), "class_name": row["class_name"], "section": row["section"],
            "fees": fees, "total_amount_text": _decimal_text(total), "paid_amount_text": _decimal_text(paid),
            "balance_amount_text": _decimal_text(balance),
            "status": "Paid" if balance <= 0 else ("Part paid" if paid > 0 else "Unpaid"),
            "carried_over": not (same_session and int(row["term_order"]) == int(term["term_order"])),
        })
        billed += total
        paid_total += paid
        outstanding += balance
    return {
        "student": {
            "id": int(student["id"]), "admission_no": student["admission_no"],
            "first_name": student["first_name"], "last_name": student["last_name"],
        },
        "current_session": session["name"], "current_term": term["term_name"],
        "items": items, "later_term_invoice_count": later,
        "total_billed": _decimal_text(billed), "total_paid": _decimal_text(paid_total),
        "total_outstanding": _decimal_text(outstanding),
    }


def load_student_fee_statement(student_id: int, actor: str) -> dict:
    """Administrator-only fee statement for one student through the active term."""
    if isinstance(student_id, bool) or not isinstance(student_id, int) or student_id <= 0:
        raise ValueError("Select an existing student.")
    with connection() as conn:
        conn.execute("BEGIN")
        _require_admin(conn, actor)
        statement = _fee_statement_in_transaction(conn, student_id)
        statement["school_profile"] = _school_profile_in_transaction(conn)
        statement["school_logo"] = _school_logo_in_transaction(conn)
        return statement


def record_fee_lump_sum_payment(
    student_id: int, amount, received_on: str, method: str, reference: str, actor: str
) -> dict:
    """Record one payment and allocate it oldest-first across a student's outstanding invoices.

    Only issued invoices up to the active term are paid. The amount may not exceed what
    is outstanding. Each allocation is a normal immutable payment row, all sharing one
    lump-sum receipt number, so individual allocations can still be reversed with a reason.
    """
    if isinstance(student_id, bool) or not isinstance(student_id, int) or student_id <= 0:
        raise ValueError("Select an existing student.")
    value = _decimal_value(amount, "Payment amount")
    if value <= 0:
        raise ValueError("Payment amount must be greater than zero.")
    if not isinstance(received_on, str):
        raise ValueError("Payment date must use YYYY-MM-DD format.")
    received_on = _iso_date(received_on, "Payment date").isoformat()
    if not isinstance(method, str) or method not in PAYMENT_METHODS:
        raise ValueError("Choose a supported payment method.")
    if not isinstance(reference, str) or len(reference.strip()) > 100:
        raise ValueError("Payment reference must be 100 characters or fewer.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        statement = _fee_statement_in_transaction(conn, student_id)
        outstanding = _decimal_value(statement["total_outstanding"], "Outstanding balance")
        if outstanding <= 0:
            raise ValueError("This student has no outstanding balance up to the active term.")
        if value > outstanding:
            raise ValueError(f"Payment exceeds the outstanding balance of {_decimal_text(outstanding)}.")
        lump_sum_number = f"LS-{received_on[:4]}-{secrets.token_hex(4).upper()}"
        remaining = value
        allocations = []
        for item in statement["items"]:
            balance = _decimal_value(item["balance_amount_text"], "Invoice balance")
            if balance <= 0:
                continue
            part = min(balance, remaining)
            receipt_number = f"RCP-{received_on[:4]}-{secrets.token_hex(5).upper()}"
            cursor = conn.execute(
                """INSERT INTO fee_payments
                   (invoice_id,receipt_number,amount_text,received_on,method,reference,received_by,lump_sum_number)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (item["invoice_id"], receipt_number, _decimal_text(part), received_on, method,
                 reference.strip(), actor, lump_sum_number),
            )
            _audit_in_transaction(conn, actor, "RECEIVE", "fee_payments", int(cursor.lastrowid))
            allocations.append({
                "payment_id": int(cursor.lastrowid), "receipt_number": receipt_number,
                "invoice_number": item["invoice_number"], "session_name": item["session_name"],
                "term_name": item["term_name"], "amount_text": _decimal_text(part),
            })
            remaining -= part
            if remaining <= 0:
                break
        return {"lump_sum_number": lump_sum_number, "total_amount_text": _decimal_text(value), "allocations": allocations}


def load_fee_lump_sum_receipt_data(lump_sum_number: str, actor: str) -> dict:
    """Administrator-only data for one printable lump-sum receipt and its allocations."""
    if not isinstance(lump_sum_number, str) or not lump_sum_number.strip():
        raise ValueError("Select a valid lump-sum receipt.")
    with connection() as conn:
        conn.execute("BEGIN")
        _require_admin(conn, actor)
        rows = conn.execute(
            """SELECT p.id,p.invoice_id,p.receipt_number,p.amount_text,p.received_on,p.method,p.reference,
                      p.received_by,i.invoice_number,i.admission_no_snapshot,i.student_name_snapshot,
                      i.total_amount_text,a.name AS session_name,t.term_name,t.term_order,c.class_name,c.section
               FROM fee_payments p JOIN fee_invoices i ON i.id=p.invoice_id
               JOIN fee_invoice_batches b ON b.id=i.batch_id
               JOIN academic_sessions a ON a.id=b.academic_session_id
               JOIN terms t ON t.id=b.term_id JOIN classes c ON c.id=b.class_id
               WHERE p.lump_sum_number=? ORDER BY a.starts_on,t.term_order,p.id""",
            (lump_sum_number.strip(),),
        ).fetchall()
        if not rows:
            raise ValueError("Lump-sum receipt was not found.")
        allocations = []
        total = reversed_total = Decimal("0")
        for row in rows:
            amount = _decimal_value(row["amount_text"], "Payment amount")
            reversed_amount = sum((_decimal_value(r[0], "Reversal amount") for r in conn.execute(
                "SELECT amount_text FROM fee_payment_reversals WHERE payment_id=?", (row["id"],)
            )), Decimal("0"))
            fees = ", ".join(line[0] for line in conn.execute(
                "SELECT fee_type_name_snapshot FROM fee_invoice_lines WHERE invoice_id=? ORDER BY id", (row["invoice_id"],)
            ))
            invoice_balance = _decimal_value(row["total_amount_text"], "Invoice total") - _invoice_net_paid(conn, int(row["invoice_id"]))
            allocations.append({
                "receipt_number": row["receipt_number"], "invoice_number": row["invoice_number"],
                "session_name": row["session_name"], "term_name": row["term_name"],
                "term_order": int(row["term_order"]), "class_name": row["class_name"], "section": row["section"],
                "fees": fees, "amount_text": _decimal_text(amount),
                "reversed_amount_text": _decimal_text(reversed_amount),
                "invoice_balance_text": _decimal_text(invoice_balance),
            })
            total += amount
            reversed_total += reversed_amount
        first = rows[0]
        return {
            "lump_sum_number": lump_sum_number.strip(),
            "student_name": first["student_name_snapshot"], "admission_no": first["admission_no_snapshot"],
            "received_on": first["received_on"], "method": first["method"],
            "reference": first["reference"], "received_by": first["received_by"],
            "allocations": allocations, "total_amount_text": _decimal_text(total),
            "net_amount_text": _decimal_text(total - reversed_total),
            "school_profile": _school_profile_in_transaction(conn),
            "school_logo": _school_logo_in_transaction(conn),
        }


def reverse_fee_payment(payment_id: int, amount, reason: str, actor: str) -> None:
    if isinstance(payment_id, bool) or not isinstance(payment_id, int) or payment_id <= 0:
        raise ValueError("Select a valid payment receipt.")
    value = _decimal_value(amount, "Reversal amount")
    if value <= 0:
        raise ValueError("Reversal amount must be greater than zero.")
    if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 300:
        raise ValueError("Enter a reversal reason between 5 and 300 characters.")
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _require_admin(conn, actor)
        payment = conn.execute("SELECT amount_text FROM fee_payments WHERE id=?", (payment_id,)).fetchone()
        if payment is None:
            raise ValueError("Payment receipt was not found.")
        reversed_value = sum((_decimal_value(row[0], "Reversal amount") for row in conn.execute(
            "SELECT amount_text FROM fee_payment_reversals WHERE payment_id=?", (payment_id,)
        )), Decimal("0"))
        remaining = _decimal_value(payment["amount_text"], "Payment amount") - reversed_value
        if value > remaining:
            raise ValueError(f"Reversal exceeds the unreversed receipt amount of {_decimal_text(remaining)}.")
        cursor = conn.execute(
            """INSERT INTO fee_payment_reversals(payment_id,amount_text,reason,reversed_by)
               VALUES(?,?,?,?)""",
            (payment_id, _decimal_text(value), reason.strip(), actor),
        )
        _audit_in_transaction(conn, actor, "REVERSE", "fee_payment_reversals", int(cursor.lastrowid))


def load_fee_receipt_data(payment_id: int) -> dict:
    if isinstance(payment_id, bool) or not isinstance(payment_id, int) or payment_id <= 0:
        raise ValueError("Select a valid payment receipt.")
    with connection() as conn:
        payment = conn.execute(
            """SELECT p.*,i.invoice_number,i.admission_no_snapshot,i.student_name_snapshot,
                      i.total_amount_text,i.batch_id,b.academic_session_id,b.class_id,b.term_id,
                      a.name AS session_name,c.class_name,c.section,t.term_name,t.term_order
               FROM fee_payments p JOIN fee_invoices i ON i.id=p.invoice_id
               JOIN fee_invoice_batches b ON b.id=i.batch_id
               JOIN academic_sessions a ON a.id=b.academic_session_id
               JOIN classes c ON c.id=b.class_id JOIN terms t ON t.id=b.term_id
               WHERE p.id=?""",
            (payment_id,),
        ).fetchone()
        if payment is None:
            raise ValueError("Payment receipt was not found.")
        lines = [dict(row) for row in conn.execute(
            "SELECT fee_type_name_snapshot,amount_text_snapshot,due_on_snapshot FROM fee_invoice_lines WHERE invoice_id=(SELECT invoice_id FROM fee_payments WHERE id=?) ORDER BY id",
            (payment_id,),
        )]
        reversals = [dict(row) for row in conn.execute(
            "SELECT amount_text,reason,reversed_by,created_at FROM fee_payment_reversals WHERE payment_id=? ORDER BY id",
            (payment_id,),
        )]
        profile = _school_profile_in_transaction(conn)
        logo = _school_logo_in_transaction(conn)
        invoice_payments = conn.execute(
            "SELECT id,amount_text FROM fee_payments WHERE invoice_id=?",
            (payment["invoice_id"],),
        ).fetchall()
        gross = sum((_decimal_value(row["amount_text"], "Payment amount") for row in invoice_payments), Decimal("0"))
        reverse_total = sum((_decimal_value(row["amount_text"], "Reversal amount") for row in conn.execute(
            "SELECT r.amount_text FROM fee_payment_reversals r JOIN fee_payments p ON p.id=r.payment_id WHERE p.invoice_id=?",
            (payment["invoice_id"],),
        )), Decimal("0"))
        receipt_reversed = sum((_decimal_value(row["amount_text"], "Reversal amount") for row in reversals), Decimal("0"))
    return {
        "payment": dict(payment),
        "lines": lines,
        "reversals": reversals,
        "receipt_net_amount_text": _decimal_text(_decimal_value(payment["amount_text"], "Payment amount") - receipt_reversed),
        "invoice_paid_amount_text": _decimal_text(gross - reverse_total),
        "invoice_balance_text": _decimal_text(_decimal_value(payment["total_amount_text"], "Invoice total") - gross + reverse_total),
        "school_profile": profile,
        "school_logo": logo,
    }


def load_fee_reconciliation(start_on: str, end_on: str, actor: str) -> dict:
    start = _iso_date(start_on, "Start date").isoformat()
    end = _iso_date(end_on, "End date").isoformat()
    if start > end:
        raise ValueError("Start date must be on or before end date.")
    with connection() as conn:
        conn.execute("BEGIN")
        _require_admin(conn, actor)
        receipts = [dict(row) for row in conn.execute(
            """SELECT p.id,p.receipt_number,p.received_on,p.amount_text,p.method,p.reference,
                      i.invoice_number,i.admission_no_snapshot,i.student_name_snapshot
               FROM fee_payments p JOIN fee_invoices i ON i.id=p.invoice_id
               WHERE p.received_on BETWEEN ? AND ? ORDER BY p.received_on,p.id""",
            (start, end),
        )]
        reversals = [dict(row) for row in conn.execute(
            """SELECT r.id,r.amount_text,r.reason,r.created_at,p.receipt_number,p.method,
                      i.invoice_number,i.admission_no_snapshot,i.student_name_snapshot
               FROM fee_payment_reversals r JOIN fee_payments p ON p.id=r.payment_id
               JOIN fee_invoices i ON i.id=p.invoice_id
               WHERE substr(r.created_at,1,10) BETWEEN ? AND ? ORDER BY r.created_at,r.id""",
            (start, end),
        )]
    receipt_total = sum((_decimal_value(row["amount_text"], "Payment amount") for row in receipts), Decimal("0"))
    reversal_total = sum((_decimal_value(row["amount_text"], "Reversal amount") for row in reversals), Decimal("0"))
    methods: dict[str, dict[str, Decimal]] = {}
    events = []
    for row in receipts:
        method = row["method"]
        methods.setdefault(method, {"received": Decimal("0"), "reversed": Decimal("0")})["received"] += _decimal_value(row["amount_text"], "Payment amount")
        events.append({"date": row["received_on"], "type": "Receipt", "receipt_number": row["receipt_number"], "invoice_number": row["invoice_number"], "admission_no": row["admission_no_snapshot"], "student_name": row["student_name_snapshot"], "method": method, "amount_text": row["amount_text"], "reference_or_reason": row["reference"]})
    for row in reversals:
        method = row["method"]
        methods.setdefault(method, {"received": Decimal("0"), "reversed": Decimal("0")})["reversed"] += _decimal_value(row["amount_text"], "Reversal amount")
        events.append({"date": row["created_at"][:10], "type": "Reversal", "receipt_number": row["receipt_number"], "invoice_number": row["invoice_number"], "admission_no": row["admission_no_snapshot"], "student_name": row["student_name_snapshot"], "method": method, "amount_text": row["amount_text"], "reference_or_reason": row["reason"]})
    by_method = [
        {"method": method, "received_text": _decimal_text(values["received"]),
         "reversed_text": _decimal_text(values["reversed"]),
         "net_text": _decimal_text(values["received"] - values["reversed"])}
        for method, values in sorted(methods.items())
    ]
    events.sort(key=lambda row: (row["date"], row["type"], row["receipt_number"]))
    return {
        "start_on": start,
        "end_on": end,
        "receipt_count": len(receipts),
        "reversal_count": len(reversals),
        "received_total_text": _decimal_text(receipt_total),
        "reversed_total_text": _decimal_text(reversal_total),
        "net_total_text": _decimal_text(receipt_total - reversal_total),
        "by_method": by_method,
        "events": events,
    }
