import hashlib
import hmac
import os

_ITERATIONS = 310_000
_MIN_ITERATIONS = 100_000
_MAX_ITERATIONS = 2_000_000

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${digest.hex()}"

def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt_hex, digest_hex = encoded.split("$", 3)
        rounds = int(rounds)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        if scheme != "pbkdf2_sha256" or not _MIN_ITERATIONS <= rounds <= _MAX_ITERATIONS:
            return False
        if len(salt) != 16 or len(expected) != 32:
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, rounds
        )
        return hmac.compare_digest(candidate, expected)
    except (ValueError, TypeError):
        return False
