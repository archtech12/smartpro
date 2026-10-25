"""Read-only, local diagnostics for the active school database."""

from __future__ import annotations

import shutil
from pathlib import Path

from . import database as db


COUNT_TABLES = (
    ("Students", "students"),
    ("Teachers", "teachers"),
    ("Classes", "classes"),
    ("Subjects", "subjects"),
    ("Enrollments", "student_enrollments"),
    ("Attendance registers", "attendance_registers"),
    ("Timetable slots", "timetable_entries"),
)


def collect_diagnostics() -> dict:
    """Run read-only SQLite checks and return a user-displayable summary."""
    path = Path(db.DB_PATH).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("The active school database file was not found.")
    disk = shutil.disk_usage(path.parent)
    with db.connection() as conn:
        quick_check = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
        fk_violations = sum(1 for _row in conn.execute("PRAGMA foreign_key_check"))
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "schema_migrations" not in tables:
            raise RuntimeError("Schema migration history is missing.")
        applied = [
            int(row[0])
            for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]
        expected = list(range(1, version + 1))
        counts = {}
        for label, table in COUNT_TABLES:
            if table in tables:
                counts[label] = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    ledger_ok = version == db.SCHEMA_VERSION and applied == expected
    return {
        "database_path": str(path),
        "database_size_bytes": path.stat().st_size,
        "free_disk_bytes": disk.free,
        "schema_version": version,
        "supported_schema_version": db.SCHEMA_VERSION,
        "migration_ledger_ok": ledger_ok,
        "migration_versions": applied,
        "quick_check": quick_check,
        "foreign_key_violations": fk_violations,
        "record_counts": counts,
        "overall_ok": quick_check == ["ok"] and fk_violations == 0 and ledger_ok,
    }
