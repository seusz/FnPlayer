package com.fnplayer.mobile;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Private loopback HTTP server. The WebView is its only client.
 *
 * It serves the bundled UI, exposes the CMS search/play APIs and proxies every
 * media request (m3u8 rewrite, AES key, segments, mp4 Range). Binding is
 * strictly 127.0.0.1 - no LAN service exists in this build.
 */
public final class LocalServer {
    private static final String COOKIE = "fn_token";
    private final Context ctx;
    private final Store store;
    private final Cms cms;
    private final Danmu danmu;
    private final String token;
    private final ExecutorService pool = Executors.newFixedThreadPool(12);
    private final Map<String, byte[]> posterCache = new LinkedHashMap<String, byte[]>();
    private ServerSocket server;
    private volatile int port;

    public LocalServer(Context c) {
        ctx = c.getApplicationContext();
        store = new Store(ctx);
        cms = new Cms(store);
        danmu = new Danmu(store);
        token = UUID.randomUUID().toString().replace("-", "").substring(0, 24);
    }

    public int port() { return port; }
    public String token() { return token; }

    public void start() throws IOException {
        server = new ServerSocket();
        server.setReuseAddress(true);
        server.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), 0));
        port = server.getLocalPort();
        Thread t = new Thread(new Runnable() {
            @Override public void run() { acceptLoop(); }
        }, "fnplayer-http");
        t.setDaemon(true);
        t.start();

        // 弹幕服务随应用启动在后台静默就绪：只做一次上游连通预热，
        // 不阻塞 WebView 加载，也不要求用户在设置里点任何开关。
        Thread warm = new Thread(new Runnable() {
            @Override public void run() { danmu.prewarm(); }
        }, "fnplayer-danmu-warmup");
        warm.setDaemon(true);
        warm.start();

        // 内置弹幕核心（Node.js + danmu_api）随后台线程安装并启动，
        // 就绪后自动接管弹幕请求；失败时保持内置镜像兜底。
        DanmuCore.start(ctx);
    }

    public void stop() {
        try { if (server != null) server.close(); } catch (Exception ignored) {}
        pool.shutdownNow();
    }

    private void acceptLoop() {
        while (server != null && !server.isClosed()) {
            try {
                final Socket s = server.accept();
                pool.execute(new Runnable() {
                    @Override public void run() { handle(s); }
                });
            } catch (Exception e) {
                if (server == null || server.isClosed()) break;
            }
        }
    }

    // ------------------------------------------------------------- HTTP parsing

    private static final class Req {
        String method = "GET";
        String path = "/";
        String query = "";
        Map<String, String> headers = new HashMap<String, String>();
        byte[] body = new byte[0];

        String header(String k) { return headers.get(k.toLowerCase(Locale.US)); }

        String param(String key) {
            for (String pair : query.split("&")) {
                int p = pair.indexOf('=');
                String k = p < 0 ? pair : pair.substring(0, p);
                if (!k.equals(key)) continue;
                String v = p < 0 ? "" : pair.substring(p + 1);
                try { return URLDecoder.decode(v, "UTF-8"); } catch (Exception e) { return v; }
            }
            return "";
        }

        int intParam(String key, int def) {
            try { return Integer.parseInt(param(key)); } catch (Exception e) { return def; }
        }

        JSONObject json() throws Exception {
            String s = new String(body, StandardCharsets.UTF_8);
            return s.trim().isEmpty() ? new JSONObject() : new JSONObject(s);
        }
    }

    private static String readLine(InputStream in) throws IOException {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        int c;
        while ((c = in.read()) != -1) {
            if (c == '\n') break;
            if (c != '\r') b.write(c);
        }
        if (c == -1 && b.size() == 0) return null;
        return new String(b.toByteArray(), StandardCharsets.ISO_8859_1);
    }

    private Req parse(InputStream in) throws IOException {
        String first = readLine(in);
        if (first == null || first.isEmpty()) return null;
        String[] parts = first.split(" ");
        if (parts.length < 2) return null;
        Req q = new Req();
        q.method = parts[0].toUpperCase(Locale.US);
        String target = parts[1];
        int i = target.indexOf('?');
        q.path = i < 0 ? target : target.substring(0, i);
        q.query = i < 0 ? "" : target.substring(i + 1);
        String line;
        while ((line = readLine(in)) != null && !line.isEmpty()) {
            int p = line.indexOf(':');
            if (p > 0) {
                q.headers.put(line.substring(0, p).trim().toLowerCase(Locale.US),
                        line.substring(p + 1).trim());
            }
        }
        int len = 0;
        try { len = Integer.parseInt(q.header("content-length")); } catch (Exception ignored) {}
        if (len > 0 && len <= 2 * 1024 * 1024) {
            q.body = new byte[len];
            int off = 0;
            while (off < len) {
                int n = in.read(q.body, off, len - off);
                if (n < 0) break;
                off += n;
            }
        }
        return q;
    }

    private boolean authorized(Req q) {
        // Static assets contain no private data. The token is injected into the
        // HTML only; leaking app.js/app.css to another local app is harmless.
        if (q.path.startsWith("/static/") || q.path.equals("/favicon.ico")) return true;
        String c = q.header("cookie");
        if (c != null && c.contains(COOKIE + "=" + token)) return true;
        if (token.equals(q.header("x-fn-token"))) return true;
        if (token.equals(q.param("t"))) return true;
        return false;
    }

    private static String reason(int code) {
        if (code == 200) return "OK";
        if (code == 206) return "Partial Content";
        if (code == 302) return "Found";
        if (code == 400) return "Bad Request";
        if (code == 403) return "Forbidden";
        if (code == 404) return "Not Found";
        if (code == 502) return "Bad Gateway";
        return "Error";
    }

    private void send(Req q, OutputStream out, int code, String type,
                      byte[] data, String extra) throws IOException {
        StringBuilder h = new StringBuilder();
        h.append("HTTP/1.1 ").append(code).append(' ').append(reason(code)).append("\r\n");
        h.append("Content-Type: ").append(type).append("\r\n");
        h.append("Content-Length: ").append(data.length).append("\r\n");
        h.append("Cache-Control: no-store\r\n");
        h.append("X-Content-Type-Options: nosniff\r\n");
        h.append("Connection: close\r\n");
        if (extra != null) h.append(extra);
        h.append("\r\n");
        out.write(h.toString().getBytes(StandardCharsets.US_ASCII));
        if (!"HEAD".equals(q.method)) out.write(data);
        out.flush();
    }

    private void json(Req q, OutputStream out, int code, JSONObject j) throws IOException {
        send(q, out, code, "application/json; charset=utf-8",
                j.toString().getBytes(StandardCharsets.UTF_8), null);
    }

    private void jsonArray(Req q, OutputStream out, JSONArray a) throws IOException {
        send(q, out, 200, "application/json; charset=utf-8",
                a.toString().getBytes(StandardCharsets.UTF_8), null);
    }

    private void error(Req q, OutputStream out, int code, String msg) {
        try {
            JSONObject j = new JSONObject();
            j.put("error", msg);
            json(q, out, code, j);
        } catch (Exception ignored) {}
    }

    // ------------------------------------------------------------------ routing

    private void handle(Socket socket) {
        try (Socket s = socket) {
            s.setSoTimeout(45_000);
            InputStream in = new BufferedInputStream(s.getInputStream());
            OutputStream out = new BufferedOutputStream(s.getOutputStream());
            Req q = parse(in);
            if (q == null) return;
            if (!authorized(q)) {
                error(q, out, 403, "forbidden");
                return;
            }
            route(q, out);
        } catch (Exception e) {
            // Broken pipe while seeking is normal for a video element.
        }
    }

    private void route(Req q, OutputStream out) throws Exception {
        String p = q.path;
        if (p.equals("/") || p.equals("/index.html")) {
            String extra = null;
            if (token.equals(q.param("t"))) {
                extra = "Set-Cookie: " + COOKIE + "=" + token
                        + "; Path=/; HttpOnly; SameSite=Strict\r\n";
            }
            asset(q, out, "index.html", "text/html; charset=utf-8", extra);
            return;
        }
        if (p.equals("/favicon.ico")) { asset(q, out, "icon.png", "image/png", null); return; }
        if (p.startsWith("/static/")) {
            String name = p.substring("/static/".length());
            asset(q, out, name, mime(name), "Cache-Control: public, max-age=86400\r\n");
            return;
        }
        if (p.equals("/api/health")) {
            JSONObject j = new JSONObject();
            j.put("ok", true).put("version", "1.2.0").put("platform", "android")
                    .put("lan", false);
            json(q, out, 200, j);
            return;
        }
        if (p.equals("/api/bootstrap")) {
            JSONObject c = store.configCopy();
            JSONObject j = new JSONObject();
            j.put("version", "1.2.0");
            j.put("platform", "android");
            j.put("config", c);
            j.put("config_file", new java.io.File(ctx.getFilesDir(), "config.json").getAbsolutePath());
            j.put("source_count", store.sources().size());
            json(q, out, 200, j);
            return;
        }
        if (p.equals("/api/search")) {
            long t0 = System.currentTimeMillis();
            JSONArray items = cms.search(q.param("q"),
                    store.config().optInt("search_timeout", 9) * 1000);
            JSONObject j = new JSONObject();
            j.put("items", items).put("elapsed_ms", System.currentTimeMillis() - t0)
                    .put("sources", store.enabledSources().size());
            json(q, out, 200, j);
            return;
        }
        if (p.equals("/api/discover")) {
            JSONArray items = cms.discover(q.intParam("pg", 1),
                    store.config().optInt("search_timeout", 9) * 1000);
            JSONObject j = new JSONObject();
            j.put("items", items).put("pg", q.intParam("pg", 1));
            json(q, out, 200, j);
            return;
        }
        if (p.equals("/api/title")) {
            JSONObject t = cms.get(q.param("tid"));
            if (t == null) t = recoverTitle(q.param("tid"));
            if (t == null) { error(q, out, 404, "未找到该影片，请重新搜索"); return; }
            json(q, out, 200, t);
            return;
        }
        if (p.equals("/api/play")) { play(q, out); return; }
        if (p.startsWith("/api/stream/")) { stream(q, out, p.substring("/api/stream/".length())); return; }
        if (p.equals("/api/poster")) { poster(q, out); return; }
        if (p.equals("/api/history")) { history(q, out); return; }
        if (p.equals("/api/history/clear")) { store.clearHistory(); ok(q, out); return; }
        if (p.equals("/api/favorites")) { favorites(q, out); return; }
        if (p.equals("/api/favorites/remove")) { removeFavorite(q, out); return; }
        if (p.equals("/api/favorites/clear")) { clearFavorites(q, out); return; }
        if (p.startsWith("/api/danmu/") || p.startsWith("/api/v2/")) {
            if (danmuApi(q, out, p)) return;
        }
        if (p.equals("/api/settings")) { settings(q, out); return; }
        if (p.equals("/api/sources")) { sources(q, out); return; }
        if (p.equals("/api/sources/bulk")) { bulkSources(q, out); return; }
        if (p.equals("/api/sources/toggle")) { toggleSource(q, out); return; }
        if (p.equals("/api/sources/delete")) { deleteSource(q, out); return; }
        if (p.equals("/api/sources/test")) { json(q, out, 200, cms.probe(q.json().optString("api", ""))); return; }
        if (p.equals("/api/config/import")) { importConfig(q, out); return; }
        error(q, out, 404, "not found");
    }

    private void ok(Req q, OutputStream out) throws Exception {
        JSONObject j = new JSONObject();
        j.put("ok", true);
        json(q, out, 200, j);
    }

    private static String mime(String name) {
        String n = name.toLowerCase(Locale.US);
        if (n.endsWith(".js")) return "application/javascript; charset=utf-8";
        if (n.endsWith(".css")) return "text/css; charset=utf-8";
        if (n.endsWith(".svg")) return "image/svg+xml";
        if (n.endsWith(".png")) return "image/png";
        if (n.endsWith(".jpg") || n.endsWith(".jpeg")) return "image/jpeg";
        if (n.endsWith(".txt")) return "text/plain; charset=utf-8";
        return "application/octet-stream";
    }

    private static final java.util.Set<String> ASSETS = new java.util.HashSet<String>(
            java.util.Arrays.asList("index.html", "app.css", "app.js", "danmu.js",
                    "hls.min.js", "hls.LICENSE.txt", "placeholder.svg", "icon.png"));

    private void asset(Req q, OutputStream out, String name, String type, String extra) throws Exception {
        if (!ASSETS.contains(name)) { error(q, out, 404, "not found"); return; }
        try (InputStream in = ctx.getAssets().open("web/" + name)) {
            ByteArrayOutputStream b = new ByteArrayOutputStream();
            Http.copy(in, b, 4 * 1024 * 1024);
            byte[] data = b.toByteArray();
            if (name.equals("index.html")) {
                String html = new String(data, StandardCharsets.UTF_8);
                html = html.replace("__FN_TOKEN__", token);
                data = html.getBytes(StandardCharsets.UTF_8);
            }
            send(q, out, 200, type, data, extra);
        }
    }

    // --------------------------------------------------------------------- play

    /**
     * Last-resort recovery when a history entry points at a title that is no
     * longer on disk (old build, cleared data, or a source that changed ids).
     * Re-search by the recorded name and rebind the result under the old id so
     * the existing history record stays valid.
     */
    private JSONObject recoverTitle(String tid) {
        if (tid == null || tid.isEmpty()) return null;
        String name = store.historyTitle(tid);
        if (name.isEmpty()) name = store.favoriteTitle(tid);
        if (name.isEmpty()) return null;
        int timeout = Math.min(store.config().optInt("search_timeout", 9) * 1000, 6000);
        try {
            JSONArray results = cms.search(name, timeout);
            if (results == null || results.length() == 0) return null;
            String norm = name.replaceAll("[\\s:：·_]", "");
            String chosen = "";
            for (int i = 0; i < results.length(); i++) {
                JSONObject b = results.optJSONObject(i);
                if (b == null) continue;
                if (norm.equals(b.optString("title", "").replaceAll("[\\s:：·_]", ""))) {
                    chosen = b.optString("id", "");
                    break;
                }
            }
            if (chosen.isEmpty()) chosen = results.optJSONObject(0).optString("id", "");
            if (chosen.isEmpty()) return null;
            JSONObject found = cms.get(chosen);
            if (found == null) return null;
            return cms.rebind(tid, found);
        } catch (Exception e) {
            return null;
        }
    }

    private void play(Req q, OutputStream out) throws Exception {
        JSONObject body = q.json();
        JSONObject t = cms.get(body.optString("tid", ""));
        if (t == null) t = recoverTitle(body.optString("tid", ""));
        if (t == null) { error(q, out, 404, "未找到该影片，请重新搜索该片名"); return; }
        JSONObject lines = t.optJSONObject("lines");
        if (lines == null || lines.length() == 0) { error(q, out, 404, "没有可播放的线路"); return; }

        String line = body.optString("line", "");
        if (!lines.has(line)) line = lines.keys().next();
        int epIndex = Math.max(0, body.optInt("ep", 0));
        String finalUrl = null;
        JSONObject chosen = null;
        String chosenLine = line;
        int chosenIndex = epIndex;

        for (int attempt = 0; attempt < 6; attempt++) {
            JSONArray eps = lines.optJSONArray(chosenLine);
            if (eps == null || eps.length() == 0) break;
            if (chosenIndex >= eps.length()) chosenIndex = 0;
            chosen = eps.optJSONObject(chosenIndex);
            if (chosen == null) break;
            String u = cms.resolveEpisode(chosen);
            if (u != null && (u.startsWith("http://") || u.startsWith("https://"))) {
                finalUrl = u;
                break;
            }
            JSONObject alt = fallback(lines, chosenLine, chosenIndex, chosen);
            if (alt == null) break;
            chosenLine = alt.getString("line");
            chosenIndex = alt.getInt("index");
        }
        if (finalUrl == null) { error(q, out, 502, "该线路暂时无法解析，请切换线路重试"); return; }

        String lower = finalUrl.toLowerCase(Locale.US);
        boolean isHls = lower.contains(".m3u8");
        String referer = chosen.optString("referer", "");
        if (referer.isEmpty()) referer = Http.siteRoot(finalUrl);
        Streams.Ctx sc = Streams.create(finalUrl, referer, t.optString("title", ""),
                chosenLine, chosen.optString("name", ""), isHls);

        JSONArray episodes = new JSONArray();
        JSONArray eps = lines.optJSONArray(chosenLine);
        if (eps != null) {
            for (int i = 0; i < eps.length(); i++) {
                JSONObject e = new JSONObject();
                e.put("name", eps.optJSONObject(i).optString("name", "第" + (i + 1) + "集"));
                e.put("index", i);
                episodes.put(e);
            }
        }
        JSONArray lineNames = new JSONArray();
        Iterator<String> it = lines.keys();
        while (it.hasNext()) lineNames.put(it.next());

        double resume = 0;
        JSONArray hist = store.history();
        for (int i = 0; i < hist.length(); i++) {
            JSONObject h = hist.optJSONObject(i);
            if (h != null && t.optString("id", "").equals(h.optString("id", ""))
                    && chosen.optString("name", "").equals(h.optString("episode", ""))) {
                resume = h.optDouble("ts", 0);
                break;
            }
        }
        if (!store.config().optBoolean("auto_resume", true)) resume = 0;

        JSONObject j = new JSONObject();
        j.put("kind", isHls ? "hls" : "file");
        j.put("url", "/api/stream/" + sc.token
                + (isHls ? "/playlist.m3u8" : "/media") + "?t=" + token);
        j.put("stream", sc.token);
        j.put("id", t.optString("id"));
        j.put("title", t.optString("title"));
        j.put("poster", t.optString("poster"));
        j.put("line", chosenLine);
        j.put("episode", chosen.optString("name", ""));
        j.put("ep_index", chosenIndex);
        j.put("lines", lineNames);
        j.put("episodes", episodes);
        j.put("resume", resume);
        json(q, out, 200, j);
    }

    private JSONObject fallback(JSONObject lines, String current, int index, JSONObject ep) {
        String target = ep == null ? "" : ep.optString("name", "");
        Iterator<String> it = lines.keys();
        while (it.hasNext()) {
            String name = it.next();
            if (name.equals(current)) continue;
            JSONArray eps = lines.optJSONArray(name);
            if (eps == null || eps.length() == 0) continue;
            for (int i = 0; i < eps.length(); i++) {
                JSONObject e = eps.optJSONObject(i);
                if (e == null) continue;
                if (!target.isEmpty() && target.equals(e.optString("name", ""))) {
                    try { return new JSONObject().put("line", name).put("index", i); }
                    catch (Exception ignored) { return null; }
                }
            }
            int i = Math.min(index, eps.length() - 1);
            try { return new JSONObject().put("line", name).put("index", i); }
            catch (Exception ignored) { return null; }
        }
        return null;
    }

    // ------------------------------------------------------------------- stream

    private void stream(Req q, OutputStream out, String rest) throws Exception {
        int slash = rest.indexOf('/');
        String tok = slash < 0 ? rest : rest.substring(0, slash);
        String tail = slash < 0 ? "" : rest.substring(slash + 1);
        Streams.Ctx c = Streams.get(tok);
        if (c == null) { error(q, out, 404, "stream expired"); return; }

        if (tail.equals("playlist.m3u8") || tail.equals("sub")) {
            String url;
            if (tail.equals("sub")) {
                url = c.subAt(q.intParam("i", -1));
                if (url == null) { error(q, out, 404, "sub playlist missing"); return; }
            } else {
                url = c.url;
            }
            Http.Resp up = null;
            try {
                up = Http.open(url, c.referer, null, 20_000, true);
                if (up.code != 200 || up.body == null) { error(q, out, 502, "无法获取播放清单"); return; }
                ByteArrayOutputStream b = new ByteArrayOutputStream();
                Http.copy(up.body, b, 2 * 1024 * 1024);
                String text = new String(b.toByteArray(), StandardCharsets.UTF_8);
                if (text.length() > 0 && text.charAt(0) == '\uFEFF') text = text.substring(1);
                if (!text.contains("#EXTM3U")) { error(q, out, 502, "上游不是 m3u8 清单"); return; }
                String base = up.url == null ? url : up.url.toString();
                String body = Streams.rewrite(text, base, c, token);
                send(q, out, 200, "application/vnd.apple.mpegurl",
                        body.getBytes(StandardCharsets.UTF_8),
                        "Access-Control-Allow-Origin: *\r\n");
            } catch (Exception e) {
                error(q, out, 502, "清单获取失败");
            } finally {
                if (up != null) up.close();
            }
            return;
        }
        if (tail.equals("key")) {
            String url = c.keyAt(q.intParam("i", -1));
            if (url == null) { error(q, out, 404, "key missing"); return; }
            byte[] cached;
            synchronized (c.keyCache) { cached = c.keyCache.get(url); }
            if (cached == null) {
                cached = Http.bytes(url, c.referer, 20_000);
                if (cached.length <= 64 * 1024) {
                    synchronized (c.keyCache) { c.keyCache.put(url, cached); }
                }
            }
            send(q, out, 200, "application/octet-stream", cached,
                    "Cache-Control: public, max-age=3600\r\n"
                    + "Access-Control-Allow-Origin: *\r\n");
            return;
        }
        String url = null;
        if (tail.equals("seg")) url = c.segAt(q.intParam("i", -1));
        else if (tail.equals("media")) url = c.url;
        if (url == null) { error(q, out, 404, "segment missing"); return; }
        proxyBinary(q, out, url, c.referer, null);
    }

    private void proxyBinary(Req q, OutputStream out, String url,
                             String referer, String forcedType) throws Exception {
        Http.Resp up = null;
        try {
            up = Http.open(url, referer, q.header("range"), 30_000, true);
            int code = up.code;
            if (code != 200 && code != 206) { error(q, out, 502, "上游返回 " + code); return; }
            String type = forcedType;
            if (type == null) type = up.header("content-type");
            if (type == null || type.isEmpty()) type = "application/octet-stream";
            String cl = up.header("content-length");
            String cr = up.header("content-range");
            StringBuilder h = new StringBuilder();
            h.append("HTTP/1.1 ").append(code).append(' ').append(reason(code)).append("\r\n");
            h.append("Content-Type: ").append(type).append("\r\n");
            if (cl != null && !cl.isEmpty()) h.append("Content-Length: ").append(cl).append("\r\n");
            if (cr != null && !cr.isEmpty()) h.append("Content-Range: ").append(cr).append("\r\n");
            h.append("Accept-Ranges: bytes\r\n");
            h.append("Cache-Control: no-store\r\n");
            h.append("Access-Control-Allow-Origin: *\r\n");
            h.append("Connection: close\r\n\r\n");
            out.write(h.toString().getBytes(StandardCharsets.US_ASCII));
            if (!"HEAD".equals(q.method) && up.body != null) Http.copy(up.body, out, 0);
            out.flush();
        } catch (Exception e) {
            error(q, out, 502, "媒体代理失败");
        } finally {
            if (up != null) up.close();
        }
    }

    private void poster(Req q, OutputStream out) throws Exception {
        String url = q.param("u");
        if (!url.startsWith("http://") && !url.startsWith("https://")) {
            error(q, out, 400, "bad url"); return;
        }
        try {
            byte[] data;
            synchronized (posterCache) { data = posterCache.get(url); }
            if (data == null) {
                data = Http.bytes(url, "", 12_000);
                synchronized (posterCache) {
                    posterCache.put(url, data);
                    while (posterCache.size() > 80) {
                        String oldest = posterCache.keySet().iterator().next();
                        posterCache.remove(oldest);
                    }
                }
            }
            String type = url.toLowerCase(Locale.US).contains(".png") ? "image/png" : "image/jpeg";
            send(q, out, 200, type, data, "Cache-Control: public, max-age=3600\r\n");
        } catch (Exception e) {
            error(q, out, 502, "海报获取失败");
        }
    }

    // --------------------------------------------------------------- danmaku

    /**
     * Local danmaku service endpoints. /api/danmu/* is what the bundled UI
     * uses; /api/v2/* exposes the same data in dandanplay's native shape so the
     * in-app service can also be consumed by any compatible client.
     */
    private boolean danmuApi(Req q, OutputStream out, String p) throws Exception {
        try {
            JSONObject r;
            if (p.equals("/api/danmu/status")) {
                r = danmu.status();
                r.put("ok", true);
            } else if (p.equals("/api/danmu/probe")) {
                r = danmu.probe(q.json().optString("api", ""));
            } else if (p.equals("/api/danmu/prewarm")) {
                danmu.prewarm();
                r = danmu.status();
                r.put("ok", true);
            } else if (p.equals("/api/danmu/search")) {
                r = new JSONObject();
                r.put("ok", true).put("items", danmu.searchSmart(q.param("keyword")));
            } else if (p.startsWith("/api/danmu/bangumi/")) {
                r = danmu.bangumi(tail(p, "/api/danmu/bangumi/"));
                r.put("ok", true);
            } else if (p.equals("/api/danmu/match")) {
                r = danmu.match(q.json().optString("fileName", ""));
                r.put("ok", true);
            } else if (p.startsWith("/api/danmu/comment/")) {
                r = danmu.comment(tail(p, "/api/danmu/comment/"));
                r.put("ok", true);
            } else if (p.equals("/api/v2/search/anime")) {
                r = danmu.raw("search/anime?keyword=" + q.param("keyword"));
            } else if (p.startsWith("/api/v2/bangumi/")) {
                r = danmu.raw("bangumi/" + tail(p, "/api/v2/bangumi/"));
            } else if (p.startsWith("/api/v2/comment/")) {
                r = danmu.raw("comment/" + tail(p, "/api/v2/comment/")
                        + "?from=0&withRelated=true&chConvert=0");
            } else if (p.equals("/api/v2/match")) {
                JSONObject m = danmu.match(q.json().optString("fileName", ""));
                JSONArray items = m.optJSONArray("items");
                JSONArray matches = new JSONArray();
                if (items != null) {
                    for (int i = 0; i < items.length(); i++) {
                        JSONObject x = items.optJSONObject(i);
                        if (x == null) continue;
                        JSONObject o = new JSONObject();
                        o.put("episodeId", x.get("episodeId"));
                        o.put("animeId", x.opt("animeId"));
                        o.put("animeTitle", x.optString("title", ""));
                        o.put("episodeTitle", x.optString("episodeTitle", ""));
                        o.put("shift", x.optDouble("shift", 0));
                        matches.put(o);
                    }
                }
                r = new JSONObject();
                r.put("isMatched", m.optBoolean("matched", false));
                r.put("matches", matches);
                r.put("errorCode", 0).put("success", true);
            } else {
                return false;
            }
            json(q, out, 200, r);
            return true;
        } catch (Exception e) {
            JSONObject j = new JSONObject();
            String msg = e.getMessage();
            j.put("ok", false).put("error", msg == null || msg.isEmpty() ? "弹幕服务异常" : msg);
            json(q, out, 502, j);
            return true;
        }
    }

    private static String tail(String path, String prefix) {
        String s = path.substring(prefix.length());
        try { return URLDecoder.decode(s, "UTF-8"); } catch (Exception e) { return s; }
    }

    // -------------------------------------------------------------- favorites

    private void favorites(Req q, OutputStream out) throws Exception {
        if ("POST".equals(q.method)) {
            JSONObject b = q.json();
            JSONObject item = b.optJSONObject("item");
            if (item != null && !item.optString("id", "").isEmpty()) {
                store.addFavorite(item);
                // Persist the full detail (including play lines) so the card in
                // "我的收藏" still opens after the app is killed and relaunched.
                JSONObject snap = b.optJSONObject("snapshot");
                if (snap != null) store.saveTitle(snap);
            }
        }
        favoritesJson(q, out);
    }

    private void removeFavorite(Req q, OutputStream out) throws Exception {
        store.removeFavorite(q.json().optString("id", ""));
        favoritesJson(q, out);
    }

    private void clearFavorites(Req q, OutputStream out) throws Exception {
        store.clearFavorites();
        favoritesJson(q, out);
    }

    private void favoritesJson(Req q, OutputStream out) throws Exception {
        JSONObject j = new JSONObject();
        j.put("ok", true).put("items", store.favorites());
        json(q, out, 200, j);
    }

    // ----------------------------------------------------------- config/history

    private void settings(Req q, OutputStream out) throws Exception {
        if ("POST".equals(q.method)) {
            JSONObject patch = q.json();
            JSONObject clean = new JSONObject();
            String[] keys = {"auto_resume", "search_timeout", "filter_junk", "max_results",
                    "danmu_enabled", "danmu_auto", "danmu_api", "danmu_opacity",
                    "danmu_font_size", "danmu_area", "danmu_speed", "danmu_offset",
                    "danmu_bold"};
            for (String k : keys) if (patch.has(k)) clean.put(k, patch.get(k));
            store.saveConfig(clean);
        }
        json(q, out, 200, store.configCopy());
    }

    private void sources(Req q, OutputStream out) throws Exception {
        if ("POST".equals(q.method)) {
            JSONObject b = q.json();
            String api = b.optString("api", "").trim();
            String name = b.optString("name", "").trim();
            List<JSONObject> parsed = store.parseBulk(name + " " + api);
            appendSources(parsed);
        }
        JSONArray arr = new JSONArray();
        for (JSONObject s : store.sources()) arr.put(s);
        JSONObject j = new JSONObject();
        j.put("items", arr);
        json(q, out, 200, j);
    }

    private void bulkSources(Req q, OutputStream out) throws Exception {
        List<JSONObject> parsed = store.parseBulk(q.json().optString("text", ""));
        int added = appendSources(parsed);
        JSONObject j = new JSONObject();
        j.put("ok", true).put("added", added).put("parsed", parsed.size());
        json(q, out, 200, j);
    }

    private synchronized int appendSources(java.util.List<JSONObject> list) throws Exception {
        JSONArray arr = new JSONArray();
        java.util.Set<String> seen = new java.util.HashSet<String>();
        for (JSONObject s : store.sources()) {
            arr.put(s);
            seen.add(s.optString("api", ""));
        }
        int added = 0;
        for (JSONObject s : list) {
            if (seen.add(s.optString("api", ""))) { arr.put(s); added++; }
        }
        store.saveSources(arr);
        return added;
    }

    private void toggleSource(Req q, OutputStream out) throws Exception {
        JSONObject b = q.json();
        int i = b.optInt("index", -1);
        JSONArray arr = new JSONArray();
        for (JSONObject s : store.sources()) arr.put(s);
        if (i >= 0 && i < arr.length()) {
            JSONObject s = arr.optJSONObject(i);
            s.put("enabled", b.optBoolean("enabled", !s.optBoolean("enabled", true)));
            store.saveSources(arr);
        }
        ok(q, out);
    }

    private void deleteSource(Req q, OutputStream out) throws Exception {
        int i = q.json().optInt("index", -1);
        JSONArray arr = new JSONArray();
        for (JSONObject s : store.sources()) arr.put(s);
        if (i >= 0 && i < arr.length()) {
            JSONArray next = new JSONArray();
            for (int k = 0; k < arr.length(); k++) if (k != i) next.put(arr.opt(k));
            store.saveSources(next);
        }
        ok(q, out);
    }

    private void importConfig(Req q, OutputStream out) throws Exception {
        String text = q.json().optString("text", "");
        JSONObject j = new JSONObject(text);
        store.saveConfig(j);
        ok(q, out);
    }

    private void history(Req q, OutputStream out) throws Exception {
        if ("POST".equals(q.method)) {
            JSONObject item = q.json();
            JSONArray next = new JSONArray();
            next.put(item);
            JSONArray old = store.history();
            for (int i = 0; i < old.length(); i++) {
                JSONObject h = old.optJSONObject(i);
                if (h == null) continue;
                boolean same = item.optString("id", "").equals(h.optString("id", ""))
                        && item.optString("episode", "").equals(h.optString("episode", ""));
                if (!same) next.put(h);
            }
            store.saveHistory(next);
        }
        jsonArray(q, out, store.history());
    }
}
