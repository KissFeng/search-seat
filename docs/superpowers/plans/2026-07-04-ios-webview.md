# iOS WebView Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a minimal iOS shell app that opens the existing Seat Radar web service in a native `WKWebView`.

**Architecture:** The iOS app is a small UIKit wrapper. Internal traffic for `103.203.140.44` stays in the `WKWebView`; external, Chaoxing, and Xuexitong links are opened by iOS through `UIApplication`.

**Tech Stack:** Swift, UIKit, WebKit, Xcode project files, asset catalogs.

---

### Task 1: Scaffold iOS App

**Files:**
- Create: `ios-webview/SeatRadar.xcodeproj/project.pbxproj`
- Create: `ios-webview/SeatRadar/AppDelegate.swift`
- Create: `ios-webview/SeatRadar/SceneDelegate.swift`
- Create: `ios-webview/SeatRadar/WebViewController.swift`
- Create: `ios-webview/SeatRadar/Info.plist`
- Create: `ios-webview/SeatRadar/LaunchScreen.storyboard`

- [ ] Create the Xcode project structure for a UIKit app named `SeatRadar`.
- [ ] Implement `WebViewController` with `WKWebView`.
- [ ] Configure HTTP access in `Info.plist` for the current server.

### Task 2: Add Branding Assets

**Files:**
- Create: `ios-webview/SeatRadar/Assets.xcassets/Contents.json`
- Create: `ios-webview/SeatRadar/Assets.xcassets/AppIcon.appiconset/Contents.json`
- Create: `ios-webview/SeatRadar/Assets.xcassets/AppIcon.appiconset/*.png`

- [ ] Generate iOS app icons based on the current green seat-radar visual direction.
- [ ] Set the app display name to `座位雷达`.

### Task 3: Document Usage

**Files:**
- Create: `ios-webview/README.md`

- [ ] Document how to open the project in Xcode.
- [ ] Document that full Xcode is required to build and install on an iPhone.
- [ ] Document the temporary HTTP/ATS setting and HTTPS recommendation.

### Task 4: Verify

- [ ] Run `plutil -lint` for plist and storyboard files.
- [ ] Check that the Xcode project references all created source files and assets.
- [ ] If full Xcode is installed, run `xcodebuild -project ios-webview/SeatRadar.xcodeproj -scheme SeatRadar -destination 'generic/platform=iOS' build`.
