"""Own-password change, audit log viewer and printable term dates (synthetic data)."""

import pytest

from smartschool import account_security as acct
from smartschool import database as db
from smartschool.security import hash_password, verify_password


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "school.db")
    db.initialize()
    db.create_user("admin", hash_password("current-password-1"))
    db.create_user("teacher", hash_password("teacher-password-1"), "teacher")


def _hash_of(username):
    with db.connection() as conn:
        return conn.execute("SELECT password_hash FROM users WHERE username=?", (username,)).fetchone()[0]


def test_change_own_password_requires_current_password_and_policy(accounts):
    with pytest.raises(ValueError, match="current password"):
        acct.change_own_password("admin", "wrong", "brand-new-pass-1", "brand-new-pass-1")
    with pytest.raises(ValueError, match="do not match"):
        acct.change_own_password("admin", "current-password-1", "brand-new-pass-1", "other")
    with pytest.raises(ValueError, match="at least 10"):
        acct.change_own_password("admin", "current-password-1", "short", "short")
    with pytest.raises(ValueError, match="different"):
        acct.change_own_password("admin", "current-password-1", "current-password-1", "current-password-1")
    before = _hash_of("admin")
    acct.change_own_password("admin", "current-password-1", "brand-new-pass-1", "brand-new-pass-1")
    assert _hash_of("admin") != before and verify_password("brand-new-pass-1", _hash_of("admin"))
    assert verify_password("teacher-password-1", _hash_of("teacher"))  # other accounts untouched
    # teachers can change their own password too, and the event is audited without the secret
    acct.change_own_password("teacher", "teacher-password-1", "teacher-new-pass-9", "teacher-new-pass-9")
    log = db.load_audit_log("admin", action="CHANGE_PASSWORD")
    assert log["total"] == 2 and all("pass" not in str(row).lower().replace("change_password", "") for row in log["rows"])


def test_inactive_or_unknown_account_cannot_change_password(accounts):
    db.set_user_active("teacher", False, "admin")
    with pytest.raises(ValueError, match="current password"):
        acct.change_own_password("teacher", "teacher-password-1", "teacher-new-pass-9", "teacher-new-pass-9")
    with pytest.raises(ValueError, match="current password"):
        acct.change_own_password("nobody", "x", "brand-new-pass-1", "brand-new-pass-1")


def test_audit_log_is_admin_only_filterable_and_wildcard_safe(accounts):
    with pytest.raises(PermissionError):
        db.load_audit_log("teacher")
    db.set_user_active("teacher", True, "admin")
    db.save_school_profile({"school_name": "Audit School"}, "admin")
    everything = db.load_audit_log("admin")
    assert everything["total"] >= 2 and "UPDATE" in everything["actions"]
    assert everything["rows"][0]["id"] > everything["rows"][-1]["id"]            # newest first
    assert {r["action"] for r in db.load_audit_log("admin", action="UPDATE")["rows"]} == {"UPDATE"}
    assert db.load_audit_log("admin", search="school_profile")["total"] >= 1
    assert db.load_audit_log("admin", search="%")["total"] == 0                   # '%' is literal, not a wildcard
    assert db.load_audit_log("admin", starts_on="2999-01-01")["total"] == 0
    assert db.load_audit_log("admin", limit=1)["rows"].__len__() == 1
    with pytest.raises(ValueError):
        db.load_audit_log("admin", starts_on="not-a-date")


def test_term_dates_save_validate_clear_and_stay_unchanged_when_omitted(accounts):
    assert db.load_report_settings()["term_closing_date"] == ""
    db.save_school_profile({}, "admin", term_closing_date="2026-12-18", next_term_begins="2027-01-11")
    settings = db.load_report_settings()
    assert (settings["term_closing_date"], settings["next_term_begins"]) == ("2026-12-18", "2027-01-11")
    db.save_school_profile({"school_name": "Other"}, "admin")                     # omitted -> unchanged
    assert db.load_report_settings()["next_term_begins"] == "2027-01-11"
    db.save_school_profile({}, "admin", next_term_begins="")                      # empty clears
    assert db.load_report_settings()["next_term_begins"] == "" and db.load_report_settings()["term_closing_date"] == "2026-12-18"
    with pytest.raises(ValueError, match="Term closing date"):
        db.save_school_profile({}, "admin", term_closing_date="18/12/2026")
