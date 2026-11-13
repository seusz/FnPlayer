package com.fnplayer.mobile;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.net.URLEncoder;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Apple CMS V10 JSON client: fan-out search, title merge, play-list parsing and
 * share-page / 302 resolution. Ported from the desktop kernel (MIT, fnmedia-ext).
 */
public final class Cms {
    private static final Pattern MEDIA = Pattern.compile(
            "\\.(m3u8|mp4|flv|mkv|ts)(\\?|$)", Pattern.CASE_INSENSITIVE);
    private static final Pattern SHARE = Pattern.compile("/(share|play|player)/[A-Za-z0-9]+");
    private static final Pattern DIAG = Pattern.compile("[?&]ac=videodiag\\b", Pattern.CASE_INSENSITIVE);
    private static final Pattern AD = Pattern.compile(
            "关注|公众号|加微信|加群|下载APP|扫码|QQ群|QQ\\s*群", Pattern.CASE_INSENSITIVE);
    private static final Pattern EMBED_URL = Pattern.compile(
            "(?:const|let|var)\\s+url\\s*=\\s*[\"']([^\"']+)[\"']", Pattern.CASE_INSENSITIVE);
    private static final Pattern JSON_URL = Pattern.compile(
            "[\"']url[\"']\\s*:\\s*[\"']([^\"']+)[\"']", Pattern.CASE_INSENSITIVE);
    private static final Pattern RAW_M3U8 = Pattern.compile(
            "(https?://[^\"'\\s<>]+?\\.m3u8[^\"'\\s<>]*)", Pattern.CASE_INSENSITIVE);

    private static final long RESOLVE_TTL = 30L * 60L * 1000L;
    private static final int MAX_TITLES = 120;

    private final Store store;
    private final ExecutorService pool = Executors.newFixedThreadPool(8);
    private final LinkedHashMap<String, JSONObject> titles =
            new LinkedHashMap<String, JSONObject>();
    private final Map<String, long[]> unused = new HashMap<String, long[]>();
    private final Map<String, String> resolveCache = new HashMap<String, String>();
    private final Map<String, Long> resolveTime = new HashMap<String, Long>();

    public Cms(Store store) { this.store = store; }

    // ------------------------------------------------------------------ search

    private List<JSONObject> fetch(final JSONObject source, final String keyword,
                                  final int page, final int timeoutMs) {
        try {
            String api = source.optString("api", "");
            if (!api.startsWith("http://") && !api.startsWith("https://")) return null;
            String sep = api.contains("?") ? "&" : "?";
            String url;
            if (keyword != null && !keyword.isEmpty()) {
                url = api + sep + "ac=detail&wd=" + URLEncoder.encode(keyword, "UTF-8");
            } else {
                url = api + sep + "ac=detail&pg=" + page;
            }
            String body = Http.text(url, "", timeoutMs);
            JSONObject j = new JSONObject(body);
            Object code = j.opt("code");
            if (!"1".equals(String.valueOf(code)) && j.optInt("code", 0) != 1) return null;
            JSONArray list = j.optJSONArray("list");
            if (list == null) return null;
            List<JSONObject> out = new ArrayList<JSONObject>();
            for (int i = 0; i < list.length(); i++) {
                JSONObject v = list.optJSONObject(i);
                if (v == null) continue;
                JSONObject t = mapVod(v, source);
                if (t != null) out.add(t);
            }
            return out;
        } catch (Exception e) {
            return null;
        }
    }

    private JSONObject mapVod(JSONObject v, JSONObject source) {
        try {
            String sid = source.optString("id", "u0");
            String sname = source.optString("name", sid);
            String api = source.optString("api", "");
            String referer = Http.siteRoot(api);
            String vid = String.valueOf(v.opt("vod_id"));
            JSONObject t = new JSONObject();
            t.put("id", sid + ":" + vid);
            t.put("title", v.optString("vod_name", "").trim());
            t.put("type", v.optString("type_name", ""));
            t.put("year", v.optString("vod_year", ""));
            t.put("area", v.optString("vod_area", ""));
            t.put("remarks", v.optString("vod_remarks", ""));
            t.put("score", v.optString("vod_score", ""));
            t.put("poster", v.optString("vod_pic", ""));
            String desc = v.optString("vod_content", "");
            if (desc.isEmpty()) desc = v.optString("vod_blurb", "");
            t.put("overview", desc.trim());
            t.put("actor", v.optString("vod_actor", ""));
            t.put("director", v.optString("vod_director", ""));
            JSONArray providers = new JSONArray();
            providers.put(sname);
            t.put("providers", providers);
            t.put("lines", parsePlay(v.optString("vod_play_from", ""),
                    v.optString("vod_play_url", ""), referer));
            return t.getString("title").isEmpty() ? null : t;
        } catch (Exception e) {
            return null;
        }
    }

    private JSONObject parsePlay(String froms, String groups, String referer) {
        JSONObject lines = new JSONObject();
        String[] names = froms.split("\\$\\$\\$", -1);
        String[] blocks = groups.split("\\$\\$\\$", -1);
        for (int g = 0; g < blocks.length; g++) {
            String lineName = (g < names.length && !names[g].isEmpty())
                    ? names[g] : ("线路" + (g + 1));
            JSONArray eps = new JSONArray();
            String[] items = blocks[g].split("#");
            for (String item : items) {
                item = item.trim();
                int p = item.indexOf('$');
                if (p <= 0) continue;
                String epName = item.substring(0, p).trim();
                String url = item.substring(p + 1).trim();
                if (epName.isEmpty()) epName = "第" + (eps.length() + 1) + "集";
                if (AD.matcher(epName).find()) continue;
                if (!url.startsWith("http://") && !url.startsWith("https://")) continue;
                boolean direct = MEDIA.matcher(url).find();
                boolean resolve = !direct && SHARE.matcher(url).find();
                boolean redirect = DIAG.matcher(url).find();
                try {
                    JSONObject ep = new JSONObject();
                    ep.put("name", epName);
                    ep.put("url", url);
                    ep.put("direct", direct || redirect);
                    ep.put("resolve", resolve);
                    ep.put("redirect", redirect);
                    ep.put("referer", referer);
                    eps.put(ep);
                } catch (Exception ignored) {}
            }
            if (eps.length() > 0) {
                String key = lineName;
                int dup = 2;
                while (lines.has(key)) key = lineName + "(" + (dup++) + ")";
                try { lines.put(key, eps); } catch (Exception ignored) {}
            }
        }
        return lines;
    }

    private List<JSONObject> fanout(final String keyword, final int page, int timeoutMs) {
        final List<JSONObject> sources = store.enabledSources();
        if (sources.isEmpty()) return new ArrayList<JSONObject>();
        List<Future<List<JSONObject>>> futures = new ArrayList<Future<List<JSONObject>>>();
        for (final JSONObject s : sources) {
            futures.add(pool.submit(new Callable<List<JSONObject>>() {
                @Override public List<JSONObject> call() {
                    return fetch(s, keyword, page, timeoutMs);
                }
            }));
        }
        List<List<JSONObject>> chunks = new ArrayList<List<JSONObject>>();
        long deadline = System.currentTimeMillis() + timeoutMs + 600L;
        for (Future<List<JSONObject>> f : futures) {
            long left = Math.max(50L, deadline - System.currentTimeMillis());
            try {
                List<JSONObject> r = f.get(left, TimeUnit.MILLISECONDS);
                if (r != null) chunks.add(r);
            } catch (Exception ignored) {}
        }
        for (Future<List<JSONObject>> f : futures) if (!f.isDone()) f.cancel(true);
        List<JSONObject> all = new ArrayList<JSONObject>();
        for (List<JSONObject> c : chunks) all.addAll(c);
        return all;
    }

    private List<JSONObject> merge(List<JSONObject> raw) {
        LinkedHashMap<String, JSONObject> byKey = new LinkedHashMap<String, JSONObject>();
        for (JSONObject t : raw) {
            String key = t.optString("title", "").replaceAll("[\\s:：·_]", "")
                    + "|" + t.optString("year", "");
            JSONObject cur = byKey.get(key);
            if (cur == null) {
                byKey.put(key, t);
                continue;
            }
            try {
                JSONObject srcLines = t.optJSONObject("lines");
                JSONObject dstLines = cur.optJSONObject("lines");
                if (srcLines != null && dstLines != null) {
                    Iterator<String> it = srcLines.keys();
                    while (it.hasNext()) {
                        String k = it.next();
                        String nk = k;
                        int i = 2;
                        while (dstLines.has(nk)) nk = k + "(" + (i++) + ")";
                        dstLines.put(nk, srcLines.get(k));
                    }
                }
                JSONArray p = cur.optJSONArray("providers");
                if (p == null) { p = new JSONArray(); cur.put("providers", p); }
                JSONArray add = t.optJSONArray("providers");
                if (add != null) {
                    for (int j = 0; j < add.length(); j++) {
                        String n = add.optString(j, "");
                        boolean seen = false;
                        for (int q = 0; q < p.length(); q++) if (n.equals(p.optString(q, ""))) seen = true;
                        if (!seen && !n.isEmpty()) p.put(n);
                    }
                }
                if (cur.optString("poster", "").isEmpty()) cur.put("poster", t.optString("poster", ""));
                if (cur.optString("overview", "").isEmpty()) cur.put("overview", t.optString("overview", ""));
            } catch (Exception ignored) {}
        }
        return new ArrayList<JSONObject>(byKey.values());
    }

    private synchronized void put(JSONObject t) {
        titles.remove(t.optString("id", ""));
        titles.put(t.optString("id", ""), t);
        while (titles.size() > MAX_TITLES) {
            String oldest = titles.keySet().iterator().next();
            titles.remove(oldest);
        }
    }

    /**
     * Memory-first lookup with a disk fallback. Process restarts wipe the
     * in-memory map, but the title snapshots survive in titles.json, so
     * "continue watching" keeps working without a new search or network call.
     */
    public synchronized JSONObject get(String id) {
        JSONObject t = titles.get(id);
        if (t == null && id != null && !id.isEmpty()) {
            JSONObject disk = store.title(id);
            if (disk != null) {
                titles.put(id, disk);
                t = disk;
            }
        }
        return t;
    }

    /**
     * Re-attach a freshly searched title under the id stored in history, so a
     * recovered record keeps pointing at a valid tid.
     */
    public synchronized JSONObject rebind(String oldId, JSONObject found) {
        try {
            JSONObject copy = new JSONObject(found.toString());
            copy.put("id", oldId);
            put(copy);
            store.saveTitle(copy);
            return copy;
        } catch (Exception e) {
            return null;
        }
    }

    private JSONArray briefs(List<JSONObject> list, int limit) {
        JSONArray out = new JSONArray();
        List<JSONObject> persisted = new ArrayList<JSONObject>();
        for (int i = 0; i < list.size() && i < limit; i++) {
            JSONObject t = list.get(i);
            put(t);
            persisted.add(t);
            try {
                JSONObject b = new JSONObject();
                b.put("id", t.optString("id"));
                b.put("title", t.optString("title"));
                b.put("type", t.optString("type"));
                b.put("year", t.optString("year"));
                b.put("area", t.optString("area"));
                b.put("remarks", t.optString("remarks"));
                b.put("score", t.optString("score"));
                b.put("poster", t.optString("poster"));
                b.put("providers", t.optJSONArray("providers"));
                int count = 0;
                JSONObject lines = t.optJSONObject("lines");
                if (lines != null) {
                    Iterator<String> it = lines.keys();
                    while (it.hasNext()) {
                        JSONArray eps = lines.optJSONArray(it.next());
                        if (eps != null) count = Math.max(count, eps.length());
                    }
                }
                b.put("episodes", count);
                out.put(b);
            } catch (Exception ignored) {}
        }
        store.saveTitles(persisted);
        return out;
    }

    public JSONArray search(String keyword, int timeoutMs) {
        if (keyword == null || keyword.trim().isEmpty()) return new JSONArray();
        List<JSONObject> merged = merge(fanout(keyword.trim(), 1, timeoutMs));
        return briefs(merged, 40);
    }

    public JSONArray discover(int page, int timeoutMs) {
        List<JSONObject> merged = merge(fanout(null, Math.max(1, page), timeoutMs));
        return briefs(merged, 40);
    }

    /** Single-source probe for the settings page. */
    public JSONObject probe(String api) {
        JSONObject out = new JSONObject();
        long t0 = System.currentTimeMillis();
        try {
            JSONObject fake = new JSONObject();
            fake.put("id", "probe");
            fake.put("name", "probe");
            fake.put("api", api);
            List<JSONObject> r = fetch(fake, "庆余年", 1, 8000);
            out.put("ok", r != null && !r.isEmpty());
            out.put("count", r == null ? 0 : r.size());
            out.put("sample", (r == null || r.isEmpty()) ? "" : r.get(0).optString("title", ""));
            out.put("error", (r == null || r.isEmpty()) ? "接口无结果或不可达" : "");
        } catch (Exception e) {
            try { out.put("ok", false); out.put("error", e.toString()); } catch (Exception ignored) {}
        }
        try { out.put("elapsed_ms", System.currentTimeMillis() - t0); } catch (Exception ignored) {}
        return out;
    }

    // --------------------------------------------------------------- resolution

    public String resolveEpisode(JSONObject ep) {
        String url = ep.optString("url", "");
        String ref = ep.optString("referer", "");
        if (ref.isEmpty()) ref = Http.siteRoot(url);
        if (ep.optBoolean("resolve", false)) return resolveShare(url, ref);
        if (ep.optBoolean("redirect", false)) return resolveRedirect(url, ref);
        if (ep.optBoolean("direct", false)) return url;
        return null;
    }

    private synchronized String cached(String key) {
        Long t = resolveTime.get(key);
        if (t != null && System.currentTimeMillis() - t < RESOLVE_TTL) return resolveCache.get(key);
        return null;
    }

    private synchronized void remember(String key, String value) {
        resolveCache.put(key, value);
        resolveTime.put(key, System.currentTimeMillis());
        if (resolveCache.size() > 300) {
            resolveCache.clear();
            resolveTime.clear();
        }
    }

    private String resolveRedirect(String url, String ref) {
        String hit = cached(url);
        if (hit != null) return hit;
        for (int attempt = 0; attempt < 2; attempt++) {
            Http.Resp r = null;
            try {
                r = Http.open(url, ref, null, 15000, false);
                String loc = r.location;
                if (loc != null && !loc.isEmpty()) {
                    String abs = Http.abs(url, loc);
                    if (MEDIA.matcher(abs).find()) {
                        remember(url, abs);
                        return abs;
                    }
                }
            } catch (Exception ignored) {
            } finally {
                if (r != null) r.close();
            }
        }
        return null;
    }

    private String resolveShare(String url, String ref) {
        String hit = cached(url);
        if (hit != null) return hit;
        Http.Resp r = null;
        try {
            r = Http.open(url, ref, null, 12000, true);
            if (r.code != 200 || r.body == null) return null;
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            Http.copy(r.body, buf, 2 * 1024 * 1024);
            String html = new String(buf.toByteArray(), java.nio.charset.StandardCharsets.UTF_8);
            html = html.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/");
            String base = r.url == null ? url : r.url.toString();
            Pattern[] pats = new Pattern[] { EMBED_URL, JSON_URL, RAW_M3U8 };
            for (Pattern p : pats) {
                Matcher m = p.matcher(html);
                while (m.find()) {
                    String cand = m.group(1).trim().replace("\\/", "/");
                    if (cand.isEmpty() || cand.startsWith("//") && !cand.contains(".")) continue;
                    if (MEDIA.matcher(cand).find()) {
                        String abs = Http.abs(base, cand);
                        remember(url, abs);
                        return abs;
                    }
                }
            }
        } catch (Exception ignored) {
        } finally {
            if (r != null) r.close();
        }
        return null;
    }
}
