"""The vendor's licence-verification (public) key.

Leave ``PUBLIC_KEY`` empty while developing: the app then runs without asking for an activation key.
Before building the installer, run ``python tools/license_keygen.py init`` and paste the printed public key
here. The matching private key must stay with you and is never shipped (see docs/LICENSING.md).
"""

PUBLIC_KEY = ""  # base64 text of a 32-byte Ed25519 public key
