package com.fnplayer.mobile;

import java.io.ByteArrayOutputStream;
import java.io.Closeable;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * Minimal HTTP client on top of the Android framework. No third-party library.
 * Used by the CMS client and by the local HLS proxy.
 */
public final class Http {
    public static final String UA =
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            + "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

    private Http() {}

    public static final class Resp implements Closeable {
        public int code;
        public URL url;
        public String location = "";
        public Map<String, String> headers = new HashMap<String, String>();
        public InputStream body;
        private HttpURLConnection conn;

        public String header(String key) {
            return headers.get(key.toLowerCase(Locale.US));
        }

        @Override public void close() {
            if (body != null) {
                try { body.close(); } catch (Exception ignored) {}
            }
            if (conn != null) conn.disconnect();
        }
    }

    public static Resp open(String url, String referer, String range,
                            int timeoutMs, boolean follow) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(timeoutMs);
        c.setReadTimeout(timeoutMs);
        c.setRequestMethod("GET");
        c.setRequestProperty("User-Agent", UA);
        c.setRequestProperty("Accept", "*/*");
        c.setRequestProperty("Accept-Encoding", "identity");
        if (referer != null && referer.length() > 0) c.setRequestProperty("Referer", referer);
        if (range != null && range.length() > 0) c.setRequestProperty("Range", range);
        c.setInstanceFollowRedirects(follow);
        return finish(c, new Resp());
    }

    private static Resp finish(HttpURLConnection c, Resp r) throws IOException {
        r.conn = c;
        r.code = c.getResponseCode();
        r.url = c.getURL();
        for (Map.Entry<String, List<String>> e : c.getHeaderFields().entrySet()) {
            if (e.getKey() == null || e.getValue() == null || e.getValue().isEmpty()) continue;
            String k = e.getKey().toLowerCase(Locale.US);
            r.headers.put(k, e.getValue().get(0));
            if (k.equals("location")) r.location = e.getValue().get(0);
        }
        r.body = (r.code >= 400) ? c.getErrorStream() : c.getInputStream();
        return r;
    }

    /** POST a JSON body (full danmu_api deployments expose /api/v2/match). */
    public static Resp postJson(String url, String json, int timeoutMs) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(timeoutMs);
        c.setReadTimeout(timeoutMs);
        c.setRequestMethod("POST");
        c.setDoOutput(true);
        c.setRequestProperty("User-Agent", UA);
        c.setRequestProperty("Accept", "application/json");
        c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
        c.setRequestProperty("Accept-Encoding", "identity");
        byte[] b = json.getBytes(StandardCharsets.UTF_8);
        c.setFixedLengthStreamingMode(b.length);
        OutputStream os = c.getOutputStream();
        try { os.write(b); } finally { os.close(); }
        c.setInstanceFollowRedirects(true);
        return finish(c, new Resp());
    }

    public static byte[] bytes(String url, String referer, int timeoutMs) throws IOException {
        try (Resp r = open(url, referer, null, timeoutMs, true)) {
            if (r.code != 200) throw new IOException("HTTP " + r.code);
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            copy(r.body, out, 4 * 1024 * 1024);
            return out.toByteArray();
        }
    }

    public static String text(String url, String referer, int timeoutMs) throws IOException {
        byte[] raw = bytes(url, referer, timeoutMs);
        String s = new String(raw, StandardCharsets.UTF_8);
        if (s.length() > 0 && s.charAt(0) == '\uFEFF') s = s.substring(1);
        // Some CMS sites still answer in GBK. Fall back when UTF-8 decoding produced
        // a suspicious amount of replacement characters.
        int bad = 0;
        for (int i = 0; i < s.length(); i++) if (s.charAt(i) == '\uFFFD') bad++;
        if (bad > 4) {
            try { return new String(raw, "GBK"); } catch (Exception ignored) {}
        }
        return s;
    }

    public static long copy(InputStream in, OutputStream out, long limit) throws IOException {
        if (in == null) return 0;
        byte[] buf = new byte[64 * 1024];
        long total = 0;
        int n;
        while ((n = in.read(buf)) != -1) {
            out.write(buf, 0, n);
            total += n;
            if (limit > 0 && total > limit) throw new IOException("response too large");
        }
        return total;
    }

    /** java.net.URL based relative resolution, equivalent to Python urljoin for our use. */
    public static String abs(String base, String rel) {
        if (rel == null) return base;
        try {
            return new URL(new URL(base), rel.trim()).toString();
        } catch (Exception e) {
            return rel;
        }
    }

    public static String query(String api, String key, String value) {
        try {
            return api + (api.contains("?") ? "&" : "?") + key + "="
                    + URLEncoder.encode(value, "UTF-8");
        } catch (Exception e) {
            return api;
        }
    }

    public static String queryInt(String api, String key, int value) {
        return api + (api.contains("?") ? "&" : "?") + key + "=" + value;
    }

    public static String siteRoot(String api) {
        try {
            URL u = new URL(api);
            return u.getProtocol() + "://" + u.getAuthority() + "/";
        } catch (Exception e) {
            return "";
        }
    }
}
