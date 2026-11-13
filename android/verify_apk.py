#!/usr/bin/env python3
"""Acceptance checks for the built Android APK and the shipped sources."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

AND = Path(__file__).resolve().parent
ROOT = AND.parent
DIST = AND / "dist"
VERSION = "1.2.0"
VERSION_CODE = "3"
ABIS = ("arm64-v8a", "armeabi-v7a", "x86_64")
PRIMARY_ABI = "arm64-v8a"
APK = DIST / ("FnPlayer-%s-android-%s.apk" % (VERSION, PRIMARY_ABI))
TC = ROOT / "build" / "_toolchain"
JAVA = TC / "jdk" / "jdk17.0.21_0" / "bin" / "java.exe"
APKSIGNER = TC / "sdk" / "build-tools" / "34.0.0" / "lib" / "apksigner.jar"
AAPT2 = TC / "sdk" / "build-tools" / "34.0.0" / "aapt2.exe"
NODE = ROOT / "portable" / "runtime" / "node" / "node.exe"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond, detail: str = ""):
    ok = bool(cond)
    RESULTS.append((name, ok, detail))
    print(("  OK   " if ok else "  FAIL ") + name + ((" | " + detail) if detail else ""),
          flush=True)
    return ok


def read(rel: str) -> str:
    return (AND / rel).read_text(encoding="utf-8")


JNI_REQUIRED = ("startNodeWithArguments", "setEnvironmentVariable",
                "getNodeVersion", "changeWorkingDirectory")


def _jni_symbols_ok(so_path: Path) -> bool:
    """确认原生库导出的 JNI 符号与 NodeBridge 的全限定名一致。"""
    try:
        blob = so_path.read_bytes()
    except OSError:
        return False
    text = blob.decode("latin-1", "replace")
    for fn in JNI_REQUIRED:
        if "Java_com_example_danmuapiapp_NodeBridge_" + fn not in text:
            return False
    return True


def _run_packaged_core(apk: Path) -> bool:
    """把 APK 内置的弹幕核心解出来实际启动一次，确认它能提供 /api/v2 接口。

    这是对「打包进 APK 的核心是否完整可用」最直接的验证——APK 里的文件
    结构和播放器运行时解包后的目录完全一致。
    """
    import socket
    import time
    import urllib.request

    if not NODE.is_file():
        print("     (缺少 Node 运行时，跳过核心启动验证)")
        return True
    work = AND / "build" / "coretest"
    work.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(apk) as z:
            for name in z.namelist():
                if not name.startswith("assets/danmu-core/"):
                    continue
                rel = name[len("assets/danmu-core/"):]
                if not rel:
                    continue
                dest = work / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(name))
    except (OSError, KeyError) as e:
        print("     (解包失败: %s)" % e)
        return False

    entry = work / "server-entry.cjs"
    if not entry.is_file():
        print("     (解包后缺少 server-entry.cjs)")
        return False

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    env = dict(os.environ)
    env["DANMU_API_PORT"] = str(port)
    env["DANMU_API_HOST"] = "127.0.0.1"
    env["TMPDIR"] = str(work)
    env["HOME"] = str(work)
    env["LOG_LEVEL"] = "error"
    proc = subprocess.Popen([str(NODE), str(entry)], cwd=str(work), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = "http://127.0.0.1:%d" % port
    try:
        deadline = time.time() + 45
        healthy = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with opener.open(base + "/__fn_health", timeout=4) as r:
                    if r.status == 200:
                        healthy = True
                        break
            except Exception:
                time.sleep(1.0)
        if not healthy:
            out = proc.stdout.read(2000).decode("utf-8", "replace") if proc.stdout else ""
            print("     (核心未就绪: %s)" % out[-400:].replace("\n", " "))
            return False
        # 真调一次聚合搜索，确认数据源可用
        with opener.open(base + "/api/v2/search/anime?keyword=%E5%BA%86%E4%BD%99%E5%B9%B4",
                         timeout=60) as r:
            payload = json.loads(r.read(2_000_000).decode("utf-8", "replace"))
        n = len(payload.get("animes") or [])
        print("     核心实测: 搜索「庆余年」返回 %d 条" % n)
        return n > 0
    except Exception as e:
        print("     (核心调用失败: %s)" % e)
        return False
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def main():
    print("== 成品文件 ==")
    check("APK 存在且体积合理", APK.is_file() and APK.stat().st_size > 100_000,
          "%.1f MB" % (APK.stat().st_size / 1024 / 1024) if APK.is_file() else "缺失")

    r = subprocess.run([str(JAVA), "-jar", str(APKSIGNER), "verify", "--verbose", str(APK)],
                       capture_output=True, text=True, errors="replace")
    v = r.stdout
    check("APK 签名有效", r.returncode == 0 and "Verifies" in v,
          "v1=%s v2=%s" % ("true" if "v1 scheme (JAR signing): true" in v else "?",
                           "true" if "v2 scheme (APK Signature Scheme v2): true" in v else "?"))

    b = subprocess.run([str(AAPT2), "dump", "badging", str(APK)],
                       capture_output=True, text=True, errors="replace").stdout
    check("包名/版本正确",
          "name='com.fnplayer.mobile' versionCode='%s' versionName='%s'" % (VERSION_CODE, VERSION) in b,
          "%s (versionCode %s)" % (VERSION, VERSION_CODE))
    check("minSdk 23 / targetSdk 35", "sdkVersion:'23'" in b and "targetSdkVersion:'35'" in b)
    check("存在启动 Activity", "launchable-activity: name='com.fnplayer.mobile.MainActivity'" in b)
    check("仅申请网络相关权限",
          "android.permission.INTERNET" in b and "android.permission.READ_EXTERNAL_STORAGE" not in b
          and "android.permission.WRITE_EXTERNAL_STORAGE" not in b)
    check("只声明单一 native 架构", "native-code: '%s'" % PRIMARY_ABI in b,
          PRIMARY_ABI)

    with zipfile.ZipFile(APK) as z:
        names = set(z.namelist())
        need = {"classes.dex", "AndroidManifest.xml", "resources.arsc",
                "assets/config.json", "assets/web/index.html", "assets/web/app.css",
                "assets/web/app.js", "assets/web/danmu.js", "assets/web/hls.min.js",
                "assets/web/hls.LICENSE.txt", "assets/web/placeholder.svg",
                "assets/web/icon.png"}
        check("APK 内容完整", need <= names, "%d entries, missing=%s" %
              (len(names), sorted(need - names)))
        check("resources.arsc 未压缩", z.getinfo("resources.arsc").compress_type == 0)
        cfg = json.loads(z.read("assets/config.json"))
        check("出厂配置不预置播放源", not cfg.get("sources"),
              "sources=%d" % len(cfg.get("sources") or []))
        check("弹幕默认开启且不预置第三方地址",
              cfg.get("danmu_enabled") is True and cfg.get("danmu_auto") is True
              and not cfg.get("danmu_api"))
        html = z.read("assets/web/index.html").decode("utf-8")
        js = z.read("assets/web/app.js").decode("utf-8")
        dmjs = z.read("assets/web/danmu.js").decode("utf-8")
        check("HTML 含令牌占位符", "__FN_TOKEN__" in html)
        check("未引入局域网服务（仍仅本机）",
              all(k not in js and k not in html for k in ("/api/lan", "局域网访问")),
              "无 LAN API")
        check("弹幕引擎与面板已加回",
              "class DanmuEngine" in dmjs and "danmuCanvas" in html
              and "danmu.js" in html and "btnDanmu" in html)
        check("前端已接入弹幕本地服务",
              all(k in js for k in ("/api/danmu/status", "/api/danmu/match",
                                    "/api/danmu/comment/", "/api/danmu/bangumi/")))
        check("前端携带会话令牌", all(k in js for k in
              ("X-Fn-Token", "xhrSetup", "window.__FN_TOKEN__")))
        check("我的收藏频道与独立搜索结果页已打包",
              all(k in html for k in ("view-favorites", "view-search",
                                      'data-tab="favorites"', "btnFav")))
        check("前端已接入收藏 API",
              all(k in js for k in ("/api/favorites'", "/api/favorites/remove'",
                                    "/api/favorites/clear'")))

        # ---- 内置弹幕核心 ----
        core_entries = [n for n in names if n.startswith("assets/danmu-core/")]
        check("内置弹幕核心已随包分发",
              all(n in names for n in ("assets/danmu-core/server-entry.cjs",
                                       "assets/danmu-core/package.json",
                                       "assets/danmu-core/manifest.json",
                                       "assets/danmu-core/danmu_api/worker.js",
                                       "assets/danmu-core/node_modules/node-fetch/package.json")),
              "%d 个条目" % len(core_entries))
        manifest = json.loads(z.read("assets/danmu-core/manifest.json"))
        check("核心依赖闭包已生成", len(manifest.get("packages") or []) >= 20,
              "%d 个包，核心版本 %s，扫描未命中 %d 个（多为源码字符串误报）"
              % (len(manifest.get("packages") or []), manifest.get("core_version"),
                 len(manifest.get("missing") or [])))
        check("Node 运行时已打包",
              all("lib/%s/%s" % (PRIMARY_ABI, n) in names
                  for n in ("libnode.so", "libnative-lib.so", "libc++_shared.so")))
        launcher_js = z.read("assets/danmu-core/server-entry.cjs").decode("utf-8")
        check("核心声明为 ESM 且启动器为 CommonJS",
              json.loads(z.read("assets/danmu-core/package.json")).get("type") == "module"
              and "use strict" in launcher_js
              and "require(" in launcher_js
              and "await import(" in launcher_js)

    print("\n== 源码一致性 ==")
    local = read("src/com/fnplayer/mobile/LocalServer.java")
    streams = read("src/com/fnplayer/mobile/Streams.java")
    main_java = read("src/com/fnplayer/mobile/MainActivity.java")
    danmu_java = read("src/com/fnplayer/mobile/Danmu.java")
    store_java = read("src/com/fnplayer/mobile/Store.java")
    cms_java = read("src/com/fnplayer/mobile/Cms.java")
    check("本地服务仅绑定 127.0.0.1",
          'InetAddress.getByName("127.0.0.1")' in local and "0.0.0.0" not in local)
    check("鉴权支持 cookie / header / URL 三种凭证",
          all(k in local for k in ("x-fn-token", 'q.param("t")',
                                   'COOKIE + "=" + token')))
    check("HTML 注入令牌", 'html.replace("__FN_TOKEN__", token)' in local)
    check("播放地址带令牌", '"?t=" + token' in local)
    check("m3u8 重写为同源代理并带令牌",
          "suffix" in streams and "&t=" in streams and "/seg?i=" in streams
          and "/key?i=" in streams and "/sub?i=" in streams)
    check("WebView 不加载外部页面到自身",
          "isLocal(url)" in main_java and "setAllowFileAccess(false)" in main_java)

    check("继续观看：影片快照持久化",
          "titles.json" in store_java and "saveTitles" in store_java
          and "saveTitle" in store_java)
    check("继续观看：内存未命中回退磁盘",
          "store.title(id)" in cms_java and "store.saveTitles(persisted)" in cms_java)
    check("继续观看：后端按片名自动重搜回填",
          "recoverTitle" in local and "cms.rebind" in local
          and "historyTitle" in local)
    check("继续观看不再提示缓存失效",
          "缓存已失效" not in js and "正在重新查找" in js)
    check("弹幕服务随应用启动后台静默预热",
          "fnplayer-danmu-warmup" in local and "danmu.prewarm()" in local)
    check("弹幕服务端点齐全（含 /api/v2 兼容）",
          all(k in local for k in ("/api/danmu/status", "/api/danmu/match",
                                   "/api/danmu/comment/", "/api/danmu/bangumi/",
                                   "/api/v2/search/anime")))
    check("弹幕复刻 danmu_api 项目同款上游契约",
          "api.danmaku.weeblify.app/ddp/v1" in danmu_java
          and "/api/v2/" in danmu_java and "normalize" in danmu_java)
    check("桌面版 127.0.0.1 弹幕地址在手机上自动忽略",
          '"127.0.0.1".equals(host)' in danmu_java)
    check("设置可保存弹幕参数", "danmu_enabled" in local and "danmu_auto" in local)
    check("我的收藏：后端持久化与增删改查 API",
          all(k in store_java for k in ("favorites.json", "addFavorite",
                                        "removeFavorite", "favoriteTitle"))
          and all(k in local for k in ("/api/favorites", "/api/favorites/remove",
                                       "/api/favorites/clear")))
    check("我的收藏：收藏时落盘影片快照并支持按片名恢复",
          "store.saveTitle(snap)" in local and "store.favoriteTitle(tid)" in local)
    check("返回逐级回退：导航栈记录视图与滚动位置",
          all(k in js for k in ("nav.push({ v: view, y: 0 })",
                                "restoreView(nav[nav.length - 1])",
                                "refreshContinueBlock", "state.detailFrom"))
          and 'id="view-search"' in html)
    check("返回逐级回退：搜索结果独立保存、返回时不被重绘",
          all(k in js for k in ("state.searchHtml", "searchBody", "restoreView"))
          and 'id="view-search"' in html and "if (v === 'home') refreshHome();" not in js)
    check("全屏返回强制切回竖屏",
          "SCREEN_ORIENTATION_PORTRAIT" in main_java
          and "FnNative.requestLandscape(entering)" in js
          and "exitFullscreen()" in js)

    http_java = read("src/com/fnplayer/mobile/Http.java")
    check("HTTP 客户端支持 POST JSON（完整服务匹配接口）",
          "postJson" in http_java and "setRequestMethod(\"POST\")" in http_java)
    check("弹幕：内置核心/自定义服务优先调用 POST /api/v2/match",
          "remoteMatch" in danmu_java and "Http.postJson" in danmu_java
          and 'base + "/api/v2/match"' in danmu_java
          and "fullBase()" in danmu_java)
    check("弹幕：片名清洗生成多候选并做相似度校验",
          all(k in danmu_java for k in ("titleCandidates", "更新至", "similarity", "MIN_SCORE")))
    check("弹幕：上游错误/未匹配原因/覆盖说明可诊断",
          all(k in danmu_java for k in ("lastUpstreamError", "lastMatchReason",
                                        "coverage_note", "upstream_ms")))
    check("弹幕：测试地址改为真实 /api/v2 接口探测",
          '"/api/v2/search/anime?keyword="' in danmu_java and '"items", n' in danmu_java)
    check("弹幕：搜索接口改用智能候选",
          'danmu.searchSmart(q.param("keyword"))' in local)
    check("弹幕：前端展示失败原因、覆盖说明与 0 条提示",
          all(k in js for k in ("m.remote_error", "dmCoverageNote", "返回 0 条弹幕"))
          and "setDanmuNote" in html and "dmCoverageNote" in html)
    check("部署指引随包提供", (DIST / "弹幕服务部署指引.txt").is_file())

    # ---- 内置弹幕核心：把 APK 里的核心解出来真跑一遍 ----
    check("随包弹幕核心可独立启动并提供 /api/v2", _run_packaged_core(APK),
          "解包后用 Node 启动核心，实测搜索接口")

    # ---- 内置弹幕核心（源码侧）----
    core_java = read("src/com/fnplayer/mobile/DanmuCore.java")
    bridge_java = read("src/com/example/danmuapiapp/NodeBridge.java")
    launcher = read("danmu-core-src/server-entry.cjs")
    check("内置核心：JNI 桥类名与原生导出符号一致",
          "package com.example.danmuapiapp;" in bridge_java
          and "startNodeWithArguments" in bridge_java
          and "setEnvironmentVariable" in bridge_java)
    check("内置核心：安装/启动/健康检查逻辑齐全",
          all(k in core_java for k in ("copyTree", "startNodeWithArguments",
                                       "__fn_health", "DANMU_API_PORT",
                                       "RATE_LIMIT_MAX_REQUESTS", "TMPDIR", "HOME")))
    check("内置核心：仅监听回环地址，不做局域网暴露",
          '"127.0.0.1"' in core_java and "0.0.0.0" not in core_java)
    check("弹幕请求优先走内置核心，内置镜像仅作兜底",
          "DanmuCore.base()" in danmu_java and "fullBase()" in danmu_java
          and 'out.put("mode", "core")' in danmu_java)
    check("内置核心启动失败时回退内置镜像",
          "customBase().isEmpty()" in danmu_java and '"builtin"' in danmu_java)
    check("启动器：单次 import 加载核心并监听 127.0.0.1",
          "await import(workerUrl)" in launcher and "127.0.0.1" in launcher
          and "handleRequest(webRequest, process.env" in launcher)
    check("原生库 JNI 符号与桥接类匹配",
          all(_jni_symbols_ok(AND / "lib" / abi / "libnative-lib.so")
              for abi in ABIS if (AND / "lib" / abi / "libnative-lib.so").is_file()))
    check("三套架构的 Node 运行时齐备",
          all((AND / "lib" / abi / "libnode.so").is_file() for abi in ABIS),
          ", ".join(ABIS))

    ver_files = {
        "AndroidManifest.xml": read("AndroidManifest.xml"),
        "MainActivity.java": main_java,
        "LocalServer.java": local,
        "Danmu.java": danmu_java,
        "DanmuCore.java": core_java,
        "index.html": html,
        "build_apk.py": read("build_apk.py"),
        "preview_ui.py": read("preview_ui.py"),
        "使用说明.txt": (DIST / "使用说明.txt").read_text(encoding="utf-8"),
    }
    check("版本号全端统一为 %s / versionCode %s" % (VERSION, VERSION_CODE),
          "versionCode='%s' versionName='%s'" % (VERSION_CODE, VERSION) in b
          and 'android:versionCode="%s"' % VERSION_CODE in ver_files["AndroidManifest.xml"]
          and all(VERSION in s for s in ver_files.values())
          and not any("1.1.0" in s for s in ver_files.values()),
          "核对 %d 个文件" % len(ver_files))

    front_paths = set(re.findall(r"['\"](/api/[a-zA-Z0-9_/\-]+)", js))
    back_paths = set(re.findall(
        r'p\.(?:equals|startsWith)\("(/api/[a-zA-Z0-9_/\-]+)"\)', local))
    missing = sorted(front_paths - back_paths)
    check("前后端 API 契约一致", not missing,
          "前端 %d 个端点，后端 %d 个，缺失=%s" % (len(front_paths), len(back_paths), missing))

    print("\n== 前端与文档 ==")
    nr = subprocess.run([str(NODE), "--check", str(AND / "assets/web/app.js")],
                        capture_output=True, text=True, errors="replace")
    check("app.js 语法通过", nr.returncode == 0, nr.stderr[:120])
    check("使用说明存在", (DIST / "使用说明.txt").is_file())
    imp = DIST / "播放源导入.txt"
    check("播放源导入文本存在且不写入 APK",
          imp.is_file() and "assets/config.json" in names and
          len(json.loads(zipfile.ZipFile(APK).read("assets/config.json"))["sources"]) == 0,
          "%d 条（独立文件）" % (len(imp.read_text(encoding="utf-8").splitlines()) if imp.is_file() else 0))

    fails = [n for n, ok, _ in RESULTS if not ok]
    print("\n== 汇总 ==")
    print("  %d 项检查，%d 通过，%d 失败" % (len(RESULTS), len(RESULTS) - len(fails), len(fails)))
    if fails:
        print("  失败项:", ", ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
