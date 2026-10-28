<#
 Builds the Windows installer.  Run from the repository root in PowerShell:
     powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
 Needs: Python 3.11+ (64-bit) and Inno Setup 6 (https://jrsoftware.org/isdl.php).
 Output: installer\SmartSchoolManager-Setup-<version>.exe
#>
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

Write-Host "==> Creating build environment"
python -m venv .venv-build
$py = ".\.venv-build\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -r requirements-build.txt -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw "Build and test dependencies could not be installed. Check package-network access, then retry." }

Write-Host "==> Running tests (the build stops if any fail)"
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { throw "Tests failed - installer not built." }

$enforced = (& $py -c "from smartschool import licensing; print(licensing.is_enforced())").Trim()
if ($enforced -ne "True") {
    Write-Warning "No licence public key is set (smartschool\license_public_key.py): this build will NOT ask for an activation key. See docs\LICENSING.md."
}

Write-Host "==> Building the application folder with PyInstaller"
& $py -m PyInstaller packaging\SmartSchoolManager.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }
if (-not (Test-Path "dist\SmartSchoolManager\SmartSchoolManager.exe")) { throw "Executable missing after build." }

$version = (& $py -c "from smartschool.version import __version__ as v; print(v)").Trim()

Write-Host "==> Building the installer with Inno Setup ($version)"
$candidates = @(
    (Get-Command iscc -ErrorAction SilentlyContinue).Source,
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) }
if (-not $candidates) { throw "Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php" }
& $candidates[0] "/DAppVersion=$version" packaging\installer.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }

Write-Host "==> Done: installer\SmartSchoolManager-Setup-$version.exe"
