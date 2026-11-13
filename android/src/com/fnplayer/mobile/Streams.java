package com.fnplayer.mobile;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** In-memory HLS stream contexts and m3u8 URI rewriting (same rules as app/hls.py). */
public final class Streams {
    private static final Pattern URI_ATTR = Pattern.compile("URI=\"([^\"]+)\"", Pattern.CASE_INSENSITIVE);
    private static final int MAX = 24;

    public static final class Ctx {
        public final String token;
        public final String url;
        public final String referer;
        public final String title;
        public final String line;
        public final String episode;
        public final boolean hls;
        public final List<String> seg = new ArrayList<String>();
        public final List<String> sub = new ArrayList<String>();
        public final List<String> key = new ArrayList<String>();
        public final Map<String, byte[]> keyCache = new LinkedHashMap<String, byte[]>();
        public String base = "";

        private Ctx(String url, String referer, String title, String line,
                    String episode, boolean hls) {
            this.token = UUID.randomUUID().toString().replace("-", "").substring(0, 16);
            this.url = url;
            this.referer = referer;
            this.title = title;
            this.line = line;
            this.episode = episode;
            this.hls = hls;
        }

        public synchronized int addSeg(String u) { seg.add(u); return seg.size() - 1; }
        public synchronized int addSub(String u) { sub.add(u); return sub.size() - 1; }
        public synchronized int addKey(String u) { key.add(u); return key.size() - 1; }
        public synchronized String segAt(int i) { return i >= 0 && i < seg.size() ? seg.get(i) : null; }
        public synchronized String subAt(int i) { return i >= 0 && i < sub.size() ? sub.get(i) : null; }
        public synchronized String keyAt(int i) { return i >= 0 && i < key.size() ? key.get(i) : null; }
    }

    private static final LinkedHashMap<String, Ctx> ALL = new LinkedHashMap<String, Ctx>();

    public static synchronized Ctx create(String url, String referer, String title,
                                          String line, String episode, boolean hls) {
        Ctx c = new Ctx(url, referer, title, line, episode, hls);
        c.base = url;
        ALL.put(c.token, c);
        while (ALL.size() > MAX) {
            String oldest = ALL.keySet().iterator().next();
            ALL.remove(oldest);
        }
        return c;
    }

    public static synchronized Ctx get(String token) { return ALL.get(token); }

    /**
     * Rewrite every URI in a playlist to a same-origin proxy URL.
     * Keys, fMP4 init segments, variant playlists and media segments are all covered.
     */
    public static String rewrite(String text, String base, Ctx ctx, String tk) {
        String prefix = "/api/stream/" + ctx.token;
        String suffix = "&t=" + tk;
        StringBuilder out = new StringBuilder();
        String lastTag = "";
        for (String raw : text.split("\n")) {
            String line = raw.trim();
            if (line.isEmpty()) { out.append('\n'); continue; }
            if (line.charAt(0) == '#') {
                int colon = line.indexOf(':');
                lastTag = (colon > 0 ? line.substring(0, colon) : line).toUpperCase(Locale.US);
                if (line.contains("URI=")) {
                    Matcher m = URI_ATTR.matcher(line);
                    StringBuffer sb = new StringBuffer();
                    boolean found = false;
                    while (m.find()) {
                        found = true;
                        String abs = Http.abs(base, m.group(1));
                        String target;
                        if ("#EXT-X-KEY".equals(lastTag)) {
                            target = prefix + "/key?i=" + ctx.addKey(abs) + suffix;
                        } else if ("#EXT-X-I-FRAME-STREAM-INF".equals(lastTag)
                                || "#EXT-X-MEDIA".equals(lastTag)) {
                            target = prefix + "/sub?i=" + ctx.addSub(abs) + suffix;
                        } else {
                            target = prefix + "/seg?i=" + ctx.addSeg(abs) + suffix;
                        }
                        m.appendReplacement(sb, Matcher.quoteReplacement("URI=\"" + target + "\""));
                    }
                    m.appendTail(sb);
                    out.append(found ? sb.toString() : line).append('\n');
                } else {
                    out.append(line).append('\n');
                }
                continue;
            }
            String abs = Http.abs(base, line);
            if ("#EXT-X-STREAM-INF".equals(lastTag)
                    || "#EXT-X-I-FRAME-STREAM-INF".equals(lastTag)) {
                out.append(prefix).append("/sub?i=").append(ctx.addSub(abs))
                        .append(suffix).append('\n');
            } else {
                out.append(prefix).append("/seg?i=").append(ctx.addSeg(abs))
                        .append(suffix).append('\n');
            }
            lastTag = "";
        }
        return out.toString();
    }
}
