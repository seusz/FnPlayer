package com.fnplayer.mobile;

import android.content.Context;
import android.content.res.AssetManager;

import com.example.danmuapiapp.NodeBridge;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

/**
 * 内置弹幕核心（Node.js + danmu_api）的安装与生命周期管理。
 *
 * 做法与 danmu-api-android 相同：把 Node 运行时（nodejs-mobile）与 danmu_api
 * 核心一起塞进 APK，首次启动时解到应用私有目录，然后在进程内启动 Node，
 * 由它在本机回环地址上提供 /api/v2 兼容接口。
 *
 * 与上游的差异：
 *  - 只监听 127.0.0.1，不做任何局域网暴露；
 *  - 由播放器内部直接调用，不做账号/设备管理；
 *  - 启动失败时自动回退到内置镜像（见 Danmu.endpoint）。
 *
 * 注意：node::Start 在一个进程里只能成功执行一次，失败后不可重启，
 * 因此所有启动前的准备工作（解包、环境变量）都放在调用之前完成。
 */
public final class DanmuCore {

    public static final String VERSION = "1.2.0";
    public static final String HOST = "127.0.0.1";
    public static final int PORT = 9322;
    public static final String BASE = "http://" + HOST + ":" + PORT;

    private static final String ASSET_DIR = "danmu-core";
    private static final String MARKER = ".installed";

    private static volatile String state = "idle";   // idle/installing/starting/ready/failed
    private static volatile String message = "";
    private static volatile long startedAt = 0;
    private static volatile boolean launched = false;
    private static volatile String nodeVersion = "";
    private static volatile String sources = "";
    private static final Object LOCK = new Object();

    private DanmuCore() { }

    /** 核心地址；未就绪时返回空串（调用方回退到内置镜像）。 */
    public static String base() {
        return isReady() ? BASE : "";
    }

    public static boolean isReady() {
        return "ready".equals(state);
    }

    public static String state() {
        return state;
    }

    public static String message() {
        return message;
    }

    /**
     * 启动内置弹幕核心（异步、幂等）。
     * 在应用启动后调用即可，失败不影响播放器其它功能。
     */
    public static void start(final Context ctx) {
        synchronized (LOCK) {
            if (launched) return;
            launched = true;
        }
        final Context app = ctx.getApplicationContext();
        Thread t = new Thread(new Runnable() {
            @Override public void run() {
                try {
                    prepare(app);
                    launch(app);
                } catch (Throwable e) {
                    state = "failed";
                    message = describe(e);
                }
            }
        }, "fnplayer-danmu-core");
        t.setDaemon(true);
        t.start();
    }

    // ------------------------------------------------------------ 准备阶段

    /** 把随包分发的核心解到应用私有目录（已安装过则跳过）。 */
    private static void prepare(Context ctx) throws Exception {
        state = "installing";
        message = "正在准备弹幕核心…";
        File target = new File(ctx.getFilesDir(), ASSET_DIR);
        String stamp = assetStamp(ctx);
        File marker = new File(target, MARKER);
        if (target.isDirectory() && marker.isFile()) {
            try {
                String have = read(marker).trim();
                if (have.equals(stamp)) {
                    message = "弹幕核心已安装";
                    return;
                }
            } catch (Exception ignored) { }
        }
        copyTree(ctx.getAssets(), ASSET_DIR, target);
        write(new File(target, MARKER), stamp);
        message = "弹幕核心已安装";
    }

    /** 用随包 manifest 的摘要作为版本戳，核心更新后会重新解包。 */
    private static String assetStamp(Context ctx) {
        try {
            byte[] b = readAll(ctx.getAssets().open(ASSET_DIR + "/manifest.json"));
            return String.valueOf(b.length) + ":" + hash(b);
        } catch (Exception e) {
            return "0";
        }
    }

    private static void copyTree(AssetManager am, String path, File target) throws Exception {
        String[] names = am.list(path);
        if (names == null || names.length == 0) {
            File out = new File(target, path.substring(path.lastIndexOf('/') + 1));
            copyFile(am.open(path), out);
            return;
        }
        for (String n : names) {
            String child = path + "/" + n;
            File out = new File(target, n);
            String[] sub = am.list(child);
            if (sub != null && sub.length > 0) {
                if (!out.isDirectory() && !out.mkdirs()) throw new Exception("无法创建目录 " + out);
                copyTree(am, child, out);
            } else {
                File parent = out.getParentFile();
                if (parent != null && !parent.isDirectory() && !parent.mkdirs()) {
                    throw new Exception("无法创建目录 " + parent);
                }
                copyFile(am.open(child), out);
            }
        }
    }

    private static void copyFile(InputStream in, File out) throws Exception {
        OutputStream os = new FileOutputStream(out);
        try {
            byte[] buf = new byte[64 * 1024];
            int n;
            while ((n = in.read(buf)) > 0) os.write(buf, 0, n);
            os.flush();
        } finally {
            try { in.close(); } catch (Exception ignored) { }
            try { os.close(); } catch (Exception ignored) { }
        }
    }

    // ------------------------------------------------------------ 启动阶段

    private static void launch(Context ctx) throws Exception {
        if (!NodeBridge.INSTANCE.ensureLoaded(ctx)) {
            state = "failed";
            message = "无法加载 Node 运行时（本机 ABI 不匹配或被系统拦截）";
            return;
        }
        nodeVersion = String.valueOf(NodeBridge.INSTANCE.nodeVersion());

        File coreDir = new File(ctx.getFilesDir(), ASSET_DIR);
        File entry = new File(coreDir, "server-entry.cjs");
        if (!entry.isFile()) {
            state = "failed";
            message = "弹幕核心入口缺失";
            return;
        }

        File tmp = new File(ctx.getCacheDir(), "tmp");
        if (!tmp.isDirectory() && !tmp.mkdirs()) throw new Exception("无法创建临时目录");
        File compileCache = new File(ctx.getCacheDir(), "node-compile-cache");
        if (!compileCache.isDirectory() && !compileCache.mkdirs()) compileCache = tmp;

        env("TMPDIR", tmp.getAbsolutePath());
        env("HOME", ctx.getFilesDir().getAbsolutePath());
        env("NODE_COMPILE_CACHE", compileCache.getAbsolutePath());
        env("DANMU_API_HOST", HOST);
        env("DANMU_API_PORT", String.valueOf(PORT));
        env("TOKEN", "87654321");
        env("LOG_LEVEL", "warn");
        env("SOURCE_ORDER", defaultSources());
        env("RATE_LIMIT_MAX_REQUESTS", "0");
        env("SEARCH_CACHE_MINUTES", "3");
        env("COMMENT_CACHE_MINUTES", "3");
        env("LOCAL_CACHE_ENABLED", "false");

        try {
            NodeBridge.INSTANCE.changeWorkingDirectory(coreDir.getAbsolutePath());
        } catch (Throwable ignored) { }

        state = "starting";
        message = "正在启动弹幕核心…";

        Thread t = new Thread(new Runnable() {
            @Override public void run() {
                int code = -1;
                try {
                    code = NodeBridge.INSTANCE.startNodeWithArguments(
                            new String[]{"node", entry.getAbsolutePath()});
                } catch (Throwable e) {
                    state = "failed";
                    message = "弹幕核心启动失败：" + describe(e);
                    return;
                }
                if (!"ready".equals(state)) {
                    state = "failed";
                    message = "弹幕核心已退出（code=" + code + "）";
                }
            }
        }, "fnplayer-node-runtime");
        t.setDaemon(true);
        t.start();

        // 等待服务就绪
        long deadline = System.currentTimeMillis() + 40_000L;
        while (System.currentTimeMillis() < deadline) {
            if ("failed".equals(state)) return;
            if (health()) {
                state = "ready";
                message = "内置弹幕核心已就绪";
                startedAt = System.currentTimeMillis();
                return;
            }
            try { Thread.sleep(600L); } catch (InterruptedException e) { return; }
        }
        state = "failed";
        message = "弹幕核心启动超时（已回退到内置镜像）";
    }

    private static void env(String k, String v) {
        try {
            NodeBridge.INSTANCE.setEnvironmentVariable(k, v, true);
        } catch (Throwable ignored) { }
    }

    private static String defaultSources() {
        return "douban,360,renren,hanjutv,tencent,youku,iqiyi,migu,bilibili,dandan";
    }

    /** 探测本机核心是否已可服务。 */
    private static boolean health() {
        Http.Resp r = null;
        try {
            r = Http.open(BASE + "/__fn_health", "", null, 4000, true);
            if (r.code < 200 || r.code >= 300) return false;
            ByteArrayOutputStream b = new ByteArrayOutputStream();
            Http.copy(r.body, b, 256 * 1024);
            String s = new String(b.toByteArray(), StandardCharsets.UTF_8);
            JSONObject j = new JSONObject(s);
            if (j.optBoolean("ok", false)) {
                String src = j.optString("sources", "");
                if (!src.isEmpty()) sources = src;
                return true;
            }
            return false;
        } catch (Exception e) {
            return false;
        } finally {
            if (r != null) r.close();
        }
    }

    /** 供设置页展示的详细状态。 */
    public static JSONObject status() {
        JSONObject o = new JSONObject();
        try {
            o.put("app_version", VERSION);
            o.put("state", state);
            o.put("message", message);
            o.put("ready", isReady());
            o.put("base", BASE);
            o.put("node_version", nodeVersion);
            o.put("sources", sources);
            o.put("started_at", startedAt);
        } catch (Exception ignored) { }
        return o;
    }

    // -------------------------------------------------------------- 小工具

    private static String describe(Throwable e) {
        if (e == null) return "未知错误";
        String m = e.getMessage();
        return (m == null || m.isEmpty()) ? e.getClass().getSimpleName() : m;
    }

    private static byte[] readAll(InputStream in) throws Exception {
        try {
            ByteArrayOutputStream b = new ByteArrayOutputStream();
            byte[] buf = new byte[16 * 1024];
            int n;
            while ((n = in.read(buf)) > 0) b.write(buf, 0, n);
            return b.toByteArray();
        } finally {
            try { in.close(); } catch (Exception ignored) { }
        }
    }

    private static String read(File f) throws Exception {
        byte[] b = readAll(new java.io.FileInputStream(f));
        return new String(b, StandardCharsets.UTF_8);
    }

    private static void write(File f, String text) throws Exception {
        OutputStream os = new FileOutputStream(f);
        try {
            os.write(text.getBytes(StandardCharsets.UTF_8));
            os.flush();
        } finally {
            try { os.close(); } catch (Exception ignored) { }
        }
    }

    private static String hash(byte[] b) {
        try {
            java.security.MessageDigest md = java.security.MessageDigest.getInstance("SHA-256");
            byte[] d = md.digest(b);
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < 16 && i < d.length; i++) {
                sb.append(String.format("%02x", d[i]));
            }
            return sb.toString();
        } catch (Exception e) {
            return String.valueOf(b.length);
        }
    }
}
