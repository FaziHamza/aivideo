@echo off
rem Run the regression suite: planner rules, detection accuracy, render output.
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m tests.run_all %*
) else (
    python -m tests.run_all %*
)
if errorlevel 1 pause
endlocal
