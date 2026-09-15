#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import JSONDecodeError, dumps, loads
import hashlib
import hmac
import html
import os
import queue
import threading
import time
from urllib.parse import parse_qs, quote, urlencode, urlparse

import auth
import chaoxing
import config
import database
import requests

from services.template_service import get_template, clear_template_cache
from services.chat_service import (
    parse_chat_datetime,
    chat_message_text_from_payload,
    public_chat_message,
    admin_chat_delete_scope,
    fetch_chat_messages,
    fetch_chat_message,
    save_chat_message,
    delete_chat_messages,
)
from services.update_service import (
    android_version_code_from_params,
    safe_apk_filename,
    sha256_hex,
    public_app_version,
    app_update_payload,
    apk_upload_metadata,
    request_base_url,
    parse_multipart_form,
    form_value,
    fetch_app_versions,
    fetch_latest_android_version_above,
    insert_app_version,
    stream_apk_file,
)
from services.push_service import (
    truthy,
    json_default,
    getui_configured,
    public_push_device,
    save_push_device,
    disable_push_device,
    fetch_push_cids,
    getui_base_url,
    getui_sign,
    getui_auth_token,
    getui_post,
    watch_alert_title,
    watch_alert_body,
    getui_request_id,
    getui_watch_payload,
    push_getui_watch_alert_to_cid,
    send_getui_watch_alert,
    admin_push_payload,
    fetch_push_devices_for_users,
    getui_admin_payload,
    push_getui_admin_message_to_cid,
    send_admin_push_message,
    send_watch_webhook,
    subscribe_watch_alerts,
    unsubscribe_watch_alerts,
    publish_watch_alert,
    WATCH_STATUS_LABELS,
)
from services.seat_service import (
    validate_day,
    validate_time,
    validate_time_range,
    millisecond_time,
    millisecond_value,
    reserve_status_label,
    timetable_week_num_from_payload,
    room_seat_config,
    public_room,
    get_room,
    room_label,
    room_seat_width,
    public_seat_num,
    checkin_url,
    reserve_checkin_window,
    public_current_reserve,
    public_current_reserves,
    public_reserve_records,
    local_reserve_path,
    official_seat_index_url,
    seat_range,
    filter_unwanted_seats,
    build_seat_response,
    save_query_history,
    delete_query_history,
    fetch_user_query_history,
    RESERVE_STATUS_LABELS,
    CHECKIN_WINDOW_MINUTES,
)
from services.transcript_service import (
    normalize_student_info,
    save_academic_transcript,
    prune_academic_transcripts,
    fetch_recent_academic_transcripts,
    fetch_academic_transcript_by_id,
)
from services.user_service import (
    get_chaoxing_session,
    get_user_settings,
    save_default_webhook_url,
    save_chaoxing_session,
    mark_chaoxing_error,
    update_chaoxing_cookies,
    save_chaoxing_profile_success,
    save_chaoxing_profile_error,
    sync_chaoxing_profile,
    public_chaoxing_cookie_payload,
    public_admin_user,
    fetch_current_reserves_from_cookies,
    invalidate_user_reserves_cache,
    public_admin_users_with_current_reserves,
    public_admin_user_current_reserves,
    fetch_admin_current_reserve_rows,
    fetch_admin_user_rows,
    fetch_admin_user,
    admin_summary,
    first_query_value,
    admin_pagination,
)
from services.watch_service import (
    validate_watch_interval,
    watch_expires_at,
    public_watch_task,
    fetch_watch_tasks,
    fetch_watch_alerts,
    acknowledge_watch_alert,
    cancel_watch_task,
    build_watch_alert,
    finish_watch_task,
    schedule_watch_retry,
    process_watch_task,
    run_due_watch_tasks,
    watch_worker_loop,
    start_watch_worker,
    WATCH_WORKER_STARTED,
    WATCH_WORKER_LOCK,
)

ADMIN_HTML = get_template("admin.html")
INDEX_HTML = get_template("index.html")


class AppHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def send_body(self, status: int, body: str, content_type: str = "text/html; charset=utf-8", headers=None) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or []):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status: int, body: dict, headers=None) -> None:
        self.send_body(
            status,
            dumps(body, ensure_ascii=False, default=json_default),
            "application/json; charset=utf-8",
            headers=headers,
        )

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return loads(raw)

    def discard_request_body(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 0:
            self.rfile.read(length)

    def current_user(self):
        return auth.current_user_from_header(self.headers.get("Cookie", ""))

    def current_admin(self):
        return auth.current_admin_from_header(self.headers.get("Cookie", ""))

    def require_user(self):
        user = self.current_user()
        if not user:
            self.send_json(401, {"error": "请先登录"})
            return None
        return user

    def require_admin(self):
        admin = self.current_admin()
        if not admin:
            self.send_json(401, {"error": "请先登录管理后台"})
            return None
        return admin

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                user = self.current_user()
                html_body = get_template("index.html")
                if user:
                    html_body = html_body.replace(
                        '<section id="authPanel" class="auth">',
                        '<section id="authPanel" class="auth hidden">',
                    )
                    html_body = html_body.replace(
                        '<div id="appPanel" class="hidden">',
                        '<div id="appPanel">',
                    )
                    cx = get_chaoxing_session(user["id"])
                    display_name = (cx and cx.get("cx_user_name")) or user["username"]
                    html_body = html_body.replace(
                        '<div id="topUser" class="row hidden">',
                        '<div id="topUser" class="row">',
                    )
                    html_body = html_body.replace(
                        '<span id="username"></span>',
                        f'<span id="username">{html.escape(display_name)}</span>',
                    )
                page_headers = [("Cache-Control", "no-cache, no-store, must-revalidate")]
                if user:
                    page_headers.append(("Set-Cookie", auth.make_session_cookie(user["id"])))
                self.send_body(
                    200,
                    html_body,
                    headers=page_headers,
                )
                return
            if path == "/admin":
                self.send_body(200, get_template("admin.html"))
                return
            if path == "/reserve":
                self.handle_reserve_redirect(parsed.query)
                return
            if path == "/checkin":
                self.handle_checkin_redirect(parsed.query)
                return
            if path == "/api/admin/me":
                self.handle_admin_me()
                return
            if path == "/api/admin/app-versions":
                self.handle_admin_app_versions()
                return
            if path == "/api/admin/users":
                self.handle_admin_users()
                return
            if path == "/api/admin/users/current-reserves":
                self.handle_admin_users_current_reserves()
                return
            if path == "/api/admin/chat/messages":
                self.handle_admin_chat_messages()
                return
            if path.startswith("/api/admin/users/") and path.endswith("/cookie"):
                self.handle_admin_user_cookie(path)
                return
            if path.startswith("/api/admin/users/"):
                self.handle_admin_user_detail(parsed)
                return
            if path == "/api/app-update/android":
                self.handle_android_app_update(parsed)
                return
            if path.startswith("/downloads/apks/"):
                self.handle_apk_download(path)
                return
            if path == "/api/me":
                self.handle_me()
                return
            if path == "/api/chaoxing/cookies":
                self.handle_chaoxing_cookies()
                return
            if path == "/api/history":
                self.handle_history()
                return
            if path == "/api/seats/current-reserves":
                self.handle_current_reserves()
                return
            if path == "/api/seats/reserve-history":
                self.handle_reserve_history()
                return
            if path == "/api/watch-tasks":
                self.handle_watch_tasks()
                return
            if path == "/api/watch-alerts":
                self.handle_watch_alerts()
                return
            if path == "/api/push/devices":
                self.handle_push_devices()
                return
            if path == "/api/watch-alerts/stream":
                self.handle_watch_alert_stream()
                return
            if path == "/api/chat/messages":
                self.handle_chat_messages()
                return
            if path == "/api/chaoxing/transcript":
                self.handle_chaoxing_transcript_history()
                return
            if path.startswith("/api/history/"):
                self.handle_history_detail(path)
                return
            self.send_json(404, {"error": "not found"})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"error": f"服务器内部错误：{str(exc)}"})

    def handle_reserve_redirect(self, query: str):
        params = parse_qs(query)
        room = get_room((params.get("room_id") or [config.ROOMS[0]["room_id"]])[0])
        day = validate_day((params.get("day") or [datetime.now().strftime("%Y-%m-%d")])[0])
        seat = (params.get("seat") or [""])[0]
        user = self.current_user()
        if user:
            invalidate_user_reserves_cache(user["id"])
        target = chaoxing.build_reserve_url(str(room["room_id"]), str(room["fid_enc"]), day, seat)
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def handle_checkin_redirect(self, query: str):
        params = parse_qs(query)
        room = get_room((params.get("room_id") or [config.ROOMS[0]["room_id"]])[0])
        seat = (params.get("seat") or [""])[0]
        user = self.current_user()
        if user:
            invalidate_user_reserves_cache(user["id"])
        target = checkin_url(str(room["room_id"]), seat)
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/logout":
                user = self.current_user()
                payload = self.read_json()
                if user:
                    disable_push_device(user["id"], payload.get("cid"))
                self.send_json(200, {"ok": True}, headers=[("Set-Cookie", auth.clear_session_cookie())])
                return
            if path == "/api/admin/login":
                self.handle_admin_login()
                return
            if path == "/api/admin/logout":
                self.discard_request_body()
                self.send_json(200, {"ok": True}, headers=[("Set-Cookie", auth.clear_admin_session_cookie())])
                return
            if path == "/api/admin/users/sync-missing-profiles":
                self.discard_request_body()
                self.handle_admin_sync_missing_profiles()
                return
            if path == "/api/admin/chat/messages/delete":
                self.handle_admin_chat_messages_delete()
                return
            if path == "/api/admin/push/send":
                self.handle_admin_push_send()
                return
            if path.startswith("/api/admin/users/") and path.endswith("/disable"):
                self.discard_request_body()
                self.handle_admin_user_disabled(path, True)
                return
            if path.startswith("/api/admin/users/") and path.endswith("/enable"):
                self.discard_request_body()
                self.handle_admin_user_disabled(path, False)
                return
            if path.startswith("/api/admin/users/") and path.endswith("/sync-profile"):
                self.discard_request_body()
                self.handle_admin_user_sync(path)
                return
            if path == "/api/admin/app-versions":
                self.handle_admin_app_version_upload()
                return
            if path == "/api/chaoxing/login":
                self.handle_chaoxing_login()
                return
            if path == "/api/chaoxing/timetable":
                self.handle_chaoxing_timetable()
                return
            if path == "/api/chaoxing/transcript":
                self.handle_chaoxing_transcript()
                return
            if path == "/api/seats/query":
                self.handle_seat_query()
                return
            if path == "/api/history/delete":
                self.handle_history_delete()
                return
            if path.startswith("/api/history/") and path.endswith("/delete"):
                self.discard_request_body()
                self.handle_history_delete(path)
                return
            if path == "/api/watch-tasks":
                self.handle_watch_task_create()
                return
            if path.startswith("/api/watch-tasks/") and path.endswith("/cancel"):
                self.discard_request_body()
                self.handle_watch_task_cancel(path)
                return
            if path.startswith("/api/watch-alerts/") and path.endswith("/ack"):
                self.handle_watch_alert_ack(path)
                return
            if path == "/api/push/devices":
                self.handle_push_device_register()
                return
            if path == "/api/chat/messages":
                self.handle_chat_message_create()
                return
            self.send_json(404, {"error": "not found"})
        except JSONDecodeError:
            self.send_json(400, {"error": "JSON 格式不正确"})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

    def handle_admin_login(self):
        payload = self.read_json()
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        if not config.ADMIN_USERNAME or not config.ADMIN_PASSWORD:
            self.send_json(500, {"error": "管理账号未配置，请设置 ADMIN_USERNAME 和 ADMIN_PASSWORD"})
            return
        if not (
            hmac.compare_digest(username, config.ADMIN_USERNAME)
            and hmac.compare_digest(password, config.ADMIN_PASSWORD)
        ):
            self.send_json(401, {"error": "管理账号或密码不正确"})
            return
        self.send_json(
            200,
            {"ok": True, "admin": {"username": username}},
            headers=[("Set-Cookie", auth.make_admin_session_cookie(username))],
        )

    def handle_admin_me(self):
        admin = self.current_admin()
        self.send_json(200, {"admin": admin})

    def handle_admin_users(self):
        if not self.require_admin():
            return
        users = fetch_admin_user_rows()
        self.send_json(200, {"users": users, "summary": admin_summary(users)})

    def handle_admin_users_current_reserves(self):
        if not self.require_admin():
            return
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        force = (params.get("force") or ["0"])[0] in ("1", "true", "True")
        user_id_param = (params.get("user_id") or [""])[0]
        if user_id_param.isdigit():
            user_id = int(user_id_param)
            cx = get_chaoxing_session(user_id)
            if not cx or not cx.get("cookies_json"):
                self.send_json(200, {"reserves": [{"user_id": user_id, "current_reserves": [], "current_reserves_error": "未绑定学习通", "updated_at": "", "stale": False}]})
                return
            current = fetch_current_reserves_from_cookies(user_id, cx["cookies_json"], force=True)
            self.send_json(200, {"reserves": [{
                "user_id": user_id,
                "current_reserves": current.get("reserves") or [],
                "current_reserves_error": current.get("error") or "",
                "updated_at": current.get("updated_at") or "",
                "stale": bool(current.get("stale")),
            }]})
            return

        reserves = fetch_admin_current_reserve_rows(force=force)
        self.send_json(200, {"reserves": reserves})

    def handle_admin_app_versions(self):
        if not self.require_admin():
            return
        base_url = request_base_url(self)
        versions = [public_app_version(row, base_url) for row in fetch_app_versions()]
        self.send_json(200, {"versions": versions})

    def handle_android_app_update(self, parsed):
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
        version_code = int(form_value(form, "version_code", "0") or "0")
        if database.fetch_one(
            "SELECT id FROM app_versions WHERE platform = 'android' AND version_code = %s",
            (version_code,),
        ):
            raise ValueError("该 versionCode 已存在")

        metadata = apk_upload_metadata(
            original_filename=apk_item["filename"],
            version_code=version_code,
            version_name=form_value(form, "version_name"),
            release_notes=form_value(form, "release_notes"),
            force_update=truthy(form_value(form, "force_update")),
            data=data,
        )
        config.APK_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        target_path = config.APK_UPLOAD_DIR / metadata["apk_filename"]
        target_path.write_bytes(data)
        metadata["id"] = insert_app_version(metadata)
        self.send_json(200, {"ok": True, "version": public_app_version(metadata, request_base_url(self))})

    def handle_apk_download(self, path: str):
        filename = safe_apk_filename(path.rsplit("/", 1)[-1])
        target = config.APK_UPLOAD_DIR / filename
        if not target.exists() or not target.is_file():
            self.send_json(404, {"error": "APK 不存在"})
            return
        stream_apk_file(target, self)

    def handle_admin_user_detail(self, parsed):
        if not self.require_admin():
            return
        try:
            user_id = self.admin_user_id_from_path(parsed.path)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        user = fetch_admin_user(user_id)
        if not user:
            self.send_json(404, {"error": "用户不存在"})
            return
        params = parse_qs(parsed.query)
        query_pagination = admin_pagination(
            {
                "page": params.get("query_page", params.get("page", ["1"])),
                "page_size": params.get("query_page_size", params.get("page_size", ["10"])),
            }
        )
        watch_pagination = admin_pagination(
            {
                "page": params.get("watch_page", params.get("page", ["1"])),
                "page_size": params.get("watch_page_size", params.get("page_size", ["10"])),
            }
        )
        query_total = database.fetch_one(
            "SELECT COUNT(*) AS count FROM seat_query_history WHERE user_id = %s",
            (user_id,),
        )["count"]
        watch_total = database.fetch_one(
            "SELECT COUNT(*) AS count FROM seat_watch_tasks WHERE user_id = %s",
            (user_id,),
        )["count"]

        query_rows = database.fetch_all(
            """
            SELECT id, room_id, fid_enc, day, start_time, end_time,
                   available_count, occupied_count, pair_count, created_at
            FROM seat_query_history
            WHERE user_id = %s
            ORDER BY id DESC
            LIMIT %s OFFSET %s
            """,
            (user_id, query_pagination["page_size"], query_pagination["offset"]),
        )
        for row in query_rows:
            row["room_name"] = room_label(row["room_id"])

        watch_rows = database.fetch_all(
            """
            SELECT id, user_id, room_id, fid_enc, day, start_time, end_time,
                   target_seats_json, ignore_no_power, ignore_sunny, webhook_url,
                   interval_seconds, status, matched_seats_json, last_checked_at,
                   next_check_at, expires_at, last_error, created_at,
                   reminder_ack_at, reminder_action
            FROM seat_watch_tasks
            WHERE user_id = %s
            ORDER BY id DESC
            LIMIT %s OFFSET %s
            """,
            (user_id, watch_pagination["page_size"], watch_pagination["offset"]),
        )
        self.send_json(
            200,
            {
                "user": user,
                "query_history": query_rows,
                "watch_history": [public_watch_task(row) for row in watch_rows],
                "query_pagination": {
                    **query_pagination,
                    "total": int(query_total or 0),
                    "total_pages": max(1, ((int(query_total or 0) - 1) // query_pagination["page_size"]) + 1),
                },
                "watch_pagination": {
                    **watch_pagination,
                    "total": int(watch_total or 0),
                    "total_pages": max(1, ((int(watch_total or 0) - 1) // watch_pagination["page_size"]) + 1),
                },
            },
        )

    def handle_admin_user_sync(self, path: str):
        if not self.require_admin():
            return
        try:
            user_id = self.admin_user_id_from_path(path)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        cx = get_chaoxing_session(user_id)
        if not cx:
            self.send_json(404, {"error": "用户未保存学习通 Cookie"})
            return
        result = sync_chaoxing_profile(user_id, cx["cookies_json"])
        self.send_json(200, {"ok": True, "sync": result, "user": fetch_admin_user(user_id)})

    def handle_admin_user_cookie(self, path: str):
        if not self.require_admin():
            return
        try:
            user_id = self.admin_user_id_from_path(path)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        cx = get_chaoxing_session(user_id)
        if not cx:
            self.send_json(404, {"error": "用户未保存学习通 Cookie"})
            return
        self.send_json(
            200,
            {
                "user_id": user_id,
                "session_valid": bool(cx["session_valid"]),
                "cookie": chaoxing.cookie_json_to_header(cx["cookies_json"]),
            },
        )

    def handle_admin_user_disabled(self, path: str, disabled: bool):
        if not self.require_admin():
            return
        try:
            user_id = self.admin_user_id_from_path(path)
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
            return
        if disabled:
            changed = database.execute(
                "UPDATE users SET disabled_at = NOW() WHERE id = %s AND disabled_at IS NULL",
                (user_id,),
            )
        else:
            changed = database.execute(
                "UPDATE users SET disabled_at = NULL WHERE id = %s",
                (user_id,),
            )
        user = fetch_admin_user(user_id)
        if not user:
            self.send_json(404, {"error": "用户不存在"})
            return
        self.send_json(200, {"ok": True, "changed": changed, "user": user})

    def handle_admin_sync_missing_profiles(self):
        if not self.require_admin():
            return
        rows = database.fetch_all(
            """
            SELECT user_id, cookies_json
            FROM chaoxing_sessions
            WHERE cx_user_name IS NULL OR cx_user_name = ''
            ORDER BY user_id ASC
            LIMIT 20
            """,
        )
        results = []
        for row in rows:
            result = sync_chaoxing_profile(row["user_id"], row["cookies_json"])
            results.append({"user_id": row["user_id"], **result})
        users = fetch_admin_user_rows()
        self.send_json(
            200,
            {
                "ok": True,
                "processed": len(results),
                "results": results,
                "users": users,
                "summary": admin_summary(users),
            },
        )

    def handle_admin_chat_messages(self):
        if not self.require_admin():
            return
        self.send_json(200, {"messages": fetch_chat_messages(200)})

    def handle_admin_chat_messages_delete(self):
        if not self.require_admin():
            return
        payload = self.read_json()
        scope = admin_chat_delete_scope(payload)
        deleted = delete_chat_messages(scope)
        self.send_json(200, {"ok": True, "deleted": deleted, "messages": fetch_chat_messages(200)})

    def handle_admin_push_send(self):
        if not self.require_admin():
            return
        result = send_admin_push_message(self.read_json())
        self.send_json(200, {"ok": True, "result": result})

    def handle_me(self):
        user = self.current_user()
        if not user:
            self.send_json(200, {"user": None})
            return

        cx = get_chaoxing_session(user["id"])
        settings = get_user_settings(user["id"])
        display_name = (cx and cx.get("cx_user_name")) or user["username"]
        self.send_json(
            200,
            {
                "user": {
                    **user,
                    "name": display_name,
                },
                "chaoxing": {
                    "bound": bool(cx),
                    "account": cx["cx_account"] if cx else "",
                    "user_name": (cx and cx.get("cx_user_name")) or "",
                    "session_valid": bool(cx["session_valid"]) if cx else False,
                    "last_error": cx["last_error"] if cx else "",
                    "cookies_updated_at": cx["cookies_updated_at"] if cx else "",
                    "last_login_at": cx["last_login_at"] if cx else "",
                },
                "defaults": {
                    "rooms": [public_room(room) for room in config.ROOMS],
                    "default_room_id": config.ROOMS[0]["room_id"],
                    "official_index_url": official_seat_index_url(config.FID_ENC),
                    "default_webhook_url": settings.get("default_webhook_url") or config.NOTIFY_WEBHOOK_URL or "",
                },
            },
            headers=[("Set-Cookie", auth.make_session_cookie(user["id"]))],
        )

    def handle_chaoxing_cookies(self):
        user = self.require_user()
        if not user:
            return
        cx = get_chaoxing_session(user["id"])
        if not cx:
            self.send_json(409, {"error": "请先登录学习通"})
            return
        self.send_json(
            200,
            public_chaoxing_cookie_payload(cx["cookies_json"]),
            headers=[("Cache-Control", "no-store")],
        )

    def handle_history(self):
        user = self.require_user()
        if not user:
            return
        rows = database.fetch_all(
            """
            SELECT id, room_id, fid_enc, day, start_time, end_time, available_count,
                   occupied_count, pair_count, created_at
            FROM seat_query_history
            WHERE user_id = %s
            ORDER BY id DESC
            LIMIT 50
            """,
            (user["id"],),
        )
        for row in rows:
            row["room_name"] = room_label(row["room_id"])
        self.send_json(200, {"history": rows})

    def handle_current_reserves(self):
        user = self.require_user()
        if not user:
            return
        cx = get_chaoxing_session(user["id"])
        if not cx:
            self.send_json(409, {"error": "请先登录学习通"})
            return

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        force = (params.get("force") or ["0"])[0] in ("1", "true", "True")

        res = fetch_current_reserves_from_cookies(user["id"], cx["cookies_json"], force=force)
        if res.get("error") and not res.get("reserves"):
            self.send_json(502, {"error": res["error"], "reserves": []})
            return

        self.send_json(200, {
            "ok": True,
            "reserves": res.get("reserves") or [],
            "updated_at": res.get("updated_at") or "",
            "stale": bool(res.get("stale")),
        })

    def handle_reserve_history(self):
        user = self.require_user()
        if not user:
            return
        cx = get_chaoxing_session(user["id"])
        if not cx:
            self.send_json(409, {"error": "请先登录学习通"})
            return

        session = chaoxing.session_from_cookie_json(cx["cookies_json"])
        try:
            result = chaoxing.fetch_reserve_list(session, config.FID_ENC, page_size=10)
        except Exception as exc:
            mark_chaoxing_error(user["id"], str(exc))
            raise

        if not result.get("success"):
            mark_chaoxing_error(user["id"], str(result))
            self.send_json(502, {"error": "学习通预约记录接口返回失败", "raw": result})
            return

        update_chaoxing_cookies(user["id"], chaoxing.cookie_jar_to_json(session))
        self.send_json(200, {"ok": True, "records": public_reserve_records(result, 10)})

    def handle_history_detail(self, path: str):
        user = self.require_user()
        if not user:
            return
        history_id = self.history_id_from_path(path)
        row = database.fetch_one(
            """
            SELECT id, room_id, day, start_time, end_time, result_json
            FROM seat_query_history
            WHERE id = %s AND user_id = %s
            """,
            (history_id, user["id"]),
        )
        if not row:
            self.send_json(404, {"error": "查询记录不存在"})
            return
        if not row.get("result_json"):
            self.send_json(
                200,
                {
                    "history_id": row["id"],
                    "missing": True,
                    "message": "这条旧记录没有保存座位快照，请重新查询一次。之后的新查询记录都可以查看当次座位内容。",
                },
            )
            return
        self.send_json(200, {"history_id": row["id"], "result": loads(row["result_json"])})

    def handle_history_delete(self, path: str = ""):
        user = self.require_user()
        if not user:
            return

        if path:
            ids = [self.history_id_from_path(path)]
        else:
            payload = self.read_json()
            ids = [int(item) for item in payload.get("ids", []) if str(item).isdigit()]
        if not ids:
            self.send_json(400, {"error": "请选择要删除的查询记录"})
            return

        placeholders = ",".join(["%s"] * len(ids))
        params = ids + [user["id"]]
        deleted = database.execute(
            f"DELETE FROM seat_query_history WHERE id IN ({placeholders}) AND user_id = %s",
            params,
        )
        self.send_json(200, {"ok": True, "deleted": deleted})

    def handle_watch_tasks(self):
        user = self.require_user()
        if not user:
            return
        self.send_json(200, {"tasks": fetch_watch_tasks(user["id"])})

    def handle_watch_alerts(self):
        user = self.require_user()
        if not user:
            return
        self.send_json(200, {"alerts": fetch_watch_alerts(user["id"])})

    def handle_push_devices(self):
        user = self.require_user()
        if not user:
            return
        rows = database.fetch_all(
            """
            SELECT *
            FROM push_devices
            WHERE user_id = %s AND enabled = 1
            ORDER BY last_seen_at DESC
            LIMIT 10
            """,
            (user["id"],),
        )
        self.send_json(
            200,
            {
                "configured": getui_configured(),
                "devices": [public_push_device(row) for row in rows],
            },
        )

    def handle_push_device_register(self):
        user = self.require_user()
        if not user:
            return
        device = save_push_device(user["id"], self.read_json())
        self.send_json(200, {"ok": True, "configured": getui_configured(), "device": device})

    def handle_watch_alert_stream(self):
        user = self.require_user()
        if not user:
            return
        subscriber = subscribe_watch_alerts(user["id"])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    alert = subscriber.get(timeout=25)
                    payload = dumps(alert, ensure_ascii=False, default=json_default)
                    self.wfile.write(f"event: alert\ndata: {payload}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            unsubscribe_watch_alerts(user["id"], subscriber)

    def handle_watch_task_create(self):
        user = self.require_user()
        if not user:
            return

        cx = get_chaoxing_session(user["id"])
        if not cx:
            self.send_json(409, {"error": "请先登录学习通"})
            return

        payload = self.read_json()
        room = get_room(str(payload.get("room_id") or config.ROOMS[0]["room_id"]).strip())
        room_id = str(room["room_id"])
        fid_enc = str(room["fid_enc"])
        day = validate_day(str(payload.get("day", "")).strip())
        start_time, end_time = validate_time_range(
            str(payload.get("start_time", "")).strip(),
            str(payload.get("end_time", "")).strip(),
        )
        expires_at = watch_expires_at(day, start_time)
        if datetime.now() >= expires_at:
            raise ValueError("蹲座位截止时间已经过去")

        room_seat_config(room)
        interval_seconds = validate_watch_interval(payload.get("interval_seconds"))
        webhook_url = str(payload.get("webhook_url") or config.NOTIFY_WEBHOOK_URL or "").strip()
        if truthy(payload.get("save_webhook")):
            save_default_webhook_url(user["id"], webhook_url)
        webhook_url = webhook_url or None
        task_id = database.execute(
            """
            INSERT INTO seat_watch_tasks
                (user_id, room_id, fid_enc, day, start_time, end_time, target_seats_json,
                 ignore_no_power, ignore_sunny, webhook_url, interval_seconds,
                 status, next_check_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'running', NOW(), %s)
            """,
            (
                user["id"],
                room_id,
                fid_enc,
                day,
                start_time,
                end_time,
                "[]",
                1 if truthy(payload.get("ignore_no_power")) else 0,
                1 if truthy(payload.get("ignore_sunny")) else 0,
                webhook_url,
                interval_seconds,
                expires_at,
            ),
        )
        self.send_json(200, {"ok": True, "task_id": task_id, "tasks": fetch_watch_tasks(user["id"])})

    def handle_watch_task_cancel(self, path: str):
        user = self.require_user()
        if not user:
            return
        task_id = self.watch_task_id_from_path(path)
        cancel_watch_task(task_id, user["id"])
        self.send_json(200, {"ok": True, "tasks": fetch_watch_tasks(user["id"])})

    def handle_watch_alert_ack(self, path: str):
        user = self.require_user()
        if not user:
            return
        task_id = self.watch_alert_id_from_path(path)
        payload = self.read_json()
        action = str(payload.get("action") or "").strip()
        changed = acknowledge_watch_alert(user["id"], task_id, action)
        self.send_json(200, {"ok": True, "changed": changed, "alerts": fetch_watch_alerts(user["id"])})

    def handle_chat_messages(self):
        user = self.require_user()
        if not user:
            return
        self.send_json(200, {"messages": fetch_chat_messages(100)})

    def handle_chat_message_create(self):
        user = self.require_user()
        if not user:
            return
        payload = self.read_json()
        content = chat_message_text_from_payload(payload)
        message = save_chat_message(user["id"], content)
        self.send_json(200, {"ok": True, "message": message, "messages": fetch_chat_messages(100)})

    def history_id_from_path(self, path: str) -> int:
        parts = [part for part in path.split("/") if part]
        try:
            return int(parts[2])
        except (IndexError, ValueError) as exc:
            raise ValueError("查询记录 ID 不正确") from exc

    def watch_task_id_from_path(self, path: str) -> int:
        parts = [part for part in path.split("/") if part]
        try:
            return int(parts[2])
        except (IndexError, ValueError) as exc:
            raise ValueError("任务 ID 不正确") from exc

    def watch_alert_id_from_path(self, path: str) -> int:
        parts = [part for part in path.split("/") if part]
        try:
            return int(parts[2])
        except (IndexError, ValueError) as exc:
            raise ValueError("提醒 ID 不正确") from exc

    def admin_user_id_from_path(self, path: str) -> int:
        parts = [part for part in path.split("/") if part]
        try:
            return int(parts[3])
        except (IndexError, ValueError) as exc:
            raise ValueError("用户 ID 不正确") from exc

    def handle_chaoxing_login(self):
        payload = self.read_json()
        account = str(payload.get("account", "")).strip()
        password = str(payload.get("password", "")).strip()
        if not account:
            raise ValueError("请填写学习通账号")
        if not password:
            raise ValueError("请填写学习通密码")

        try:
            session = chaoxing.login(account, password)
        except chaoxing.ChaoxingAuthError as exc:
            self.send_json(401, {"error": str(exc)})
            return
        try:
            user = auth.get_or_create_external_user(account)
        except PermissionError as exc:
            self.send_json(403, {"error": str(exc)})
            return
        cookies_json = chaoxing.cookie_jar_to_json(session)
        save_chaoxing_session(user["id"], account, cookies_json)
        sync_chaoxing_profile(user["id"], cookies_json)
        self.send_json(
            200,
            {"ok": True, "account": account, "user": user},
            headers=[("Set-Cookie", auth.make_session_cookie(user["id"]))],
        )

    def handle_seat_query(self):
        user = self.require_user()
        if not user:
            return

        cx = get_chaoxing_session(user["id"])
        if not cx:
            self.send_json(409, {"error": "请先登录学习通"})
            return

        payload = self.read_json()
        room = get_room(str(payload.get("room_id") or config.ROOMS[0]["room_id"]).strip())
        room_id = str(room["room_id"])
        fid_enc = str(room["fid_enc"])
        day = validate_day(str(payload.get("day", "")).strip())
        start_time, end_time = validate_time_range(
            str(payload.get("start_time", "")).strip(),
            str(payload.get("end_time", "")).strip(),
        )
        room_seat_config(room)

        session = chaoxing.session_from_cookie_json(cx["cookies_json"])
        try:
            result = chaoxing.query_seats(session, room_id, fid_enc, day, start_time, end_time)
        except Exception as exc:
            mark_chaoxing_error(user["id"], str(exc))
            raise

        if not result.get("success"):
            mark_chaoxing_error(user["id"], str(result))
            self.send_json(502, {"error": "学习通接口返回失败", "raw": result})
            return

        response = build_seat_response(room, day, start_time, end_time, result, payload)
        update_chaoxing_cookies(user["id"], chaoxing.cookie_jar_to_json(session))
        history_id = save_query_history(
            user["id"],
            {
                "room_id": room_id,
                "fid_enc": fid_enc,
                "day": day,
                "start_time": start_time,
                "end_time": end_time,
                "available_count": response["summary"]["available"],
                "occupied_count": response["summary"]["occupied"],
                "pair_count": response["summary"]["pairs"],
                "result_json": dumps(response, ensure_ascii=False),
            },
        )
        response["history_id"] = history_id

        self.send_json(200, response)

    def handle_chaoxing_timetable(self):
        user = self.require_user()
        if not user:
            return

        cx = get_chaoxing_session(user["id"])
        if not cx:
            self.send_json(409, {"error": "请先登录学习通"})
            return

        payload = self.read_json()
        week_num = timetable_week_num_from_payload(payload)

        session = chaoxing.session_from_cookie_json(cx["cookies_json"])
        try:
            result = chaoxing.fetch_timetable_response(session, week_num=week_num)
        except Exception as exc:
            mark_chaoxing_error(user["id"], str(exc))
            raise

        update_chaoxing_cookies(user["id"], chaoxing.cookie_jar_to_json(session))
        self.send_json(200, {"ok": True, "timetable": result})

    def handle_chaoxing_transcript(self):
        user = self.current_user()
        payload = self.read_json()

        cookie_raw = str(payload.get("cookie") or "").strip()
        token = str(payload.get("token") or "").strip()
        student_no = str(payload.get("student_no") or "").strip()
        fid = str(payload.get("fid") or "").strip()
        mapp_id = str(payload.get("mapp_id") or "").strip()

        session = None
        cookies_json = ""
        user_id = user["id"] if user else None

        if not token and not cookie_raw:
            if not user:
                self.send_json(401, {"error": "请先登录或提供学习通凭证"})
                return
            cx = get_chaoxing_session(user["id"])
            if not cx or not cx.get("cookies_json"):
                self.send_json(409, {"error": "请先登录学习通"})
                return
            session = chaoxing.session_from_cookie_json(cx["cookies_json"])
            cookies_json = cx["cookies_json"]

        try:
            result = chaoxing.fetch_transcript_pdf_url(
                session=session,
                cookie_raw=cookie_raw,
                cookies_json=cookies_json,
                token=token,
                student_no=student_no,
                fid=fid,
                mapp_id=mapp_id,
            )
            if user_id and session:
                update_chaoxing_cookies(user_id, chaoxing.cookie_jar_to_json(session))
            if user_id and result.get("pdf_url"):
                save_academic_transcript(user_id, result["pdf_url"], student_info=result.get("student_info") or {}, max_keep=3)
                result["transcripts"] = fetch_recent_academic_transcripts(user_id, limit=3)
            self.send_json(200, result)
        except Exception as exc:
            if user_id:
                mark_chaoxing_error(user_id, str(exc))
            self.send_json(500, {"error": f"导出成绩单失败：{str(exc)}"})

    def handle_chaoxing_transcript_history(self):
        user = self.require_user()
        if not user:
            return
        transcripts = fetch_recent_academic_transcripts(user["id"], limit=3)
        self.send_json(200, {"ok": True, "transcripts": transcripts})


def main() -> None:
    config.ensure_secret()
    database.init_db()
    start_watch_worker()
    server = ThreadingHTTPServer((config.APP_HOST, config.APP_PORT), AppHandler)
    print(f"Search Seat 已启动：http://127.0.0.1:{config.APP_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
