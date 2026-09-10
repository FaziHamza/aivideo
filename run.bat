@echo off
rem Launch the desktop app, using the project venv if it exists.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
    echo No .venv found - using the system Python.
    echo Create one with:  py -3.13 -m venv .venv
    echo.
)

"%PY%" main.py
if errorlevel 1 pause
endlocal
