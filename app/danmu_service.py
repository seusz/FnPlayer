#!/usr/bin/env python3
"""便携版「本地弹幕服务」进程管理。

弹幕服务是独立的 Node.js 工作区（workspace/danmu，参考上游「本地运行」：
npm install → node ./danmu_api/server.js）。本模块只负责：

- 用便携 Node 运行时（runtime/node/node.exe）拉起 / 停止该子进程
- 只绑定 127.0.0.1，日志写入工作区 logs/
- 健康检查、状态查询、随主进程退出时回收
- 与 config.json 的开关联动，并把 danmu_api 自动指向本地服务

测试可用 FNPLAYER_DANMU_CMD（JSON 数组）覆盖启动命令，注入一个轻量
HTTP 服务验证启停链路，不必真的启动 Node。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _direct_opener():
    # 关键：本机系统代理常是「开着但没进程」，urllib 默认会读注册表代理。
    # 本地回环探测必须显式直连。
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def asset_root() -> Path:
    """便携资源根目录：打包态=exe 目录；开发态=工程内 portable/。"""
    import config
    root = config.portable_root()
    if root is None:
        return Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False):
        return root
    dev = root / "portable"
    return dev if dev.is_dir() else root


class DanmuService:
    def __init__(self):
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._logf = None
        self._last_error = ""
        self._started_at = 0.0

    # ------------------------------------------------------------ 配置 / 路径
    @staticmethod
    def settings() -> dict:
        import config
        c = config.config.get()
        try:
            port = int(c.get("danmu_service_port") or 9321)
        except (TypeError, ValueError):
            port = 9321
        port = port if 1 <= port <= 65535 else 9321
        token = str(c.get("danmu_service_token") or "87654321").strip() or "87654321"
        return {"port": port, "token": token,
                "enabled": bool(c.get("danmu_service_enabled"))}

    def local_base(self) -> str:
        s = self.settings()
        return f"http://127.0.0.1:{s['port']}/{s['token']}"

    def paths(self) -> dict:
        root = asset_root()
        work = root / "workspace" / "danmu"
        return {
            "root": root,
            "node": root / "runtime" / "node" / "node.exe",
            "work": work,
            "server": work / "danmu_api" / "server.js",
            "log": work / "logs" / "service.log",
        }

    def available(self) -> tuple[bool, str]:
        if os.environ.get("FNPLAYER_DANMU_CMD", "").strip():
            return True, ""
        p = self.paths()
        if not p["node"].is_file():
            return False, "缺少便携 Node 运行时（runtime/node/node.exe）"
        if not p["server"].is_file():
            return False, "缺少弹幕服务独立工作区（workspace/danmu）"
        return True, ""

    def sync_env(self):
        """让工作区 config/.env 与播放器设置一致，便于脱离播放器手动启动。"""
        s = self.settings()
        f = self.paths()["work"] / "config" / ".env"
        # 测试注入的模拟服务不应改写真实工作区配置
        if os.environ.get("FNPLAYER_DANMU_CMD", "").strip() or not f.is_file():
            return
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except Exception:
            return
        want = {"TOKEN": s["token"], "DANMU_API_PORT": str(s["port"])}
        out, seen = [], set()
        for line in lines:
            key = line.split("=", 1)[0].strip()
            if key in want:
                out.append(f"{key}={want[key]}")
                seen.add(key)
            else:
                out.append(line)
        for k, v in want.items():
            if k not in seen:
                out.append(f"{k}={v}")
        try:
            f.write_text("\n".join(out) + "\n", encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------ 状态探测
    def _port_alive(self, port: int) -> bool:
        # 先做 TCP 连通，再要一个 HTTP 响应（任何状态码都算服务在跑），
        # 避免把「端口被非 HTTP 程序占用」误判成弹幕服务已启动。
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.6):
                pass
        except OSError:
            return False
        try:
            with _direct_opener().open(f"http://127.0.0.1:{port}/", timeout=1.5) as r:
                return r.status < 600
        except urllib.error.HTTPError as e:
            return e.code < 600
        except Exception:
            return False

    def log_tail(self, lines: int = 12) -> str:
        p = self.paths()["log"]
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            return "\n".join(text.splitlines()[-lines:])
        except Exception:
            return ""

    def status(self) -> dict:
        s = self.settings()
        ok, why = self.available()
        with self._lock:
            managed = self._proc is not None and self._proc.poll() is None
            pid = self._proc.pid if managed else None
            if self._proc is not None and not managed:
                self._proc = None
        ready = self._port_alive(s["port"]) if ok else False
        return {
            "ok": True,
            "available": ok,
            "reason": why,
            "running": ready,
            "managed": managed,
            "pid": pid,
            "port": s["port"],
            "token": s["token"],
            "enabled": s["enabled"],
            "base": self.local_base(),
            "workspace": str(self.paths()["work"]),
            "node": str(self.paths()["node"]),
            "log_tail": self.log_tail(),
            "error": self._last_error,
            "started_at": self._started_at,
        }

    # ------------------------------------------------------------ 启停
    def start(self, wait: float = 25.0) -> dict:
        ok, why = self.available()
        if not ok:
            self._last_error = why
            return {"ok": False, "error": why, **self.status()}
        s = self.settings()
        if self._port_alive(s["port"]):
            return {"ok": True, "already": True, **self.status()}

        p = self.paths()
        override = os.environ.get("FNPLAYER_DANMU_CMD", "").strip()
        if override:
            try:
                cmd = json.loads(override)
            except Exception:
                cmd = None
            if not isinstance(cmd, list) or not cmd:
                self._last_error = "FNPLAYER_DANMU_CMD 需为 JSON 数组"
                return {"ok": False, "error": self._last_error, **self.status()}
        else:
            cmd = [str(p["node"]), "danmu_api/server.js"]

        env = os.environ.copy()
        # 上游配置优先级：系统环境变量 > .env；这里显式注入，保证与播放器一致
        env.update({
            "TOKEN": s["token"],
            "DANMU_API_PORT": str(s["port"]),
            "DANMU_API_HOST": "127.0.0.1",
            "DANMU_API_PROXY_PORT": "5321",
            "NODE_ENV": "production",
            "NO_PROXY": "127.0.0.1,localhost",
        })
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                  "ALL_PROXY", "all_proxy"):
            env.pop(k, None)

        p["log"].parent.mkdir(parents=True, exist_ok=True)
        logf = p["log"].open("a", encoding="utf-8", errors="replace")
        logf.write(f"\n===== FnPlayer start {time.strftime('%Y-%m-%d %H:%M:%S')} "
                   f"port={s['port']} =====\n")
        logf.flush()
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(p["work"]), env=env,
                stdin=subprocess.DEVNULL, stdout=logf,
                stderr=subprocess.STDOUT, close_fds=True,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception as e:
            logf.close()
            self._last_error = f"启动失败：{e}"
            return {"ok": False, "error": self._last_error, **self.status()}
        with self._lock:
            self._proc, self._logf = proc, logf
            self._started_at = time.time()
        self._last_error = ""

        deadline = time.time() + wait
        while time.time() < deadline:
            if self._port_alive(s["port"]):
                return {"ok": True, "already": False, **self.status()}
            if proc.poll() is not None:
                self._last_error = (f"弹幕服务进程提前退出（rc={proc.returncode}）；"
                                    "请查看设置页日志")
                return {"ok": False, "error": self._last_error, **self.status()}
            time.sleep(0.35)
        self._last_error = "启动超时，请查看设置页日志"
        return {"ok": False, "error": self._last_error, **self.status()}

    def stop(self) -> dict:
        with self._lock:
            proc, logf = self._proc, self._logf
            self._proc, self._logf = None, None
        self._last_error = ""
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:
                    pass
        if logf is not None:
            try:
                logf.write(f"===== FnPlayer stop {time.strftime('%H:%M:%S')} =====\n")
                logf.close()
            except Exception:
                pass
        return {"ok": True, **self.status()}

    # ------------------------------------------------------------ 配置联动
    def apply(self, enabled: bool, patch: dict | None = None) -> dict:
        """设置页开关：改配置 → 启停服务 → 同步 danmu_api 地址。"""
        import config
        patch = patch or {}
        changes: dict = {"danmu_service_enabled": bool(enabled)}
        for k in ("danmu_service_port", "danmu_service_token"):
            if k in patch:
                changes[k] = patch[k]
        # 端口/令牌变化时先停旧进程，避免留下孤儿服务
        if patch:
            self.stop()
        config.config.save(changes)
        # 只有用户明确改了端口/令牌时才回写工作区 .env；
        # 单纯开关切换（含构建验收）不动它。
        if any(k in patch for k in ("danmu_service_port", "danmu_service_token")):
            self.sync_env()
        if not enabled:
            self.stop()
            c = config.config.get()
            base = str(c.get("danmu_api") or "").strip()
            if not base or base.rstrip("/") == self.local_base():
                config.config.save({"danmu_api": ""})
            return {"ok": True, "action": "stopped", **self.status()}
        res = self.start()
        if res.get("ok"):
            c = config.config.get()
            base = str(c.get("danmu_api") or "").strip()
            if not base or base.startswith(f"http://127.0.0.1:"):
                config.config.save({"danmu_api": self.local_base()})
        res["action"] = "started" if res.get("ok") else "failed"
        return res

    def auto_start(self):
        """桌面壳启动后按配置自动拉起（后台线程，不阻塞窗口）。"""
        if not self.settings()["enabled"]:
            return None

        def run():
            try:
                res = self.start()
                import config
                if res.get("ok"):
                    c = config.config.get()
                    base = str(c.get("danmu_api") or "").strip()
                    if not base or base.startswith("http://127.0.0.1:"):
                        config.config.save({"danmu_api": self.local_base()})
            except Exception:
                pass

        t = threading.Thread(target=run, daemon=True, name="danmu-autostart")
        t.start()
        return t


service = DanmuService()
