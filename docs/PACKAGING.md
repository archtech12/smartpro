# Building the Windows installer

The app ships as a normal Windows installer (`SmartSchoolManager-Setup-<version>.exe`)
built with **PyInstaller** (turns the Python app into a folder with `SmartSchoolManager.exe`)
and **Inno Setup 6** (wraps that folder in a wizard installer).

## One-time setup (on a Windows 10/11 64-bit PC)
1. Install Python 3.11 or newer (64-bit) from python.org; tick *Add python.exe to PATH*.
2. Install Inno Setup 6 from https://jrsoftware.org/isdl.php.

## Build
From the repository root in PowerShell:

    powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1

The script creates `.venv-build`, installs `requirements-build.txt`, **runs the full test
suite (the build stops if anything fails)**, runs PyInstaller, then Inno Setup.
Result: `installer\SmartSchoolManager-Setup-<version>.exe`.

## Releasing a new version
1. Change `__version__` in `smartschool/version.py`.
2. Run the build script. Never change the `AppId` GUID in `packaging/installer.iss`:
   it is how a new installer upgrades an existing installation in place.

## What the installer does
- Installs per user by default (no admin password); the wizard offers "all users".
- Optional desktop shortcut; Start menu entry; clean uninstall entry.
- Closes a running copy of the app before upgrading.

## School data is never touched
The database, backups and logs live in `%LOCALAPPDATA%\SmartSchoolManagerRebuild`
(or the folder in the `SMARTSCHOOL_DATA_DIR` environment variable). The installer and
uninstaller only manage the program folder, so **upgrading or uninstalling keeps all school
data**. The app takes an integrity-checked backup before any schema migration.
To move to a new PC, copy that folder (or use the app's Data backup page) and restore it.

## Before giving the installer to a school
- Install it on a clean Windows PC (or VM) and walk through: first-run admin setup, setup guide,
  a report card PDF with 20+ subjects, a fee statement, backup and restore.
- Upgrade test: install the previous version, add synthetic data, install the new one, confirm
  the data is still there.
- **SmartScreen**: an unsigned installer shows "Windows protected your PC". A code-signing
  certificate removes the warning (sign `SmartSchoolManager.exe` and the setup file with
  `signtool`). Without one, users choose *More info -> Run anyway*.
- Antivirus: the build deliberately uses a one-folder layout and no UPX compression, which
  avoids most false positives from self-extracting executables.

## Why not a single .exe?
A one-file PyInstaller build unpacks itself to a temp folder on every start (slower, and
often flagged by antivirus). A proper installer with a one-folder app starts faster and
supports upgrades, shortcuts and uninstall.

## Activation keys
Before the release build, set up the licence key pair and paste the public key into `smartschool/license_public_key.py`
(see `docs/LICENSING.md`). `build_windows.ps1` warns when no public key is set, because such a build starts without
asking for an activation key. `tools/license_keygen.py` is vendor-only and is not included in the installer.
