"""Vendor-only activation key generator. NEVER ship this file or the private key to customers.

  python tools/license_keygen.py init  --private-key keys/private.pem
  python tools/license_keygen.py issue --private-key keys/private.pem --machine 1A2B-3C4D-5E6F-7A8B \
         [--expires 2027-12-31] [--edition standard|professional]

``init`` creates the signing key pair once and prints the public key to paste into
``smartschool/license_public_key.py``. ``issue`` prints an activation key for one customer computer.
"""

import argparse
import base64
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from smartschool import licensing  # noqa: E402


def public_key_b64(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def init(path: Path) -> str:
    if path.exists():
        raise SystemExit(f"{path} already exists. Refusing to overwrite your private key.")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return public_key_b64(key)


def issue(private_pem: bytes, machine: str, expires: date | None, edition: str) -> str:
    key = serialization.load_pem_private_key(private_pem, password=None)
    edition_id = {"standard": 1, "professional": 2}[edition]
    payload = licensing._build_payload(edition_id, expires, licensing.parse_machine_code(machine))
    return licensing.encode_key(payload, key.sign(payload))


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_init = sub.add_parser("init")
    p_init.add_argument("--private-key", required=True, type=Path)
    p_issue = sub.add_parser("issue")
    p_issue.add_argument("--private-key", required=True, type=Path)
    p_issue.add_argument("--machine", required=True)
    p_issue.add_argument("--expires", type=date.fromisoformat, default=None, help="YYYY-MM-DD; omit for no expiry")
    p_issue.add_argument("--edition", choices=("standard", "professional"), default="standard")
    args = parser.parse_args(argv)
    if args.command == "init":
        print("Private key saved to:", args.private_key, "(keep it secret and back it up)")
        print("Public key (paste into smartschool/license_public_key.py):")
        print(init(args.private_key))
    else:
        print(issue(args.private_key.read_bytes(), args.machine, args.expires, args.edition))


if __name__ == "__main__":
    main()
