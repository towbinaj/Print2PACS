# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for the standalone Print2PACS app (onefile)."""

import os
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in ("customtkinter", "tkinterdnd2"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# Bundle the assets folder if present (e.g. assets/icon.ico).
if os.path.isdir("assets"):
    datas.append(("assets", "assets"))

_icon = "assets/icon.ico"
icon = _icon if os.path.exists(_icon) else None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Print2PACS",
    debug=False,
    strip=False,
    upx=True,
    console=False,
    icon=icon,
)
