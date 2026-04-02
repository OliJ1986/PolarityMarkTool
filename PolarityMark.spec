# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for PolarityMark
# Build:  .venv\Scripts\pyinstaller.exe PolarityMark.spec

from pathlib import Path
import sys, os

# ── Locate key package directories ───────────────────────────────────────────

VENV = Path(".venv/Lib/site-packages")

def pkg(name: str) -> Path:
    return VENV / name

# ── Data files (non-Python assets bundled alongside the exe) ─────────────────

datas = []

# ezdxf: font definitions + resources (needed for DXF rendering)
for subdir in ("fonts", "resources"):
    src = pkg("ezdxf") / subdir
    if src.exists():
        datas.append((str(src), f"ezdxf/{subdir}"))

# pymupdf: the __init__.py, table.py, utils.py, mupdf.py etc. are in the package
# PyInstaller collects them automatically, but we add the package folder explicitly
# so the large mupdf.py stub is available at runtime.
pymupdf_dir = pkg("pymupdf")
if pymupdf_dir.exists():
    datas.append((str(pymupdf_dir), "pymupdf"))

# ── Native binaries ───────────────────────────────────────────────────────────

binaries = []

# mupdfcpp64.dll — the main MuPDF engine DLL (loaded by _mupdf.pyd)
mupdf_dll = pkg("pymupdf") / "mupdfcpp64.dll"
if mupdf_dll.exists():
    binaries.append((str(mupdf_dll), "pymupdf"))

# ── Hidden imports (not auto-detected by PyInstaller) ────────────────────────

hiddenimports = [
    # PyMuPDF
    "fitz",
    "fitz.fitz",
    "pymupdf",
    # PySide6 essentials
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtPrintSupport",
    # shapely geometry ops
    "shapely",
    "shapely.geometry",
    "shapely.geometry.polygon",
    "shapely.geometry.multipolygon",
    "shapely.ops",
    "shapely.affinity",
    # ezdxf internals
    "ezdxf.fonts",
    "ezdxf.resources",
    "ezdxf.math._vector",
    "ezdxf.math._matrix44",
    # project packages
    "core",
    "gui",
    "utils",
]

# ── Modules to exclude (unused Qt modules — reduces size by ~200 MB) ─────────

excludes = [
    "tkinter",
    "matplotlib",
    "scipy",
    "pandas",
    "IPython",
    "PIL",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtBluetooth",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtLocation",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetwork",
    "PySide6.QtNfc",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtPositioning",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects",
    "PySide6.QtSensors",
    "PySide6.QtSerialBus",
    "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio",
    "PySide6.QtSql",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtTest",
    "PySide6.QtTextToSpeech",
    "PySide6.QtUiTools",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebSockets",
    "PySide6.QtXml",
]

# ─────────────────────────────────────────────────────────────────────────────
# Analysis
# ─────────────────────────────────────────────────────────────────────────────

a = Analysis(
    ["app.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={
        "PySide6": {
            # Keep only what we actually use
            "excluded_plugins": [
                "Qt3D*", "QtBluetooth*", "QtCharts*",
                "QtDataVisualization*", "QtMultimedia*",
                "QtQml*", "QtQuick*", "QtSensors*",
                "QtWebEngine*",
            ],
        }
    },
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PolarityMark",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX can break Qt DLLs — leave off
    console=False,      # no black console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon="assets/icon.ico",  # uncomment if you add an icon file
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PolarityMark",
)

