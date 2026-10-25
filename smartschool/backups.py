"""Consistent, verified backups for the local SQLite school database."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from . import database as db


@dataclass(frozen=True)
class BackupVerification:
    path: Path
    schema_version: int
    integrity: str


def _open_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"Database file not found: {path}")
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)


def verify_backup(path: str | Path) -> BackupVerification:
    """Check SQLite integrity and confirm the migration ledger is complete."""
    backup_path = Path(path).expanduser().resolve()
    with closing(_open_read_only(backup_path)) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"Backup integrity check failed: {integrity}")
        foreign_key_issue = conn.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_key_issue is not None:
            table_name = str(foreign_key_issue[0])
            raise sqlite3.DatabaseError(
                f"Backup has a foreign-key violation in table {table_name}."
            )
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version < 1 or version > db.SCHEMA_VERSION:
            raise sqlite3.DatabaseError(f"Unsupported database schema version: {version}")
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "schema_migrations" not in tables:
            raise sqlite3.DatabaseError("Backup has no schema migration history.")
        versions = [
            int(row[0])
            for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]
        if versions != list(range(1, version + 1)):
            raise sqlite3.DatabaseError("Backup migration history is incomplete.")
    return BackupVerification(backup_path, version, integrity)


def _publish_verified_copy(source_path: Path, destination_path: Path) -> BackupVerification:
    if destination_path.exists():
        raise FileExistsError(f"A file already exists at {destination_path}; choose a new file name.")
    if not destination_path.parent.is_dir():
        raise FileNotFoundError("The selected destination folder does not exist.")
    _open_read_only(source_path).close()

    handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination_path.name}.",
        suffix=".partial",
        dir=destination_path.parent,
        delete=False,
    )
    temporary_path = Path(handle.name)
    handle.close()
    reserved_destination = False
    try:
        with closing(_open_read_only(source_path)) as source:
            with closing(sqlite3.connect(temporary_path)) as target:
                source.backup(target, pages=256, sleep=0.01)
        verified = verify_backup(temporary_path)
        # Reserve the final path atomically, then publish the verified file on the same volume.
        descriptor = os.open(destination_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        reserved_destination = True
        os.replace(temporary_path, destination_path)
        reserved_destination = False
        return BackupVerification(destination_path, verified.schema_version, verified.integrity)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        if reserved_destination:
            destination_path.unlink(missing_ok=True)
        raise


def create_backup(destination: str | Path) -> BackupVerification:
    """Create a consistent online snapshot and verify it before publishing it."""
    source_path = Path(db.DB_PATH).expanduser().resolve()
    destination_path = Path(destination).expanduser().resolve()
    if destination_path == source_path:
        raise ValueError("Choose a different file for the backup.")
    if not source_path.is_file():
        raise FileNotFoundError("The school database does not exist yet.")
    return _publish_verified_copy(source_path, destination_path)


def restore_to_new_file(source: str | Path, destination: str | Path) -> BackupVerification:
    """Restore a verified backup to a new file without touching the active database."""
    source_path = Path(source).expanduser().resolve()
    destination_path = Path(destination).expanduser().resolve()
    if destination_path == source_path or destination_path == Path(db.DB_PATH).resolve():
        raise ValueError("Choose a new file separate from the backup and active database.")
    verified_source = verify_backup(source_path)
    restored = _publish_verified_copy(source_path, destination_path)
    if restored.schema_version != verified_source.schema_version:
        destination_path.unlink(missing_ok=True)
        raise sqlite3.DatabaseError("Restored database schema does not match the source backup.")
    return restored
