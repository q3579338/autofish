# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [
    ("assets/bite_wave.npy", "assets"),
    ("assets/show_wave.npy", "assets"),
]
binaries = []
hiddenimports = [
    "soundcard.mediafoundation",
    "multiprocessing",
    "multiprocessing.resource_tracker",
    "multiprocessing.spawn",
]

for pkg in ("rapidocr_onnxruntime", "onnxruntime", "soundcard"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    ["autofish.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib",
        "pandas",
        "tkinter",
        "IPython",
        "notebook",
        "pytest",
        "pythoncom",
        "pywintypes",
        "win32com",
        "win32api",
        "win32con",
        "pywin32",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AutoFish",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AutoFish",
)
