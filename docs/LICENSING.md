# Activation keys

When a public key is built into the program, SmartSchool asks for an activation key on first start. Without one
(the default while developing) the program starts normally.

How it works: a key is an Ed25519 signature over the edition, an optional expiry date and the customer's
**machine code**. The installed program only holds the *public* key, so keys cannot be forged from the installed
files, and a key works on one computer only. Verification is fully offline. Improvements over the old
`license_manager.py`: no secret salt inside the program, no 30-hour key window, no `wmic` call (it is gone on new
Windows), and the key is stored in the data folder (`activation.key`), not the working directory.

## One-time setup (vendor)
1. `pip install cryptography`
2. `python tools/license_keygen.py init --private-key D:\secure\smartschool_private.pem`
3. Paste the printed public key into `smartschool/license_public_key.py` (`PUBLIC_KEY = "..."`).
4. Build the installer. **Back up the private key and never put it in the repository or the installer.**
   If it is lost you can no longer issue keys for builds that contain the matching public key.

## For each customer
1. The customer installs, starts the program and sends you the machine code shown on the activation window.
2. `python tools/license_keygen.py issue --private-key D:\secure\smartschool_private.pem --machine 1A2B-3C4D-5E6F-7A8B [--expires 2027-12-31] [--edition professional]`
3. Send them the printed key (`SSA1-...`); they paste it into the window. Done.

## Limits to know
- A determined user can remove the check by patching the program; this stops casual copying, not reverse engineering.
- Reinstalling Windows changes the machine code, so a new key is needed.
- An expiry date relies on the computer clock.
- The email credit vouchers (`credits.py`) are separate; they need the `SMARTSCHOOL_VOUCHER_SECRET` environment
  variable or a `voucher_secret.key` file in the data folder on the computer that checks them.
