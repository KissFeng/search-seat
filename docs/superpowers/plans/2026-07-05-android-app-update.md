# Android App Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Android-only APK publishing in the admin console and native Android update checking, downloading, verification, and installer launch.

**Architecture:** The backend keeps APK metadata in MySQL and stores APK files under a local upload directory. The admin console uploads APKs through multipart form data, while Android clients call a public JSON endpoint with their current `versionCode`. The Android app downloads the APK, verifies SHA-256, and opens the system package installer because normal Android apps cannot silently install packages.

**Tech Stack:** Python standard library HTTP server, PyMySQL, embedded HTML/CSS/JavaScript admin UI, Java Android Activity, custom Android `ContentProvider`, `unittest`, Android SDK command-line build tools.

---

## File Structure

- Modify `config.py`
  - Add `APK_UPLOAD_DIR` with a default inside the repository runtime directory.
- Modify `database.py`
  - Add the `app_versions` table in `init_db()`.
- Modify `app.py`
  - Add app-version helper functions.
  - Add admin list/upload handlers.
  - Add public update-check and APK download handlers.
  - Add the admin UI menu/view/form/table JavaScript.
- Create `tests/test_app_updates.py`
  - Unit tests for version validation, public payload formatting, latest-update selection, APK filename validation, and upload metadata construction.
- Modify `android-webview/app/src/main/AndroidManifest.xml`
  - Add install permission.
  - Register an internal APK content provider.
- Create `android-webview/app/src/main/java/com/searchseat/webview/ApkContentProvider.java`
  - Serve downloaded APK files from app cache through `content://`.
- Modify `android-webview/app/src/main/java/com/searchseat/webview/MainActivity.java`
  - Add update check, dialog, APK download, SHA-256 verification, unknown-source permission handling, and installer launch.
- Modify `.gitignore`
  - Ignore uploaded APK files if they are stored under the repository runtime directory.
- Modify `README.md`
  - Document the Android update flow and the system installer limitation.

Before executing, check `git status --short`. Preserve existing uncommitted changes in `app.py`, `database.py`, Android Java, and tests; do not revert or overwrite them.

---

### Task 1: Backend Data Model And Pure Helpers

**Files:**
- Modify: `config.py`
- Modify: `database.py`
- Modify: `app.py`
- Create: `tests/test_app_updates.py`
- Modify: `.gitignore`

- [ ] **Step 1: Write failing helper tests**

Create `tests/test_app_updates.py`:

```python
import hashlib
import unittest

import app


class AppUpdateTests(unittest.TestCase):
    def test_android_version_code_from_params_accepts_positive_integer(self):
        value = app.android_version_code_from_params({"version_code": ["6"]})

        self.assertEqual(value, 6)

    def test_android_version_code_from_params_rejects_invalid_values(self):
        with self.assertRaisesRegex(ValueError, "version_code 必须是正整数"):
            app.android_version_code_from_params({"version_code": ["0"]})
        with self.assertRaisesRegex(ValueError, "version_code 必须是正整数"):
            app.android_version_code_from_params({"version_code": ["bad"]})

    def test_safe_apk_filename_accepts_apk_basename(self):
        self.assertEqual(app.safe_apk_filename("SearchSeat-7.apk"), "SearchSeat-7.apk")

    def test_safe_apk_filename_rejects_path_traversal_and_non_apk(self):
        with self.assertRaisesRegex(ValueError, "APK 文件名不正确"):
            app.safe_apk_filename("../bad.apk")
        with self.assertRaisesRegex(ValueError, "只支持上传 APK 文件"):
            app.safe_apk_filename("bad.txt")

    def test_public_app_version_payload_includes_download_url(self):
        row = {
            "id": 3,
            "platform": "android",
            "version_code": 7,
            "version_name": "1.6",
            "apk_filename": "search-seat-7.apk",
            "apk_size": 123,
            "apk_sha256": "a" * 64,
            "release_notes": "修复通知",
            "force_update": 1,
            "published": 1,
            "created_at": "2026-07-05 18:00:00",
        }

        payload = app.public_app_version(row, "http://127.0.0.1:8000")

        self.assertEqual(payload["version_code"], 7)
        self.assertEqual(payload["version_name"], "1.6")
        self.assertEqual(payload["apk_url"], "http://127.0.0.1:8000/downloads/apks/search-seat-7.apk")
        self.assertTrue(payload["force_update"])

    def test_app_update_payload_returns_false_when_no_newer_version(self):
        payload = app.app_update_payload(None, 7, "http://127.0.0.1:8000")

        self.assertEqual(payload, {"update": False})

    def test_app_update_payload_returns_newer_version(self):
        row = {
            "id": 3,
            "platform": "android",
            "version_code": 8,
            "version_name": "1.7",
            "apk_filename": "search-seat-8.apk",
            "apk_size": 456,
            "apk_sha256": "b" * 64,
            "release_notes": "",
            "force_update": 0,
            "published": 1,
            "created_at": "2026-07-05 18:00:00",
        }

        payload = app.app_update_payload(row, 7, "http://127.0.0.1:8000")

        self.assertTrue(payload["update"])
        self.assertEqual(payload["version_code"], 8)

    def test_sha256_hex_reads_file(self):
        digest = app.sha256_hex(b"abc")

        self.assertEqual(digest, hashlib.sha256(b"abc").hexdigest())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run helper tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_app_updates -v
```

Expected: FAIL with missing attributes such as `android_version_code_from_params`.

- [ ] **Step 3: Add config and ignore rules**

In `config.py`, after `get_int_env()`:

```python
APK_UPLOAD_DIR = Path(get_env("APK_UPLOAD_DIR", str(BASE_DIR / "uploads" / "apks")))
```

In `.gitignore`, add:

```gitignore
uploads/
```

- [ ] **Step 4: Add database table**

In `database.init_db()`, after the `chat_messages` table creation, add:

```python
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS app_versions (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                    platform VARCHAR(20) NOT NULL,
                    version_code INT UNSIGNED NOT NULL,
                    version_name VARCHAR(64) NOT NULL,
                    apk_filename VARCHAR(255) NOT NULL,
                    apk_size BIGINT UNSIGNED NOT NULL,
                    apk_sha256 CHAR(64) NOT NULL,
                    release_notes TEXT NULL,
                    force_update TINYINT(1) NOT NULL DEFAULT 0,
                    published TINYINT(1) NOT NULL DEFAULT 1,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (id),
                    UNIQUE KEY uk_app_versions_platform_code (platform, version_code),
                    KEY idx_app_versions_latest (platform, published, version_code)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
```

- [ ] **Step 5: Add pure helpers**

In `app.py`, add imports:

```python
import hashlib
import os
from urllib.parse import quote
```

Near the other helper functions, add:

```python
def android_version_code_from_params(params: dict) -> int:
    raw = (params.get("version_code") or [""])[0]
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("version_code 必须是正整数") from exc
    if value <= 0:
        raise ValueError("version_code 必须是正整数")
    return value


def safe_apk_filename(filename: str) -> str:
    name = os.path.basename(str(filename or "").strip())
    if not name or name in {".", ".."} or name != str(filename or "").strip():
        raise ValueError("APK 文件名不正确")
    if not name.lower().endswith(".apk"):
        raise ValueError("只支持上传 APK 文件")
    return name


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def public_app_version(row: dict, base_url: str) -> dict:
    filename = safe_apk_filename(row["apk_filename"])
    return {
        "id": row.get("id"),
        "platform": row.get("platform", "android"),
        "version_code": int(row["version_code"]),
        "version_name": row["version_name"],
        "force_update": bool(row.get("force_update")),
        "apk_url": f"{base_url.rstrip('/')}/downloads/apks/{quote(filename)}",
        "apk_size": int(row["apk_size"]),
        "apk_sha256": row["apk_sha256"],
        "release_notes": row.get("release_notes") or "",
        "created_at": row.get("created_at"),
    }


def app_update_payload(row: dict, current_version_code: int, base_url: str) -> dict:
    if not row or int(row["version_code"]) <= int(current_version_code):
        return {"update": False}
    return {"update": True, **public_app_version(row, base_url)}
```

- [ ] **Step 6: Run helper tests and verify pass**

Run:

```bash
python3 -m unittest tests.test_app_updates -v
```

Expected: PASS.

- [ ] **Step 7: Commit backend foundation**

Run:

```bash
git add config.py database.py app.py tests/test_app_updates.py .gitignore
git commit -m "Add Android app update metadata helpers"
```

---

### Task 2: Backend Admin Upload, Public Check, And Download Routes

**Files:**
- Modify: `app.py`
- Modify: `tests/test_app_updates.py`

- [ ] **Step 1: Add failing tests for metadata construction and latest lookup**

Append to `tests/test_app_updates.py`:

```python
    def test_apk_upload_metadata_builds_stable_filename(self):
        metadata = app.apk_upload_metadata(
            original_filename="Search Seat.apk",
            version_code=9,
            version_name="1.8",
            release_notes="优化更新",
            force_update=True,
            data=b"apk bytes",
        )

        self.assertEqual(metadata["platform"], "android")
        self.assertEqual(metadata["version_code"], 9)
        self.assertEqual(metadata["version_name"], "1.8")
        self.assertEqual(metadata["apk_filename"], "search-seat-9.apk")
        self.assertEqual(metadata["apk_size"], 9)
        self.assertEqual(metadata["apk_sha256"], hashlib.sha256(b"apk bytes").hexdigest())
        self.assertEqual(metadata["release_notes"], "优化更新")
        self.assertEqual(metadata["force_update"], 1)

    def test_apk_upload_metadata_requires_apk_bytes(self):
        with self.assertRaisesRegex(ValueError, "APK 文件不能为空"):
            app.apk_upload_metadata("x.apk", 10, "1.9", "", False, b"")
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_app_updates -v
```

Expected: FAIL with missing `apk_upload_metadata`.

- [ ] **Step 3: Implement upload metadata and DB functions**

In `app.py`, add:

```python
def apk_upload_metadata(
    original_filename: str,
    version_code: int,
    version_name: str,
    release_notes: str,
    force_update: bool,
    data: bytes,
) -> dict:
    safe_apk_filename(original_filename)
    if version_code <= 0:
        raise ValueError("版本号必须是正整数")
    version_name = str(version_name or "").strip()
    if not version_name:
        raise ValueError("版本名称不能为空")
    if not data:
        raise ValueError("APK 文件不能为空")
    return {
        "platform": "android",
        "version_code": int(version_code),
        "version_name": version_name,
        "apk_filename": f"search-seat-{int(version_code)}.apk",
        "apk_size": len(data),
        "apk_sha256": sha256_hex(data),
        "release_notes": str(release_notes or "").strip(),
        "force_update": 1 if force_update else 0,
    }


def fetch_app_versions(limit: int = 20) -> list:
    return database.fetch_all(
        """
        SELECT id, platform, version_code, version_name, apk_filename, apk_size,
               apk_sha256, release_notes, force_update, published, created_at
        FROM app_versions
        WHERE platform = 'android'
        ORDER BY version_code DESC
        LIMIT %s
        """,
        (limit,),
    )


def fetch_latest_android_version_above(version_code: int):
    return database.fetch_one(
        """
        SELECT id, platform, version_code, version_name, apk_filename, apk_size,
               apk_sha256, release_notes, force_update, published, created_at
        FROM app_versions
        WHERE platform = 'android' AND published = 1 AND version_code > %s
        ORDER BY version_code DESC
        LIMIT 1
        """,
        (version_code,),
    )


def insert_app_version(metadata: dict) -> int:
    return database.execute(
        """
        INSERT INTO app_versions (
            platform, version_code, version_name, apk_filename, apk_size,
            apk_sha256, release_notes, force_update, published
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1)
        """,
        (
            metadata["platform"],
            metadata["version_code"],
            metadata["version_name"],
            metadata["apk_filename"],
            metadata["apk_size"],
            metadata["apk_sha256"],
            metadata["release_notes"],
            metadata["force_update"],
        ),
    )
```

- [ ] **Step 4: Add base URL and multipart helpers**

In `app.py`, add:

```python
from email.parser import BytesParser
from email.policy import default as email_default_policy


def request_base_url(handler) -> str:
    proto = "https" if handler.headers.get("X-Forwarded-Proto") == "https" else "http"
    host = handler.headers.get("Host") or f"127.0.0.1:{config.APP_PORT}"
    return f"{proto}://{host}"


def parse_multipart_form(handler) -> dict:
    content_type = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        raise ValueError("请求必须使用 multipart/form-data")
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length)
    header = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n\r\n"
    ).encode("utf-8")
    message = BytesParser(policy=email_default_policy).parsebytes(header + raw)
    form = {}
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename() or ""
        data = part.get_payload(decode=True) or b""
        form[name] = {
            "filename": filename,
            "data": data,
            "value": "" if filename else data.decode("utf-8", errors="replace"),
        }
    return form


def form_value(form: dict, name: str, default: str = "") -> str:
    item = form.get(name)
    if item is None or item.get("filename"):
        return default
    return str(item.get("value") or "").strip()
```

- [ ] **Step 5: Add HTTP handlers**

In `AppHandler.do_GET()`, before `/api/admin/users`:

```python
            if path == "/api/admin/app-versions":
                self.handle_admin_app_versions()
                return
```

In `AppHandler.do_GET()`, before `/api/me`:

```python
            if path == "/api/app-update/android":
                self.handle_android_app_update(parsed)
                return
            if path.startswith("/downloads/apks/"):
                self.handle_apk_download(path)
                return
```

In `AppHandler.do_POST()`, before `/api/chaoxing/login`:

```python
            if path == "/api/admin/app-versions":
                self.handle_admin_app_version_upload()
                return
```

Add methods to `AppHandler`:

```python
    def handle_admin_app_versions(self):
        if not self.require_admin():
            return
        database.init_db()
        base_url = request_base_url(self)
        versions = [public_app_version(row, base_url) for row in fetch_app_versions()]
        self.send_json(200, {"versions": versions})

    def handle_android_app_update(self, parsed):
        database.init_db()
        current_version_code = android_version_code_from_params(parse_qs(parsed.query))
        row = fetch_latest_android_version_above(current_version_code)
        self.send_json(200, app_update_payload(row, current_version_code, request_base_url(self)))

    def handle_admin_app_version_upload(self):
        if not self.require_admin():
            self.discard_request_body()
            return
        form = parse_multipart_form(self)
        apk_item = form.get("apk")
        if apk_item is None or not apk_item.get("filename"):
            raise ValueError("请选择 APK 文件")
        data = apk_item.get("data") or b""
        metadata = apk_upload_metadata(
            apk_item["filename"],
            int(form_value(form, "version_code", "0") or "0"),
            form_value(form, "version_name"),
            form_value(form, "release_notes"),
            form_value(form, "force_update") in {"1", "true", "on", "yes"},
            data,
        )
        config.APK_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        target = config.APK_UPLOAD_DIR / metadata["apk_filename"]
        if target.exists():
            raise ValueError("这个版本的 APK 文件已存在")
        target.write_bytes(data)
        try:
            insert_app_version(metadata)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        base_url = request_base_url(self)
        self.send_json(200, {"ok": True, "version": public_app_version(metadata, base_url)})

    def handle_apk_download(self, path: str):
        filename = safe_apk_filename(path.rsplit("/", 1)[-1])
        target = config.APK_UPLOAD_DIR / filename
        if not target.exists() or not target.is_file():
            self.send_json(404, {"error": "APK 不存在"})
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.android.package-archive")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)
```

- [ ] **Step 6: Run backend tests**

Run:

```bash
python3 -m unittest tests.test_app_updates -v
python3 -m unittest discover tests -v
```

Expected: PASS. If tests fail due to existing unrelated uncommitted changes, capture the failing test names before editing.

- [ ] **Step 7: Commit backend routes**

Run:

```bash
git add app.py tests/test_app_updates.py
git commit -m "Add Android app update backend routes"
```

---

### Task 3: Management Console App Update View

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Add admin menu and view markup**

In `ADMIN_HTML`, add a third menu button after `消息聊天室`:

```html
<button class="admin-menu-item" type="button" data-admin-target="appUpdateAdminView">App 更新</button>
```

Add a new view after `chatAdminView`:

```html
      <div id="appUpdateAdminView" class="admin-view hidden" data-admin-view>
        <section>
          <div class="toolbar">
            <div>
              <h2>Android App 更新</h2>
              <div class="muted">上传 APK 后，Android 客户端会在进入应用时检查版本。</div>
            </div>
            <button class="ghost" id="refreshAppVersionsBtn" type="button">刷新版本</button>
          </div>
          <form id="appVersionForm" class="range-actions" enctype="multipart/form-data">
            <label>APK
              <input name="apk" type="file" accept=".apk,application/vnd.android.package-archive" required>
            </label>
            <label>版本号
              <input name="version_code" type="number" min="1" step="1" required>
            </label>
            <label>版本名
              <input name="version_name" placeholder="1.6" required>
            </label>
            <label class="check-row">
              <input name="force_update" type="checkbox" value="1">
              强制更新
            </label>
            <label>更新说明
              <textarea name="release_notes" rows="3" placeholder="本次更新内容"></textarea>
            </label>
            <button type="submit">上传发布</button>
          </form>
          <div class="table-wrap" style="margin-top: 12px;">
            <table>
              <thead>
                <tr>
                  <th>版本号</th>
                  <th>版本名</th>
                  <th>大小</th>
                  <th>SHA-256</th>
                  <th>强制</th>
                  <th>时间</th>
                </tr>
              </thead>
              <tbody id="appVersionRows"></tbody>
            </table>
          </div>
          <div id="appUpdateMessage" class="message"></div>
        </section>
      </div>
```

- [ ] **Step 2: Add JavaScript bindings and render functions**

Near other `querySelector` constants:

```javascript
const appVersionForm = document.querySelector('#appVersionForm');
const appVersionRows = document.querySelector('#appVersionRows');
const appUpdateMessage = document.querySelector('#appUpdateMessage');
let appVersions = [];
```

Add functions:

```javascript
function formatBytes(value) {
  const size = Number(value || 0);
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  if (size >= 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${size} B`;
}

function renderAppVersions() {
  appVersionRows.innerHTML = appVersions.map(version => `<tr>
    <td>${escapeHtml(version.version_code)}</td>
    <td><b>${escapeHtml(version.version_name)}</b></td>
    <td>${escapeHtml(formatBytes(version.apk_size))}</td>
    <td><code>${escapeHtml(String(version.apk_sha256 || '').slice(0, 12))}</code></td>
    <td>${version.force_update ? '<span class="pill warn">强制</span>' : '<span class="pill">可选</span>'}</td>
    <td>${escapeHtml(version.created_at || '-')}</td>
  </tr>`).join('') || '<tr><td colspan="6">暂无版本</td></tr>';
}

async function loadAppVersions() {
  const data = await api('/api/admin/app-versions', { method: 'GET', headers: {} });
  appVersions = data.versions || [];
  renderAppVersions();
}
```

In `switchAdminView()`, add:

```javascript
  if (targetId === 'appUpdateAdminView') {
    loadAppVersions().catch(error => setMessage(appUpdateMessage, error.message));
  }
```

In `loadMe()` and successful login, call:

```javascript
  await loadAppVersions();
```

- [ ] **Step 3: Add upload submission**

Add event listeners near other admin listeners:

```javascript
document.querySelector('#refreshAppVersionsBtn').addEventListener('click', async () => {
  try {
    await loadAppVersions();
    setMessage(appUpdateMessage, '已刷新', true);
  } catch (error) {
    setMessage(appUpdateMessage, error.message);
  }
});

appVersionForm.addEventListener('submit', async event => {
  event.preventDefault();
  setMessage(appUpdateMessage, '正在上传 APK...');
  const formData = new FormData(appVersionForm);
  try {
    const res = await fetch('/api/admin/app-versions', {
      method: 'POST',
      body: formData
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '上传失败');
    appVersionForm.reset();
    await loadAppVersions();
    setMessage(appUpdateMessage, `已发布 ${data.version.version_name}`, true);
  } catch (error) {
    setMessage(appUpdateMessage, error.message);
  }
});
```

- [ ] **Step 4: Run syntax and unit tests**

Run:

```bash
python3 -m py_compile app.py
python3 -m unittest discover tests -v
```

Expected: PASS.

- [ ] **Step 5: Manual admin smoke test**

Run:

```bash
python3 app.py
```

Open `http://127.0.0.1:8000/admin`, log in, switch to `App 更新`, and verify the empty table renders. Stop the server after the check.

- [ ] **Step 6: Commit management UI**

Run:

```bash
git add app.py
git commit -m "Add Android app update admin UI"
```

---

### Task 4: Android APK Content Provider

**Files:**
- Modify: `android-webview/app/src/main/AndroidManifest.xml`
- Create: `android-webview/app/src/main/java/com/searchseat/webview/ApkContentProvider.java`

- [ ] **Step 1: Update manifest**

In `AndroidManifest.xml`, add permission near the existing permissions:

```xml
<uses-permission android:name="android.permission.REQUEST_INSTALL_PACKAGES" />
```

Inside `<application>`, add:

```xml
        <provider
            android:name=".ApkContentProvider"
            android:authorities="com.searchseat.webview.apkprovider"
            android:exported="false"
            android:grantUriPermissions="true" />
```

- [ ] **Step 2: Add provider implementation**

Create `android-webview/app/src/main/java/com/searchseat/webview/ApkContentProvider.java`:

```java
package com.searchseat.webview;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;
import android.database.MatrixCursor;

import java.io.File;
import java.io.FileNotFoundException;

public class ApkContentProvider extends ContentProvider {
    public static final String AUTHORITY = "com.searchseat.webview.apkprovider";

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public String getType(Uri uri) {
        return "application/vnd.android.package-archive";
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        File file = fileForUri(uri);
        if (!file.exists() || !file.isFile()) {
            throw new FileNotFoundException("APK not found");
        }
        return ParcelFileDescriptor.open(file, ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection, String[] selectionArgs, String sortOrder) {
        File file = fileForUri(uri);
        MatrixCursor cursor = new MatrixCursor(new String[]{OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE});
        cursor.addRow(new Object[]{file.getName(), file.length()});
        return cursor;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        throw new UnsupportedOperationException("Read only");
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException("Read only");
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException("Read only");
    }

    private File fileForUri(Uri uri) {
        String name = uri.getLastPathSegment();
        if (name == null || !name.endsWith(".apk") || name.contains("/") || name.contains("..")) {
            name = "search-seat-update.apk";
        }
        return new File(getContext().getCacheDir(), name);
    }
}
```

- [ ] **Step 3: Build Android project**

Run:

```bash
./android-webview/build.sh
```

Expected: build succeeds and writes `android-webview/dist/search-seat-webview.apk`.

- [ ] **Step 4: Commit provider**

Run:

```bash
git add android-webview/app/src/main/AndroidManifest.xml android-webview/app/src/main/java/com/searchseat/webview/ApkContentProvider.java
git commit -m "Add Android APK install content provider"
```

---

### Task 5: Android Update Check, Download, Verify, And Install

**Files:**
- Modify: `android-webview/app/src/main/java/com/searchseat/webview/MainActivity.java`

- [ ] **Step 1: Add imports and constants**

Add imports:

```java
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.content.pm.PackageInfo;
import android.os.Environment;
import android.provider.Settings;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.security.MessageDigest;
```

Add constants and fields:

```java
    private static final String UPDATE_CHECK_URL = HOME_URL + "api/app-update/android";
    private static final int UNKNOWN_APP_SOURCE_REQUEST = 61;
    private boolean updateCheckInFlight = false;
    private boolean optionalUpdateDismissed = false;
    private JSONObject pendingInstallUpdate = null;
    private File pendingInstallApk = null;
```

- [ ] **Step 2: Trigger update checks**

In `onCreate()` after `startNativeAlertPolling();`:

```java
        checkForUpdatesInBackground(false);
```

In `onResume()` after `pollAlertsOnceInBackground();`:

```java
        if (pendingInstallUpdate != null && pendingInstallApk != null) {
            installDownloadedApk(pendingInstallApk, pendingInstallUpdate);
        } else {
            checkForUpdatesInBackground(false);
        }
```

- [ ] **Step 3: Add version and check methods**

Add methods to `MainActivity`:

```java
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
                        + "&version_name=" + java.net.URLEncoder.encode(currentVersionName(), "UTF-8");
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
```

- [ ] **Step 4: Add update dialog**

Add:

```java
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
        if (!forceUpdate) {
            builder.setNegativeButton("稍后", (dialog, which) -> optionalUpdateDismissed = true);
        } else {
            builder.setCancelable(false);
            builder.setNegativeButton("退出", (dialog, which) -> finish());
        }
        builder.show();
    }
```

- [ ] **Step 5: Add download and SHA-256 verification**

Add:

```java
    private void downloadUpdateInBackground(JSONObject update) {
        Toast.makeText(this, "正在下载更新...", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            File apkFile = new File(getCacheDir(), "search-seat-update.apk");
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

    private String sha256File(File file) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        InputStream input = new java.io.FileInputStream(file);
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
```

- [ ] **Step 6: Add installer launch**

Add:

```java
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
```

- [ ] **Step 7: Build Android project**

Run:

```bash
./android-webview/build.sh
```

Expected: build succeeds and writes `android-webview/dist/search-seat-webview.apk`.

- [ ] **Step 8: Commit Android update client**

Run:

```bash
git add android-webview/app/src/main/java/com/searchseat/webview/MainActivity.java
git commit -m "Add Android app update client"
```

---

### Task 6: Documentation And End-To-End Verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document Android update operations**

Add to `README.md` under the management backend section:

````markdown
## Android App 更新

管理后台的“App 更新”页可以上传 Android APK、填写 `versionCode` 和 `versionName`、更新说明，并选择是否强制更新。

Android 客户端进入应用时会请求 `/api/app-update/android` 检查更新。发现新版本后会下载 APK、校验 SHA-256，然后打开 Android 系统安装器。普通 Android 应用不能静默安装 APK，用户仍需要在系统安装界面确认。

APK 文件默认保存到 `uploads/apks/`，可通过环境变量覆盖：

```env
APK_UPLOAD_DIR=/absolute/path/to/apks
```
````

- [ ] **Step 2: Run full Python verification**

Run:

```bash
python3 -m py_compile app.py database.py config.py auth.py chaoxing.py main.py web_service.py
python3 -m unittest discover tests -v
```

Expected: PASS.

- [ ] **Step 3: Run Android build verification**

Run:

```bash
./android-webview/build.sh
```

Expected: PASS with `Built .../android-webview/dist/search-seat-webview.apk`.

- [ ] **Step 4: Manual backend update check**

Start the server:

```bash
python3 app.py
```

In another terminal, after uploading a version through `/admin`, run:

```bash
curl "http://127.0.0.1:8000/api/app-update/android?version_code=1&version_name=1.0"
```

Expected JSON contains `"update": true`, the higher `version_code`, and a `/downloads/apks/` URL.

Stop the server after the check.

- [ ] **Step 5: Commit docs and verification cleanup**

Run:

```bash
git add README.md
git commit -m "Document Android app update flow"
```

---

## Self-Review

- Spec coverage: data model, admin upload, public update check, APK download, Android prompt/download/hash/install flow, installer limitation, tests, and docs are covered.
- Placeholder scan: no `TODO`, `TBD`, or deferred implementation language is intentionally left in the task steps.
- Type consistency: backend uses `version_code`, `version_name`, `apk_filename`, `apk_size`, `apk_sha256`, `release_notes`, and `force_update` consistently across tests, DB helpers, API payloads, admin UI, and Android JSON parsing.
