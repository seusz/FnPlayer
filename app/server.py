#!/usr/bin/env python3
"""FnPlayer 本地服务：搜索聚合 / 播放解析 / HLS 代理 / 历史 / 设置 / 局域网访问。

默认只监听 127.0.0.1。在设置里显式开启「局域网访问」后，由桌面壳改为绑定
0.0.0.0 并启用访问码；所有播放流量仍走本机代理，播放端不直连源站，
因此不存在跨域问题，Referer/UA 防盗链也在代理层统一处理。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles

BASE = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent)
WEB = BASE / "web"
KERNEL = BASE / "kernel"
# 开发态（工作目录任意）与 PyInstaller 打包态都保证模块可被导入
for _p in (str(BASE), str(KERNEL)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import danmu
import danmu_service
import hls
import lan
from registry import StreamRegistry, TitleRegistry, merge_titles

SESSION_TOKEN = secrets.token_urlsafe(24)
VERSION = "1.0.0"
titles = TitleRegistry()
streams = StreamRegistry()
_client: httpx.AsyncClient | None = None
_shutdown_cb = None
_restart_cb = None
_history_lock = threading.RLock()
# 由桌面壳在每次（重新）绑定端口后告知实际监听状态
LAN_ACTIVE = False
LAN_PORT = 0
LAN_ERROR = ""
BOOT_EPOCH = 0          # 每次（重新）绑定监听地址后 +1，前端用它判断重启完成
# 仅本机可调用的管理接口：这些接口能改配置、探测任意 URL、启停子进程，
# 一旦开放到局域网会形成 SSRF / 远程启动程序风险，故对远端一律拒绝。
ADMIN_PREFIXES = (
    "/api/settings", "/api/sources", "/api/junk-keywords",
    "/api/open-external", "/api/shutdown",
    "/api/danmu/service", "/api/danmu/probe",
    "/api/lan/apply", "/api/lan/restart", "/api/lan/qr",
)


def set_shutdown_callback(cb):
    global _shutdown_cb
    _shutdown_cb = cb


def set_restart_callback(cb):
    """桌面壳注册「重新绑定监听地址」回调（切换局域网开关时调用）。"""
    global _restart_cb
    _restart_cb = cb


def set_lan_state(active: bool, port: int):
    global LAN_ACTIVE, LAN_PORT, BOOT_EPOCH
    LAN_ACTIVE, LAN_PORT = bool(active), int(port)
    BOOT_EPOCH += 1


def set_lan_error(msg: str = ""):
    """桌面壳记录最近一次绑定失败原因，供设置页展示。"""
    global LAN_ERROR
    LAN_ERROR = msg


def data_file(name: str) -> Path:
    import config as cfg
    return cfg.data_dir() / name


def read_history() -> list[dict]:
    try:
        j = json.loads(data_file("history.json").read_text(encoding="utf-8"))
        return j if isinstance(j, list) else []
    except Exception:
        return []


def write_history(items: list[dict]):
    p = data_file("history.json")
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(items[:200], ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    _client = httpx.AsyncClient(
        trust_env=False, follow_redirects=True, timeout=30.0,
        limits=httpx.Limits(max_connections=120, max_keepalive_connections=30),
    )
    yield
    # 弹幕子进程的回收放在桌面壳退出 / /api/shutdown 里统一处理：
    # 切换局域网开关会重启 uvicorn，这里停会把正在用的弹幕服务一起掐掉。
    await _client.aclose()
    _client = None


app = FastAPI(title="FnPlayer", version=VERSION, lifespan=lifespan)


def _set_lan_cookie(response: Response, password: str):
    response.set_cookie(
        lan.COOKIE_NAME, lan.digest(password), max_age=lan.COOKIE_MAX_AGE,
        httponly=True, samesite="lax", path="/")


@app.middleware("http")
async def guard(request: Request, call_next):
    """防跨站调用 + 写操作会话令牌 + 局域网访问码 + 管理接口仅本机。

    本机回环请求始终视为可信，桌面窗口行为与只监听 127.0.0.1 时完全一致。
    """
    path = request.url.path
    remote = request.client.host if request.client else ""
    local = lan.is_local_client(remote)
    password = str(cfg().get().get("lan_password") or "").strip()
    need_cookie = False

    if LAN_ACTIVE and not local:
        if password:
            ok, need_cookie = lan.authorized(request, password)
            if not ok:
                wrong = bool(request.query_params.get(lan.KEY_PARAM))
                if path.startswith("/api/"):
                    return JSONResponse({"error": "需要访问码"}, status_code=401)
                return HTMLResponse(
                    lan.login_page("访问码不正确，请重新输入" if wrong else ""),
                    status_code=401)
        # 管理 / 配置 / 进程控制类接口不开放给局域网，避免 SSRF 与远程启停
        if path.startswith("/api/") and any(
                path == p or path.startswith(p + "/") for p in ADMIN_PREFIXES):
            return JSONResponse({"error": "该管理操作仅允许在主机上进行"},
                                status_code=403)

    if path.startswith("/api/"):
        site = request.headers.get("sec-fetch-site", "")
        if site == "cross-site":
            return JSONResponse({"error": "cross-site denied"}, status_code=403)
        origin = request.headers.get("origin", "")
        if origin:
            host = urllib.parse.urlsplit(origin).netloc
            # 同源（含 http://192.168.x.x:port）放行，回环地址保持兼容
            same_host = not request.headers.get("host", "") or \
                host == request.headers["host"]
            if host and not same_host and not (
                    host.startswith("127.0.0.1") or host.startswith("localhost")):
                return JSONResponse({"error": "origin denied"}, status_code=403)
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            if request.headers.get("x-fn-token") != SESSION_TOKEN:
                return JSONResponse({"error": "bad token"}, status_code=403)

    if need_cookie and path == "/" and request.method == "GET":
        # 扫码链接里的明文访问码只出现一次：立刻换成 cookie 并清掉地址栏
        resp = RedirectResponse("/", status_code=302)
        _set_lan_cookie(resp, password)
        return resp
    response = await call_next(request)
    if need_cookie:
        _set_lan_cookie(response, password)
    return response


def C() -> httpx.AsyncClient:
    assert _client is not None, "client not ready"
    return _client


def cfg():
    import config
    return config.config


# ---------------------------------------------------------------- 页面 / 静态

@app.get("/", response_class=HTMLResponse)
async def index():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("__FN_TOKEN__", SESSION_TOKEN)
                        .replace("__FN_VERSION__", VERSION))


app.mount("/static", StaticFiles(directory=str(WEB)), name="static")


@app.get("/favicon.ico")
async def favicon():
    p = WEB / "fnplayer.ico"
    return FileResponse(p) if p.exists() else Response(status_code=404)


@app.get("/api/health")
async def health():
    return {"ok": True, "version": VERSION,
            "sources": len(cfg().all_sources()), "epoch": BOOT_EPOCH}


@app.get("/api/bootstrap")
async def bootstrap():
    c = cfg().get()
    import config
    return {
        "version": VERSION,
        "has_sources": bool(c["sources"]),
        "settings": {k: v for k, v in c.items() if k != "sources"},
        "history": read_history()[:30],
        "portable": config.is_portable(),
        "config_file": str(config.state_file()),
        "data_dir": str(config.data_dir()),
    }


# ---------------------------------------------------------------- 搜索 / 发现

async def _search(keyword: str) -> list[dict]:
    import providers as cms
    c = cfg()
    sources = c.enabled_sources()
    if not sources:
        return []
    t0 = time.monotonic()
    raw = await cms.search_all(keyword, sources,
                               soft_timeout=float(c.get().get("search_timeout", 9.0)))
    names = {s["id"]: s["name"] for s in sources}
    merged = merge_titles(raw, names, c.is_junk)
    limit = int(c.get().get("max_per_keyword", 24))
    for t in merged[:limit]:
        titles.put(t)
    return [t.brief() for t in merged[:limit]]


@app.get("/api/search")
async def search(q: str = ""):
    q = (q or "").strip()
    if not q:
        return {"items": [], "elapsed_ms": 0, "sources": len(cfg().enabled_sources())}
    t0 = time.monotonic()
    try:
        items = await _search(q)
    except Exception as exc:
        return JSONResponse({"items": [], "error": str(exc)}, status_code=200)
    return {"items": items, "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "sources": len(cfg().enabled_sources())}


@app.get("/api/discover")
async def discover(pg: int = 1):
    """首页片单：按接口拉「最近更新 / 分类列表」（ac=detail&pg=N）。"""
    import providers as cms
    c = cfg()
    sources = c.enabled_sources()
    if not sources:
        return {"items": [], "pg": pg}
    pg = max(1, min(int(pg), 500))

    async def one(s):
        try:
            r = await C().get(s["api"], params={"ac": "detail", "pg": pg},
                              headers={"User-Agent": hls.UA}, timeout=12.0)
            if r.status_code != 200:
                return []
            j = cms.parse_cms_xml(r.text) if "/at/xml" in s["api"].lower() else r.json()
            if not isinstance(j, dict) or j.get("code") not in (1, "1"):
                return []
            return [cms._map_vod(v, s) for v in (j.get("list") or []) if isinstance(v, dict)]
        except Exception:
            return []

    results = await asyncio.gather(*(one(s) for s in sources))
    raw = [t for group in results for t in group if t]
    names = {s["id"]: s["name"] for s in sources}
    merged = merge_titles(raw, names, c.is_junk)[:40]
    for t in merged:
        titles.put(t)
    return {"items": [t.brief() for t in merged], "pg": pg}


@app.get("/api/title/{tid}")
async def title_detail(tid: str):
    t = titles.get(tid)
    if not t:
        return JSONResponse({"error": "影片缓存已失效，请重新搜索"}, status_code=404)
    return t.full()


# ---------------------------------------------------------------- 播放解析

def _referer_for(ep: dict) -> str:
    r = (ep.get("headers") or {}).get("Referer", "")
    if not r:
        p = urllib.parse.urlsplit(ep.get("url", ""))
        r = f"{p.scheme}://{p.netloc}/" if p.scheme and p.netloc else ""
    return r


async def _resolve_episode(ep: dict) -> str | None:
    """按需解析中转页 / 302 跳转，得到最终媒体地址。"""
    import providers as cms
    url = ep["url"]
    ref = _referer_for(ep)
    if ep.get("resolve"):
        found = await cms.resolve_share(C(), url, ref)
        return found.strip() if found else None
    if ep.get("redirect"):
        found = await cms.resolve_redirect(C(), url, ref)
        return found.strip() if found else None
    if ep.get("direct"):
        return url
    # 非直链中转页：不能把网页地址当媒体交给播放器，交给上层自动换线
    return None


def _fallback_episode(t, line: str, index: int, exclude_line: str = ""):
    """当前线路不可播时，找其它线路里同名 / 同序的一集。"""
    cur = t.lines.get(line, [])
    target = cur[index]["name"] if 0 <= index < len(cur) else ""
    for name, eps in t.lines.items():
        if name == line or name == exclude_line or not eps:
            continue
        for i, e in enumerate(eps):
            if target and e["name"] == target:
                return name, i, e
        if index < len(eps):
            return name, index, eps[index]
        if eps:
            return name, 0, eps[0]
    return None


@app.post("/api/play")
async def play(request: Request):
    body = await request.json()
    t = titles.get(str(body.get("id", "")))
    if not t:
        return JSONResponse({"error": "影片缓存已失效，请重新搜索该片名"}, status_code=404)
    line = str(body.get("line") or (list(t.lines)[0] if t.lines else ""))
    index = max(0, int(body.get("ep", 0) or 0))
    eps = t.lines.get(line, [])
    if not eps:
        return JSONResponse({"error": "线路不存在"}, status_code=404)
    tried = set()
    chosen_line, chosen_i = line, index
    final_url = None
    ep = None
    for _ in range(6):
        if chosen_i >= len(t.lines.get(chosen_line, [])):
            chosen_i = 0
        ep = t.lines[chosen_line][chosen_i]
        tried.add((chosen_line, chosen_i))
        final_url = await _resolve_episode(ep)
        if final_url and re.match(r"^https?://", final_url, re.I):
            break
        alt = _fallback_episode(t, chosen_line, chosen_i, chosen_line)
        if not alt or (alt[0], alt[1]) in tried:
            final_url = None
            break
        chosen_line, chosen_i, ep = alt
    if not final_url:
        return JSONResponse({"error": "该线路暂时无法解析，请切换线路重试"}, status_code=502)

    is_hls = bool(re.search(r"\.m3u8(\?|$)", final_url, re.I))
    ctx = streams.create(
        url=final_url,
        headers={"Referer": _referer_for(ep), "User-Agent": hls.UA},
        kind="hls" if is_hls else "file",
        title_id=t.id, title_name=t.title, line=chosen_line,
        episode=ep["name"],
    )
    prefix = f"/api/stream/{ctx.token}"
    url = f"{prefix}/playlist.m3u8" if is_hls else f"{prefix}/media"
    hist = next((h for h in read_history() if h.get("id") == t.id
                 and h.get("episode") == ep["name"]), {})
    names = list(t.lines)
    return {
        "kind": ctx.kind,
        "url": url,
        "stream": ctx.token,
        "id": t.id,
        "title": t.title,
        "poster": t.poster,
        "line": chosen_line,
        "episode": ep["name"],
        "ep_index": chosen_i,
        "lines": names,
        "episodes": [{"name": e["name"], "index": i}
                     for i, e in enumerate(t.lines.get(chosen_line, []))],
        "resume": float(hist.get("ts", 0) or 0) if cfg().get().get("auto_resume") else 0,
        "duration": float(hist.get("duration", 0) or 0),
    }


# ---------------------------------------------------------------- 流代理

def _ctx(token: str):
    ctx = streams.get(token)
    if not ctx:
        return None
    return ctx


@app.get("/api/stream/{token}/playlist.m3u8")
async def playlist(token: str):
    ctx = _ctx(token)
    if not ctx:
        return Response(status_code=404)
    text, base = await hls.fetch_playlist(C(), ctx.url, ctx)
    if text is None:
        return JSONResponse({"error": "无法获取播放清单"}, status_code=502)
    return hls.playlist_response(
        hls.rewrite_playlist(text, base, ctx, f"/api/stream/{token}"))


@app.get("/api/stream/{token}/sub")
async def sub_playlist(token: str, i: int = 0):
    ctx = _ctx(token)
    if not ctx or not (0 <= i < len(ctx.sub_urls)):
        return Response(status_code=404)
    url = ctx.sub_urls[i]
    text, base = await hls.fetch_playlist(C(), url, ctx)
    if text is None:
        return JSONResponse({"error": "无法获取子清单"}, status_code=502)
    return hls.playlist_response(
        hls.rewrite_playlist(text, base, ctx, f"/api/stream/{token}"))


@app.get("/api/stream/{token}/seg")
async def segment(token: str, i: int, request: Request):
    ctx = _ctx(token)
    if not ctx or not (0 <= i < len(ctx.seg_urls)):
        return Response(status_code=404)
    return await hls.proxy_binary(C(), ctx.seg_urls[i], ctx, request)


@app.get("/api/stream/{token}/key")
async def key(token: str, i: int, request: Request):
    ctx = _ctx(token)
    if not ctx or not (0 <= i < len(ctx.key_urls)):
        return Response(status_code=404)
    return await hls.proxy_binary(C(), ctx.key_urls[i], ctx, request,
                                  content_type="application/octet-stream",
                                  cache=ctx.key_cache)


@app.get("/api/stream/{token}/media")
async def media(token: str, request: Request):
    ctx = _ctx(token)
    if not ctx:
        return Response(status_code=404)
    return await hls.proxy_binary(C(), ctx.url, ctx, request)


@app.get("/api/poster")
async def poster(u: str = "", t: str = ""):
    if t != SESSION_TOKEN:
        return Response(status_code=403)
    try:
        url = base64.urlsafe_b64decode(u + "=" * (-len(u) % 4)).decode()
    except Exception:
        return Response(status_code=400)
    if not re.match(r"^https?://", url, re.I):
        return Response(status_code=400)
    p = urllib.parse.urlsplit(url)
    try:
        r = await C().get(url, headers={
            "User-Agent": hls.UA,
            "Referer": f"{p.scheme}://{p.netloc}/",
        }, timeout=12.0)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
            return Response(content=r.content, media_type=r.headers["content-type"],
                            headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        pass
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="300" height="420">'
           b'<rect width="100%" height="100%" fill="#232733"/>'
           b'<text x="50%" y="50%" fill="#6b7280" font-size="20" '
           b'text-anchor="middle" dominant-baseline="middle">\xe6\x9a\x82\xe6\x97\xa0\xe6\xb5\xb7\xe6\x8a\xa5</text></svg>')
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=3600"})


# ---------------------------------------------------------------- 历史 / 设置

@app.get("/api/history")
async def history():
    return {"items": read_history()}


@app.post("/api/history")
async def save_history(request: Request):
    b = await request.json()
    rid = str(b.get("id", ""))
    if not rid:
        return {"ok": False}
    with _history_lock:
        items = [h for h in read_history() if h.get("id") != rid]
        items.insert(0, {
            "id": rid,
            "title": str(b.get("title", ""))[:120],
            "poster": str(b.get("poster", ""))[:500],
            "line": str(b.get("line", ""))[:120],
            "episode": str(b.get("episode", ""))[:80],
            "ts": max(0.0, float(b.get("ts", 0) or 0)),
            "duration": max(0.0, float(b.get("duration", 0) or 0)),
            "updated": time.time(),
        })
        items.sort(key=lambda h: h.get("updated", 0), reverse=True)
        write_history(items)
    return {"ok": True}


@app.delete("/api/history/{rid}")
async def delete_history(rid: str):
    with _history_lock:
        write_history([h for h in read_history() if h.get("id") != rid])
    return {"ok": True}


# ---------------------------------------------------------------- 弹幕代理

def _danmu_base() -> str:
    return str(cfg().get().get("danmu_api") or "")


def _danmu_fail(e: Exception):
    return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


@app.post("/api/danmu/probe")
async def danmu_probe(request: Request):
    """测试填写的弹幕服务是否可达（不写入配置）。"""
    b = await request.json()
    try:
        return await danmu.probe(C(), str(b.get("api") or ""))
    except danmu.DanmuError as e:
        return _danmu_fail(e)


@app.get("/api/danmu/search")
async def danmu_search(keyword: str = ""):
    try:
        return {"ok": True, **(await danmu.search(C(), _danmu_base(), keyword))}
    except danmu.DanmuError as e:
        return _danmu_fail(e)


@app.get("/api/danmu/bangumi/{anime_id}")
async def danmu_bangumi(anime_id: str):
    try:
        return {"ok": True, **(await danmu.bangumi(C(), _danmu_base(), anime_id))}
    except danmu.DanmuError as e:
        return _danmu_fail(e)


@app.post("/api/danmu/match")
async def danmu_match(request: Request):
    b = await request.json()
    try:
        return {"ok": True, **(await danmu.match(
            C(), _danmu_base(), str(b.get("fileName") or "")))}
    except danmu.DanmuError as e:
        return _danmu_fail(e)


@app.get("/api/danmu/comment/{comment_id}")
async def danmu_comment(comment_id: str):
    try:
        return {"ok": True, **(await danmu.comment(C(), _danmu_base(), comment_id))}
    except danmu.DanmuError as e:
        return _danmu_fail(e)


@app.get("/api/danmu/status")
async def danmu_status():
    c = cfg().get()
    base = str(c.get("danmu_api") or "")
    return {"ok": True, "configured": bool(base.strip()),
            "enabled": bool(c.get("danmu_enabled")),
            "base": danmu.normalize_base(base) if base.strip() else ""}


# ---- 本地弹幕服务（便携版独立工作区）----

@app.get("/api/danmu/service")
async def danmu_service_status():
    """状态：是否可用 / 是否运行 / 端口 / 日志尾部。"""
    return await asyncio.to_thread(danmu_service.service.status)


@app.post("/api/danmu/service")
async def danmu_service_toggle(request: Request):
    """设置页开关：打开时拉起本地 Node 服务，关闭时停止。

    启动可能耗时数秒，放到线程池里执行，避免阻塞 uvicorn 事件循环。
    """
    b = await request.json()
    enabled = bool(b.get("enabled"))
    patch = {}
    if "danmu_service_port" in b:
        try:
            port = int(b["danmu_service_port"])
            if not (1 <= port <= 65535):
                raise ValueError
            patch["danmu_service_port"] = port
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "端口需为 1-65535 的整数"},
                                status_code=400)
    if "danmu_service_token" in b:
        token = str(b["danmu_service_token"]).strip()
        if not token or len(token) > 64 or "/" in token:
            return JSONResponse({"ok": False, "error": "TOKEN 不能为空且不能含 /"},
                                status_code=400)
        patch["danmu_service_token"] = token
    return await asyncio.to_thread(danmu_service.service.apply, enabled, patch)


@app.get("/api/danmu/service/logs")
async def danmu_service_logs(lines: int = 60):
    return {"ok": True, "text": danmu_service.service.log_tail(
        max(1, min(400, lines)))}


# ---------------------------------------------------------------- 设置 / 源

@app.get("/api/settings")
async def get_settings():
    return cfg().get()


@app.post("/api/settings")
async def set_settings(request: Request):
    b = await request.json()
    # 本地弹幕服务、局域网监听的开关/端口/访问码只走各自专用接口，
    # 避免「保存设置」绕过进程启停或端口重绑逻辑，造成配置与实际状态不一致。
    for k in ("danmu_service_enabled", "danmu_service_port",
              "danmu_service_token", "lan_enabled", "lan_port",
              "lan_password"):
        b.pop(k, None)
    return {"ok": True, "settings": cfg().save(b)}


@app.get("/api/sources")
async def list_sources():
    return {"items": cfg().all_sources()}


@app.post("/api/sources")
async def add_source(request: Request):
    b = await request.json()
    ok, msg = cfg().add_source(b.get("name", ""), b.get("api", ""))
    return {"ok": ok, "message": msg,
            "items": cfg().all_sources()} if ok else JSONResponse(
                {"ok": False, "message": msg}, status_code=400)


@app.post("/api/sources/bulk")
async def add_sources_bulk(request: Request):
    b = await request.json()
    text = str(b.get("text") or "")
    if not text.strip():
        return JSONResponse({"ok": False, "message": "内容为空"}, status_code=400)
    added, skipped, items = cfg().add_sources_bulk(text, bool(b.get("enabled", True)))
    return {"ok": True, "added": added, "skipped": skipped, "items": items}


@app.patch("/api/sources/{sid}")
async def patch_source(sid: str, request: Request):
    b = await request.json()
    ok, msg = cfg().update_source(sid, b)
    return {"ok": ok, "message": msg, "items": cfg().all_sources()}


@app.delete("/api/sources/{sid}")
async def del_source(sid: str):
    ok, msg = cfg().remove_source(sid)
    return {"ok": ok, "message": msg, "items": cfg().all_sources()}


@app.post("/api/sources/test")
async def test_source(request: Request):
    import providers as cms
    b = await request.json()
    api = str(b.get("api", "")).strip()
    return await cms.probe_source(api, keyword=str(b.get("keyword") or "测试"))


@app.post("/api/sources/test-all")
async def test_all_sources():
    import providers as cms
    srcs = cfg().all_sources()

    async def one(s):
        r = await cms.probe_source(s["api"], keyword="庆余年")
        return {"id": s["id"], "name": s["name"], **r}

    return {"items": await asyncio.gather(*(one(s) for s in srcs))}


@app.post("/api/junk-keywords")
async def junk_keywords(request: Request):
    b = await request.json()
    if b.get("reset"):
        return {"items": cfg().reset_junk_keywords()}
    return {"items": cfg().set_junk_keywords(b.get("keywords", ""))}


@app.post("/api/open-external")
async def open_external(request: Request):
    """用使用者自行配置的外部播放器（mpv / VLC）打开当前本地流。"""
    import subprocess
    exe = str(cfg().get().get("external_player") or "").strip()
    if not exe or not Path(exe).is_file():
        return JSONResponse({"ok": False, "message": "请先在设置中配置外部播放器路径"},
                            status_code=400)
    b = await request.json()
    url = str(b.get("url", ""))
    if not url.startswith("/api/stream/"):
        return JSONResponse({"ok": False, "message": "无效的流地址"}, status_code=400)
    port = int(os.environ.get("FNPLAYER_PORT", "18790"))
    full = f"http://127.0.0.1:{port}{url}"
    try:
        subprocess.Popen([exe, full], shell=False)
        return {"ok": True}
    except Exception as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=500)


# ---------------------------------------------------------------- 局域网访问

def _lan_password() -> str:
    return str(cfg().get().get("lan_password") or "").strip()


def _lan_urls() -> list[dict]:
    port = LAN_PORT or int(cfg().get().get("lan_port") or 18790)
    return lan.urls(port, _lan_password()) if LAN_ACTIVE else []


def _schedule_restart():
    """延迟一点再重启，让 POST 的响应先回给前端。"""
    async def _later():
        await asyncio.sleep(0.35)
        if _restart_cb:
            _restart_cb()
    asyncio.create_task(_later())


@app.get("/api/lan")
async def lan_status(request: Request):
    """监听状态 / 访问地址。带访问码的 URL 只回显给本机页面。"""
    c = cfg().get()
    enabled = bool(c.get("lan_enabled"))
    port = LAN_PORT or int(c.get("lan_port") or 18790)
    local = lan.is_local_client(request.client.host if request.client else "")
    out = {
        "active": LAN_ACTIVE,
        "enabled": enabled,
        "port": port,
        "configured_port": int(c.get("lan_port") or 18790),
        "bind": "0.0.0.0" if LAN_ACTIVE else "127.0.0.1",
        "local": local,
        "password_set": bool(_lan_password()),
        "need_restart": enabled != LAN_ACTIVE or port != int(c.get("lan_port") or 18790),
        "hostname": socket.gethostname(),
        "error": LAN_ERROR,
    }
    if local:
        out["password"] = _lan_password()
        out["urls"] = _lan_urls()
        out["ips"] = lan.local_ips()
    else:
        out["urls"] = []
        out["ips"] = []
    return out


@app.get("/api/lan/qr")
async def lan_qr(i: int = 0):
    """第 i 个网卡地址的二维码（仅本机，避免把带访问码的链接外泄）。"""
    items = _lan_urls()
    if not (0 <= i < len(items)):
        return Response(status_code=404)
    svg = lan.qr_svg(items[i]["url"])
    if svg is None:
        return Response(status_code=501)
    return Response(svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/lan/apply")
async def lan_apply(request: Request):
    """保存局域网设置并重新绑定监听地址（会短暂中断播放）。"""
    b = await request.json()
    try:
        port = int(b.get("port") or 18790)
    except (TypeError, ValueError):
        port = 0
    if not (1 <= port <= 65535):
        return JSONResponse({"ok": False, "error": "端口需为 1-65535 的整数"},
                            status_code=400)
    password = str(b.get("password") or "").strip()
    if password and (len(password) < 4 or len(password) > 32
                     or not re.match(r"^[A-Za-z0-9_-]+$", password)):
        return JSONResponse(
            {"ok": False, "error": "访问码需为 4-32 位字母、数字、- 或 _"},
            status_code=400)
    enabled = bool(b.get("enabled"))
    cfg().save({"lan_enabled": enabled, "lan_port": port,
                "lan_password": password})
    _schedule_restart()
    return {"ok": True, "restarting": True, "port": port,
            "enabled": enabled, "password_set": bool(password)}


@app.post("/api/lan/restart")
async def lan_restart():
    """不修改配置，仅按当前配置重新绑定（设置页「重新应用」）。"""
    _schedule_restart()
    return {"ok": True, "restarting": True,
            "port": int(cfg().get().get("lan_port") or 18790)}


@app.post("/api/shutdown")
async def shutdown():
    async def _later():
        await asyncio.sleep(0.35)
        try:
            await asyncio.to_thread(danmu_service.service.stop)
        except Exception:
            pass
        if _shutdown_cb:
            _shutdown_cb()
    asyncio.create_task(_later())
    return {"ok": True}
