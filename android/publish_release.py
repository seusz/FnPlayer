#!/usr/bin/env python3
"""把成品发布到 GitHub Release（通过 REST API）。

用法：
    GH_TOKEN=<你的 access token> python android/publish_release.py \
        --tag v1.2.0 --body android/dist/RELEASE_BODY_v1.2.0.md

行为：
  * 标签不存在 → 新建 Release（draft=False，即创建即发布）
  * 标签已存在 → 更新正文与标题，并按需替换附件
  * 同名附件已存在且大小一致 → 跳过（避免重复上传 85MB 的 zip）

令牌只从环境变量 GH_TOKEN 读取，不落盘、不写入任何配置文件。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AND = Path(__file__).resolve().parent
ROOT = AND.parent
ASSETS_DIR = AND / "dist" / "release_assets"
API = "https://api.github.com"
UPLOAD = "https://uploads.github.com"
REPO = os.environ.get("GH_REPO", "seusz/FnPlayer")
API_VER = "2022-11-28"

# GitHub 会剥离附件名里的非 ASCII 字符：纯中文名会变成 "default.txt"，
# 中英混合名会丢掉中文部分（"使用说明-Android.txt" -> "-Android.txt"）。
# 所以上传前先把中文文件名映射成等义的英文名，网页上的下载链接才看得懂。
ASCII_NAMES = {
    "使用说明-Android.txt": "FnPlayer-Usage-Android.txt",
    "使用说明-Windows.txt": "FnPlayer-Usage-Windows.txt",
    "弹幕服务部署指引.txt": "FnPlayer-Danmu-Deploy-Guide.txt",
}


def asset_name(path: Path) -> str:
    return ASCII_NAMES.get(path.name, path.name)



def opener():
    # 必须显式清空代理：Windows 注册表里的系统代理可能指向一个没有进程在听的
    # 端口（127.0.0.1:7890），urllib 会照着它走，导致所有请求被拒。
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(method: str, url: str, token: str, data=None, content_type=None):
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VER,
        "User-Agent": "FnPlayer-publisher",
    }
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener().open(req, timeout=600) as r:
            raw = r.read()
            return r.status, (json.loads(raw.decode("utf-8")) if raw else {})
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("message", detail)
        except Exception:
            pass
        return e.code, {"message": detail, "raw": detail[:500]}


def find_release(token: str, tag: str):
    code, data = request("GET", "%s/repos/%s/releases" % (API, REPO), token)
    if code != 200:
        return None, "列出 Release 失败: %s %s" % (code, data.get("message"))
    for r in data:
        if r.get("tag_name") == tag:
            return r, None
    return None, None


def upsert_asset(token: str, release_id: int, path: Path):
    """上传一个附件；同名同大小则跳过。"""
    name = asset_name(path)
    size = path.stat().st_size
    code, rel = request("GET", "%s/repos/%s/releases/%d" % (API, REPO, release_id), token)
    if code == 200:
        for a in rel.get("assets") or []:
            if a["name"] == name:
                if a.get("size") == size and a.get("state") == "uploaded":
                    print("   = 跳过（已存在且一致）: %s  (%.1f MB)" % (name, size / 1048576))
                    return True
                print("   - 删除旧附件: %s" % name)
                request("DELETE", "%s/repos/%s/releases/assets/%d" % (API, REPO, a["id"]), token)
                break

    url = "%s/repos/%s/releases/%d/assets?name=%s" % (
        UPLOAD, REPO, release_id, urllib.parse.quote(name))
    print("   + 上传 %s  (%.1f MB) ..." % (name, size / 1048576))
    with open(path, "rb") as f:
        data = f.read()
    code, resp = request("POST", url, token, data=data,
                         content_type="application/octet-stream")
    if code not in (201, 200):
        print("   ! 上传失败 %s: %s" % (code, resp.get("message")))
        return False
    got = resp.get("name") or ""
    if got != name:
        # GitHub 仍然改名了（极端情况下会退回 default.<ext>），强制改回
        print("   ! GitHub 把名字改成了 %s，改回 %s" % (got, name))
        payload = json.dumps({"name": name}, ensure_ascii=False).encode("utf-8")
        code2, r2 = request(
            "PATCH", "%s/repos/%s/releases/assets/%d" % (API, REPO, resp["id"]),
            token, data=payload, content_type="application/json; charset=utf-8")
        if code2 == 200:
            print("   OK 已改名为 %s" % r2.get("name"))
        else:
            print("   ! 改名失败 %s: %s" % (code2, r2.get("message")))
    print("   OK %s  id=%s" % (name, resp.get("id")))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="v1.2.0")
    ap.add_argument("--name", default="FnPlayer v1.2.0")
    ap.add_argument("--body", default=str(AND / "dist" / "RELEASE_BODY_v1.2.0.md"))
    ap.add_argument("--assets-dir", default=str(ASSETS_DIR))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-publish", action="store_true", help="只更新草稿，不正式发布")
    args = ap.parse_args()

    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        sys.exit("请先设置环境变量 GH_TOKEN=<access token>")

    body_path = Path(args.body)
    body = body_path.read_text(encoding="utf-8") if body_path.is_file() else args.body
    adir = Path(args.assets_dir)
    files = sorted([p for p in adir.iterdir() if p.is_file()], key=lambda p: p.name)

    print("仓库: %s   标签: %s" % (REPO, args.tag))
    print("附件目录: %s  (%d 个文件)" % (adir, len(files)))
    for f in files:
        print("   - %s  (%.1f MB)  -> 附件名 %s" % (
            f.name, f.stat().st_size / 1048576, asset_name(f)))

    code, me = request("GET", "%s/user" % API, token)
    if code != 200:
        sys.exit("令牌无效或无法访问 /user: %s" % me.get("message"))
    print("令牌身份: %s" % me.get("login"))

    rel, err = find_release(token, args.tag)
    if err:
        sys.exit(err)

    if args.dry_run:
        print("\n--dry-run: 到此为止，未做任何写操作")
        return

    if rel is None:
        payload = json.dumps({
            "tag_name": args.tag,
            "name": args.name,
            "body": body,
            "draft": bool(args.no_publish),
            "prerelease": False,
        }).encode("utf-8")
        code, rel = request("POST", "%s/repos/%s/releases" % (API, REPO), token,
                            data=payload, content_type="application/json")
        if code not in (200, 201):
            sys.exit("创建 Release 失败: %s %s" % (code, rel.get("message")))
        print("\n已创建 Release id=%s tag=%s draft=%s" % (rel["id"], rel["tag_name"], rel["draft"]))
    else:
        payload = json.dumps({
            "tag_name": args.tag, "name": args.name, "body": body,
            "draft": bool(args.no_publish), "prerelease": False,
        }).encode("utf-8")
        code, rel = request("PATCH", "%s/repos/%s/releases/%d" % (API, REPO, rel["id"]),
                            token, data=payload, content_type="application/json")
        if code != 200:
            sys.exit("更新 Release 失败: %s %s" % (code, rel.get("message")))
        print("\n已更新 Release id=%s tag=%s draft=%s" % (rel["id"], rel["tag_name"], rel["draft"]))

    ok = True
    for f in files:
        if not upsert_asset(token, rel["id"], f):
            ok = False

    code, final = request("GET", "%s/repos/%s/releases/%d" % (API, REPO, rel["id"]), token)
    if code == 200:
        print("\n== 最终状态 ==")
        print("   标题: %s | 标签: %s | 草稿: %s | 发布时间: %s" % (
            final.get("name"), final.get("tag_name"), final.get("draft"),
            final.get("published_at")))
        print("   附件 %d 个:" % len(final.get("assets") or []))
        for a in final.get("assets") or []:
            print("     - %-42s %8.2f MB  %s" % (
                a["name"], a["size"] / 1048576, a.get("state")))
        print("   页面: %s" % final.get("html_url"))
    if not ok:
        sys.exit("有附件上传失败")
    print("\n发布完成。")


if __name__ == "__main__":
    main()
