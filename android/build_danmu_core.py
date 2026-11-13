#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
组装安卓端内置弹幕核心（Node + danmu_api）。

步骤：
  1. 从桌面便携版的 danmu_api 复制核心源码（排除仅服务端/构建期需要的文件）。
  2. 静态扫描 import/require，解析出真正的运行时依赖闭包，
     只把用到的包复制进 node_modules（避免 100MB+ 的整包塞进 APK）。
  3. 生成 manifest（版本 + 文件清单 + 校验值），供播放器判断是否需要重新安装。

用法：python android/build_danmu_core.py
"""
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

AND = Path(__file__).resolve().parent
ROOT = AND.parent
SRC_WORKSPACE = ROOT / "portable" / "workspace" / "danmu"
SRC_CORE = SRC_WORKSPACE / "danmu_api"
SRC_MODULES = SRC_WORKSPACE / "node_modules"
OUT = AND / "assets" / "danmu-core"
OUT_CORE = OUT / "danmu_api"
OUT_MODULES = OUT / "node_modules"

# 仅服务端入口/构建期使用，手机端用 server-entry.cjs 替代 server.js
# 注意：ui/ 目录虽只 572KB，但 apis/system-api.js 会 import ui/template.js，必须保留。
EXCLUDE_FILES = {"server.js", "esm-shim.cjs"}
EXCLUDE_DIRS = {"logs", ".cache", "__pycache__"}

# Node 内置模块：不进 node_modules
BUILTINS = {
    "assert", "async_hooks", "buffer", "child_process", "cluster", "console",
    "constants", "crypto", "dgram", "diagnostics_channel", "dns", "domain",
    "events", "fs", "http", "http2", "https", "inspector", "module", "net",
    "os", "path", "perf_hooks", "process", "punycode", "querystring",
    "readline", "repl", "stream", "string_decoder", "sys", "timers", "tls",
    "trace_events", "tty", "url", "util", "v8", "vm", "wasi", "worker_threads",
    "zlib", "test", "sqlite", "sea", "quic",
}

# 本地 Redis 属可选能力（仅在配置 LOCAL_REDIS_URL 时才会被动态导入），
# 其依赖链含 @electric-sql/pglite + drizzle-orm，合计 60MB+ 且与播放无关，整族剔除。
EXCLUDE_PACKAGES = {
    "redis", "redis-errors", "redis-parser", "cluster-key-slot",
    "drizzle-orm", "@redis/client", "@redis/bloom", "@redis/json", "@redis/search",
    "@redis/time-series", "@redis/error", "@redis/client-dist",
    "@electric-sql/pglite",
}

IMPORT_RE = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*\(\s*|\brequire\s*\(\s*|\bimport\s+|^\s*import\s+)['"]([^'"]+)['"]""",
    re.M,
)

# 许可文件名（大小写不敏感）。MIT / BSD / Apache 等许可都要求「随副本附带
# 许可声明」，这些文件不能被裁剪规则删掉。
LICENSE_NAMES = {
    "license", "licence", "license.md", "licence.md", "license.txt",
    "licence.txt", "license-mit", "licence-mit", "copying", "copying.txt",
    "notice", "notice.txt", "unlicense", "licenses",
}

# 拷贝时要剔除的目录名
PRUNE_DIRS = {
    "test", "tests", "__tests__", "docs", "doc", "example", "examples",
    ".github", ".idea", ".vscode", "benchmark", "benchmarks",
}


def _ignore_for_copy(directory: str, names: list) -> set:
    """shutil.copytree 的 ignore 回调。

    剔除文档与测试目录、README 等说明文件，但**保留所有许可文件**
    （node-fetch 的 LICENSE.md、bignumber.js 的 LICENCE.md 都在此列）。
    """
    dropped = set()
    for name in names:
        full = Path(directory) / name
        if full.is_dir():
            if name in PRUNE_DIRS:
                dropped.add(name)
            continue
        low = name.lower()
        if low in LICENSE_NAMES:
            continue
        if low.startswith("license") or low.startswith("licence") or low.startswith("copying"):
            continue
        if low.startswith("readme") or low.endswith(".md") or low.endswith(".markdown"):
            dropped.add(name)
    return dropped


def pkg_of(spec: str) -> str:
    """把 import 说明符解析成 node_modules 包名。"""
    if not spec or spec.startswith((".", "/", "node:", "data:", "http:", "https:", "file:")):
        return ""
    parts = spec.split("/")
    if spec.startswith("@"):
        if len(parts) < 2:
            return ""
        name = "/".join(parts[:2])
    else:
        name = parts[0]
    # 只接受合法的 npm 包名，避免把字符串里的标点误当成依赖
    if not re.match(r"^(@[a-z0-9-~][a-z0-9-._~]*/)?[a-z0-9-~][a-z0-9-._~]*$",
                    name, re.I):
        return ""
    return name


def is_excluded(name: str) -> bool:
    if name in BUILTINS or name in EXCLUDE_PACKAGES:
        return True
    for base in EXCLUDE_PACKAGES:
        if name.startswith(base + "/"):
            return True
    return False


def scan_imports(root: Path) -> set:
    found = set()
    for path in root.rglob("*"):
        if path.suffix.lower() not in (".js", ".cjs", ".mjs"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in IMPORT_RE.finditer(text):
            name = pkg_of(m.group(1))
            if name:
                found.add(name)
    return found


def copy_package(name: str, out_modules: Path, stats: dict) -> None:
    if name in stats["done"]:
        return
    if is_excluded(name):
        stats["skipped"].append(name)
        return
    src = SRC_MODULES / name
    if not src.is_dir():
        stats["missing"].append(name)
        return
    dst = out_modules / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True, ignore=_ignore_for_copy)
    stats["done"].add(name)
    pkg_json = src / "package.json"
    if not pkg_json.is_file():
        return
    try:
        meta = json.loads(pkg_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    deps = dict(meta.get("dependencies") or {})
    for dep in deps:
        copy_package(dep, out_modules, stats)


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main() -> int:
    if not SRC_CORE.is_dir():
        print("找不到源核心:", SRC_CORE)
        return 1

    if OUT.exists():
        try:
            shutil.rmtree(OUT)
        except OSError as e:
            # 某些环境不允许整体删除（安全删除策略），退化为增量覆盖：
            # 文件按 copytree(dirs_exist_ok=True) 覆盖，manifest 仍按实际目录重算。
            print("注意：无法整体清理旧目录（%s），改为增量覆盖" % e)
    OUT_CORE.mkdir(parents=True, exist_ok=True)
    OUT_MODULES.mkdir(parents=True, exist_ok=True)

    # 1) 复制核心源码
    copied = 0
    for src in SRC_CORE.rglob("*"):
        rel = src.relative_to(SRC_CORE)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if src.is_dir():
            continue
        if src.name in EXCLUDE_FILES:
            continue
        dst = OUT_CORE / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1

    # 1b) 随核心分发许可原文（AGPL 第 4 条要求「随副本附带许可声明」）
    # danmu_api 的 LICENSE 在工作区根目录，不在 danmu_api/ 内，需单独取。
    upstream_license = SRC_WORKSPACE / "LICENSE"
    if upstream_license.is_file():
        shutil.copy2(upstream_license, OUT / "LICENSE.danmu_api-AGPL-3.0.txt")
        print("  + 许可原文: LICENSE.danmu_api-AGPL-3.0.txt")
    else:
        print("  ! 未找到上游 LICENSE，APK 将缺少 AGPL 许可原文")

    # 2) 依赖闭包
    stats = {"done": set(), "missing": [], "skipped": []}
    roots = scan_imports(OUT_CORE)
    roots |= scan_imports(OUT)  # 包含 server-entry.js
    for name in sorted(roots):
        copy_package(name, OUT_MODULES, stats)

    # 3) 复制启动器（CommonJS .cjs，避免被核心的 type:module 影响）
    launcher = AND / "danmu-core-src" / "server-entry.cjs"
    if launcher.is_file():
        shutil.copy2(launcher, OUT / "server-entry.cjs")

    # 3b) 补齐上游遗漏的许可文件
    #     brotli / data-uri-to-buffer / @bufbuild/protobuf / @nodable/entities
    #     这几个 npm 包上游没带许可文本文件，但 MIT / Apache-2.0 都要求
    #     「随副本附带许可声明」，因此用 android/danmu-core-src/licenses/ 里的
    #     补录文本填进对应包目录。
    #     包名可能带作用域（@bufbuild/protobuf），不能编码进文件名，
    #     故用 licenses/manifest.json 显式映射「包名 -> 许可文件」。
    patched_lic = 0
    lic_dir = AND / "danmu-core-src" / "licenses"
    lic_manifest = lic_dir / "manifest.json"
    if lic_manifest.is_file():
        try:
            lic_map = json.loads(lic_manifest.read_text(encoding="utf-8"))
        except ValueError as e:
            print("  ! 许可清单解析失败: %s" % e)
            lic_map = {}
        for pkg, fname in sorted((lic_map.get("packages") or {}).items()):
            src_lic = lic_dir / fname
            pkg_dir = OUT_MODULES / pkg
            if not pkg_dir.is_dir():
                print("  ! 补录许可找不到包目录: %s" % pkg)
                continue
            if not src_lic.is_file():
                print("  ! 补录许可文件缺失: %s" % fname)
                continue
            shutil.copy2(src_lic, pkg_dir / "LICENSE.txt")
            patched_lic += 1
    if patched_lic:
        print("  + 补录许可文件: %d 个" % patched_lic)

    # 核心源码本身是 ESM（原本依赖工作区根目录的 package.json 声明 type:module），
    # 这里在核心根目录补一份等价声明。
    (OUT / "package.json").write_text(json.dumps({
        "name": "fn-danmu-core",
        "version": "1.0.0",
        "private": True,
        "type": "module",
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    # 4) manifest
    files = []
    for path in sorted(OUT.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files.append({
                "path": path.relative_to(OUT).as_posix(),
                "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    core_version = "unknown"
    pkg = SRC_WORKSPACE / "package.json"
    if pkg.is_file():
        try:
            core_version = json.loads(pkg.read_text(encoding="utf-8")).get("version", "unknown")
        except ValueError:
            pass
    manifest = {
        "core_version": core_version,
        "packages": sorted(stats["done"]),
        "missing": sorted(stats["missing"]),
        "skipped": sorted(stats["skipped"]),
        "files": files,
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    size_mb = dir_size(OUT) / 1024 / 1024
    print("核心文件: %d" % copied)
    print("依赖包: %d 个%s" % (len(stats["done"]),
          ("（缺失: " + ", ".join(sorted(stats["missing"])) + "）") if stats["missing"] else ""))
    print("已跳过可选包: %s" % (", ".join(sorted(stats["skipped"])) or "无"))
    print("目录大小: %.1f MB" % size_mb)
    return 0


if __name__ == "__main__":
    sys.exit(main())
