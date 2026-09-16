#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import concurrent.futures
from datetime import datetime, timezone, timedelta
import json
from json import loads
import sys
import threading
import time

import chaoxing
import config
import database
from services import seat_service

BEIJING_TZ = timezone(timedelta(hours=8))

_CURRENT_RESERVES_CACHE = {}
_CURRENT_RESERVES_CACHE_LOCK = threading.Lock()
_REVALIDATING_USER_IDS = set()
_REVALIDATING_LOCK = threading.Lock()

FRESH_TTL_SECONDS = 20
STALE_TTL_SECONDS = 300
CACHE_TTL_SECONDS = 180


def _get_db():
    app_mod = sys.modules.get("app")
    return getattr(app_mod, "database", database) if app_mod else database


def _get_app_attr(name, default):
    app_mod = sys.modules.get("app")
    return getattr(app_mod, name, default) if app_mod else default


def get_chaoxing_session(user_id: int):
    return database.fetch_one(
        """
        SELECT id, user_id, cx_account, cookies_json, session_valid, last_error,
               cookies_updated_at, last_login_at, cx_user_name,
               curriculum_synced_at, curriculum_sync_error
        FROM chaoxing_sessions
        WHERE user_id = %s
        """,
        (user_id,),
    )


def get_user_settings(user_id: int) -> dict:
    row = database.fetch_one(
        """
        SELECT default_webhook_url
        FROM user_settings
        WHERE user_id = %s
        """,
        (user_id,),
    )
    return row or {"default_webhook_url": ""}


def save_default_webhook_url(user_id: int, webhook_url: str) -> None:
    database.execute(
        """
        INSERT INTO user_settings (user_id, default_webhook_url)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE
            default_webhook_url = VALUES(default_webhook_url),
            updated_at = NOW()
        """,
        (user_id, webhook_url),
    )


def save_chaoxing_session(user_id: int, account: str, cookies_json: str) -> None:
    database.execute(
        """
        INSERT INTO chaoxing_sessions
            (user_id, cx_account, cookies_json, session_valid, last_error, cookies_updated_at, last_login_at)
        VALUES (%s, %s, %s, 1, NULL, NOW(), NOW())
        ON DUPLICATE KEY UPDATE
            cx_account = VALUES(cx_account),
            cookies_json = VALUES(cookies_json),
            session_valid = 1,
            last_error = NULL,
            cookies_updated_at = NOW(),
            last_login_at = NOW()
        """,
        (user_id, account, cookies_json),
    )


def mark_chaoxing_error(user_id: int, error: str) -> None:
    database.execute(
        """
        UPDATE chaoxing_sessions
        SET session_valid = 0, last_error = %s, cookies_updated_at = NOW()
        WHERE user_id = %s
        """,
        (error[:2000], user_id),
    )


def update_chaoxing_cookies(user_id: int, cookies_json: str) -> None:
    database.execute(
        """
        UPDATE chaoxing_sessions
        SET cookies_json = %s, session_valid = 1, last_error = NULL, cookies_updated_at = NOW()
        WHERE user_id = %s
        """,
        (cookies_json, user_id),
    )


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


def public_chaoxing_cookie_payload(cookies_json: str) -> dict:
    cookies = []
    for item in loads(cookies_json or "[]"):
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        cookies.append(
            {
                "name": name,
                "value": str(item.get("value") or ""),
                "domain": str(item.get("domain") or ""),
                "path": str(item.get("path") or "/") or "/",
                "secure": bool(item.get("secure")),
            }
        )
    return {"ok": True, "cookies": cookies}


def public_admin_user(row: dict) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "disabled": bool(row.get("disabled_at")),
        "disabled_at": row.get("disabled_at") or "",
        "created_at": row.get("created_at") or "",
        "last_login_at": row.get("last_login_at") or "",
        "cx_account": row.get("cx_account") or "",
        "cx_user_name": row.get("cx_user_name") or "",
        "session_valid": bool(row.get("session_valid")) if row.get("session_valid") is not None else False,
        "last_error": row.get("last_error") or "",
        "cookies_updated_at": row.get("cookies_updated_at") or "",
        "cx_last_login_at": row.get("cx_last_login_at") or "",
        "curriculum_synced_at": row.get("curriculum_synced_at") or "",
        "curriculum_sync_error": row.get("curriculum_sync_error") or "",
        "query_count": int(row.get("query_count") or 0),
        "watch_count": int(row.get("watch_count") or 0),
        "running_watch_count": int(row.get("running_watch_count") or 0),
        "push_device_count": int(row.get("push_device_count") or 0),
        "last_query_at": row.get("last_query_at") or "",
        "last_watch_at": row.get("last_watch_at") or "",
    }


def invalidate_user_reserves_cache(user_id: int) -> None:
    with _CURRENT_RESERVES_CACHE_LOCK:
        _CURRENT_RESERVES_CACHE.pop(user_id, None)


def _do_fetch_chaoxing_reserves(user_id: int, cookies_json: str) -> dict:
    now = time.time()
    now_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S")
    session = chaoxing.session_from_cookie_json(cookies_json)
    try:
        result = chaoxing.fetch_seat_index(session, config.FID_ENC)
    except Exception as exc:
        mark_chaoxing_error(user_id, str(exc))
        data = {"reserves": [], "error": str(exc), "updated_at": now_str, "stale": False}
        with _CURRENT_RESERVES_CACHE_LOCK:
            _CURRENT_RESERVES_CACHE[user_id] = {"timestamp": now, "data": data}
        return data

    if not result.get("success"):
        mark_chaoxing_error(user_id, str(result))
        data = {"reserves": [], "error": "学习通预约接口返回失败", "updated_at": now_str, "stale": False}
        with _CURRENT_RESERVES_CACHE_LOCK:
            _CURRENT_RESERVES_CACHE[user_id] = {"timestamp": now, "data": data}
        return data

    update_chaoxing_cookies(user_id, chaoxing.cookie_jar_to_json(session))
    reserves = seat_service.public_current_reserves(result)
    data = {"reserves": reserves, "error": "", "updated_at": now_str, "stale": False}
    with _CURRENT_RESERVES_CACHE_LOCK:
        _CURRENT_RESERVES_CACHE[user_id] = {"timestamp": now, "data": data}

    try:
        _get_db().execute(
            """
            UPDATE chaoxing_sessions
            SET current_reserves_json = %s, reserves_updated_at = %s
            WHERE user_id = %s
            """,
            (json.dumps(reserves, ensure_ascii=False), now_str, user_id),
        )
    except Exception:
        pass

    return data


def _async_revalidate_user_reserves(user_id: int, cookies_json: str) -> None:
    with _REVALIDATING_LOCK:
        if user_id in _REVALIDATING_USER_IDS:
            return
        _REVALIDATING_USER_IDS.add(user_id)

    def _worker():
        try:
            _do_fetch_chaoxing_reserves(user_id, cookies_json)
        finally:
            with _REVALIDATING_LOCK:
                _REVALIDATING_USER_IDS.discard(user_id)

    threading.Thread(target=_worker, name=f"reserves-revalidate-{user_id}", daemon=True).start()


def fetch_current_reserves_from_cookies(
    user_id: int,
    cookies_json: str,
    force: bool = False,
    allow_stale: bool = True,
) -> dict:
    if force:
        return _do_fetch_chaoxing_reserves(user_id, cookies_json)

    now = time.time()
    with _CURRENT_RESERVES_CACHE_LOCK:
        cached = _CURRENT_RESERVES_CACHE.get(user_id)
        if cached:
            age = now - cached["timestamp"]
            if age < FRESH_TTL_SECONDS:
                return cached["data"]
            if allow_stale and age < STALE_TTL_SECONDS:
                data = dict(cached["data"])
                data["stale"] = True
                _async_revalidate_user_reserves(user_id, cookies_json)
                return data

    if allow_stale:
        try:
            row = _get_db().fetch_one(
                "SELECT current_reserves_json, reserves_updated_at FROM chaoxing_sessions WHERE user_id = %s",
                (user_id,),
            )
            if row and row.get("current_reserves_json"):
                reserves = json.loads(row["current_reserves_json"])
                updated_at = str(row.get("reserves_updated_at") or "")
                data = {"reserves": reserves, "error": "", "updated_at": updated_at, "stale": True}
                with _CURRENT_RESERVES_CACHE_LOCK:
                    _CURRENT_RESERVES_CACHE[user_id] = {"timestamp": 0, "data": data}
                _async_revalidate_user_reserves(user_id, cookies_json)
                return data
        except Exception:
            pass

    return _do_fetch_chaoxing_reserves(user_id, cookies_json)


def public_admin_users_with_current_reserves(rows: list) -> list:
    users = []
    fetch_fn = _get_app_attr("fetch_current_reserves_from_cookies", fetch_current_reserves_from_cookies)
    user_fn = _get_app_attr("public_admin_user", public_admin_user)
    for row in rows:
        user = user_fn(row)
        cookies_json = row.get("cookies_json") or ""
        if cookies_json:
            current = fetch_fn(user["id"], cookies_json)
        else:
            current = {"reserves": [], "error": "未绑定学习通"}
        user["current_reserves"] = current.get("reserves") or []
        user["current_reserves_error"] = current.get("error") or ""
        users.append(user)
    return users


def public_admin_user_current_reserves(row: dict, force: bool = False) -> dict:
    user_id = int(row.get("id") or row.get("user_id") or 0)
    cookies_json = row.get("cookies_json") or ""
    fetch_fn = _get_app_attr("fetch_current_reserves_from_cookies", fetch_current_reserves_from_cookies)
    if cookies_json:
        if force:
            try:
                current = fetch_fn(user_id, cookies_json, force=True)
            except TypeError:
                current = fetch_fn(user_id, cookies_json)
        else:
            current = fetch_fn(user_id, cookies_json)
    else:
        current = {"reserves": [], "error": "未绑定学习通"}
    return {
        "user_id": user_id,
        "current_reserves": current.get("reserves") or [],
        "current_reserves_error": current.get("error") or "",
        "updated_at": current.get("updated_at") or "",
        "stale": bool(current.get("stale")),
    }


def fetch_admin_current_reserve_rows(limit: int = 500, force: bool = False) -> list:
    rows = _get_db().fetch_all(
        """
        SELECT u.id, s.cookies_json, s.current_reserves_json, s.reserves_updated_at
        FROM users u
        LEFT JOIN chaoxing_sessions s ON s.user_id = u.id
        ORDER BY u.id ASC
        LIMIT %s
        """,
        (limit,),
    )
    user_fn = _get_app_attr("public_admin_user_current_reserves", public_admin_user_current_reserves)
    if len(rows) <= 1:
        if force:
            try:
                return [user_fn(row, force=True) for row in rows]
            except TypeError:
                return [user_fn(row) for row in rows]
        return [user_fn(row) for row in rows]

    def _call_user_fn(r):
        if force:
            try:
                return user_fn(r, force=True)
            except TypeError:
                return user_fn(r)
        return user_fn(r)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(rows))) as executor:
        return list(executor.map(_call_user_fn, rows))


def fetch_admin_user_rows(limit: int = 500) -> list:
    rows = _get_db().fetch_all(
        """
        SELECT u.id, u.username, u.disabled_at, u.created_at, u.last_login_at,
               s.cx_account, s.session_valid, s.last_error, s.cookies_updated_at,
               s.last_login_at AS cx_last_login_at, s.cx_user_name,
               s.curriculum_synced_at, s.curriculum_sync_error,
               COALESCE(q.query_count, 0) AS query_count,
               q.last_query_at,
               COALESCE(w.watch_count, 0) AS watch_count,
               COALESCE(w.running_watch_count, 0) AS running_watch_count,
               w.last_watch_at,
               COALESCE(pd.push_device_count, 0) AS push_device_count
        FROM users u
        LEFT JOIN chaoxing_sessions s ON s.user_id = u.id
        LEFT JOIN (
            SELECT user_id, COUNT(*) AS query_count, MAX(created_at) AS last_query_at
            FROM seat_query_history
            GROUP BY user_id
        ) q ON q.user_id = u.id
        LEFT JOIN (
            SELECT user_id,
                   COUNT(*) AS watch_count,
                   SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_watch_count,
                   MAX(created_at) AS last_watch_at
            FROM seat_watch_tasks
            GROUP BY user_id
        ) w ON w.user_id = u.id
        LEFT JOIN (
            SELECT user_id, COUNT(*) AS push_device_count
            FROM push_devices
            WHERE enabled = 1
            GROUP BY user_id
        ) pd ON pd.user_id = u.id
        ORDER BY u.id ASC
        LIMIT %s
        """,
        (limit,),
    )
    user_fn = _get_app_attr("public_admin_user", public_admin_user)
    return [user_fn(row) for row in rows]


def fetch_admin_user(user_id: int):
    row = database.fetch_one(
        """
        SELECT u.id, u.username, u.disabled_at, u.created_at, u.last_login_at,
               s.cx_account, s.session_valid, s.last_error, s.cookies_updated_at,
               s.last_login_at AS cx_last_login_at, s.cx_user_name,
               s.curriculum_synced_at, s.curriculum_sync_error,
               COALESCE(q.query_count, 0) AS query_count,
               q.last_query_at,
               COALESCE(w.watch_count, 0) AS watch_count,
               COALESCE(w.running_watch_count, 0) AS running_watch_count,
               w.last_watch_at,
               COALESCE(pd.push_device_count, 0) AS push_device_count
        FROM users u
        LEFT JOIN chaoxing_sessions s ON s.user_id = u.id
        LEFT JOIN (
            SELECT user_id, COUNT(*) AS query_count, MAX(created_at) AS last_query_at
            FROM seat_query_history
            GROUP BY user_id
        ) q ON q.user_id = u.id
        LEFT JOIN (
            SELECT user_id,
                   COUNT(*) AS watch_count,
                   SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_watch_count,
                   MAX(created_at) AS last_watch_at
            FROM seat_watch_tasks
            GROUP BY user_id
        ) w ON w.user_id = u.id
        LEFT JOIN (
            SELECT user_id, COUNT(*) AS push_device_count
            FROM push_devices
            WHERE enabled = 1
            GROUP BY user_id
        ) pd ON pd.user_id = u.id
        WHERE u.id = %s
        """,
        (user_id,),
    )
    return public_admin_user(row) if row else None


def admin_summary(users: list) -> dict:
    return {
        "users": len(users),
        "synced_names": sum(1 for user in users if user.get("cx_user_name")),
        "queries": sum(int(user.get("query_count") or 0) for user in users),
        "watch_tasks": sum(int(user.get("watch_count") or 0) for user in users),
    }


def first_query_value(params: dict, name: str, default: str) -> str:
    value = params.get(name, default)
    if isinstance(value, list):
        return value[0] if value else default
    return value


def admin_pagination(params: dict) -> dict:
    try:
        page = int(first_query_value(params, "page", "1"))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(first_query_value(params, "page_size", "10"))
    except (TypeError, ValueError):
        page_size = 10
    page = max(1, page)
    page_size = min(50, max(1, page_size))
    return {
        "page": page,
        "page_size": page_size,
        "offset": (page - 1) * page_size,
    }
