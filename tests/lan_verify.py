#!/usr/bin/env python3
"""打包后 exe 的局域网模式端到端验证。

与 smoke_test.py（源码态 / mock）不同，本脚本启动真实的 dist/FnPlayer/FnPlayer.exe：
  1. 用独立数据目录以局域网模式启动（绑定 0.0.0.0）
  2. 验证访问码登录页 / cookie / 管理接口限制 / 二维码 SVG 可用
  3. 通过 /api/lan/apply 关闭再打开开关，验证不重启 exe 即可重新绑定
  4. 收尾退出进程

用法：python tests/lan_verify.py [port] [exe路径]
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
EXE = (Path(sys.argv[2]) if len(sys.argv) > 2
       else ROOT / "dist" / "FnPlayer" / "FnPlayer.exe")
KEY = "lan_test_9527"


def check(name: str, cond: bool, extra: str = ""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          ((" | " + str(extra)) if extra else ""), flush=True)
    if not cond:
        globals()["FAILED"] = True


def wait_health(client: httpx.Client, after_epoch: int | None = None,
                seconds: float = 25) -> dict | None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            j = client.get("/api/health", timeout=2).json()
            if j.get("ok") and (after_epoch is None or j.get("epoch", 0) > after_epoch):
                return j
        except Exception:
            pass
        time.sleep(0.3)
    return None


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18795
    if not EXE.is_file():
        print("请先运行 python build/build.py 生成 " + str(EXE))
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="fnplayer-lan-"))
    (tmp / "config.json").write_text(json.dumps({
        "sources": [],
        "lan_enabled": True,
        "lan_port": port,
        "lan_password": KEY,
        "danmu_service_enabled": False,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    proc = subprocess.Popen(
        [str(EXE), "--no-browser", "--no-danmu", "--data-dir", str(tmp),
         "--lan-port", str(port)], cwd=str(EXE.parent))
    local = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=6,
                         trust_env=False, follow_redirects=False)
    remote = None
    try:
        h = wait_health(local)
        if not h:
            log = tmp / "launch.log"
            print("服务未能启动；launch.log:\n" +
                  (log.read_text(encoding="utf-8", errors="replace")[-2000:]
                   if log.exists() else "无日志"))
            return 1
        check("exe 以局域网模式启动", bool(h.get("ok")), h)
        html = local.get("/").text
        m = re.search(r'name="fn-token" content="([^"]+)"', html)
        token = m.group(1) if m else ""
        H = {"X-Fn-Token": token}
        check("首页注入会话令牌", bool(token))
        check("设置页含局域网面板", "lanApplyBtn" in html and "setLanKey" in html)

        st = local.get("/api/lan").json()
        check("实际绑定 0.0.0.0", st.get("active") is True
              and st.get("bind") == "0.0.0.0", st.get("bind"))
        check("监听端口与配置一致", st.get("port") == port, st.get("port"))
        check("生成本机访问地址", bool(st.get("urls"))
              and all(f"key={KEY}" in u["url"] for u in st["urls"]),
              [u["ip"] for u in st.get("urls", [])])
        qr = local.get("/api/lan/qr?i=0")
        check("二维码组件在打包后可用", qr.status_code == 200
              and qr.headers.get("content-type", "").startswith("image/svg+xml")
              and "<svg" in qr.text, qr.status_code)

        # 用本机真实局域网 IP 模拟另一台设备
        ip = st["ips"][0] if st.get("ips") else ""
        check("已枚举到局域网 IP", bool(ip), st.get("ips"))
        if not ip:
            return 1
        remote = httpx.Client(base_url=f"http://{ip}:{port}", timeout=6,
                              trust_env=False, follow_redirects=False)
        r = remote.get("/")
        check("远端未认证显示访问码页", r.status_code == 401
              and "访问码" in r.text, r.status_code)
        check("远端未认证 API 被拒绝",
              remote.get("/api/health").status_code == 401)
        r = remote.get("/", params={"key": KEY})
        check("扫码链接自动换 cookie", r.status_code == 302
              and "fn_lan=" in r.headers.get("set-cookie", ""), r.status_code)
        remote.cookies.set("fn_lan", __import__("hashlib").sha256(
            ("fnplayer-lan|" + KEY).encode()).hexdigest())
        check("认证后远端可打开页面", remote.get("/").status_code == 200)
        check("认证后远端只读接口可用",
              remote.get("/api/health").json().get("ok") is True)
        check("远端管理接口被拒绝",
              remote.get("/api/settings").status_code == 403)
        check("远端弹幕服务控制被拒绝",
              remote.get("/api/danmu/service").status_code == 403)

        # 关闭开关 → 只重绑 uvicorn，exe 不退出
        e0 = local.get("/api/health").json().get("epoch", 0)
        r = local.post("/api/lan/apply", headers=H,
                       json={"enabled": False, "port": port, "password": KEY})
        check("关闭开关接口返回成功", r.json().get("ok") is True, r.text[:160])
        check("重绑后进程仍在", proc.poll() is None)
        check("服务已重新起来", wait_health(local, e0) is not None)
        st2 = local.get("/api/lan").json()
        check("关闭后回到仅本机", st2.get("active") is False
              and st2.get("bind") == "127.0.0.1", st2.get("bind"))

        # 再次打开
        e1 = local.get("/api/health").json().get("epoch", 0)
        local.post("/api/lan/apply", headers=H,
                   json={"enabled": True, "port": port, "password": KEY})
        check("二次重绑服务可用", wait_health(local, e1) is not None)
        st3 = local.get("/api/lan").json()
        check("再次开放局域网", st3.get("active") is True
              and st3.get("bind") == "0.0.0.0", st3.get("bind"))
        check("二维码二次生成正常",
              local.get("/api/lan/qr?i=0").status_code == 200)

        # 收尾：关掉局域网 + 退出应用
        e2 = local.get("/api/health").json().get("epoch", 0)
        local.post("/api/lan/apply", headers=H,
                   json={"enabled": False, "port": port, "password": KEY})
        wait_health(local, e2)
        try:
            local.post("/api/shutdown", headers=H, content=b"{}", timeout=3)
        except Exception:
            pass
        for _ in range(30):
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        check("应用可正常退出", proc.poll() is not None)
    finally:
        local.close()
        if remote:
            remote.close()
        if proc.poll() is None:
            proc.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n结果：" + ("存在失败项" if globals().get("FAILED") else "全部通过"))
    return 1 if globals().get("FAILED") else 0


if __name__ == "__main__":
    raise SystemExit(main())
