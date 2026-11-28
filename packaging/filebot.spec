# -*- mode: python ; coding: utf-8 -*-
# One-folder PyInstaller build:  pyinstaller packaging/filebot.spec --noconfirm
# Models and llama-server are NOT bundled here; packaging/build.ps1 stages them into
# dist/FileBot/_internal/{bin,models} after the freeze so this step stays fast and CI-friendly.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve().parent

datas = [(str(ROOT / "config"), "config")]
binaries = []
hiddenimports = []

# Heavy third-party packages that load plugins/data/native libraries dynamically.
for package in ("fastembed", "chromadb", "onnxruntime", "tokenizers", "docx", "pymupdf", "fitz"):
    try:
        package_datas, package_binaries, package_hidden = collect_all(package)
    except Exception as exc:  # optional alias (fitz/pymupdf) or a package missing on this platform
        print(f"spec: skipping {package}: {exc}")
        continue
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

# First-party modules are imported lazily at wake time, so static analysis misses them.
for package in ("agent", "daemon", "executor", "retrieval", "storage", "ui"):
    hiddenimports += collect_submodules(package)
hiddenimports += ["selfcheck", "resources", "filebot_config", "filebot_version", "demo_support"]

# pynput picks its backend at runtime.
if sys.platform == "win32":
    hiddenimports += ["pynput.keyboard._win32", "pynput.mouse._win32"]
elif sys.platform.startswith("linux"):
    hiddenimports += ["pynput.keyboard._xorg", "pynput.mouse._xorg"]

a = Analysis(
    [str(ROOT / "app.pyw")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FileBot",
    console=False,  # windowed: the tray/avatar app has no console
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="FileBot")
