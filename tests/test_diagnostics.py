import pytest

from smartschool import database as db
from smartschool.diagnostics import collect_diagnostics


@pytest.fixture
def diagnostics_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    db.create_user("admin", "synthetic-hash")
    class_id = db.add_row("Classes", ["Grade 4", "A", ""], actor="admin")
    db.add_row("Students", ["S-401", "Synthetic", "Student", "", "", ""], actor="admin")
    return tmp_path


def test_diagnostics_checks_schema_counts_and_leaves_db_unchanged(diagnostics_db):
    before = db.DB_PATH.read_bytes()
    report = collect_diagnostics()
    assert report["overall_ok"] is True
    assert report["quick_check"] == ["ok"]
    assert report["foreign_key_violations"] == 0
    assert report["migration_ledger_ok"] is True
    assert report["schema_version"] == db.SCHEMA_VERSION
    assert report["record_counts"]["Students"] == 1
    assert report["record_counts"]["Classes"] == 1
    assert db.DB_PATH.read_bytes() == before


def test_diagnostics_reports_incomplete_migration_ledger(diagnostics_db):
    with db.connection() as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version=?", (db.SCHEMA_VERSION,))
    report = collect_diagnostics()
    assert report["migration_ledger_ok"] is False
    assert report["overall_ok"] is False


def test_diagnostics_requires_existing_database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "missing.db")
    with pytest.raises(FileNotFoundError, match="not found"):
        collect_diagnostics()
