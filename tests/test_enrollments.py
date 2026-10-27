import sqlite3

import pytest

from smartschool import database as db


@pytest.fixture
def enrollment_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    db.create_user("admin", "test-hash", "admin")
    db.create_user("teacher", "test-hash", "teacher")
    student_id = db.add_row("Students", ["S-001", "Amina", "Okafor", "", "Legacy class text", ""], actor="admin")
    first_class = db.add_row("Classes", ["JSS 1", "A", ""], actor="admin")
    second_class = db.add_row("Classes", ["JSS 1", "B", ""], actor="admin")
    db.save_class_educational_stage(first_class, "secondary", "admin")
    db.save_class_educational_stage(second_class, "secondary", "admin")
    session_id = db.save_academic_session("2026/2027", "2026-09-01", "2027-07-31", True, "admin")
    return {"student": student_id, "class_a": first_class, "class_b": second_class, "session": session_id}


def test_enrollment_uses_explicit_class_and_session_and_preserves_legacy_text(enrollment_db):
    ids = enrollment_db
    enrollment_id = db.create_student_enrollment(ids["student"], ids["class_a"], ids["session"], "2026-09-10", "admin")
    rows = db.list_student_enrollments(ids["session"])
    assert len(rows) == 1
    assert rows[0]["id"] == enrollment_id
    assert rows[0]["class_name"] == "JSS 1"
    assert rows[0]["section"] == "A"
    assert rows[0]["status"] == "active"
    with db.connection() as conn:
        assert conn.execute("SELECT class_name FROM students WHERE id=?", (ids["student"],)).fetchone()[0] == "Legacy class text"


def test_guided_student_registration_generates_id_and_enrolls_selected_class(enrollment_db):
    ids = enrollment_db
    first = db.register_student(
        "Musa", "Bello", "Male", "555-0101", ids["class_a"], ids["session"], "2026-09-10", "admin",
        nin="NIN-TEST-001", date_of_birth="2012-04-05", email="musa@example.org",
    )
    second = db.register_student(
        "Sara", "Khan", "Female", "", ids["class_b"], ids["session"], "2026-09-11", "admin"
    )
    assert first["admission_no"] == "ADM-2026-00002"
    assert second["admission_no"] == "ADM-2026-00003"
    with db.connection() as conn:
        student = conn.execute(
            "SELECT admission_no,first_name,last_name,class_name,nin,date_of_birth,email FROM students WHERE id=?",
            (first["student_id"],),
        ).fetchone()
        placement = conn.execute(
            "SELECT class_id,academic_session_id,status FROM student_enrollments WHERE id=?",
            (first["enrollment_id"],),
        ).fetchone()
        actions = conn.execute(
            """SELECT action,entity FROM audit_log
               WHERE (entity='Students' AND entity_id=?)
                  OR (entity='student_enrollments' AND entity_id=?) ORDER BY id""",
            (str(first["student_id"]), str(first["enrollment_id"])),
        ).fetchall()
    assert tuple(student) == (first["admission_no"], "Musa", "Bello", "JSS 1 A", "NIN-TEST-001", "2012-04-05", "musa@example.org")
    assert tuple(placement) == (ids["class_a"], ids["session"], "active")
    assert [tuple(row) for row in actions] == [("CREATE", "Students"), ("ENROLL", "student_enrollments")]


def test_guided_student_registration_rejects_unauthorized_or_invalid_dates(enrollment_db):
    ids = enrollment_db
    with pytest.raises(PermissionError):
        db.register_student("Musa", "Bello", "", "", ids["class_a"], ids["session"], "2026-09-10", "teacher")
    with pytest.raises(ValueError, match="within the academic session"):
        db.register_student("Musa", "Bello", "", "", ids["class_a"], ids["session"], "2026-08-31", "admin")
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM student_enrollments").fetchone()[0] == 0


@pytest.mark.parametrize(("field", "value", "message"), [
    ("email", "not-an-email", "valid email"),
    ("date_of_birth", "2099-01-01", "future"),
])
def test_guided_student_registration_validates_optional_details(enrollment_db, field, value, message):
    ids = enrollment_db
    details = {field: value}
    with pytest.raises(ValueError, match=message):
        db.register_student(
            "Musa", "Bello", "", "", ids["class_a"], ids["session"], "2026-09-10", "admin", **details
        )
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 1


def test_bulk_student_registration_assigns_ids_and_enrollments_atomically(enrollment_db):
    ids = enrollment_db
    result = db.register_students_bulk(
        [
            {"first_name": "Musa", "last_name": "Bello", "gender": "Male", "guardian_phone": "555-0101", "nin": "NIN-002", "date_of_birth": "2010-01-02", "email": "musa@example.org"},
            {"first_name": "Sara", "last_name": "Khan", "gender": "", "guardian_phone": ""},
        ],
        ids["class_a"], ids["session"], "2026-09-10", "admin",
    )
    assert result == {
        "count": 2,
        "admission_numbers": ["ADM-2026-00002", "ADM-2026-00003"],
    }
    placements = db.list_student_enrollments(ids["session"])
    assert len(placements) == 2
    assert {row["class_id"] for row in placements} == {ids["class_a"]}


def test_bulk_student_registration_preserves_supplied_ids_and_generates_missing_ids(enrollment_db):
    ids = enrollment_db
    result = db.register_students_bulk(
        [
            {"admission_no": "RECOVERED-42", "first_name": "Musa", "last_name": "Bello"},
            {"first_name": "Sara", "last_name": "Khan"},
        ],
        ids["class_a"], ids["session"], "2026-09-10", "admin",
    )
    assert result["admission_numbers"] == ["RECOVERED-42", "ADM-2026-00002"]
    with pytest.raises(ValueError, match="already in use"):
        db.register_students_bulk(
            [{"admission_no": "RECOVERED-42", "first_name": "A", "last_name": "B"}],
            ids["class_a"], ids["session"], "2026-09-10", "admin",
        )


def test_student_id_prefix_setting_applies_to_guided_and_bulk_registration(enrollment_db):
    ids = enrollment_db
    db.save_school_profile({"admission_id_prefix": "SCH"}, "admin")
    guided = db.register_student(
        "Amina", "Okafor", "Female", "", ids["class_a"], ids["session"], "2026-09-10", "admin"
    )
    bulk = db.register_students_bulk(
        [{"first_name": "Musa", "last_name": "Bello"}],
        ids["class_a"], ids["session"], "2026-09-10", "admin",
    )
    assert guided["admission_no"] == "SCH-2026-00002"
    assert bulk["admission_numbers"] == ["SCH-2026-00003"]


def test_bulk_student_registration_rolls_back_whole_file_on_audit_failure(enrollment_db, monkeypatch):
    ids = enrollment_db
    original_audit = db._audit_in_transaction

    def fail_enrollment_audit(conn, actor, action, entity, entity_id):
        if entity == "student_enrollments":
            raise sqlite3.OperationalError("synthetic enrollment audit failure")
        return original_audit(conn, actor, action, entity, entity_id)

    monkeypatch.setattr(db, "_audit_in_transaction", fail_enrollment_audit)
    with pytest.raises(sqlite3.OperationalError, match="enrollment audit failure"):
        db.register_students_bulk(
            [
                {"first_name": "Musa", "last_name": "Bello"},
                {"first_name": "Sara", "last_name": "Khan"},
            ],
            ids["class_a"], ids["session"], "2026-09-10", "admin",
        )
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM student_enrollments").fetchone()[0] == 0


def test_guided_student_registration_rolls_back_student_when_enrollment_audit_fails(enrollment_db, monkeypatch):
    ids = enrollment_db
    original_audit = db._audit_in_transaction
    with db.connection() as conn:
        original_audit_count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]

    def fail_enrollment_audit(conn, username, action, entity, entity_id):
        if entity == "student_enrollments":
            raise sqlite3.IntegrityError("synthetic enrollment audit failure")
        return original_audit(conn, username, action, entity, entity_id)

    monkeypatch.setattr(db, "_audit_in_transaction", fail_enrollment_audit)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic enrollment audit failure"):
        db.register_student("Musa", "Bello", "", "", ids["class_a"], ids["session"], "2026-09-10", "admin")
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM student_enrollments").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == original_audit_count


def test_student_details_migration_preserves_existing_students(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    monkeypatch.setattr(db, "SCHEMA_VERSION", 21)
    db.initialize()
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO students(admission_no,first_name,last_name,gender,class_name,guardian_phone) "
            "VALUES('OLD-1','Amina','Okafor','','Class 1','555-0101')"
        )
    monkeypatch.setattr(db, "SCHEMA_VERSION", 22)
    db.initialize()
    with db.connection() as conn:
        student = conn.execute(
            "SELECT admission_no,first_name,last_name,guardian_phone,nin,date_of_birth,email FROM students"
        ).fetchone()
        assert tuple(student) == ("OLD-1", "Amina", "Okafor", "555-0101", "", "", "")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 22


def test_legacy_class_review_summarizes_values_without_changing_records(enrollment_db):
    ids = enrollment_db
    db.add_row("Students", ["S-002", "Musa", "Bello", "", "Legacy class text", ""], actor="admin")
    db.add_row("Students", ["S-003", "Sara", "Khan", "", "", ""], actor="admin")
    enrollment_id = db.create_student_enrollment(
        ids["student"], ids["class_a"], ids["session"], "2026-09-10", "admin"
    )
    with db.connection() as conn:
        students_before = [tuple(row) for row in conn.execute(
            "SELECT admission_no,class_name FROM students ORDER BY admission_no"
        )]
        enrollments_before = [tuple(row) for row in conn.execute(
            "SELECT id,student_id,class_id,status,enrolled_on,left_on "
            "FROM student_enrollments ORDER BY id"
        )]

    summary = db.list_legacy_class_summary()

    assert [(row["class_name"], row["student_count"]) for row in summary] == [
        ("", 1),
        ("Legacy class text", 2),
    ]
    assert enrollment_id == enrollments_before[0][0]
    with db.connection() as conn:
        assert [tuple(row) for row in conn.execute(
            "SELECT admission_no,class_name FROM students ORDER BY admission_no"
        )] == students_before
        assert [tuple(row) for row in conn.execute(
            "SELECT id,student_id,class_id,status,enrolled_on,left_on "
            "FROM student_enrollments ORDER BY id"
        )] == enrollments_before


def test_explicit_legacy_class_mapping_enrolls_exact_active_group(enrollment_db):
    ids = enrollment_db
    active_id = db.add_row("Students", ["S-002", "Musa", "Bello", "", "Legacy class text", ""], actor="admin")
    inactive_id = db.add_row("Students", ["S-003", "Sara", "Khan", "", "Legacy class text", ""], actor="admin")
    db.add_row("Students", ["S-004", "Zara", "Ali", "", "Different legacy", ""], actor="admin")
    with db.connection() as conn:
        conn.execute("UPDATE students SET is_active=0 WHERE id=?", (inactive_id,))
    preview = db.preview_legacy_class_mapping("Legacy class text", ids["session"])
    assert preview == {"total": 3, "active": 2, "inactive": 1, "already_enrolled": 0, "eligible": 2}

    created = db.map_legacy_class_to_enrollments(
        "Legacy class text", ids["class_b"], ids["session"], "2026-09-15", "admin"
    )

    assert created == 2
    rows = db.list_student_enrollments(ids["session"])
    assert len(rows) == 2
    assert {row["student_id"] for row in rows} == {ids["student"], active_id}
    assert all(row["class_id"] == ids["class_b"] for row in rows)
    assert all(row["enrolled_on"] == "2026-09-15" for row in rows)
    with db.connection() as conn:
        legacy_values = dict(conn.execute("SELECT admission_no,class_name FROM students"))
        event = conn.execute(
            "SELECT action,entity,entity_id FROM audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert legacy_values["S-001"] == "Legacy class text"
    assert legacy_values["S-002"] == "Legacy class text"
    assert legacy_values["S-003"] == "Legacy class text"
    assert legacy_values["S-004"] == "Different legacy"
    assert event["action"] == "MAP_LEGACY_CLASS (2 students)"
    assert event["entity"] == "legacy_student_class"
    assert event["entity_id"] == "Legacy class text"


def test_legacy_mapping_conflict_denies_whole_group_and_teacher_is_denied(enrollment_db):
    ids = enrollment_db
    second = db.add_row("Students", ["S-002", "Musa", "Bello", "", "Legacy class text", ""], actor="admin")
    db.create_student_enrollment(ids["student"], ids["class_a"], ids["session"], "2026-09-10", "admin")
    with pytest.raises(PermissionError):
        db.map_legacy_class_to_enrollments(
            "Legacy class text", ids["class_b"], ids["session"], "2026-09-15", "teacher"
        )
    with pytest.raises(ValueError, match="already have an enrollment"):
        db.map_legacy_class_to_enrollments(
            "Legacy class text", ids["class_b"], ids["session"], "2026-09-15", "admin"
        )
    rows = db.list_student_enrollments(ids["session"])
    assert len(rows) == 1 and rows[0]["student_id"] == ids["student"]
    assert second not in {row["student_id"] for row in rows}


def test_legacy_mapping_audit_failure_rolls_back_every_enrollment(enrollment_db, monkeypatch):
    ids = enrollment_db
    db.add_row("Students", ["S-002", "Musa", "Bello", "", "Legacy class text", ""], actor="admin")

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic audit failure")

    monkeypatch.setattr(db, "_audit_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="audit failure"):
        db.map_legacy_class_to_enrollments(
            "Legacy class text", ids["class_b"], ids["session"], "2026-09-15", "admin"
        )
    assert db.list_student_enrollments(ids["session"]) == []


def test_one_active_enrollment_per_student_and_close_is_terminal(enrollment_db):
    ids = enrollment_db
    enrollment_id = db.create_student_enrollment(ids["student"], ids["class_a"], ids["session"], "2026-09-10", "admin")
    with pytest.raises(ValueError, match="already has an active enrollment"):
        db.create_student_enrollment(ids["student"], ids["class_b"], ids["session"], "2026-09-02", "admin")
    with pytest.raises(PermissionError):
        db.close_student_enrollment(enrollment_id, "withdrawn", "2026-09-10", "teacher")
    with pytest.raises(ValueError, match="earlier"):
        db.close_student_enrollment(enrollment_id, "completed", "2026-09-09", "admin")

    db.close_student_enrollment(enrollment_id, "withdrawn", "2026-09-20", "admin")
    assert db.list_student_enrollments(ids["session"])[0]["status"] == "withdrawn"
    with pytest.raises(ValueError, match="Only an active enrollment"):
        db.close_student_enrollment(enrollment_id, "completed", "2026-09-21", "admin")


def test_transfer_closes_old_row_and_creates_new_active_row(enrollment_db):
    ids = enrollment_db
    old_id = db.create_student_enrollment(ids["student"], ids["class_a"], ids["session"], "2026-09-01", "admin")
    new_id = db.transfer_student_enrollment(old_id, ids["class_b"], "2026-10-01", "admin")
    rows = db.list_student_enrollments(ids["session"])
    by_id = {row["id"]: row for row in rows}
    assert by_id[old_id]["status"] == "transferred"
    assert by_id[old_id]["left_on"] == "2026-10-01"
    assert by_id[new_id]["status"] == "active"
    assert by_id[new_id]["class_id"] == ids["class_b"]
    assert by_id[new_id]["enrolled_on"] == "2026-10-01"
    with pytest.raises(ValueError, match="different destination"):
        db.transfer_student_enrollment(new_id, ids["class_b"], "2026-10-02", "admin")


def test_transfer_failure_rolls_back_close_and_new_enrollment(enrollment_db, monkeypatch):
    ids = enrollment_db
    old_id = db.create_student_enrollment(ids["student"], ids["class_a"], ids["session"], "2026-09-01", "admin")

    def fail_audit(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic audit failure")

    monkeypatch.setattr(db, "_audit_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="audit failure"):
        db.transfer_student_enrollment(old_id, ids["class_b"], "2026-10-01", "admin")
    rows = db.list_student_enrollments(ids["session"])
    assert len(rows) == 1
    assert rows[0]["id"] == old_id and rows[0]["status"] == "active" and rows[0]["left_on"] is None


def test_enrollment_rejects_bad_dates_and_non_admin(enrollment_db):
    ids = enrollment_db
    with pytest.raises(ValueError, match="valid date"):
        db.create_student_enrollment(ids["student"], ids["class_a"], ids["session"], "2026-02-30", "admin")
    with pytest.raises(ValueError, match="within the academic session"):
        db.create_student_enrollment(ids["student"], ids["class_a"], ids["session"], "2026-08-31", "admin")
    with pytest.raises(PermissionError):
        db.create_student_enrollment(ids["student"], ids["class_a"], ids["session"], "2026-09-01", "teacher")
    assert db.list_student_enrollments() == []


def test_failed_v3_migration_rolls_back_and_keeps_v2_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    conn = sqlite3.connect(db.DB_PATH)
    try:
        conn.execute("BEGIN IMMEDIATE")
        db._migration_1(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        db._migration_2(conn)
        conn.commit()
    finally:
        conn.close()

    def fail_after_ddl(conn):
        conn.execute("CREATE TABLE should_rollback(value TEXT)")
        raise RuntimeError("synthetic v3 failure")

    monkeypatch.setattr(db, "_migration_3", fail_after_ddl)
    with pytest.raises(RuntimeError, match="synthetic v3 failure"):
        db.initialize()
    with db.connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='student_enrollments'").fetchone() is None
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='should_rollback'").fetchone() is None
    backups = list(tmp_path.glob("school.pre-migration-v2-*.db"))
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        backup.close()


def _promotion_target(ids):
    return db.save_academic_session("2027/2028", "2027-09-01", "2028-07-31", False, "admin")


def test_promotion_preview_is_explicit_and_reports_ineligible_students(enrollment_db):
    ids = enrollment_db
    destination_session = _promotion_target(ids)
    inactive = db.add_row("Students", ["S-002", "Musa", "Bello", "", "", ""], actor="admin")
    already_enrolled = db.add_row("Students", ["S-003", "Sara", "Khan", "", "", ""], actor="admin")
    for student_id in (ids["student"], inactive, already_enrolled):
        db.create_student_enrollment(student_id, ids["class_a"], ids["session"], "2026-09-10", "admin")
    with db.connection() as conn:
        conn.execute("UPDATE students SET is_active=0 WHERE id=?", (inactive,))
    db.create_student_enrollment(already_enrolled, ids["class_b"], destination_session, "2027-09-01", "admin")
    before = len(db.list_student_enrollments())

    preview = db.preview_student_promotion(
        ids["session"], ids["class_a"], destination_session, ids["class_b"], "2027-09-03", "admin"
    )

    assert preview["eligible_count"] == 1 and preview["skipped_count"] == 2
    assert [row["student_id"] for row in preview["students"] if row["eligible"]] == [ids["student"]]
    assert "inactive" in next(row["reason"] for row in preview["students"] if row["student_id"] == inactive)
    assert "Already has" in next(row["reason"] for row in preview["students"] if row["student_id"] == already_enrolled)
    assert len(db.list_student_enrollments()) == before


def test_promotion_creates_destination_rows_and_retains_source_records(enrollment_db):
    ids = enrollment_db
    destination_session = _promotion_target(ids)
    source_id = db.create_student_enrollment(
        ids["student"], ids["class_a"], ids["session"], "2026-09-10", "admin"
    )

    promoted = db.promote_class_students(
        ids["session"], ids["class_a"], destination_session, ids["class_b"],
        "2027-09-03", [ids["student"]], "admin",
    )

    assert promoted == 1
    rows = db.list_student_enrollments()
    by_id = {row["id"]: row for row in rows}
    assert by_id[source_id]["status"] == "active"
    destination = next(row for row in rows if row["academic_session_id"] == destination_session)
    assert destination["class_id"] == ids["class_b"]
    assert destination["enrolled_on"] == "2027-09-03" and destination["status"] == "active"
    with db.connection() as conn:
        actions = [tuple(row) for row in conn.execute(
            "SELECT action,entity FROM audit_log WHERE entity='student_enrollments' ORDER BY id DESC LIMIT 2"
        )]
    assert actions == [("PROMOTE_CLASS (1 students)", "student_enrollments"), ("PROMOTE_IN", "student_enrollments")]


def test_promotion_rejects_ineligible_or_unauthorized_selection(enrollment_db):
    ids = enrollment_db
    destination_session = _promotion_target(ids)
    db.create_student_enrollment(ids["student"], ids["class_a"], ids["session"], "2026-09-10", "admin")
    with pytest.raises(PermissionError):
        db.promote_class_students(
            ids["session"], ids["class_a"], destination_session, ids["class_b"],
            "2027-09-03", [ids["student"]], "teacher",
        )
    with pytest.raises(ValueError, match="different destination"):
        db.preview_student_promotion(
            ids["session"], ids["class_a"], ids["session"], ids["class_b"], "2027-09-03", "admin"
        )
    with pytest.raises(ValueError, match="within the academic session"):
        db.preview_student_promotion(
            ids["session"], ids["class_a"], destination_session, ids["class_b"], "2027-08-31", "admin"
        )
    with pytest.raises(PermissionError):
        db.preview_student_promotion(
            ids["session"], ids["class_a"], destination_session, ids["class_b"], "2027-09-03", "teacher"
        )
    with pytest.raises(ValueError, match="ineligible"):
        db.promote_class_students(
            ids["session"], ids["class_a"], destination_session, ids["class_b"],
            "2027-09-03", [99999], "admin",
        )
    assert len(db.list_student_enrollments()) == 1


def test_promotion_audit_failure_rolls_back_all_destination_enrollments(enrollment_db, monkeypatch):
    ids = enrollment_db
    destination_session = _promotion_target(ids)
    second_student = db.add_row("Students", ["S-002", "Musa", "Bello", "", "", ""], actor="admin")
    for student_id in (ids["student"], second_student):
        db.create_student_enrollment(student_id, ids["class_a"], ids["session"], "2026-09-10", "admin")
    original_audit = db._audit_in_transaction
    calls = 0

    def fail_second_promotion_audit(conn, actor, action, entity, entity_id):
        nonlocal calls
        if action == "PROMOTE_IN":
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError("synthetic promotion audit failure")
        return original_audit(conn, actor, action, entity, entity_id)

    monkeypatch.setattr(db, "_audit_in_transaction", fail_second_promotion_audit)
    with pytest.raises(sqlite3.OperationalError, match="promotion audit failure"):
        db.promote_class_students(
            ids["session"], ids["class_a"], destination_session, ids["class_b"],
            "2027-09-03", [ids["student"], second_student], "admin",
        )
    assert db.list_student_enrollments(destination_session) == []
    assert len(db.list_student_enrollments(ids["session"])) == 2
