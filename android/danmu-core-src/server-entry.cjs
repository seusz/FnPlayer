/**
 * FnPlayer 安卓端 · 内置弹幕核心启动器
 * ------------------------------------------------------------------
 * 作用：把 danmu_api 核心以 /api/v2 兼容协议跑在手机本机 127.0.0.1 上，
 *       供播放器内部调用。不对外网开放任何端口。
 *
 * 设计要点：
 *  1. 本文件是 CommonJS，通过动态 import() 加载 ESM 的 danmu_api/worker.js，
 *     规避 Android 端缺少 esbuild 二进制的问题。
 *  2. 只监听回环地址，端口由 DANMU_API_PORT 指定（默认 9322，避开
 *     danmu-api-android 常用的 9321）。
 *  3. 不使用 dotenv：所有配置通过环境变量由播放器注入，避免额外依赖。
 *  4. 任何异常都打印到 stdout，便于播放器读取日志定位问题。
 */
'use strict';

const http = require('http');
const path = require('path');
const { pathToFileURL } = require('url');

const PORT = parseInt(process.env.DANMU_API_PORT || '9322', 10);
const HOST = process.env.DANMU_API_HOST || '127.0.0.1';
const TOKEN = process.env.TOKEN || '87654321';
const CORE_DIR = path.join(__dirname, 'danmu_api');

// 播放源顺序：默认覆盖国产剧/电影/韩剧/综艺的常用站点。
// 可通过 SOURCE_ORDER 环境变量覆盖。
if (!process.env.SOURCE_ORDER) {
  process.env.SOURCE_ORDER =
    'douban,360,renren,hanjutv,tencent,youku,iqiyi,migu,bilibili,dandan';
}
if (!process.env.TOKEN) process.env.TOKEN = TOKEN;
if (!process.env.LOG_LEVEL) process.env.LOG_LEVEL = 'warn';
if (!process.env.SEARCH_CACHE_MINUTES) process.env.SEARCH_CACHE_MINUTES = '3';
if (!process.env.COMMENT_CACHE_MINUTES) process.env.COMMENT_CACHE_MINUTES = '3';
if (!process.env.LOCAL_CACHE_ENABLED) process.env.LOCAL_CACHE_ENABLED = 'false';
if (!process.env.DANMU_API_HOST) process.env.DANMU_API_HOST = HOST;
if (!process.env.DANMU_API_PORT) process.env.DANMU_API_PORT = String(PORT);
// 核心默认限流 3 次/分钟（同 IP），播放器一次播放会连续发起匹配/弹幕/搜索，
// 这里只监听回环地址且仅供应用自身使用，放开限制避免误伤。
if (!process.env.RATE_LIMIT_MAX_REQUESTS) process.env.RATE_LIMIT_MAX_REQUESTS = '0';

function log() {
  const args = Array.prototype.slice.call(arguments);
  try {
    console.log.apply(console, ['[fn-danmu]'].concat(args));
  } catch (e) {
    /* ignore */
  }
}

async function readBody(req) {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  return Buffer.concat(chunks);
}

function buildUrl(req) {
  const host = req.headers['host'] || (HOST + ':' + PORT);
  return 'http://' + host + req.url;
}

async function main() {
  log('启动中，核心目录:', CORE_DIR);
  log('Node 版本:', process.version);

  const workerUrl = pathToFileURL(path.join(CORE_DIR, 'worker.js')).href;
  const mod = await import(workerUrl);
  const handleRequest = mod.handleRequest;
  if (typeof handleRequest !== 'function') {
    throw new Error('danmu_api/worker.js 未导出 handleRequest');
  }
  log('核心加载完成');

  const server = http.createServer(async (req, res) => {
    // 播放器内部健康检查
    if (req.url === '/__fn_health') {
      res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' });
      res.end(JSON.stringify({
        ok: true, pid: process.pid, node: process.version,
        port: PORT, host: HOST, sources: process.env.SOURCE_ORDER
      }));
      return;
    }

    try {
      const fullUrl = buildUrl(req);
      let clientIp = req.socket && req.socket.remoteAddress ? req.socket.remoteAddress : '127.0.0.1';
      if (clientIp.startsWith('::ffff:')) clientIp = clientIp.substring(7);

      let body;
      if (req.method === 'POST' || req.method === 'PUT' || req.method === 'PATCH') {
        body = await readBody(req);
      }

      const webRequest = new globalThis.Request(fullUrl, {
        method: req.method,
        headers: req.headers,
        body: body && body.length ? body : undefined,
        duplex: 'half',
      });

      const webResponse = await handleRequest(webRequest, process.env, 'node', clientIp);

      res.statusCode = webResponse.status;
      webResponse.headers.forEach(function (value, key) {
        const lower = key.toLowerCase();
        if (lower === 'content-encoding' || lower === 'content-length') return;
        res.setHeader(key, value);
      });
      const buf = Buffer.from(await webResponse.arrayBuffer());
      res.setHeader('content-length', buf.length);
      res.end(buf);
    } catch (err) {
      log('请求处理异常:', (err && (err.stack || err.message)) || err);
      if (!res.headersSent) res.writeHead(500, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: String((err && err.message) || err) }));
    }
  });

  server.on('error', function (err) {
    log('HTTP 服务错误:', (err && err.message) || err);
    process.exitCode = 1;
  });

  server.listen(PORT, HOST, function () {
    log('弹幕核心已就绪 -> http://' + HOST + ':' + PORT);
  });

  process.on('uncaughtException', function (err) {
    log('未捕获异常:', (err && (err.stack || err.message)) || err);
  });
  process.on('unhandledRejection', function (err) {
    log('未处理的 Promise 拒绝:', (err && (err.stack || err.message)) || err);
  });
}

main().catch(function (err) {
  log('核心启动失败:', (err && (err.stack || err.message)) || err);
  process.exitCode = 1;
});
