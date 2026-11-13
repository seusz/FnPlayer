package com.fnplayer.mobile;

import android.content.Context;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** App-private config.json / history.json storage. Nothing is baked into the APK. */
public final class Store {
    private static final Pattern URL_RE = Pattern.compile(
            "https?://[^\\s,;，；\"'<>]+", Pattern.CASE_INSENSITIVE);

    private final Context ctx;
    private JSONObject config;
    private JSONArray history;

    public Store(Context c) { ctx = c; }

    private File file(String name) { return new File(ctx.getFilesDir(), name); }

    private String read(File f) {
        try (FileInputStream in = new FileInputStream(f)) {
            java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
            Http.copy(in, out, 4 * 1024 * 1024);
            String s = new String(out.toByteArray(), StandardCharsets.UTF_8);
            return s.length() > 0 && s.charAt(0) == '\uFEFF' ? s.substring(1) : s;
        } catch (Exception e) {
            return "";
        }
    }

    /** Atomic-ish write: a killed process can at worst leave a stale .tmp file. */
    private void write(File f, String s) throws Exception {
        File tmp = new File(f.getParentFile(), f.getName() + ".tmp");
        try (FileOutputStream out = new FileOutputStream(tmp)) {
            out.write(s.getBytes(StandardCharsets.UTF_8));
            out.flush();
            try { out.getFD().sync(); } catch (Exception ignored) {}
        }
        if (!tmp.renameTo(f)) {
            // Some devices refuse rename over an existing file.
            if (f.exists()) f.delete();
            if (!tmp.renameTo(f)) throw new Exception("cannot write " + f.getName());
        }
    }

    private String asset(String name) {
        try (java.io.InputStream in = ctx.getAssets().open(name)) {
            java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
            Http.copy(in, out, 1024 * 1024);
            return new String(out.toByteArray(), StandardCharsets.UTF_8);
        } catch (Exception e) {
            return "";
        }
    }

    public synchronized JSONObject config() {
        if (config == null) {
            String s = read(file("config.json"));
            if (s.isEmpty()) s = asset("config.json");
            if (s.isEmpty()) s = "{}";
            try { config = new JSONObject(s); }
            catch (Exception e) { config = new JSONObject(); }
            try {
                if (config.optJSONArray("sources") == null) config.put("sources", new JSONArray());
                if (!config.has("auto_resume")) config.put("auto_resume", true);
                if (!config.has("search_timeout")) config.put("search_timeout", 9);
                // 弹幕默认开启；服务随应用启动静默就绪，无需任何开关。
                // danmu_api 留空 = 使用内置的 danmu_api 同款上游镜像。
                if (!config.has("danmu_enabled")) config.put("danmu_enabled", true);
                if (!config.has("danmu_auto")) config.put("danmu_auto", true);
                if (!config.has("danmu_api")) config.put("danmu_api", "");
                if (!config.has("danmu_opacity")) config.put("danmu_opacity", 0.85);
                if (!config.has("danmu_font_size")) config.put("danmu_font_size", 20);
                if (!config.has("danmu_area")) config.put("danmu_area", 0.75);
                if (!config.has("danmu_speed")) config.put("danmu_speed", 8.0);
                if (!config.has("danmu_offset")) config.put("danmu_offset", 0.0);
                if (!config.has("danmu_bold")) config.put("danmu_bold", false);
            } catch (Exception ignored) {}
        }
        return config;
    }

    public synchronized JSONObject configCopy() {
        try { return new JSONObject(config().toString()); }
        catch (Exception e) { return new JSONObject(); }
    }

    public synchronized void saveConfig(JSONObject patch) throws Exception {
        JSONObject c = config();
        Iterator<String> it = patch.keys();
        while (it.hasNext()) {
            String k = it.next();
            if (!k.startsWith("_")) c.put(k, patch.get(k));
        }
        write(file("config.json"), c.toString(2));
    }

    public synchronized List<JSONObject> sources() {
        List<JSONObject> out = new ArrayList<JSONObject>();
        JSONArray a = config().optJSONArray("sources");
        if (a == null) return out;
        for (int i = 0; i < a.length(); i++) {
            JSONObject o = a.optJSONObject(i);
            if (o != null) out.add(o);
        }
        return out;
    }

    public synchronized List<JSONObject> enabledSources() {
        List<JSONObject> out = new ArrayList<JSONObject>();
        for (JSONObject s : sources()) {
            if (s.optBoolean("enabled", true)) out.add(s);
        }
        return out;
    }

    public synchronized void saveSources(JSONArray arr) throws Exception {
        config().put("sources", arr);
        write(file("config.json"), config().toString(2));
    }

    public synchronized JSONArray history() {
        if (history == null) {
            String s = read(file("history.json"));
            try { history = new JSONArray(s); }
            catch (Exception e) { history = new JSONArray(); }
        }
        return history;
    }

    public synchronized void saveHistory(JSONArray a) throws Exception {
        JSONArray trimmed = new JSONArray();
        for (int i = 0; i < a.length() && i < 100; i++) trimmed.put(a.opt(i));
        history = trimmed;
        write(file("history.json"), trimmed.toString(2));
    }

    public synchronized void clearHistory() throws Exception {
        saveHistory(new JSONArray());
    }

    /** Title of a history entry, used to rebuild an expired in-memory cache. */
    public synchronized String historyTitle(String id) {
        JSONArray a = history();
        for (int i = 0; i < a.length(); i++) {
            JSONObject h = a.optJSONObject(i);
            if (h != null && id.equals(h.optString("id", ""))) {
                return h.optString("title", "").trim();
            }
        }
        return "";
    }

    // ----------------------------------------------------------- favorites
    // "我的收藏" is a small persisted list of brief cards. The full title
    // snapshot is put into titles.json at the same time, so a favorite stays
    // openable after the process restarts (same recovery path as history).

    private static final int MAX_FAVORITES = 300;
    private JSONArray favorites;

    public synchronized JSONArray favorites() {
        if (favorites == null) {
            String s = read(file("favorites.json"));
            try { favorites = new JSONArray(s); }
            catch (Exception e) { favorites = new JSONArray(); }
        }
        return favorites;
    }

    private synchronized void persistFavorites(JSONArray a) throws Exception {
        JSONArray trimmed = new JSONArray();
        for (int i = 0; i < a.length() && i < MAX_FAVORITES; i++) trimmed.put(a.opt(i));
        favorites = trimmed;
        write(file("favorites.json"), trimmed.toString(2));
    }

    /** Add a brief, or move an existing one back to the top. */
    public synchronized JSONArray addFavorite(JSONObject item) throws Exception {
        String id = item == null ? "" : item.optString("id", "");
        if (id.isEmpty()) return favorites();
        JSONObject clean = new JSONObject();
        String[] keys = {"id", "title", "poster", "year", "type", "area",
                "remarks", "score", "providers", "episodes"};
        for (String k : keys) if (item.has(k)) clean.put(k, item.get(k));
        clean.put("at", System.currentTimeMillis());
        JSONArray next = new JSONArray();
        next.put(clean);
        JSONArray old = favorites();
        for (int i = 0; i < old.length(); i++) {
            JSONObject o = old.optJSONObject(i);
            if (o != null && !id.equals(o.optString("id", ""))) next.put(o);
        }
        persistFavorites(next);
        return favorites();
    }

    public synchronized JSONArray removeFavorite(String id) throws Exception {
        if (id == null || id.isEmpty()) return favorites();
        JSONArray next = new JSONArray();
        JSONArray old = favorites();
        for (int i = 0; i < old.length(); i++) {
            JSONObject o = old.optJSONObject(i);
            if (o != null && !id.equals(o.optString("id", ""))) next.put(o);
        }
        persistFavorites(next);
        return favorites();
    }

    public synchronized JSONArray clearFavorites() throws Exception {
        persistFavorites(new JSONArray());
        return favorites();
    }

    /** Title of a favorite, used when its snapshot was evicted from the cache. */
    public synchronized String favoriteTitle(String id) {
        JSONArray a = favorites();
        for (int i = 0; i < a.length(); i++) {
            JSONObject f = a.optJSONObject(i);
            if (f != null && id.equals(f.optString("id", ""))) {
                return f.optString("title", "").trim();
            }
        }
        return "";
    }

    // ------------------------------------------------------- title snapshots
    // Search results live in Cms' in-memory LinkedHashMap, which is wiped on
    // process restart. Persist full title objects (including play lines) so
    // "continue watching" still resolves after the app is closed and reopened.

    private static final int MAX_TITLES = 150;
    private JSONObject titleDb;

    private synchronized JSONObject titleDb() {
        if (titleDb == null) {
            String s = read(file("titles.json"));
            try {
                titleDb = new JSONObject(s);
                if (titleDb.optJSONObject("items") == null) titleDb.put("items", new JSONObject());
                if (titleDb.optJSONArray("order") == null) titleDb.put("order", new JSONArray());
            } catch (Exception e) {
                titleDb = new JSONObject();
                try { titleDb.put("items", new JSONObject()).put("order", new JSONArray()); }
                catch (Exception ignored) {}
            }
        }
        return titleDb;
    }

    /** Full title object by id, or null when never cached on disk. */
    public synchronized JSONObject title(String id) {
        if (id == null || id.isEmpty()) return null;
        return titleDb().optJSONObject("items").optJSONObject(id);
    }

    /**
     * Merge title snapshots into the disk cache (LRU, most recent last) and
     * write once. Called in batches from Cms.briefs(), never per item.
     */
    public synchronized void saveTitles(List<JSONObject> list) {
        if (list == null || list.isEmpty()) return;
        JSONObject db = titleDb();
        JSONObject items = db.optJSONObject("items");
        JSONArray order = db.optJSONArray("order");
        LinkedHashMap<String, JSONObject> snap = new LinkedHashMap<String, JSONObject>();
        for (int i = 0; i < order.length(); i++) {
            String k = order.optString(i, "");
            JSONObject v = k.isEmpty() ? null : items.optJSONObject(k);
            if (v != null) snap.put(k, v);
        }
        for (JSONObject t : list) {
            String id = t.optString("id", "");
            if (id.isEmpty()) continue;
            snap.remove(id);
            snap.put(id, t);
        }
        while (snap.size() > MAX_TITLES) snap.remove(snap.keySet().iterator().next());
        JSONObject newItems = new JSONObject();
        JSONArray newOrder = new JSONArray();
        for (Map.Entry<String, JSONObject> e : snap.entrySet()) {
            try { newItems.put(e.getKey(), e.getValue()); } catch (Exception ignored) {}
            newOrder.put(e.getKey());
        }
        try {
            db.put("items", newItems).put("order", newOrder);
            write(file("titles.json"), db.toString(2));
        } catch (Exception ignored) {}
    }

    /** Convenience wrapper for a single title (rebind / autosearch recovery). */
    public synchronized void saveTitle(JSONObject t) {
        if (t != null) saveTitles(Collections.singletonList(t));
    }

    /**
     * Bulk import: accepts a JSON array, or plain text with one source per line.
     * Supported line formats: "name,url" / "name url" / bare url.
     */
    public List<JSONObject> parseBulk(String text) throws Exception {
        List<JSONObject> out = new ArrayList<JSONObject>();
        if (text == null) return out;
        String t = text.trim();
        if (t.isEmpty()) return out;
        if (t.startsWith("[")) {
            JSONArray a = new JSONArray(t);
            for (int i = 0; i < a.length(); i++) {
                Object o = a.opt(i);
                String api = "";
                String name = "";
                if (o instanceof JSONObject) {
                    JSONObject j = (JSONObject) o;
                    api = j.optString("api", j.optString("url", ""));
                    name = j.optString("name", "");
                } else if (o instanceof String) {
                    api = (String) o;
                }
                JSONObject s = source(name, api);
                if (s != null) out.add(s);
            }
            return out;
        }
        for (String line : t.split("[\\r\\n]+")) {
            Matcher m = URL_RE.matcher(line);
            if (!m.find()) continue;
            String api = m.group().replaceAll("[\\)\\]\\},，;；]+$", "");
            String prefix = line.substring(0, m.start()).trim();
            prefix = prefix.replaceAll("^[,，:：\\-]+|[,，:：\\-]+$", "").trim();
            JSONObject s = source(prefix, api);
            if (s != null) out.add(s);
        }
        return out;
    }

    private JSONObject source(String name, String api) {
        api = (api == null ? "" : api.trim());
        if (!api.startsWith("http://") && !api.startsWith("https://")) return null;
        if (name == null || name.isEmpty()) {
            try { name = new java.net.URL(api).getHost(); }
            catch (Exception e) { name = "自定义源"; }
        }
        try {
            JSONObject s = new JSONObject();
            s.put("id", "u" + Integer.toHexString((name + api).hashCode()));
            s.put("name", name);
            s.put("api", api);
            s.put("enabled", true);
            return s;
        } catch (Exception e) {
            return null;
        }
    }
}
