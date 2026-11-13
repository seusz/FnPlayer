#!/usr/bin/env python3
"""搜索结果聚合、线路注册表与播放令牌。

上游 providers.search_all 返回每台接口各自的 OnlineTitle；这里把「不同接口
命中的同一部影片」按规范化片名 + 年份合并成一张卡片，每个接口/播放分组成为
一条可切换线路，与上游 NAS 版的多线路设计保持一致。
"""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from dataclasses import dataclass, field


def norm_title(name: str) -> str:
    s = str(name or "").strip()
    s = re.sub(r"【[^】]*】|\[[^\]]*\]|（[^）]*）|\([^)]*\)", "", s)
    s = re.sub(
        r"(第[一二三四五六七八九十百\d]+季|全集|完结|更新至\d+集?|"
        r"HD|4K|8K|1080P|720P|蓝光|国语|粤语|中字|超清|抢先版).*$",
        "", s, flags=re.I,
    )
    s = re.sub(r"[\s:：·\-—_,，.!！?？'\"“”‘’/\\|]+", "", s)
    return s.lower()


def clean_year(year: str, remarks: str = "") -> str:
    m = re.search(r"(19|20)\d{2}", str(year or ""))
    if not m:
        m = re.search(r"(19|20)\d{2}", str(remarks or ""))
    return m.group(0) if m else ""


@dataclass
class Title:
    id: str
    title: str
    year: str = ""
    area: str = ""
    type_name: str = ""
    poster: str = ""
    overview: str = ""
    actor: str = ""
    director: str = ""
    remarks: str = ""
    score: str = ""
    lines: dict[str, list[dict]] = field(default_factory=dict)
    providers: list[str] = field(default_factory=list)

    def brief(self) -> dict:
        eps = sum(len(v) for v in self.lines.values())
        return {
            "id": self.id,
            "title": self.title,
            "year": self.year,
            "area": self.area,
            "type_name": self.type_name,
            "poster": self.poster,
            "remarks": self.remarks,
            "score": self.score,
            "line_count": len(self.lines),
            "episode_count": eps,
        }

    def full(self) -> dict:
        d = self.brief()
        d.update({
            "overview": self.overview,
            "actor": self.actor,
            "director": self.director,
            "providers": self.providers,
            "lines": {
                name: [
                    {"name": e["name"], "index": i, "kind": e.get("kind", "hls")}
                    for i, e in enumerate(eps)
                ]
                for name, eps in self.lines.items()
            },
        })
        return d


class TitleRegistry:
    """内存影片库：搜索结果按会话保留，重启后由播放记录重新可找回。"""

    def __init__(self, max_items: int = 1200):
        self.max_items = max_items
        self._items: dict[str, Title] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()

    @staticmethod
    def make_id(provider: str, vod_id: str) -> str:
        return hashlib.md5(f"{provider}:{vod_id}".encode()).hexdigest()[:16]

    def put(self, t: Title):
        with self._lock:
            if t.id not in self._items:
                self._order.append(t.id)
            self._items[t.id] = t
            while len(self._order) > self.max_items:
                old = self._order.pop(0)
                self._items.pop(old, None)

    def get(self, tid: str) -> Title | None:
        with self._lock:
            return self._items.get(tid)

    def all(self) -> list[Title]:
        with self._lock:
            return [self._items[k] for k in reversed(self._order) if k in self._items]


@dataclass
class StreamCtx:
    token: str
    url: str                      # 最终媒体/清单地址
    headers: dict                 # Referer / UA 等
    kind: str                     # hls | file
    title_id: str = ""
    title_name: str = ""
    line: str = ""
    episode: str = ""
    seg_urls: list[str] = field(default_factory=list)
    key_urls: list[str] = field(default_factory=list)
    sub_urls: list[str] = field(default_factory=list)
    key_cache: dict[str, bytes] = field(default_factory=dict)
    created: float = field(default_factory=time.time)


class StreamRegistry:
    TTL = 6 * 3600.0

    def __init__(self):
        self._items: dict[str, StreamCtx] = {}
        self._lock = threading.RLock()

    def create(self, **kw) -> StreamCtx:
        with self._lock:
            self.gc()
            token = secrets.token_urlsafe(12)
            ctx = StreamCtx(token=token, **kw)
            self._items[token] = ctx
            return ctx

    def get(self, token: str) -> StreamCtx | None:
        with self._lock:
            ctx = self._items.get(token)
            if ctx and time.time() - ctx.created > self.TTL:
                self._items.pop(token, None)
                return None
            return ctx

    def gc(self):
        now = time.time()
        for k in [k for k, v in self._items.items() if now - v.created > self.TTL]:
            self._items.pop(k, None)
        if len(self._items) > 500:
            for k in sorted(self._items, key=lambda x: self._items[x].created)[:200]:
                self._items.pop(k, None)


def merge_titles(raw_titles: list, source_names: dict, is_junk) -> list[Title]:
    """跨接口聚合：同名同年份合并为一张卡片，接口×播放分组为线路。"""
    grouped: dict[str, Title] = {}
    order: list[str] = []
    for t in raw_titles:
        name = (t.title or "").strip()
        if not name or is_junk(name, t.remarks):
            continue
        year = clean_year(t.year, t.remarks)
        key = norm_title(name) + "|" + year
        card = grouped.get(key)
        if card is None:
            tid = TitleRegistry.make_id(t.provider, t.vod_id)
            card = Title(
                id=tid, title=name, year=year, area=t.area or "",
                type_name=t.type_name or "", poster=t.poster or "",
                overview=re.sub(r"<[^>]+>", "", t.overview or "").strip(),
                actor=t.actor or "", director=t.director or "",
                remarks=t.remarks or "", score=t.score or "",
            )
            grouped[key] = card
            order.append(key)
        pname = source_names.get(t.provider, t.provider)
        if pname not in card.providers:
            card.providers.append(pname)
        for line_name, episodes in (t.sources or {}).items():
            label = f"{pname} · {line_name}"
            eps = []
            for e in episodes:
                url = (e.url or "").strip()
                if not url.startswith(("http://", "https://")):
                    continue
                is_hls = bool(re.search(r"\.m3u8(\?|$)", url, re.I))
                eps.append({
                    "name": e.title or f"第{len(eps)+1}集",
                    "url": url,
                    "direct": bool(e.direct),
                    "resolve": bool(e.resolve),
                    "redirect": bool(e.redirect),
                    "headers": dict(e.headers or {}),
                    "kind": "hls" if is_hls and not e.resolve else "file",
                    "provider": t.provider,
                })
            if eps:
                existing = card.lines.setdefault(label, [])
                existing.extend(eps)
    return [grouped[k] for k in order if grouped[k].lines]
