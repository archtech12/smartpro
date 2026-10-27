# PyInstaller spec — one-folder, windowed build.  Run from the repository root:
#   pyinstaller packaging/SmartSchoolManager.spec --noconfirm --clean
# One-folder starts faster than one-file and avoids antivirus false positives
# caused by self-extracting executables.
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

ROOT = Path(SPECPATH).parent
ns = {}
exec((ROOT / "smartschool" / "version.py").read_text(encoding="utf-8"), ns)
version = ns["__version__"]
parts = (version.split(".") + ["0", "0", "0", "0"])[:4]
numeric = tuple(int(p) if p.isdigit() else 0 for p in parts)

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=numeric, prodvers=numeric, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", ns["PUBLISHER"]),
            StringStruct("FileDescription", ns["APP_NAME"]),
            StringStruct("FileVersion", version),
            StringStruct("InternalName", "SmartSchoolManager"),
            StringStruct("OriginalFilename", "SmartSchoolManager.exe"),
            StringStruct("ProductName", ns["APP_NAME"]),
            StringStruct("ProductVersion", version),
        ])]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

# The app only uses QtCore, QtGui and QtWidgets; leaving the rest out keeps the installer small.
UNUSED_QT = [
    "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtWebEngineQuick", "PyQt6.QtWebChannel",
    "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.QtQuickWidgets", "PyQt6.QtQuick3D", "PyQt6.Qt3DCore",
    "PyQt6.Qt3DRender", "PyQt6.Qt3DInput", "PyQt6.Qt3DLogic", "PyQt6.Qt3DAnimation", "PyQt6.Qt3DExtras",
    "PyQt6.QtMultimedia", "PyQt6.QtMultimediaWidgets", "PyQt6.QtBluetooth", "PyQt6.QtNfc",
    "PyQt6.QtSensors", "PyQt6.QtSerialPort", "PyQt6.QtPositioning", "PyQt6.QtLocation",
    "PyQt6.QtCharts", "PyQt6.QtDataVisualization", "PyQt6.QtRemoteObjects", "PyQt6.QtSql",
    "PyQt6.QtTest", "PyQt6.QtDesigner", "PyQt6.QtHelp", "PyQt6.QtNetwork", "PyQt6.QtOpenGL",
    "PyQt6.QtOpenGLWidgets", "PyQt6.QtSvg", "PyQt6.QtSvgWidgets", "PyQt6.QtPdf", "PyQt6.QtPdfWidgets",
    "PyQt6.QtDBus", "PyQt6.QtStateMachine", "PyQt6.QtTextToSpeech", "PyQt6.QtSpatialAudio",
    "tkinter", "pytest",
]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=["openpyxl", "et_xmlfile"],
    hookspath=[],
    runtime_hooks=[],
    excludes=UNUSED_QT,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SmartSchoolManager",
    console=False,            # no console window for end users
    icon=str(ROOT / "packaging" / "app.ico"),
    version=version_info,
    upx=False,                # UPX-packed executables trigger antivirus warnings
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="SmartSchoolManager")
