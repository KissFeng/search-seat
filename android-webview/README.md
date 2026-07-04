# Search Seat Android WebView

Minimal Android wrapper for the Search Seat web service.

- Home URL: `http://103.203.140.44:18000/`
- The home server stays inside WebView.
- External links open outside the WebView.
- Chaoxing / Xuexitong links try `com.chaoxing.mobile` first, then fall back to the normal Android link handler.

## Build

This project intentionally avoids Gradle. It uses the local Android SDK tools directly.

```sh
./android-webview/build.sh
```

The signed APK is written to:

```text
android-webview/dist/search-seat-webview.apk
```

