# Seat Radar iOS WebView

Minimal iOS wrapper for the Seat Radar web service.

- Home URL: `http://103.203.140.44:18000/`
- App name: `座位雷达`
- Internal links for `103.203.140.44` stay inside `WKWebView`.
- External links, Chaoxing links, and Xuexitong links open through iOS.

## Build And Install

Open the project with full Xcode:

```sh
open ios-webview/SeatRadar.xcodeproj
```

In Xcode:

1. Select the `SeatRadar` target.
2. Set `Signing & Capabilities` to your Apple team.
3. Select an iPhone or simulator.
4. Run the app.

The current machine has Command Line Tools selected, not full Xcode, so command-line iOS builds will fail until Xcode is installed and selected:

```sh
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
```

Then a generic build can be checked with:

```sh
xcodebuild -project ios-webview/SeatRadar.xcodeproj -scheme SeatRadar -destination 'generic/platform=iOS' build
```

## Network Note

The app currently allows HTTP traffic because the service URL is plain HTTP. This is acceptable for local testing, but HTTPS is the better long-term setup for iOS compatibility and App Store review.
