@echo off
rem Pass-through to the command line tool:  cli doctor / cli render <files>
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" cli.py %*
) else (
    python cli.py %*
)
endlocal
