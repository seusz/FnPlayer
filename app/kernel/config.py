#!/usr/bin/env python3
"""FnPlayer 桌面版运行配置（热加载 + 原子落盘）。

同时兼容上游 netutil.py 对 `from config import config` 的两个调用：
  config.get()["source_proxy"] / config.source_proxy_mode()

目录策略（便携优先）：
  1. FNPLAYER_DATA 环境变量（测试 / 显式覆盖，最高优先级）
  2. 便携模式：程序同目录存在 config/config.json 时，
     配置就是该文件，其余数据放同目录 data/ —— 整个目录可拷走
  3. 否则：%APPDATA%/FnPlayer/config.json

桌面版不预置任何资源站地址，sources 初始为空，由使用者在
config/config.json 或设置页自行填写。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
from pathlib import Path


def portable_root() -> Path | None:
    """便携版根目录（exe 所在目录 / 源码态工程根 / FNPLAYER_HOME 覆盖）。"""
    override = os.environ.get("FNPLAYER_HOME", "").strip()
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    dev = Path(__file__).resolve().parents[2]  # app/kernel/config.py -> 工程根
    return dev


def is_portable() -> bool:
    # 显式指定数据目录（测试 / 命令行覆盖）时，一律按独立实例处理
    if os.environ.get("FNPLAYER_DATA", "").strip():
        return False
    root = portable_root()
    return bool(root and (root / "config" / "config.json").is_file())


def state_file() -> Path:
    """用户可编辑的主配置文件。"""
    if os.environ.get("FNPLAYER_DATA", "").strip():
        return data_dir() / "config.json"
    root = portable_root()
    if root and (root / "config" / "config.json").is_file():
        return root / "config" / "config.json"
    return data_dir() / "config.json"


def data_dir() -> Path:
    override = os.environ.get("FNPLAYER_DATA", "").strip()
    if override:
        p = Path(override)
    elif is_portable():
        p = portable_root() / "data"
    else:
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        p = Path(base) / "FnPlayer" if base else Path.home() / ".fnplayer"
    p.mkdir(parents=True, exist_ok=True)
    return p


_LOCK = threading.RLock()

JUNK_KEYWORDS = [
    "电影解说", "影视解说", "解说", "预告片", "预告", "花絮", "抢先", "枪版",
    "AI漫剧", "漫剧", "动态漫画", "彩蛋", "删减", "片场", "开机", "特辑",
]

DEFAULTS = {
    "sources": [],                 # [{id, name, api, enabled}]
    "max_per_keyword": 24,
    "filter_junk": True,
    "junk_keywords": list(JUNK_KEYWORDS),
    "search_timeout": 9.0,
    # 网络：trust_env 恒为 False（不读系统代理，避免本机"代理开着但没进程"的坑）
    "source_proxy": "",            # 仅取源用，留空=直连
    "source_proxy_mode": "off",    # off / on / auto
    "auto_resume": True,
    "auto_next": True,
    "theme": "dark",               # dark / light
    "external_player": "",         # 可选：mpv.exe / vlc.exe 绝对路径
    # 播放失败时自动尝试下一条可用线路
    "auto_switch_line": True,
    # ---- 局域网访问（默认关闭：服务只监听 127.0.0.1）----
    # 打开后服务绑定 0.0.0.0，同网段设备可用浏览器访问；管理接口仍仅限本机。
    "lan_enabled": False,
    "lan_port": 18790,            # 固定端口，方便手机收藏 / 扫码
    "lan_password": "",           # 访问码，留空=不鉴权（仅限完全可信网络）
    # ---- 弹幕（服务地址留空 = 未启用；不预置任何第三方服务）----
    # 便携版：开关打开后由播放器拉起 workspace/danmu 里的本地 Node 服务
    "danmu_service_enabled": False,   # 本地弹幕服务总开关
    "danmu_service_port": 9321,       # 监听端口，只绑 127.0.0.1
    "danmu_service_token": "87654321",  # 与 workspace/danmu/config/.env 一致
    "danmu_api": "",               # 自建/自部署弹幕服务根地址，可含 /{TOKEN} 路径段
    "danmu_enabled": True,         # 播放页弹幕总开关
    "danmu_auto": True,            # 开播时自动匹配弹幕
    "danmu_opacity": 0.85,         # 0.2 ~ 1
    "danmu_font_size": 24,         # px，16 ~ 40
    "danmu_area": 0.75,            # 显示高度占画面比例，0.25 ~ 1
    "danmu_speed": 8.0,            # 滚动弹幕穿越整屏的秒数，4 ~ 16
    "danmu_offset": 0.0,           # 时间偏移（秒），正数=延后出现
    "danmu_bold": False,           # 粗体
}

_URL_RE = re.compile(r"^https?://\S+$", re.I)
_SEP_RE = re.compile(r"[\t|,，;；]+")


def parse_bulk_sources(text: str):
    """解析用户粘贴的接口清单。

    兼容：JSON 数组（字符串或 {name, api} 对象）、`名称,地址`、`名称<空格>地址`、
    `名称|地址`、以及每行一个纯地址。`#` / `//` 开头的行视为注释跳过。
    返回 (有效项 [(name, api)], 无法识别的明细)。
    """
    text = (text or "").strip().lstrip("\ufeff")
    if not text:
        return [], []

    pairs: list[tuple[str, str]] = []
    bad: list[dict] = []

    # 1) JSON 数组
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except Exception:
            data = None
        if isinstance(data, dict):
            data = data.get("sources") or data.get("items")
        if isinstance(data, list):
            for it in data:
                if isinstance(it, str):
                    api, name = it.strip(), ""
                elif isinstance(it, dict):
                    api = str(it.get("api") or it.get("url") or
                              it.get("api_url") or "").strip()
                    name = str(it.get("name") or it.get("title") or "").strip()
                else:
                    bad.append({"line": str(it)[:80], "reason": "不支持的条目类型"})
                    continue
                if _URL_RE.match(api):
                    pairs.append((name, api))
                else:
                    bad.append({"line": (name or api)[:80],
                                "reason": "地址需以 http(s):// 开头"})
            return _dedupe(pairs), bad

    # 2) 逐行文本
    for raw in text.splitlines():
        line = raw.strip().strip("\"'").strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        segs = [s.strip() for s in _SEP_RE.split(line) if s.strip()]
        if len(segs) == 1 and not _URL_RE.match(segs[0]):
            # 空格分隔：名称可能含空格，取唯一的 URL 词作为地址
            words = segs[0].split()
            idx = [i for i, w in enumerate(words) if _URL_RE.match(w)]
            if len(idx) == 1:
                api = words[idx[0]]
                name = " ".join(w for i, w in enumerate(words) if i != idx[0]).strip()
                segs = [name, api] if name else [api]
        if len(segs) == 1 and _URL_RE.match(segs[0]):
            pairs.append(("", segs[0]))
            continue
        urls = [s for s in segs if _URL_RE.match(s)]
        if not urls:
            bad.append({"line": line[:80], "reason": "未找到 http(s) 地址"})
            continue
        api = urls[0]
        name = next((s for s in segs if s is not api), "")
        pairs.append((name, api))

    return _dedupe(pairs), bad


def _dedupe(pairs):
    seen, out = set(), []
    for name, api in pairs:
        key = api.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((name, api))
    return out


def normalize_sources(items) -> list[dict]:
    """规范化 config/config.json 中手写的播放源。

    用户最少只需写 {"name": "...", "api": "https://..."}；
    id / enabled 自动补齐，非法地址与重复项跳过。
    """
    out, seen = [], set()
    for s in items or []:
        if not isinstance(s, dict):
            continue
        api = str(s.get("api") or "").strip()
        if not re.match(r"^https?://", api, re.I):
            continue
        key = api.rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        enabled = s.get("enabled", True) not in (
            False, 0, "false", "False", "0", "no", "off")
        name = str(s.get("name") or "").strip()
        if not name:
            name = api.split("/")[2] if "://" in api else "自定义接口"
        out.append({
            "id": str(s.get("id") or "").strip() or
                  "src_" + hashlib.md5(api.encode("utf-8")).hexdigest()[:10],
            "name": name[:40],
            "api": api,
            "enabled": enabled,
        })
    return out


class Config:
    def __init__(self):
        self._cfg: dict = {}
        self._mtime = 0.0
        self.load()

    def load(self):
        with _LOCK:
            f = state_file()
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
                if not isinstance(j, dict):
                    j = {}
            except Exception:
                j = {}
            cfg = json.loads(json.dumps(DEFAULTS))
            cfg.update({k: v for k, v in j.items() if k in DEFAULTS})
            # 允许用户在 config/config.json 里只写 name + api，其余字段自动补齐
            cfg["sources"] = normalize_sources(j.get("sources") or [])
            self._cfg = cfg
            try:
                self._mtime = f.stat().st_mtime
            except OSError:
                self._mtime = 0.0

    def maybe_reload(self):
        try:
            mt = state_file().stat().st_mtime
        except OSError:
            return
        if mt != self._mtime:
            self.load()

    def get(self) -> dict:
        self.maybe_reload()
        with _LOCK:
            return json.loads(json.dumps(self._cfg))

    def all_sources(self) -> list[dict]:
        return [dict(s) for s in self.get()["sources"]]

    def enabled_sources(self) -> list[dict]:
        return [dict(s) for s in self.get()["sources"] if s.get("enabled")]

    def source_proxy_mode(self) -> str:
        m = str(self.get().get("source_proxy_mode") or "off").strip().lower()
        return m if m in ("off", "on", "auto") else "off"

    def _persist(self, cfg: dict):
        f = state_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        # 保留用户写在 config.json 里的说明性键（以 _ 开头），
        # 避免从界面保存设置后把注释冲掉。
        notes = {}
        try:
            old = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                notes = {k: v for k, v in old.items() if k.startswith("_")}
        except Exception:
            pass
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({**notes, **cfg}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(f)
        self._cfg = cfg
        try:
            self._mtime = f.stat().st_mtime
        except OSError:
            self._mtime = 0.0

    def save(self, patch: dict) -> dict:
        with _LOCK:
            cfg = self.get()
            for k, v in patch.items():
                if k in DEFAULTS and k != "sources":
                    cfg[k] = v
            self._persist(cfg)
            return cfg

    @staticmethod
    def make_custom_id(api: str) -> str:
        return "src_" + hashlib.md5(api.strip().encode("utf-8")).hexdigest()[:10]

    def add_sources_bulk(self, text: str, enabled: bool = True):
        """批量导入接口。返回 (新增条数, 跳过明细, 全部接口列表)。"""
        pairs, bad = parse_bulk_sources(text)
        added, skipped = 0, list(bad)
        for name, api in pairs:
            ok, msg = self.add_source(name, api, enabled)
            if ok:
                added += 1
            else:
                skipped.append({"line": api, "reason": msg})
        return added, skipped, self.all_sources()

    def add_source(self, name: str, api: str, enabled: bool = True):
        api = (api or "").strip()
        name = (name or "").strip()
        if not re.match(r"^https?://", api, re.I):
            return False, "接口地址需以 http(s):// 开头"
        if not name:
            name = api.split("/")[2] if "://" in api else "自定义接口"
        with _LOCK:
            cfg = self.get()
            norm = api.rstrip("/").lower()
            if any(s.get("api", "").rstrip("/").lower() == norm for s in cfg["sources"]):
                return False, "该接口地址已存在"
            sid = self.make_custom_id(api)
            cfg["sources"].append(
                {"id": sid, "name": name[:40], "api": api, "enabled": bool(enabled)}
            )
            self._persist(cfg)
            return True, sid

    def update_source(self, sid: str, patch: dict):
        with _LOCK:
            cfg = self.get()
            target = next((s for s in cfg["sources"] if s.get("id") == sid), None)
            if not target:
                return False, "接口不存在"
            if "name" in patch:
                target["name"] = str(patch["name"]).strip()[:40] or target["name"]
            if "enabled" in patch:
                target["enabled"] = bool(patch["enabled"])
            if "api" in patch:
                api = str(patch["api"]).strip()
                if not re.match(r"^https?://", api, re.I):
                    return False, "接口地址需以 http(s):// 开头"
                target["api"] = api
            self._persist(cfg)
            return True, "ok"

    def remove_source(self, sid: str):
        with _LOCK:
            cfg = self.get()
            left = [s for s in cfg["sources"] if s.get("id") != sid]
            if len(left) == len(cfg["sources"]):
                return False, "接口不存在"
            cfg["sources"] = left
            self._persist(cfg)
            return True, "ok"

    def set_junk_keywords(self, raw) -> list[str]:
        parts = re.split(r"[,，、\n\r;；\s]+", raw) if isinstance(raw, str) else [
            str(x) for x in (raw or [])
        ]
        out, seen = [], set()
        for p in parts:
            k = p.strip()
            if k and k not in seen and len(k) <= 30:
                seen.add(k)
                out.append(k)
        self.save({"junk_keywords": out})
        return out

    def reset_junk_keywords(self) -> list[str]:
        self.save({"junk_keywords": list(JUNK_KEYWORDS)})
        return list(JUNK_KEYWORDS)

    def is_junk(self, title: str, remarks: str = "") -> bool:
        cfg = self.get()
        if not cfg.get("filter_junk", True):
            return False
        hay = f"{title} {remarks}"
        return any(k and k in hay for k in cfg.get("junk_keywords", JUNK_KEYWORDS))


config = Config()
