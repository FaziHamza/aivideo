# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the desktop app.  Built via build.bat.

Two things here are not boilerplate:

* FFmpeg and FFprobe are copied into the bundle when they can be found, so the
  machine this is handed to needs nothing installed.  core/ffmpeg.py looks in
  the bundle before it looks at PATH.  Set NO_FFMPEG=1 to build without them
  (about 420 MB smaller, but the user must install FFmpeg themselves).
* data/ is deliberately NOT bundled.  It holds the reaction library, settings
  and rendered output, and it is created next to the .exe on first run.
"""

import os
import shutil
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

APP_NAME = "ReactionVideoBuilder"
ROOT = Path(SPECPATH)

# ---- bundled FFmpeg -------------------------------------------------------
binaries = []
if os.environ.get("NO_FFMPEG") != "1":
    for tool in ("ffmpeg", "ffprobe"):
        found = shutil.which(tool)
        if found:
            binaries.append((found, "ffmpeg"))
        else:
            print(f"[spec] {tool} not found on PATH - building without it")

hiddenimports = collect_submodules("scenedetect")

# ---- branding -------------------------------------------------------------
# The header logo and the window icon. Bundled under assets/ so the paths in
# desktop/theme.py resolve the same way frozen as they do from source; the
# .exe icon is separate and has to be an .ico, hence both files.
datas = [("assets/xtroedge-logo-colour.png", "assets"),
         ("assets/xtroedge-logo-white.png", "assets"),
         ("assets/xtroedge-symbol.png", "assets"),
         ("assets/app-icon.png", "assets")]
ICON = str(ROOT / "assets" / "app-icon.ico")
if not Path(ICON).is_file():
    print("[spec] app-icon.ico missing - building without an .exe icon")
    ICON = None

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "pytest", "IPython", "notebook",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtQml",
        "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # windowed: no console flashing behind the GUI
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
