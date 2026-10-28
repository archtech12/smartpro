"""Activation keys: signing, machine binding, expiry, storage (synthetic keys only)."""

import base64
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from smartschool import database as db
from smartschool import license_public_key, licensing

TODAY = date(2026, 10, 27)
MACHINE = bytes.fromhex("1a2b3c4d5e6f7a8b")


@pytest.fixture()
def keys():
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    return private, public


def _key(private, expires=None, machine=MACHINE, edition=1):
    payload = licensing._build_payload(edition, expires, machine)
    return licensing.encode_key(payload, private.sign(payload))


def test_valid_key_is_active_and_formatted(keys):
    private, public = keys
    key = _key(private)
    assert key.startswith("SSA1-")
    result = licensing.check_key(key, today=TODAY, machine_hash=MACHINE, public_key_b64=public)
    assert result["state"] == "active" and result["edition"] == "Standard" and result["expires"] is None
    # whitespace/lower case from copy-paste is tolerated
    assert licensing.check_key(" " + key.lower().replace("-", " ") + "\n", today=TODAY, machine_hash=MACHINE,
                               public_key_b64=public)["state"] == "active"


def test_key_is_bound_to_machine_expiry_and_signer(keys):
    private, public = keys
    other = Ed25519PrivateKey.generate()
    key = _key(private, expires=date(2026, 12, 31))
    assert licensing.check_key(key, today=TODAY, machine_hash=bytes(8), public_key_b64=public)["state"] == "wrong_machine"
    assert licensing.check_key(key, today=date(2027, 1, 1), machine_hash=MACHINE, public_key_b64=public)["state"] == "expired"
    assert licensing.check_key(key, today=date(2026, 12, 31), machine_hash=MACHINE, public_key_b64=public)["state"] == "active"
    forged = _key(other)
    assert licensing.check_key(forged, today=TODAY, machine_hash=MACHINE, public_key_b64=public)["state"] == "invalid"


def test_tampering_and_garbage_are_rejected(keys):
    private, public = keys
    key = _key(private)
    chars = list(key)
    index = len(chars) - 3
    chars[index] = "A" if chars[index] != "A" else "B"
    for bad in ("".join(chars), key[:-10], "", "hello", "SSA1-", "XXXX-" + key[5:]):
        assert licensing.check_key(bad, today=TODAY, machine_hash=MACHINE, public_key_b64=public)["state"] == "invalid"
    assert licensing.check_key(key, today=TODAY, machine_hash=MACHINE, public_key_b64="")["state"] == "invalid"


def test_machine_code_round_trip():
    code = licensing.format_machine_code(MACHINE)
    assert code == "1A2B-3C4D-5E6F-7A8B" and licensing.parse_machine_code(code.lower()) == MACHINE
    with pytest.raises(ValueError):
        licensing.parse_machine_code("1234")
    assert licensing.machine_code() == licensing.machine_code()


def test_status_activation_and_persistence(tmp_path, monkeypatch, keys):
    private, public = keys
    monkeypatch.setattr(db, "APP_DIR", tmp_path)
    assert licensing.status()["state"] == "not_required"          # no public key built in: development mode
    monkeypatch.setattr(license_public_key, "PUBLIC_KEY", public)
    monkeypatch.setattr(licensing, "_machine_hash", lambda raw=None: MACHINE)
    assert licensing.status(TODAY)["state"] == "unlicensed"
    with pytest.raises(ValueError, match="not genuine|not a valid|incomplete"):
        licensing.activate("SSA1-AAAAA")
    with pytest.raises(ValueError, match="different computer"):
        licensing.activate(_key(private, machine=bytes(8)), today=TODAY)
    assert not (tmp_path / "activation.key").exists()
    result = licensing.activate(_key(private, expires=date(2027, 6, 30)), today=TODAY)
    assert result["state"] == "active"
    state = licensing.status(TODAY)
    assert state["state"] == "active" and licensing.is_usable(state) and licensing.days_left(state, TODAY) == 246
    assert licensing.status(date(2027, 7, 1))["state"] == "expired"
    assert not licensing.is_usable(licensing.status(date(2027, 7, 1)))


def test_keygen_tool_issues_a_key_the_app_accepts(tmp_path):
    tool = Path(__file__).resolve().parents[1] / "tools" / "license_keygen.py"
    pem = tmp_path / "private.pem"
    out = subprocess.run([sys.executable, str(tool), "init", "--private-key", str(pem)], capture_output=True, text=True, check=True)
    public = out.stdout.strip().splitlines()[-1]
    assert subprocess.run([sys.executable, str(tool), "init", "--private-key", str(pem)]).returncode != 0  # no overwrite
    key = subprocess.run([sys.executable, str(tool), "issue", "--private-key", str(pem), "--machine", "1A2B-3C4D-5E6F-7A8B",
                          "--expires", "2030-01-31", "--edition", "professional"],
                         capture_output=True, text=True, check=True).stdout.strip()
    result = licensing.check_key(key, today=TODAY, machine_hash=MACHINE, public_key_b64=public)
    assert result["state"] == "active" and result["edition"] == "Professional" and result["expires"] == date(2030, 1, 31)
