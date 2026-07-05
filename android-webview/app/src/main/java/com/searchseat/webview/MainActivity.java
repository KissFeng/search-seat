package com.searchseat.webview;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.media.AudioAttributes;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.Message;
import android.os.VibrationEffect;
import android.os.VibrationAttributes;
import android.os.Vibrator;
import android.provider.Settings;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URISyntaxException;
import java.net.URL;
import java.net.URLEncoder;
import java.security.MessageDigest;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;

public class MainActivity extends Activity {
    private static final String HOME_URL = "http://103.203.140.44:18000/";
    private static final String INTERNAL_HOST = "103.203.140.44";
    private static final String UPDATE_CHECK_URL = HOME_URL + "api/app-update/android";
    private static final String CHAOXING_PACKAGE = "com.chaoxing.mobile";
    private static final String NOTIFICATION_CHANNEL_ID = GetuiPushBridge.NOTIFICATION_CHANNEL_ID;
    private static final int NOTIFICATION_PERMISSION_REQUEST = 42;
    private static final long ALERT_POLL_INTERVAL_MS = 5000L;
    private static final long[] NOTIFICATION_VIBRATION_PATTERN = new long[]{0, 260, 130, 260};
    private static final int[] NOTIFICATION_VIBRATION_AMPLITUDES = new int[]{0, 255, 0, 255};

    private WebView webView;
    private ProgressBar progressBar;
    private View errorView;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final Object chaoxingCookieLock = new Object();
    private final Set<String> preparedChaoxingCookieHosts = new HashSet<>();
    private boolean alertPollRunning = false;
    private volatile boolean alertPollInFlight = false;
    private volatile boolean updateCheckInFlight = false;
    private boolean optionalUpdateDismissed = false;
    private JSONObject pendingInstallUpdate = null;
    private File pendingInstallApk = null;
    private boolean chaoxingCookieSyncInFlight = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        configureWindow();
        setContentView(createContentView());
        configureWebView();
        GetuiPushBridge.initialize(this);
        GetuiPushBridge.ensureNotificationChannels(this);
        requestNotificationPermission();
        startNativeAlertPolling();
        checkForUpdatesInBackground(false);

        if (savedInstanceState == null) {
            loadUrlFromIntent(getIntent());
        } else {
            webView.restoreState(savedInstanceState);
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        loadUrlFromIntent(intent);
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        webView.saveState(outState);
        flushCookies();
    }

    @Override
    protected void onPause() {
        flushCookies();
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        GetuiPushBridge.initialize(this);
        GetuiPushBridge.syncClientIdInBackground(this);
        pollAlertsOnceInBackground();
        if (pendingInstallUpdate != null && pendingInstallApk != null) {
            installDownloadedApk(pendingInstallApk, pendingInstallUpdate);
        } else {
            checkForUpdatesInBackground(false);
        }
    }

    @Override
    protected void onStop() {
        flushCookies();
        super.onStop();
    }

    @Override
    protected void onDestroy() {
        flushCookies();
        alertPollRunning = false;
        if (webView != null) {
            webView.destroy();
        }
        super.onDestroy();
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
            return;
        }
        super.onBackPressed();
    }

    private void configureWindow() {
        Window window = getWindow();
        window.setStatusBarColor(Color.WHITE);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            window.getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR);
        }
    }

    private View createContentView() {
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.WHITE);
        root.setPadding(0, getStatusBarHeight(), 0, 0);

        webView = new WebView(this);
        root.addView(webView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
        ));

        progressBar = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progressBar.setMax(100);
        progressBar.setVisibility(View.GONE);
        root.addView(progressBar, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(3),
                Gravity.TOP
        ));

        errorView = createErrorView();
        errorView.setVisibility(View.GONE);
        root.addView(errorView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
        ));

        return root;
    }

    private View createErrorView() {
        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setGravity(Gravity.CENTER);
        panel.setPadding(dp(28), dp(28), dp(28), dp(28));
        panel.setBackgroundColor(Color.WHITE);

        TextView title = new TextView(this);
        title.setText("Page unavailable");
        title.setTextColor(Color.rgb(25, 28, 33));
        title.setTextSize(20);
        title.setGravity(Gravity.CENTER);
        panel.addView(title, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        ));

        TextView detail = new TextView(this);
        detail.setText("Check the network or server, then try again.");
        detail.setTextColor(Color.rgb(92, 99, 112));
        detail.setTextSize(14);
        detail.setGravity(Gravity.CENTER);
        LinearLayout.LayoutParams detailParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        );
        detailParams.setMargins(0, dp(10), 0, dp(22));
        panel.addView(detail, detailParams);

        Button retry = new Button(this);
        retry.setText("Retry");
        retry.setAllCaps(false);
        retry.setOnClickListener(v -> {
            errorView.setVisibility(View.GONE);
            webView.reload();
        });
        panel.addView(retry, new LinearLayout.LayoutParams(dp(140), dp(48)));

        return panel;
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void configureWebView() {
        CookieManager cookieManager = CookieManager.getInstance();
        cookieManager.setAcceptCookie(true);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setSupportMultipleWindows(true);
        settings.setJavaScriptCanOpenWindowsAutomatically(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            cookieManager.setAcceptThirdPartyCookies(webView, true);
            settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        }

        WebView.setWebContentsDebuggingEnabled(true);
        webView.addJavascriptInterface(new AndroidBridge(), "SearchSeatAndroid");

        webView.setWebViewClient(new SearchSeatWebViewClient());
        webView.setWebChromeClient(new SearchSeatChromeClient());
    }

    private void loadUrlFromIntent(Intent intent) {
        String targetUrl = intent == null ? null : intent.getStringExtra("target_url");
        webView.loadUrl(normalizeTargetUrl(targetUrl));
    }

    private String normalizeTargetUrl(String targetUrl) {
        if (targetUrl == null || targetUrl.trim().isEmpty()) {
            return HOME_URL;
        }
        String value = targetUrl.trim();
        Uri uri = Uri.parse(value);
        if (uri.getScheme() != null) {
            return value;
        }
        if (value.startsWith("/")) {
            return HOME_URL.replaceAll("/+$", "") + value;
        }
        return HOME_URL + value;
    }

    private void requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, NOTIFICATION_PERMISSION_REQUEST);
        }
    }

    private void startNativeAlertPolling() {
        if (alertPollRunning) {
            return;
        }
        alertPollRunning = true;
        mainHandler.post(this::pollAlertsInBackground);
    }

    private void pollAlertsInBackground() {
        pollAlertsInBackground(true);
    }

    private void pollAlertsOnceInBackground() {
        pollAlertsInBackground(false);
    }

    private void pollAlertsInBackground(boolean scheduleNext) {
        if (!alertPollRunning) {
            return;
        }
        if (alertPollInFlight) {
            if (scheduleNext) {
                mainHandler.postDelayed(this::pollAlertsInBackground, ALERT_POLL_INTERVAL_MS);
            }
            return;
        }
        alertPollInFlight = true;
        new Thread(() -> {
            try {
                String cookie = CookieManager.getInstance().getCookie(HOME_URL);
                if (cookie == null || cookie.trim().isEmpty()) {
                    return;
                }
                URL url = new URL(HOME_URL + "api/watch-alerts");
                HttpURLConnection connection = (HttpURLConnection) url.openConnection();
                connection.setRequestMethod("GET");
                connection.setRequestProperty("Cookie", cookie);
                connection.setConnectTimeout(8000);
                connection.setReadTimeout(8000);
                int status = connection.getResponseCode();
                if (status < 200 || status >= 300) {
                    return;
                }
                JSONArray alerts = new JSONObject(readResponseBody(connection)).optJSONArray("alerts");
                if (alerts == null) {
                    return;
                }
                for (int i = 0; i < alerts.length(); i++) {
                    JSONObject alert = alerts.optJSONObject(i);
                    if (alert == null) {
                        continue;
                    }
                    String alertId = String.valueOf(alert.optLong("id"));
                    String title = (alert.optString("room_name", alert.optString("room_id", "座位")) + " 有可预约座位").trim();
                    String message = buildAlertBody(alert);
                    String targetUrl = alert.optString("reserve_url", "");
                    mainHandler.post(() -> showSeatNotification(alertId, title, message, targetUrl));
                }
            } catch (Exception ignored) {
            } finally {
                alertPollInFlight = false;
                if (scheduleNext) {
                    mainHandler.postDelayed(this::pollAlertsInBackground, ALERT_POLL_INTERVAL_MS);
                }
            }
        }, "seat-alert-poll").start();
    }

    private int currentVersionCode() {
        try {
            PackageInfo info = getPackageManager().getPackageInfo(getPackageName(), 0);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                return (int) info.getLongVersionCode();
            }
            return info.versionCode;
        } catch (Exception ignored) {
            return 1;
        }
    }

    private String currentVersionName() {
        try {
            PackageInfo info = getPackageManager().getPackageInfo(getPackageName(), 0);
            return info.versionName == null ? "" : info.versionName;
        } catch (Exception ignored) {
            return "";
        }
    }

    private void checkForUpdatesInBackground(boolean forcePrompt) {
        if (updateCheckInFlight || (!forcePrompt && optionalUpdateDismissed)) {
            return;
        }
        updateCheckInFlight = true;
        new Thread(() -> {
            try {
                String urlText = UPDATE_CHECK_URL
                        + "?version_code=" + currentVersionCode()
                        + "&version_name=" + URLEncoder.encode(currentVersionName(), "UTF-8");
                HttpURLConnection connection = (HttpURLConnection) new URL(urlText).openConnection();
                connection.setRequestMethod("GET");
                connection.setRequestProperty("Accept", "application/json");
                connection.setConnectTimeout(8000);
                connection.setReadTimeout(8000);
                int status = connection.getResponseCode();
                if (status < 200 || status >= 300) {
                    return;
                }
                JSONObject payload = new JSONObject(readResponseBody(connection));
                if (payload.optBoolean("update", false)) {
                    mainHandler.post(() -> showUpdateDialog(payload));
                }
            } catch (Exception ignored) {
            } finally {
                updateCheckInFlight = false;
            }
        }, "app-update-check").start();
    }

    private void showUpdateDialog(JSONObject update) {
        boolean forceUpdate = update.optBoolean("force_update", false);
        String versionName = update.optString("version_name", "");
        String notes = update.optString("release_notes", "");
        String message = notes == null || notes.trim().isEmpty()
                ? "发现新版本 " + versionName
                : "发现新版本 " + versionName + "\n\n" + notes;
        AlertDialog.Builder builder = new AlertDialog.Builder(this)
                .setTitle(forceUpdate ? "需要更新" : "发现新版本")
                .setMessage(message)
                .setPositiveButton("立即更新", (dialog, which) -> downloadUpdateInBackground(update));
        if (forceUpdate) {
            builder.setCancelable(false);
            builder.setNegativeButton("退出", (dialog, which) -> finish());
        } else {
            builder.setNegativeButton("稍后", (dialog, which) -> optionalUpdateDismissed = true);
        }
        builder.show();
    }

    private void downloadUpdateInBackground(JSONObject update) {
        Toast.makeText(this, "正在下载更新...", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            File apkFile = updateApkFile(update);
            deleteOldUpdateApks(apkFile);
            try {
                HttpURLConnection connection = (HttpURLConnection) new URL(update.optString("apk_url")).openConnection();
                connection.setConnectTimeout(10000);
                connection.setReadTimeout(30000);
                int status = connection.getResponseCode();
                if (status < 200 || status >= 300) {
                    throw new RuntimeException("download failed");
                }
                InputStream input = connection.getInputStream();
                FileOutputStream output = new FileOutputStream(apkFile);
                byte[] buffer = new byte[8192];
                int read;
                while ((read = input.read(buffer)) != -1) {
                    output.write(buffer, 0, read);
                }
                output.close();
                input.close();
                String expectedHash = update.optString("apk_sha256", "");
                String actualHash = sha256File(apkFile);
                if (!expectedHash.equalsIgnoreCase(actualHash)) {
                    apkFile.delete();
                    throw new RuntimeException("hash mismatch");
                }
                mainHandler.post(() -> installDownloadedApk(apkFile, update));
            } catch (Exception e) {
                mainHandler.post(() -> {
                    Toast.makeText(this, "更新下载失败，请稍后重试", Toast.LENGTH_LONG).show();
                    if (update.optBoolean("force_update", false)) {
                        showUpdateDialog(update);
                    }
                });
            }
        }, "app-update-download").start();
    }

    private File updateApkFile(JSONObject update) {
        int versionCode = Math.max(1, update.optInt("version_code", 1));
        return new File(getCacheDir(), "search-seat-update-" + update.optInt("version_code", versionCode) + ".apk");
    }

    private void deleteOldUpdateApks(File keepFile) {
        File[] files = getCacheDir().listFiles();
        if (files == null) {
            return;
        }
        for (File file : files) {
            String name = file.getName();
            if (file.equals(keepFile)) {
                continue;
            }
            if ("search-seat-update.apk".equals(name) || (name.startsWith("search-seat-update-") && name.endsWith(".apk"))) {
                file.delete();
            }
        }
    }

    private String sha256File(File file) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        InputStream input = new FileInputStream(file);
        byte[] buffer = new byte[8192];
        int read;
        while ((read = input.read(buffer)) != -1) {
            digest.update(buffer, 0, read);
        }
        input.close();
        byte[] bytes = digest.digest();
        StringBuilder hex = new StringBuilder();
        for (byte b : bytes) {
            hex.append(String.format(Locale.ROOT, "%02x", b));
        }
        return hex.toString();
    }

    private void installDownloadedApk(File apkFile, JSONObject update) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !getPackageManager().canRequestPackageInstalls()) {
            pendingInstallApk = apkFile;
            pendingInstallUpdate = update;
            Intent intent = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES);
            intent.setData(Uri.parse("package:" + getPackageName()));
            tryStart(intent);
            Toast.makeText(this, "请允许本应用安装未知来源应用", Toast.LENGTH_LONG).show();
            return;
        }
        pendingInstallApk = null;
        pendingInstallUpdate = null;
        Uri apkUri = Uri.parse("content://" + ApkContentProvider.AUTHORITY + "/" + apkFile.getName());
        Intent intent = new Intent(Intent.ACTION_VIEW);
        intent.setDataAndType(apkUri, "application/vnd.android.package-archive");
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        if (!tryStart(intent)) {
            Toast.makeText(this, "无法打开系统安装器", Toast.LENGTH_LONG).show();
            if (update.optBoolean("force_update", false)) {
                showUpdateDialog(update);
            }
        }
    }

    private String readResponseBody(HttpURLConnection connection) throws Exception {
        BufferedReader reader = new BufferedReader(new InputStreamReader(connection.getInputStream(), "UTF-8"));
        StringBuilder body = new StringBuilder();
        String line;
        while ((line = reader.readLine()) != null) {
            body.append(line);
        }
        reader.close();
        return body.toString();
    }

    private String buildAlertBody(JSONObject alert) {
        JSONArray seatsArray = alert.optJSONArray("matched_seats");
        StringBuilder seats = new StringBuilder();
        int count = seatsArray == null ? 0 : seatsArray.length();
        int shown = Math.min(count, 8);
        for (int i = 0; i < shown; i++) {
            if (i > 0) {
                seats.append(", ");
            }
            seats.append(seatsArray.optString(i));
        }
        if (count > shown) {
            seats.append(" 等 ").append(count).append(" 个");
        }
        return alert.optString("day")
                + " "
                + alert.optString("start_time")
                + "-"
                + alert.optString("end_time")
                + " · "
                + seats;
    }

    private boolean hasNotifiedAlert(String alertId) {
        if (alertId == null || alertId.trim().isEmpty()) {
            return false;
        }
        return getPreferences(MODE_PRIVATE).getBoolean("notified_alert_" + alertId, false);
    }

    private void markAlertNotified(String alertId) {
        if (alertId == null || alertId.trim().isEmpty()) {
            return;
        }
        SharedPreferences.Editor editor = getPreferences(MODE_PRIVATE).edit();
        editor.putBoolean("notified_alert_" + alertId, true);
        editor.apply();
    }

    private void showSeatNotification(String alertId, String title, String body, String targetUrl) {
        if (hasNotifiedAlert(alertId)) {
            return;
        }
        NotificationManager manager = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager == null) {
            return;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestNotificationPermission();
            return;
        }
        markAlertNotified(alertId);

        Intent launchIntent = new Intent(this, MainActivity.class);
        launchIntent.setFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        if (targetUrl != null && !targetUrl.trim().isEmpty()) {
            launchIntent.putExtra("target_url", targetUrl);
        }

        int pendingFlags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            pendingFlags |= PendingIntent.FLAG_IMMUTABLE;
        }
        PendingIntent pendingIntent = PendingIntent.getActivity(
                this,
                Math.abs((title + body + targetUrl).hashCode()),
                launchIntent,
                pendingFlags
        );

        Notification.Builder builder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(this, NOTIFICATION_CHANNEL_ID)
                : new Notification.Builder(this);
        builder.setSmallIcon(R.drawable.ic_launcher)
                .setContentTitle(title == null || title.isEmpty() ? "找到可预约座位" : title)
                .setContentText(body == null ? "" : body)
                .setStyle(new Notification.BigTextStyle().bigText(body == null ? "" : body))
                .setContentIntent(pendingIntent)
                .setAutoCancel(true)
                .setPriority(Notification.PRIORITY_HIGH)
                .setDefaults(Notification.DEFAULT_SOUND | Notification.DEFAULT_VIBRATE)
                .setVibrate(NOTIFICATION_VIBRATION_PATTERN);
        manager.notify(Math.abs((title + body + targetUrl).hashCode()), builder.build());
        vibrateForSeatAlert();
    }

    private void vibrateForSeatAlert() {
        Vibrator vibrator = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
        if (vibrator == null || !vibrator.hasVibrator()) {
            return;
        }
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                VibrationEffect effect = VibrationEffect.createWaveform(
                        NOTIFICATION_VIBRATION_PATTERN,
                        NOTIFICATION_VIBRATION_AMPLITUDES,
                        -1
                );
                VibrationAttributes attributes = new VibrationAttributes.Builder()
                        .setUsage(VibrationAttributes.USAGE_ALARM)
                        .build();
                vibrator.vibrate(effect, attributes);
                return;
            }

            AudioAttributes attributes = new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build();
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                VibrationEffect effect = VibrationEffect.createWaveform(
                        NOTIFICATION_VIBRATION_PATTERN,
                        NOTIFICATION_VIBRATION_AMPLITUDES,
                        -1
                );
                vibrator.vibrate(effect, attributes);
            } else {
                vibrator.vibrate(NOTIFICATION_VIBRATION_PATTERN, -1, attributes);
            }
        } catch (RuntimeException ignored) {
        }
    }

    private void flushCookies() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            CookieManager.getInstance().flush();
        }
    }

    private void flushCookiesSoon() {
        mainHandler.postDelayed(this::flushCookies, 1200);
    }

    private boolean handleUri(Uri uri) {
        if (uri == null) {
            return false;
        }

        String scheme = lower(uri.getScheme());
        if (scheme == null) {
            return false;
        }

        if ("http".equals(scheme) || "https".equals(scheme)) {
            if (prepareChaoxingCookiesThenLoad(uri)) {
                return true;
            }
            if (isInternalWebHost(uri.getHost())) {
                return false;
            }
            openExternal(uri, false);
            return true;
        }

        if ("intent".equals(scheme)) {
            openIntentUri(uri.toString());
            return true;
        }

        openExternal(uri, isChaoxingScheme(scheme));
        return true;
    }

    private boolean shouldOpenOutside(Uri uri) {
        if (uri == null) {
            return false;
        }
        String scheme = lower(uri.getScheme());
        if (scheme == null) {
            return false;
        }
        if ("http".equals(scheme) || "https".equals(scheme)) {
            return !isInternalWebHost(uri.getHost());
        }
        return "intent".equals(scheme) || isChaoxingScheme(scheme);
    }

    private boolean prepareChaoxingCookiesThenLoad(Uri uri) {
        if (!isChaoxingWebUrl(uri)) {
            return false;
        }

        String host = lower(uri.getHost());
        synchronized (chaoxingCookieLock) {
            if (preparedChaoxingCookieHosts.contains(host)) {
                return false;
            }
            if (chaoxingCookieSyncInFlight) {
                return true;
            }
            chaoxingCookieSyncInFlight = true;
        }

        String targetUrl = uri.toString();
        new Thread(() -> {
            JSONArray cookies = null;
            try {
                cookies = fetchChaoxingCookies();
            } catch (Exception ignored) {
            }

            JSONArray cookiesToInject = cookies;
            mainHandler.post(() -> {
                if (cookiesToInject != null) {
                    injectChaoxingCookies(uri, cookiesToInject);
                }
                synchronized (chaoxingCookieLock) {
                    preparedChaoxingCookieHosts.add(host);
                    chaoxingCookieSyncInFlight = false;
                }
                webView.loadUrl(targetUrl);
            });
        }, "chaoxing-cookie-sync").start();
        return true;
    }

    private boolean isChaoxingWebUrl(Uri uri) {
        if (uri == null) {
            return false;
        }
        String scheme = lower(uri.getScheme());
        return ("http".equals(scheme) || "https".equals(scheme)) && isChaoxingHost(uri.getHost());
    }

    private JSONArray fetchChaoxingCookies() throws Exception {
        String cookie = CookieManager.getInstance().getCookie(HOME_URL);
        if (cookie == null || cookie.trim().isEmpty()) {
            return null;
        }

        URL url = new URL(HOME_URL + "api/chaoxing/cookies");
        HttpURLConnection connection = (HttpURLConnection) url.openConnection();
        connection.setRequestMethod("GET");
        connection.setRequestProperty("Cookie", cookie);
        connection.setRequestProperty("Accept", "application/json");
        connection.setConnectTimeout(8000);
        connection.setReadTimeout(8000);
        int status = connection.getResponseCode();
        if (status < 200 || status >= 300) {
            return null;
        }
        return new JSONObject(readResponseBody(connection)).optJSONArray("cookies");
    }

    private void clearPreparedChaoxingCookieHosts() {
        synchronized (chaoxingCookieLock) {
            preparedChaoxingCookieHosts.clear();
            chaoxingCookieSyncInFlight = false;
        }
    }

    private void injectChaoxingCookies(Uri targetUri, JSONArray cookies) {
        CookieManager cookieManager = CookieManager.getInstance();
        for (int i = 0; i < cookies.length(); i++) {
            JSONObject item = cookies.optJSONObject(i);
            if (item == null) {
                continue;
            }
            String name = item.optString("name", "").trim();
            if (name.isEmpty()) {
                continue;
            }
            String value = item.optString("value", "");
            String domain = item.optString("domain", "").trim();
            String path = item.optString("path", "/").trim();
            if (path.isEmpty()) {
                path = "/";
            }
            boolean secure = item.optBoolean("secure", false);
            String cookieUrl = cookieUrlFor(targetUri, domain, secure);
            StringBuilder cookie = new StringBuilder();
            cookie.append(name).append("=").append(value);
            cookie.append("; Path=").append(path);
            if (!domain.isEmpty()) {
                cookie.append("; Domain=").append(domain);
            }
            if (secure) {
                cookie.append("; Secure");
            }
            cookieManager.setCookie(cookieUrl, cookie.toString());
        }
        flushCookies();
    }

    private String cookieUrlFor(Uri targetUri, String domain, boolean secure) {
        String host = cookieHost(domain);
        if (host.isEmpty()) {
            host = targetUri.getHost();
        }
        String scheme = secure ? "https" : lower(targetUri.getScheme());
        if (!"http".equals(scheme) && !"https".equals(scheme)) {
            scheme = "https";
        }
        return scheme + "://" + host + "/";
    }

    private String cookieHost(String domain) {
        if (domain == null) {
            return "";
        }
        String host = domain.trim();
        while (host.startsWith(".")) {
            host = host.substring(1);
        }
        return host;
    }

    private boolean isInternalHost(String host) {
        return INTERNAL_HOST.equals(host);
    }

    private boolean isInternalWebHost(String host) {
        return isInternalHost(host) || isChaoxingHost(host);
    }

    private boolean isChaoxingHost(String host) {
        String value = lower(host);
        return isDomain(value, "chaoxing.com") || isDomain(value, "xuexitong.com");
    }

    private boolean isDomain(String host, String domain) {
        return host != null && (host.equals(domain) || host.endsWith("." + domain));
    }

    private boolean isChaoxingScheme(String scheme) {
        return "xuexitong".equals(scheme) || "chaoxing".equals(scheme) || "cxstudy".equals(scheme);
    }

    private void openIntentUri(String uriString) {
        try {
            Intent intent = Intent.parseUri(uriString, Intent.URI_INTENT_SCHEME);
            intent.addCategory(Intent.CATEGORY_BROWSABLE);
            intent.setComponent(null);
            if (tryStart(intent)) {
                return;
            }

            String fallbackUrl = intent.getStringExtra("browser_fallback_url");
            if (fallbackUrl != null && !fallbackUrl.trim().isEmpty()) {
                Uri fallbackUri = Uri.parse(fallbackUrl);
                openExternal(fallbackUri, isChaoxingHost(fallbackUri.getHost()));
                return;
            }

            showNoHandlerToast();
        } catch (URISyntaxException e) {
            showNoHandlerToast();
        }
    }

    private void openExternal(Uri uri, boolean preferChaoxing) {
        if (preferChaoxing) {
            Intent chaoxingIntent = createViewIntent(uri);
            chaoxingIntent.setPackage(CHAOXING_PACKAGE);
            if (tryStart(chaoxingIntent)) {
                return;
            }
        }

        Intent genericIntent = createViewIntent(uri);
        if (!tryStart(genericIntent)) {
            showNoHandlerToast();
        }
    }

    private Intent createViewIntent(Uri uri) {
        Intent intent = new Intent(Intent.ACTION_VIEW, uri);
        intent.addCategory(Intent.CATEGORY_BROWSABLE);
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        return intent;
    }

    private boolean tryStart(Intent intent) {
        try {
            startActivity(intent);
            return true;
        } catch (ActivityNotFoundException | SecurityException ignored) {
            return false;
        }
    }

    private void showNoHandlerToast() {
        Toast.makeText(this, "No app can open this link", Toast.LENGTH_SHORT).show();
    }

    private String lower(String value) {
        return value == null ? null : value.toLowerCase(Locale.ROOT);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private int getStatusBarHeight() {
        int resourceId = getResources().getIdentifier("status_bar_height", "dimen", "android");
        if (resourceId > 0) {
            return getResources().getDimensionPixelSize(resourceId);
        }
        return dp(24);
    }

    private final class AndroidBridge {
        @JavascriptInterface
        public void notifySeatMatched(String alertId, String title, String body, String targetUrl) {
            mainHandler.post(() -> showSeatNotification(alertId, title, body, targetUrl));
        }

        @JavascriptInterface
        public String getGetuiClientId() {
            return GetuiPushBridge.getClientId(MainActivity.this);
        }

        @JavascriptInterface
        public void syncGetuiClientId() {
            GetuiPushBridge.syncClientIdInBackground(MainActivity.this);
        }
    }

    private final class SearchSeatWebViewClient extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            return handleUri(request.getUrl());
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, String url) {
            return handleUri(Uri.parse(url));
        }

        @Override
        public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
            errorView.setVisibility(View.GONE);
            progressBar.setVisibility(View.VISIBLE);
            progressBar.setProgress(8);
            Uri uri = Uri.parse(url);
            if (prepareChaoxingCookiesThenLoad(uri)) {
                view.stopLoading();
                return;
            }
            if (shouldOpenOutside(uri) && handleUri(uri)) {
                view.stopLoading();
            }
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            flushCookies();
            GetuiPushBridge.syncClientIdInBackground(MainActivity.this);
            progressBar.setVisibility(View.GONE);
        }

        @Override
        public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
            Uri uri = request.getUrl();
            if (isInternalHost(uri.getHost())) {
                String path = uri.getPath();
                if ("/api/chaoxing/login".equals(path) || "/api/logout".equals(path)) {
                    clearPreparedChaoxingCookieHosts();
                    flushCookiesSoon();
                }
            }
            return super.shouldInterceptRequest(view, request);
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && request.isForMainFrame()) {
                progressBar.setVisibility(View.GONE);
                errorView.setVisibility(View.VISIBLE);
            }
        }

        @Override
        public void onReceivedError(WebView view, int errorCode, String description, String failingUrl) {
            progressBar.setVisibility(View.GONE);
            errorView.setVisibility(View.VISIBLE);
        }

        @Override
        public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse errorResponse) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP
                    && request.isForMainFrame()
                    && errorResponse.getStatusCode() >= 500) {
                progressBar.setVisibility(View.GONE);
                errorView.setVisibility(View.VISIBLE);
            }
        }

        @Override
        public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
            handler.cancel();
            progressBar.setVisibility(View.GONE);
            errorView.setVisibility(View.VISIBLE);
        }
    }

    private final class SearchSeatChromeClient extends WebChromeClient {
        @Override
        public void onProgressChanged(WebView view, int newProgress) {
            progressBar.setProgress(newProgress);
            progressBar.setVisibility(newProgress >= 100 ? View.GONE : View.VISIBLE);
        }

        @Override
        public boolean onCreateWindow(WebView view, boolean isDialog, boolean isUserGesture, Message resultMsg) {
            WebView popup = new WebView(view.getContext());
            popup.setWebViewClient(new WebViewClient() {
                @Override
                public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                    return handlePopupUri(request.getUrl(), view);
                }

                @Override
                public boolean shouldOverrideUrlLoading(WebView view, String url) {
                    return handlePopupUri(Uri.parse(url), view);
                }

                @Override
                public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                    handlePopupUri(Uri.parse(url), view);
                }
            });

            WebView.WebViewTransport transport = (WebView.WebViewTransport) resultMsg.obj;
            transport.setWebView(popup);
            resultMsg.sendToTarget();
            return true;
        }

        private boolean handlePopupUri(Uri uri, WebView popup) {
            if (uri != null && isInternalWebHost(uri.getHost())) {
                webView.loadUrl(uri.toString());
                popup.stopLoading();
                return true;
            }
            return handleUri(uri);
        }
    }
}
