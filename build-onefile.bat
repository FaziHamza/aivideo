@echo off
rem Package the desktop app as ONE .exe: dist\ReactionVideoBuilder.exe
rem
rem   build-onefile.bat          one file, FFmpeg included (nothing to install)
rem   build-onefile.bat --slim   one file without FFmpeg (much smaller and
rem                              faster to start; every machine then needs
rem                              FFmpeg on PATH)
rem
rem Measured on the build machine: one file is 160 MB and takes about 7
rem seconds to open, every time, because it unpacks itself to %TEMP% on each
rem launch. build.bat's folder build is 428 MB and opens in about 1.2s. The
rem single file wins when the app is opened once and left running for a
rem day's batch, and loses if it is opened and closed all day.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
    echo No .venv found - using the system Python.
)

set "NO_FFMPEG="
for %%A in (%*) do (
    if /I "%%A"=="--slim" set "NO_FFMPEG=1"
)

"%PY%" -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo Installing PyInstaller...
    "%PY%" -m pip install pyinstaller || goto :fail
)

echo Building one file...
rmdir /s /q "build\app-onefile" 2>nul
del "dist\ReactionVideoBuilder.exe" 2>nul
"%PY%" -m PyInstaller --noconfirm --clean app-onefile.spec || goto :fail

rem PyInstaller reports success even when the EXE step produced nothing, so
rem check. This is the same trap build.bat fell into with its zip step.
if not exist "dist\ReactionVideoBuilder.exe" goto :fail

echo.
echo Done: dist\ReactionVideoBuilder.exe
echo.
echo Hand over that single file. It needs no folder and no zip. It creates a
echo "data" folder beside itself on first run, so put it somewhere writable -
echo the Desktop is fine, Program Files is not.
endlocal
exit /b 0

:fail
echo.
echo BUILD FAILED
endlocal
exit /b 1
