"""Communication credits: an append-only ledger topped up by redeem codes (vouchers), spent per message.

Offline now, online later: ``redeem_voucher`` only depends on a *verifier* (``verify(code, license_id) -> int``).
The bundled ``HmacVoucherVerifier`` checks codes locally; an online service can provide another verifier that
asks a server instead, with no change to the ledger or the screens.

Voucher format (compatible with the old application): ``SSM-XXXX-XXXX-XXXX-XXXX`` = Base32 of
version(1 byte) + credits(uint16) + licence hash(4 bytes) + HMAC-SHA256 tag(3 bytes).

The signing secret is NOT embedded in this program. It is read from the ``SMARTSCHOOL_VOUCHER_SECRET``
environment variable or from a ``voucher_secret.key`` file in the data folder, so only people who hold the
secret can issue vouchers.
"""

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import struct

from . import database as db

# Credits charged per email the app sends, by kind (same table as the old application; plain email is free).
COSTS = {"birthday": 1, "fee_reminder": 1, "welcome": 0, "notice": 0, "report_card": 0}
CHANNEL_COSTS = {"email": 0, "whatsapp": 0}
MAX_VOUCHER_CREDITS = 65535
_PREFIX = "SSM"
_VERSION = 1


def message_cost(kind: str, channel: str) -> int:
    return int(COSTS.get(kind, 0)) + int(CHANNEL_COSTS.get(channel, 0))


def _license_hash(license_id: str) -> bytes:
    return hashlib.sha256(license_id.strip().upper().encode("utf-8")).digest()[:4]


def _normalise(code: str) -> str:
    text = re.sub(r"[^A-Z2-7]", "", (code or "").upper())
    if text.startswith(_PREFIX):
        text = text[len(_PREFIX):]
    if len(text) != 16:
        raise ValueError("That is not a valid redeem code. Check it and try again.")
    return text


def format_code(raw: str) -> str:
    return f"{_PREFIX}-" + "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def issue_voucher(secret: bytes, license_id: str, credits: int) -> str:
    """Create a redeem code. Used by whoever holds the secret (and by tests)."""
    if not secret:
        raise ValueError("A signing secret is required.")
    if isinstance(credits, bool) or not isinstance(credits, int) or not 1 <= credits <= MAX_VOUCHER_CREDITS:
        raise ValueError(f"Credits must be a whole number from 1 to {MAX_VOUCHER_CREDITS}.")
    payload = struct.pack(">BH", _VERSION, credits) + _license_hash(license_id)
    tag = hmac.new(secret, payload, hashlib.sha256).digest()[:3]
    return format_code(base64.b32encode(payload + tag).decode("ascii"))


class HmacVoucherVerifier:
    """Offline verifier: the code must be signed with the secret and issued for this school's licence id."""

    def __init__(self, secret: bytes | None = None):
        self._secret = secret

    def _load_secret(self) -> bytes:
        if self._secret:
            return self._secret
        value = os.environ.get("SMARTSCHOOL_VOUCHER_SECRET", "").strip()
        if not value:
            path = db.APP_DIR / "voucher_secret.key"
            if path.is_file():
                value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(
                "Redeem codes cannot be checked on this computer yet: no voucher secret is installed. "
                "Ask your Smart School provider."
            )
        return value.encode("utf-8")

    def verify(self, code: str, license_id: str) -> int:
        raw = _normalise(code)
        try:
            data = base64.b32decode(raw)
        except Exception:
            raise ValueError("That is not a valid redeem code. Check it and try again.") from None
        payload, tag = data[:7], data[7:]
        expected = hmac.new(self._load_secret(), payload, hashlib.sha256).digest()[:3]
        version, credits = struct.unpack(">BH", payload[:3])
        if version != _VERSION or not hmac.compare_digest(tag, expected):
            raise ValueError("That redeem code is not valid.")
        if not hmac.compare_digest(payload[3:], _license_hash(license_id)):
            raise ValueError("That redeem code was issued for a different school.")
        if credits <= 0:
            raise ValueError("That redeem code is not valid.")
        return credits


def _admin(conn, actor):
    return db._require_admin(conn, actor)


def _license_in_transaction(conn, actor: str) -> str:
    row = conn.execute("SELECT value FROM communication_settings WHERE key='license_id'").fetchone()
    if row:
        return row["value"]
    value = "SCH-" + secrets.token_hex(4).upper()
    conn.execute(
        "INSERT INTO communication_settings(key,value,updated_by) VALUES('license_id',?,?)", (value, actor)
    )
    return value


def get_license_id(actor: str) -> str:
    """The school's licence id (created once). Voucher codes are issued against it."""
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        return _license_in_transaction(conn, actor)


def balance_in_transaction(conn) -> int:
    return int(conn.execute("SELECT COALESCE(SUM(amount),0) FROM communication_credits").fetchone()[0])


def get_balance(actor: str) -> int:
    with db.connection() as conn:
        _admin(conn, actor)
        return balance_in_transaction(conn)


def list_ledger(actor: str, limit: int = 200) -> list[dict]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("Limit must be between 1 and 1000.")
    with db.connection() as conn:
        _admin(conn, actor)
        rows = conn.execute(
            "SELECT id,entry_type,amount,description,reference,created_by,created_at "
            "FROM communication_credits ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def redeem_voucher(code: str, actor: str, verifier=None) -> dict:
    """Verify a redeem code and add its credits. A code can only be used once."""
    verifier = verifier or HmacVoucherVerifier()
    with db.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        actor = _admin(conn, actor)
        license_id = _license_in_transaction(conn, actor)
        credits = verifier.verify(code, license_id)
        reference = _normalise(code)
        try:
            conn.execute(
                "INSERT INTO communication_credits(entry_type,amount,description,reference,created_by) "
                "VALUES('topup',?,?,?,?)",
                (credits, "Redeem code", reference, actor),
            )
        except sqlite3.IntegrityError:
            raise ValueError("That redeem code has already been used.") from None
        db._audit_in_transaction(conn, actor, "REDEEM", "communication_credits", reference[:4] + "…")
        return {"added": credits, "balance": balance_in_transaction(conn)}


def deduct_in_transaction(conn, amount: int, description: str, reference: str, actor: str) -> None:
    """Spend credits inside the caller's write transaction; refuses to overdraw."""
    if amount <= 0:
        return
    if balance_in_transaction(conn) < amount:
        raise ValueError("Not enough communication credits.")
    conn.execute(
        "INSERT INTO communication_credits(entry_type,amount,description,reference,created_by) "
        "VALUES('deduction',?,?,?,?)",
        (-amount, description[:200], reference[:60], actor),
    )
