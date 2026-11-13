# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir 构建配置：产出 dist/FnPlayer/FnPlayer.exe（无控制台窗口）。"""
from pathlib import Path
import os

from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable,
    VarFileInfo, VarStruct, VSVersionInfo,
)

ROOT = Path(SPECPATH).parent
WEB = ROOT / "app" / "web"
KERNEL = ROOT / "app" / "kernel"
# 构建到独立目录时，COLLECT 也必须跟着改名，避免误删正在使用的默认目录
BUILD_NAME = os.environ.get("FNPLAYER_BUILD_NAME") or "FnPlayer"

hidden = []
for _m in ("uvicorn", "fastapi", "starlette", "pydantic", "anyio",
           "httpx", "httpcore", "h11", "idna", "certifi", "sniffio",
           "segno"):
    hidden += collect_submodules(_m)
hidden += ["server", "hls", "registry", "danmu", "danmu_service", "lan",
           "config", "providers", "netutil", "features", "sniff"]

version = VSVersionInfo(
    ffi=FixedFileInfo(filevers=(1, 0, 0, 0), prodvers=(1, 0, 0, 0),
                      mask=0x3F, flags=0x0, OS=0x40004,
                      fileType=0x1, subtype=0x0, date=(0, 0)),
    kids=[
        StringFileInfo([StringTable("080404b0", [
            StringStruct("CompanyName", "FnPlayer"),
            StringStruct("FileDescription", "FnPlayer - 本地媒体播放器"),
            StringStruct("FileVersion", "1.0.0.0"),
            StringStruct("InternalName", "FnPlayer"),
            StringStruct("LegalCopyright", "Upstream fnmedia-ext (MIT); hls.js (Apache-2.0)"),
            StringStruct("OriginalFilename", "FnPlayer.exe"),
            StringStruct("ProductName", "FnPlayer"),
            StringStruct("ProductVersion", "1.0.0"),
        ])]),
        VarFileInfo([VarStruct("Translation", [2052, 1200])]),
    ],
)

a = Analysis(
    [str(ROOT / "app" / "main.py")],
    pathex=[str(ROOT / "app"), str(KERNEL)],
    binaries=[],
    datas=[
        (str(WEB), "web"),
        (str(KERNEL), "kernel"),
    ],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PIL", "numpy", "pandas", "matplotlib",
              "PyQt5", "PySide6", "IPython", "pytest", "unittest",
              "setuptools", "wheel"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="FnPlayer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(WEB / "fnplayer.ico"),
    version=version,
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, upx_exclude=[],
    name=BUILD_NAME,
)
