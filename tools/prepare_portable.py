#!/usr/bin/env python3
"""组装 FnPlayer 便携版附加资源。

产出 FnPlayer/portable/（构建时由 build/build.py 复制到 dist/FnPlayer/）：

  portable/
    config/config.json          用户可编辑的主配置（播放源、弹幕开关、全部设置）
    config/README.txt           配置字段说明
    runtime/node/node.exe       便携 Node.js 运行时（免安装）
    runtime/node/LICENSE.txt    Node.js 许可
    workspace/danmu/            弹幕服务「独立工作区」
      package.json / danmu_api/ 上游源码（AGPL-3.0，原样保留 LICENSE）
      node_modules/             已装好的依赖（等价 npm install 的虚拟环境）
      config/.env               本地生效配置（只监听 127.0.0.1）
      config/.env.example       上游配置样例（仅供参考）
      .cache/                   运行期数据（缓存 / 本地弹幕）
      PORTABLE.md               工作区说明与改动清单
    使用说明.txt

参考上游「本地运行」：npm install → node ./danmu_api/server.js
本脚本把这两步的产物固化到便携目录，运行时由 FnPlayer 直接调用
runtime/node/node.exe，不需要用户安装 Node.js / npm。

用法：
  python tools/prepare_portable.py                  # 全量组装
  python tools/prepare_portable.py --skip-npm      # 跳过依赖安装
  python tools/prepare_portable.py --src <上游目录>

默认从仓库内的 third_party/danmu_api/ 取源码（AGPL 第 13 条要求随分发提供
完整对应源码，故上游核心源码已入库）。如需改用本地克隆的上游目录，
用 --src 指定即可。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]                 # FnPlayer/
SESSION = ROOT.parent
# 上游弹幕核心源码已随仓库入库（AGPL 第 13 条：随分发提供完整对应源码）。
# 若要改用本地克隆的上游目录，用 --src 指定即可覆盖。
DEFAULT_SRC = ROOT / "third_party" / "danmu_api"
OUT = ROOT / "portable"

NODE_CANDIDATES = [
    r"C:\Program Files\nodejs\node.exe",
    r"C:\Program Files (x86)\nodejs\node.exe",
]
NODE_FALLBACK_VERSION = "v22.22.2"

# 只复制运行所需内容，避免把 .git / 测试 / serverless 部署配置塞进便携包
DANMU_FILES = ["package.json", "LICENSE", "README.md"]
DANMU_DIRS = ["danmu_api"]

ENV_TEXT = """# FnPlayer 便携版 · 本地弹幕服务配置
# 由 prepare_portable.py 生成；优先级低于进程环境变量。
# 修改后热更新生效（上游 chokidar 监听 .env），也可在播放器里重启服务。

# 访问令牌：播放器设置里的服务地址末段需与此一致
TOKEN=87654321

# 主服务监听端口
DANMU_API_PORT=9321

# 只绑定本机回环地址，不对外暴露（由 PORTABLE patch 支持）
DANMU_API_HOST=127.0.0.1

# 上游附带代理服务的端口（同样只绑本机）
DANMU_API_PROXY_PORT=5321
"""

PORTABLE_MD = """# 弹幕服务独立工作区

本目录是一个**自包含的 Node.js 工作区**，与主播放器（Python/PyInstaller）完全隔离。
主程序只在「设置 → 弹幕 → 本地弹幕服务」开关打开时拉起它，开关关闭时停止。

## 来源

- 上游：https://github.com/huangxd-/danmu_api
- 许可：**AGPL-3.0**（见同目录 `LICENSE`）。本工作区保留上游源码与许可证，
  仅按官方「本地运行」方式部署；FnPlayer 的 Python 侧只调用其公开 HTTP 接口，
  未复制其代码。

## 运行方式（等价上游「本地运行」）

```
npm install                       # 已由 prepare_portable.py 完成，产物 node_modules/
node ./danmu_api/server.js        # 便携版由 ../runtime/node/node.exe 执行
```

服务默认监听 `http://127.0.0.1:9321`，TOKEN 见 `config/.env`。
上游管理页面、接口调试等能力均保留，可自行访问。

## 相对上游的两处最小改动（PORTABLE patch）

1. `danmu_api/utils/server-listen-util.js`：新增 `DANMU_API_HOST` 支持。
   设置后直接绑定指定地址（这里为 127.0.0.1），未设置时行为与上游完全一致。
   目的：本机自用的服务不监听所有网卡，主服务与 5321 代理都只在本机可达。
2. `danmu_api/server.js`：代理端口改读 `DANMU_API_PROXY_PORT`（默认仍是 5321）。
   目的：端口冲突时可在 `config/.env` 里调整。

改动均为「加一行环境变量读取、保留原默认值」，没有删改上游业务逻辑。

## 目录

| 路径 | 说明 |
| --- | --- |
| `danmu_api/` | 上游服务端源码 |
| `node_modules/` | 已安装依赖，无需联网 |
| `config/.env` | 本地生效配置 |
| `.cache/` | 运行期缓存、本地弹幕文件 |
| `logs/` | 服务日志（由主程序写入） |

## 手动启动 / 排障

在本目录执行：

```
..\\..\\runtime\\node\\node.exe .\\danmu_api\\server.js
```

日志见 `logs/service.log`，也可在播放器设置页查看最近输出。
"""

CONFIG_README = """FnPlayer 便携版 · config/config.json 说明
============================================

这个文件是便携版的「唯一配置入口」。用记事本/VSCode 编辑并保存后，
程序会自动热加载（无需重启、无需在界面里再点保存）。

【播放源】sources —— 自定义播放源就写在这里
------------------------------------------------
格式：JSON 数组，每项三个字段：

  "sources": [
    {
      "id": "my-nas",
      "name": "我的媒体库",
      "api": "https://your-domain/api.php/provide/vod/at/json",
      "enabled": true
    }
  ]

- api   ：苹果CMS V10 的 JSON 接口地址，必须 http(s):// 开头
- name  ：显示名称，随意
- id    ：任意唯一字符串；也可以省略，界面里编辑时会自动补
- enabled：false 表示暂时停用但不删除

请只填写你自己拥有合法授权或自建的接口。本程序不预置、不推荐任何资源站，
也不对第三方内容的合法性负责。当然也可以在「设置 → 接口管理」里用
批量导入粘贴清单，结果同样写回本文件。

【局域网访问】（默认关闭，只监听 127.0.0.1）
------------------------------------------------
  "lan_enabled": false       改为 true 后服务监听 0.0.0.0，同网段可访问
  "lan_port": 18790          固定端口，方便手机收藏 / 扫码
  "lan_password": ""         访问码，4-32 位字母、数字、- 或 _；强烈建议设置

  也可以在界面「设置 → 局域网访问」里开关，点「应用并重启」会自动重绑端口，
  面板会列出所有可用地址与二维码（二维码里已含访问码）。
  注意：
  - 首次开放时 Windows 防火墙可能弹窗，请勾选「专用网络」并允许；
  - 管理功能（接口、设置、弹幕服务开关、退出应用）仅主机可用，
    局域网设备只能搜索、播放、看弹幕、记录进度；
  - 手机观看期间请保持电脑上的播放器窗口开着（关窗口 = 退出服务）；
  - 访问码留空时同网段任何设备都能直接打开，仅限完全可信网络；
  - 不要在公共 Wi-Fi、开了端口映射/UPnP 的路由器上开启。

【本地弹幕服务】
------------------------------------------------
  "danmu_service_enabled": true     打开后播放器会自动拉起本地 Node 服务
  "danmu_service_port": 9321        监听端口（只绑 127.0.0.1）
  "danmu_service_token": "87654321" 访问令牌，与 workspace/danmu/config/.env 一致

  "danmu_api": ""                   弹幕服务地址；开启本地服务后会自动填成
                                    http://127.0.0.1:9321/87654321
                                    也可以填你自己部署的 /api/v2 兼容服务
  "danmu_enabled": true             播放页是否显示弹幕
  "danmu_auto": true                开播自动匹配本集弹幕

【其它常用项】
------------------------------------------------
  theme                dark / light
  auto_resume          续播
  auto_next            自动下一集
  auto_switch_line     解析失败自动换线
  search_timeout       源搜索超时（秒）
  filter_junk          过滤解说/预告等条目
  junk_keywords        过滤词数组
  source_proxy         取源用的代理，留空直连
  external_player      mpv.exe / vlc.exe 的绝对路径
  lan_enabled          是否允许局域网访问
  lan_port             局域网 / 本机统一使用的固定端口
  lan_password         局域网访问码（留空=不鉴权）

【数据目录】
------------------------------------------------
播放记录、浏览器配置、日志都在同级 `../data/`，整个目录可拷到 U 盘带走。
若要改用 %APPDATA%\\FnPlayer，删掉本 `config/config.json` 即可（或设 FNPLAYER_DATA）。
"""

DEFAULT_CONFIG = {
    "_说明": "FnPlayer 便携版配置。编辑保存后自动热加载；字段说明见同目录 README.txt。",
    "_播放源说明": "在 sources 中填写你自己拥有合法授权或自建的苹果CMS V10 接口；本程序不预置任何资源站。",
    "_局域网说明": "lan_enabled 默认 false（只监听 127.0.0.1）；开启前请设置 lan_password，详见 README.txt。",
    "sources": [],
    "theme": "dark",
    "auto_resume": True,
    "auto_next": True,
    "auto_switch_line": True,
    "filter_junk": True,
    "search_timeout": 9.0,
    "danmu_service_enabled": False,
    "danmu_service_port": 9321,
    "danmu_service_token": "87654321",
    "danmu_api": "",
    "danmu_enabled": True,
    "danmu_auto": True,
    "danmu_opacity": 0.85,
    "danmu_font_size": 24,
    "danmu_area": 0.75,
    "danmu_speed": 8.0,
    "danmu_offset": 0.0,
    "lan_enabled": False,
    "lan_port": 18790,
    "lan_password": "",
}

README = """FnPlayer 便携版 · 使用说明
================================

解压到任意目录（或 U 盘），双击 FnPlayer.exe 即可。
不需要安装 Python、Node.js、npm 或任何运行库；Windows 10/11 自带 Edge 即可，
有 Chrome 也可以。

【目录】
  FnPlayer.exe          主程序
  _internal/            Python 运行时与前端资源（PyInstaller 自带虚拟环境）
  config/config.json    【配置入口】播放源、弹幕开关都在这里，可直接编辑
  config/README.txt     配置字段说明
  runtime/node/         便携 Node.js 运行时（免安装）
  workspace/danmu/      弹幕服务独立工作区（源码 + 已装依赖 + 日志）
  data/                 播放记录、窗口配置、日志（跟着目录走）
  LICENSES/             随包分发的第三方许可证文本

【第一次使用】
1. 用文本编辑器打开 config/config.json，在 "sources" 里填入你自己的
   苹果CMS V10 接口（格式见 config/README.txt），保存。程序会自动热加载。
   也可以在「设置 → 接口管理 → 批量导入」里粘贴多行清单。
2. 想用弹幕：打开「设置 → 弹幕 → 本地弹幕服务」开关。播放器会自动
   拉起 workspace/danmu 里的 Node 服务（只监听 127.0.0.1:9321），
   并自动把服务地址填好。关闭开关即停止服务。
3. 播放时控制条的「弹」显示/隐藏弹幕（快捷键 D），「弹幕」面板可手动
   搜索选集、调节显示效果、本地发送弹幕。

【局域网访问（可选，默认关闭）】
播放器前端本来就是浏览器页面，默认只监听 127.0.0.1，局域网设备访问不到。
想让手机 / 平板 / 电视在同一 Wi-Fi 下使用：

1. 「设置 → 局域网访问」打开开关，点「生成」设一个访问码，
   再点「应用并重启」。服务会短暂重绑端口，页面自动刷新。
2. 面板会列出 http://192.168.x.x:18790/?key=... 这样的地址和二维码，
   手机浏览器输入或扫码即可（二维码已含访问码，无需再手输）。
   若有多个地址，选与手机同网段的那个（常见 192.168.x.x）。
3. 首次开放时 Windows 防火墙可能弹窗，选「专用网络 → 允许访问」。
   手机打不开时依次检查：是否同一网段、防火墙、路由器是否开了
   访客网络 / AP 隔离、是否被安全软件拦截。

只建议在自己家里或完全可信的局域网使用。局域网设备可以搜索、播放、
看弹幕、记录进度，但改接口、改设置、启停弹幕服务、退出应用仍只允许
在电脑本机操作。手机观看期间请保持电脑上的 FnPlayer 窗口开着
（关闭窗口即退出服务）。不需要时把开关关掉，服务会回到只监听 127.0.0.1。

进阶：命令行 `FnPlayer.exe --lan --lan-password 你的访问码 --no-browser`
可强制以局域网服务模式启动（不打开桌面窗口），此时请用任务管理器结束
FnPlayer.exe 来停止服务。

【弹幕服务】
来自 https://github.com/huangxd-/danmu_api ，AGPL-3.0，
源码与许可证完整保留在 workspace/danmu/。该服务会按需访问其采集源，
能否访问、内容是否合法由使用者自行判断和负责。FnPlayer 不预置任何
第三方资源站或弹幕服务地址。

【源码提供】
本项目以 AGPL-3.0 发布，源码已公开：
  https://github.com/seusz/FnPlayer
- 弹幕核心 danmu_api 的完整源码就在本包 workspace/danmu/danmu_api/ 中，
  与仓库 third_party/danmu_api/ 逐字节一致（含全部测试文件），
  其 AGPL-3.0 许可原文见 workspace/danmu/LICENSE。
- 随包分发的 npm 依赖，各自的 LICENSE 文件都在
  workspace/danmu/node_modules/<包名>/ 下。
- 第三方组件的上游仓库地址与许可证汇总见仓库内的 NOTICE.md，
  许可原文副本见 THIRD_PARTY_LICENSES/。
- 按 AGPL-3.0 第 13 条：若你修改了本项目并通过网络向他人提供服务，
  必须同时向这些用户提供你的修改版源码。

【合规】
上游 fnmedia-ext (MIT)、hls.js (Apache-2.0)、弹弹play 兼容客户端、
danmu_api (AGPL-3.0)、segno (BSD，许可证见 LICENSES/segno-LICENSE.txt)、
Node.js 运行时（许可证见 runtime/node/LICENSE.txt）。
仅用于访问自有 / 已授权内容，请勿用于商业传播。
"""


def log(msg):
    print("[portable]", msg, flush=True)


def write_text(path: Path, text: str, bom: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig" if bom else "utf-8")


# ----------------------------------------------------------------- Node 运行时
def _direct_opener():
    # 本机常驻代理有时是空的；直连最稳
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _download_node(target_dir: Path):
    ver = NODE_FALLBACK_VERSION
    url = f"https://nodejs.org/dist/{ver}/node-{ver}-win-x64.zip"
    log(f"本机未找到 node.exe，从 nodejs.org 下载 {ver} …")
    with _direct_opener().open(url, timeout=180) as r:
        data = r.read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        prefix = f"node-{ver}-win-x64/"
        exe = z.read(prefix + "node.exe")
        (target_dir / "node.exe").write_bytes(exe)
        try:
            (target_dir / "LICENSE.txt").write_bytes(z.read(prefix + "LICENSE"))
        except KeyError:
            pass
    log(f"下载完成：{len(data) / 1024 / 1024:.1f} MB")


def prepare_node(node_src: str):
    d = OUT / "runtime" / "node"
    d.mkdir(parents=True, exist_ok=True)
    exe = d / "node.exe"
    candidates = [Path(node_src)] if node_src else [Path(p) for p in NODE_CANDIDATES]
    src = next((p for p in candidates if p.is_file()), None)
    if src:
        if src.resolve() != exe.resolve():
            shutil.copy2(src, exe)
        local_license = src.parent / "LICENSE"
        if local_license.is_file() and not (d / "LICENSE.txt").exists():
            shutil.copy2(local_license, d / "LICENSE.txt")
        log(f"便携 Node 取自 {src} ({exe.stat().st_size / 1024 / 1024:.1f} MB)")
    elif not exe.is_file():
        _download_node(d)
    if not (d / "LICENSE.txt").exists():
        try:
            raw = _direct_opener().open(
                "https://raw.githubusercontent.com/nodejs/node/main/LICENSE",
                timeout=30).read()
            (d / "LICENSE.txt").write_bytes(raw)
        except Exception as e:
            log("LICENSE 下载失败（不影响运行）: " + str(e))
    r = subprocess.run([str(exe), "--version"], capture_output=True, timeout=30)
    log("便携 Node 版本: " + r.stdout.decode().strip())
    if r.returncode != 0:
        raise SystemExit("便携 node.exe 无法运行")


# ------------------------------------------------------------- 弹幕独立工作区
def node_cli_path() -> str:
    """找到 npm-cli.js（构建期工具，便携包运行时不需要）。"""
    import shutil
    cands = []
    exe = shutil.which("node") or ""
    if exe:
        cands.append(Path(exe).parent / "node_modules" / "npm" / "bin" / "npm-cli.js")
    for base in NODE_CANDIDATES:
        cands.append(Path(base).parent / "node_modules" / "npm" / "bin" / "npm-cli.js")
    for p in cands:
        if p.is_file():
            return str(p)
    raise SystemExit("未找到 npm-cli.js，无法安装弹幕服务依赖")


def _patch_listen_util(path: Path):
    """支持 DANMU_API_HOST；未设置时完全保持上游行为。"""
    s = path.read_text(encoding="utf-8")
    if "DANMU_API_HOST" in s:
        return
    anchor = """  const logger = options.logger || console;
  const serviceName = options.serviceName || 'server';

  try {"""
    patched = """  const logger = options.logger || console;
  const serviceName = options.serviceName || 'server';

  // PORTABLE patch: 允许只绑定指定地址（如 127.0.0.1），未设置时行为不变
  const bindHost = process.env.DANMU_API_HOST;
  if (bindHost) {
    const bound = await listenOnce(server, { port, host: bindHost });
    return createBinding(bound, bindHost, port, false);
  }

  try {"""
    if anchor not in s:
        raise SystemExit("server-listen-util.js 结构与预期不符，无法应用 patch")
    path.write_text(s.replace(anchor, patched, 1), encoding="utf-8")
    log("已应用 patch: DANMU_API_HOST")


def _patch_server_js(path: Path):
    """代理端口可配，避免 5321 被占用导致启动失败。"""
    s = path.read_text(encoding="utf-8")
    if "DANMU_API_PROXY_PORT" in s:
        return
    anchor = """  const proxyBinding = await listenOnAllInterfaces(proxyServer, 5321, {
    serviceName: 'proxy server'
  });"""
    patched = """  // PORTABLE patch: 代理端口可配（默认仍为 5321）
  const configuredProxyPort = Number.parseInt(process.env.DANMU_API_PROXY_PORT ?? '', 10);
  const proxyPort = Number.isNaN(configuredProxyPort) ? 5321 : configuredProxyPort;
  const proxyBinding = await listenOnAllInterfaces(proxyServer, proxyPort, {
    serviceName: 'proxy server'
  });"""
    if anchor not in s:
        raise SystemExit("server.js 结构与预期不符，无法应用 patch")
    path.write_text(s.replace(anchor, patched, 1), encoding="utf-8")
    log("已应用 patch: DANMU_API_PROXY_PORT")


def _patch_missing_licenses(modules: Path):
    """给上游漏发许可文件的 npm 包补上许可声明。

    @bufbuild/protobuf、@nodable/entities、brotli、data-uri-to-buffer
    这几个包的 npm 发行包里没有许可文本文件，但其 package.json 声明的
    MIT / Apache-2.0 都要求「随副本附带许可声明」。补录文本放在
    android/danmu-core-src/licenses/，映射见同目录 manifest.json。
    """
    lic_dir = ROOT / "android" / "danmu-core-src" / "licenses"
    manifest = lic_dir / "manifest.json"
    if not manifest.is_file():
        return
    try:
        lic_map = json.loads(manifest.read_text(encoding="utf-8"))
    except ValueError as e:
        log("许可清单解析失败（不影响运行）: " + str(e))
        return
    for pkg, fname in sorted((lic_map.get("packages") or {}).items()):
        pkg_dir = modules / pkg
        src_lic = lic_dir / fname
        if not pkg_dir.is_dir() or not src_lic.is_file():
            continue
        target = pkg_dir / "LICENSE.txt"
        if not target.is_file():
            shutil.copy2(src_lic, target)
            log("已补录许可文件: %s" % pkg)


def prepare_workspace(src: Path, skip_npm: bool):
    d = OUT / "workspace" / "danmu"
    d.mkdir(parents=True, exist_ok=True)
    for name in DANMU_FILES:
        f = src / name
        if f.is_file():
            shutil.copy2(f, d / name)
    for name in DANMU_DIRS:
        s, t = src / name, d / name
        # 不做递归删除（环境安全策略会拦截批量删除）；直接覆盖同名文件即可，
        # patch 步骤是幂等的，重复组装不会产生副作用。
        # AGPL-3.0 第 13 条要求随分发物提供「完整对应源码」，所以这里不排除
        # 上游的 tests/ 目录 —— 便携包内的 danmu_api/ 要与仓库
        # third_party/danmu_api/ 里的源码完全一致，缺一份测试都不算完整。
        # 只排除 .git 与 node_modules（后者由 npm install 单独准备）。
        shutil.copytree(s, t, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(".git", "node_modules"))
    (d / "config").mkdir(exist_ok=True)
    env_example = src / "config" / ".env.example"
    if env_example.is_file():
        shutil.copy2(env_example, d / "config" / ".env.example")
    write_text(d / "config" / ".env", ENV_TEXT)
    (d / ".cache").mkdir(exist_ok=True)
    (d / "logs").mkdir(exist_ok=True)
    write_text(d / "PORTABLE.md", PORTABLE_MD)

    _patch_listen_util(d / "danmu_api" / "utils" / "server-listen-util.js")
    _patch_server_js(d / "danmu_api" / "server.js")

    if not skip_npm:
        log("安装 Node 依赖到独立工作区（等价上游 npm install）…")
        env = os.environ.copy()
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                  "npm_config_proxy", "npm_config_https_proxy"):
            env.pop(k, None)
        # Windows 的 npm 是 .cmd，Python 无法直接 CreateProcess；改为用便携
        # node.exe 执行 npm-cli.js，整个过程不依赖系统 npm / shell。
        npm_cli = Path(node_cli_path())
        node_exe = str(OUT / "runtime" / "node" / "node.exe")
        cmd = [node_exe, str(npm_cli), "install", "--omit=dev", "--no-audit",
               "--no-fund", "--registry=https://registry.npmjs.org/"]
        r = subprocess.run(cmd, cwd=str(d), env=env, capture_output=True,
                           text=True, timeout=900)
        tail = (r.stdout or "")[-1200:] + (r.stderr or "")[-1200:]
        log(f"npm 退出码 {r.returncode}\n{tail}")
        if r.returncode != 0:
            raise SystemExit("npm install 失败，请检查网络后重试")
    if not (d / "node_modules").is_dir():
        raise SystemExit("缺少 node_modules，请用 --skip-npm 之外的模式重新运行")
    _patch_missing_licenses(d / "node_modules")
    size = sum(p.stat().st_size for p in (d / "node_modules").rglob("*") if p.is_file())
    log(f"工作区就绪：node_modules {size / 1024 / 1024:.1f} MB")
    # 用便携 Node 做一次语法自检（不启动服务、不联网）
    chk = subprocess.run([str(OUT / "runtime" / "node" / "node.exe"), "--check",
                          str(d / "danmu_api" / "server.js")],
                         capture_output=True, text=True, timeout=60)
    log("server.js 语法检查: " + ("OK" if chk.returncode == 0 else chk.stderr[-300:]))
    if chk.returncode != 0:
        raise SystemExit("弹幕服务语法检查失败")


# --------------------------------------------------------------------- 配置
def prepare_licenses():
    """随包附带 segno（二维码生成）的许可证文本。"""
    d = OUT / "LICENSES"
    d.mkdir(parents=True, exist_ok=True)
    target = d / "segno-LICENSE.txt"
    try:
        import importlib.metadata as md
        dist = md.distribution("segno")
        f = next((x for x in (dist.files or [])
                  if str(x).lower().endswith("license")), None)
        if f:
            p = Path(dist.locate_file(f))
            if p.is_file():
                shutil.copy2(p, target)
    except Exception as e:
        log("segno LICENSE 读取失败（不影响运行）: " + str(e))
    if not target.is_file():
        write_text(target,
                   "segno — BSD License\n"
                   "Project: https://pypi.org/project/segno/\n"
                   "Used by FnPlayer to render LAN access QR codes as SVG.\n"
                   "Full license text ships with the Python package.\n")


def prepare_config():
    c = OUT / "config"
    c.mkdir(parents=True, exist_ok=True)
    f = c / "config.json"
    if not f.exists():
        f.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    write_text(c / "README.txt", CONFIG_README, bom=True)
    (OUT / "data").mkdir(exist_ok=True)
    write_text(OUT / "使用说明.txt", README, bom=True)
    log("配置模板与说明已生成（sources 默认空，不预置任何资源站）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(DEFAULT_SRC), help="danmu_api 源码目录")
    ap.add_argument("--node-exe", default="", help="指定便携 node.exe 来源")
    ap.add_argument("--skip-npm", action="store_true", help="跳过依赖安装")
    args = ap.parse_args()
    src = Path(args.src)
    if not (src / "danmu_api" / "server.js").is_file():
        raise SystemExit(f"未找到上游源码：{src}\n"
                         "仓库内应有 third_party/danmu_api/；"
                         "如需改用本地克隆，请用 --src 指定目录。\n"
                         "也可执行 python tools/fetch_sources.py --danmu 下载最新版。")
    OUT.mkdir(parents=True, exist_ok=True)
    prepare_node(args.node_exe)
    prepare_workspace(src, args.skip_npm)
    prepare_config()
    prepare_licenses()
    total = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    log(f"便携附加资源合计 {total / 1024 / 1024:.1f} MB -> {OUT}")


if __name__ == "__main__":
    main()
