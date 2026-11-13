# 第三方组件与上游许可 · NOTICE

FnPlayer 是一个**聚合了多个开源项目**的播放器。本文件列出全部第三方组件的
**上游 Git 仓库地址**与**许可证**，并说明它们在 FnPlayer 中的用途与所在位置。

许可证原文副本统一放在 [`THIRD_PARTY_LICENSES/`](THIRD_PARTY_LICENSES/) 目录。

---

## 1. 直接内置进发行包的源码

| 组件 | 上游 Git 仓库 | 许可证 | 在 FnPlayer 中的用途与位置 |
| --- | --- | --- | --- |
| **danmu_api** | https://github.com/huangxd-/danmu_api | **AGPL-3.0** | 弹幕核心。桌面版以「独立工作区」形式随便携包携带（`portable/workspace/danmu/`，原样保留其 `LICENSE`）；安卓版把裁剪后的核心源码打进 APK（`android/assets/danmu-core/`，由 `android/build_danmu_core.py` 从上游源码生成） |
| **nodejs-mobile** | https://github.com/nodejs-mobile/nodejs-mobile | **MIT** | 安卓端内嵌的 Node.js 运行时（`libnode.so`）。用于在手机上直接跑 danmu_api 核心，无需额外部署云服务 |
| **Node.js** | https://github.com/nodejs/node | **MIT** | 桌面便携版的免安装 Node 运行时（`portable/runtime/node/node.exe`），用于运行本地弹幕服务 |
| **fnmedia-ext** | https://github.com/yujian0815/fnmedia-ext | **MIT** | 桌面版苹果 CMS 内核模块（`app/kernel/`），原始许可保留在 `app/kernel/LICENSE.upstream` |
| **videojs-contrib-hls (hls.js)** | https://github.com/videojs/videojs-contrib-hls | **Apache-2.0** | 前端 HLS 播放（`app/web/hls.min.js`、`android/assets/web/hls.min.js`），许可原文见 `app/web/hls.LICENSE.txt` |

## 2. 间接依赖 / 工具链

| 组件 | 上游 Git 仓库 | 许可证 | 说明 |
| --- | --- | --- | --- |
| **danmu-api-android** | https://github.com/lilixu3/danmu-api-android | README 声明 MIT（仓库内未附 `LICENSE` 文件） | 本项目的**安卓整合方案参考来源**；安卓端 Node 运行时二进制（`libnode.so` / `libnative-lib.so` / `libc++_shared.so`）取自其 Release **v1.0.5.118** 的 APK |
| **dan-any** | https://github.com/ani-uni/dan-any | **LGPL-3.0** | 弹幕输出格式规范，由 danmu_api 间接引入 |
| **FastAPI** | https://github.com/fastapi/fastapi | MIT | 桌面端 HTTP 服务框架 |
| **Starlette** | https://github.com/encode/starlette | BSD-3-Clause | FastAPI 依赖 |
| **Uvicorn** | https://github.com/encode/uvicorn | BSD-3-Clause | ASGI 服务器 |
| **httpx** | https://github.com/encode/httpx | BSD-3-Clause | 异步 HTTP 客户端 |
| **requests** | https://github.com/psf/requests | Apache-2.0 | 仅上游内核 PoC 分支使用 |
| **segno** | https://github.com/heuer/segno | BSD-3-Clause | 局域网访问二维码生成（可选依赖） |

## 2.1 随发行包分发的 npm 运行时依赖

弹幕核心（danmu_api）依赖一批 npm 包。**桌面便携版分发完整 `node_modules`**，
**安卓版只分发静态扫描出的运行时依赖闭包**（`android/build_danmu_core.py`
裁掉了 redis 家族与 `@electric-sql/pglite` 等可选依赖）。
每个包目录内的 LICENSE 文件都随发行包分发。

| 组件 | 上游 Git 仓库 | 许可证 |
| --- | --- | --- |
| @dan-uni/dan-any | https://github.com/ani-uni/dan-any | LGPL-3.0-or-later |
| @bufbuild/protobuf | https://github.com/bufbuild/protobuf-es | Apache-2.0 AND BSD-3-Clause |
| @nodable/entities | https://github.com/nodable/val-parsers | MIT |
| bignumber.js | https://github.com/MikeMcl/bignumber.js | MIT |
| brotli | https://github.com/devongovett/brotli.js | MIT |
| chokidar | https://github.com/paulmillr/chokidar | MIT |
| data-uri-to-buffer | https://github.com/TooTallNate/node-data-uri-to-buffer | MIT |
| dotenv | https://github.com/motdotla/dotenv | BSD-3-Clause |
| drizzle-orm | https://github.com/drizzle-team/drizzle-orm | Apache-2.0 |
| esbuild（含 @esbuild/win32-x64） | https://github.com/evanw/esbuild | MIT |
| fast-xml-parser | https://github.com/NaturalIntelligence/fast-xml-parser | MIT |
| https-proxy-agent | https://github.com/TooTallNate/https-proxy-agent | MIT |
| node-fetch | https://github.com/node-fetch/node-fetch | MIT |
| opencc-js | https://github.com/BYVoid/OpenCC | Apache-2.0 |
| pako | https://github.com/nodeca/pako | MIT |
| redis / @redis/* | https://github.com/redis/node-redis | MIT |
| zod | https://github.com/colinhacks/zod | MIT |
| 其余包 | 见各包 `package.json` 的 `repository` 字段 | MIT / Apache-2.0 / BSD-3-Clause |

> **关于「补录」的许可文件**
>
> 下列 npm 包**上游既没在发行包里附带许可文本文件，仓库根目录也没有 LICENSE**
> （属于上游的发布遗漏）：`@bufbuild/protobuf`、`@nodable/entities`、`brotli`、
> `data-uri-to-buffer`、`redis` 与 `@redis/*` 全家族、`drizzle-orm`、
> `@esbuild/win32-x64`。
>
> 但它们的 `package.json` / npm registry 元数据**明确声明了许可证**
> （MIT / Apache-2.0），而这些许可都要求「随副本附带许可声明」。
> 因此 FnPlayer 按其声明的许可条款**补录**了许可全文，存放在
> `android/danmu-core-src/licenses/`（包名 → 许可文件的映射见该目录的
> `manifest.json`），构建时自动填入对应包目录；副本同时收录在
> [`THIRD_PARTY_LICENSES/`](THIRD_PARTY_LICENSES/)。
>
> 每份补录文件末尾都附有说明（写明为何补录、版权归属来自何处），
> **未对上游包内容作任何修改**。


## 3. 构建工具链（不随源码分发，仅构建时使用）

| 组件 | 上游 | 许可证 |
| --- | --- | --- |
| Android SDK Build-Tools / Platform Tools | https://developer.android.com/tools/releases/platform-tools | Android SDK 许可协议 |
| Eclipse Temurin JDK | https://adoptium.net/ | Apache-2.0 / GPLv2+CPE |
| Python | https://www.python.org/ | PSF License |
| Node.js（构建与校验用） | https://github.com/nodejs/node | MIT |

---

## 4. 组合作品的许可结论

**FnPlayer 整体以 AGPL-3.0 发布**（见仓库根目录 [`LICENSE`](LICENSE)）。

原因很直接：安卓端把 **danmu_api（AGPL-3.0）** 的源码直接编入安装包并随应用分发，
按 AGPL-3.0 的传染性要求，组合作品必须同样以 AGPL-3.0 提供**完整对应源码**。
本仓库即为该对应源码。

其中 **danmu_api 的完整源码已随本仓库提供**，位于
[`third_party/danmu_api/`](third_party/danmu_api/)（源 commit
`4c865798ddf8972fc80a6cc850d896f459d01d78`，说明见该目录的
`README.FnPlayer.md`），满足 AGPL-3.0 第 13 条「提供完整对应源码」的要求。
安卓端 APK 内也随包附带了 danmu_api 的 AGPL-3.0 许可原文
（`assets/danmu-core/LICENSE.danmu_api-AGPL-3.0.txt`）。

同时：

- 桌面版（`app/`、`tools/`）中属于 fnmedia-ext 的部分按其 MIT 许可使用，
  原始许可文件保留在 `app/kernel/LICENSE.upstream`；
- `app/web/hls.min.js` 按 Apache-2.0 使用，许可原文随文件保留；
- 其余第三方组件的许可均要求「保留版权声明与许可声明」，本仓库已逐项保留，
  并汇总在 `THIRD_PARTY_LICENSES/`。

> **给使用者的提醒**：AGPL-3.0 第 13 条要求「通过网络与修改版交互」的用户可以获得
> 对应源码。FnPlayer 的安卓端在**本机回环地址**运行弹幕核心，不对外提供服务；
> 桌面端的本地弹幕服务默认也只监听 `127.0.0.1`。若你修改了本项目并**通过网络向
> 他人提供服务**，必须同时向这些用户提供你的修改版源码。

## 5. 致谢

- [@huangxd-](https://github.com/huangxd-)/danmu_api —— 弹幕核心
- [@lilixu3](https://github.com/lilixu3)/danmu-api-android —— 安卓整合方案与 Node 运行时二进制
- [nodejs-mobile](https://github.com/nodejs-mobile/nodejs-mobile) —— 安卓端 Node 运行时
- [@yujian0815](https://github.com/yujian0815)/fnmedia-ext —— 苹果 CMS 内核
- [videojs](https://github.com/videojs/videojs-contrib-hls) —— hls.js
- [heuer](https://github.com/heuer/segno) —— segno

## 6. 内容与合规声明

FnPlayer **不预置任何播放源**，出厂配置为空，用户需自行添加自有或已获授权的
接口地址。它只做播放与弹幕的客户端聚合，不提供、不索引、不存储影视内容。
请仅用于访问自有或已授权内容。
