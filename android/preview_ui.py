#!/usr/bin/env python3
"""Render the Android front-end in headless Chrome at a phone viewport.

A tiny pure-stdlib HTTP server mocks the in-app 127.0.0.1 API, so the exact
same index.html / app.js / app.css shipped inside the APK can be inspected.
Outputs: android/dist/preview_home.png, preview_detail.png, preview_settings.png
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PIL import Image, ImageDraw

AND = Path(__file__).resolve().parent
WEB = AND / "assets" / "web"
DIST = AND / "dist"
DIST.mkdir(exist_ok=True)
PORT = 18991
BASE = "http://127.0.0.1:%d" % PORT

TITLES = ["星际穿越", "漫长的季节", "流浪地球", "三体", "繁花", "奥本海默",
          "千与千寻", "山海情", "大明王朝", "瞬息全宇宙", "沙丘", "看不见的客人"]
SOURCES = [
    {"id": "u1", "name": "示例源 A", "api": "https://example-a.test/api.php/provide/vod/at/json", "enabled": True},
    {"id": "u2", "name": "示例源 B", "api": "https://example-b.test/api.php/provide/vod/at/json", "enabled": True},
    {"id": "u3", "name": "已停用源", "api": "https://example-c.test/api.php/provide/vod/at/json", "enabled": False},
]

DANMU_CONFIG = {
    "auto_resume": True, "search_timeout": 9,
    "danmu_enabled": True, "danmu_auto": True, "danmu_api": "",
    "danmu_opacity": 0.85, "danmu_font_size": 20, "danmu_area": 0.75,
    "danmu_speed": 8.0, "danmu_offset": 0.0, "danmu_bold": False,
}


def poster(seed: int) -> bytes:
    im = Image.new("RGB", (300, 420))
    d = ImageDraw.Draw(im)
    a = (seed * 47) % 90
    for y in range(420):
        t = y / 420
        d.line([(0, y), (300, y)], fill=(int(30 + 45 * t + a), int(45 + 30 * t), int(85 + 60 * (1 - t))))
    d.ellipse((95, 130, 205, 240), outline=(255, 255, 255, 150), width=3)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


POSTERS = [poster(i + 1) for i in range(len(TITLES))]


def brief(i: int) -> dict:
    n = TITLES[i]
    return {
        "id": "u1:%d" % (1000 + i), "title": n,
        "type": ["剧情", "科幻", "悬疑"][i % 3],
        "year": str(2014 + i % 9), "area": "中国大陆",
        "remarks": "更新至12集", "score": str(round(7.2 + i * .11, 1)),
        "poster": "%s/poster/%d.jpg" % (BASE, i),
        "providers": ["示例源 A", "示例源 B"], "episodes": 12,
    }


def full(i: int) -> dict:
    t = brief(i)
    eps = [{"name": "第%d集" % (k + 1),
            "url": "%s/hls/demo.m3u8" % BASE, "direct": True,
            "resolve": False, "redirect": False, "referer": BASE + "/"}
           for k in range(12)]
    t.update({
        "overview": "这是一段用于界面自检的示例简介文本，用于检查详情页在窄屏下的排版、"
                    "简介折叠与选集网格是否正常。内容仅作演示，不代表任何真实影片。",
        "actor": "示例演员", "director": "示例导演",
        "lines": {"高清线路": eps, "备用线路": eps[:8]},
    })
    return t


PREVIEW_SCRIPTS = {
    "detail": "window.addEventListener('load',function(){setTimeout(function(){"
              "var c=document.querySelector('[data-act=\"detail\"]');if(c)c.click();},1500);});",
    "settings": "window.addEventListener('load',function(){setTimeout(function(){"
                "var b=document.querySelector('[data-tab=\"settings\"]');if(b)b.click();},900);});",
    "player": "window.addEventListener('load',function(){setTimeout(function(){"
              "var c=document.querySelector('[data-act=\"detail\"]');if(c)c.click();},1200);"
              "setTimeout(function(){var p=document.querySelector('[data-act=\"play-ep\"]');"
              "if(p)p.click();},2600);});",
    "danmu": "window.addEventListener('load',function(){setTimeout(function(){"
             "var c=document.querySelector('[data-act=\"detail\"]');if(c)c.click();},1200);"
             "setTimeout(function(){var p=document.querySelector('[data-act=\"play-ep\"]');"
             "if(p)p.click();},2500);"
             "setTimeout(function(){var b=document.getElementById('btnDanmu');if(b)b.click();},4200);"
             "setTimeout(function(){var t=document.getElementById('dmText');"
             "if(t){t.value='前方高能';document.getElementById('dmSendBtn').click();}},4700);});",
    "favorites": "window.addEventListener('load',function(){setTimeout(function(){"
                 "var b=document.querySelector('[data-tab=\"favorites\"]');if(b)b.click();},1200);});",
    "search": "window.addEventListener('load',function(){setTimeout(function(){"
              "var i=document.getElementById('searchInput');if(i){i.value='三体';"
              "document.getElementById('btnSearch').click();}},1200);});",
    "back": "window.addEventListener('load',function(){"
            "setTimeout(function(){var i=document.getElementById('searchInput');"
            "i.value='三体';document.getElementById('btnSearch').click();},900);"
            "setTimeout(function(){var c=document.querySelector('#searchBody [data-act=\"detail\"]');"
            "if(c)c.click();},1900);"
            "setTimeout(function(){var p=document.querySelector('#detailBody [data-act=\"play-ep\"]');"
            "if(p)p.click();},2900);"
            "setTimeout(function(){var cur=function(){var e=document.querySelector('.view.active');"
            "return e?e.id.replace('view-',''):'?';};var tr=[cur()];"
            "for(var k=0;k<4;k++){var ok=window.__fnBack();tr.push(cur());"
            "if(!ok){tr.push('exit');break;}}"
            "var d=document.createElement('div');d.id='backTrace';"
            "d.textContent=tr.join(',');document.body.appendChild(d);},4300);});",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send_bytes(self, code, ctype, data):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def send_json(self, obj, code=200):
        self.send_bytes(code, "application/json; charset=utf-8",
                        json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        u = urlsplit(self.path)
        p, q = u.path, parse_qs(u.query)
        if p == "/" or p == "/index.html":
            html = (WEB / "index.html").read_text(encoding="utf-8")
            tag = q.get("preview", [""])[0]
            js = PREVIEW_SCRIPTS.get(tag, "")
            css = ""
            if q.get("narrow", [""])[0] == "1":
                # Headless Chrome clamps the CSS viewport to 500px. Constrain the
                # app container to emulate a real 390dp phone screen.
                css = ("<style>#app{width:390px;max-width:390px;}"
                       ".tabbar{right:auto;width:390px;}"
                       ".sheet{right:auto;width:390px;}"
                       ".sheet-mask{right:auto;width:390px;}"
                       ".dm-panel{right:auto;width:390px;}"
                       ".dm-mask{right:auto;width:390px;}"
                       ".toast{left:195px}</style>")
            html = html.replace("</head>", css + "</head>")
            if js:
                html = html.replace("</body>", "<script>%s</script></body>" % js)
            self.send_bytes(200, "text/html; charset=utf-8", html.encode("utf-8"))
            return
        if p.startswith("/static/"):
            name = p[len("/static/"):]
            f = WEB / name
            if f.is_file():
                if name.endswith(".css"):
                    ct = "text/css; charset=utf-8"
                elif name.endswith(".js"):
                    ct = "application/javascript; charset=utf-8"
                elif name.endswith(".svg"):
                    ct = "image/svg+xml"
                elif name.endswith(".txt"):
                    ct = "text/plain; charset=utf-8"
                else:
                    ct = "image/png"
                self.send_bytes(200, ct, f.read_bytes())
                return
        if p.startswith("/poster/"):
            i = int(p.split("/")[-1].split(".")[0]) % len(POSTERS)
            self.send_bytes(200, "image/jpeg", POSTERS[i])
            return
        if p == "/api/poster":
            self.send_bytes(200, "image/jpeg", POSTERS[0])
            return
        if p == "/api/bootstrap":
            self.send_json({"version": "1.2.0", "platform": "android",
                            "config": DANMU_CONFIG,
                            "config_file": "/data/user/0/com.fnplayer.mobile/files/config.json",
                            "source_count": 2})
            return
        if p == "/api/health":
            self.send_json({"ok": True, "version": "1.2.0", "platform": "android", "lan": False})
            return
        if p == "/api/history":
            self.send_json([{"id": "u1:1000", "title": TITLES[0], "poster": "%s/poster/0.jpg" % BASE,
                             "episode": "第3集", "line": "高清线路", "ts": 1830, "duration": 5400}])
            return
        if p == "/api/favorites":
            self.send_json({"ok": True, "items": [brief(i) for i in (0, 2, 5)]})
            return
        if p == "/api/discover":
            self.send_json({"items": [brief(i) for i in range(len(TITLES))],
                            "pg": int(q.get("pg", ["1"])[0])})
            return
        if p == "/api/search":
            kw = q.get("q", [""])[0]
            picked = [i for i, n in enumerate(TITLES) if kw in n] or list(range(4))
            self.send_json({"items": [brief(i) for i in picked], "elapsed_ms": 236, "sources": 2})
            return
        if p == "/api/title":
            tid = q.get("tid", ["u1:1000"])[0]
            i = int(tid.split(":")[-1]) - 1000
            self.send_json(full(max(0, min(i, len(TITLES) - 1))))
            return
        if p == "/api/sources":
            self.send_json({"items": SOURCES})
            return
        if p == "/api/settings":
            self.send_json(DANMU_CONFIG)
            return
        if p == "/api/danmu/status":
            self.send_json({"ok": True, "configured": True, "enabled": True, "auto": True,
                            "mode": "core", "base": "内置弹幕核心（127.0.0.1:9322）",
                            "coverage": "core",
                            "coverage_note": "内置弹幕核心已在本机运行，支持豆瓣/腾讯/优酷/爱奇艺/咪咕/B站等平台，国产剧与电影均可匹配",
                            "core": {"state": "ready", "ready": True,
                                     "message": "内置弹幕核心已就绪",
                                     "base": "http://127.0.0.1:9322",
                                     "node_version": "24.19.0",
                                     "sources": "douban,360,renren,hanjutv,tencent,youku,iqiyi,migu,bilibili,dandan",
                                     "started_at": 1},
                            "ready": "ok", "ready_text": "内置弹幕核心已就绪（多平台聚合）· 812 ms", "checked_at": 1,
                            "upstream_ms": 812, "upstream_error": "",
                            "last_match_title": TITLES[0],
                            "last_match_reason": "内置弹幕核心自动匹配成功", "last_match_count": 6})
            return
        if p == "/api/danmu/search":
            self.send_json({"ok": True, "items": [
                {"animeId": 17617, "title": TITLES[0], "type": "tvseries",
                 "typeDescription": "示例剧集", "episodeCount": 12, "source": "dandan"}]})
            return
        if p.startswith("/api/danmu/bangumi/"):
            self.send_json({"ok": True, "title": TITLES[0],
                            "episodes": [{"episodeId": 176170001 + i,
                                          "title": "第%d集" % (i + 1), "number": str(i + 1)}
                                         for i in range(12)]})
            return
        if p.startswith("/api/danmu/comment/"):
            self.send_json({"ok": True, "count": 6, "comments": [
                {"t": 1.0 + i * 1.6, "mode": ["scroll", "top", "bottom"][i % 3],
                 "color": "#ffffff", "text": "这条弹幕用于界面自检 %d" % (i + 1)}
                for i in range(6)], "videoDuration": 0})
            return
        if p == "/api/hls/demo.m3u8":
            self.send_bytes(200, "application/vnd.apple.mpegurl",
                            b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
                            b"#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:4.0,\nseg0.ts\n#EXT-X-ENDLIST\n")
            return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            data = {}
        p = urlsplit(self.path).path
        if p == "/api/play":
            self.send_json({"kind": "file", "url": BASE + "/hls/demo.m3u8", "stream": "mock",
                            "id": "u1:1000", "title": TITLES[0], "poster": "",
                            "line": "高清线路", "episode": "第1集", "ep_index": 0,
                            "lines": ["高清线路", "备用线路"],
                            "episodes": [{"name": "第%d集" % (k + 1), "index": k} for k in range(12)],
                            "resume": 120})
            return
        if p == "/api/sources/bulk":
            self.send_json({"ok": True, "added": 2, "parsed": 2})
            return
        if p == "/api/sources/test":
            self.send_json({"ok": True, "count": 12, "sample": TITLES[0], "error": ""})
            return
        if p == "/api/danmu/match":
            self.send_json({"ok": True, "matched": True, "source": "custom-match", "items": [
                {"episodeId": 176170001, "animeId": 17617,
                 "title": TITLES[0], "episodeTitle": "第1集", "shift": 0}]})
            return
        if p == "/api/danmu/probe":
            self.send_json({"ok": True, "status": 200, "base": data.get("api", ""),
                            "items": 4, "ms": 320})
            return
        if p == "/api/danmu/prewarm":
            self.send_json({"ok": True})
            return
        if p == "/api/settings":
            self.send_json(DANMU_CONFIG)
            return
        if p in ("/api/favorites", "/api/favorites/remove", "/api/favorites/clear"):
            self.send_json({"ok": True, "items": [brief(i) for i in (0, 2, 5)]})
            return
        self.send_json({"ok": True})


def browser() -> str:
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"):
        if Path(p).is_file():
            return p
    raise RuntimeError("未找到 Chrome / Edge")


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    exe = browser()
    tmp = Path(tempfile.mkdtemp(prefix="fn-android-ui-"))
    common = ["--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
              "--no-first-run", "--no-default-browser-check",
              "--virtual-time-budget=9000", "--force-prefers-reduced-motion=reduce",
              "--force-device-scale-factor=1"]

    checks = {}
    for tag in ("home", "detail", "settings", "player", "narrow", "danmu",
                "favorites", "search", "back"):
        url = BASE + "/?preview=" + tag
        if tag == "home":
            url = BASE + "/"
        if tag == "narrow":
            url = BASE + "/?preview=home&narrow=1"
        out = DIST / ("preview_%s.png" % tag)
        profile = tmp / ("p-" + tag)
        # Settings grew with the danmaku group; use a taller viewport there.
        height = 1900 if tag == "settings" else 900
        r = subprocess.run([exe] + common + ["--user-data-dir=" + str(profile),
                                             "--window-size=500,%d" % height,
                                             "--screenshot=" + str(out), url],
                           capture_output=True, timeout=120)
        dom = subprocess.run([exe] + common + ["--user-data-dir=" + str(profile) + "2",
                                               "--window-size=500,%d" % height, "--dump-dom", url],
                             capture_output=True, timeout=120)
        text = dom.stdout.decode("utf-8", "replace")
        (DIST / ("preview_%s_dom.txt" % tag)).write_text(text, encoding="utf-8")
        checks[tag] = {
            "png": out.is_file() and out.stat().st_size > 1000,
            "dom_len": len(text),
            "has_ui": all(x in text for x in ("FnPlayer", "tabbar", "searchInput")),
        }
        print(tag, "screenshot:", checks[tag]["png"], "dom:", len(text),
              "rc:", r.returncode)
        if tag == "detail":
            checks[tag]["detail_ok"] = "播放线路" in text and "选集" in text
        if tag == "settings":
            checks[tag]["settings_ok"] = all(x in text for x in
                                             ("批量导入", "配置迁移", "自动续播", "示例源 A",
                                              "弹幕", "dmSvcStatus", "setDanmuEnabled",
                                              "国产剧与电影通常匹配不到"))
        if tag == "home":
            checks[tag]["home_ok"] = "最近更新" in text and "继续观看" in text and "class=\"card\"" in text
        if tag == "player":
            checks[tag]["player_ok"] = "player-controls" in text and "倍速" in text and "btnDanmu" in text
        if tag == "danmu":
            checks[tag]["danmu_ok"] = ("dm-panel show" in text
                                       and ("已加载" in text or "弹幕服务" in text)
                                       and "dmOpacity" in text
                                       and "国产剧与电影通常匹配不到" in text)
        if tag == "favorites":
            checks[tag]["favorites_ok"] = ("view-favorites" in text and "共 3 部" in text
                                            and "fav-remove" in text and "我的收藏" in text)
        if tag == "search":
            checks[tag]["search_ok"] = ("view-search" in text and "搜索结果" in text
                                         and "searchInput2" in text)
        if tag == "back":
            checks[tag]["back_ok"] = "backTrace" in text and "player,detail,search,home" in text

    srv.shutdown()
    print("\n--- checks ---")
    for k, v in checks.items():
        print(k, v)
    ok = all(v["png"] and v["has_ui"] for v in checks.values()) and all(
        v.get(k + "_ok", True) for k, v in checks.items())
    print("UI PREVIEW:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
