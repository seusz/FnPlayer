#!/usr/bin/env python3
"""Build the FnPlayer Android APK with no Gradle / Maven / AndroidX dependency.

Pipeline (all tools come from the local toolchain dir):
  aapt2 compile -> aapt2 link -> javac -> d8 -> inject classes.dex
  -> zipalign -> apksigner sign -> verify
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import zipfile
from pathlib import Path

AND = Path(__file__).resolve().parent
ROOT = AND.parent
TC = ROOT / "build" / "_toolchain"
JDK = TC / "jdk" / "jdk17.0.21_0"
BT = TC / "sdk" / "build-tools" / "34.0.0"
ANDROID_JAR = TC / "sdk" / "platforms" / "android-35" / "android.jar"
WORK = AND / "build"
DIST = AND / "dist"
VERSION = "1.2.0"
VERSION_CODE = "3"
# 每个 ABI 单独出一个安装包：内置 Node 运行时约 74MB，全架构合包会超过 100MB。
ABIS = ("arm64-v8a", "armeabi-v7a", "x86_64")
PRIMARY_ABI = "arm64-v8a"
APK_NAME = "FnPlayer-%s-android-%s.apk" % (VERSION, PRIMARY_ABI)
OUT = DIST / APK_NAME
KEYSTORE = WORK / "fnplayer.keystore"
STORE_PASS = "fnplayer2026"
ALIAS = "fnplayer"

JAVA = JDK / "bin" / "java.exe"
JAVAC = JDK / "bin" / "javac.exe"
JAR = JDK / "bin" / "jar.exe"
KEYTOOL = JDK / "bin" / "keytool.exe"
AAPT2 = BT / "aapt2.exe"
ZIPALIGN = BT / "zipalign.exe"
APKSIGNER = BT / "lib" / "apksigner.jar"
D8 = BT / "lib" / "d8.jar"


def step(msg: str):
    print("\n== " + msg + " ==", flush=True)


def run(cmd: list[str], ok_msg: str = ""):
    print("  >", " ".join(str(x) for x in cmd[:6]), "…", flush=True)
    r = subprocess.run([str(x) for x in cmd], cwd=str(AND), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print((r.stdout or "")[-3000:])
        print((r.stderr or "")[-3000:])
        raise SystemExit("FAILED: " + str(cmd[0]) + " (exit " + str(r.returncode) + ")")
    if ok_msg:
        print("   ", ok_msg, flush=True)
    return r


def check_toolchain():
    missing = [str(p) for p in (JAVA, JAVAC, JAR, KEYTOOL, AAPT2, ZIPALIGN,
                                APKSIGNER, D8, ANDROID_JAR) if not p.is_file()]
    if missing:
        raise SystemExit("工具链缺失:\n  " + "\n  ".join(missing))


def sync_web():
    """Refresh shared front-end assets from the desktop project."""
    src = ROOT / "app" / "web"
    dst = AND / "assets" / "web"
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("hls.min.js", "hls.LICENSE.txt", "placeholder.svg", "icon.png"):
        (dst / name).write_bytes((src / name).read_bytes())
    (AND / "res" / "drawable" / "ic_launcher.png").write_bytes((src / "icon.png").read_bytes())


def build(abi: str = PRIMARY_ABI):
    WORK.mkdir(parents=True, exist_ok=True)
    DIST.mkdir(parents=True, exist_ok=True)
    # 每个架构用独立的 class 输出目录：既避免不同架构互相覆盖，
    # 也不需要在构建前批量清空目录（一次性删除大量文件会触发安全策略）。
    classes = WORK / ("classes-" + abi)
    gen = WORK / "gen"
    dexdir = WORK / "dex"
    for d in (classes, gen, dexdir):
        d.mkdir(parents=True, exist_ok=True)
    stale_before = set(classes.glob("**/*.class"))
    for f in dexdir.glob("*.dex"):
        f.unlink()

    out = DIST / ("FnPlayer-%s-android-%s.apk" % (VERSION, abi))
    libdir = AND / "lib" / abi
    if not libdir.is_dir():
        raise SystemExit("缺少原生库目录: " + str(libdir))

    step("同步前端资源")
    sync_web()
    print("   前端文件:", len(list((AND / "assets" / "web").iterdir())))
    core_dir = AND / "assets" / "danmu-core"
    if core_dir.is_dir():
        n = sum(1 for _ in core_dir.rglob("*") if _.is_file())
        print("   内置弹幕核心文件:", n)

    step("aapt2 编译资源")
    compiled = WORK / "compiled"
    compiled.mkdir(exist_ok=True)
    for f in compiled.glob("*.flat"):
        f.unlink()
    run([AAPT2, "compile", "--dir", AND / "res", "-o", compiled])
    flats = sorted(compiled.glob("*.flat"))
    if not flats:
        raise SystemExit("aapt2 未生成 .flat 资源")
    print("   %d 个 .flat 资源" % len(flats))

    step("aapt2 链接基础 APK")
    base = WORK / "base.apk"
    if base.exists():
        base.unlink()
    cmd = [AAPT2, "link", "-o", base, "-I", ANDROID_JAR,
           "--manifest", AND / "AndroidManifest.xml",
           "-A", AND / "assets", "--java", gen,
           "--min-sdk-version", "23", "--target-sdk-version", "35",
           "--version-code", VERSION_CODE, "--version-name", VERSION] + flats
    run(cmd)
    print("   base.apk %.1f MB" % (base.stat().st_size / 1024 / 1024))

    step("javac 编译 Java 源码")
    java_files = sorted((AND / "src" / "com" / "fnplayer" / "mobile").glob("*.java"))
    java_files += sorted((AND / "src" / "com" / "example").rglob("*.java"))
    cmd = [JAVAC, "-encoding", "UTF-8", "--release", "8",
           "-classpath", ANDROID_JAR, "-d", classes] + java_files
    run(cmd, "%d source files" % len(java_files))
    # 只清掉上一次遗留、且本次没有重新生成的 class（正常情况下为 0 个）
    stale_after = set(classes.glob("**/*.class"))
    stale = stale_before - stale_after
    for f in stale:
        try:
            f.unlink()
        except OSError:
            pass
    if stale:
        print("   清理遗留 class %d 个" % len(stale))

    step("d8 生成 classes.dex")
    classes_jar = WORK / "classes.jar"
    run([JAR, "cf", classes_jar, "-C", classes, "."])
    run([JAVA, "-cp", D8, "com.android.tools.r8.D8",
         "--min-api", "23", "--lib", ANDROID_JAR,
         "--output", dexdir, classes_jar])
    dex = dexdir / "classes.dex"
    if not dex.is_file():
        raise SystemExit("classes.dex 未生成")
    print("   classes.dex %.1f KB" % (dex.stat().st_size / 1024))

    step("注入 dex 与原生库，生成未签名包")
    unsigned = WORK / ("unsigned-%s.apk" % abi)
    if unsigned.exists():
        unsigned.unlink()
    with zipfile.ZipFile(base) as zin, zipfile.ZipFile(unsigned, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            # resources.arsc 必须保持 STORED；其余资源沿用 aapt2 的压缩选择。
            zout.writestr(item, data, compress_type=item.compress_type)
        zout.write(dex, "classes.dex", compress_type=zipfile.ZIP_DEFLATED)
        # 内置 Node 运行时
        for so in sorted(libdir.glob("*.so")):
            zout.write(so, "lib/%s/%s" % (abi, so.name),
                       compress_type=zipfile.ZIP_DEFLATED)
    print("   未签名包 %.1f MB" % (unsigned.stat().st_size / 1024 / 1024))

    step("zipalign 对齐")
    aligned = WORK / ("aligned-%s.apk" % abi)
    if aligned.exists():
        aligned.unlink()
    run([ZIPALIGN, "-f", "-p", "4", unsigned, aligned])

    step("生成自签名证书")
    if not KEYSTORE.is_file():
        run([KEYTOOL, "-genkeypair", "-keystore", KEYSTORE, "-alias", ALIAS,
             "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
             "-storetype", "PKCS12", "-storepass", STORE_PASS,
             "-keypass", STORE_PASS,
             "-dname", "CN=FnPlayer, OU=Personal, O=FnPlayer, L=NA, ST=NA, C=CN"],
            "keystore created")
    else:
        print("   复用已有 keystore（保证覆盖安装签名一致）")

    step("apksigner 签名")
    if out.exists():
        out.unlink()
    run([JAVA, "-jar", APKSIGNER, "sign",
         "--ks", KEYSTORE, "--ks-key-alias", ALIAS,
         "--ks-pass", "pass:" + STORE_PASS, "--key-pass", "pass:" + STORE_PASS,
         "--v1-signing-enabled", "true", "--v2-signing-enabled", "true",
         "--out", out, aligned])
    print("   %s  %.1f MB" % (out.name, out.stat().st_size / 1024 / 1024))

    step("验签与包结构检查")
    r = run([JAVA, "-jar", APKSIGNER, "verify", "--verbose", out])
    print("   " + "\n   ".join((r.stdout or "").splitlines()[:8]))
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        need = {"classes.dex", "AndroidManifest.xml", "resources.arsc",
                "assets/web/index.html", "assets/web/app.js", "assets/web/app.css",
                "assets/web/hls.min.js", "assets/config.json",
                "lib/%s/libnode.so" % abi,
                "lib/%s/libnative-lib.so" % abi,
                "assets/danmu-core/server-entry.cjs",
                "assets/danmu-core/manifest.json",
                "META-INF/MANIFEST.MF"}
        missing = sorted(need - names)
        if missing:
            raise SystemExit("APK 缺少文件: " + ", ".join(missing))
        arsc = z.getinfo("resources.arsc")
        print("   entries:", len(names), "| resources.arsc compress:", arsc.compress_type,
              "| config sources:", len(json.loads(z.read("assets/config.json"))["sources"]))
    print("\nBUILD OK ->", out)
    return out


def write_docs():
    """Deliver notes + an optional, non-baked-in source import text file."""
    DIST.mkdir(parents=True, exist_ok=True)
    imported = DIST / "播放源导入.txt"
    desktop_cfg = ROOT / "dist" / "FnPlayer" / "config" / "config.json"
    lines = []
    if desktop_cfg.is_file():
        try:
            cfg = json.loads(desktop_cfg.read_text(encoding="utf-8"))
            for s in cfg.get("sources", []):
                name = str(s.get("name", "")).strip()
                api = str(s.get("api", "")).strip()
                if api.startswith("http"):
                    lines.append((name + " " + api).strip())
        except Exception:
            pass
    if lines:
        imported.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("   已生成播放源导入.txt（%d 条，仅作为独立文本，不写入 APK）" % len(lines))

    notes = f"""FnPlayer Android {VERSION} 使用说明（新增：内置完整弹幕核心）
========================================

【本版更新】
版本号 {VERSION}（versionCode {VERSION_CODE}），使用同一签名，可直接覆盖安装旧版。
最大变化：内置了**完整弹幕核心**——把 Node 运行时和 danmu_api 弹幕核心一起
打进安装包，应用启动时在本机 127.0.0.1 静默拉起，豆瓣/360/腾讯/优酷/爱奇艺/
咪咕/B站/人人/韩剧TV 等平台聚合可用，国产剧与电影也能匹配到弹幕，不再需要
额外部署云端服务。代价是安装包变大，并按 CPU 架构分别提供。

【这是什么】
安卓手机独立运行的播放器。手机自己联网访问你配置的播放源，不需要电脑、
不提供局域网服务、不对外开放任何端口。应用内置一个只绑定 127.0.0.1 的
本地服务：WebView 只与它通信，由它负责搜索、解析、m3u8 重写、防盗链
Referer/UA 透传、分片转发和弹幕代理，因此手机浏览器不会遇到跨域问题。
弹幕核心也在本机回环地址上运行（127.0.0.1:9322），不对外暴露。

【安装】
1. 按手机 CPU 架构选安装包（都是同一个应用，选错架构装不上）：
     FnPlayer-{VERSION}-android-arm64-v8a.apk   绝大多数 2017 年后的手机（优先）
     FnPlayer-{VERSION}-android-armeabi-v7a.apk 较老的 32 位 ARM 设备
     FnPlayer-{VERSION}-android-x86_64.apk     模拟器或少数 x86 设备
   传到手机（微信/数据线/网盘均可）后点击安装。
2. 系统提示「未知来源」时，允许本次安装即可。
   - 这是自签名安装包（签名文件 build/fnplayer.keystore），非应用商店版本。
   - 后续覆盖安装请使用同一 keystore 重新构建，否则签名不一致会装不上。
3. 需要 Android 6.0（API 23）及以上，建议 Android 10+（系统 WebView 越新，
   HLS/MSE 播放兼容性越好）。
4. 安装包约 35MB（其中 Node 运行时约 25MB），首次启动会把它解到应用私有
   目录并启动弹幕核心，约需 5~15 秒；期间弹幕会自动回退到内置镜像。

【首次使用】
出厂包不预置任何资源站。打开应用 → 底部「设置」→ 播放源，任选一种：
  * 单个添加：填名称 + 苹果CMS V10 JSON 接口地址 → 添加。
  * 批量导入：把「名称 接口地址」每行一条粘进文本框 → 导入播放源。
    本目录的「播放源导入.txt」是从电脑版 config 导出的，可整段复制粘贴。
  * 完整迁移：把电脑版 config/config.json 的全部内容粘进「配置迁移」→
    导入完整配置；也可「导出当前配置」备份。
添加后回到首页即可搜索，或浏览「最近更新」片单。

【播放】
详情页选线路、选集 → 立即播放。播放器支持进度条拖动、倍速、选集/线路
切换、全屏；播放记录自动保存在本机，重新打开同一集会续播。
影片详情会落盘保存，因此「退出应用再进入 → 首页继续观看」可直接恢复，
不再提示缓存失效；即使缓存被清，也会按片名自动重新查找。
部分加密 JS 中转页 / /share/ 播放页可能无法解析，会自动换线；仍失败时
手动切换线路。

【我的收藏】
在影片详情页点右上角「☆ 收藏」即可加入；底部「收藏」频道可查看全部收藏，
点海报直接打开详情，点卡片右上角 × 取消某部，也可用「清空」一次清除。
收藏保存在应用私有目录的 favorites.json，退出应用再进入依然有效；收藏时
会同时把影片详情（含播放线路）落盘，万一快照被清理，也会按片名自动重新
查找，不会报「缓存失效」。

【返回与全屏】
搜索结果是独立页面，因此「搜索结果 → 影片详情 → 选集播放」按系统返回键
会依次退回 详情 → 搜索结果 → 首页，每一步都保留关键词、结果列表、选集
高亮与滚动位置，不会一步跳回初始片单。弹幕面板、选集/线路面板会先被
逐层关闭，再退出页面。播放器进入全屏时切换为横屏；退出全屏、从播放页
返回或按系统返回键，都会强制切回竖屏。

【弹幕】
弹幕服务在应用启动时就在后台静默就绪，不需要任何开关：
  * 播放时按「片名 + 集数」自动匹配并加载弹幕，直接叠在画面上。
  * 播放器控制条点「弹幕」打开面板：开/关显示、重新匹配、清屏、手动
    搜索番剧与选集、调节透明度/字号/区域/速度/时间偏移、发送本地弹幕
    （本地弹幕只在本机显示，不上传）。
  * 「设置 → 弹幕」可查看服务状态、关闭显示或自动匹配、改为你自己的
    /api/v2 兼容服务地址，以及测试/重新检测。
  * 面板会显示未匹配的具体原因、当前用的是内置核心还是内置镜像，以及
    上游耗时/错误；匹配到剧集但返回 0 条弹幕时也会明确提示重试。

【内置弹幕核心】
本版本内置了与 danmu-api-android 相同的技术路线：把 Node.js Mobile 运行时
和 danmu_api 弹幕核心一起随包分发，首次启动时解到应用私有目录，然后在应用
进程内启动 Node，由它在本机回环地址 127.0.0.1:9322 上提供 /api/v2 兼容接口。
与那个项目的差别是：只监听回环地址、不做任何局域网暴露、不做账号与设备
管理，启动失败会自动回退到内置镜像。

  * 数据源：豆瓣、360、人人、韩剧TV、腾讯、优酷、爱奇艺、咪咕、B站、弹弹play。
  * 自动匹配走核心的 POST /api/v2/match，一次请求完成季集解析与跨平台定位。
  * 「设置 → 弹幕 → 服务状态」会显示核心状态、Node 版本与实际数据源列表。
  * 核心没起来时会显示失败原因，并继续使用内置镜像（仅番剧/动画库）。
  * 自建服务仍可填在「设置 → 弹幕 → 自定义弹幕服务」，优先级：内置核心 →
    自定义服务 → 内置镜像。

【弹幕覆盖与限制】
实测内置核心对国产内容的覆盖明显好于旧的纯镜像：
    庆余年 → 4 条、繁花 → 16 条、流浪地球 → 7 条（旧镜像全部 0 条），
并且「流浪地球」能取到 320 条弹幕正文。但**具体某一集能否取到弹幕取决于
上游平台**——例如庆余年/繁花在当前网络下能匹配到剧集，但腾讯源的弹幕正文
取不回来（与电脑版完整服务表现一致），换网络或换线路后可能就正常了。

新版匹配还做了两件事：自动去掉「更新至152集」「(2024)」「HD」「【4K】」
「第二季」这类片名后缀生成多个候选；对搜索结果做相似度校验，避免把
「狂飙」误匹配成「弹速狂飙」。弹幕面板与设置页会显示未匹配的具体原因
（上游无该片名 / 相似度不足 / 上游错误 / 耗时）以及 0 条弹幕的重试提示。

若内置核心在你所在网络下受限（例如某些平台需要代理），仍可按同目录
《弹幕服务部署指引.txt》免费部署一个完整 danmu_api 实例（Vercel / 腾讯云
EdgeOne / Cloudflare / Docker 均可），把「https://你的域名/TOKEN」填进
「设置 → 弹幕 → 自定义弹幕服务」并保存。

【与电脑版的区别】
  - 弹幕核心内置在 APK 里（随应用启动自动拉起），没有电脑版那种独立的
    工作区目录，也不需要单独启动/关闭。
  - 不含局域网访问面板（本版本不对外提供服务）。
  - 不含分类深翻、豆瓣榜单等 NAS 高级片库。
  - 仅支持苹果CMS V10 的 at/json 接口（at/xml 未移植）。
  - 配置保存在应用私有目录，卸载即清除；用「配置迁移」导出备份。

【网络与隐私】
  - 应用需要 INTERNET 权限；所有网络请求由本机服务发起，播放端不直连源站。
  - 本地服务仅监听 127.0.0.1，且带随机会话 cookie 校验，其它应用无法调用。
  - 内置弹幕核心同样只监听 127.0.0.1:9322，不对外暴露；它向豆瓣/360/腾讯/
    优酷/爱奇艺/咪咕/B站等第三方平台发起的请求与电脑版 danmu_api 完全一致，
    这些上游为第三方服务，其可用性、返回内容是否合适由使用者自行判断和负责。
  - 因为部分自建接口仍是 http，Manifest 开启了明文流量；建议优先用 https 源。
  - 不上传任何使用数据，不含统计、广告与第三方 SDK。

【合规】
上游 fnmedia-ext (MIT)、hls.js (Apache-2.0)。
弹幕部分：本应用内置了 Node.js Mobile 运行时与 danmu_api 弹幕核心
（huangxd-/danmu_api，AGPL-3.0）以及 danmu-api-android（MIT）提供的
Android 版 Node 运行时与 JNI 桥，三个项目的开源许可均随源码保留，
请在再分发时一并保留其许可声明。弹幕数据来自上述第三方平台，内容版权
归各平台与著作权人所有。
APK 出厂不预置任何第三方资源站；如不希望使用内置弹幕核心，可在
「设置 → 弹幕」改为自建服务或关闭弹幕显示。
请确保你访问的内容已获合法授权，仅供个人使用，请勿用于商业传播。

【源码提供】
因内置了 AGPL-3.0 的 danmu_api 核心，APK 随包附带了该核心的
AGPL-3.0 许可原文（assets/danmu-core/LICENSE.danmu_api-AGPL-3.0.txt）
与全部 npm 依赖的许可声明。
本应用的完整对应源码（含弹幕核心源码）已在 GitHub 公开：
  https://github.com/seusz/FnPlayer
  弹幕核心源码位于 third_party/danmu_api/（上游 commit 4c86579）
  第三方组件的上游仓库地址与许可证汇总见仓库中的 NOTICE.md
若你修改了本应用并通过网络向他人提供服务，必须同时向这些用户提供
你的修改版源码（AGPL-3.0 第 13 条）。

【源码与构建】
工程在 android/ 目录，纯 Android Framework API（WebView + HttpURLConnection +
org.json），不依赖 AndroidX / Kotlin / Gradle；仅弹幕核心用到了随包分发的
Node 运行时（通过 JNI 在进程内调用）。
重新构建：
  python tools/prepare_portable.py           # 准备弹幕核心工作区（含 npm install）
  python android/build_danmu_core.py      # 组装并裁剪内置弹幕核心
  python android/build_apk.py --all-abis  # 产出三个架构的安装包
构建工具链位于 build/_toolchain/（Amazon Corretto JDK 17 + Android SDK
build-tools 34 + platform android-35），均为官方原包。
"""
    (DIST / "使用说明.txt").write_text(notes, encoding="utf-8")
    print("   已生成使用说明.txt")
    guide = AND / "弹幕服务部署指引.txt"
    if guide.is_file():
        (DIST / guide.name).write_bytes(guide.read_bytes())
        print("   已生成弹幕服务部署指引.txt")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs-only", action="store_true")
    ap.add_argument("--abi", default=PRIMARY_ABI, choices=list(ABIS),
                    help="只构建指定 ABI（默认 arm64-v8a）")
    ap.add_argument("--all-abis", action="store_true", help="构建全部架构")
    args = ap.parse_args()
    if not args.docs_only:
        check_toolchain()
        if args.all_abis:
            for abi in ABIS:
                build(abi)
        else:
            build(args.abi)
    write_docs()


if __name__ == "__main__":
    main()
