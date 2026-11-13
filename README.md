# FnPlayer

Windows 便携播放器 + Android APK。搜索、播放、弹幕一体化；不预置任何播放源，
由使用者自行添加自有或已获授权的接口。

* **桌面版**：Python/FastAPI 本机服务 + 系统浏览器 `--app` 模式窗口，免安装、免 .NET、
  免 Electron，只依赖 Windows 10/11 自带的 Edge。可选开启局域网访问（带访问码与二维码）。
* **Android 版**：Java 本机服务 + WebView，从 **1.2.0** 起**内置完整弹幕核心**
  （Node 运行时 + danmu_api），手机上无需额外部署云端弹幕服务。

当前版本：桌面端 `1.0.0` · Android 端 `1.2.0`（versionCode 3）

---

## 1. 功能

| | Windows | Android |
| --- | :---: | :---: |
| 苹果 CMS V10 接口搜索 / 详情 / 选集 | ✅ | ✅ |
| HLS (m3u8) 播放，同源代理重写 + 防盗链 Referer/UA 透传 | ✅ | ✅ |
| 弹幕（多平台聚合：豆瓣/360/腾讯/优酷/爱奇艺/咪咕/B站/人人/韩剧TV） | ✅ 内置本地弹幕服务 | ✅ **内置弹幕核心** |
| 我的收藏频道（落盘影片快照，可按片名恢复） | ✅ | ✅ |
| 继续观看（进程重启后仍可恢复） | ✅ | ✅ |
| 返回逐级回退（搜索结果 → 详情 → 选集播放） | ✅ | ✅ |
| 全屏播放返回后强制切回竖屏 | — | ✅ |
| 局域网访问（访问码 + 二维码） | ✅ 可选 | ❌ 仅本机 |

**Android 弹幕核心**：应用启动时在 `127.0.0.1:9322` 静默拉起内置弹幕核心，
提供与上游一致的 `/api/v2` 接口。国产剧、电影也能匹配到弹幕（例如《庆余年》
《繁花》《流浪地球》）。核心启动失败会自动回退到内置镜像，不影响其它功能。
首次启动需要解包并拉起核心，约 5~15 秒。

---

## 2. 仓库结构

```
FnPlayer/
├─ app/                     桌面版源码（Python + 前端）
│  ├─ main.py               入口：起服务 → 开窗口 → 收尾
│  ├─ server.py             FastAPI 应用：/api/* 与静态资源
│  ├─ hls.py                m3u8 解析 / 同源重写 / 分片转发
│  ├─ danmu.py              弹幕接口封装（/api/v2 契约）
│  ├─ danmu_service.py      弹幕服务进程生命周期
│  ├─ lan.py                局域网访问（访问码 + 二维码）
│  ├─ registry.py           播放源注册表
│  ├─ kernel/               苹果 CMS 内核（来自 fnmedia-ext，MIT）
│  └─ web/                  前端（index.html / app.js / danmu.js / hls.min.js）
├─ android/                 Android 端源码
│  ├─ src/com/fnplayer/mobile/    Java：LocalServer / Danmu / DanmuCore / Cms …
│  ├─ src/com/example/danmuapiapp/NodeBridge.java   JNI 桥（Node 运行时）
│  ├─ assets/web/            前端（与桌面端同源）
│  ├─ danmu-core-src/        内置弹幕核心启动器（CommonJS）
│  ├─ build_apk.py           打包脚本（支持 --abi / --all-abis）
│  ├─ build_danmu_core.py    裁剪并生成内置弹幕核心
│  ├─ verify_apk.py          成品验收（含「随包核心真实启动」实跑检查）
│  ├─ preview_ui.py          界面预览（9 个视口场景）
│  └─ publish_release.py     GitHub Release 发布脚本
├─ build/                   桌面版构建脚本（build.py / FnPlayer.spec）
├─ tools/
│  ├─ prepare_portable.py    组装便携版（弹幕服务工作区 + 便携 Node）
│  ├─ prepare_kernel.py      从 fnmedia-ext 提取内核模块
│  └─ fetch_sources.py       获取体积大的外部资源（弹幕核心源码 / Node 运行时）
├─ tests/                   冒烟测试与界面预览脚本
├─ portable/                 便携版附加资源（配置模板；运行时由脚本组装）
├─ NOTICE.md                **第三方组件、上游仓库地址与许可证**
├─ THIRD_PARTY_LICENSES/    各上游许可证原文副本
└─ LICENSE                  AGPL-3.0
```

---

## 3. 从源码构建

### 3.1 桌面版（Windows）

```bash
# 1) 准备弹幕核心源码与便携 Node
python tools/fetch_sources.py --danmu
python tools/prepare_portable.py --src ../danmu_api_src

# 2) 安装 Python 依赖
pip install -r requirements.txt

# 3) 构建（PyInstaller 打包 → 冒烟验证 → 便携 zip）
python build/build.py
# 产物：dist/FnPlayer/FnPlayer.exe、dist/FnPlayer-1.0.0-Windows-x64.zip
```

> `build/build.py` 依赖 PyInstaller；首次运行会自动准备工具链（体积较大）。
> 便携 Node 运行时若本机没有，会使用 `tools/prepare_portable.py` 找到的
> `node.exe`，或从 Node.js 官方分发包获取。

### 3.2 Android APK

```bash
# 1) 获取安卓端 Node 运行时（从上游 Release 提取 libnode.so 等，约 213 MB）
python tools/fetch_sources.py --node-runtime

# 2) 生成内置弹幕核心（裁剪依赖闭包，106 MB → 31 MB）
python android/build_danmu_core.py

# 3) 打包（按架构分别产出）
python android/build_apk.py --all-abis
# 产物：android/dist/FnPlayer-1.2.0-android-{arm64-v8a,armeabi-v7a,x86_64}.apk

# 4) 验收（含解包出内置核心并真实启动、实调搜索接口）
python android/verify_apk.py

# 5) 界面预览
python android/preview_ui.py
```

构建安卓端需要 JDK 17 与 Android SDK Build-Tools（aapt2 / d8 / zipalign / apksigner）。
`android/build_apk.py` 会把工具链放在 `build/_toolchain/`。

**为什么按架构分别出包**：`libnode.so` 单架构约 74 MB，三架构合包会超过 100 MB，
既拖慢安装也不利于分发。所以提供三个安装包，使用者按手机 CPU 架构选一个即可：

| 包 | 适用设备 |
| --- | --- |
| `FnPlayer-1.2.0-android-arm64-v8a.apk` | 绝大多数 2017 年后的手机（优先选这个） |
| `FnPlayer-1.2.0-android-armeabi-v7a.apk` | 较老的 32 位 ARM 设备 |
| `FnPlayer-1.2.0-android-x86_64.apk` | 模拟器或少数 x86 设备 |

### 3.3 发布到 GitHub Release

```bash
GH_TOKEN=<你的 access token> python android/publish_release.py --tag v1.2.0
```

脚本会创建（或更新）Release、写入正文、上传 `android/dist/release_assets/`
下的全部成品，并回读最终状态核对。令牌只从环境变量读取，不落盘。

---

## 4. 不在 Git 里的大文件

为保持仓库轻量，以下内容被 `.gitignore` 排除，需用脚本获取：

| 内容 | 体积 | 获取方式 |
| --- | --- | --- |
| 安卓端 Node 运行时（`android/lib/`） | ~213 MB | `python tools/fetch_sources.py --node-runtime` |
| 内置弹幕核心（`android/assets/danmu-core/`） | ~31 MB | `python android/build_danmu_core.py`（依赖上游源码） |
| 弹幕核心源码（`../danmu_api_src`） | ~2.4 MB | `python tools/fetch_sources.py --danmu` |
| 便携 Node 运行时（`portable/runtime/`） | ~80 MB | `python tools/prepare_portable.py` |
| 构建工具链（`build/_toolchain/`） | ~1.1 GB | `build/build.py` 自动准备 |
| 构建产物（`dist/`、`android/dist/`） | — | 不入库，已发布为 GitHub Release 附件 |

`tools/fetch_sources.py` 会校验提取出的原生库 SHA-256，指纹不符会明确报错。

---

## 5. 第三方组件与许可

FnPlayer 聚合了多个开源项目。**完整的上游仓库地址、许可证与用途说明见
[`NOTICE.md`](NOTICE.md)**，各许可证原文副本在 [`THIRD_PARTY_LICENSES/`](THIRD_PARTY_LICENSES/)。

主要上游：

| 组件 | 上游仓库 | 许可证 |
| --- | --- | --- |
| danmu_api（弹幕核心） | https://github.com/huangxd-/danmu_api | AGPL-3.0 |
| danmu-api-android（安卓整合方案参考、Node 运行时二进制来源） | https://github.com/lilixu3/danmu-api-android | README 声明 MIT |
| nodejs-mobile（安卓端 Node 运行时） | https://github.com/nodejs-mobile/nodejs-mobile | MIT |
| Node.js（桌面便携运行时） | https://github.com/nodejs/node | MIT |
| fnmedia-ext（苹果 CMS 内核） | https://github.com/yujian0815/fnmedia-ext | MIT |
| hls.js / videojs-contrib-hls | https://github.com/videojs/videojs-contrib-hls | Apache-2.0 |
| segno（二维码） | https://github.com/heuer/segno | BSD-3-Clause |

---

## 6. 许可

**FnPlayer 以 AGPL-3.0 发布**（[LICENSE](LICENSE)）。

因为安卓端把 danmu_api（AGPL-3.0）的源码直接编入安装包并随应用分发，
按 AGPL-3.0 的传染性要求，组合作品必须同样以 AGPL-3.0 提供完整对应源码 ——
本仓库即该对应源码。

按 AGPL-3.0 第 13 条：**如果你修改了本项目并通过网络向他人提供服务，
必须同时向这些用户提供你的修改版源码。**

---

## 7. 合规声明

* FnPlayer **不预置任何播放源**，出厂配置为空，需自行添加自有或已获授权的接口。
* 它只做播放与弹幕的客户端聚合，**不提供、不索引、不存储任何影视内容**。
* 桌面端局域网访问默认关闭；开启后由访问码中间件控制，且会显示二维码便于授权。
* Android 端**不对外开放任何端口**，内置弹幕核心只绑定 `127.0.0.1`。
* 请仅用于访问自有或已授权内容，勿用于商业传播。
