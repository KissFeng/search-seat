# Android App Update Design

## Goal

Add an Android-only update flow for the Search Seat WebView app.

Administrators can upload a new APK and publish version metadata from the existing management console. Android clients check the server on app entry, prompt the user when a newer APK exists, download it, verify it, and open the Android package installer.

The project no longer supports an iOS shell app. The `ios-webview` app folder and old iOS implementation plan are removed to avoid stale product scope.

## Constraints

- The current backend is a standard-library `BaseHTTPRequestHandler` app in `app.py`, not Flask.
- The management console is embedded in `ADMIN_HTML` inside `app.py`.
- The Android app is a plain Java WebView wrapper built by `android-webview/build.sh`, without Gradle.
- Android does not allow a normal third-party app to silently install APKs. The app can automatically download an APK and launch the system installer, but the user must confirm installation.
- Existing uncommitted work in `app.py`, `database.py`, Android Java, and tests must be preserved.

## Recommended Approach

Use server-hosted APK updates.

The backend stores APK files under a local upload directory and records version metadata in MySQL. The Android app calls a public update-check endpoint with its current `versionCode`. When a higher published `versionCode` exists, the endpoint returns the APK URL, size, SHA-256, version name, release notes, and force-update flag.

This keeps deployment simple and fits the current self-hosted app. It avoids app-market dependencies and does not require changing the Android build system.

## Data Model

Add an `app_versions` table:

- `id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY`
- `platform VARCHAR(20) NOT NULL`, currently only `android`
- `version_code INT UNSIGNED NOT NULL`
- `version_name VARCHAR(64) NOT NULL`
- `apk_filename VARCHAR(255) NOT NULL`
- `apk_size BIGINT UNSIGNED NOT NULL`
- `apk_sha256 CHAR(64) NOT NULL`
- `release_notes TEXT NULL`
- `force_update TINYINT(1) NOT NULL DEFAULT 0`
- `published TINYINT(1) NOT NULL DEFAULT 1`
- `created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP`

Indexes:

- unique key on `(platform, version_code)`
- lookup key on `(platform, published, version_code)`

Only published rows are visible to clients.

## Backend API

### Admin APIs

`GET /api/admin/app-versions`

Returns recent Android version records for the management console.

`POST /api/admin/app-versions`

Accepts `multipart/form-data` with:

- `apk`: APK file
- `version_code`: positive integer
- `version_name`: non-empty string
- `release_notes`: optional text
- `force_update`: boolean-like value

The handler validates the admin session, validates the APK filename/content enough for this project, writes the file to the configured APK upload directory, calculates SHA-256 and size, then inserts metadata.

The backend should reject duplicate `version_code` values and reject non-APK uploads.

### Public APIs

`GET /api/app-update/android?version_code=<int>&version_name=<string>`

Returns:

```json
{
  "update": false
}
```

or:

```json
{
  "update": true,
  "version_code": 7,
  "version_name": "1.6",
  "force_update": false,
  "apk_url": "http://103.203.140.44:18000/downloads/apks/search-seat-7.apk",
  "apk_size": 1234567,
  "apk_sha256": "hex...",
  "release_notes": "更新说明"
}
```

`GET /downloads/apks/<filename>`

Serves stored APK files with:

- `Content-Type: application/vnd.android.package-archive`
- `Content-Length`
- `Content-Disposition: attachment`

Range requests are useful but not required for the first version.

## Management UI

Add a new menu item: `App 更新`.

The view contains:

- upload form for APK, version code, version name, force-update checkbox, and release notes
- latest version summary
- table of recent versions with version code, version name, APK size, SHA-256 prefix, force-update status, and created time
- upload status/error message

The existing JSON `api()` helper should remain for JSON endpoints. APK upload should use `fetch()` with `FormData` and no explicit `Content-Type` header so the browser sets the multipart boundary.

## Android Client Flow

On app start and on resume, check for updates with the current package `versionCode` and `versionName`.

If no update exists, continue normally.

If an update exists:

1. Show a native update dialog with version name and release notes.
2. For optional updates, allow canceling.
3. For forced updates, prevent canceling and keep prompting until the user starts the update or exits.
4. Download the APK to app-private cache or external files storage.
5. Verify the downloaded file SHA-256 against the server response.
6. Open the system package installer with a `content://` URI.

Required Android changes:

- Add `android.permission.REQUEST_INSTALL_PACKAGES`.
- Add a `FileProvider`-style content URI mechanism. Because this project does not use AndroidX, implement a small custom `ContentProvider` or switch to a file path accepted by supported SDK levels only if safe. The preferred implementation is a minimal provider that serves the downloaded APK from the app cache.
- On Android 8+, if unknown-source install permission is missing, open `Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES` for this package.
- Avoid repeated prompts in the same session after the user dismisses an optional update.

## Error Handling

- Admin upload returns clear validation errors for missing APK, invalid version code, duplicate version code, and write failures.
- Client update check failures are non-blocking unless a forced update has already been returned.
- Download failures show a retryable toast/dialog.
- SHA mismatch deletes the downloaded APK and reports a verification failure.
- Installer launch failures show a message explaining that the APK was downloaded but Android could not open the installer.

## Testing

Backend tests:

- database migration creates `app_versions`
- public update API returns `update: false` when current version is latest
- public update API returns latest higher published version
- admin upload rejects unauthenticated requests
- admin upload rejects duplicate version codes and non-APK files

Android/manual checks:

- build still succeeds with `./android-webview/build.sh`
- fresh app launch checks update endpoint
- optional update can be dismissed
- forced update cannot be dismissed into normal app usage
- downloaded APK hash is verified before installer launch

## Out Of Scope

- iOS update support
- silent APK installation
- Play Store or third-party app-store integration
- rollback management
- differential/patch updates
