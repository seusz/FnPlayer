# 第三方源码副本 · danmu_api

本目录是 **[danmu_api](https://github.com/huangxd-/danmu_api)** 的源码副本，
按 **AGPL-3.0** 授权。

## 为什么在这里

安卓端 1.2.0 起把裁剪后的弹幕核心**编入了 APK 并随应用分发**。
AGPL-3.0 第 13 条要求：向他人分发该作品时，必须同时提供**完整对应源码**。
因此核心源码随本仓库一并公开，无需任何人再从别处获取。

桌面便携版使用的是同一份源码（`portable/workspace/danmu/danmu_api/`，
由 `tools/prepare_portable.py` 从本目录组装）。

## 来源

| 项 | 值 |
| --- | --- |
| 上游仓库 | https://github.com/huangxd-/danmu_api |
| 源 commit | `4c865798ddf8972fc80a6cc850d896f459d01d78` |
| commit 日期 | 2026-11-07 |
| commit 标题 | `fix(hongguo): 修复开启全集合并后无法获取弹幕 (#553)` |
| 许可证 | AGPL-3.0（原文见本目录 `LICENSE`） |

如需更新到更新的上游版本：

```bash
python tools/fetch_sources.py --danmu   # 下载最新版到 ../danmu_api_src
# 对比 third_party/danmu_api/danmu_api 与 ../danmu_api_src/danmu_api
# 确认改动后覆盖本目录，并更新上表中的源 commit
```

## 本项目对其施加的改动

改动全部由 `tools/prepare_portable.py` 在组装时**自动施加**，
且是幂等的（检测到已打过补丁就跳过），因此本目录保持**与上游一致的原样**：

| 文件 | 改动 | 目的 |
| --- | --- | --- |
| `danmu_api/utils/server-listen-util.js` | 新增 `DANMU_API_HOST` 环境变量支持（未设置时行为与上游完全一致） | 本机自用的服务只绑定 `127.0.0.1`，不对外暴露 |
| `danmu_api/server.js` | 代理端口改读 `DANMU_API_PROXY_PORT`（默认仍是 5321） | 端口冲突时可调 |

安卓端另有构建期裁剪（`android/build_danmu_core.py`），**不修改源码内容**，
只排除仅服务端/构建期需要的文件：

- 排除 `server.js`：手机端改用 `android/danmu-core-src/server-entry.cjs` 作为入口
- 排除 `esm-shim.cjs`：仅构建期使用
- 排除 `tests/`：不随 APK 分发（但**完整源码在此目录已包含**）

## 依赖

上游的 `package.json` 声明的依赖通过 `npm install` 安装到
`portable/workspace/danmu/node_modules/`（不随源码入库）。
安卓端只把**静态扫描出的实际运行时依赖闭包**打进 APK
（`android/build_danmu_core.py`），这些包各自的 LICENSE 文件随包分发。

上游 `package.json` 声明的 `license` 字段是 `ISC`，但仓库根目录的 `LICENSE`
文件是 **AGPL-3.0**。以仓库中的 `LICENSE` 文件为准（详见 [`NOTICE.md`](../../NOTICE.md)）。
