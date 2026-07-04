# Admin Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a separate `/admin` dashboard with admin login, persisted Chaoxing real names, user activity summaries, and manual profile sync.

**Architecture:** Keep the existing single-process Python `http.server` structure. Add small helper functions to `auth.py`, `chaoxing.py`, and `app.py`; persist profile fields on `chaoxing_sessions`; render a separate inline `ADMIN_HTML` page. Admin APIs read database state by default and only call Chaoxing from explicit sync endpoints or after normal Chaoxing login.

**Tech Stack:** Python standard library HTTP server, PyMySQL, Requests, inline HTML/CSS/JavaScript, `unittest`.

---

## File Structure

- Modify `config.py`: add admin credential and cookie configuration.
- Modify `auth.py`: add signed admin session helpers.
- Modify `database.py`: add `chaoxing_sessions` profile columns.
- Modify `chaoxing.py`: add curriculum profile parsing and fetch helpers.
- Modify `app.py`: add profile persistence, admin routes, admin API handlers, and `ADMIN_HTML`.
- Create `tests/test_auth_admin.py`: pure admin auth tests.
- Create `tests/test_chaoxing_curriculum.py`: pure curriculum parser tests.

## Task 1: Admin Auth Helpers

**Files:**
- Modify: `config.py`
- Modify: `auth.py`
- Test: `tests/test_auth_admin.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_auth_admin.py`:

```python
import time
import unittest

import auth
import config


class AdminAuthTests(unittest.TestCase):
    def setUp(self):
        self.original_secret = config.APP_SECRET
        config.APP_SECRET = "test-secret"

    def tearDown(self):
        config.APP_SECRET = self.original_secret

    def test_admin_cookie_round_trips(self):
        cookie = auth.make_admin_session_cookie("admin")
        token = cookie.split("=", 1)[1].split(";", 1)[0]

        admin = auth.current_admin_from_header(f"{config.ADMIN_SESSION_COOKIE}={token}")

        self.assertEqual(admin, {"username": "admin"})

    def test_invalid_admin_cookie_returns_none(self):
        admin = auth.current_admin_from_header(f"{config.ADMIN_SESSION_COOKIE}=bad-token")

        self.assertIsNone(admin)

    def test_expired_admin_signature_returns_none(self):
        token = auth.sign_admin_session("admin", int(time.time()) - 1)

        admin = auth.current_admin_from_header(f"{config.ADMIN_SESSION_COOKIE}={token}")

        self.assertIsNone(admin)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_auth_admin -v`

Expected: FAIL or ERROR because `ADMIN_SESSION_COOKIE`, `make_admin_session_cookie`, `current_admin_from_header`, and `sign_admin_session` do not exist yet.

- [ ] **Step 3: Write minimal implementation**

In `config.py`, add:

```python
ADMIN_USERNAME = get_env("ADMIN_USERNAME")
ADMIN_PASSWORD = get_env("ADMIN_PASSWORD")
ADMIN_SESSION_COOKIE = "search_seat_admin_session"
ADMIN_SESSION_HOURS = get_int_env("ADMIN_SESSION_HOURS", 12)
```

In `auth.py`, add admin helpers mirroring the existing user session style:

```python
def sign_admin_session(username: str, expires_at: int) -> str:
    body = f"admin:{username}:{expires_at}"
    signature = hmac.new(config.APP_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    token = f"{body}:{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(token).decode("ascii")


def make_admin_session_cookie(username: str) -> str:
    expires_at = int(time.time() + config.ADMIN_SESSION_HOURS * 3600)
    token = sign_admin_session(username, expires_at)
    max_age = config.ADMIN_SESSION_HOURS * 3600
    return (
        f"{config.ADMIN_SESSION_COOKIE}={token}; Path=/admin; Max-Age={max_age}; "
        "HttpOnly; SameSite=Lax"
    )


def clear_admin_session_cookie() -> str:
    return f"{config.ADMIN_SESSION_COOKIE}=; Path=/admin; Max-Age=0; HttpOnly; SameSite=Lax"


def current_admin_from_header(cookie_header: str) -> Optional[dict]:
    cookie = SimpleCookie()
    cookie.load(cookie_header or "")
    morsel = cookie.get(config.ADMIN_SESSION_COOKIE)
    if not morsel:
        return None

    try:
        raw = base64.urlsafe_b64decode(morsel.value.encode("ascii")).decode("utf-8")
        prefix, username, expires_at, signature = raw.split(":", 3)
        if prefix != "admin":
            return None
        body = f"{prefix}:{username}:{expires_at}"
        expected = hmac.new(
            config.APP_SECRET.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        if int(expires_at) < int(time.time()):
            return None
    except Exception:
        return None

    return {"username": username}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_auth_admin -v`

Expected: PASS.

## Task 2: Chaoxing Curriculum Profile Parser

**Files:**
- Modify: `chaoxing.py`
- Test: `tests/test_chaoxing_curriculum.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_chaoxing_curriculum.py`:

```python
import unittest

import chaoxing


class CurriculumProfileTests(unittest.TestCase):
    def test_extracts_user_name(self):
        result = {
            "result": 1,
            "data": {
                "curriculum": {
                    "userName": "赵杰"
                }
            }
        }

        self.assertEqual(chaoxing.parse_curriculum_user_name(result), "赵杰")

    def test_rejects_missing_user_name(self):
        with self.assertRaises(RuntimeError):
            chaoxing.parse_curriculum_user_name({"result": 1, "data": {"curriculum": {}}})

    def test_rejects_failed_result(self):
        with self.assertRaises(RuntimeError):
            chaoxing.parse_curriculum_user_name({"result": 0, "msg": "未登录"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_chaoxing_curriculum -v`

Expected: FAIL or ERROR because `parse_curriculum_user_name` does not exist yet.

- [ ] **Step 3: Write minimal implementation**

In `chaoxing.py`, add:

```python
import time
```

Add constants/helpers:

```python
CHAOXING_CURRICULUM_URL = "https://kb.chaoxing.com/pc/curriculum/getMyLessons"


def build_curriculum_headers() -> dict:
    return {
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://kb.chaoxing.com/res/pc/curriculum/schedule.html",
    }


def parse_curriculum_user_name(result: dict) -> str:
    if not result.get("result"):
        raise RuntimeError(result.get("msg") or "课程接口返回失败")
    user_name = (
        result.get("data", {})
        .get("curriculum", {})
        .get("userName")
    )
    user_name = str(user_name or "").strip()
    if not user_name:
        raise RuntimeError("课程接口没有返回用户姓名")
    return user_name


def fetch_curriculum_user_name(session: requests.Session) -> str:
    params = {"curTime": int(time.time() * 1000)}
    resp = session.get(CHAOXING_CURRICULUM_URL, headers=build_curriculum_headers(), params=params, timeout=12)
    resp.raise_for_status()
    return parse_curriculum_user_name(resp.json())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_chaoxing_curriculum -v`

Expected: PASS.

## Task 3: Persist Profile Fields and Login-Time Sync

**Files:**
- Modify: `database.py`
- Modify: `app.py`

- [ ] **Step 1: Add migration columns**

In `database.py`, after `chaoxing_sessions` table creation, add:

```python
ensure_column(cursor, "chaoxing_sessions", "cx_user_name", "VARCHAR(128) NULL")
ensure_column(cursor, "chaoxing_sessions", "curriculum_synced_at", "DATETIME NULL")
ensure_column(cursor, "chaoxing_sessions", "curriculum_sync_error", "TEXT NULL")
```

- [ ] **Step 2: Add profile persistence helpers**

In `app.py`, add functions:

```python
def save_chaoxing_profile_success(user_id: int, user_name: str) -> None:
    database.execute(
        """
        UPDATE chaoxing_sessions
        SET cx_user_name = %s,
            curriculum_synced_at = NOW(),
            curriculum_sync_error = NULL
        WHERE user_id = %s
        """,
        (user_name[:128], user_id),
    )


def save_chaoxing_profile_error(user_id: int, error: str) -> None:
    database.execute(
        """
        UPDATE chaoxing_sessions
        SET curriculum_sync_error = %s
        WHERE user_id = %s
        """,
        (error[:2000], user_id),
    )


def sync_chaoxing_profile(user_id: int, cookies_json: str) -> dict:
    try:
        session = chaoxing.session_from_cookie_json(cookies_json)
        user_name = chaoxing.fetch_curriculum_user_name(session)
        save_chaoxing_profile_success(user_id, user_name)
        update_chaoxing_cookies(user_id, chaoxing.cookie_jar_to_json(session))
        return {"ok": True, "user_name": user_name}
    except Exception as exc:
        save_chaoxing_profile_error(user_id, str(exc))
        return {"ok": False, "error": str(exc)}
```

- [ ] **Step 3: Update login flow**

In `handle_chaoxing_login`, after `save_chaoxing_session(...)`, call:

```python
sync_chaoxing_profile(user["id"], chaoxing.cookie_jar_to_json(session))
```

The response remains successful even if sync fails.

- [ ] **Step 4: Verify syntax**

Run: `python3 -m py_compile app.py database.py chaoxing.py auth.py config.py`

Expected: no output and exit code 0.

## Task 4: Admin API

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Add admin route dispatch**

In `do_GET`, route:

```python
if path == "/admin":
    self.send_body(200, ADMIN_HTML)
    return
if path == "/api/admin/me":
    self.handle_admin_me()
    return
if path == "/api/admin/users":
    self.handle_admin_users()
    return
if path.startswith("/api/admin/users/"):
    self.handle_admin_user_detail(path)
    return
```

In `do_POST`, route:

```python
if path == "/api/admin/login":
    self.handle_admin_login()
    return
if path == "/api/admin/logout":
    self.discard_request_body()
    self.send_json(200, {"ok": True}, headers=[("Set-Cookie", auth.clear_admin_session_cookie())])
    return
if path.startswith("/api/admin/users/") and path.endswith("/sync-profile"):
    self.handle_admin_user_sync(path)
    return
if path == "/api/admin/users/sync-missing-profiles":
    self.handle_admin_sync_missing_profiles()
    return
```

- [ ] **Step 2: Add admin auth helpers**

In `AppHandler`, add:

```python
def current_admin(self):
    return auth.current_admin_from_header(self.headers.get("Cookie", ""))


def require_admin(self):
    admin = self.current_admin()
    if not admin:
        self.send_json(401, {"error": "请先登录管理后台"})
        return None
    return admin
```

- [ ] **Step 3: Add admin handlers**

Implement handlers that:

- Validate `ADMIN_USERNAME` and `ADMIN_PASSWORD`.
- Use `hmac.compare_digest` for credential comparison.
- List users with aggregate query and no Chaoxing network calls.
- Return detail history with recent 50 query rows and recent 50 watch rows.
- Sync one user's profile by reading `chaoxing_sessions.cookies_json`.
- Sync at most 20 users missing `cx_user_name` in one batch.

- [ ] **Step 4: Verify syntax**

Run: `python3 -m py_compile app.py`

Expected: no output and exit code 0.

## Task 5: Admin HTML

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Add `ADMIN_HTML`**

Add a compact admin page with:

- Login form.
- Header with admin username and logout.
- Summary cards for total users, synced names, total queries, total watch tasks.
- User table.
- Detail panel for a selected user.
- Buttons for refresh, sync one, and sync missing.

- [ ] **Step 2: Add JavaScript API calls**

Implement:

```javascript
async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {})
    }
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || '请求失败');
  return data;
}
```

Use it for admin login, logout, list, detail, sync one, and sync missing.

- [ ] **Step 3: Verify browser page loads**

Run the app and open `/admin`.

Expected: login form renders before authentication. After a correct admin login, user data loads from `/api/admin/users`.

## Task 6: Documentation and Final Verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document admin env vars**

Add:

```markdown
## 管理后台

访问：

```text
http://127.0.0.1:8000/admin
```

在 `.env` 配置：

```env
ADMIN_USERNAME=admin
ADMIN_PASSWORD=your-password
```

后台只读取已持久化的学习通姓名。老用户可在后台点击“补全缺失姓名”后批量同步。
```

- [ ] **Step 2: Run all tests**

Run:

```bash
python3 -m unittest discover -v
python3 -m py_compile app.py auth.py chaoxing.py config.py database.py web_service.py main.py
```

Expected: tests pass and py_compile emits no errors.

- [ ] **Step 3: Inspect git diff**

Run: `git diff --stat`

Expected: changes are limited to admin dashboard implementation, tests, and README.
