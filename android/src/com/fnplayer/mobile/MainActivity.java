package com.fnplayer.mobile;

import android.app.Activity;
import android.content.Intent;
import android.content.pm.ActivityInfo;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.Toast;

/** App shell: a private loopback server plus a WebView that only talks to itself. */
public class MainActivity extends Activity {
    private static LocalServer server;
    private WebView web;
    private View customView;
    private WebChromeClient.CustomViewCallback customCallback;

    @Override protected void onCreate(Bundle b) {
        super.onCreate(b);
        try { getWindow().setStatusBarColor(Color.parseColor("#0d1016")); }
        catch (Exception ignored) {}

        if (server == null) {
            try {
                server = new LocalServer(getApplicationContext());
                server.start();
            } catch (Exception e) {
                Toast.makeText(this, "本地服务启动失败：" + e.getMessage(), Toast.LENGTH_LONG).show();
                finish();
                return;
            }
        }

        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0d1016"));
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setAllowFileAccessFromFileURLs(false);
        s.setAllowUniversalAccessFromFileURLs(false);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        if (Build.VERSION.SDK_INT >= 21) s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        CookieManager.getInstance().setAcceptCookie(true);

        web.addJavascriptInterface(new Bridge(), "FnNative");
        web.setWebViewClient(new WebViewClient() {
            private boolean isLocal(String url) {
                return url != null && url.startsWith("http://127.0.0.1:" + server.port() + "/");
            }
            private boolean openOutside(String url) {
                try { startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url))); }
                catch (Exception ignored) {}
                return true;
            }
            @Override public boolean shouldOverrideUrlLoading(WebView v, String url) {
                return isLocal(url) ? false : openOutside(url);
            }
            @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest req) {
                String url = req.getUrl().toString();
                return isLocal(url) ? false : openOutside(url);
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public void onShowCustomView(View view, CustomViewCallback cb) {
                showCustom(view, cb);
            }
            @Override public void onHideCustomView() {
                hideCustom();
            }
        });

        web.loadUrl("http://127.0.0.1:" + server.port() + "/?t=" + server.token());
    }

    private void showCustom(View view, WebChromeClient.CustomViewCallback cb) {
        if (customView != null) { try { cb.onCustomViewHidden(); } catch (Exception ignored) {} return; }
        customView = view;
        customCallback = cb;
        setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE);
        ((FrameLayout) getWindow().getDecorView()).addView(view, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        web.setVisibility(View.GONE);
        immersive(true);
    }

    private void hideCustom() {
        if (customView == null) return;
        ViewGroup parent = (ViewGroup) customView.getParent();
        if (parent != null) parent.removeView(customView);
        customView = null;
        web.setVisibility(View.VISIBLE);
        // Leaving fullscreen must always restore portrait, even when the
        // orientation was landscape before the custom view was shown.
        setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_PORTRAIT);
        immersive(false);
        if (customCallback != null) {
            try { customCallback.onCustomViewHidden(); } catch (Exception ignored) {}
            customCallback = null;
        }
    }

    private void immersive(boolean on) {
        if (Build.VERSION.SDK_INT < 19) return;
        View d = getWindow().getDecorView();
        if (!on) { d.setSystemUiVisibility(View.SYSTEM_UI_FLAG_VISIBLE); return; }
        d.setSystemUiVisibility(View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_FULLSCREEN
                | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);
    }

    @Override public void onBackPressed() {
        if (customView != null) { hideCustom(); return; }
        if (web == null) { super.onBackPressed(); return; }
        web.evaluateJavascript(
                "(function(){try{return window.__fnBack?String(window.__fnBack()):'false'}"
                + "catch(e){return 'false'}})()",
                new ValueCallback<String>() {
                    @Override public void onReceiveValue(String value) {
                        boolean consumed = value != null && value.contains("true");
                        if (!consumed) MainActivity.this.finish();
                    }
                });
    }

    @Override protected void onPause() {
        super.onPause();
        if (web != null) {
            web.onPause();
            web.evaluateJavascript("(function(){try{window.__fnPause&&window.__fnPause()}catch(e){}})()", null);
        }
    }

    @Override protected void onResume() {
        super.onResume();
        if (web != null) web.onResume();
    }

    @Override protected void onDestroy() {
        if (web != null) { web.destroy(); web = null; }
        super.onDestroy();
    }

    private class Bridge {
        @JavascriptInterface public void setKeepScreenOn(final boolean on) {
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    if (on) getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                    else getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                }
            });
        }

        @JavascriptInterface public void requestLandscape(final boolean on) {
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    setRequestedOrientation(on
                            ? ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
                            : ActivityInfo.SCREEN_ORIENTATION_PORTRAIT);
                }
            });
        }

        @JavascriptInterface public void openExternal(final String url) {
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    try { startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url))); }
                    catch (Exception ignored) {}
                }
            });
        }

        @JavascriptInterface public void toast(final String msg) {
            runOnUiThread(new Runnable() {
                @Override public void run() {
                    Toast.makeText(MainActivity.this, msg, Toast.LENGTH_SHORT).show();
                }
            });
        }

        @JavascriptInterface public String version() { return "1.2.0"; }
    }
}
