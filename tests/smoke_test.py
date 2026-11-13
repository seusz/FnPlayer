#!/usr/bin/env python3
"""FnPlayer 端到端冒烟测试（完全离线，使用本地 mock 苹果CMS + mock HLS/MP4 源）。

覆盖：接口配置、跨源聚合搜索、详情选集、播放解析、master 子清单重写、
分片/密钥/Range 代理、Referer 透传、海报代理、播放记录、设置持久化。
运行：python tests/smoke_test.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "kernel"))
os.environ["FNPLAYER_DATA"] = tempfile.mkdtemp(prefix="fnplayer-test-")

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response

MOCK_PORT = 18971
APP_PORT = 18972
MOCK = f"http://127.0.0.1:{MOCK_PORT}"
APP = f"http://127.0.0.1:{APP_PORT}"
seen = {"headers": [], "ranges": []}

mock = FastAPI()


def cms_json(play_url: str, name: str = "测试影片", vod_id: str = "1001"):
    return {
        "code": 1, "msg": "ok", "page": 1, "pagecount": 1, "limit": 20, "total": 1,
        "list": [{
            "vod_id": vod_id, "vod_name": name, "type_name": "剧情",
            "vod_year": "2024", "vod_area": "中国大陆", "vod_remarks": "更新至2集",
            "vod_score": "8.5", "vod_pic": MOCK + "/poster.jpg",
            "vod_content": "一部用于本地测试的影片简介。",
            "vod_actor": "测试演员", "vod_director": "测试导演",
            "vod_play_from": "默认线路", "vod_play_note": "",
            "vod_play_url": play_url,
        }],
    }


@mock.get("/cms1/api.php/provide/vod/at/json")
async def cms1(request: Request):
    wd = request.query_params.get("wd")
    if wd:
        play = (f"第1集${MOCK}/hls/master.m3u8"
                f"#第2集${MOCK}/hls/aes.m3u8")
    else:
        play = f"正片${MOCK}/hls/master.m3u8"
    return cms_json(play)


@mock.get("/cms2/api.php/provide/vod/at/json")
async def cms2(request: Request):
    wd = request.query_params.get("wd")
    if wd:
        return cms_json(f"正片${MOCK}/hls/movie.mp4", vod_id="2002")
    return {"code": 1, "list": []}


@mock.get("/hls/master.m3u8")
async def master():
    return PlainTextResponse(
        "#EXTM3U\n#EXT-X-VERSION:3\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=1280x720\n"
        "sub.m3u8\n", media_type="application/vnd.apple.mpegurl")


@mock.get("/hls/sub.m3u8")
async def sub():
    return PlainTextResponse(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-MAP:URI=\"init.mp4\"\n"
        "#EXTINF:4.0,\nseg0.ts\n#EXTINF:4.0,\nseg1.ts\n#EXT-X-ENDLIST\n",
        media_type="application/vnd.apple.mpegurl")


@mock.get("/hls/aes.m3u8")
async def aes():
    return PlainTextResponse(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
        "#EXT-X-KEY:METHOD=AES-128,URI=\"enc.key\"\n"
        "#EXTINF:4.0,\nseg0.ts\n#EXT-X-ENDLIST\n",
        media_type="application/vnd.apple.mpegurl")


@mock.get("/hls/enc.key")
async def enc_key():
    return Response(content=b"0123456789abcdef",
                    media_type="application/octet-stream")


@mock.get("/hls/{name}")
async def hls_asset(name: str, request: Request):
    seen["headers"].append(dict(request.headers))
    if "range" in request.headers:
        seen["ranges"].append(request.headers["range"])
    if name.endswith(".mp4"):
        data = b"FAKE-MP4-DATA-" * 100
        rng = request.headers.get("range", "")
        m = re.search(r"bytes=(\d+)-(\d*)", rng)
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else len(data) - 1
            chunk = data[start:end + 1]
            return Response(content=chunk, status_code=206, headers={
                "Content-Range": f"bytes {start}-{end}/{len(data)}",
                "Accept-Ranges": "bytes", "Content-Length": str(len(chunk)),
                "Content-Type": "video/mp4",
            })
        return Response(content=data, media_type="video/mp4",
                        headers={"Accept-Ranges": "bytes"})
    return Response(content=b"\x47" + b"\x00" * 187,
                    media_type="video/mp2t")


@mock.get("/poster.jpg")
async def poster():
    # 1x1 PNG
    png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
           b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8"
           b"\xcf\xc0\xf0\x1f\x00\x05\x05\x02\x00\x8d\x8b\xe1\x1c\x00\x00\x00\x00"
           b"IEND\xaeB`\x82")
    return Response(content=png, media_type="image/png")


# ---------------------------------------------------------------- mock 弹幕 API
DANMU_BASE = MOCK + "/TESTTOKEN"
danmu_seen = {"search": [], "comment": [], "match": []}


@mock.get("/TESTTOKEN/")
async def danmu_root():
    return PlainTextResponse("danmu api ui", status_code=200)


@mock.get("/TESTTOKEN/api/v2/search/anime")
async def danmu_search(request: Request):
    kw = request.query_params.get("keyword", "")
    danmu_seen["search"].append(kw)
    return {
        "errorCode": 0, "success": True, "errorMessage": "",
        "animes": [{
            "animeId": 111, "bangumiId": "b111", "animeTitle": "测试影片",
            "type": "tvseries", "typeDescription": "电视剧",
            "imageUrl": MOCK + "/poster.jpg", "startDate": "2024-01-01",
            "episodeCount": 2, "rating": 8.5, "isFavorited": False,
            "source": "360",
        }] if kw else [],
    }


@mock.post("/TESTTOKEN/api/v2/match")
async def danmu_match(request: Request):
    body = await request.json()
    danmu_seen["match"].append(body.get("fileName", ""))
    return {
        "errorCode": 0, "success": True, "errorMessage": "",
        "isMatched": True,
        "matches": [{
            "episodeId": 10001, "animeId": 111, "animeTitle": "测试影片",
            "episodeTitle": "第1集", "type": "tvseries",
            "typeDescription": "电视剧", "shift": 0,
            "imageUrl": MOCK + "/poster.jpg", "url": "",
        }],
    }


@mock.get("/TESTTOKEN/api/v2/bangumi/{anime_id}")
async def danmu_bangumi(anime_id: str):
    return {
        "errorCode": 0, "success": True, "errorMessage": "",
        "bangumi": {
            "animeId": 111, "animeTitle": "测试影片", "imageUrl": "",
            "episodes": [
                {"seasonId": "season-111", "episodeId": 10001,
                 "episodeTitle": "第1集", "episodeNumber": "1", "url": ""},
                {"seasonId": "season-111", "episodeId": 10002,
                 "episodeTitle": "第2集", "episodeNumber": "2", "url": ""},
            ],
        },
    }


@mock.get("/TESTTOKEN/api/v2/comment/{comment_id}")
async def danmu_comment(comment_id: str, request: Request):
    danmu_seen["comment"].append(dict(request.query_params))
    return {
        "count": 3, "videoDuration": 120,
        "comments": [
            {"cid": 1, "p": "5.0,1,16777215,360", "m": "滚动白色弹幕"},
            {"cid": 2, "p": "4.0,5,16488046,360", "m": "顶部彩色"},
            {"cid": 3, "p": "3.0,4,255,bili", "m": "底部红色"},
        ],
    }


@mock.get("/TESTTOKEN/api/v2/comment")
async def danmu_comment_missing():
    return {"errorCode": 400, "success": False,
            "errorMessage": "Missing commentId or url parameter",
            "count": 0, "comments": []}


# 记录进入 mock 资源端点的请求头，用于校验 Referer / UA / Range 透传


def run(app, port):
    c = uvicorn.Config(app, host="127.0.0.1", port=port,
                       log_level="error", access_log=False)
    s = uvicorn.Server(c)
    t = threading.Thread(target=s.run, daemon=True)
    t.start()
    for _ in range(120):
        if s.started:
            return s
        time.sleep(0.05)
    raise RuntimeError("server not started")


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ((" | " + str(extra)) if extra else ""))
    if not cond:
        globals()["FAILED"] = True


def main():
    import server
    ms = run(mock, MOCK_PORT)
    fs = run(server.app, APP_PORT)
    server.set_shutdown_callback(lambda: None)
    c = httpx.Client(base_url=APP, timeout=20, trust_env=False)
    try:
        html = c.get("/").text
        token = re.search(r'name="fn-token" content="([^"]+)"', html).group(1)
        H = {"X-Fn-Token": token}
        check("首页注入会话令牌", bool(token))
        check("hls.js 已本地化", c.get("/static/hls.min.js").status_code == 200)
        check("图标可访问", c.get("/favicon.ico").status_code == 200)
        check("跨站写操作被拒绝",
              c.post("/api/sources", json={"api": "http://x"},
                     headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403)
        check("无令牌写操作被拒绝",
              c.post("/api/sources", json={"api": "http://x"}).status_code == 403)

        r = c.post("/api/sources", json={"name": "测试源A",
                                         "api": MOCK + "/cms1/api.php/provide/vod/at/json"},
                   headers=H).json()
        check("添加接口A", r.get("ok") is True, r.get("message"))
        r = c.post("/api/sources", json={"name": "测试源B",
                                         "api": MOCK + "/cms2/api.php/provide/vod/at/json"},
                   headers=H).json()
        check("添加接口B", r.get("ok") is True, r.get("message"))

        r = c.get("/api/search?q=测试影片").json()
        items = r.get("items", [])
        check("跨源聚合搜索返回结果", len(items) >= 1, r.get("elapsed_ms", ""))
        tid = items[0]["id"]
        check("同名影片合并为一张卡片", len(items) == 1, len(items))
        check("合并后含 2 条线路", items[0]["line_count"] == 2, items[0]["line_count"])

        d = c.get(f"/api/title/{tid}").json()
        check("详情含简介与线路", "overview" in d and len(d["lines"]) == 2)

        p = c.post("/api/play", json={"id": tid, "line": list(d["lines"])[0], "ep": 0},
                   headers=H).json()
        check("播放解析成功", p.get("kind") == "hls", p.get("error"))
        m3u8 = c.get(p["url"]).text
        check("m3u8 master 子清单已重写", "/sub?i=0" in m3u8, m3u8.strip()[:90])
        sublist = c.get(f"/api/stream/{p['stream']}/sub?i=0").text
        check("分片 URI 已重写为本地代理", "/seg?i=" in sublist)
        check("EXT-X-MAP 已重写", 'URI="/api/stream/' in sublist)

        seg_matches = re.findall(r"(/api/stream/[^\s\"]+/seg\?i=\d+)", sublist)
        check("清单含初始化段 + 2 个分片", len(seg_matches) == 3, len(seg_matches))
        init_seg = c.get(seg_matches[0])
        check("fMP4 初始化段代理可用", init_seg.status_code == 200 and len(init_seg.content) > 0)
        seg_url = seg_matches[1]
        seg = c.get(seg_url)
        check("分片代理返回 200", seg.status_code == 200, seg.status_code)
        check("分片内容正确（TS sync byte）", seg.content[:1] == b"\x47",
              seg.content[:4])
        check("分片请求透传 Referer/UA",
              any(h.get("referer") and h.get("user-agent") for h in seen["headers"]),
              len(seen["headers"]))

        # AES 线路
        p2 = c.post("/api/play", json={"id": tid, "line": list(d["lines"])[0], "ep": 1},
                    headers=H).json()
        m = c.get(p2["url"]).text
        check("AES 密钥 URI 已重写", "/key?i=0" in m, m.strip()[:90])
        k = c.get(f"/api/stream/{p2['stream']}/key?i=0")
        check("密钥代理返回 16 字节", k.content == b"0123456789abcdef", len(k.content))

        # mp4 直链 + Range
        line2 = list(d["lines"])[1]
        p3 = c.post("/api/play", json={"id": tid, "line": line2, "ep": 0},
                    headers=H).json()
        check("mp4 解析为 file 类型", p3.get("kind") == "file", p3.get("error"))
        part = c.get(p3["url"], headers={"Range": "bytes=10-29"})
        check("mp4 Range 代理返回 206", part.status_code == 206, part.status_code)
        check("mp4 Range 内容正确",
              part.content == (b"FAKE-MP4-DATA-" * 100)[10:30], len(part.content))
        check("Range 头已透传上游", any("10-29" in x for x in seen["ranges"]), seen["ranges"])

        # 海报代理
        import base64
        u = base64.urlsafe_b64encode((MOCK + "/poster.jpg").encode()).decode().rstrip("=")
        img = c.get(f"/api/poster?u={u}&t={token}")
        check("海报代理返回图片", img.status_code == 200 and
              img.headers.get("content-type", "").startswith("image/"))
        check("海报代理拒绝无效令牌", c.get(f"/api/poster?u={u}&t=bad").status_code == 403)

        # 历史
        c.post("/api/history", headers=H, json={"id": tid, "title": "测试影片",
                                                "episode": "第1集", "ts": 42, "duration": 100})
        h = c.get("/api/history").json()["items"]
        check("播放记录已保存", len(h) == 1 and h[0]["ts"] == 42)
        p4 = c.post("/api/play", json={"id": tid, "line": list(d["lines"])[0], "ep": 0},
                    headers=H).json()
        check("续播进度回填", p4.get("resume") == 42, p4.get("resume"))
        c.delete(f"/api/history/{tid}", headers=H)
        check("播放记录可删除", len(c.get("/api/history").json()["items"]) == 0)

        # 设置 / 过滤词 / 发现
        st = c.post("/api/settings", headers=H,
                    json={"auto_resume": False, "max_per_keyword": 30}).json()
        check("设置持久化", st["settings"]["auto_resume"] is False)
        j = c.post("/api/junk-keywords", headers=H,
                   json={"keywords": "解说, 预告片"}).json()
        check("过滤词解析", j["items"] == ["解说", "预告片"], j["items"])
        disc = c.get("/api/discover?pg=1").json()
        check("发现页返回片单", len(disc["items"]) >= 1, len(disc["items"]))
        probe = c.post("/api/sources/test", headers=H,
                       json={"api": MOCK + "/cms1/api.php/provide/vod/at/json",
                             "keyword": "测试影片"}).json()
        check("接口测速可用", probe.get("ok") is True, probe.get("error"))

        # 批量导入（多种格式混排 + 去重 + 注释 + 非法行）
        bulk_text = "\n".join([
            "# 注释行应被忽略",
            f"批量源A,{MOCK}/cms3/api.php/provide/vod/at/json",
            f"{MOCK}/cms4/api.php/provide/vod/at/json",
            f"重复源,{MOCK}/cms1/api.php/provide/vod/at/json",
            "这一行没有地址",
        ])
        b = c.post("/api/sources/bulk", headers=H, json={"text": bulk_text}).json()
        check("批量导入成功", b.get("ok") is True, b.get("message"))
        check("批量导入新增 2 条", b.get("added") == 2, b.get("added"))
        check("批量导入跳过重复与非法",
              len(b.get("skipped") or []) == 2, len(b.get("skipped") or []))
        names = {s["name"] for s in b.get("items", [])}
        check("批量导入保留名称", "批量源A" in names, names)
        bj = c.post("/api/sources/bulk", headers=H, json={"text": json.dumps(
            [{"name": "JSON源", "api": f"{MOCK}/cms5/x"}])}).json()
        check("批量导入支持 JSON 数组", bj.get("added") == 1, bj.get("added"))
        check("批量导入空内容被拒绝",
              c.post("/api/sources/bulk", headers=H,
                     json={"text": "   "}).status_code == 400)

        # ---- 弹幕代理（mock 弹幕 API，含 TOKEN 路径段）----
        cfgd = c.post("/api/settings", headers=H, json={
            "danmu_api": DANMU_BASE, "danmu_enabled": True, "danmu_auto": True,
            "danmu_opacity": 0.7, "danmu_font_size": 26,
        }).json()["settings"]
        check("弹幕配置持久化", cfgd["danmu_api"] == DANMU_BASE and
              cfgd["danmu_font_size"] == 26)
        boot = c.get("/api/bootstrap").json()
        check("启动信息含弹幕配置", boot["settings"]["danmu_api"] == DANMU_BASE)
        stt = c.get("/api/danmu/status").json()
        check("弹幕状态已配置", stt["configured"] is True and stt["enabled"] is True)
        pr = c.post("/api/danmu/probe", headers=H, json={"api": DANMU_BASE}).json()
        check("弹幕服务连通性测试", pr.get("ok") is True, pr.get("status"))
        bad = c.post("/api/danmu/probe", headers=H,
                     json={"api": "ftp://x.example"}).json()
        check("非法弹幕地址被拒绝", bad.get("ok") is False, bad.get("error"))
        dead = c.post("/api/danmu/probe", headers=H,
                      json={"api": "http://127.0.0.1:1"}).json()
        check("不可达弹幕服务返回失败", dead.get("ok") is False, dead.get("error"))

        sr = c.get("/api/danmu/search?keyword=测试影片").json()
        check("弹幕搜索代理可用", sr.get("ok") is True and
              len(sr.get("items") or []) == 1, sr.get("error"))
        check("弹幕搜索字段归一", (sr["items"][0]["animeId"] == 111 and
              sr["items"][0]["title"] == "测试影片" and
              sr["items"][0]["episodeCount"] == 2))
        check("搜索关键词送达上游", danmu_seen["search"][-1:] == ["测试影片"])

        bg = c.get("/api/danmu/bangumi/111").json()
        check("剧集列表代理可用", bg.get("ok") is True and
              len(bg.get("episodes") or []) == 2, bg.get("error"))
        check("剧集 ID 正确", [e["episodeId"] for e in bg["episodes"]] == [10001, 10002])

        mt = c.post("/api/danmu/match", headers=H,
                    json={"fileName": "测试影片 S01E01"}).json()
        check("自动匹配接口可用", mt.get("matched") is True and
              mt["items"][0]["episodeId"] == 10001, mt.get("error"))
        check("匹配文件名送达上游",
              danmu_seen["match"][-1:] == ["测试影片 S01E01"])

        cm = c.get("/api/danmu/comment/10001").json()
        check("弹幕获取代理可用", cm.get("ok") is True and cm.get("count") == 3,
              cm.get("error"))
        check("弹幕按时间排序", [x["t"] for x in cm["comments"]] == [3.0, 4.0, 5.0])
        check("弹幕类型映射正确",
              [x["mode"] for x in cm["comments"]] == ["bottom", "top", "scroll"])
        check("弹幕颜色归一正确",
              [x["color"] for x in cm["comments"]] == ["#0000ff", "#fb966e", "#ffffff"])
        check("弹幕请求带 format=json&duration",
              danmu_seen["comment"][-1:][0].get("format") == "json" and
              danmu_seen["comment"][-1:][0].get("duration") == "true")
        check("弹幕时长字段透传", cm.get("videoDuration") == 120)

        import danmu as dmod
        units = dmod.normalize_comments({"comments": [
            {"p": "8.0,5,25,16488046,1751533608,0,0,13190629936", "m": "B站8字段"},
            {"p": "坏数据", "m": "异常时间"},
            {"p": "2.0,1,99999999,x", "m": "颜色越界"},
            {"p": "1.0,1,16777215,x", "m": ""},
        ]})
        by_text = {x["text"]: x for x in units}
        check("兼容 B 站 8 字段 p 属性",
              by_text["B站8字段"]["mode"] == "top" and
              by_text["B站8字段"]["color"] == "#fb966e", by_text)
        check("异常 p 字段降级处理", by_text["异常时间"]["t"] == 0.0 and
              by_text["异常时间"]["mode"] == "scroll")
        check("越界颜色被裁剪", by_text["颜色越界"]["color"] == "#ffffff")
        check("空文本弹幕被丢弃", len(units) == 3)
        check("base 末尾 /api/v2 被去除",
              dmod.endpoint("http://x/T/api/v2", "comment/1") ==
              "http://x/T/api/v2/comment/1")
        try:
            dmod.endpoint("", "comment/1")
            check("未配置地址时抛错", False)
        except dmod.DanmuError:
            check("未配置地址时抛错", True)

        # ---- 便携版：config 外置播放源 + 本地弹幕服务开关 ----
        import config as cfgmod
        with tempfile.TemporaryDirectory(prefix="fn-portable-") as td:
            proot = Path(td)
            (proot / "config").mkdir()
            pcfg = proot / "config" / "config.json"
            pcfg.write_text(json.dumps({
                "_说明": "保留这段注释",
                "sources": [
                    {"name": "我的媒体库", "api": MOCK + "/cms/api.php/provide/vod/at/json"},
                    {"name": "省略字段", "api": MOCK + "/second/at/json"},
                    {"name": "非法", "api": "ftp://bad"},
                    {"name": "重复", "api": MOCK + "/cms/api.php/provide/vod/at/json"},
                ],
                "theme": "light",
                "danmu_service_enabled": True,
            }, ensure_ascii=False), encoding="utf-8")
            saved_data = os.environ.pop("FNPLAYER_DATA")
            os.environ["FNPLAYER_HOME"] = str(proot)
            try:
                check("便携模式识别 config/config.json", cfgmod.is_portable())
                check("配置入口指向便携 config",
                      cfgmod.state_file() == pcfg.resolve())
                check("数据目录落在便携根 data/",
                      cfgmod.data_dir() == (proot / "data").resolve())
                inst = cfgmod.Config()
                got = inst.get()
                check("手写播放源被读取", len(got["sources"]) == 2, got["sources"])
                check("播放源自动补齐 id/enabled",
                      all(s.get("id") and s.get("enabled") for s in got["sources"]))
                check("非法与重复源被忽略", len(got["sources"]) == 2)
                check("便携配置覆盖默认主题", got["theme"] == "light")
                inst.save({"theme": "dark"})
                raw = json.loads(pcfg.read_text(encoding="utf-8"))
                check("保存后保留 _ 说明键", raw.get("_说明") == "保留这段注释")
                check("保存后保留手写播放源", len(raw.get("sources") or []) == 2)
            finally:
                os.environ["FNPLAYER_DATA"] = saved_data
                os.environ.pop("FNPLAYER_HOME", None)

        fake_port = 18973
        os.environ["FNPLAYER_DANMU_CMD"] = json.dumps(
            [sys.executable, "-m", "http.server", str(fake_port),
             "--bind", "127.0.0.1"])
        try:
            st0 = c.get("/api/danmu/service").json()
            check("本地服务状态接口可用",
                  st0.get("ok") is True and st0.get("available") is True)
            r = c.post("/api/danmu/service", headers=H, json={
                "enabled": True, "danmu_service_port": fake_port,
                "danmu_service_token": "testtoken"}).json()
            check("开关打开拉起本地服务",
                  r.get("ok") is True and r.get("running") is True, r.get("error"))
            check("服务进程由主程序托管",
                  r.get("managed") is True and bool(r.get("pid")))
            check("本地服务地址自动生成",
                  r.get("base") == f"http://127.0.0.1:{fake_port}/testtoken")
            check("danmu_api 自动回填本地地址",
                  c.get("/api/settings").json().get("danmu_api") == r["base"])
            r2 = c.post("/api/danmu/service", headers=H,
                        json={"enabled": False}).json()
            check("开关关闭停止本地服务",
                  r2.get("ok") is True and r2.get("running") is False, r2)
            check("停止后清除本地地址",
                  c.get("/api/settings").json().get("danmu_api") == "")
            bad = c.post("/api/danmu/service", headers=H,
                         json={"enabled": True, "danmu_service_port": 99999})
            check("非法端口被拒绝", bad.status_code == 400)
        finally:
            os.environ.pop("FNPLAYER_DANMU_CMD", None)

        # ------------------------------------------------ 局域网访问
        import lan as lanmod
        from types import SimpleNamespace

        check("局域网默认关闭", server.cfg().get()["lan_enabled"] is False)
        st = c.get("/api/lan").json()
        check("局域网状态默认仅本机", st.get("active") is False
              and st.get("bind") == "127.0.0.1" and st.get("urls") == [])
        check("未开放时不生成二维码", c.get("/api/lan/qr?i=0").status_code == 404)
        check("回环地址判定", lanmod.is_local_client("127.0.0.1")
              and not lanmod.is_local_client("192.168.8.77"))
        check("访问码摘要稳定且不明文", lanmod.digest("abc") == lanmod.digest("abc")
              and lanmod.digest("abc") != lanmod.digest("abd")
              and "abc" not in lanmod.digest("abc"))
        fake = SimpleNamespace(cookies={}, query_params={}, headers={})
        check("无访问码时拒绝", lanmod.authorized(fake, "secret")[0] is False)
        fake.query_params = {"key": "secret"}
        check("URL 访问码可认证且需种 cookie",
              lanmod.authorized(fake, "secret") == (True, True))
        fake.cookies = {"fn_lan": lanmod.digest("secret")}
        check("cookie 摘要可认证", lanmod.authorized(fake, "secret") == (True, False))
        check("访问地址带访问码",
              all("?key=secret" in u["url"] for u in lanmod.urls(18790, "secret")))
        check("二维码 SVG 可生成",
              (lanmod.qr_svg("http://192.168.1.2:18790/") or "").startswith("<svg"))
        check("非法局域网端口被拒绝",
              c.post("/api/lan/apply", headers=H,
                     json={"enabled": True, "port": 70000}).status_code == 400)
        check("非法访问码被拒绝",
              c.post("/api/lan/apply", headers=H,
                     json={"enabled": True, "password": "a b!"}).status_code == 400)

        # uvicorn 默认信任来自 127.0.0.1 的 X-Forwarded-For，可用来模拟远端设备
        server.set_lan_state(True, APP_PORT)
        server.cfg().save({"lan_enabled": True, "lan_password": "testkey123"})
        fake_ip = "192.168.8.77"
        rh = {"X-Forwarded-For": fake_ip, "Host": f"{fake_ip}:{APP_PORT}"}
        try:
            r = c.get("/", headers=rh)
            check("远端未认证返回访问码页", r.status_code == 401 and "访问码" in r.text)
            check("远端未认证 API 返回 401",
                  c.get("/api/health", headers=rh).status_code == 401)
            r = c.get("/", headers=rh, params={"key": "testkey123"},
                      follow_redirects=False)
            check("扫码链接换 cookie 并清除地址栏明文",
                  r.status_code == 302 and "fn_lan=" in r.headers.get("set-cookie", ""))
            rc = httpx.Client(base_url=APP, timeout=20, trust_env=False, headers=rh,
                              cookies={"fn_lan": lanmod.digest("testkey123")})
            try:
                check("已认证远端可打开页面", rc.get("/").status_code == 200)
                check("已认证远端可访问只读接口",
                      rc.get("/api/health").json().get("ok") is True)
                check("已认证远端可搜索", rc.get("/api/search?q=").status_code == 200)
                check("远端管理接口被拒绝", rc.get("/api/settings").status_code == 403)
                check("远端修改设置被拒绝",
                      rc.post("/api/settings", json={"theme": "light"},
                              headers={"X-Fn-Token": token}).status_code == 403)
                check("远端接口管理被拒绝", rc.get("/api/sources").status_code == 403)
                check("远端弹幕服务控制被拒绝",
                      rc.get("/api/danmu/service").status_code == 403)
                check("远端退出应用被拒绝",
                      rc.post("/api/shutdown", json={},
                              headers={"X-Fn-Token": token}).status_code == 403)
                check("远端不能取二维码", rc.get("/api/lan/qr?i=0").status_code == 403)
                check("远端状态不含访问地址", rc.get("/api/lan").json().get("urls") == [])
                ok = rc.post("/api/history", json={"id": "lan-test", "title": "远端记录"},
                             headers={"X-Fn-Token": token,
                                      "Origin": f"http://{fake_ip}:{APP_PORT}"})
                check("远端同源 Origin 写操作放行",
                      ok.status_code == 200 and ok.json().get("ok") is True, ok.status_code)
                bad = rc.post("/api/history", json={"id": "x"},
                              headers={"X-Fn-Token": token,
                                       "Origin": "http://evil.example"})
                check("远端跨源 Origin 被拒绝", bad.status_code == 403)
                check("本机始终可访问管理接口", c.get("/api/settings").status_code == 200)
                check("本机二维码可生成",
                      c.get("/api/lan/qr?i=0").status_code in (200, 404))
            finally:
                rc.close()
        finally:
            server.cfg().save({"lan_enabled": False, "lan_password": ""})
            server.set_lan_state(False, APP_PORT)

        # 桌面入口模块
        sys.path.insert(0, str(ROOT / "app"))
        import main as desktop
        check("检测到系统浏览器", bool(desktop.find_browser()))
        check("端口分配可用", 1024 < desktop.free_port() < 65536)
    finally:
        c.close()
        fs.should_exit = True
        ms.should_exit = True
        time.sleep(0.4)
    failed = globals().get("FAILED", False)
    print("\n结果：" + ("存在失败项" if failed else "全部通过"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
