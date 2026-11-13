#!/usr/bin/env python3
"""用本机 Edge/Chrome 无头模式对 FnPlayer 界面做可视化自检（离线 mock 数据）。

产出：dist/preview_home.png、dist/preview_settings.png、dist/preview_dom.txt
运行：python tests/ui_preview.py
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "kernel"))
DATA = Path(tempfile.mkdtemp(prefix="fnplayer-ui-"))
os.environ["FNPLAYER_DATA"] = str(DATA)
os.environ["FNPLAYER_PORT"] = "18982"

import uvicorn
from fastapi import FastAPI
from fastapi.responses import Response
from PIL import Image, ImageDraw

MOCK_PORT = 18981
APP_PORT = 18982
MOCK = f"http://127.0.0.1:{MOCK_PORT}"
APP = f"http://127.0.0.1:{APP_PORT}"
mock = FastAPI()

TITLES = ["星际穿越", "漫长的季节", "流浪地球", "三体", "繁花", "奥本海默",
          "千与千寻", "山海情", "大明王朝", "瞬息全宇宙", "沙丘", "看不见的客人"]


def poster(seed: int) -> bytes:
    img = Image.new("RGB", (300, 420))
    d = ImageDraw.Draw(img)
    a, b = (seed * 37) % 360, (seed * 37 + 60) % 360
    for y in range(420):
        t = y / 420
        d.line([(0, y), (300, y)], fill=(
            int(40 + 60 * t + a % 40), int(50 + 30 * t + b % 30),
            int(90 + 50 * (1 - t) + a % 25)))
    d.ellipse((90, 120, 210, 240), outline=(255, 255, 255, 120), width=3)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


POSTERS = [poster(i + 1) for i in range(len(TITLES))]


@mock.get("/cms/api.php/provide/vod/at/json")
async def cms(wd: str = "", pg: int = 1):
    names = [n for n in TITLES if not wd or wd in n] or TITLES[:4]
    lst = []
    for i, name in enumerate(names):
        lst.append({
            "vod_id": str(1000 + i), "vod_name": name,
            "type_name": ["剧情", "科幻", "悬疑"][i % 3],
            "vod_year": str(2019 + i % 6), "vod_area": "中国大陆",
            "vod_remarks": "更新至12集", "vod_score": str(round(7.1 + i * .13, 1)),
            "vod_pic": f"{MOCK}/poster/{i}.jpg",
            "vod_content": "这是一段用于界面自检的示例简介文本，展示详情区域的排版效果。",
            "vod_actor": "示例演员", "vod_director": "示例导演",
            "vod_play_from": "高清线路",
            "vod_play_url": f"第1集${MOCK}/hls/demo.m3u8#第2集${MOCK}/hls/demo.m3u8",
        })
    return {"code": 1, "list": lst}


@mock.get("/poster/{i}.jpg")
async def poster_img(i: int):
    return Response(content=POSTERS[i % len(POSTERS)], media_type="image/jpeg")


@mock.get("/hls/demo.m3u8")
async def demo_m3u8():
    return Response(content=(
        b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
        b"#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:4.0,\nseg0.ts\n#EXT-X-ENDLIST\n"),
        media_type="application/vnd.apple.mpegurl")


@mock.get("/hls/seg0.ts")
async def demo_seg():
    return Response(content=b"\x47" + b"\x00" * 187, media_type="video/mp2t")


# ------------------------------------------------------- mock 弹幕 API 服务
DANMU_BASE = MOCK + "/UITOKEN"


@mock.get("/UITOKEN/")
async def danmu_root():
    return Response(content=b"danmu api ui", media_type="text/plain")


@mock.post("/UITOKEN/api/v2/match")
async def danmu_match():
    return {"errorCode": 0, "success": True, "isMatched": True,
            "matches": [{"episodeId": 9001, "animeId": 9,
                         "animeTitle": "自检影片", "episodeTitle": "第1集",
                         "shift": 0, "type": "movie"}]}


@mock.get("/UITOKEN/api/v2/comment/{cid}")
async def danmu_comment(cid: str):
    return {"count": 4, "videoDuration": 120, "comments": [
        {"cid": 1, "p": "1.0,1,16777215,360", "m": "这条弹幕来自弹幕 API"},
        {"cid": 2, "p": "2.0,5,16488046,360", "m": "顶部彩色弹幕"},
        {"cid": 3, "p": "3.0,4,65280,360", "m": "底部绿色弹幕"},
        {"cid": 4, "p": "4.0,1,16744448,360", "m": "滚动橙色弹幕"},
    ]}


@mock.get("/UITOKEN/api/v2/search/anime")
async def danmu_search(keyword: str = ""):
    return {"errorCode": 0, "success": True, "animes": [
        {"animeId": 9, "animeTitle": "自检影片", "type": "movie",
         "typeDescription": "电影", "episodeCount": 1, "source": "360"}]}


@mock.get("/UITOKEN/api/v2/bangumi/{aid}")
async def danmu_bangumi(aid: str):
    return {"errorCode": 0, "success": True, "bangumi": {
        "animeId": 9, "animeTitle": "自检影片",
        "episodes": [{"episodeId": 9001, "episodeTitle": "第1集",
                      "episodeNumber": "1", "url": ""}]}}


def preview_page(action: str, src: str = "/") -> str:
    """生成同源 iframe 包裹页，用脚本自动触发详情 / 播放 / 批量导入，供无头截图。"""
    step = {
        "detail": "w.openDetail(card.dataset.id);",
        "player": ("w.openDetail(card.dataset.id);"
                   "setTimeout(function(){var b=w.document.getElementById('detailPlay');"
                   "if(b)b.click();},400);"
                   "setTimeout(function(){w.$('playerOverlay').hidden=true;"
                   "w.$('playerError').hidden=true;"
                   "w.$('timeLabel').textContent='02:35 / 46:20';"
                   "w.document.getElementById('played').style.width='22%';},900);"),
        "settings_bulk": (
            "var box=w.document.querySelector('.bulk-box'); if(box)box.open=true;"
            "var ta=w.document.getElementById('bulkText');"
            "if(ta)ta.value='我的媒体库,https://my-nas.example/api.php/provide/vod/at/json\\n"
            "https://another.example/api.php/provide/vod/at/json';"
            "var m=w.document.getElementById('bulkMsg'); if(m)m.textContent='新增 2 条';"),
        # 真实点击「导入」，用于 DOM 断言（结果回写到父页 title，见 preview_bulk_dom.txt）
        "settings_import": (
            "var box=w.document.querySelector('.bulk-box'); if(box)box.open=true;"
            "var ta=w.document.getElementById('bulkText');"
            "if(ta)ta.value='UI导入源,'+location.origin+'/cms/api.php/provide/vod/at/json';"
            "setTimeout(function(){var b=w.document.getElementById('bulkBtn');"
            "if(b)b.click();},300);"
            "setTimeout(function(){"
            "var m=w.document.getElementById('bulkMsg').textContent;"
            "var rows=w.document.querySelectorAll('#srcTable tbody tr').length;"
            "document.title='IMPORT|'+m+'|rows='+rows;},2200);"),
        # 播放页弹幕：开播后自动匹配，再注入若干本地弹幕用于可视化与 DOM 断言
        "danmu": (
            "w.openDetail(card.dataset.id);"
            "setTimeout(function(){var b=w.document.getElementById('detailPlay');"
            "if(b)b.click();},400);"
            "setTimeout(function(){"
            "var d=w.document;"
            "d.getElementById('playerOverlay').hidden=true;"
            "d.getElementById('playerError').hidden=true;"
            "d.getElementById('timeLabel').textContent='00:06 / 02:00';"
            "d.getElementById('played').style.width='5%';"
            "d.getElementById('btnDanmuPanel').click();"
            "w.eval(\"S.danmu.setEnabled(true);"
            "['弹幕引擎已接入','本地发送一条','上屏正常 ✓','测试通过']."
            "forEach(function(x,i){S.danmu.send(x,['#ffffff','#ffd54a','#8de0b0','#7fb4ff'][i]);});\");"
            "},1000);"
            "setTimeout(function(){"
            "var status=w.document.getElementById('dpStatus').textContent;"
            "var total=w.eval('S.danmu ? S.danmu.count : -1');"
            "var active=w.eval('S.danmu ? S.danmu.activeCount : -1');"
            "var enabled=w.eval('S.danmu ? S.danmu.enabled : false');"
            "document.title='DANMU|'+status+'|total='+total+'|active='+active+'|on='+enabled;"
            "},2000);"),
    }[action]
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
    html,body{{margin:0;background:#0e1016}} iframe{{width:1380px;height:900px;border:0;display:block}}
    </style></head><body><iframe id="f" src="{src}"></iframe><script>
    var f=document.getElementById('f');
    f.onload=function(){{setTimeout(function(){{
      var w=f.contentWindow; var card=w.document.querySelector('.card');
      if(card || '{action}'.indexOf('settings_')===0){{{step}}}
    }},1400);}};
    </script></body></html>"""


def run(app, port):
    s = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                      log_level="error", access_log=False))
    threading.Thread(target=s.run, daemon=True).start()
    for _ in range(120):
        if s.started:
            return s
        time.sleep(0.05)
    raise RuntimeError("server not started")


def browser() -> str:
    # 无头自检优先 Chrome：部分受限会话中 Edge 的 headless 不回显输出
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"):
        if Path(p).is_file():
            return p
    raise RuntimeError("未找到 Edge/Chrome")


def chrome_args(profile: Path) -> list[str]:
    return ["--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
            "--no-first-run", "--no-default-browser-check",
            f"--user-data-dir={profile}", "--virtual-time-budget=6000",
            "--force-prefers-reduced-motion=reduce"]


def shot(exe: str, url: str, out: Path, dump: Path | None = None,
         height: int = 900):
    profile = DATA / ("profile-" + out.stem)
    common = chrome_args(profile)
    r = subprocess.run([exe] + common + [f"--window-size=1380,{height}",
                                         f"--screenshot={out}", url],
                       capture_output=True, timeout=90)
    print(f"  screenshot {out.name}: rc={r.returncode} err={r.stderr[:120]!r}")
    if dump is not None:
        dom = subprocess.run([exe] + common + ["--dump-dom", url],
                             capture_output=True, timeout=90)
        dump.write_bytes(dom.stdout)
    return out.exists() and out.stat().st_size > 1000


def dump_page(exe: str, url: str, tag: str) -> str:
    dom = subprocess.run([exe] + chrome_args(DATA / ("profile-" + tag))
                         + ["--dump-dom", url], capture_output=True, timeout=90)
    return dom.stdout.decode("utf-8", "replace")


def main() -> int:
    import server
    (DATA / "config.json").write_text(json.dumps({
        "sources": [{"id": "ui_demo", "name": "本地演示接口",
                     "api": MOCK + "/cms/api.php/provide/vod/at/json",
                     "enabled": True}],
        "theme": "dark",
        "danmu_api": DANMU_BASE, "danmu_enabled": True, "danmu_auto": True,
        "danmu_opacity": 0.95, "danmu_font_size": 24,
        "danmu_area": 0.75, "danmu_speed": 8, "danmu_offset": 0,
    }, ensure_ascii=False), encoding="utf-8")
    ms = run(mock, MOCK_PORT)
    fs = run(server.app, APP_PORT)
    # 让设置页展示「局域网已开启」状态；回环请求始终免访问码，不影响其它截图
    server.set_lan_state(True, APP_PORT)
    server.cfg().save({"lan_enabled": True, "lan_port": APP_PORT,
                       "lan_password": "demo9527"})
    out = ROOT / "dist"
    out.mkdir(exist_ok=True)
    exe = browser()
    home = shot(exe, APP + "/", out / "preview_home.png",
                dump=out / "preview_dom.txt")
    settings = shot(exe, APP + "/?view=settings", out / "preview_settings.png",
                    dump=out / "preview_settings_dom.txt", height=2000)
    web = ROOT / "app" / "web"
    detail_html = web / "__preview_detail.html"
    player_html = web / "__preview_player.html"
    bulk_html = web / "__preview_bulk.html"
    import_html = web / "__preview_import.html"
    danmu_html = web / "__preview_danmu.html"
    detail_html.write_text(preview_page("detail"), encoding="utf-8")
    player_html.write_text(preview_page("player"), encoding="utf-8")
    bulk_html.write_text(preview_page("settings_bulk", "/?view=settings"),
                         encoding="utf-8")
    import_html.write_text(preview_page("settings_import", "/?view=settings"),
                           encoding="utf-8")
    danmu_html.write_text(preview_page("danmu"), encoding="utf-8")
    try:
        detail = shot(exe, APP + "/static/__preview_detail.html",
                      out / "preview_detail.png")
        player = shot(exe, APP + "/static/__preview_player.html",
                      out / "preview_player.png")
        bulk = shot(exe, APP + "/static/__preview_bulk.html",
                    out / "preview_bulk.png")
        idom = dump_page(exe, APP + "/static/__preview_import.html", "import")
        (out / "preview_bulk_dom.txt").write_text(idom, encoding="utf-8")
        dshot = shot(exe, APP + "/static/__preview_danmu.html",
                     out / "preview_danmu.png")
        ddom = dump_page(exe, APP + "/static/__preview_danmu.html", "danmu")
        (out / "preview_danmu_dom.txt").write_text(ddom, encoding="utf-8")
    finally:
        for f in (detail_html, player_html, bulk_html, import_html, danmu_html):
            f.unlink(missing_ok=True)
    dom = (out / "preview_dom.txt").read_text(encoding="utf-8") if (
        out / "preview_dom.txt").exists() else ""
    imported = "IMPORT|" in idom and "新增 1 条" in idom and "rows=2" in idom
    danmu_ok = ("DANMU|" in ddom and "已加载 4 条弹幕" in ddom
                and "active=" in ddom and "on=true" in ddom)
    sdom = (out / "preview_settings_dom.txt").read_text(encoding="utf-8") if (
        out / "preview_settings_dom.txt").exists() else ""
    service_ui = all(x in sdom for x in (
        "本地弹幕服务", "setDanmuService", "svcBadge", "svcRestartBtn",
        "config/config.json"))
    lan_ui = all(x in sdom for x in (
        "局域网访问", "lanApplyBtn", "setLanKey", "已开放局域网"))
    print("首页截图:", "OK" if home else "FAIL")
    print("设置页截图:", "OK" if settings else "FAIL")
    print("详情页截图:", "OK" if detail else "FAIL")
    print("播放器截图:", "OK" if player else "FAIL")
    print("批量导入截图:", "OK" if bulk else "FAIL")
    print("点击导入生效:", "OK" if imported else "FAIL",
          "" if imported else "(见 preview_bulk_dom.txt)")
    print("弹幕页面截图:", "OK" if dshot else "FAIL")
    print("弹幕自动匹配与渲染:", "OK" if danmu_ok else "FAIL",
          "" if danmu_ok else "(见 preview_danmu_dom.txt)")
    print("本地弹幕服务开关渲染:", "OK" if service_ui else "FAIL")
    print("局域网面板渲染:", "OK" if lan_ui else "FAIL")
    print("海报卡片已渲染:", dom.count('class="card"'))
    print("发现页标题:", "发现" in dom)
    print("预览文件:", out)
    fs.should_exit = True
    ms.should_exit = True
    time.sleep(0.3)
    return 0 if all((home, settings, detail, player, bulk, imported,
                     dshot, danmu_ok, service_ui, lan_ui,
                     dom.count('class="card"') > 0)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
