#!/usr/bin/env python3
"""FnPlayer Windows 桌面入口。

启动本机服务 → 用 Edge/Chrome 的 --app 模式打开独立无地址栏窗口
→ 窗口关闭或前端请求退出时一并收尾。不依赖 WebView2 SDK / .NET / Electron，
只要系统有 Edge（Win10/11 自带）或 Chrome 即可运行。

监听地址：
  默认 127.0.0.1（仅本机）；在设置里开启「局域网访问」后改绑 0.0.0.0，
  由 server.py 的访问码中间件控制谁能访问。切换开关时只重绑 uvicorn，
  不重启 exe、不关闭已有窗口，端口尽量保持不变。
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

# PyInstaller --windowed 下 sys.stdout / sys.stderr 为 None，
# uvicorn / logging 会访问 .isatty() 而崩溃，这里补上哑流。
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()
logging.basicConfig(handlers=[logging.NullHandler()], level=logging.WARNING)

BASE = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent)
for _p in (str(BASE), str(BASE / "kernel")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def data_dir() -> Path:
    import config as cfg
    return cfg.data_dir()


def log(msg: str):
    try:
        with (data_dir() / "launch.log").open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def choose_port(preferred: int) -> int:
    """优先固定端口（手机收藏 / 二维码不会失效），被占用时向后顺延。"""
    for p in range(max(1, int(preferred)), min(65535, int(preferred) + 12) + 1):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return free_port()


def find_browsers() -> list[str]:
    cands = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
    ]
    return [p for p in cands if p and Path(p).is_file()]


def find_browser() -> str:
    """兼容测试脚本的单例接口。"""
    all_browsers = find_browsers()
    return all_browsers[0] if all_browsers else ""


def open_window(url: str, profile: Path) -> subprocess.Popen | None:
    """依次尝试系统浏览器；进程立刻退出（被转发给已有实例等）则换下一个。"""
    for idx, exe in enumerate(find_browsers()):
        args = [
            exe,
            f"--app={url}",
            "--window-size=1380,880",
            "--window-position=80,50",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-default-search-check",
            "--disable-features=msEdgeSplitScreen,msWebOOUI,msPdfOOUI,Translate",
            "--disable-background-networking",
        ]
        try:
            proc = subprocess.Popen(args, close_fds=True)
        except Exception as exc:
            log(f"browser {exe} failed: {exc}")
            continue
        time.sleep(1.2)
        if proc.poll() is None:
            log(f"window opened with {exe}")
            return proc
        log(f"browser {exe} exited immediately (rc={proc.returncode}), try next")
    import webbrowser
    log("fallback to default browser")
    webbrowser.open(url)
    return None


class Service:
    """可重复启停的 uvicorn 实例（切换局域网开关时重绑监听地址）。"""

    def __init__(self, app):
        self.app = app
        self.server = None
        self.thread = None

    def _run(self, s):
        try:
            s.run()
        except Exception:
            log("uvicorn crashed:\n" + traceback.format_exc())

    def start(self, host: str, port: int) -> bool:
        import uvicorn
        conf = uvicorn.Config(
            self.app, host=host, port=port, log_level="warning",
            access_log=False, lifespan="on", log_config=None,
            timeout_graceful_shutdown=2)
        s = uvicorn.Server(conf)
        t = threading.Thread(target=self._run, args=(s,), daemon=True,
                             name="uvicorn")
        self.server, self.thread = s, t
        t.start()
        for _ in range(120):
            if s.started:
                return True
            if not t.is_alive():
                break
            time.sleep(0.05)
        return False

    def stop(self):
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=10)
        self.server, self.thread = None, None


def bind_target(args, server_mod):
    """返回 (绑定地址, 期望端口, 是否局域网模式)。"""
    c = server_mod.cfg().get()
    enabled = bool(c.get("lan_enabled")) or bool(args.lan)
    preferred = (args.lan_port or args.port
                 or int(c.get("lan_port") or 18790))
    return ("0.0.0.0" if enabled else "127.0.0.1"), int(preferred), enabled


def main() -> int:
    ap = argparse.ArgumentParser(description="FnPlayer 桌面播放器")
    ap.add_argument("--port", type=int, default=0, help="指定本机服务端口")
    ap.add_argument("--no-browser", action="store_true", help="只启动服务，不打开窗口")
    ap.add_argument("--no-danmu", action="store_true", help="不自动拉起本地弹幕服务")
    ap.add_argument("--data-dir", default="", help="覆盖数据目录（测试用）")
    ap.add_argument("--lan", action="store_true",
                    help="命令行强制开启局域网访问（覆盖配置）")
    ap.add_argument("--lan-port", type=int, default=0, help="覆盖局域网端口")
    ap.add_argument("--lan-password", default="", help="设置局域网访问码")
    args = ap.parse_args()
    if args.data_dir:
        os.environ["FNPLAYER_DATA"] = args.data_dir

    import server

    if args.lan_password:
        server.cfg().save({"lan_password": args.lan_password})

    stop = threading.Event()
    restart = threading.Event()
    server.set_shutdown_callback(stop.set)
    server.set_restart_callback(restart.set)
    svc = Service(server.app)

    def bring_up():
        host, preferred, enabled = bind_target(args, server)
        port = choose_port(preferred)
        if not svc.start(host, port):
            msg = f"端口 {port} 绑定失败，已回退到本机随机端口"
            server.set_lan_error(msg)
            log(f"bind {host}:{port} failed, fallback to loopback")
            host, enabled, port = "127.0.0.1", False, free_port()
            if not svc.start(host, port):
                return None, None, None
            server.set_lan_state(False, port)
            os.environ["FNPLAYER_PORT"] = str(port)
            log(f"fallback listen {host}:{port}")
            return host, port, enabled
        server.set_lan_state(host == "0.0.0.0", port)
        server.set_lan_error("")
        os.environ["FNPLAYER_PORT"] = str(port)
        log(f"listen {host}:{port} lan={enabled} forced={args.lan}")
        if enabled:
            import lan
            urls = lan.urls(port, str(server.cfg().get().get("lan_password") or ""))
            log("lan urls: " + ", ".join(u["url"] for u in urls))
        return host, port, enabled

    host, port, enabled = bring_up()
    if port is None:
        log("server failed to start")
        return 1
    url = f"http://127.0.0.1:{port}/"
    log("server ready " + url)

    if not args.no_danmu:
        # 便携版：设置页开关打开过时，随播放器一起拉起独立工作区的 Node 服务
        try:
            server.danmu_service.service.auto_start()
            log("danmu auto-start scheduled")
        except Exception:
            log("danmu auto-start failed:\n" + traceback.format_exc())

    proc = None
    if not args.no_browser:
        profile = data_dir() / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        try:
            proc = open_window(url, profile)
        except Exception:
            log("open window failed:\n" + traceback.format_exc())

    try:
        while True:
            if stop.wait(0.4):
                break
            if restart.is_set():
                restart.clear()
                log("rebind requested")
                svc.stop()
                host2, port2, enabled2 = bring_up()
                if port2 is None:
                    log("rebind failed, exiting")
                    break
                url = f"http://127.0.0.1:{port2}/"
                log(f"rebind done -> {host2}:{port2} lan={enabled2}")
                continue
            if proc is not None and proc.poll() is not None:
                log("window closed")
                break
    except KeyboardInterrupt:
        pass
    finally:
        svc.stop()
        try:
            server.danmu_service.service.stop()
        except Exception:
            pass
        log("exit")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        log("fatal:\n" + traceback.format_exc())
        raise
