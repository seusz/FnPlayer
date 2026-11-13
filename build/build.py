#!/usr/bin/env python3
"""一键构建 FnPlayer Windows 应用：PyInstaller 打包 → 冒烟验证 → 便携 zip。

用法：python build/build.py [--skip-verify]
产物：dist/FnPlayer/FnPlayer.exe、dist/FnPlayer-1.0.0-Windows-x64.zip
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
OUT = DIST / "FnPlayer"
EXE = OUT / "FnPlayer.exe"
VERSION = "1.0.0"


def step(msg):
    print(f"\n=== {msg} ===", flush=True)


def build():
    step("PyInstaller 打包（onedir / 无控制台）")
    # 不删除旧产物（大目录批量删除可能受环境安全策略限制），改为重命名保留。
    # 这样 COLLECT 目标目录不存在，PyInstaller 也无需执行递归删除。
    if OUT.exists():
        backup = DIST / f"_prev_FnPlayer_{int(time.time())}"
        OUT.rename(backup)
        print(f"旧产物已保留为: {backup.name}")
    work = ROOT / "build" / "_work"
    cmd = [sys.executable, "-m", "PyInstaller",
           str(ROOT / "build" / "FnPlayer.spec"),
           "--distpath", str(DIST), "--workpath", str(work),
           "--noconfirm", "--clean"]
    r = subprocess.run(cmd, cwd=str(ROOT), env={**os.environ,
                                                "FNPLAYER_BUILD_NAME": OUT.name})
    if r.returncode != 0:
        raise SystemExit("PyInstaller 构建失败")
    if not EXE.is_file():
        raise SystemExit("未找到产物 " + str(EXE))
    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"产物: {EXE}  ({size / 1024 / 1024:.1f} MB, "
          f"{len(list(OUT.rglob('*')))} 个文件)")


def assemble_portable():
    """把便携附加资源装进 dist/FnPlayer，形成解压即用的完整目录。"""
    step("组装便携版（config / runtime / workspace / data）")
    src = ROOT / "portable"
    for must in (src / "runtime" / "node" / "node.exe",
                 src / "workspace" / "danmu" / "danmu_api" / "server.js",
                 src / "workspace" / "danmu" / "node_modules",
                 src / "config" / "config.json"):
        if not must.exists():
            raise SystemExit(f"缺少便携资源 {must}\n"
                             "请先运行 python tools/prepare_portable.py")
    for sub in ("config", "runtime", "workspace", "LICENSES"):
        shutil.copytree(src / sub, OUT / sub, dirs_exist_ok=True)
    (OUT / "data").mkdir(exist_ok=True)
    readme = src / "使用说明.txt"
    if readme.is_file():
        shutil.copy2(readme, OUT / readme.name)
    # 构建期产生的服务日志不随包分发；保留一个说明占位，避免空目录丢失
    logs = OUT / "workspace" / "danmu" / "logs"
    logs.mkdir(exist_ok=True)
    for f in logs.glob("*.log"):
        f.unlink()
    (logs / "README.txt").write_text(
        "弹幕服务运行日志会写在这里（service.log）。\n", encoding="utf-8")
    size = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"便携目录: {OUT}  ({size / 1024 / 1024:.1f} MB, "
          f"{len(list(OUT.rglob('*')))} 个文件)")
    return size


def verify():
    step("运行打包后的 exe 做冒烟验证")
    import httpx
    port = 18993
    data = Path(tempfile.mkdtemp(prefix="fnplayer-verify-"))
    proc = subprocess.Popen(
        [str(EXE), "--no-browser", "--port", str(port), "--data-dir", str(data)],
        cwd=str(OUT))
    base = f"http://127.0.0.1:{port}"
    client = httpx.Client(base_url=base, timeout=10, trust_env=False)
    ok = False
    try:
        for _ in range(80):
            try:
                if client.get("/api/health").json().get("ok"):
                    ok = True
                    break
            except Exception:
                pass
            if proc.poll() is not None:
                break
            time.sleep(0.25)
        if not ok:
            log = data / "launch.log"
            detail = log.read_text(encoding="utf-8") if log.exists() else "无日志"
            raise SystemExit("打包后的服务未能启动；launch.log:\n" + detail)
        print("  服务启动 OK")
        html = client.get("/").text
        token = re.search(r'name="fn-token" content="([^"]+)"', html)
        assert token, "首页未注入令牌"
        assert client.get("/static/hls.min.js").status_code == 200, "hls.js 缺失"
        assert client.get("/static/danmu.js").status_code == 200, "弹幕引擎缺失"
        assert "danmuCanvas" in html, "首页缺少弹幕图层"
        assert "setDanmuApi" in html, "设置页缺少弹幕配置"
        assert client.get("/favicon.ico").status_code == 200, "图标缺失"
        assert client.get("/api/settings").json().get("theme") == "dark", "配置层异常"
        assert "setDanmuService" in html, "设置页缺少本地弹幕服务开关"
        print("  静态资源 / 配置层 OK")
        # ---- 便携版资源与本地弹幕服务 ----
        assert (OUT / "config" / "config.json").is_file(), "缺少便携配置入口"
        assert (OUT / "runtime" / "node" / "node.exe").is_file(), "缺少便携 Node"
        assert (OUT / "workspace" / "danmu" / "danmu_api" / "server.js").is_file(), \
            "缺少弹幕独立工作区"
        assert (OUT / "workspace" / "danmu" / "node_modules").is_dir(), \
            "缺少弹幕服务依赖"
        pcfg = json.loads((OUT / "config" / "config.json").read_text(encoding="utf-8"))
        assert pcfg.get("sources") == [], "便携配置不应预置播放源"
        assert pcfg.get("danmu_service_enabled") is False, "本地服务默认应关闭"
        svc = client.get("/api/danmu/service").json()
        assert svc.get("available") is True, f"便携运行时不可用: {svc.get('reason')}"
        assert svc.get("enabled") is False, "默认不应自动开启弹幕服务"
        assert svc.get("port") == 9321 and svc.get("token"), "服务参数异常"
        print("  便携资源 / 本地弹幕服务状态 OK")
        assert "lanApplyBtn" in html, "设置页缺少局域网访问面板"
        lanst = client.get("/api/lan").json()
        assert lanst.get("active") is False and lanst.get("enabled") is False, \
            "全新安装不应开放局域网"
        assert lanst.get("bind") == "127.0.0.1", "默认应只绑定回环地址"
        assert client.get("/api/lan/qr?i=0").status_code == 404, \
            "未开放时不应生成访问二维码"
        print("  局域网默认关闭 / 状态接口 OK")
        hdr = {"X-Fn-Token": token.group(1)}
        dst = client.get("/api/danmu/status").json()
        assert dst.get("configured") is False, "全新数据目录不应预置弹幕服务"
        bad = client.post("/api/danmu/probe", headers=hdr,
                          json={"api": "ftp://bad"}).json()
        assert bad.get("ok") is False, "非法弹幕地址未被拒绝"
        assert client.post("/api/lan/apply", headers=hdr,
                           json={"enabled": True, "port": 99999}).status_code == 400, \
            "非法局域网端口未被拒绝"
        saved = client.post("/api/settings", headers=hdr,
                            json={"danmu_api": "http://127.0.0.1:9321/87654321",
                                  "danmu_enabled": True}).json()["settings"]
        assert saved["danmu_api"].endswith("/87654321"), "弹幕配置未持久化"
        assert client.get("/api/danmu/status").json()["configured"] is True, \
            "弹幕状态未更新"
        print("  弹幕配置与代理校验 OK")
        bulk = client.post("/api/sources/bulk", headers=hdr, json={
            "text": "打包验收源,http://127.0.0.1:1/api.php/provide/vod/at/json\n"
                    "无效行没有地址"}).json()
        assert bulk.get("added") == 1, f"批量导入异常: {bulk}"
        assert len(bulk.get("skipped") or []) == 1, f"非法行未跳过: {bulk}"
        assert any(s["name"] == "打包验收源"
                   for s in bulk.get("items", [])), "导入后名称丢失"
        assert "bulkText" in html, "首页缺少批量导入入口"
        assert client.get("/static/app.js").text.count("api/sources/bulk") >= 1, \
            "前端未引用批量导入接口"
        print("  批量导入链路 OK")
        # ---- 真实拉起便携 Node 弹幕服务（不是 mock）----
        started = client.post("/api/danmu/service", headers=hdr,
                              json={"enabled": True}, timeout=45).json()
        assert started.get("ok") is True and started.get("running") is True, \
            f"本地弹幕服务未能拉起: {started.get('error')}"
        assert started.get("managed") is True and started.get("pid"), \
            "弹幕服务进程未被主程序托管"
        pr = client.post("/api/danmu/probe", headers=hdr,
                         json={"api": started["base"]}, timeout=20).json()
        assert pr.get("ok") is True, f"本地弹幕服务连通失败: {pr}"
        stopped = client.post("/api/danmu/service", headers=hdr,
                              json={"enabled": False}, timeout=30).json()
        assert stopped.get("running") is False, "本地弹幕服务未能停止"
        print("  真实本地弹幕服务启停 OK")
        client.post("/api/shutdown", headers=hdr, content=b"{}")
        for _ in range(30):
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        print("  窗口关闭联动 OK" if proc.poll() is not None else "  WARN: 进程未自行退出")
    finally:
        client.close()
        if proc.poll() is None:
            proc.terminate()
        shutil.rmtree(data, ignore_errors=True)
    return ok


def make_zip():
    step("生成便携 zip")
    # 验收过程会产生服务日志，不随包分发
    logs = OUT / "workspace" / "danmu" / "logs"
    for f in logs.glob("*.log"):
        f.unlink()
    target = DIST / f"FnPlayer-{VERSION}-Windows-x64.zip"
    if target.exists():
        target.unlink()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in OUT.rglob("*"):
            if p.is_file() and p.suffix != ".log":
                z.write(p, str(Path("FnPlayer") / p.relative_to(OUT)))
    print(f"压缩包: {target} ({target.stat().st_size / 1024 / 1024:.1f} MB)")


def saved_user_config() -> bytes | None:
    """构建前备份本机工作目录里用户自己填的 config.json。

    zip 里始终是「出厂空配置」，但 dist/FnPlayer/ 往往就是用户正在用的目录，
    不能因为一次重新构建就把他填的播放源冲掉。
    """
    f = OUT / "config" / "config.json"
    return f.read_bytes() if f.is_file() else None


def restore_user_config(raw: bytes | None):
    if raw is None:
        return
    try:
        j = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(j, dict):
            j = {}
    except Exception:
        j = {}
    # 补齐新增字段，但不改动用户已填的任何值
    j.setdefault("lan_enabled", False)
    j.setdefault("lan_port", 18790)
    j.setdefault("lan_password", "")
    f = OUT / "config" / "config.json"
    f.write_text(json.dumps(j, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已保留本机原有配置: {f}")


def set_out(name: str):
    """切换构建目标目录（旧目录被运行中的程序占用时可用独立目录构建）。"""
    global OUT, EXE
    OUT = DIST / name
    EXE = OUT / "FnPlayer.exe"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-verify", action="store_true")
    ap.add_argument("--out", default="FnPlayer",
                    help="构建输出目录名（默认 FnPlayer）")
    args = ap.parse_args()
    DIST.mkdir(exist_ok=True)
    set_out(args.out)
    user_cfg = saved_user_config()
    # 清理调试残留（仅本工程 dist 下自建目录）
    for name in ("edge-test", "edge-test2"):
        p = DIST / name
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    build()
    assemble_portable()
    if not args.skip_verify:
        verify()
    make_zip()
    # zip 已用出厂配置打包完成；本机工作目录恢复用户自己的配置
    restore_user_config(user_cfg)
    step("完成")
    print(f"可执行程序: {EXE}")
    print(f"便携压缩包: {DIST / f'FnPlayer-{VERSION}-Windows-x64.zip'}")
    print("提示：解压后双击 FnPlayer.exe 即可；播放源写在 config/config.json，")
    print("      弹幕服务在「设置 → 弹幕 → 本地弹幕服务」打开开关即可自动拉起。")
    print("      需要手机/电视访问时，在「设置 → 局域网访问」开启并设访问码。")


if __name__ == "__main__":
    main()
