"""Guards the installer inputs so a release build cannot silently drift."""

import re
from pathlib import Path

from smartschool import version

ROOT = Path(__file__).resolve().parents[1]


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", version.__version__)


def test_installer_script_protects_school_data_and_upgrades_in_place():
    iss = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    assert "AppId={{E4932109-4FDE-4069-BA22-947BFEA9DCB5}" in iss
    assert "PrivilegesRequired=lowest" in iss
    assert "SmartSchoolManagerRebuild" in iss  # documented as kept on uninstall
    # the uninstaller may only delete the program folder, never the data folder
    deletes = [line for line in iss.splitlines() if line.startswith("Type:") and "Name:" in line]
    assert deletes and all("{app}" in line for line in deletes)
    assert "LOCALAPPDATA" not in "\n".join(deletes).upper()


def test_pyinstaller_spec_builds_windowed_onedir_app_with_icon():
    spec = (ROOT / "packaging" / "SmartSchoolManager.spec").read_text(encoding="utf-8")
    assert "console=False" in spec and "COLLECT(" in spec and "upx=False" in spec
    assert (ROOT / "packaging" / "app.ico").stat().st_size > 1000


def test_installer_data_directory_matches_application_default():
    from smartschool import database as db

    source = Path(db.__file__).read_text(encoding="utf-8")
    assert "SmartSchoolManagerRebuild" in source
