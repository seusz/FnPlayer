#!/usr/bin/env python3
"""局域网访问支持（独立模块，不改动播放内核）。

职责：
  * 枚举本机可用于局域网访问的 IPv4 地址
  * 访问码校验 / cookie 摘要 / 二维码 URL 生成
  * 未认证设备的轻量登录页

安全边界：绑定地址由 app/main.py 决定（默认只绑 127.0.0.1），
本模块只提供判定与展示，不主动开放任何端口。
"""
from __future__ import annotations

import hashlib
import hmac
import html
import ipaddress
import socket
import urllib.parse
from io import BytesIO

COOKIE_NAME = "fn_lan"
KEY_PARAM = "key"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30


def local_ips() -> list[str]:
    """返回可用于局域网访问的 IPv4（排除回环 / 链路本地 / 虚拟无效地址）。"""
    found: list[str] = []
    # 默认出口网卡（UDP connect 不产生实际流量）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.4)
        s.connect(("223.5.5.5", 80))
        found.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    # 主机名解析到的其它网卡（多网卡 / 虚拟机时会列出多个）
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except Exception:
        pass
    out: list[str] = []
    for raw in found:
        ip = str(raw or "").strip()
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.version != 4 or addr.is_loopback or addr.is_link_local \
                or addr.is_unspecified or addr.is_multicast:
            continue
        # RFC 2544 基准测试段，常见于代理 / 虚拟网卡，不是真实局域网地址
        if ip.startswith(("198.18.", "198.19.")):
            continue
        if ip not in out:
            out.append(ip)

    def rank(ip: str) -> int:
        if ip.startswith(("192.168.", "10.")):
            return 0
        try:
            if ipaddress.ip_address(ip).is_private:
                return 1
        except ValueError:
            pass
        return 2

    out.sort(key=rank)
    return out


def is_local_client(host: str | None) -> bool:
    """请求是否来自本机（回环地址一律视为可信）。"""
    h = str(host or "").strip().lower()
    return h in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1") \
        or h.startswith("127.")


def digest(password: str) -> str:
    return hashlib.sha256(("fnplayer-lan|" + password).encode("utf-8")).hexdigest()


def authorized(request, password: str) -> tuple[bool, bool]:
    """校验访问码。返回 (是否通过, 是否来自 URL key 需要种 cookie)。

    URL 里的 ?key= 是明文访问码；cookie 里只存摘要，避免明文常驻浏览器。
    """
    if not password:
        return True, False
    cookie = request.cookies.get(COOKIE_NAME, "")
    if cookie and hmac.compare_digest(cookie, digest(password)):
        return True, False
    raw = request.query_params.get(KEY_PARAM, "")
    if raw and hmac.compare_digest(raw, password):
        return True, True
    hdr = request.headers.get("x-fn-key", "")
    if hdr and (hmac.compare_digest(hdr, password)
                or hmac.compare_digest(hdr, digest(password))):
        return True, False
    return False, False


def urls(port: int, password: str = "") -> list[dict]:
    """每个网卡生成一条带访问码的直达 URL（供复制 / 扫码）。"""
    suffix = ("?" + urllib.parse.urlencode({KEY_PARAM: password})) if password else ""
    return [{"ip": ip, "url": f"http://{ip}:{port}/{suffix}"} for ip in local_ips()]


def qr_svg(text: str) -> str | None:
    """生成二维码 SVG；segno 为可选依赖，缺失时返回 None（前端隐藏二维码）。"""
    try:
        import segno
    except Exception:
        return None
    try:
        buf = BytesIO()
        segno.make(text, error="m").save(
            buf, kind="svg", scale=4, xmldecl=False, nl=False,
            dark="#111827", light="#ffffff")
        return buf.getvalue().decode("utf-8")
    except Exception:
        return None


LOGIN_CSS = """
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;
align-items:center;justify-content:center;background:#0f1117;color:#e6e9f2;
font:15px/1.6 -apple-system,BlinkMacSystemFont,"Microsoft YaHei",sans-serif}
.card{width:min(92vw,380px);background:#1a1e2a;border:1px solid #2c3346;
border-radius:16px;padding:30px 28px;box-shadow:0 18px 50px rgba(0,0,0,.35)}
h1{margin:0 0 6px;font-size:20px}p{margin:0 0 20px;color:#98a0b5;font-size:13px}
input{width:100%;padding:11px 13px;border-radius:9px;border:1px solid #39415a;
background:#111520;color:#e6e9f2;font-size:16px;outline:none}
input:focus{border-color:#6c8cff}button{width:100%;margin-top:14px;padding:11px;
border:0;border-radius:9px;background:#5b7cfa;color:#fff;font-size:15px;cursor:pointer}
button:hover{background:#6c8cff}.err{margin-top:12px;color:#ff9b9b;font-size:13px}
.tip{margin:18px 0 0;font-size:12px;color:#6f778c}
"""


def login_page(error: str = "") -> str:
    """未认证设备的访问码输入页（手机浏览器友好）。"""
    msg = (f'<div class="err">{html.escape(error)}</div>' if error else "")
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<title>FnPlayer · 访问码</title>'
        f'<style>{LOGIN_CSS}</style></head><body><form class="card" method="get" action="/">'
        '<h1>FnPlayer</h1><p>这是一台本机播放器提供的局域网页面，请输入访问码。</p>'
        f'<input type="text" name="{KEY_PARAM}" placeholder="访问码" autocomplete="off" '
        'autocapitalize="off" spellcheck="false" autofocus required>'
        '<button type="submit">进入</button>' + msg +
        '<p class="tip">访问码在播放器「设置 → 局域网访问」中查看或修改。</p>'
        '</form></body></html>'
    )
