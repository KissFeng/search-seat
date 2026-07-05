package com.searchseat.webview;

import android.content.Context;

import com.igexin.sdk.GTIntentService;
import com.igexin.sdk.message.GTCmdMessage;
import com.igexin.sdk.message.GTNotificationMessage;
import com.igexin.sdk.message.GTTransmitMessage;

public class GetuiIntentService extends GTIntentService {
    @Override
    public void onReceiveServicePid(Context context, int pid) {
    }

    @Override
    public void onReceiveClientId(Context context, String clientId) {
        GetuiPushBridge.saveClientId(context, clientId);
        GetuiPushBridge.syncClientIdInBackground(context);
    }

    @Override
    public void onReceiveMessageData(Context context, GTTransmitMessage message) {
        // 当前服务端使用个推通知消息；透传消息不自动拉起应用。
    }

    @Override
    public void onReceiveOnlineState(Context context, boolean online) {
    }

    @Override
    public void onReceiveCommandResult(Context context, GTCmdMessage cmdMessage) {
    }

    @Override
    public void onNotificationMessageArrived(Context context, GTNotificationMessage message) {
    }

    @Override
    public void onNotificationMessageClicked(Context context, GTNotificationMessage message) {
        if (message == null) {
            return;
        }
        String payload = message.getPayload();
        if (payload != null && !payload.trim().isEmpty()) {
            GetuiPushBridge.openPayload(context, payload);
            return;
        }
        String url = message.getUrl();
        if (url != null && !url.trim().isEmpty()) {
            GetuiPushBridge.openPayload(context, "{\"target_url\":\"" + escapeJson(url) + "\"}");
        }
    }

    private String escapeJson(String value) {
        return value.replace("\\", "\\\\").replace("\"", "\\\"");
    }
}
