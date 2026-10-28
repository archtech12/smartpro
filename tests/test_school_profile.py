import sqlite3
import base64

import pytest

from smartschool import database as db


@pytest.fixture
def school_profile_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    db.create_user("admin", "synthetic-hash")
    db.create_user("teacher", "synthetic-hash", "teacher")
    return tmp_path


def test_school_profile_saves_and_loads_with_admin_audit(school_profile_db):
    profile = {
        "school_name": "Synthetic Primary School",
        "address": "12 Example Road",
        "phone": "+1 555 0100",
        "email": "office@example.test",
        "head_name": "A. Administrator",
        "head_title": "Head Teacher",
        "class_teacher_name": "T. Teacher",
        "class_teacher_title": "Class Teacher",
        "school_type": "combined",
    }
    db.save_school_profile(profile, "admin")
    assert db.load_school_profile() == profile
    with db.connection() as conn:
        event = conn.execute(
            "SELECT action,entity,entity_id FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert tuple(event) == ("UPDATE", "school_profile", "1")


def test_school_profile_rejects_non_admin_invalid_email_and_overlong_values(school_profile_db):
    with pytest.raises(PermissionError):
        db.save_school_profile({"school_name": "No"}, "teacher")
    with pytest.raises(ValueError, match="valid email"):
        db.save_school_profile({"email": "bad-address"}, "admin")
    with pytest.raises(ValueError, match="120 characters"):
        db.save_school_profile({"school_name": "x" * 121}, "admin")
    with pytest.raises(ValueError, match="school type"):
        db.save_school_profile({"school_type": "tertiary"}, "admin")
    assert db.load_school_profile()["school_name"] == ""


def test_school_profile_audit_failure_rolls_back(school_profile_db, monkeypatch):
    def fail_audit(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic profile audit failure")

    monkeypatch.setattr(db, "_audit_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="profile audit failure"):
        db.save_school_profile({"school_name": "Should not persist"}, "admin")
    assert db.load_school_profile()["school_name"] == ""


def test_school_logo_is_stored_locally_and_validated(school_profile_db):
    logo = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aocAAAAASUVORK5CYII="
    )
    db.save_school_profile({"school_name": "Logo School"}, "admin", logo)
    assert db.load_school_logo() == logo
    with pytest.raises(ValueError, match="PNG or JPEG"):
        db.save_school_profile({"school_name": "Nope"}, "admin", b"not-an-image")
    with pytest.raises(ValueError, match="1 MB"):
        db.save_school_profile({"school_name": "Nope"}, "admin", b"\x89PNG\r\n\x1a\n" + b"0" * 1_000_001)
    assert db.load_school_profile()["school_name"] == "Logo School"


def test_school_profile_can_remove_existing_logo(school_profile_db):
    logo = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aocAAAAASUVORK5CYII="
    )
    db.save_school_profile({}, "admin", logo)
    db.save_school_profile({"school_name": "No Logo"}, "admin", None)
    assert db.load_school_logo() is None


def test_failed_v12_migration_rolls_back_and_keeps_v11_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    conn = sqlite3.connect(db.DB_PATH)
    try:
        for migration in (
            db._migration_1, db._migration_2, db._migration_3, db._migration_4,
            db._migration_5, db._migration_6, db._migration_7, db._migration_8,
            db._migration_9, db._migration_10, db._migration_11,
        ):
            conn.execute("BEGIN IMMEDIATE")
            migration(conn)
            conn.commit()
    finally:
        conn.close()

    migration_12 = db._migration_12

    def fail_after_ddl(conn):
        migration_12(conn)
        raise RuntimeError("synthetic v12 migration failure")

    monkeypatch.setattr(db, "_migration_12", fail_after_ddl)
    with pytest.raises(RuntimeError, match="v12 migration failure"):
        db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='school_profile'").fetchone() is None
    backups = list(tmp_path.glob("school.pre-migration-v11-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11


def test_failed_v13_migration_rolls_back_and_keeps_v12_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    conn = sqlite3.connect(db.DB_PATH)
    try:
        for migration in (
            db._migration_1, db._migration_2, db._migration_3, db._migration_4,
            db._migration_5, db._migration_6, db._migration_7, db._migration_8,
            db._migration_9, db._migration_10, db._migration_11, db._migration_12,
        ):
            conn.execute("BEGIN IMMEDIATE")
            migration(conn)
            conn.commit()
    finally:
        conn.close()

    migration_13 = db._migration_13

    def fail_after_ddl(conn):
        migration_13(conn)
        raise RuntimeError("synthetic v13 migration failure")

    monkeypatch.setattr(db, "_migration_13", fail_after_ddl)
    with pytest.raises(RuntimeError, match="v13 migration failure"):
        db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 12
        columns = {row[1] for row in conn.execute("PRAGMA table_info(school_profile)")}
        assert "logo_data" not in columns
    backups = list(tmp_path.glob("school.pre-migration-v12-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 12


PNG_PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aocAAAAASUVORK5CYII="
)


def test_report_ranking_style_defaults_to_position_and_is_validated(school_profile_db):
    assert db.load_report_settings() == {
        "ranking_style": "position", "head_signature": None, "class_teacher_signature": None,
        "term_closing_date": "", "next_term_begins": "",
    }
    db.save_school_profile({"school_name": "Synthetic School"}, "admin", report_ranking_style="average_grade")
    assert db.load_report_settings()["ranking_style"] == "average_grade"
    # Saving the profile without mentioning the style leaves the saved style alone.
    db.save_school_profile({"school_name": "Renamed School"}, "admin")
    assert db.load_report_settings()["ranking_style"] == "average_grade"
    for bad in ("both", "", None, "Position"):
        with pytest.raises(ValueError, match="ranking style"):
            db.save_school_profile({"school_name": "Nope"}, "admin", report_ranking_style=bad)
    assert db.load_school_profile()["school_name"] == "Renamed School"
    with pytest.raises(PermissionError):
        db.save_school_profile({}, "teacher", report_ranking_style="position")


def test_report_signatures_are_stored_validated_and_removable(school_profile_db):
    db.save_school_profile(
        {"school_name": "Signed"}, "admin", head_signature=PNG_PIXEL, class_teacher_signature=PNG_PIXEL + b"x"
    )
    settings = db.load_report_settings()
    assert settings["head_signature"] == PNG_PIXEL and settings["class_teacher_signature"] == PNG_PIXEL + b"x"
    # Leaving a signature out keeps it; None removes only that one.
    db.save_school_profile({"school_name": "Signed"}, "admin", head_signature=None)
    settings = db.load_report_settings()
    assert settings["head_signature"] is None and settings["class_teacher_signature"] == PNG_PIXEL + b"x"
    for bad, message in ((b"not-an-image", "PNG or JPEG"), (b"\x89PNG\r\n\x1a\n" + b"0" * 1_000_001, "1 MB"), ("text", "validated")):
        with pytest.raises(ValueError, match=message):
            db.save_school_profile({}, "admin", class_teacher_signature=bad)
        with pytest.raises(ValueError, match=message):
            db.save_school_profile({}, "admin", head_signature=bad)
    assert db.load_report_settings()["class_teacher_signature"] == PNG_PIXEL + b"x"
    assert db.load_school_logo() is None  # the logo is independent of the signatures


def test_v22_database_upgrades_to_report_settings_and_lump_sum_without_losing_data(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    db.create_user("admin", "synthetic-hash")
    db.save_school_profile({"school_name": "Kept School", "school_type": "combined"}, "admin", PNG_PIXEL)
    with db.connection() as conn:  # roll the schema back to v22 the way a pre-upgrade database looks
        conn.execute("DROP INDEX idx_fee_payments_lump_sum")
        conn.execute("ALTER TABLE fee_payments DROP COLUMN lump_sum_number")
        for column in ("report_ranking_style", "head_signature", "class_teacher_signature"):
            conn.execute(f"ALTER TABLE school_profile DROP COLUMN {column}")
        conn.execute("DELETE FROM schema_migrations WHERE version>22")
        conn.execute("PRAGMA user_version=22")

    db.initialize()

    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert [row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")][-5:] == [23, 24, 25, 26, 27]
        assert "lump_sum_number" in {row[1] for row in conn.execute("PRAGMA table_info(fee_payments)")}
    assert db.load_school_profile()["school_name"] == "Kept School"
    assert db.load_school_logo() == PNG_PIXEL
    assert db.load_report_settings() == {
        "ranking_style": "position", "head_signature": None, "class_teacher_signature": None,
        "term_closing_date": "", "next_term_begins": "",
    }
    backups = list(tmp_path.glob("school.pre-migration-v22-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT school_name FROM school_profile").fetchone()[0] == "Kept School"
