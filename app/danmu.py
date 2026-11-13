#!/usr/bin/env python3
"""弹幕服务代理：兼容 LogVar / 弹弹play 的 /api/v2 接口规范。

设计边界：
- 本模块只做「客户端」，不内置、不预置任何第三方弹幕服务地址。
- 服务地址（含 TOKEN 路径段）由使用者在设置中自行填写，保存在本机配置里。
- 浏览器只与本机 127.0.0.1 通信，由本模块转发，避免跨域并避免前端暴露调用细节。
- 不复制上游 danmu_api（AGPL-3.0）的任何代码，仅按其公开 HTTP 契约对接。

上游契约（huangxd-/danmu_api，兼容弹弹play）：
  GET  /api/v2/search/anime?keyword=xxx       -> {errorCode, success, animes: [...]}
  POST /api/v2/match       {fileName: "..."}  -> {isMatched, matches: [{episodeId, ...}]}
  GET  /api/v2/bangumi/:animeId               -> {bangumi: {episodes: [{episodeId, ...}]}}
  GET  /api/v2/comment/:commentId?format=json -> {count, comments: [{cid, p, m}], videoDuration}
  弹幕 p 字段：时间,类型,颜色,平台（或 B 站 8 字段，前 3 位一致）
"""
from __future__ import annotations

import re
import urllib.parse

import httpx

TIMEOUT = 18.0
_DEFAULT_COLOR = 16777215
_V2_SUFFIX = re.compile(r"/api/v2/?$", re.I)


class DanmuError(Exception):
    """弹幕服务调用失败（供路由层转成友好提示）。"""


def normalize_base(value: str) -> str:
    """校验并规范化服务地址，返回 '' 表示未配置。"""
    v = (value or "").strip().rstrip("/")
    if not v:
        return ""
    if not re.match(r"^https?://", v, re.I):
        raise DanmuError("地址需以 http:// 或 https:// 开头")
    parts = urllib.parse.urlsplit(v)
    if not parts.netloc:
        raise DanmuError("地址缺少主机名")
    # 允许示例：http://127.0.0.1:9321、http://127.0.0.1:9321/自定义TOKEN、
    #           https://xxx.vercel.app/自定义TOKEN/api/v2
    v = _V2_SUFFIX.sub("", v).rstrip("/")
    return v


def endpoint(base: str, tail: str) -> str:
    """拼出 /api/v2/... 完整地址；base 可含 TOKEN 路径段。"""
    b = normalize_base(base)
    if not b:
        raise DanmuError("尚未配置弹幕服务地址")
    return f"{b}/api/v2/{tail.lstrip('/')}"


async def _request(client: httpx.AsyncClient, method: str, url: str,
                   *, params: dict | None = None, json_body: dict | None = None):
    try:
        r = await client.request(method, url, params=params, json=json_body,
                                 timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise DanmuError(f"连接弹幕服务失败：{type(e).__name__}") from e
    if r.status_code >= 400:
        hint = ""
        try:
            j = r.json()
            hint = j.get("errorMessage") or j.get("message") or ""
        except Exception:
            pass
        if r.status_code == 401:
            hint = hint or "TOKEN 不正确，请在地址中带上 /{TOKEN}"
        raise DanmuError(f"弹幕服务返回 HTTP {r.status_code}" + (f"：{hint}" if hint else ""))
    try:
        return r.json()
    except Exception as e:
        raise DanmuError("弹幕服务返回的不是合法 JSON") from e


def _check(data: dict):
    if not isinstance(data, dict):
        raise DanmuError("弹幕服务响应格式异常")
    if data.get("success") is False:
        raise DanmuError(data.get("errorMessage") or "弹幕服务返回失败")
    return data


# ------------------------------------------------------------------ 对外能力

async def probe(client: httpx.AsyncClient, base: str) -> dict:
    """连通性测试：只访问服务根路径，不触发上游采集。"""
    b = normalize_base(base)
    if not b:
        raise DanmuError("请填写弹幕服务地址")
    try:
        r = await client.get(b + "/", timeout=8.0)
    except httpx.HTTPError as e:
        raise DanmuError(f"无法连接：{type(e).__name__}") from e
    if r.status_code >= 500:
        raise DanmuError(f"服务返回 HTTP {r.status_code}")
    return {"ok": True, "status": r.status_code, "base": b}


async def search(client: httpx.AsyncClient, base: str, keyword: str) -> dict:
    if not (keyword or "").strip():
        return {"items": []}
    data = _check(await _request(
        client, "GET", endpoint(base, "search/anime"),
        params={"keyword": keyword.strip()}))
    items = []
    for a in data.get("animes") or []:
        if not isinstance(a, dict):
            continue
        items.append({
            "animeId": a.get("animeId"),
            "title": str(a.get("animeTitle") or "").strip(),
            "type": str(a.get("type") or "").strip(),
            "typeDescription": str(a.get("typeDescription") or "").strip(),
            "imageUrl": str(a.get("imageUrl") or "").strip(),
            "episodeCount": a.get("episodeCount") or 0,
            "source": str(a.get("source") or "").strip(),
        })
    return {"items": [i for i in items if i["title"] and i["animeId"] is not None]}


async def bangumi(client: httpx.AsyncClient, base: str, anime_id: str) -> dict:
    data = _check(await _request(
        client, "GET", endpoint(base, f"bangumi/{urllib.parse.quote(str(anime_id))}")))
    bg = data.get("bangumi") or {}
    eps = []
    for e in bg.get("episodes") or []:
        if not isinstance(e, dict):
            continue
        eps.append({
            "episodeId": e.get("episodeId"),
            "title": str(e.get("episodeTitle") or "").strip(),
            "number": str(e.get("episodeNumber") or "").strip(),
            "url": str(e.get("url") or "").strip(),
        })
    return {"title": str(bg.get("animeTitle") or ""),
            "episodes": [e for e in eps if e["episodeId"] is not None]}


async def match(client: httpx.AsyncClient, base: str, file_name: str) -> dict:
    data = _check(await _request(
        client, "POST", endpoint(base, "match"),
        json_body={"fileName": (file_name or "").strip()}))
    items = []
    for m in data.get("matches") or []:
        if not isinstance(m, dict):
            continue
        items.append({
            "episodeId": m.get("episodeId"),
            "animeId": m.get("animeId"),
            "title": str(m.get("animeTitle") or "").strip(),
            "episodeTitle": str(m.get("episodeTitle") or "").strip(),
            "shift": m.get("shift") or 0,
        })
    return {"matched": bool(data.get("isMatched")) and bool(items),
            "items": [i for i in items if i["episodeId"] is not None]}


def _color(value) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = _DEFAULT_COLOR
    n = max(0, min(0xFFFFFF, n))
    return f"#{n:06x}"


def _time(value) -> float:
    try:
        t = float(value)
        return t if 0 <= t < 86400 else 0.0
    except (TypeError, ValueError):
        return 0.0


def normalize_comments(raw: dict) -> list[dict]:
    """把上游 /api/v2/comment 的 JSON 归一成渲染引擎所需的精简结构。"""
    if isinstance(raw, list):
        src = raw
    elif isinstance(raw, dict):
        src = raw.get("comments") or []
        # 兼容某些实现的 data / danmaku 字段名
        if not src and isinstance(raw.get("data"), list):
            src = raw["data"]
        if not src and isinstance(raw.get("danmaku"), list):
            src = raw["danmaku"]
    else:
        src = []
    out: list[dict] = []
    for c in src:
        if not isinstance(c, dict):
            continue
        text = str(c.get("m") or c.get("text") or c.get("content") or "").strip()
        if not text:
            continue
        p = str(c.get("p") or "0,1,16777215").split(",")
        mode = 1
        try:
            mode = int(float(p[1]))
        except (IndexError, ValueError, TypeError):
            pass
        # JSON 输出为 4 字段（时间,类型,颜色,平台）；B 站 XML 为 8 字段
        # （时间,类型,字号,颜色,时间戳,池,hash,id），颜色位置不同。
        color_at = 3 if len(p) >= 6 else 2
        out.append({
            "t": _time(p[0] if p else 0),
            "mode": "bottom" if mode == 4 else ("top" if mode == 5 else "scroll"),
            "color": _color(p[color_at] if len(p) > color_at else _DEFAULT_COLOR),
            "text": text[:200],
        })
    out.sort(key=lambda x: x["t"])
    return out


async def comment(client: httpx.AsyncClient, base: str, comment_id: str) -> dict:
    data = await _request(
        client, "GET",
        endpoint(base, f"comment/{urllib.parse.quote(str(comment_id))}"),
        params={"format": "json", "duration": "true"})
    if isinstance(data, dict) and data.get("success") is False:
        raise DanmuError(data.get("errorMessage") or "弹幕获取失败")
    items = normalize_comments(data)
    return {
        "count": len(items),
        "comments": items,
        "videoDuration": (data.get("videoDuration") or 0) if isinstance(data, dict) else 0,
    }
