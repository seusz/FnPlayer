#!/usr/bin/env python3
"""从上游 fnmedia-ext 仓库提取桌面版所需的苹果CMS 内核模块。

用法：python tools/prepare_kernel.py [上游仓库路径]

设计要点：
  * 上游模块之间使用顶层绝对 import（import providers / import netutil ...），
    因此 kernel/ 目录整体被加入 sys.path，文件保持上游原样，最大化可追溯性。
  * 唯一的实质性改写是清空 providers.DEFAULT_SOURCES：桌面版不预置、不分发、
    不推荐任何第三方资源站地址，接口完全由使用者自行配置并自行核实合法性。
  * 移除仅 NAS 版使用的 T4 远程订阅分支（桌面版只支持标准苹果CMS JSON/XML 接口）。
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KERNEL = ROOT / "app" / "kernel"
DEFAULT_UPSTREAM = ROOT.parent / "fnmedia-ext"

COPY_AS_IS = ("features.py", "netutil.py", "sniff.py")

T4_SEARCH_BLOCK = '''    if source.get("kind") == "t4":
        import logging
        import t4
        try:
            return await t4.search(client, source, keyword)
        except t4.T4Error as exc:
            logging.getLogger("fnmedia").warning("T4 search %s: %s", source["id"], exc)
            return []
'''

T4_PROBE_BLOCK = '''    if source and source.get("kind") == "t4":
        import t4
        async with httpx.AsyncClient(trust_env=False) as client:
            return await t4.probe(client, source, timeout)
'''


def main() -> int:
    upstream = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_UPSTREAM
    proxy_dir = upstream / "proxy"
    if not proxy_dir.is_dir():
        print(f"ERROR: 找不到上游 proxy 目录: {proxy_dir}")
        return 1

    KERNEL.mkdir(parents=True, exist_ok=True)
    for name in COPY_AS_IS:
        shutil.copy2(proxy_dir / name, KERNEL / name)
        print(f"copied {name}")

    src = (proxy_dir / "providers.py").read_text(encoding="utf-8")

    # 1) 清空内置资源站列表（合规：桌面版零预置站点）
    pat = re.compile(
        r"(DEFAULT_SOURCES:\s*list\[dict\]\s*=\s*\[).*?(\n\]\n)", re.S)
    replacement = (
        "\\1\n"
        "    # 桌面版不预置任何资源站地址。请使用者在「设置」中自行添加\n"
        "    # 自己拥有合法授权 / 自建的苹果CMS V10 JSON 接口。\n"
        "\\2"
    )
    src, n = pat.subn(replacement, src, count=1)
    if n != 1:
        print("ERROR: 未能定位 DEFAULT_SOURCES")
        return 1

    # 2) 去掉 NAS 版专有的 T4 订阅分支（避免缺失模块导致异常）
    if T4_SEARCH_BLOCK in src:
        src = src.replace(T4_SEARCH_BLOCK, "")
    else:
        print("WARN: T4 search 分支未匹配，跳过")
    if T4_PROBE_BLOCK in src:
        src = src.replace(T4_PROBE_BLOCK, "")
    else:
        print("WARN: T4 probe 分支未匹配，跳过")

    header = (
        "# 来源: github.com/yujian0815/fnmedia-ext (MIT), proxy/providers.py\n"
        "# 桌面版改写: 清空内置资源站、移除 T4 订阅分支；其余保持上游实现。\n"
    )
    (KERNEL / "providers.py").write_text(header + src, encoding="utf-8")
    print("copied providers.py (patched)")

    lic = upstream / "LICENSE"
    if lic.exists():
        shutil.copy2(lic, KERNEL / "LICENSE.upstream")
        print("copied LICENSE.upstream")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
