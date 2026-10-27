@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo The project virtual environment is missing.
    echo Create it with: py -m venv .venv
    echo Then install dependencies with: .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
    pause
    exit /b 1
)

".venv\Scripts\python.exe" main.py
exit /b %errorlevel%
