package com.fnplayer.mobile;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * Danmaku service for the player.
 *
 * Three backends, in priority order:
 *  - core: the bundled Node.js + danmu_api core running on this phone
 *    (127.0.0.1:9322). Aggregates Douban / 360 / Tencent / Youku / iQiyi /
 *    Migu / Bilibili / ... so domestic dramas and films are covered.
 *  - custom: a user-supplied full danmu_api deployment (Vercel / EdgeOne /
 *    Cloudflare / Docker). Its POST /api/v2/match resolves season/episode
 *    across platforms in one request.
 *  - builtin: the dandanplay mirror used by huangxd-/danmu_api's
 *    sources/dandan.js. It only indexes anime/animation, so it is the
 *    last-resort fallback when neither of the above is available.
 */
public final class Danmu {
    private static final String MIRROR = "https://api.danmaku.weeblify.app/ddp/v1?path=";
    private static final int TIMEOUT = 20_000;
    private static final int MAX_BODY = 12 * 1024 * 1024;
    private static final int COLOR_DEFAULT = 0xFFFFFF;
    private static final String UA_HINT = "FnPlayer/1.2.0 (Android)";
    private static final double MIN_SCORE = 0.70;

    private final Store store;
    private volatile String readyState = "pending";   // pending / ok / error
    private volatile String readyText = "正在后台启动…";
    private volatile long readyAt = 0;
    private volatile long lastUpstreamMs = 0;
    private volatile String lastUpstreamError = "";
    private volatile String lastMatchTitle = "";
    private volatile String lastMatchReason = "";
    private volatile int lastMatchCount = 0;
    private final Map<String, JSONArray> commentCache =
            Collections.synchronizedMap(new LinkedHashMap<String, JSONArray>());

    public Danmu(Store store) { this.store = store; }

    // ------------------------------------------------------------- upstream

    /** Custom danmu_api instance, or "" for the built-in project mirror. */
    private String customBase() {
        String v = store.config().optString("danmu_api", "").trim();
        if (!v.startsWith("http://") && !v.startsWith("https://")) return "";
        // A desktop config.json often carries http://127.0.0.1:9321/<TOKEN>.
        // On a phone that address points at the phone itself.
        try {
            String host = new java.net.URL(v).getHost();
            if ("127.0.0.1".equals(host) || "localhost".equalsIgnoreCase(host)
                    || "::1".equals(host)) return "";
        } catch (Exception ignored) {}
        v = v.replaceAll("(?i)/api/v2/?$", "").replaceAll("/+$", "");
        return v;
    }

    /**
     * @param v2tail raw /api/v2 tail, e.g. "search/anime?keyword=葬送的芙莉莲".
     *               Callers must NOT pre-encode it; encoding happens here, once.
     */
    private String endpoint(String v2tail) {
        String core = DanmuCore.base();
        if (!core.isEmpty()) return core + "/api/v2/" + encodeQuery(v2tail);
        String custom = customBase();
        if (!custom.isEmpty()) return custom + "/api/v2/" + encodeQuery(v2tail);
        try {
            // The mirror takes the whole /v2/... path as its "path" parameter.
            // A missing /v2/ prefix or double-encoded keyword breaks it.
            return MIRROR + URLEncoder.encode("/v2/" + v2tail, "UTF-8")
                    .replace("%2F", "/").replace("+", "%20");
        } catch (Exception e) {
            return MIRROR + "/v2/" + v2tail;
        }
    }

    /**
     * Full danmu_api endpoint used for cross-platform matching:
     * the bundled core first, then a user-supplied deployment.
     */
    private String fullBase() {
        String core = DanmuCore.base();
        if (!core.isEmpty()) return core;
        return customBase();
    }

    /** For a self-hosted danmu_api: keep the path, encode only query values. */
    private static String encodeQuery(String tail) {
        int i = tail.indexOf('?');
        if (i < 0) return tail;
        StringBuilder b = new StringBuilder(tail.substring(0, i)).append('?');
        boolean first = true;
        for (String pair : tail.substring(i + 1).split("&")) {
            int j = pair.indexOf('=');
            String k = j < 0 ? pair : pair.substring(0, j);
            String v = j < 0 ? "" : pair.substring(j + 1);
            if (!first) b.append('&');
            first = false;
            b.append(k).append('=').append(enc(v));
        }
        return b.toString();
    }

    private JSONObject fetch(String v2tail) throws Exception {
        Http.Resp r = null;
        long t0 = System.currentTimeMillis();
        try {
            r = Http.open(endpoint(v2tail), "", null, TIMEOUT, true);
            if (r.code < 200 || r.code >= 300) {
                throw new Exception("弹幕服务返回 HTTP " + r.code
                        + (r.code == 429 ? "（请求过于频繁，请稍后重试）" : ""));
            }
            ByteArrayOutputStream b = new ByteArrayOutputStream();
            Http.copy(r.body, b, MAX_BODY);
            String s = new String(b.toByteArray(), StandardCharsets.UTF_8);
            if (!s.isEmpty() && s.charAt(0) == '\uFEFF') s = s.substring(1);
            JSONObject j = new JSONObject(s);
            if (j.has("success") && !j.optBoolean("success", true)) {
                String msg = j.optString("errorMessage", "");
                throw new Exception(msg.isEmpty() ? "弹幕服务返回失败" : msg);
            }
            lastUpstreamMs = System.currentTimeMillis() - t0;
            lastUpstreamError = "";
            return j;
        } catch (Exception e) {
            lastUpstreamMs = System.currentTimeMillis() - t0;
            lastUpstreamError = e.getMessage() == null ? e.toString() : e.getMessage();
            throw e;
        } finally {
            if (r != null) r.close();
        }
    }

    /**
     * A full danmu_api deployment has a much stronger POST /api/v2/match:
     * it parses season/episode, resolves the platform and aggregates sources.
     * One request replaces the local search+bangumi guesswork.
     */
    private JSONObject remoteMatch(String fileName) throws Exception {
        String base = fullBase();
        if (base.isEmpty()) return null;
        JSONObject body = new JSONObject();
        body.put("fileName", fileName).put("fileHash", "")
                .put("fileSize", 0).put("videoDuration", 0)
                .put("matchMode", "fileNameOnly");
        Http.Resp r = null;
        long t0 = System.currentTimeMillis();
        try {
            r = Http.postJson(base + "/api/v2/match", body.toString(), TIMEOUT + 5000);
            if (r.code < 200 || r.code >= 300) {
                throw new Exception("匹配接口 HTTP " + r.code
                        + (r.code == 429 ? "（请求过于频繁，请稍后重试）" : ""));
            }
            ByteArrayOutputStream b = new ByteArrayOutputStream();
            Http.copy(r.body, b, MAX_BODY);
            String s = new String(b.toByteArray(), StandardCharsets.UTF_8);
            if (!s.isEmpty() && s.charAt(0) == '\uFEFF') s = s.substring(1);
            JSONObject j = new JSONObject(s);
            lastUpstreamMs = System.currentTimeMillis() - t0;
            lastUpstreamError = "";
            if (!j.optBoolean("isMatched", false)) return null;
            JSONArray ms = j.optJSONArray("matches");
            if (ms == null || ms.length() == 0) return null;
            JSONArray items = new JSONArray();
            for (int i = 0; i < ms.length() && items.length() < 5; i++) {
                JSONObject x = ms.optJSONObject(i);
                if (x == null || !x.has("episodeId")) continue;
                JSONObject o = new JSONObject();
                o.put("episodeId", x.get("episodeId"));
                o.put("animeId", x.opt("animeId"));
                o.put("title", x.optString("animeTitle", ""));
                o.put("episodeTitle", x.optString("episodeTitle", ""));
                o.put("shift", x.optDouble("shift", 0));
                items.put(o);
            }
            JSONObject out = new JSONObject();
            out.put("matched", items.length() > 0).put("items", items);
            return out;
        } catch (Exception e) {
            lastUpstreamMs = System.currentTimeMillis() - t0;
            lastUpstreamError = e.getMessage() == null ? e.toString() : e.getMessage();
            throw e;
        } finally {
            if (r != null) r.close();
        }
    }

    private static String enc(String s) {
        try { return URLEncoder.encode(String.valueOf(s), "UTF-8"); }
        catch (Exception e) { return String.valueOf(s); }
    }

    /** Raw upstream payload for the local /api/v2 compatibility endpoints. */
    public JSONObject raw(String v2tail) throws Exception { return fetch(v2tail); }

    // --------------------------------------------------------------- search

    public JSONArray search(String keyword) throws Exception {
        JSONArray out = new JSONArray();
        if (keyword == null || keyword.trim().isEmpty()) return out;
        JSONObject j = fetch("search/anime?keyword=" + keyword.trim());
        JSONArray a = j.optJSONArray("animes");
        if (a == null) return out;
        for (int i = 0; i < a.length(); i++) {
            JSONObject x = a.optJSONObject(i);
            if (x == null) continue;
            String title = x.optString("animeTitle", "").trim();
            if (title.isEmpty() || !x.has("animeId")) continue;
            JSONObject o = new JSONObject();
            o.put("animeId", x.get("animeId"));
            o.put("title", title);
            o.put("type", x.optString("type", ""));
            o.put("typeDescription", x.optString("typeDescription", ""));
            o.put("imageUrl", x.optString("imageUrl", ""));
            o.put("episodeCount", x.optInt("episodeCount", 0));
            o.put("source", x.optString("source", ""));
            out.put(o);
        }
        return out;
    }

    /** Try cleaned variants of the title, merge and rank by similarity. */
    public JSONArray searchSmart(String keyword) throws Exception {
        List<String> cands = titleCandidates(keyword);
        if (cands.isEmpty() && keyword != null && !keyword.trim().isEmpty()) {
            cands.add(keyword.trim());
        }
        Map<String, JSONObject> merged = new LinkedHashMap<String, JSONObject>();
        Exception err = null;
        double best = 0;
        for (String kw : cands) {
            try {
                JSONArray a = search(kw);
                for (int i = 0; i < a.length(); i++) {
                    JSONObject x = a.optJSONObject(i);
                    if (x == null) continue;
                    String key = String.valueOf(x.opt("animeId"));
                    double s = similarity(kw, x.optString("title", ""));
                    if (s > best) best = s;
                    JSONObject old = merged.get(key);
                    if (old == null || s > old.optDouble("_score", 0)) {
                        x.put("_score", s);
                        merged.put(key, x);
                    }
                }
                if (best >= 0.88) break;       // good enough, avoid rate limits
            } catch (Exception e) { err = e; }
        }
        if (merged.isEmpty() && err != null) throw err;
        List<JSONObject> list = new ArrayList<JSONObject>(merged.values());
        Collections.sort(list, new Comparator<JSONObject>() {
            @Override public int compare(JSONObject a, JSONObject b) {
                return Double.compare(b.optDouble("_score", 0), a.optDouble("_score", 0));
            }
        });
        JSONArray out = new JSONArray();
        for (JSONObject o : list) out.put(o);
        return out;
    }

    // -------------------------------------------------------------- bangumi

    public JSONObject bangumi(String animeId) throws Exception {
        JSONObject out = new JSONObject();
        JSONArray eps = new JSONArray();
        String title = "";
        JSONObject j = fetch("bangumi/" + animeId);
        JSONObject bg = j.optJSONObject("bangumi");
        if (bg != null) {
            title = bg.optString("animeTitle", "");
            JSONArray src = bg.optJSONArray("episodes");
            if (src != null) {
                for (int i = 0; i < src.length(); i++) {
                    JSONObject e = src.optJSONObject(i);
                    if (e == null || !e.has("episodeId")) continue;
                    JSONObject o = new JSONObject();
                    o.put("episodeId", e.get("episodeId"));
                    o.put("title", e.optString("episodeTitle", "").trim());
                    o.put("number", e.optString("episodeNumber", "").trim());
                    o.put("url", e.optString("url", "").trim());
                    eps.put(o);
                }
            }
        }
        out.put("title", title).put("episodes", eps);
        return out;
    }

    // -------------------------------------------------------------- comment

    private static String color(String raw) {
        int n;
        try {
            String s = String.valueOf(raw).trim();
            if (s.startsWith("#")) n = Integer.parseInt(s.substring(1), 16);
            else n = Integer.parseInt(s);
        } catch (Exception e) { n = COLOR_DEFAULT; }
        n = Math.max(0, Math.min(0xFFFFFF, n));
        return String.format(Locale.US, "#%06x", n);
    }

    private static double time(String raw) {
        try {
            double t = Double.parseDouble(String.valueOf(raw).trim());
            return (t >= 0 && t < 86400) ? t : 0.0;
        } catch (Exception e) { return 0.0; }
    }

    /** Normalize dandanplay /v2/comment JSON into the renderer's lean shape. */
    private static JSONArray normalize(JSONObject data) {
        JSONArray src = data.optJSONArray("comments");
        if (src == null) src = data.optJSONArray("data");
        if (src == null) src = data.optJSONArray("danmaku");
        if (src == null) src = new JSONArray();
        List<JSONObject> items = new ArrayList<JSONObject>();
        for (int i = 0; i < src.length(); i++) {
            JSONObject c = src.optJSONObject(i);
            if (c == null) continue;
            String text = c.optString("m", "").trim();
            if (text.isEmpty()) text = c.optString("text", "").trim();
            if (text.isEmpty()) text = c.optString("content", "").trim();
            if (text.isEmpty()) continue;
            String[] p = c.optString("p", "0,1,16777215").split(",");
            int mode = 1;
            try { mode = (int) Double.parseDouble(p[1]); } catch (Exception ignored) {}
            int colorAt = p.length >= 6 ? 3 : 2;
            String cval = p.length > colorAt ? p[colorAt] : String.valueOf(COLOR_DEFAULT);
            String kind = mode == 4 ? "bottom" : (mode == 5 ? "top" : "scroll");
            try {
                JSONObject o = new JSONObject();
                o.put("t", time(p.length > 0 ? p[0] : "0"));
                o.put("mode", kind);
                o.put("color", color(cval));
                o.put("text", text.length() > 200 ? text.substring(0, 200) : text);
                items.add(o);
            } catch (Exception ignored) {}
        }
        Collections.sort(items, new Comparator<JSONObject>() {
            @Override public int compare(JSONObject a, JSONObject b) {
                return Double.compare(a.optDouble("t", 0), b.optDouble("t", 0));
            }
        });
        JSONArray out = new JSONArray();
        for (JSONObject o : items) out.put(o);
        return out;
    }

    public JSONObject comment(String episodeId) throws Exception {
        String key = String.valueOf(episodeId);
        JSONArray items = commentCache.get(key);
        if (items == null) {
            JSONObject j = fetch("comment/" + key
                    + "?from=0&withRelated=true&chConvert=0");
            items = normalize(j);
            if (commentCache.size() > 12) commentCache.clear();
            commentCache.put(key, items);
        }
        JSONObject out = new JSONObject();
        out.put("count", items.length()).put("comments", items).put("videoDuration", 0);
        lastMatchCount = items.length();
        return out;
    }

    // ------------------------------------------------------- title cleaning

    private static void addCandidate(List<String> out, String s) {
        String t = (s == null ? "" : s.trim()).replaceAll("\\s+", " ");
        if (t.length() < 2) return;
        for (String x : out) if (x.equals(t)) return;
        out.add(t);
    }

    /**
     * Apple CMS titles commonly carry "更新至N集", "(2024)", "HD", "【4K】",
     * "第二季", "S01E03" ... Sending any of those verbatim to search/anime
     * returns zero rows even for titles that do exist in the library.
     */
    static List<String> titleCandidates(String fileName) {
        List<String> out = new ArrayList<String>();
        String s = fileName == null ? "" : fileName;
        s = s.replaceAll("(?i)\\s*S\\d{1,2}E\\d{1,3}\\s*", " ")
             .replaceAll("(?i)\\s*(E|EP)\\s*\\d{1,3}\\s*", " ")
             .replaceAll("\\s*第\\s*\\d{1,3}\\s*[集话話回]\\s*", " ")
             .replaceAll("(?i)\\.(m3u8|mp4|mkv|flv|ts|avi|rmvb|webm)$", "")
             .trim();
        String clean = s
             .replaceAll("【[^】]{0,16}】", " ")
             .replaceAll("\\[[^\\]]{0,16}\\]", " ")
             .replaceAll("（[^）]{0,16}）", " ")
             .replaceAll("\\([^)]{0,16}\\)", " ")
             .replaceAll("(?i)\\s*(更新至\\s*\\d+\\s*[集话話]?|共\\s*\\d+\\s*[集话話]|全集|完结|未完结|HD|BD|4K|8K|1080P|720P|2160P|蓝光|国语|粤语|普通话|中字|字幕|无删减|完整版|抢先版|WEB-DL|H265|x264)\\s*", " ")
             .replaceAll("\\s+", " ").trim();
        addCandidate(out, clean);
        String noYear = clean.replaceAll("(?<!\\d)(19|20)\\d{2}(?!\\d)", " ")
                             .replaceAll("\\s+", " ").trim();
        addCandidate(out, noYear);
        String noSeason = noYear
             .replaceAll("(?i)\\s*(第[一二三四五六七八九十\\d]{1,3}[季部篇]|Season\\s*\\d+|S\\d{1,2})\\s*$", "")
             .replaceAll("\\s+", " ").trim();
        addCandidate(out, noSeason);
        addCandidate(out, noSeason.replaceAll("[：:].*$", "")
                                  .replaceAll("\\s+[-—–]\\s+.*$", ""));
        return out;
    }

    private static int episodeHint(String fileName) {
        java.util.regex.Matcher m = java.util.regex.Pattern
                .compile("(?i)(?:S\\d{1,2}E|E|EP|第)\\s*(\\d{1,3})")
                .matcher(fileName == null ? "" : fileName);
        if (m.find()) {
            try { return Math.max(0, Integer.parseInt(m.group(1)) - 1); }
            catch (Exception ignored) {}
        }
        return -1;
    }

    private static String norm(String s) {
        if (s == null) return "";
        return s.replaceAll("[\\s\\p{Punct}，。！？、·：；（）【】\\[\\]「」『』“”‘’—…~～]", "")
                .toLowerCase(Locale.ROOT);
    }

    /** Avoid accepting "弹速狂飙" when the user asked for "狂飙". */
    private static double similarity(String query, String title) {
        String q = norm(query), t = norm(title);
        if (q.isEmpty() || t.isEmpty()) return 0;
        if (t.equals(q)) return 1.0;
        if (t.startsWith(q)) return 0.92 - Math.min(0.20, (t.length() - q.length()) * 0.01);
        if (q.startsWith(t)) return 0.80;
        if (t.contains(q)) return 0.62 - Math.min(0.20, (t.length() - q.length()) * 0.01);
        return 0;
    }

    // ---------------------------------------------------------------- match

    public JSONObject match(String fileName) throws Exception {
        JSONObject out = new JSONObject();
        JSONArray items = new JSONArray();
        JSONArray tried = new JSONArray();
        out.put("matched", false).put("items", items).put("tried", tried);
        String original = fileName == null ? "" : fileName.trim();
        lastMatchTitle = original;
        lastMatchReason = "";
        lastMatchCount = 0;

        // Full service first (bundled core or user deployment):
        // one POST covers cross-platform drama/film.
        boolean usingCore = !DanmuCore.base().isEmpty();
        if (!fullBase().isEmpty()) {
            try {
                JSONObject remote = remoteMatch(original);
                if (remote != null && remote.optBoolean("matched", false)) {
                    out.put("matched", true).put("items", remote.optJSONArray("items"))
                       .put("source", usingCore ? "core-match" : "custom-match");
                    lastMatchReason = usingCore
                            ? "内置弹幕核心自动匹配成功"
                            : "完整服务自动匹配成功";
                    return out;
                }
                out.put("remote_reason", usingCore
                        ? "内置弹幕核心未匹配到条目，已回退本地搜索"
                        : "完整服务未匹配到条目，已回退本地搜索");
            } catch (Exception e) {
                out.put("remote_error", e.getMessage() == null ? "匹配接口请求失败" : e.getMessage());
            }
        }

        List<String> cands = titleCandidates(original);
        if (cands.isEmpty()) {
            out.put("reason", "片名为空");
            lastMatchReason = "片名为空";
            return out;
        }
        JSONObject bestAnime = null;
        double bestScore = 0;
        int total = 0;
        Exception err = null;
        for (String kw : cands) {
            try {
                JSONArray a = search(kw);
                tried.put(kw);
                total += a.length();
                for (int i = 0; i < a.length(); i++) {
                    JSONObject x = a.optJSONObject(i);
                    if (x == null) continue;
                    double s = similarity(kw, x.optString("title", ""));
                    if (s > bestScore) { bestScore = s; bestAnime = x; }
                }
                if (bestScore >= 0.88) break;
            } catch (Exception e) { err = e; }
        }
        // Nothing could be queried at all: surface the network error instead
        // of silently reporting "no matching danmaku".
        if (tried.length() == 0 && err != null) throw err;
        if (bestAnime == null || bestScore < MIN_SCORE) {
            String reason = total == 0
                    ? "上游弹幕库没有该片名（已尝试 " + tried.length() + " 个关键词）"
                    : "搜索结果相似度不足，未采用（避免匹配到别的片子）";
            out.put("reason", reason);
            lastMatchReason = reason;
            return out;
        }
        JSONObject bg = bangumi(String.valueOf(bestAnime.opt("animeId")));
        JSONArray eps = bg.optJSONArray("episodes");
        if (eps == null || eps.length() == 0) {
            out.put("reason", "该条目下没有可选剧集");
            lastMatchReason = "条目下无剧集";
            return out;
        }
        int idx = episodeHint(original);
        if (idx < 0 || idx >= eps.length()) idx = 0;
        JSONObject ep = eps.optJSONObject(idx);
        if (ep == null) {
            out.put("reason", "未找到对应集数");
            lastMatchReason = "未找到对应集数";
            return out;
        }
        JSONObject hit = new JSONObject();
        hit.put("episodeId", ep.get("episodeId"));
        hit.put("animeId", bestAnime.opt("animeId"));
        hit.put("title", bestAnime.optString("title", ""));
        hit.put("episodeTitle", ep.optString("title", ""));
        hit.put("shift", 0);
        items.put(hit);
        out.put("matched", true).put("source", "local-search")
           .put("score", bestScore);
        lastMatchReason = "本地搜索匹配成功";
        return out;
    }

    // ------------------------------------------------------- status / warmup

    /** Probe a user-supplied danmu_api instance with a real /api/v2 call. */
    public JSONObject probe(String api) {
        JSONObject out = new JSONObject();
        String v = api == null ? "" : api.trim();
        try {
            if (!v.startsWith("http://") && !v.startsWith("https://")) {
                throw new Exception("地址需以 http:// 或 https:// 开头");
            }
            String base = v.replaceAll("(?i)/api/v2/?$", "").replaceAll("/+$", "");
            String url = base + "/api/v2/search/anime?keyword=" + enc("test");
            long t0 = System.currentTimeMillis();
            Http.Resp r = null;
            try {
                r = Http.open(url, "", null, 12000, true);
                ByteArrayOutputStream b = new ByteArrayOutputStream();
                Http.copy(r.body, b, 2 * 1024 * 1024);
                String s = new String(b.toByteArray(), StandardCharsets.UTF_8);
                if (!s.isEmpty() && s.charAt(0) == '\uFEFF') s = s.substring(1);
                int n = 0;
                try {
                    JSONArray a = new JSONObject(s).optJSONArray("animes");
                    if (a != null) n = a.length();
                } catch (Exception ignored) {}
                out.put("ok", r.code >= 200 && r.code < 300)
                   .put("status", r.code).put("base", base)
                   .put("items", n).put("ms", System.currentTimeMillis() - t0);
                if (r.code == 429) out.put("error", "请求过于频繁（429），稍后重试");
                else if (r.code >= 400) out.put("error", "服务返回 HTTP " + r.code + "，请检查地址与 TOKEN");
                else if (s.trim().isEmpty() || s.trim().charAt(0) != '{')
                    out.put("error", "返回的不是弹弹play 兼容接口，请检查地址与 TOKEN");
            } finally { if (r != null) r.close(); }
        } catch (Exception e) {
            try {
                out.put("ok", false)
                   .put("error", e.getMessage() == null || e.getMessage().isEmpty()
                           ? "连接失败" : e.getMessage());
            } catch (Exception ignored) {}
        }
        return out;
    }

    /** Silent warm-up, fired in a daemon thread right after the server starts. */
    public void prewarm() {
        try {
            fetch("search/anime?keyword=test");
            readyState = "ok";
            String core = DanmuCore.base();
            if (!core.isEmpty()) {
                readyText = "内置弹幕核心已就绪（多平台聚合）· " + lastUpstreamMs + " ms";
            } else if (customBase().isEmpty()) {
                readyText = "内置镜像已就绪（仅番剧/动画库）· " + lastUpstreamMs + " ms";
            } else {
                readyText = "完整弹幕服务已连接 · " + lastUpstreamMs + " ms";
            }
        } catch (Exception e) {
            readyState = "error";
            readyText = "弹幕服务暂不可达：" + (e.getMessage() == null ? "网络异常" : e.getMessage());
        }
        readyAt = System.currentTimeMillis();
    }

    public JSONObject status() {
        JSONObject c = store.config();
        String custom = customBase();
        String core = DanmuCore.base();
        JSONObject out = new JSONObject();
        try {
            out.put("configured", true);
            out.put("enabled", c.optBoolean("danmu_enabled", true));
            out.put("auto", c.optBoolean("danmu_auto", true));
            if (!core.isEmpty()) {
                out.put("mode", "core");
                out.put("base", "内置弹幕核心（127.0.0.1:" + DanmuCore.PORT + "）");
                out.put("coverage", "core");
                out.put("coverage_note", "内置弹幕核心已在本机运行，支持豆瓣/腾讯/优酷/爱奇艺/"
                        + "咪咕/B站等平台，国产剧与电影均可匹配");
            } else if (custom.isEmpty()) {
                out.put("mode", "builtin");
                out.put("base", "内置镜像（仅弹弹play 番剧库）");
                out.put("coverage", "builtin");
                out.put("coverage_note", "内置镜像只索引番剧/动画，国产剧与电影通常匹配不到；"
                        + "内置弹幕核心正在启动，请稍后重试，或在设置里填写完整 danmu_api 服务地址");
            } else {
                out.put("mode", "custom");
                out.put("base", custom);
                out.put("coverage", "custom");
                out.put("coverage_note", "已连接完整弹幕服务（多平台聚合，支持国产剧/电影）");
            }
            out.put("core", DanmuCore.status());
            out.put("ready", readyState);
            out.put("ready_text", readyText);
            out.put("checked_at", readyAt);
            out.put("upstream_ms", lastUpstreamMs);
            out.put("upstream_error", lastUpstreamError);
            out.put("last_match_title", lastMatchTitle);
            out.put("last_match_reason", lastMatchReason);
            out.put("last_match_count", lastMatchCount);
            out.put("ua", UA_HINT);
        } catch (Exception ignored) {}
        return out;
    }
}
