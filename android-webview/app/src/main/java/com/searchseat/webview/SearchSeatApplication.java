package com.searchseat.webview;

import android.app.Application;

public class SearchSeatApplication extends Application {
    @Override
    public void onCreate() {
        super.onCreate();
        GetuiPushBridge.preInit(this);
    }
}
