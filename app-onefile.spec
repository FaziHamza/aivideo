# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for a SINGLE-FILE build.  Built via build-onefile.bat.

The same app as app.spec, delivered as one .exe instead of a folder. The
trade is startup time: everything here - Qt, OpenCV, FFmpeg - is unpacked to
a temp directory on every launch, which the folder build does not do.

Worth it when the app is opened once and left running for a day's batch, and
not worth it when it is opened and closed repeatedly. Measure before
choosing; build.bat still produces the folder version.

data/ still lands next to the .exe, not in the temp payload: config.py reads
sys.executable when frozen, and in onefile mode that is the real .exe path
rather than the unpack directory. That is what keeps the reaction library and
settings across launches.
"""

import os
import shutil
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

APP_NAME = "ReactionVideoBuilder"
ROOT = Path(SPECPATH)

# ---- bundled FFmpeg -------------------------------------------------------
# NO_FFMPEG=1 drops ~193 MB from the file and a large part of the unpack time,
# at the cost of every machine needing FFmpeg on PATH.
binaries = []
if os.environ.get("NO_FFMPEG") != "1":
    for tool in ("ffmpeg", "ffprobe"):
        found = shutil.which(tool)
        if found:
            binaries.append((found, "ffmpeg"))
        else:
            print(f"[spec] {tool} not found on PATH - building without it")

hiddenimports = collect_submodules("scenedetect")
# ---- version resource -----------------------------------------------------
# Generated from core.__version__ so the exe's Properties tab cannot drift
# from the number the window title shows. See packaging/version_info.py.
import importlib.util as _ilu
_vi_spec = _ilu.spec_from_file_location(
    "rvb_version_info", str(ROOT / "packaging" / "version_info.py"))
_vi = _ilu.module_from_spec(_vi_spec)
_vi_spec.loader.exec_module(_vi)
VERSION_FILE = str(_vi.write(ROOT / "packaging" / "version_info.txt"))


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

# The single-file difference: the binaries and data go INTO the EXE
# (exclude_binaries=False) and there is no COLLECT step after it.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # windowed: no console flashing behind the GUI
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
    version=VERSION_FILE,
)
