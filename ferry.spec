# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Ferry — the Discord-to-Stoat migration GUI."""

import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

# Read version from source to avoid hardcoding
_version_match = re.search(
    r'__version__\s*=\s*"(.+?)"',
    Path("src/discord_ferry/__init__.py").read_text(),
)
VERSION = _version_match.group(1) if _version_match else "0.0.0"

# ---------------------------------------------------------------------------
# Collect package data, binaries, and hidden imports
# ---------------------------------------------------------------------------

# NiceGUI bundles static assets, Vue.js components, Quasar UI, etc.
nicegui_datas, nicegui_bins, nicegui_imports = collect_all("nicegui")

# aiohttp and stoat have their own data files
aiohttp_datas, aiohttp_bins, aiohttp_imports = collect_all("aiohttp")
stoat_datas, stoat_bins, stoat_imports = collect_all("stoat")

# pywebview (optional — native desktop window mode)
try:
    webview_datas, webview_bins, webview_imports = collect_all("webview")
except Exception:
    webview_datas, webview_bins, webview_imports = [], [], []

# Submodules that get missed by automatic analysis
uvicorn_imports = collect_submodules("uvicorn")
ferry_imports = collect_submodules("discord_ferry")

# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

all_datas = nicegui_datas + aiohttp_datas + stoat_datas + webview_datas
all_binaries = nicegui_bins + aiohttp_bins + stoat_bins + webview_bins
all_hiddenimports = (
    nicegui_imports
    + aiohttp_imports
    + stoat_imports
    + webview_imports
    + uvicorn_imports
    + ferry_imports
    + [
        "fastapi",
        "starlette",
        "starlette.routing",
        "starlette.staticfiles",
        "starlette.middleware",
        "starlette.middleware.cors",
        "starlette.middleware.gzip",
        "multipart",
        "dotenv",
        "aiofiles",
    ]
)

# ---------------------------------------------------------------------------
# Platform icon (graceful fallback if assets/ is empty)
# ---------------------------------------------------------------------------

icon_win = "assets/ferry.ico" if Path("assets/ferry.ico").exists() else None
icon_mac = "assets/ferry.icns" if Path("assets/ferry.icns").exists() else None

if sys.platform == "win32":
    icon = icon_win
elif sys.platform == "darwin":
    icon = icon_mac
else:
    icon = None

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

a = Analysis(
    ["src/discord_ferry/gui.py"],
    pathex=["src"],
    binaries=all_binaries,
    datas=all_datas,
    hiddenimports=all_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PIL", "numpy", "pandas"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Ferry",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)

# macOS .app bundle (only when building on macOS)
if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="Ferry.app",
        icon=icon_mac,
        bundle_identifier="com.discord-ferry.Ferry",
        info_plist={
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": VERSION,
        },
    )
