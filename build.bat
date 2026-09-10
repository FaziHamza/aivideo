@echo off
rem Package the desktop app into dist\ReactionVideoBuilder\ReactionVideoBuilder.exe
rem
rem   build.bat            full build, FFmpeg included (nothing to install)
rem   build.bat --slim     no FFmpeg (~420 MB smaller; user must install it)
rem   build.bat --zip      full build, then zip the folder for sending
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
    echo No .venv found - using the system Python.
)

set "NO_FFMPEG="
set "MAKEZIP="
for %%A in (%*) do (
    if /I "%%A"=="--slim" set "NO_FFMPEG=1"
    if /I "%%A"=="--zip"  set "MAKEZIP=1"
)

"%PY%" -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo Installing PyInstaller...
    "%PY%" -m pip install pyinstaller || goto :fail
)

echo Building...
rmdir /s /q build 2>nul
rmdir /s /q "dist\ReactionVideoBuilder" 2>nul
"%PY%" -m PyInstaller --noconfirm --clean app.spec || goto :fail

rem PyInstaller puts data files under _internal\; this one has to sit next to
rem the .exe where the person opening the folder will actually see it.
copy /y "packaging\READ ME FIRST.txt" "dist\ReactionVideoBuilder\" >nul || goto :fail

if defined MAKEZIP (
    echo Zipping...
    del "dist\ReactionVideoBuilder.zip" 2>nul
    powershell -NoProfile -Command "Compress-Archive -Path 'dist\ReactionVideoBuilder' -DestinationPath 'dist\ReactionVideoBuilder.zip' -CompressionLevel Optimal" || goto :fail
)

echo.
echo Done: dist\ReactionVideoBuilder\ReactionVideoBuilder.exe
if defined MAKEZIP echo Send:  dist\ReactionVideoBuilder.zip
endlocal
exit /b 0

:fail
echo.
echo BUILD FAILED
endlocal
exit /b 1
