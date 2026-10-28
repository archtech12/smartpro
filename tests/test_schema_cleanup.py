import sqlite3

from smartschool import database as db


def test_fresh_schema_does_not_install_question_bank(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='question_bank_items'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name LIKE 'cbt_%'"
        ).fetchone() is None


def test_v28_upgrade_drops_deferred_cbt_tables_and_adds_student_prefix(tmp_path, monkeypatch):
    database_path = tmp_path / "school.db"
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", database_path)
    db.initialize()
    with db.connection() as conn:
        conn.execute("ALTER TABLE school_profile DROP COLUMN admission_id_prefix")
        conn.execute("DELETE FROM schema_migrations WHERE version=29")
        conn.execute("CREATE TABLE cbt_questions(id INTEGER PRIMARY KEY, prompt TEXT)")
        conn.execute("INSERT INTO cbt_questions(prompt) VALUES('synthetic legacy record')")
        conn.execute("PRAGMA user_version=28")

    db.initialize()

    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 29
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name LIKE 'cbt_%'"
        ).fetchone() is None
    assert db.load_school_profile()["admission_id_prefix"] == "ADM"


def test_v20_upgrade_removes_question_bank_and_adds_optional_student_details(tmp_path, monkeypatch):
    database_path = tmp_path / "school.db"
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", database_path)
    with sqlite3.connect(database_path) as conn:
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)")
        conn.executemany(
            "INSERT INTO schema_migrations(version) VALUES(?)",
            [(version,) for version in range(1, 21)],
        )
        conn.execute(
            "CREATE TABLE students(id INTEGER PRIMARY KEY, admission_no TEXT NOT NULL, "
            "first_name TEXT NOT NULL, last_name TEXT NOT NULL, gender TEXT NOT NULL DEFAULT '', "
            "class_name TEXT NOT NULL DEFAULT '', guardian_phone TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(
            "INSERT INTO students(admission_no,first_name,last_name) VALUES('OLD-1','Amina','Okafor')"
        )
        conn.execute(
            "CREATE TABLE question_bank_items(id INTEGER PRIMARY KEY, prompt TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO question_bank_items(id,prompt) VALUES(1,'synthetic old record')"
        )
        # Minimal stand-ins for tables that exist in a real v20 database and are
        # extended by later additive migrations.
        conn.execute("CREATE TABLE school_profile(id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE fee_payments(id INTEGER PRIMARY KEY)")
        conn.execute("PRAGMA user_version=20")

    db.initialize()

    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='question_bank_items'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version DESC LIMIT 1"
        ).fetchone()[0] == db.SCHEMA_VERSION
        student = conn.execute(
            "SELECT admission_no,first_name,last_name,nin,date_of_birth,email FROM students"
        ).fetchone()
        assert tuple(student) == ("OLD-1", "Amina", "Okafor", "", "", "")

    backups = list(tmp_path.glob("school.pre-migration-v20-*.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute(
            "SELECT prompt FROM question_bank_items WHERE id=1"
        ).fetchone()[0] == "synthetic old record"


def test_main_window_page_content_has_small_screen_scroll_area(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication, QScrollArea
    from smartschool.ui import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow({"username": "teacher", "role": "teacher"})
    page_scroll = window.findChild(QScrollArea, "pageScroll")
    assert page_scroll is not None
    assert page_scroll.widgetResizable()
    assert page_scroll.widget() is window.stack
    window.close()
    app.processEvents()
