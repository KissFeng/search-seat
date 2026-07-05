package com.searchseat.webview;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.media.AudioAttributes;
import android.media.RingtoneManager;
import android.os.Build;
import android.webkit.CookieManager;

import com.igexin.sdk.PushManager;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.OutputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;

public final class GetuiPushBridge {
    private static final String HOME_URL = "http://103.203.140.44:18000/";
    private static final String REGISTER_URL = HOME_URL + "api/push/devices";
    public static final String WATCH_NOTIFICATION_CHANNEL_ID = "seat_match_alerts_v3";
    public static final String ADMIN_NOTIFICATION_CHANNEL_ID = "admin_messages_v1";
    public static final String NOTIFICATION_CHANNEL_ID = WATCH_NOTIFICATION_CHANNEL_ID;
    private static final long[] NOTIFICATION_VIBRATION_PATTERN = new long[]{0, 260, 130, 260};
    private static final String PREFS = "getui_push";
    private static final String KEY_CID = "cid";
    private static volatile boolean syncInFlight = false;

    private GetuiPushBridge() {
    }

    public static void preInit(Context context) {
        try {
            Context appContext = context.getApplicationContext();
            ensureNotificationChannels(appContext);
            PushManager.getInstance().preInit(appContext);
        } catch (Throwable ignored) {
        }
    }

    public static void ensureNotificationChannels(Context context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            return;
        }
        NotificationManager manager = (NotificationManager) context.getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager == null) {
            return;
        }
        createNotificationChannel(
                manager,
                WATCH_NOTIFICATION_CHANNEL_ID,
                "座位命中提醒",
                "蹲座位命中时发送提醒"
        );
        createNotificationChannel(
                manager,
                ADMIN_NOTIFICATION_CHANNEL_ID,
                "后台消息提醒",
                "管理后台主动发送的消息提醒"
        );
    }

    private static void createNotificationChannel(
            NotificationManager manager,
            String channelId,
            String channelName,
            String channelDescription
    ) {
        if (manager.getNotificationChannel(channelId) != null) {
            return;
        }
        NotificationChannel channel = new NotificationChannel(
                channelId,
                channelName,
                NotificationManager.IMPORTANCE_HIGH
        );
        channel.setDescription(channelDescription);
        channel.enableVibration(true);
        channel.setVibrationPattern(NOTIFICATION_VIBRATION_PATTERN);
        AudioAttributes attributes = new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_NOTIFICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build();
        channel.setSound(RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION), attributes);
        manager.createNotificationChannel(channel);
    }

    public static void initialize(Context context) {
        try {
            Context appContext = context.getApplicationContext();
            preInit(appContext);
            PushManager.getInstance().initialize(appContext);
            saveClientId(appContext, PushManager.getInstance().getClientid(appContext));
        } catch (Throwable ignored) {
        }
    }

    public static void saveClientId(Context context, String cid) {
        if (cid == null || cid.trim().isEmpty()) {
            return;
        }
        SharedPreferences.Editor editor = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit();
        editor.putString(KEY_CID, cid.trim());
        editor.apply();
    }

    public static String getClientId(Context context) {
        String cid = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_CID, "");
        if (cid != null && !cid.trim().isEmpty()) {
            return cid.trim();
        }
        try {
            cid = PushManager.getInstance().getClientid(context.getApplicationContext());
        } catch (Throwable ignored) {
            cid = "";
        }
        return cid == null ? "" : cid.trim();
    }

    public static void syncClientIdInBackground(Context context) {
        Context appContext = context.getApplicationContext();
        String cid = getClientId(appContext);
        if (cid.isEmpty() || syncInFlight) {
            return;
        }
        syncInFlight = true;
        new Thread(() -> {
            try {
                String cookie = CookieManager.getInstance().getCookie(HOME_URL);
                if (cookie == null || cookie.trim().isEmpty()) {
                    return;
                }
                JSONObject body = new JSONObject();
                body.put("platform", "android");
                body.put("cid", cid);
                body.put("device_name", deviceName());
                body.put("sdk_version", sdkVersion(appContext));
                body.put("app_version_code", versionCode(appContext));
                body.put("app_version_name", versionName(appContext));
                body.put("notifications_enabled", notificationsEnabled(appContext));
                postJson(REGISTER_URL, cookie, body);
            } catch (Exception ignored) {
            } finally {
                syncInFlight = false;
            }
        }, "getui-cid-sync").start();
    }

    public static void openPayload(Context context, String payload) {
        String targetUrl = "";
        try {
            JSONObject json = new JSONObject(payload == null ? "" : payload);
            targetUrl = json.optString("target_url", "");
        } catch (Exception ignored) {
        }
        Intent intent = new Intent(context, MainActivity.class);
        intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        if (targetUrl != null && !targetUrl.trim().isEmpty()) {
            intent.putExtra("target_url", targetUrl);
        }
        context.startActivity(intent);
    }

    private static void postJson(String url, String cookie, JSONObject body) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setRequestMethod("POST");
        connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
        connection.setRequestProperty("Accept", "application/json");
        connection.setRequestProperty("Cookie", cookie);
        connection.setConnectTimeout(8000);
        connection.setReadTimeout(8000);
        connection.setDoOutput(true);
        byte[] data = body.toString().getBytes("UTF-8");
        OutputStream output = connection.getOutputStream();
        output.write(data);
        output.close();
        int status = connection.getResponseCode();
        if (status >= 200 && status < 300) {
            BufferedReader reader = new BufferedReader(new InputStreamReader(connection.getInputStream(), "UTF-8"));
            while (reader.readLine() != null) {
            }
            reader.close();
        }
    }

    private static boolean notificationsEnabled(Context context) {
        try {
            return PushManager.getInstance().areNotificationsEnabled(context.getApplicationContext());
        } catch (Throwable ignored) {
            return true;
        }
    }

    private static String sdkVersion(Context context) {
        try {
            return PushManager.getInstance().getVersion(context.getApplicationContext());
        } catch (Throwable ignored) {
            return "";
        }
    }

    private static int versionCode(Context context) {
        try {
            PackageInfo info = context.getPackageManager().getPackageInfo(context.getPackageName(), 0);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                return (int) info.getLongVersionCode();
            }
            return info.versionCode;
        } catch (Exception ignored) {
            return 0;
        }
    }

    private static String versionName(Context context) {
        try {
            PackageInfo info = context.getPackageManager().getPackageInfo(context.getPackageName(), 0);
            return info.versionName == null ? "" : info.versionName;
        } catch (Exception ignored) {
            return "";
        }
    }

    private static String deviceName() {
        String manufacturer = Build.MANUFACTURER == null ? "" : Build.MANUFACTURER.trim();
        String model = Build.MODEL == null ? "" : Build.MODEL.trim();
        if (model.toLowerCase().startsWith(manufacturer.toLowerCase())) {
            return model;
        }
        return (manufacturer + " " + model).trim();
    }
}
