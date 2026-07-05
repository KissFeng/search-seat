# Search Seat Android WebView

Minimal Android wrapper for the Search Seat web service.

- Home URL: `http://103.203.140.44:18000/`
- The home server stays inside WebView.
- External links open outside the WebView.
- Chaoxing / Xuexitong links try `com.chaoxing.mobile` first, then fall back to the normal Android link handler.
- Getui self-built push channel is integrated. The build script downloads `gtsdk` and `gtc` AARs from Getui's Maven repository.

## Build

This project intentionally avoids Gradle. It uses the local Android SDK tools directly.

```sh
./android-webview/build.sh
```

`GETUI_APP_ID` defaults to the current Getui app id. Override it only when building another Getui application:

```sh
GETUI_APP_ID=your_app_id ./android-webview/build.sh
```

The signed APK is written to:

```text
android-webview/dist/search-seat-webview.apk
```

## Server push config

Set these on the Python service side before starting the server:

```text
GETUI_APP_ID=your_app_id
GETUI_APP_KEY=your_app_key
GETUI_MASTER_SECRET=your_master_secret
```

Without those values, the app still builds and the existing polling/webhook reminders keep working, but the server skips Getui REST pushes.
