#!/usr/bin/env python3
"""HLS / 直链流式代理。

播放端（hls.js）只与本机 127.0.0.1 通信，由本模块负责：
  * 抓取上游 m3u8，把分片、AES 密钥、fMP4 初始化段、多码率子清单的 URI
    全部改写为同源代理地址，解决跨域与 Referer/UA 防盗链；
  * master playlist 保留全部标签，交给 hls.js 自行按带宽选码率；
  * 分片 / 密钥 / mp4 直链以流式透传，并透传 Range（支持拖动进度）。

设计参照上游 app.py 的 /v/media/online 播放链，按桌面单机场景做了精简：
AES-128 由 hls.js 在浏览器端解密，密钥经同源代理返回即可。
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Optional

import httpx
from fastapi import Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse

from registry import StreamCtx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

HOP_HEADERS = {
    "content-encoding", "content-length", "transfer-encoding", "connection",
    "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "upgrade",
}


def upstream_headers(ctx: StreamCtx, extra: Optional[dict] = None) -> dict:
    h = {"User-Agent": UA, "Accept": "*/*"}
    if ctx.headers.get("Referer"):
        h["Referer"] = ctx.headers["Referer"]
    if extra:
        h.update(extra)
    return h


async def fetch_playlist(client: httpx.AsyncClient, url: str,
                         ctx: StreamCtx) -> tuple[str, str] | tuple[None, None]:
    """抓取 m3u8，返回 (文本, 最终绝对地址)。"""
    if not re.match(r"^https?://", url, re.I):
        return None, None
    try:
        r = await client.get(url, headers=upstream_headers(ctx), timeout=20.0,
                             follow_redirects=True)
        if r.status_code != 200:
            return None, None
        text = r.content.decode("utf-8", errors="replace")
        if "#EXTM3U" not in text[:200]:
            return None, None
        return text, str(r.url)
    except Exception:
        return None, None


def rewrite_playlist(text: str, base: str, ctx: StreamCtx, prefix: str) -> str:
    """把上游清单里的 URI 改写成本机代理地址。prefix=/api/stream/<token>"""
    out: list[str] = []
    last_tag = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            out.append("")
            continue
        if line.startswith("#"):
            last_tag = line.split(":", 1)[0].upper()
            # EXT-X-KEY / EXT-X-MAP / EXT-X-MEDIA / I-FRAME-STREAM-INF 的 URI 属性
            if "URI=" in line and re.search(r'URI="[^"]+"', line):
                def repl(m):
                    uri = m.group(1)
                    absu = urllib.parse.urljoin(base, uri)
                    if last_tag in ("#EXT-X-KEY",):
                        i = len(ctx.key_urls)
                        ctx.key_urls.append(absu)
                        return f'URI="{prefix}/key?i={i}"'
                    if last_tag in ("#EXT-X-I-FRAME-STREAM-INF", "#EXT-X-MEDIA"):
                        i = len(ctx.sub_urls)
                        ctx.sub_urls.append(absu)
                        return f'URI="{prefix}/sub?i={i}"'
                    # EXT-X-MAP 等：当作分片处理
                    i = len(ctx.seg_urls)
                    ctx.seg_urls.append(absu)
                    return f'URI="{prefix}/seg?i={i}"'
                out.append(re.sub(r'URI="([^"]+)"', repl, line, flags=re.I))
            else:
                out.append(line)
            continue
        # 非注释行：URI 行
        absu = urllib.parse.urljoin(base, line)
        if last_tag in ("#EXT-X-STREAM-INF", "#EXT-X-I-FRAME-STREAM-INF"):
            i = len(ctx.sub_urls)
            ctx.sub_urls.append(absu)
            out.append(f"{prefix}/sub?i={i}")
        else:
            i = len(ctx.seg_urls)
            ctx.seg_urls.append(absu)
            out.append(f"{prefix}/seg?i={i}")
        last_tag = ""
    body = "\n".join(out)
    # 修复相对地址在部分播放器上的解析差异：统一用根相对绝对路径
    return body + "\n"


def playlist_response(text: str) -> Response:
    return PlainTextResponse(
        text, media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store",
                 "Access-Control-Allow-Origin": "*"},
    )


async def proxy_binary(client: httpx.AsyncClient, url: str, ctx: StreamCtx,
                       request: Request, *, content_type: str | None = None,
                       cache: dict | None = None, timeout: float = 30.0) -> Response:
    """流式透传上游二进制，透传 Range / 206 / Content-Range。"""
    if not re.match(r"^https?://", url, re.I):
        return Response(status_code=400)
    if cache is not None and url in cache and not request.headers.get("range"):
        return Response(
            content=cache[url], media_type="application/octet-stream",
            headers={"Cache-Control": "public, max-age=3600",
                     "Access-Control-Allow-Origin": "*"},
        )
    headers = upstream_headers(ctx)
    rng = request.headers.get("range")
    if rng:
        headers["Range"] = rng
    try:
        req = client.build_request("GET", url, headers=headers, timeout=timeout)
        up = await client.send(req, stream=True, follow_redirects=True)
    except Exception:
        return Response(status_code=502)

    out_headers = {}
    for k, v in up.headers.items():
        if k.lower() not in HOP_HEADERS and k.lower() != "content-type":
            out_headers[k] = v
    out_headers["Access-Control-Allow-Origin"] = "*"
    ct = content_type or up.headers.get("content-type", "application/octet-stream")
    out_headers["Content-Type"] = ct

    async def body():
        chunks = []
        try:
            async for chunk in up.aiter_bytes(chunk_size=65536):
                if cache is not None and len(chunks) < 4:
                    chunks.append(chunk)
                yield chunk
        finally:
            await up.aclose()
            if cache is not None and chunks and up.status_code == 200:
                data = b"".join(chunks)
                if 0 < len(data) <= 1024 * 64:
                    cache[url] = data

    return StreamingResponse(body(), status_code=up.status_code,
                             headers=out_headers)
