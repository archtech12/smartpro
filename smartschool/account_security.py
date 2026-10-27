"""Self-service account security (change your own password)."""

from . import database as db
from .security import hash_password, verify_password

MIN_PASSWORD_LENGTH = 10


def change_own_password(username: str, current_password: str, new_password: str, confirm: str) -> None:
    """Verify the current password, then store a new hash for the same, active account.

    Works for every role; it never touches another user's account.
    """
    if new_password != confirm:
        raise ValueError("The new passwords do not match.")
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Use a new password of at least {MIN_PASSWORD_LENGTH} characters.")
    if new_password == current_password:
        raise ValueError("Choose a new password that is different from the current one.")
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute(
            "SELECT id,password_hash,is_active FROM users WHERE username=? COLLATE NOCASE", ((username or "").strip(),)
        ).fetchone()
        if user is None or not user["is_active"] or not verify_password(current_password or "", user["password_hash"]):
            raise ValueError("The current password is not correct.")
        conn.execute(
            "UPDATE users SET password_hash=?,failed_login_count=0,locked_until=NULL WHERE id=?",
            (hash_password(new_password), int(user["id"])),
        )
        db._audit_in_transaction(conn, username.strip(), "CHANGE_PASSWORD", "users", int(user["id"]))
