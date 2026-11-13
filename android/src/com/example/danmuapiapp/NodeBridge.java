package com.example.danmuapiapp;

import android.content.Context;
import android.content.pm.ApplicationInfo;

import java.io.File;

/**
 * Node.js Mobile 运行时的 JNI 桥接。
 *
 * 说明：本类的全限定名必须与随包分发的原生库 libnative-lib.so 导出的符号一致
 * （Java_com_example_danmuapiapp_NodeBridge_*），否则 JNI 找不到实现。
 * 原生库内部封装了 node::Start / setenv / chdir，只依赖 Node 运行时，
 * 不读取 Android 资源，因此不需要 AssetManager。
 */
public final class NodeBridge {

    public static final NodeBridge INSTANCE = new NodeBridge();

    private volatile boolean loaded;

    private NodeBridge() {
        this.loaded = loadLibraries(null);
    }

    public boolean isLoaded() {
        return loaded;
    }

    /** 应用启动后调用一次，拿到 nativeLibraryDir 以便绝对路径加载兜底。 */
    public boolean ensureLoaded(Context ctx) {
        if (loaded) return true;
        loaded = loadLibraries(ctx);
        return loaded;
    }

    private static boolean loadLibraries(Context ctx) {
        // 常规路径：系统按 ABI 从应用原生库目录加载
        try {
            System.loadLibrary("native-lib");
            System.loadLibrary("node");
            return true;
        } catch (Throwable first) {
            try {
                // 部分 ROM 对加载顺序敏感
                System.loadLibrary("node");
                System.loadLibrary("native-lib");
                return true;
            } catch (Throwable second) {
                return loadFromDir(nativeLibDir(ctx));
            }
        }
    }

    private static String nativeLibDir(Context ctx) {
        if (ctx == null) return null;
        try {
            Context app = ctx.getApplicationContext();
            ApplicationInfo ai = app.getPackageManager()
                    .getApplicationInfo(app.getPackageName(), 0);
            return ai.nativeLibraryDir;
        } catch (Throwable t) {
            return null;
        }
    }

    /** 兜底：从原生库目录按绝对路径加载（个别定制 ROM 会拦 loadLibrary）。 */
    private static boolean loadFromDir(String dir) {
        if (dir == null) return false;
        File cxx = new File(dir, "libc++_shared.so");
        if (cxx.exists()) {
            try { System.load(cxx.getAbsolutePath()); } catch (Throwable ignored) { }
        }
        File node = new File(dir, "libnode.so");
        File jni = new File(dir, "libnative-lib.so");
        if (!node.exists() || !jni.exists()) return false;
        try {
            System.load(node.getAbsolutePath());
            System.load(jni.getAbsolutePath());
            return true;
        } catch (Throwable t) {
            return false;
        }
    }

    /**
     * 启动 Node 运行时。
     * 参数同 node 的命令行：argv[0] 为程序名，后面跟脚本路径。
     * 该调用会阻塞直到运行时退出，必须放在独立线程里。
     *
     * @return 退出码（正常退出为 0）
     */
    public native int startNodeWithArguments(String[] args);

    /** 设置进程环境变量，必须在启动运行时之前调用。 */
    public native boolean setEnvironmentVariable(String name, String value, boolean overwrite);

    /** 切换工作目录。 */
    public native boolean changeWorkingDirectory(String path);

    private native String getNodeVersion();

    /** 运行时版本号（如 24.x.0），取不到返回 null。 */
    public String nodeVersion() {
        try {
            return getNodeVersion();
        } catch (Throwable t) {
            return null;
        }
    }
}
