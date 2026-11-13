#!/usr/bin/env python3
"""获取 FnPlayer 构建所需的、体积大而不适合进 Git 的外部资源。

两件事：

  1. 弹幕核心源码（danmu_api，AGPL-3.0）
     从 https://github.com/huangxd-/danmu_api 下载源码包，解到
     ../danmu_api_src/。之后用 tools/prepare_portable.py 组装便携版工作区，
     用 android/build_danmu_core.py 生成安卓版内置核心。

  2. 安卓端 Node 运行时（nodejs-mobile，MIT）
     从 https://github.com/lilixu3/danmu-api-android 的 Release v1.0.5.118
     下载三个架构的 APK，从中提取 lib/<abi>/{libnode.so,libnative-lib.so,
     libc++_shared.so} 到 android/lib/<abi>/，并校验 SHA-256。

用法：
  python tools/fetch_sources.py --all
  python tools/fetch_sources.py --danmu
  python tools/fetch_sources.py --node-runtime
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION = ROOT.parent
DANMU_OUT = SESSION / "danmu_api_src"
LIB_DIR = ROOT / "android" / "lib"

DANMU_REPO = "huangxd-/danmu_api"
DANMU_TARBALL = "https://codeload.github.com/%s/tar.gz/refs/heads/main" % DANMU_REPO

UPSTREAM_REPO = "lilixu3/danmu-api-android"
UPSTREAM_TAG = "v1.0.5.118"
UPSTREAM_ASSETS = {
    "arm64-v8a": "app-1.0.5.118-215-arm64-v8a.apk",
    "armeabi-v7a": "app-1.0.5.118-215-armeabi-v7a.apk",
    "x86_64": "app-1.0.5.118-215-x86_64.apk",
}
UPSTREAM_URL = ("https://github.com/%s/releases/download/%s/%%s" % (UPSTREAM_REPO, UPSTREAM_TAG))

# 提取出的原生库指纹（与 android/lib/ 中已验证的产物一致）
EXPECTED = {
    "arm64-v8a": {
        "libnode.so": "8cc5e0044969dfed7bcad79f6d77d82f623028de5dbc4587d02dd51b13badb4e",
        "libnative-lib.so": "c656ad0b7f94b4f8f9772dea1396bfc5c6794ea1fc1d1f97687bb4cb15109c5c",
        "libc++_shared.so": "c66d737477e8e0e8861cc0967a2d81ca6b8ef55b35b93886c75bbed8e8b87af0",
    },
    "armeabi-v7a": {
        "libnode.so": "b648eacbe3c641bf8bc89bef72c08e91c72e8ae1596fc59105be7ff2c148179e",
        "libnative-lib.so": "e0ba24d42b4e2510aa58101250d7addafd478ecc857b6b640938ccf83200d521",
        "libc++_shared.so": "327d8f5e96fcb749d98d001b7296618e576092c1bd8d9e50f9dc9b2b329b7e3f",
    },
    "x86_64": {
        "libnode.so": "679a4868c9e1c6df1f9a84984c82b3a00821a6ed777367e4727b53cc78346539",
        "libnative-lib.so": "609c5681cb0abdccc38dd46d2e5d29d3971b280ac5d3bb4166dd497511681fdd",
        "libc++_shared.so": "d5004405eaa19cbfb27ead588ac66e4516d5bbaa707febc27aaa20a6993b4b8d",
    },
}


def opener():
    # 显式绕过系统代理：Windows 注册表里的代理端口常常没有进程在听
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def download(url: str, dest: Path) -> Path:
    print("  下载 %s" % url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "FnPlayer-fetch"})
    with opener().open(req, timeout=600) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f, 1024 * 256)
    print("  -> %s  (%.1f MB)" % (dest, dest.stat().st_size / 1048576))
    return dest


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_danmu():
    print("== 弹幕核心源码 danmu_api (%s) ==" % DANMU_REPO)
    tmp = SESSION / "_danmu_api.tar.gz"
    download(DANMU_TARBALL, tmp)
    print("  解到 %s" % DANMU_OUT)
    DANMU_OUT.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tmp, "r:gz") as tf:
        members = tf.getmembers()
        prefix = members[0].name.split("/")[0] + "/" if members else ""
        for m in members:
            if not prefix or not m.name.startswith(prefix):
                continue
            m.name = m.name[len(prefix):]
            if not m.name:
                continue
            tf.extract(m, DANMU_OUT, filter="data")
    try:
        tmp.unlink()
    except OSError:
        pass
    lic = DANMU_OUT / "LICENSE"
    print("  许可文件: %s" % ("已保留 " + str(lic) if lic.is_file() else "缺失！"))
    print("  下一步: python tools/prepare_portable.py --src %s" % DANMU_OUT)


def fetch_node_runtime():
    print("== 安卓端 Node 运行时 (%s %s) ==" % (UPSTREAM_REPO, UPSTREAM_TAG))
    tmpdir = ROOT / "android" / "build" / "_fetch"
    tmpdir.mkdir(parents=True, exist_ok=True)
    for abi, asset in UPSTREAM_ASSETS.items():
        print(" [%s]" % abi)
        apk = download(UPSTREAM_URL % asset, tmpdir / asset)
        dest = LIB_DIR / abi
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(apk) as z:
            for name in ("libnode.so", "libnative-lib.so", "libc++_shared.so"):
                member = "lib/%s/%s" % (abi, name)
                if member not in z.namelist():
                    print("   ! APK 里没有 %s" % member)
                    continue
                out = dest / name
                out.write_bytes(z.read(member))
                got = sha256(out)
                want = EXPECTED[abi][name]
                if got == want:
                    print("   OK %-18s %s" % (name, got[:16] + "…"))
                else:
                    print("   ! %s 指纹不符\n     期望 %s\n     实际 %s" % (name, want, got))
        try:
            apk.unlink()
        except OSError:
            pass
    print("  下一步: python android/build_danmu_core.py")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--danmu", action="store_true", help="下载 danmu_api 源码")
    ap.add_argument("--node-runtime", action="store_true", help="下载安卓端 Node 运行时")
    ap.add_argument("--all", action="store_true", help="全部下载")
    args = ap.parse_args()
    if not (args.danmu or args.node_runtime or args.all):
        ap.print_help()
        return
    if args.danmu or args.all:
        fetch_danmu()
    if args.node_runtime or args.all:
        fetch_node_runtime()
    print("\n完成。")


if __name__ == "__main__":
    main()
