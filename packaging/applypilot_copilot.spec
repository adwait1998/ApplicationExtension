# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the friend build: ONE executable that is the Chrome
# native-messaging host, the local service and the host installer
# (see src/applypilot/extension/frozen_entry.py).
#
# Build through packaging/build_windows.ps1 or packaging/build_macos.sh, which
# pass --distpath/--workpath. Console app on purpose: the native host talks to
# Chrome over stdin/stdout.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")

# uvicorn loads its protocol/loop implementations by name at runtime.
hiddenimports = collect_submodules("uvicorn") + collect_submodules("applypilot.extension")

# python-docx ships its default document template as package data.
datas = collect_data_files("docx")

# Heavy packages other parts of applypilot use that the friend build never needs.
# (Tailored-résumé PDF rendering needs playwright; server.py answers 501 when frozen.)
excludes = ["playwright", "pandas", "numpy", "torch", "laya", "mcp", "typer", "bs4",
            "tkinter", "cryptography", "matplotlib", "IPython"]

a = Analysis(
    [os.path.join(SRC, "applypilot", "extension", "frozen_entry.py")],
    pathex=[SRC],
    hiddenimports=hiddenimports,
    datas=datas,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ApplyPilotCopilot",
    console=True,
    upx=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ApplyPilotCopilot")
