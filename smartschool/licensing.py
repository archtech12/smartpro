"""Activation keys: signed, tied to one computer, verified offline (Qt-free).

An activation key is an Ed25519 signature over ``version | edition | expiry | machine hash``. The program
only contains the *public* key, so nobody can create keys from the installed files; the vendor signs keys
with a private key that is never shipped. A key works on one computer only (its machine code is inside the
signed data) and may carry an expiry date.

Key text: ``SSA1-`` followed by Base32 in groups of 5, for example ``SSA1-ABCDE-FGHIJ-...``.
"""

import base64
import hashlib
import os
import re
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

from . import database as db
from . import license_public_key

PREFIX = "SSA1"
VERSION = 1
EPOCH = date(2020, 1, 1)
EDITIONS = {1: "Standard", 2: "Professional"}
_PAYLOAD_LEN = 1 + 1 + 2 + 8  # version, edition, expiry days (0 = never), machine hash
_SIGNATURE_LEN = 64


def public_key_text() -> str:
    return (license_public_key.PUBLIC_KEY or "").strip()


def is_enforced() -> bool:
    """Activation is required only when a public key has been built into this copy of the program."""
    return bool(public_key_text())


def _raw_machine_guid() -> str:
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                value = winreg.QueryValueEx(key, "MachineGuid")[0]
                if value:
                    return str(value)
        except OSError:
            pass
    else:
        for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                value = Path(path).read_text(encoding="ascii").strip()
                if value:
                    return value
            except OSError:
                continue
    return f"node-{uuid.getnode()}"  # last resort


def _machine_hash(raw: str | None = None) -> bytes:
    return hashlib.sha256(("SmartSchool|" + (raw if raw is not None else _raw_machine_guid())).encode("utf-8")).digest()[:8]


def format_machine_code(machine_hash: bytes) -> str:
    text = machine_hash.hex().upper()
    return "-".join(text[i:i + 4] for i in range(0, 16, 4))


def machine_code() -> str:
    """The code a customer sends to the vendor to receive a key for this computer."""
    return format_machine_code(_machine_hash())


def parse_machine_code(text: str) -> bytes:
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", text or "")
    if len(cleaned) != 16:
        raise ValueError("A machine code has 16 letters and digits, for example 1A2B-3C4D-5E6F-7A8B.")
    return bytes.fromhex(cleaned)


def _build_payload(edition: int, expires: date | None, machine_hash: bytes) -> bytes:
    if edition not in EDITIONS:
        raise ValueError("Unknown edition.")
    days = 0
    if expires is not None:
        days = (expires - EPOCH).days
        if not 1 <= days <= 65535:
            raise ValueError("Expiry date is out of range.")
    return bytes([VERSION, edition]) + days.to_bytes(2, "big") + machine_hash


def encode_key(payload: bytes, signature: bytes) -> str:
    text = base64.b32encode(payload + signature).decode("ascii").rstrip("=")
    return PREFIX + "-" + "-".join(text[i:i + 5] for i in range(0, len(text), 5))


def decode_key(key: str) -> tuple[bytes, bytes]:
    text = re.sub(r"[\s]", "", (key or "").upper())
    if not text.startswith(PREFIX):
        raise ValueError("That is not a valid activation key.")
    body = re.sub(r"[^A-Z2-7]", "", text[len(PREFIX):])
    body += "=" * (-len(body) % 8)
    try:
        data = base64.b32decode(body)
    except Exception:
        raise ValueError("That is not a valid activation key.") from None
    if len(data) != _PAYLOAD_LEN + _SIGNATURE_LEN:
        raise ValueError("That activation key is incomplete or has extra characters. Paste the whole key.")
    return data[:_PAYLOAD_LEN], data[_PAYLOAD_LEN:]


def _verify_signature(payload: bytes, signature: bytes, public_key_b64: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64)).verify(signature, payload)
        return True
    except (InvalidSignature, ValueError):
        return False


def check_key(key: str, *, today: date | None = None, machine_hash: bytes | None = None,
              public_key_b64: str | None = None) -> dict:
    """Validate a key. Returns ``{"state": ..., "message": ..., "edition": ..., "expires": date|None}``.

    States: ``active``, ``expired``, ``wrong_machine``, ``invalid``.
    """
    today = today or date.today()
    machine_hash = machine_hash if machine_hash is not None else _machine_hash()
    public_key_b64 = public_key_b64 if public_key_b64 is not None else public_key_text()
    try:
        payload, signature = decode_key(key)
    except ValueError as exc:
        return {"state": "invalid", "message": str(exc), "edition": "", "expires": None}
    if not public_key_b64 or not _verify_signature(payload, signature, public_key_b64):
        return {"state": "invalid", "message": "That activation key is not genuine.", "edition": "", "expires": None}
    if payload[0] != VERSION or payload[1] not in EDITIONS:
        return {"state": "invalid", "message": "That key is for a different version of the program.",
                "edition": "", "expires": None}
    edition = EDITIONS[payload[1]]
    days = int.from_bytes(payload[2:4], "big")
    expires = EPOCH + timedelta(days=days) if days else None
    if payload[4:] != machine_hash:
        return {"state": "wrong_machine", "message": "That key was issued for a different computer.",
                "edition": edition, "expires": expires}
    if expires is not None and today > expires:
        return {"state": "expired", "message": f"This licence expired on {expires:%d %B %Y}.",
                "edition": edition, "expires": expires}
    message = "Activated." if expires is None else f"Activated until {expires:%d %B %Y}."
    return {"state": "active", "message": message, "edition": edition, "expires": expires}


def _license_path() -> Path:
    return db.APP_DIR / "activation.key"


def saved_key() -> str:
    try:
        return _license_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def status(today: date | None = None) -> dict:
    """Current licence state for this computer. ``state`` is ``not_required`` when no public key is built in."""
    if not is_enforced():
        return {"state": "not_required", "message": "Activation is not required for this build.",
                "edition": "", "expires": None}
    key = saved_key()
    if not key:
        return {"state": "unlicensed", "message": "Enter your activation key to start using the program.",
                "edition": "", "expires": None}
    return check_key(key, today=today)


def is_usable(state: dict) -> bool:
    return state["state"] in ("not_required", "active")


def days_left(state: dict, today: date | None = None) -> int | None:
    if state.get("expires") is None:
        return None
    return (state["expires"] - (today or date.today())).days


def activate(key: str, today: date | None = None) -> dict:
    """Check a key and, if it is good for this computer, save it. Raises ValueError with a friendly reason."""
    result = check_key(key, today=today)
    if result["state"] != "active":
        raise ValueError(result["message"])
    db.APP_DIR.mkdir(parents=True, exist_ok=True)
    path = _license_path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(re.sub(r"\s", "", key.upper()) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return result
