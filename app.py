#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import JSONDecodeError, dumps, loads
import hmac
import queue
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse

import auth
import chaoxing
import config
import database
import requests


WATCH_STATUS_LABELS = {
    "running": "蹲座中",
    "matched": "已蹲到",
    "expired": "已到点",
    "cancelled": "已取消",
}
WATCH_WORKER_STARTED = False
WATCH_WORKER_LOCK = threading.Lock()
WATCH_ALERT_SUBSCRIBERS = {}
WATCH_ALERT_SUBSCRIBERS_LOCK = threading.Lock()


def json_default(value):
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def validate_day(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("日期必须是 YYYY-MM-DD 格式") from exc


def validate_time(value: str, field_name: str) -> str:
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise ValueError(f"{field_name} 必须是 HH:MM 格式") from exc
    return parsed.strftime("%H:%M")


def validate_time_range(start_time: str, end_time: str) -> tuple:
    start = validate_time(start_time, "开始时间")
    end = validate_time(end_time, "结束时间")
    if end <= start:
        raise ValueError("结束时间必须晚于开始时间")
    return start, end


def public_room(room: dict) -> dict:
    return {
        "label": room["label"],
        "room_id": room["room_id"],
    }


def get_room(room_id: str) -> dict:
    for room in config.ROOMS:
        if str(room.get("room_id")) == str(room_id):
            return room
    raise ValueError("请选择有效房间")


def room_label(room_id: str) -> str:
    for room in config.ROOMS:
        if str(room.get("room_id")) == str(room_id):
            return room.get("label") or str(room_id)
    return str(room_id)


def local_reserve_path(room_id: str, day: str, seat: str = "") -> str:
    query = {"room_id": room_id, "day": day}
    if seat:
        query["seat"] = seat
    return "/reserve?" + urlencode(query)


def official_seat_index_url(fid_enc: str = "") -> str:
    return "https://office.chaoxing.com/front/apps/seat/index?" + urlencode(
        {"fidEnc": fid_enc or config.FID_ENC}
    )


def seat_range(start: int, end: int, width: int) -> set:
    return {str(value).zfill(width) for value in range(start, end + 1)}


def filter_unwanted_seats(room_id: str, available: list, width: int, payload: dict) -> tuple:
    filtered = set()
    applied = []

    if payload.get("ignore_no_power") and room_id == "12818":
        seats = seat_range(168, 211, width)
        filtered.update(seats)
        applied.append({"name": "忽略无电源座位", "count": len(seats & set(available))})

    if payload.get("ignore_sunny"):
        sunny_rules = {
            "12818": (348, 371),
            "12819": (284, 299),
        }
        if room_id in sunny_rules:
            start, end = sunny_rules[room_id]
            seats = seat_range(start, end, width)
            filtered.update(seats)
            applied.append({"name": "忽略太阳晒座位", "count": len(seats & set(available))})

    return [seat for seat in available if seat not in filtered], sorted(filtered & set(available)), applied


def truthy(value) -> bool:
    return value in (True, 1, "1", "true", "on", "yes")


def room_seat_config(room: dict) -> tuple:
    seat_min = int(room.get("seat_min") or config.SEAT_MIN)
    seat_max = int(room.get("seat_max") or config.SEAT_MAX)
    seat_width = int(room.get("seat_width") or config.SEAT_WIDTH)
    if seat_min < 1 or seat_max < seat_min:
        raise ValueError("座位号范围不正确")
    if seat_width < 1 or seat_width > 8:
        raise ValueError("座位号补零宽度不正确")
    return seat_min, seat_max, seat_width


def watch_expires_at(day: str, start_time: str) -> datetime:
    return datetime.strptime(f"{day} {start_time}", "%Y-%m-%d %H:%M")


def validate_watch_interval(value) -> int:
    try:
        interval = int(value or config.WATCH_INTERVAL_SECONDS)
    except (TypeError, ValueError) as exc:
        raise ValueError("轮询间隔必须是数字") from exc
    if interval < 15:
        raise ValueError("轮询间隔不能小于 15 秒")
    if interval > 3600:
        raise ValueError("轮询间隔不能大于 3600 秒")
    return interval


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


def build_seat_response(room: dict, day: str, start_time: str, end_time: str, result: dict, payload: dict) -> dict:
    room_id = str(room["room_id"])
    seat_min, seat_max, seat_width = room_seat_config(room)
    all_seats = chaoxing.build_all_seats(seat_min, seat_max, seat_width)
    occupied = sorted(chaoxing.get_occupied_seats(result, seat_width))
    raw_available = chaoxing.get_available_seats(result, all_seats, seat_width)
    available, filtered_seats, applied_filters = filter_unwanted_seats(room_id, raw_available, seat_width, payload)
    pairs = chaoxing.find_adjacent_pairs(available, seat_width)
    return {
        "room_id": room_id,
        "room_name": room.get("label") or room_id,
        "day": day,
        "start_time": start_time,
        "end_time": end_time,
        "reserve_url": local_reserve_path(room_id, day),
        "available": [
            {"seat": seat, "url": local_reserve_path(room_id, day, seat)}
            for seat in available
        ],
        "occupied": occupied,
        "filtered": filtered_seats,
        "filters": applied_filters,
        "pairs": [
            {
                "seats": pair,
                "url": local_reserve_path(room_id, day, "-".join(pair)),
            }
            for pair in pairs
        ],
        "summary": {
            "total": len(all_seats),
            "available": len(available),
            "occupied": len(occupied),
            "filtered": len(filtered_seats),
            "pairs": len(pairs),
        },
    }


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
        "last_query_at": row.get("last_query_at") or "",
        "last_watch_at": row.get("last_watch_at") or "",
    }


def fetch_admin_user_rows(limit: int = 500) -> list:
    rows = database.fetch_all(
        """
        SELECT u.id, u.username, u.disabled_at, u.created_at, u.last_login_at,
               s.cx_account, s.session_valid, s.last_error, s.cookies_updated_at,
               s.last_login_at AS cx_last_login_at, s.cx_user_name,
               s.curriculum_synced_at, s.curriculum_sync_error,
               COALESCE(q.query_count, 0) AS query_count,
               q.last_query_at,
               COALESCE(w.watch_count, 0) AS watch_count,
               COALESCE(w.running_watch_count, 0) AS running_watch_count,
               w.last_watch_at
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
        ORDER BY u.id ASC
        LIMIT %s
        """,
        (limit,),
    )
    return [public_admin_user(row) for row in rows]


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
               w.last_watch_at
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


def save_query_history(user_id: int, payload: dict) -> None:
    return database.execute(
        """
        INSERT INTO seat_query_history
            (user_id, room_id, fid_enc, day, start_time, end_time,
             available_count, occupied_count, pair_count, result_json)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            user_id,
            payload["room_id"],
            payload["fid_enc"],
            payload["day"],
            payload["start_time"],
            payload["end_time"],
            payload["available_count"],
            payload["occupied_count"],
            payload["pair_count"],
            payload["result_json"],
        ),
    )


def public_watch_task(row: dict) -> dict:
    matched_seats = loads(row.get("matched_seats_json") or "[]") if row.get("matched_seats_json") else []
    reserve_url = local_reserve_path(row["room_id"], row["day"], matched_seats[0]) if matched_seats else ""
    return {
        "id": row["id"],
        "room_id": row["room_id"],
        "room_name": room_label(row["room_id"]),
        "day": row["day"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "ignore_no_power": bool(row["ignore_no_power"]),
        "ignore_sunny": bool(row["ignore_sunny"]),
        "interval_seconds": row["interval_seconds"],
        "status": row["status"],
        "status_label": WATCH_STATUS_LABELS.get(row["status"], row["status"]),
        "matched_seats": matched_seats,
        "reserve_url": reserve_url,
        "reminder_ack_at": row.get("reminder_ack_at") or "",
        "reminder_action": row.get("reminder_action") or "",
        "last_checked_at": row["last_checked_at"],
        "next_check_at": row["next_check_at"],
        "expires_at": row["expires_at"],
        "last_error": row["last_error"],
        "created_at": row["created_at"],
    }


def fetch_watch_tasks(user_id: int) -> list:
    rows = database.fetch_all(
        """
        SELECT id, user_id, room_id, fid_enc, day, start_time, end_time,
               target_seats_json, ignore_no_power, ignore_sunny, webhook_url,
               interval_seconds, status, matched_seats_json, last_checked_at,
               next_check_at, expires_at, last_error, created_at,
               reminder_ack_at, reminder_action
        FROM seat_watch_tasks
        WHERE user_id = %s
        ORDER BY id DESC
        LIMIT 50
        """,
        (user_id,),
    )
    return [public_watch_task(row) for row in rows]


def build_watch_alert(task: dict, matched_seats: list) -> dict:
    return {
        "id": task["id"],
        "room_id": task["room_id"],
        "room_name": room_label(task["room_id"]),
        "day": str(task["day"]),
        "start_time": task["start_time"],
        "end_time": task["end_time"],
        "status": "matched",
        "status_label": WATCH_STATUS_LABELS["matched"],
        "matched_seats": matched_seats,
        "reserve_url": local_reserve_path(str(task["room_id"]), str(task["day"]), matched_seats[0]) if matched_seats else "",
    }


def subscribe_watch_alerts(user_id: int):
    subscriber = queue.Queue(maxsize=20)
    with WATCH_ALERT_SUBSCRIBERS_LOCK:
        WATCH_ALERT_SUBSCRIBERS.setdefault(user_id, set()).add(subscriber)
    return subscriber


def unsubscribe_watch_alerts(user_id: int, subscriber) -> None:
    with WATCH_ALERT_SUBSCRIBERS_LOCK:
        subscribers = WATCH_ALERT_SUBSCRIBERS.get(user_id)
        if not subscribers:
            return
        subscribers.discard(subscriber)
        if not subscribers:
            WATCH_ALERT_SUBSCRIBERS.pop(user_id, None)


def publish_watch_alert(user_id: int, alert: dict) -> None:
    with WATCH_ALERT_SUBSCRIBERS_LOCK:
        subscribers = list(WATCH_ALERT_SUBSCRIBERS.get(user_id, ()))
    for subscriber in subscribers:
        try:
            subscriber.put_nowait(alert)
        except queue.Full:
            try:
                subscriber.get_nowait()
                subscriber.put_nowait(alert)
            except queue.Empty:
                pass


def fetch_watch_alerts(user_id: int) -> list:
    rows = database.fetch_all(
        """
        SELECT id, user_id, room_id, fid_enc, day, start_time, end_time,
               target_seats_json, ignore_no_power, ignore_sunny, webhook_url,
               interval_seconds, status, matched_seats_json, last_checked_at,
               next_check_at, expires_at, last_error, created_at,
               reminder_ack_at, reminder_action
        FROM seat_watch_tasks
        WHERE user_id = %s
          AND status = 'matched'
          AND reminder_ack_at IS NULL
        ORDER BY last_checked_at ASC, id ASC
        LIMIT 20
        """,
        (user_id,),
    )
    return [public_watch_task(row) for row in rows]


def acknowledge_watch_alert(user_id: int, task_id: int, action: str) -> int:
    if action not in {"reserve", "confirm"}:
        raise ValueError("提醒操作不正确")
    return database.execute(
        """
        UPDATE seat_watch_tasks
        SET reminder_ack_at = NOW(),
            reminder_action = %s,
            updated_at = NOW()
        WHERE id = %s
          AND user_id = %s
          AND status = 'matched'
          AND reminder_ack_at IS NULL
        """,
        (action, task_id, user_id),
    )


def send_watch_webhook(task: dict, matched_seats: list) -> None:
    webhook_url = (task.get("webhook_url") or "").strip()
    if not webhook_url:
        return

    first_seat = matched_seats[0] if matched_seats else ""
    reserve_url = chaoxing.build_reserve_url(
        str(task["room_id"]),
        str(task["fid_enc"]),
        str(task["day"]),
        first_seat,
    )
    shown_seats = matched_seats[:30]
    suffix = f" 等 {len(matched_seats)} 个" if len(matched_seats) > len(shown_seats) else ""
    message = (
        "找到可预约座位了\n"
        f"房间：{room_label(task['room_id'])}\n"
        f"日期：{task['day']}\n"
        f"时段：{task['start_time']}-{task['end_time']}\n"
        f"座位：{', '.join(shown_seats)}{suffix}\n"
        f"预约页：{reserve_url}"
    )
    parsed_url = urlparse(webhook_url)
    webhook_host = (parsed_url.hostname or "").lower()
    if webhook_host.endswith(("open.feishu.cn", "open.larksuite.com")):
        body = {
            "msg_type": "text",
            "content": {"text": message},
        }
    else:
        body = {
            "msgtype": "text",
            "text": {"content": message},
            "content": message,
            "data": {
                "task_id": task["id"],
                "room_name": room_label(task["room_id"]),
                "day": str(task["day"]),
                "start_time": task["start_time"],
                "end_time": task["end_time"],
                "matched_seats": matched_seats,
                "matched_count": len(matched_seats),
                "reserve_url": reserve_url,
            },
        }
    response = requests.post(webhook_url, json=body, timeout=10)
    response.raise_for_status()
    try:
        response_body = response.json()
    except ValueError:
        return
    if not isinstance(response_body, dict):
        return

    result_code = response_body.get("code")
    if result_code is None:
        result_code = response_body.get("errcode")
    if result_code in (None, 0):
        return

    result_message = response_body.get("msg") or response_body.get("errmsg") or response.text
    raise RuntimeError(f"Webhook 返回失败：{result_code} {result_message}")


def finish_watch_task(task_id: int, status: str, matched_seats=None, error: str = "") -> None:
    database.execute(
        """
        UPDATE seat_watch_tasks
        SET status = %s,
            matched_seats_json = %s,
            last_checked_at = NOW(),
            next_check_at = NULL,
            last_error = %s,
            updated_at = NOW()
        WHERE id = %s AND status = 'running'
        """,
        (
            status,
            dumps(matched_seats or [], ensure_ascii=False) if matched_seats else None,
            error[:2000] if error else None,
            task_id,
        ),
    )


def schedule_watch_retry(task_id: int, interval_seconds: int, error: str = "") -> None:
    next_check_at = datetime.now() + timedelta(seconds=interval_seconds)
    database.execute(
        """
        UPDATE seat_watch_tasks
        SET last_checked_at = NOW(),
            next_check_at = %s,
            last_error = %s,
            updated_at = NOW()
        WHERE id = %s AND status = 'running'
        """,
        (next_check_at, error[:2000] if error else None, task_id),
    )


def process_watch_task(task: dict) -> None:
    expires_at = task["expires_at"]
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    if datetime.now() >= expires_at:
        finish_watch_task(task["id"], "expired")
        return

    room = get_room(str(task["room_id"]))
    session = chaoxing.session_from_cookie_json(task["cookies_json"])
    result = chaoxing.query_seats(
        session,
        str(task["room_id"]),
        str(task["fid_enc"]),
        str(task["day"]),
        task["start_time"],
        task["end_time"],
    )
    if not result.get("success"):
        schedule_watch_retry(task["id"], int(task["interval_seconds"]), f"学习通接口返回失败：{result}")
        return

    payload = {
        "ignore_no_power": bool(task["ignore_no_power"]),
        "ignore_sunny": bool(task["ignore_sunny"]),
    }
    response = build_seat_response(room, str(task["day"]), task["start_time"], task["end_time"], result, payload)
    update_chaoxing_cookies(task["user_id"], chaoxing.cookie_jar_to_json(session))
    matched = [item["seat"] for item in response["available"]]
    if matched:
        finish_watch_task(task["id"], "matched", matched)
        publish_watch_alert(task["user_id"], build_watch_alert(task, matched))
        try:
            send_watch_webhook(task, matched)
        except Exception as exc:
            database.execute(
                "UPDATE seat_watch_tasks SET last_error = %s, updated_at = NOW() WHERE id = %s",
                (f"Webhook 发送失败：{str(exc)[:1800]}", task["id"]),
            )
        return

    schedule_watch_retry(task["id"], int(task["interval_seconds"]))


def run_due_watch_tasks() -> None:
    if not WATCH_WORKER_LOCK.acquire(blocking=False):
        return
    try:
        rows = database.fetch_all(
            """
            SELECT t.id, t.user_id, t.room_id, t.fid_enc, t.day, t.start_time, t.end_time,
                   t.ignore_no_power, t.ignore_sunny, t.webhook_url,
                   t.interval_seconds, t.expires_at, s.cookies_json
            FROM seat_watch_tasks t
            JOIN chaoxing_sessions s ON s.user_id = t.user_id
            WHERE t.status = 'running'
              AND (t.next_check_at IS NULL OR t.next_check_at <= NOW())
            ORDER BY t.next_check_at ASC, t.id ASC
            LIMIT 10
            """,
        )
        for task in rows:
            try:
                process_watch_task(task)
            except Exception as exc:
                schedule_watch_retry(task["id"], int(task.get("interval_seconds") or config.WATCH_INTERVAL_SECONDS), str(exc))
    finally:
        WATCH_WORKER_LOCK.release()


def watch_worker_loop() -> None:
    while True:
        try:
            run_due_watch_tasks()
        except Exception:
            pass
        time.sleep(10)


def start_watch_worker() -> None:
    global WATCH_WORKER_STARTED
    if WATCH_WORKER_STARTED:
        return
    WATCH_WORKER_STARTED = True
    threading.Thread(target=watch_worker_loop, name="seat-watch-worker", daemon=True).start()


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
        if path == "/":
            self.send_body(200, INDEX_HTML)
            return
        if path == "/admin":
            self.send_body(200, ADMIN_HTML)
            return
        if path == "/reserve":
            self.handle_reserve_redirect(parsed.query)
            return
        if path == "/api/admin/me":
            self.handle_admin_me()
            return
        if path == "/api/admin/users":
            self.handle_admin_users()
            return
        if path.startswith("/api/admin/users/") and path.endswith("/cookie"):
            self.handle_admin_user_cookie(path)
            return
        if path.startswith("/api/admin/users/"):
            self.handle_admin_user_detail(parsed)
            return
        if path == "/api/me":
            self.handle_me()
            return
        if path == "/api/history":
            self.handle_history()
            return
        if path == "/api/watch-tasks":
            self.handle_watch_tasks()
            return
        if path == "/api/watch-alerts":
            self.handle_watch_alerts()
            return
        if path == "/api/watch-alerts/stream":
            self.handle_watch_alert_stream()
            return
        if path.startswith("/api/history/"):
            self.handle_history_detail(path)
            return
        self.send_json(404, {"error": "not found"})

    def handle_reserve_redirect(self, query: str):
        params = parse_qs(query)
        room = get_room((params.get("room_id") or [config.ROOMS[0]["room_id"]])[0])
        day = validate_day((params.get("day") or [datetime.now().strftime("%Y-%m-%d")])[0])
        seat = (params.get("seat") or [""])[0]
        target = chaoxing.build_reserve_url(str(room["room_id"]), str(room["fid_enc"]), day, seat)
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/logout":
                self.discard_request_body()
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
                self.handle_admin_sync_missing_profiles()
                return
            if path.startswith("/api/admin/users/") and path.endswith("/disable"):
                self.handle_admin_user_disabled(path, True)
                return
            if path.startswith("/api/admin/users/") and path.endswith("/enable"):
                self.handle_admin_user_disabled(path, False)
                return
            if path.startswith("/api/admin/users/") and path.endswith("/sync-profile"):
                self.handle_admin_user_sync(path)
                return
            if path == "/api/chaoxing/login":
                self.handle_chaoxing_login()
                return
            if path == "/api/seats/query":
                self.handle_seat_query()
                return
            if path == "/api/history/delete":
                self.handle_history_delete()
                return
            if path.startswith("/api/history/") and path.endswith("/delete"):
                self.handle_history_delete(path)
                return
            if path == "/api/watch-tasks":
                self.handle_watch_task_create()
                return
            if path.startswith("/api/watch-tasks/") and path.endswith("/cancel"):
                self.handle_watch_task_cancel(path)
                return
            if path.startswith("/api/watch-alerts/") and path.endswith("/ack"):
                self.handle_watch_alert_ack(path)
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

    def handle_me(self):
        user = self.current_user()
        if not user:
            self.send_json(200, {"user": None})
            return

        cx = get_chaoxing_session(user["id"])
        settings = get_user_settings(user["id"])
        self.send_json(
            200,
            {
                "user": user,
                "chaoxing": {
                    "bound": bool(cx),
                    "account": cx["cx_account"] if cx else "",
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
        database.execute(
            """
            UPDATE seat_watch_tasks
            SET status = 'cancelled', next_check_at = NULL, updated_at = NOW()
            WHERE id = %s AND user_id = %s AND status = 'running'
            """,
            (task_id, user["id"]),
        )
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
            raise ValueError("蹲座位任务 ID 不正确") from exc

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

        session = chaoxing.login(account, password)
        user = auth.get_or_create_external_user(account)
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


ADMIN_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>座位雷达管理后台</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #eef3ef;
      --panel: #ffffff;
      --ink: #15211c;
      --muted: #65746d;
      --line: #d6e0da;
      --soft: #f6faf7;
      --green: #1f5b4e;
      --green-dark: #143c35;
      --amber: #b77822;
      --red: #b84a38;
      --blue: #315f86;
      --mono: "SFMono-Regular", "Cascadia Mono", "Menlo", monospace;
      --body: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      font-family: var(--body);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        linear-gradient(90deg, rgba(31, 91, 78, .07) 1px, transparent 1px) 0 0 / 42px 42px,
        linear-gradient(180deg, rgba(31, 91, 78, .06) 1px, transparent 1px) 0 0 / 42px 42px,
        var(--bg);
      color: var(--ink);
    }
    header {
      min-height: 64px;
      padding: 0 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      background: rgba(255, 255, 255, .9);
      border-bottom: 1px solid var(--line);
      position: sticky;
      top: 0;
      z-index: 5;
      backdrop-filter: blur(14px);
    }
    h1 { margin: 0; color: var(--green-dark); font-size: 19px; letter-spacing: 0; }
    h2 { margin: 0 0 12px; color: var(--green-dark); font-size: 16px; }
    main { width: min(1380px, calc(100vw - 28px)); margin: 0 auto; padding: 20px 0 34px; }
    section {
      background: rgba(255, 255, 255, .94);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      margin-bottom: 14px;
      box-shadow: 0 12px 28px rgba(21, 33, 28, .05);
    }
    input {
      width: 100%;
      min-height: 42px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      font-size: 15px;
      background: #fbfdfb;
      color: var(--ink);
      outline: none;
    }
    input:focus { border-color: var(--green); box-shadow: 0 0 0 3px rgba(31, 91, 78, .14); }
    label { display: grid; gap: 7px; color: var(--muted); font-size: 13px; font-weight: 700; }
    button {
      border: 0;
      border-radius: 6px;
      min-height: 38px;
      padding: 9px 13px;
      background: var(--green);
      color: #fff;
      font-weight: 800;
      cursor: pointer;
    }
    button:hover { background: var(--green-dark); }
    button.secondary { background: #334944; }
    button.ghost { background: #fff; color: var(--green-dark); border: 1px solid var(--line); }
    button.danger { background: var(--red); }
    button:disabled { opacity: .6; cursor: not-allowed; }
    .hidden { display: none !important; }
    .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .top-user { justify-content: flex-end; }
    .muted { color: var(--muted); font-size: 13px; }
    .bad { color: var(--red); }
    .ok { color: #12764f; }
    .message { min-height: 20px; margin-top: 10px; font-size: 13px; font-weight: 700; }
    .auth {
      width: min(460px, 100%);
      margin: 52px auto;
      border-top: 5px solid var(--green);
    }
    .auth form { display: grid; gap: 12px; }
    .toolbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 12px; }
    .stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
    .stat {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--soft);
      padding: 12px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
    }
    .stat b { display: block; margin-top: 5px; color: var(--ink); font: 900 24px/1 var(--mono); }
    .table-wrap { overflow: auto; border: 1px solid var(--line); border-radius: 8px; background: #fff; }
    table { width: 100%; border-collapse: collapse; min-width: 920px; }
    th, td { padding: 10px 11px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; font-size: 13px; }
    th { background: var(--soft); color: #4e6058; font-size: 12px; white-space: nowrap; }
    tr:hover td { background: #fbfdfb; }
    .pill {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 4px 9px;
      background: #edf2ef;
      color: #53645c;
      font-size: 12px;
      font-weight: 800;
      white-space: nowrap;
    }
    .pill.ok { background: #dff4ea; color: #0a5a3b; }
    .pill.warn { background: #fff3d6; color: #7b4d0a; }
    .pill.bad { background: #f8e3df; color: var(--red); }
    .actions { display: inline-flex; gap: 6px; align-items: center; white-space: nowrap; }
    .actions button { min-height: 32px; padding: 6px 9px; font-size: 12px; }
    .pager { display: flex; justify-content: flex-end; gap: 8px; align-items: center; margin-top: 10px; }
    .pager button { min-height: 32px; padding: 6px 10px; font-size: 12px; }
    .detail-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
    .field { background: var(--soft); border: 1px solid var(--line); border-radius: 8px; padding: 10px; min-width: 0; }
    .field span { display: block; color: var(--muted); font-size: 12px; margin-bottom: 4px; }
    .field b { display: block; color: var(--ink); font-size: 14px; overflow-wrap: anywhere; }
    .split { display: grid; grid-template-columns: 1fr; gap: 12px; }
    @media (max-width: 780px) {
      header { padding: 10px 14px; align-items: flex-start; }
      main { width: calc(100vw - 16px); padding-top: 12px; }
      section { padding: 14px; }
      .toolbar { align-items: stretch; flex-direction: column; }
      .toolbar .row { justify-content: flex-start; }
      .stats, .detail-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .top-user { justify-content: flex-start; }
    }
  </style>
</head>
<body>
  <header>
    <h1>座位雷达管理后台</h1>
    <div id="topUser" class="row top-user hidden">
      <span class="muted" id="adminName"></span>
      <button class="ghost" id="logoutBtn" type="button">退出</button>
    </div>
  </header>

  <main>
    <section id="loginPanel" class="auth">
      <h2>管理账号登录</h2>
      <form id="loginForm">
        <label>账号
          <input name="username" autocomplete="username" required>
        </label>
        <label>密码
          <input name="password" type="password" autocomplete="current-password" required>
        </label>
        <button type="submit">登录</button>
      </form>
      <div id="loginMessage" class="message"></div>
    </section>

    <div id="adminPanel" class="hidden">
      <section>
        <div class="toolbar">
          <div>
            <h2>用户一览</h2>
            <div class="muted">姓名从数据库读取，只有点击同步按钮才会请求学习通补全。</div>
          </div>
          <div class="row">
            <button class="ghost" id="refreshBtn" type="button">刷新</button>
            <button id="syncMissingBtn" type="button">补全缺失姓名</button>
          </div>
        </div>
        <div class="stats">
          <div class="stat">用户 <b id="statUsers">0</b></div>
          <div class="stat">查询记录 <b id="statQueries">0</b></div>
          <div class="stat">蹲座任务 <b id="statWatch">0</b></div>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>ID</th>
                <th>姓名</th>
                <th>用户（手机号、登录时间）</th>
                <th>Cookie</th>
                <th>查询</th>
                <th>蹲座</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody id="userRows"></tbody>
          </table>
        </div>
        <div id="usersMessage" class="message"></div>
      </section>

      <section id="detailPanel" class="hidden">
        <div class="toolbar">
          <div>
            <h2 id="detailTitle">用户详情</h2>
            <div id="detailSub" class="muted"></div>
          </div>
          <div class="row">
            <button class="ghost" id="detailSyncBtn" type="button">同步姓名</button>
            <button class="ghost" id="closeDetailBtn" type="button">关闭</button>
          </div>
        </div>
        <div class="detail-grid" id="detailFields"></div>
        <div class="split">
          <div>
            <h2>最近查询</h2>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>日期</th>
                    <th>时段</th>
                    <th>房间</th>
                    <th>可预约</th>
                    <th>已占用</th>
                    <th>连排</th>
                  </tr>
                </thead>
                <tbody id="queryRows"></tbody>
              </table>
            </div>
            <div class="pager" id="queryPager"></div>
          </div>
          <div>
            <h2>最近蹲座</h2>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>创建时间</th>
                    <th>日期</th>
                    <th>时段</th>
                    <th>房间</th>
                    <th>状态</th>
                    <th>命中座位</th>
                  </tr>
                </thead>
                <tbody id="watchRowsAdmin"></tbody>
              </table>
            </div>
            <div class="pager" id="watchPager"></div>
          </div>
        </div>
      </section>
    </div>
  </main>

<script>
const loginPanel = document.querySelector('#loginPanel');
const adminPanel = document.querySelector('#adminPanel');
const topUser = document.querySelector('#topUser');
const adminName = document.querySelector('#adminName');
const loginForm = document.querySelector('#loginForm');
const loginMessage = document.querySelector('#loginMessage');
const usersMessage = document.querySelector('#usersMessage');
const userRows = document.querySelector('#userRows');
const detailPanel = document.querySelector('#detailPanel');
const detailTitle = document.querySelector('#detailTitle');
const detailSub = document.querySelector('#detailSub');
const detailFields = document.querySelector('#detailFields');
const queryRows = document.querySelector('#queryRows');
const watchRowsAdmin = document.querySelector('#watchRowsAdmin');
const queryPager = document.querySelector('#queryPager');
const watchPager = document.querySelector('#watchPager');
const detailSyncBtn = document.querySelector('#detailSyncBtn');
let selectedUserId = null;
let users = [];
let selectedQueryPage = 1;
let selectedWatchPage = 1;

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function setMessage(node, text, ok = false) {
  node.textContent = text || '';
  node.className = `message ${ok ? 'ok' : text ? 'bad' : ''}`;
}

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

function showLogin() {
  loginPanel.classList.remove('hidden');
  adminPanel.classList.add('hidden');
  topUser.classList.add('hidden');
}

function showAdmin(admin) {
  loginPanel.classList.add('hidden');
  adminPanel.classList.remove('hidden');
  topUser.classList.remove('hidden');
  adminName.textContent = admin.username;
}

function renderSummary(summary) {
  document.querySelector('#statUsers').textContent = summary.users || 0;
  document.querySelector('#statQueries').textContent = summary.queries || 0;
  document.querySelector('#statWatch').textContent = summary.watch_tasks || 0;
}

function renderUsers(data) {
  users = data.users || [];
  renderSummary(data.summary || {});
  userRows.innerHTML = users.map(user => `<tr>
    <td>${user.id}</td>
    <td>
      <b>${escapeHtml(user.cx_user_name || '-')}</b>
      ${user.disabled ? '<div><span class="pill bad">已禁用</span></div>' : ''}
    </td>
    <td>
      <b>${escapeHtml(user.username)}</b>
      <div class="muted">登录：${escapeHtml(user.last_login_at || '-')}</div>
    </td>
    <td>
      ${cookieStatus(user)}
      <div class="actions"><button class="ghost" type="button" ${user.cx_account ? '' : 'disabled'} onclick="copyUserCookie(${user.id})">复制</button></div>
    </td>
    <td>${user.query_count}<div class="muted">${escapeHtml(user.last_query_at || '-')}</div></td>
    <td>${watchStatus(user)}</td>
    <td><span class="actions">
      <button class="ghost" type="button" onclick="viewUser(${user.id})">查看</button>
      <button type="button" onclick="syncUser(${user.id})">同步</button>
      <button id="toggleUser${user.id}" class="${user.disabled ? 'ghost' : 'danger'}" type="button" onclick="toggleUserDisabled(${user.id}, ${user.disabled ? 'false' : 'true'})">${user.disabled ? '启用' : '禁用'}</button>
    </span></td>
  </tr>`).join('') || '<tr><td colspan="7">暂无用户</td></tr>';
}

function cookieStatus(user) {
  if (!user.cx_account) return '<span class="pill warn">未绑定</span>';
  if (user.session_valid) return '<span class="pill ok">正常</span>';
  return '<span class="pill bad">异常</span>';
}

function watchStatus(user) {
  const running = Number(user.running_watch_count || 0);
  const status = running > 0
    ? `<span class="pill warn">蹲座中 ${running}</span>`
    : '<span class="pill">无进行中</span>';
  return `${user.watch_count}${status}<div class="muted">${escapeHtml(user.last_watch_at || '-')}</div>`;
}

async function loadMe() {
  const data = await api('/api/admin/me', { method: 'GET', headers: {} });
  if (!data.admin) {
    showLogin();
    return;
  }
  showAdmin(data.admin);
  await loadUsers();
}

async function loadUsers() {
  const data = await api('/api/admin/users', { method: 'GET', headers: {} });
  renderUsers(data);
}

async function refreshAdminDataForUser(id) {
  await loadUsers();
  if (selectedUserId === id) {
    await viewUser(id, selectedQueryPage, selectedWatchPage);
  }
}

function renderDetailFields(user) {
  const fields = [
    ['用户 ID', user.id],
    ['用户（手机号）', user.username],
    ['状态', user.disabled ? '已禁用' : '正常'],
    ['学习通账号', user.cx_account || '-'],
    ['学习通姓名', user.cx_user_name || '-'],
    ['查询次数', user.query_count],
    ['蹲座次数', user.watch_count]
  ];
  detailFields.innerHTML = fields.map(([label, value]) =>
    `<div class="field"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>`
  ).join('');
}

function renderPager(target, pagination, kind) {
  const page = pagination.page || 1;
  const totalPages = pagination.total_pages || 1;
  const total = pagination.total || 0;
  target.innerHTML = `
    <span class="muted">第 ${page} / ${totalPages} 页，共 ${total} 条</span>
    <button class="ghost" type="button" ${page <= 1 ? 'disabled' : ''} onclick="changeDetailPage('${kind}', ${page - 1})">上一页</button>
    <button class="ghost" type="button" ${page >= totalPages ? 'disabled' : ''} onclick="changeDetailPage('${kind}', ${page + 1})">下一页</button>
  `;
}

function renderQueryRows(rows, pagination) {
  queryRows.innerHTML = rows.map(row => `<tr>
    <td>${escapeHtml(row.created_at)}</td>
    <td>${escapeHtml(row.day)}</td>
    <td>${escapeHtml(row.start_time)}-${escapeHtml(row.end_time)}</td>
    <td>${escapeHtml(row.room_name || row.room_id)}</td>
    <td>${row.available_count}</td>
    <td>${row.occupied_count}</td>
    <td>${row.pair_count}</td>
  </tr>`).join('') || '<tr><td colspan="7">暂无查询记录</td></tr>';
  renderPager(queryPager, pagination || {}, 'query');
}

function renderWatchRows(rows, pagination) {
  watchRowsAdmin.innerHTML = rows.map(row => {
    const matched = row.matched_seats || [];
    const matchedText = matched.length > 12 ? `${matched.slice(0, 12).join(', ')} 等 ${matched.length} 个` : matched.join(', ');
    const statusClass = row.status === 'matched' ? 'ok' : row.status === 'running' ? 'warn' : '';
    return `<tr>
      <td>${escapeHtml(row.created_at)}</td>
      <td>${escapeHtml(row.day)}</td>
      <td>${escapeHtml(row.start_time)}-${escapeHtml(row.end_time)}</td>
      <td>${escapeHtml(row.room_name || row.room_id)}</td>
      <td><span class="pill ${statusClass}">${escapeHtml(row.status_label || row.status)}</span></td>
      <td>${escapeHtml(matchedText || '-')}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="6">暂无蹲座记录</td></tr>';
  renderPager(watchPager, pagination || {}, 'watch');
}

async function viewUser(id, queryPage = 1, watchPage = 1) {
  selectedUserId = id;
  selectedQueryPage = queryPage;
  selectedWatchPage = watchPage;
  setMessage(usersMessage, '');
  const data = await api(`/api/admin/users/${id}?query_page=${queryPage}&watch_page=${watchPage}`, { method: 'GET', headers: {} });
  detailTitle.textContent = `用户详情：${data.user.cx_user_name || data.user.username}`;
  detailSub.textContent = `查询 ${data.user.query_count} 次，蹲座 ${data.user.watch_count} 次`;
  renderDetailFields(data.user);
  renderQueryRows(data.query_history || [], data.query_pagination);
  renderWatchRows(data.watch_history || [], data.watch_pagination);
  detailPanel.classList.remove('hidden');
  detailPanel.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function changeDetailPage(kind, page) {
  if (!selectedUserId) return;
  const queryPage = kind === 'query' ? page : selectedQueryPage;
  const watchPage = kind === 'watch' ? page : selectedWatchPage;
  viewUser(selectedUserId, queryPage, watchPage);
}

async function syncUser(id) {
  setMessage(usersMessage, '正在同步姓名...');
  const data = await api(`/api/admin/users/${id}/sync-profile`, { method: 'POST', body: '{}' });
  await loadUsers();
  if (selectedUserId === id) await viewUser(id, selectedQueryPage, selectedWatchPage);
  if (data.sync && data.sync.ok) {
    setMessage(usersMessage, `已同步：${data.sync.user_name}`, true);
  } else {
    setMessage(usersMessage, data.sync && data.sync.error ? data.sync.error : '同步失败');
  }
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.left = '-9999px';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  document.execCommand('copy');
  textarea.remove();
}

async function copyUserCookie(id) {
  setMessage(usersMessage, '正在读取 Cookie...');
  try {
    const data = await api(`/api/admin/users/${id}/cookie`, { method: 'GET', headers: {} });
    if (!data.cookie) throw new Error('这个用户没有可复制的 Cookie');
    await copyText(data.cookie);
    setMessage(usersMessage, data.session_valid ? 'Cookie 已复制' : 'Cookie 已复制，但状态异常', true);
  } catch (error) {
    setMessage(usersMessage, error.message);
  }
}

async function toggleUserDisabled(id, disabled) {
  const action = disabled ? '禁用' : '启用';
  if (!window.confirm(`确定${action}这个用户吗？`)) return;
  const button = document.querySelector(`#toggleUser${id}`);
  if (button) button.disabled = true;
  setMessage(usersMessage, `正在${action}...`);
  try {
    await api(`/api/admin/users/${id}/${disabled ? 'disable' : 'enable'}`, { method: 'POST', body: '{}' });
    await refreshAdminDataForUser(id);
    setMessage(usersMessage, `已${action}`, true);
  } catch (error) {
    setMessage(usersMessage, error.message);
    if (button) button.disabled = false;
  }
}

loginForm.addEventListener('submit', async event => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(loginForm).entries());
  setMessage(loginMessage, '正在登录...');
  try {
    const data = await api('/api/admin/login', {
      method: 'POST',
      body: JSON.stringify(payload)
    });
    loginForm.password.value = '';
    setMessage(loginMessage, '', true);
    showAdmin(data.admin);
    await loadUsers();
  } catch (error) {
    setMessage(loginMessage, error.message);
  }
});

document.querySelector('#logoutBtn').addEventListener('click', async () => {
  await api('/api/admin/logout', { method: 'POST', body: '{}' });
  location.reload();
});

document.querySelector('#refreshBtn').addEventListener('click', async () => {
  try {
    await loadUsers();
    setMessage(usersMessage, '已刷新', true);
  } catch (error) {
    setMessage(usersMessage, error.message);
  }
});

document.querySelector('#syncMissingBtn').addEventListener('click', async () => {
  if (!window.confirm('本次最多补全 20 个缺失姓名的用户，继续吗？')) return;
  setMessage(usersMessage, '正在补全缺失姓名...');
  try {
    const data = await api('/api/admin/users/sync-missing-profiles', { method: 'POST', body: '{}' });
    renderUsers(data);
    setMessage(usersMessage, `已处理 ${data.processed} 个用户`, true);
  } catch (error) {
    setMessage(usersMessage, error.message);
  }
});

document.querySelector('#closeDetailBtn').addEventListener('click', () => {
  selectedUserId = null;
  detailPanel.classList.add('hidden');
});

detailSyncBtn.addEventListener('click', () => {
  if (selectedUserId) syncUser(selectedUserId);
});

loadMe().catch(() => showLogin());
</script>
</body>
</html>
"""


INDEX_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>工职大座位雷达</title>
  <style>
    :root {
      color-scheme: light;
      --paper: #f5f7f2;
      --panel: #ffffff;
      --ink: #17201c;
      --muted: #68746d;
      --line: #cbd8d0;
      --soft-line: #e4ebe5;
      --shelf: #1f5b4e;
      --shelf-dark: #143c35;
      --available: #128d61;
      --available-soft: #dff4ea;
      --lamp: #d9952f;
      --danger: #b84a38;
      --shadow: 0 16px 42px rgba(22, 40, 34, .11);
      --display-font: "Avenir Next", "PingFang SC", "Noto Sans CJK SC", sans-serif;
      --body-font: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      --mono-font: "SFMono-Regular", "Cascadia Mono", "Menlo", monospace;
      font-family: var(--body-font);
    }
    * { box-sizing: border-box; }
    html { width: 100%; overflow-x: hidden; }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        linear-gradient(90deg, rgba(203, 216, 208, .28) 1px, transparent 1px) 0 0 / 36px 36px,
        linear-gradient(180deg, rgba(203, 216, 208, .22) 1px, transparent 1px) 0 0 / 36px 36px,
        linear-gradient(180deg, #f8faf6 0%, var(--paper) 54%, #eef3ec 100%);
      color: var(--ink);
      overflow-x: hidden;
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background:
        linear-gradient(180deg, rgba(31, 91, 78, .08), transparent 220px),
        linear-gradient(90deg, rgba(217, 149, 47, .08), transparent 42%);
      mix-blend-mode: multiply;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 20;
      min-height: 68px;
      background: rgba(245, 247, 242, .9);
      border-bottom: 1px solid rgba(203, 216, 208, .95);
      backdrop-filter: blur(18px);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 0 26px;
      padding-left: max(26px, env(safe-area-inset-left));
      padding-right: max(26px, env(safe-area-inset-right));
    }
    h1 {
      margin: 0;
      display: flex;
      align-items: center;
      gap: 10px;
      font-family: var(--display-font);
      font-size: clamp(16px, 1.7vw, 20px);
      font-weight: 800;
      letter-spacing: 0;
      color: var(--shelf-dark);
    }
    h1::before {
      content: "SEAT";
      width: 40px;
      height: 30px;
      display: inline-grid;
      place-items: center;
      border-radius: 6px;
      background: var(--shelf);
      color: #f8fbf5;
      font: 800 10px/1 var(--mono-font);
      box-shadow: inset 0 -3px 0 rgba(0, 0, 0, .16);
    }
    main {
      position: relative;
      width: min(1420px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 24px 0;
      padding-bottom: max(24px, env(safe-area-inset-bottom));
    }
    section {
      background: rgba(255, 255, 255, .92);
      border: 1px solid var(--soft-line);
      border-radius: 8px;
      padding: 20px;
      margin-bottom: 16px;
      box-shadow: 0 1px 0 rgba(255, 255, 255, .8), 0 12px 30px rgba(22, 40, 34, .05);
    }
    h2 {
      margin: 0 0 16px;
      font-family: var(--display-font);
      font-size: 17px;
      font-weight: 800;
      letter-spacing: 0;
      color: var(--shelf-dark);
    }
    form { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; align-items: end; }
    label { display: grid; gap: 7px; color: #526059; font-size: 13px; font-weight: 650; }
    input, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px 12px;
      font-size: 16px;
      color: var(--ink);
      background: #fbfdf9;
      min-height: 42px;
      outline: none;
      transition: border-color .16s ease, box-shadow .16s ease, background .16s ease;
    }
    input:hover, select:hover { border-color: #96aa9d; }
    input:focus, select:focus {
      border-color: var(--shelf);
      background: #fff;
      box-shadow: 0 0 0 3px rgba(31, 91, 78, .14);
    }
    input[type="checkbox"] { accent-color: var(--shelf); }
    button, .button-link {
      border: 0;
      border-radius: 6px;
      background: var(--shelf);
      color: #fff;
      padding: 10px 15px;
      font-size: 14px;
      font-weight: 750;
      cursor: pointer;
      text-decoration: none;
      text-align: center;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 42px;
      box-shadow: 0 10px 20px rgba(31, 91, 78, .15);
      transition: transform .16s ease, box-shadow .16s ease, background .16s ease, border-color .16s ease;
    }
    button:hover, .button-link:hover { background: var(--shelf-dark); transform: translateY(-1px); box-shadow: 0 14px 26px rgba(31, 91, 78, .2); }
    button.secondary { background: #314641; }
    button.ghost { background: transparent; color: var(--shelf-dark); border: 1px solid var(--line); box-shadow: none; }
    button.ghost:hover { background: #eff5ef; }
    button:disabled { opacity: .55; cursor: not-allowed; transform: none; box-shadow: none; }
    button:focus-visible, .button-link:focus-visible, a:focus-visible {
      outline: 3px solid rgba(217, 149, 47, .45);
      outline-offset: 3px;
    }
    .auth {
      max-width: 560px;
      margin: 58px auto;
      border-top: 5px solid var(--shelf);
      box-shadow: var(--shadow);
    }
    .auth form { grid-template-columns: 1fr; }
    .auth .row button { width: 100%; }
    .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; min-width: 0; }
    .muted { color: var(--muted); font-size: 13px; }
    .ok { color: var(--available); }
    .bad { color: var(--danger); }
    .disclaimer {
      border: 1px solid #d8e2da;
      border-radius: 8px;
      background: #f7faf5;
      padding: 14px;
      margin: 12px 0 16px;
    }
    .disclaimer h3 {
      margin: 0 0 10px;
      color: var(--shelf-dark);
      font-size: 13px;
      font-weight: 850;
    }
    .disclaimer-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
    .disclaimer-item { border-left: 3px solid var(--line); background: #fff; padding: 10px 12px; min-width: 0; }
    .disclaimer-item b { display: block; color: var(--shelf); font-size: 13px; margin-bottom: 5px; }
    .disclaimer-item span { display: block; color: #526059; font-size: 12px; line-height: 1.55; }
    .disclaimer a { color: var(--shelf); font-weight: 800; text-decoration: none; }
    .disclaimer a:hover { text-decoration: underline; }
    .auth .disclaimer-grid { grid-template-columns: 1fr; }
    .disclaimer.compact { margin-top: 0; }
    .wide { grid-column: span 2; }
    #queryForm label.row.wide { min-height: 32px; gap: 8px; }
    #queryForm label.row.wide input[type="checkbox"] {
      width: 16px !important;
      height: 16px;
      min-height: 0;
      padding: 0;
      margin: 0;
      flex: 0 0 auto;
    }
    .full { grid-column: 1 / -1; }
    .hidden { display: none !important; }
    .statusbar { display: grid; grid-template-columns: 1fr auto; gap: 16px; align-items: center; }
    .statusbar h2 { margin-bottom: 6px; }
    .stats { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-bottom: 16px; }
    .stat {
      position: relative;
      overflow: hidden;
      border: 1px solid var(--soft-line);
      border-radius: 8px;
      padding: 13px 14px;
      background: #fbfdf9;
      color: #5d6a63;
      font-size: 12px;
      font-weight: 760;
    }
    .stat::before {
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 4px;
      background: var(--line);
    }
    .stat:nth-child(2)::before, .stat:nth-child(5)::before { background: var(--available); }
    .stat:nth-child(3)::before { background: #6f7b74; }
    .stat:nth-child(4)::before { background: var(--lamp); }
    .stat b {
      display: block;
      margin-top: 4px;
      color: var(--ink);
      font: 850 26px/1.05 var(--mono-font);
      letter-spacing: 0;
    }
    .seat-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(68px, 1fr));
      gap: 8px;
      align-items: stretch;
      padding: 12px;
      border: 1px solid #dce6df;
      border-radius: 8px;
      background:
        linear-gradient(90deg, rgba(203, 216, 208, .38) 1px, transparent 1px) 0 0 / 24px 24px,
        linear-gradient(180deg, rgba(203, 216, 208, .28) 1px, transparent 1px) 0 0 / 24px 24px,
        #fbfdf9;
    }
    .pair-grid { grid-template-columns: repeat(auto-fill, minmax(126px, 1fr)); }
    .seat, .pair {
      border: 1px solid rgba(18, 141, 97, .35);
      border-radius: 6px;
      background: linear-gradient(180deg, #f5fff9 0%, var(--available-soft) 100%);
      color: #0b4d35;
      padding: 9px 8px;
      text-align: center;
      text-decoration: none;
      font: 800 13px/1.25 var(--mono-font);
      letter-spacing: 0;
      min-width: 0;
      min-height: 38px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      box-shadow: inset 0 -2px 0 rgba(18, 141, 97, .12);
    }
    .seat:hover, .pair:hover {
      border-color: var(--available);
      color: #063925;
      background: #ffffff;
      transform: translateY(-1px);
      box-shadow: 0 10px 20px rgba(18, 141, 97, .14);
    }
    .seat, .pair { transition: transform .16s ease, border-color .16s ease, box-shadow .16s ease, background .16s ease; }
    table { width: 100%; border-collapse: separate; border-spacing: 0; overflow: hidden; }
    th, td { border-bottom: 1px solid var(--soft-line); padding: 11px 10px; font-size: 13px; text-align: left; vertical-align: top; }
    th {
      color: #526059;
      background: #f6faf5;
      font-size: 12px;
      font-weight: 850;
      white-space: nowrap;
    }
    td { background: rgba(255, 255, 255, .7); }
    tr:hover td { background: #fbfdf9; }
    .tabs { display: inline-grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px; margin-bottom: 12px; padding: 4px; border: 1px solid var(--line); border-radius: 8px; background: #edf3ee; }
    .tab { min-height: 34px; background: transparent; color: #44534c; border: 0; box-shadow: none; }
    .tab:hover { transform: none; box-shadow: none; background: rgba(255, 255, 255, .72); }
    .tab.active { background: #fff; color: var(--shelf-dark); border-color: transparent; box-shadow: 0 6px 14px rgba(22, 40, 34, .08); }
    .message { min-height: 20px; margin: 10px 0 0; font-size: 13px; font-weight: 650; }
    .section-head { display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 12px; }
    .section-head h2 { margin: 0; }
    .section-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
    .table-actions { display: inline-flex; gap: 8px; align-items: center; white-space: nowrap; }
    .mobile-list { display: none; }
    .text-button { min-height: 32px; padding: 5px 9px; border: 1px solid var(--line); background: #fff; color: var(--shelf-dark); font-size: 12px; box-shadow: none; }
    .text-button:hover { background: #f3f8f2; box-shadow: none; }
    .text-button.danger { color: var(--danger); border-color: #e7bdb4; }
    .status-pill { display: inline-flex; align-items: center; border-radius: 999px; padding: 4px 9px; background: #e6f0ff; color: #1d5187; font-size: 12px; font-weight: 800; white-space: nowrap; }
    .status-pill.done { background: var(--available-soft); color: #0a5a3b; }
    .status-pill.stop { background: #edf1ee; color: #5b6760; }
    .check-cell { width: 38px; text-align: center; }
    .modal { position: fixed; inset: 0; z-index: 50; display: grid; place-items: center; padding: 22px; background: rgba(17, 32, 28, .48); backdrop-filter: blur(8px); }
    .modal-panel { width: min(1120px, 100%); max-height: min(780px, calc(100vh - 44px)); overflow: auto; background: #fff; border-radius: 8px; border: 1px solid var(--line); box-shadow: 0 24px 80px rgba(17, 32, 28, .24); padding: 20px; }
    .modal-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
    .modal-head h2 { margin: 0 0 6px; }
    .modal-empty { border: 1px solid #e5c178; background: #fff8e8; color: #7b4d0a; border-radius: 8px; padding: 14px; }
    .alert-panel { width: min(520px, 100%); border-top: 5px solid var(--available); }
    .alert-seat-list { margin: 12px 0 0; padding: 12px; border-radius: 8px; background: var(--available-soft); color: #0b4d35; line-height: 1.55; font-family: var(--mono-font); }
    .alert-actions { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-top: 16px; }
    .alert-actions button { width: 100%; }
    .webhook-details { border: 1px solid var(--soft-line); border-radius: 8px; background: #f8fbf6; }
    .webhook-details summary { cursor: pointer; list-style: none; display: flex; justify-content: space-between; gap: 10px; align-items: center; padding: 11px 12px; color: var(--shelf-dark); font-size: 13px; font-weight: 800; }
    .webhook-details summary::-webkit-details-marker { display: none; }
    .webhook-details summary::before { content: "展开"; min-width: 34px; color: var(--shelf); font-weight: 850; }
    .webhook-details[open] summary::before { content: "收起"; }
    .webhook-summary { margin-left: auto; color: var(--muted); font-size: 12px; font-weight: 500; text-align: right; }
    .webhook-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; padding: 0 12px 12px; }
    .start-watch-panel { width: min(520px, 100%); }
    .start-watch-text { margin: 0; color: #44534c; line-height: 1.68; font-size: 14px; }
    .start-watch-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
    .consent-panel { width: min(560px, 100%); border-top: 5px solid var(--lamp); }
    .consent-copy { margin: 0 0 14px; color: #44534c; line-height: 1.68; font-size: 14px; }
    .consent-list { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
    .consent-list li { border-left: 3px solid var(--line); background: #f8fbf6; padding: 10px 12px; color: #526059; font-size: 13px; line-height: 1.55; }
    .consent-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
    #appPanel { padding-bottom: 92px; }
    .app-view.hidden { display: none !important; }
    .app-view input,
    .app-view select {
      min-height: 38px;
      padding: 8px 10px;
      font-size: 13px;
    }
    .view-empty {
      display: grid;
      place-items: center;
      min-height: 260px;
      border: 1px dashed var(--line);
      border-radius: 8px;
      background: #fbfdf9;
      color: var(--muted);
      text-align: center;
      padding: 24px;
    }
    .view-empty b { display: block; margin-bottom: 6px; color: var(--shelf-dark); font-size: 16px; }
    .app-dock {
      position: fixed;
      left: 50%;
      bottom: max(18px, env(safe-area-inset-bottom));
      transform: translateX(-50%);
      z-index: 35;
      width: min(520px, calc(100vw - 32px));
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 4px;
      padding: 6px;
      border: 1px solid rgba(203, 216, 208, .92);
      border-radius: 8px;
      background: rgba(255, 255, 255, .94);
      box-shadow: 0 18px 52px rgba(22, 40, 34, .16);
      backdrop-filter: blur(18px);
    }
    .dock-item {
      min-height: 54px;
      padding: 6px 4px;
      border-radius: 6px;
      background: transparent;
      color: #59665f;
      box-shadow: none;
      display: grid;
      place-items: center;
      gap: 2px;
      font-size: 12px;
      font-weight: 780;
    }
    .dock-item:hover { transform: none; box-shadow: none; background: #f3f8f2; color: var(--shelf-dark); }
    .dock-item.active { background: #edf7f0; color: var(--shelf); }
    .dock-icon {
      display: grid;
      place-items: center;
      width: 22px;
      height: 22px;
      color: currentColor;
    }
    .dock-icon svg {
      width: 21px;
      height: 21px;
      stroke: currentColor;
      stroke-width: 2;
      fill: none;
      stroke-linecap: round;
      stroke-linejoin: round;
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after {
        scroll-behavior: auto !important;
        transition-duration: .01ms !important;
        animation-duration: .01ms !important;
        animation-iteration-count: 1 !important;
      }
    }
    @media (max-width: 820px) {
      body { background-size: 28px 28px, 28px 28px, auto; }
      header { padding: 9px 14px; padding-left: max(14px, env(safe-area-inset-left)); padding-right: max(14px, env(safe-area-inset-right)); align-items: flex-start; min-height: 64px; }
      h1 { line-height: 36px; font-size: 16px; }
      h1::before { width: 36px; height: 28px; font-size: 9px; }
      #topUser { justify-content: flex-end; gap: 8px; }
      #username { max-width: 46vw; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      main { width: calc(100vw - 16px); padding: 12px 0; padding-bottom: max(16px, env(safe-area-inset-bottom)); }
      section { padding: 14px; margin-bottom: 10px; }
      form { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
      #authForm, #watchForm { grid-template-columns: 1fr; }
      .disclaimer { padding: 10px; }
      .disclaimer-grid { grid-template-columns: 1fr; }
      #queryForm label:nth-child(1), #queryForm label:nth-child(2), #queryForm button { grid-column: 1 / -1; }
      #queryForm label:nth-child(3), #queryForm label:nth-child(4) { grid-column: span 1; }
      #queryForm label.row { grid-column: span 1; align-content: center; min-height: 34px; padding: 5px 9px; border: 1px solid var(--soft-line); border-radius: 8px; background: #f8fbf6; gap: 7px; }
      #queryForm label.row span { font-size: 13px; }
      #queryForm label.row input { flex: 0 0 auto; }
      .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
      .stat { padding: 10px; }
      .stat b { font-size: 18px; }
      .wide { grid-column: span 1; }
      .statusbar { grid-template-columns: 1fr; }
      .button-link, form > button { width: 100%; }
      .section-head { align-items: stretch; flex-direction: row; }
      .section-head h2 { align-self: center; }
      .section-actions { justify-content: flex-end; }
      #deleteSelectedHistoryBtn { display: none; }
      .tabs { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
      .tab { width: 100%; padding-left: 8px; padding-right: 8px; }
      .seat-grid { grid-template-columns: repeat(auto-fill, minmax(58px, 1fr)); gap: 6px; padding: 8px; }
      .pair-grid { grid-template-columns: repeat(auto-fill, minmax(104px, 1fr)); }
      .seat, .pair { padding: 9px 6px; min-height: 38px; }
      .responsive-table { display: none; }
      .mobile-list { display: grid; gap: 8px; }
      .mobile-card { border: 1px solid var(--soft-line); border-radius: 8px; padding: 12px; background: #fff; }
      .mobile-card-head { display: flex; justify-content: space-between; gap: 10px; align-items: flex-start; margin-bottom: 8px; }
      .mobile-card-actions { display: flex; gap: 8px; align-items: center; flex: 0 0 auto; }
      .mobile-title { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; font-size: 15px; font-weight: 800; color: var(--ink); }
      .mobile-title small { color: var(--muted); font-size: 11px; font-weight: 600; }
      .mobile-subtitle { margin-top: 3px; color: var(--muted); font-size: 12px; }
      .mobile-meta { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }
      .mobile-meta-item { border-radius: 8px; background: #f8fbf6; padding: 8px; min-width: 0; }
      a.mobile-meta-item { display: block; color: inherit; text-decoration: none; border: 1px solid transparent; }
      a.mobile-meta-item:hover { border-color: var(--shelf); }
      .mobile-meta-item span { display: block; color: var(--muted); font-size: 11px; line-height: 1.2; }
      .mobile-meta-item b { display: block; margin-top: 3px; color: var(--ink); font-size: 14px; font-weight: 800; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .mobile-meta-item.wide { grid-column: span 2; }
      .mobile-meta-item.wrap b { font-size: 12px; line-height: 1.35; overflow: visible; text-overflow: clip; white-space: normal; word-break: break-word; }
      .mobile-card-foot { display: flex; justify-content: flex-end; gap: 8px; align-items: center; margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--soft-line); }
      .mobile-empty { color: var(--muted); font-size: 13px; padding: 12px; text-align: center; border: 1px dashed var(--line); border-radius: 8px; }
      .check-cell { width: auto; text-align: left; }
      .table-actions { display: flex; gap: 8px; }
      .table-actions .text-button { flex: 1; }
      .modal { padding: 0; align-items: stretch; }
      .modal-panel { width: 100%; max-height: 100vh; border-radius: 0; border-left: 0; border-right: 0; padding: 14px; padding-bottom: max(14px, env(safe-area-inset-bottom)); }
      .modal-head { flex-direction: column; }
      .alert-actions { grid-template-columns: 1fr; }
      .webhook-fields { grid-template-columns: 1fr; }
      .webhook-details summary { align-items: flex-start; }
      .webhook-summary { max-width: 48vw; }
      .start-watch-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .consent-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
      #appPanel { padding-bottom: calc(86px + env(safe-area-inset-bottom)); }
      .app-dock {
        left: 0;
        right: 0;
        bottom: 0;
        transform: none;
        width: 100%;
        border-left: 0;
        border-right: 0;
        border-bottom: 0;
        border-radius: 8px 8px 0 0;
        padding: 6px 8px max(6px, env(safe-area-inset-bottom));
      }
      .dock-item { min-height: 50px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>工职大座位雷达</h1>
    <div id="topUser" class="row hidden">
      <span class="muted" id="username"></span>
      <button class="ghost" id="logoutBtn" type="button">退出</button>
    </div>
  </header>

  <main>
    <section id="authPanel" class="auth">
      <h2>登录座位雷达</h2>
      <form id="authForm">
        <label>学习通账号
          <input name="account" autocomplete="username" required>
        </label>
        <label>学习通密码
          <input name="password" type="password" autocomplete="current-password" required>
        </label>
        <div class="row">
          <button type="submit">登录学习通</button>
        </div>
      </form>
      <div class="message" id="authMessage"></div>
    </section>

    <div id="appPanel" class="hidden">
      <section id="accountSection">
        <div class="statusbar">
          <div>
            <h2>学习通账号</h2>
            <div id="cxStatus" class="muted"></div>
          </div>
          <a class="button-link" id="officialLink" href="#" target="_blank" rel="noreferrer">打开预约页</a>
        </div>
      </section>

      <section id="querySection" class="app-view" data-app-view>
        <h2>座位查询</h2>
        <form id="queryForm">
          <label>房间
            <select name="room_id" required></select>
          </label>
          <label>日期
            <input name="day" type="date" required>
          </label>
          <label>开始时间
            <select name="start_time" required></select>
          </label>
          <label>结束时间
            <select name="end_time" required></select>
          </label>
          <label class="row wide">
            <input name="ignore_no_power" type="checkbox" style="width: auto;">
            <span>忽略无电源座位</span>
          </label>
          <label class="row wide">
            <input name="ignore_sunny" type="checkbox" style="width: auto;">
            <span>忽略太阳晒座位</span>
          </label>
          <button type="submit">查询可预约座位</button>
        </form>
        <div class="message" id="queryMessage"></div>
      </section>

      <section id="resultsPanel" class="app-view hidden" data-app-view>
        <div id="resultsEmpty" class="view-empty">
          <div>
            <b>还没有查询结果</b>
            <span>先在“查询”页选择房间和时段，完成后会自动切到这里。</span>
          </div>
        </div>
        <div id="resultsContent" class="hidden">
          <div class="stats">
            <div class="stat">总座位 <b id="statTotal">0</b></div>
            <div class="stat">可预约 <b id="statAvailable">0</b></div>
            <div class="stat">已占用 <b id="statOccupied">0</b></div>
            <div class="stat">已过滤 <b id="statFiltered">0</b></div>
            <div class="stat">连排座 <b id="statPairs">0</b></div>
          </div>
          <div class="tabs">
            <button class="tab active" type="button" data-tab="seats">可预约座位</button>
            <button class="tab" type="button" data-tab="pairs">双人连排</button>
          </div>
          <div id="seatGrid" class="seat-grid"></div>
          <div id="pairGrid" class="seat-grid pair-grid hidden"></div>
        </div>
      </section>

      <section id="watchSection" class="app-view hidden" data-app-view>
        <div class="section-head">
          <h2>蹲座提醒</h2>
          <div class="section-actions">
            <button class="text-button" id="refreshWatchBtn" type="button">刷新</button>
            <button class="text-button" id="toggleWatchBtn" type="button">查看历史</button>
          </div>
        </div>
        <form id="watchForm">
          <label>轮询间隔
            <input name="interval_seconds" type="number" min="15" max="3600" value="60" required>
          </label>
          <details class="webhook-details full" id="webhookDetails">
            <summary>
              <span>Webhook 通知</span>
              <span class="webhook-summary" id="webhookSummary">未设置</span>
            </summary>
            <div class="webhook-fields">
              <label>Webhook
                <input name="webhook_url" type="url" placeholder="https://">
              </label>
              <label class="row">
                <input name="save_webhook" type="checkbox" style="width: auto;">
                <span>保存 Webhook，下次自动填入</span>
              </label>
            </div>
          </details>
          <button type="submit">开启蹲座位</button>
        </form>
        <div class="message" id="watchMessage"></div>
        <table class="responsive-table">
          <thead>
            <tr>
              <th>创建时间</th>
              <th>日期</th>
              <th>时段</th>
              <th>房间</th>
              <th>筛选</th>
              <th>状态</th>
              <th>上次检查</th>
              <th>命中座位</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody id="watchRows"></tbody>
        </table>
        <div class="mobile-list" id="watchCards"></div>
      </section>

      <section id="historySection" class="app-view hidden" data-app-view>
        <div class="section-head">
          <h2>历史</h2>
          <div class="section-actions">
            <button class="text-button" id="toggleHistoryBtn" type="button">显示全部</button>
            <button class="text-button danger" id="deleteSelectedHistoryBtn" type="button">删除选中</button>
          </div>
        </div>
        <div class="tabs">
          <button class="tab active" type="button" data-history-kind="query">查询历史</button>
          <button class="tab" type="button" data-history-kind="watch">蹲座历史</button>
        </div>
        <div id="queryHistoryPanel">
          <table class="responsive-table">
            <thead>
              <tr>
                <th class="check-cell"><input id="historySelectAll" type="checkbox"></th>
                <th>时间</th>
                <th>日期</th>
                <th>时段</th>
                <th>房间</th>
                <th>可预约</th>
                <th>已占用</th>
                <th>连排</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody id="historyRows"></tbody>
          </table>
          <div class="mobile-list" id="historyCards"></div>
        </div>
        <div id="watchHistoryPanel" class="hidden">
          <table class="responsive-table">
            <thead>
              <tr>
                <th>创建时间</th>
                <th>日期</th>
                <th>时段</th>
                <th>房间</th>
                <th>筛选</th>
                <th>状态</th>
                <th>上次检查</th>
                <th>命中座位</th>
              </tr>
            </thead>
            <tbody id="watchHistoryRows"></tbody>
          </table>
          <div class="mobile-list" id="watchHistoryCards"></div>
        </div>
      </section>

      <nav class="app-dock" aria-label="主导航">
        <button class="dock-item active" type="button" data-dock-target="querySection" aria-label="座位查询">
          <span class="dock-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"></circle><path d="m16.5 16.5 4 4"></path></svg>
          </span>
          <span>查询</span>
        </button>
        <button class="dock-item" type="button" data-dock-target="resultsPanel" aria-label="查询结果">
          <span class="dock-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24"><path d="M4 5h16"></path><path d="M4 12h16"></path><path d="M4 19h16"></path><path d="M8 5v14"></path></svg>
          </span>
          <span>结果</span>
        </button>
        <button class="dock-item" type="button" data-dock-target="watchSection" aria-label="蹲座提醒">
          <span class="dock-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"></path><path d="M10 21h4"></path></svg>
          </span>
          <span>蹲座</span>
        </button>
        <button class="dock-item" type="button" data-dock-target="historySection" aria-label="查询历史">
          <span class="dock-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7"></path><path d="M3 4v5h5"></path><path d="M12 7v5l3 2"></path></svg>
          </span>
          <span>历史</span>
        </button>
      </nav>
    </div>
  </main>

  <div id="historyModal" class="modal hidden" role="dialog" aria-modal="true">
    <div class="modal-panel">
      <div class="modal-head">
        <div>
          <h2 id="historyModalTitle">历史查询结果</h2>
          <div id="historyModalMeta" class="muted"></div>
        </div>
        <button class="ghost" id="historyModalClose" type="button">关闭</button>
      </div>
      <div id="historyModalEmpty" class="modal-empty hidden"></div>
      <div id="historyModalContent">
        <div class="stats">
          <div class="stat">总座位 <b id="modalStatTotal">0</b></div>
          <div class="stat">可预约 <b id="modalStatAvailable">0</b></div>
          <div class="stat">已占用 <b id="modalStatOccupied">0</b></div>
          <div class="stat">已过滤 <b id="modalStatFiltered">0</b></div>
          <div class="stat">连排座 <b id="modalStatPairs">0</b></div>
        </div>
        <div class="tabs">
          <button class="tab active" type="button" data-modal-tab="seats">可预约座位</button>
          <button class="tab" type="button" data-modal-tab="pairs">双人连排</button>
        </div>
        <div id="modalSeatGrid" class="seat-grid"></div>
        <div id="modalPairGrid" class="seat-grid pair-grid hidden"></div>
      </div>
    </div>
  </div>

  <div id="startWatchModal" class="modal hidden" role="dialog" aria-modal="true">
    <div class="modal-panel start-watch-panel">
      <div class="modal-head">
        <div>
          <h2>开启蹲座位提醒</h2>
          <div class="muted">任务创建后会按轮询间隔自动检查</div>
        </div>
      </div>
      <p class="start-watch-text">
        命中座位后会弹窗提醒，并发起一次 App 系统通知。App 被系统完全关闭时，本地通知可能无法送达；已保存的 Webhook 会随任务一起生效，即使当前折叠也会继续发送。
      </p>
      <div class="start-watch-actions">
        <button class="ghost" id="startWatchCancel" type="button">取消</button>
        <button id="startWatchContinue" type="button">继续开启</button>
      </div>
    </div>
  </div>

  <div id="watchAlertModal" class="modal hidden" role="dialog" aria-modal="true">
    <div class="modal-panel alert-panel">
      <div class="modal-head">
        <div>
          <h2>找到可预约座位</h2>
          <div id="watchAlertMeta" class="muted"></div>
        </div>
      </div>
      <div id="watchAlertSeats" class="alert-seat-list"></div>
      <div class="alert-actions">
        <button class="ghost" id="watchAlertCancel" type="button">取消</button>
        <button id="watchAlertReserve" type="button">预约</button>
        <button class="secondary" id="watchAlertConfirm" type="button">确认</button>
      </div>
    </div>
  </div>

  <div id="disclaimerModal" class="modal hidden" role="dialog" aria-modal="true">
    <div class="modal-panel consent-panel">
      <div class="modal-head">
        <div>
          <h2>登录前确认</h2>
          <div class="muted">同意后才会提交学习通账号密码。</div>
        </div>
      </div>
      <p class="consent-copy">
        本工具只用于查询图书馆座位、跳转预约页面和维护必要登录状态。请确认你理解并接受以下说明。
      </p>
      <ul class="consent-list">
        <li>账号信息仅用于当前服务请求学习通座位接口，不会主动泄漏或分享给第三方。</li>
        <li>本项目仅供学习研究使用，请遵守学校和学习通相关规则，不要影响他人正常使用。</li>
        <li>浏览器跳转到官方预约页时，仍以学习通页面和学校规则为准。</li>
      </ul>
      <div class="consent-actions">
        <button class="ghost" id="disclaimerCancel" type="button">不同意</button>
        <button id="disclaimerAccept" type="button">同意并登录</button>
      </div>
    </div>
  </div>

<script>
const authPanel = document.querySelector('#authPanel');
const appPanel = document.querySelector('#appPanel');
const topUser = document.querySelector('#topUser');
const username = document.querySelector('#username');
const authForm = document.querySelector('#authForm');
const queryForm = document.querySelector('#queryForm');
const watchForm = document.querySelector('#watchForm');
const authMessage = document.querySelector('#authMessage');
const queryMessage = document.querySelector('#queryMessage');
const watchMessage = document.querySelector('#watchMessage');
const cxStatus = document.querySelector('#cxStatus');
const officialLink = document.querySelector('#officialLink');
const historyRows = document.querySelector('#historyRows');
const watchRows = document.querySelector('#watchRows');
const watchHistoryRows = document.querySelector('#watchHistoryRows');
const historyCards = document.querySelector('#historyCards');
const watchCards = document.querySelector('#watchCards');
const watchHistoryCards = document.querySelector('#watchHistoryCards');
const queryHistoryPanel = document.querySelector('#queryHistoryPanel');
const watchHistoryPanel = document.querySelector('#watchHistoryPanel');
const historySelectAll = document.querySelector('#historySelectAll');
const deleteSelectedHistoryBtn = document.querySelector('#deleteSelectedHistoryBtn');
const refreshWatchBtn = document.querySelector('#refreshWatchBtn');
const toggleHistoryBtn = document.querySelector('#toggleHistoryBtn');
const toggleWatchBtn = document.querySelector('#toggleWatchBtn');
const resultsPanel = document.querySelector('#resultsPanel');
const resultsEmpty = document.querySelector('#resultsEmpty');
const resultsContent = document.querySelector('#resultsContent');
const seatGrid = document.querySelector('#seatGrid');
const pairGrid = document.querySelector('#pairGrid');
const historyModal = document.querySelector('#historyModal');
const historyModalClose = document.querySelector('#historyModalClose');
const historyModalTitle = document.querySelector('#historyModalTitle');
const historyModalMeta = document.querySelector('#historyModalMeta');
const historyModalEmpty = document.querySelector('#historyModalEmpty');
const historyModalContent = document.querySelector('#historyModalContent');
const modalSeatGrid = document.querySelector('#modalSeatGrid');
const modalPairGrid = document.querySelector('#modalPairGrid');
const watchAlertModal = document.querySelector('#watchAlertModal');
const watchAlertMeta = document.querySelector('#watchAlertMeta');
const watchAlertSeats = document.querySelector('#watchAlertSeats');
const watchAlertCancel = document.querySelector('#watchAlertCancel');
const watchAlertReserve = document.querySelector('#watchAlertReserve');
const watchAlertConfirm = document.querySelector('#watchAlertConfirm');
const webhookDetails = document.querySelector('#webhookDetails');
const webhookSummary = document.querySelector('#webhookSummary');
const startWatchModal = document.querySelector('#startWatchModal');
const startWatchCancel = document.querySelector('#startWatchCancel');
const startWatchContinue = document.querySelector('#startWatchContinue');
const disclaimerModal = document.querySelector('#disclaimerModal');
const disclaimerCancel = document.querySelector('#disclaimerCancel');
const disclaimerAccept = document.querySelector('#disclaimerAccept');
const dockItems = Array.from(document.querySelectorAll('.dock-item'));
const appViews = Array.from(document.querySelectorAll('[data-app-view]'));
const allowedTimes = Array.from({ length: 15 }, (_, index) => `${String(index + 8).padStart(2, '0')}:00`);
const DEFAULT_HISTORY_LIMIT = 3;
const WATCH_ALERT_POLL_INTERVAL_MS = 5000;
let latestResult = null;
let officialIndexUrl = '';
let historyItems = [];
let watchItems = [];
let showAllHistory = false;
let activeHistoryKind = 'query';
let activeWatchAlert = null;
let currentAlerts = [];
const dismissedAlertIds = new Set();
let startWatchResolver = null;
let disclaimerResolver = null;
let watchAlertSource = null;
let watchAlertReconnectTimer = null;

function today() {
  return new Date().toISOString().slice(0, 10);
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function setMessage(node, text, ok = false) {
  node.textContent = text || '';
  node.className = `message ${ok ? 'ok' : text ? 'bad' : ''}`;
}

function setActiveDock(targetId) {
  dockItems.forEach(item => {
    item.classList.toggle('active', item.dataset.dockTarget === targetId);
  });
}

function switchAppView(targetId) {
  const target = document.querySelector(`#${targetId}`);
  if (!target) return;
  appViews.forEach(view => {
    view.classList.toggle('hidden', view.id !== targetId);
  });
  setActiveDock(targetId);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function resolveDisclaimer(value) {
  disclaimerModal.classList.add('hidden');
  if (disclaimerResolver) {
    disclaimerResolver(value);
    disclaimerResolver = null;
  }
}

function confirmDisclaimer() {
  return new Promise(resolve => {
    disclaimerResolver = resolve;
    disclaimerModal.classList.remove('hidden');
    disclaimerAccept.focus();
  });
}

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

function alertTitle(alert) {
  return `${alert.room_name || alert.room_id} 有可预约座位`;
}

function alertBody(alert) {
  const matched = alert.matched_seats || [];
  const seats = matched.length > 8
    ? `${matched.slice(0, 8).join(', ')} 等 ${matched.length} 个`
    : matched.join(', ');
  return `${alert.day} ${alert.start_time}-${alert.end_time} · ${seats}`;
}

function notifiedAlertIds() {
  try {
    return new Set(JSON.parse(localStorage.getItem('searchSeatNotifiedAlerts') || '[]'));
  } catch {
    return new Set();
  }
}

function saveNotifiedAlertIds(ids) {
  localStorage.setItem('searchSeatNotifiedAlerts', JSON.stringify(Array.from(ids).slice(-200)));
}

function notifyNative(alert) {
  const ids = notifiedAlertIds();
  const key = String(alert.id);
  if (ids.has(key)) return;
  ids.add(key);
  saveNotifiedAlertIds(ids);

  if (window.SearchSeatAndroid && typeof window.SearchSeatAndroid.notifySeatMatched === 'function') {
    window.SearchSeatAndroid.notifySeatMatched(String(alert.id), alertTitle(alert), alertBody(alert), alert.reserve_url || '');
  }
}

function mergeWatchAlert(alert) {
  if (!alert || alert.id == null) return;
  currentAlerts = [
    alert,
    ...currentAlerts.filter(item => Number(item.id) !== Number(alert.id))
  ];
}

function stopWatchAlertStream() {
  if (watchAlertReconnectTimer) {
    clearTimeout(watchAlertReconnectTimer);
    watchAlertReconnectTimer = null;
  }
  if (watchAlertSource) {
    watchAlertSource.close();
    watchAlertSource = null;
  }
}

function scheduleWatchAlertStreamReconnect() {
  if (watchAlertReconnectTimer || authPanel.classList.contains('hidden') === false) return;
  watchAlertReconnectTimer = setTimeout(() => {
    watchAlertReconnectTimer = null;
    startWatchAlertStream();
  }, WATCH_ALERT_POLL_INTERVAL_MS);
}

function startWatchAlertStream() {
  if (!window.EventSource || watchAlertSource || authPanel.classList.contains('hidden') === false) return;
  watchAlertSource = new EventSource('/api/watch-alerts/stream');
  watchAlertSource.addEventListener('alert', async event => {
    try {
      const alert = JSON.parse(event.data);
      mergeWatchAlert(alert);
      notifyNative(alert);
      showNextWatchAlert();
      await loadWatchTasks();
    } catch {
    }
  });
  watchAlertSource.onerror = () => {
    stopWatchAlertStream();
    scheduleWatchAlertStreamReconnect();
  };
}

function updateWebhookSummary() {
  const url = (watchForm.webhook_url.value || '').trim();
  const saved = watchForm.save_webhook.checked;
  if (!url) {
    webhookSummary.textContent = '未设置';
    return;
  }
  webhookSummary.textContent = saved ? '已保存，会用于通知' : '仅本次任务使用';
}

function resolveStartWatch(value) {
  startWatchModal.classList.add('hidden');
  if (startWatchResolver) {
    startWatchResolver(value);
    startWatchResolver = null;
  }
}

function confirmStartWatch() {
  return new Promise(resolve => {
    startWatchResolver = resolve;
    startWatchModal.classList.remove('hidden');
    startWatchContinue.focus();
  });
}

async function checkWatchAlerts({ showPopup = true } = {}) {
  if (authPanel.classList.contains('hidden') === false) return;
  try {
    const data = await api('/api/watch-alerts', { method: 'GET', headers: {} });
    currentAlerts = data.alerts || [];
    currentAlerts.forEach(notifyNative);
    if (showPopup) showNextWatchAlert();
  } catch {
  }
}

function showNextWatchAlert() {
  if (!watchAlertModal.classList.contains('hidden')) return;
  const alert = currentAlerts.find(item => !dismissedAlertIds.has(Number(item.id)));
  if (!alert) return;
  activeWatchAlert = alert;
  watchAlertMeta.textContent = `${alert.room_name || alert.room_id} · ${alert.day} ${alert.start_time}-${alert.end_time}`;
  const matched = alert.matched_seats || [];
  watchAlertSeats.textContent = matched.length
    ? `命中座位：${matched.join(', ')}`
    : '命中座位：-';
  watchAlertModal.classList.remove('hidden');
}

function closeWatchAlert() {
  watchAlertModal.classList.add('hidden');
  activeWatchAlert = null;
}

async function ackWatchAlert(action) {
  if (!activeWatchAlert) return;
  const alert = activeWatchAlert;
  await api(`/api/watch-alerts/${alert.id}/ack`, {
    method: 'POST',
    body: JSON.stringify({ action })
  });
  currentAlerts = currentAlerts.filter(item => Number(item.id) !== Number(alert.id));
  closeWatchAlert();
  await loadWatchTasks();
  if (action === 'reserve' && alert.reserve_url) {
    location.href = alert.reserve_url;
    return;
  }
  showNextWatchAlert();
}

function setTimeOptions() {
  queryForm.start_time.innerHTML = '<option value="">请选择开始时间</option>' +
    allowedTimes.slice(0, -1).map(time => `<option value="${time}">${time}</option>`).join('');
  queryForm.day.value = today();
  queryForm.start_time.value = '';
  syncEndTimeOptions('');
}

function syncEndTimeOptions(preferredEnd) {
  const start = queryForm.start_time.value;
  const currentEnd = preferredEnd || queryForm.end_time.value;
  if (!start) {
    queryForm.end_time.innerHTML = '<option value="">请先选择开始时间</option>';
    queryForm.end_time.value = '';
    return;
  }
  const endTimes = allowedTimes.filter(time => time > start);
  queryForm.end_time.innerHTML = '<option value="">请选择结束时间</option>' +
    endTimes.map(time => `<option value="${time}">${time}</option>`).join('');
  queryForm.end_time.value = endTimes.includes(currentEnd) ? currentEnd : '';
}

function fillDefaults(defaults) {
  const roomOptions = defaults.rooms.map(room =>
    `<option value="${escapeHtml(room.room_id)}">${escapeHtml(room.label)}</option>`
  ).join('');
  queryForm.room_id.innerHTML = roomOptions;
  queryForm.room_id.value = defaults.default_room_id;
  watchForm.webhook_url.value = defaults.default_webhook_url || '';
  watchForm.save_webhook.checked = Boolean(defaults.default_webhook_url);
  webhookDetails.open = false;
  updateWebhookSummary();
  officialIndexUrl = defaults.official_index_url || '';
  updateOfficialLink();
}

function renderMe(data) {
  if (!data.user) {
    authPanel.classList.remove('hidden');
    appPanel.classList.add('hidden');
    topUser.classList.add('hidden');
    stopWatchAlertStream();
    return;
  }
  authPanel.classList.add('hidden');
  appPanel.classList.remove('hidden');
  topUser.classList.remove('hidden');
  username.textContent = data.user.username;
  fillDefaults(data.defaults);
  if (data.chaoxing.bound) {
    const validText = data.chaoxing.session_valid ? '已保存 Cookie' : 'Cookie 可能已失效';
    cxStatus.innerHTML = `${validText} · ${escapeHtml(data.chaoxing.cookies_updated_at || '')}`;
  } else {
    cxStatus.textContent = '未登录学习通';
  }
  startWatchAlertStream();
  updateOfficialLink();
  switchAppView('querySection');
}

function updateOfficialLink() {
  officialLink.href = officialIndexUrl || '#';
}

async function loadMe() {
  const data = await api('/api/me', { method: 'GET', headers: {} });
  renderMe(data);
}

async function loadHistory() {
  const data = await api('/api/history', { method: 'GET', headers: {} });
  historyItems = data.history || [];
  renderHistoryRows();
}

function renderHistoryRows() {
  const visibleHistory = showAllHistory ? historyItems : historyItems.slice(0, DEFAULT_HISTORY_LIMIT);
  historyRows.innerHTML = visibleHistory.map(row => `<tr>
    <td class="check-cell" data-label="选择"><input class="history-check" type="checkbox" value="${row.id}"></td>
    <td data-label="时间">${escapeHtml(row.created_at)}</td>
    <td data-label="日期">${escapeHtml(row.day)}</td>
    <td data-label="时段">${escapeHtml(row.start_time)}-${escapeHtml(row.end_time)}</td>
    <td data-label="房间">${escapeHtml(row.room_name || row.room_id)}</td>
    <td data-label="可预约">${row.available_count}</td>
    <td data-label="已占用">${row.occupied_count}</td>
    <td data-label="连排">${row.pair_count}</td>
    <td data-label="操作"><span class="table-actions">
      <button class="text-button" type="button" onclick="viewHistory(${row.id})">查看</button>
      <button class="text-button danger" type="button" onclick="deleteHistory(${row.id})">删除</button>
    </span></td>
  </tr>`).join('') || '<tr><td colspan="9">暂无记录</td></tr>';
  historyCards.innerHTML = visibleHistory.map(row => `<article class="mobile-card">
    <div class="mobile-card-head">
      <div>
        <div class="mobile-title">${escapeHtml(row.room_name || row.room_id)}</div>
        <div class="mobile-subtitle">${escapeHtml(row.day)} · ${escapeHtml(row.start_time)}-${escapeHtml(row.end_time)}</div>
      </div>
      <div class="mobile-card-actions">
      <button class="text-button" type="button" onclick="viewHistory(${row.id})">查看</button>
      <button class="text-button danger" type="button" onclick="deleteHistory(${row.id})">删除</button>
      </div>
    </div>
    <div class="mobile-meta">
      <div class="mobile-meta-item"><span>可预约</span><b>${row.available_count}</b></div>
      <div class="mobile-meta-item"><span>已占用</span><b>${row.occupied_count}</b></div>
      <div class="mobile-meta-item"><span>连排</span><b>${row.pair_count}</b></div>
    </div>
  </article>`).join('') || '<div class="mobile-empty">暂无记录</div>';
  historySelectAll.checked = false;
  renderHistoryControls();
}

async function loadWatchTasks() {
  const data = await api('/api/watch-tasks', { method: 'GET', headers: {} });
  watchItems = data.tasks || [];
  renderWatchRows();
  renderWatchHistoryRows();
  await checkWatchAlerts();
}

function watchPageItems() {
  const running = watchItems.filter(row => row.status === 'running');
  return running.length ? running : watchItems.slice(0, 1);
}

function renderWatchTableRows(items, includeActions = true) {
  return items.map(row => {
    const statusClass = row.status === 'matched' ? 'done' : row.status === 'running' ? '' : 'stop';
    const actions = includeActions && row.status === 'running'
      ? `<button class="text-button danger" type="button" onclick="cancelWatchTask(${row.id})">取消</button>`
      : '';
    const error = row.last_error ? `<div class="bad">${escapeHtml(row.last_error)}</div>` : '';
    const filters = [
      row.ignore_no_power ? '忽略无电源' : '',
      row.ignore_sunny ? '忽略太阳晒' : ''
    ].filter(Boolean).join('，') || '无';
    const matched = row.matched_seats || [];
    const matchedText = matched.length > 20
      ? `${matched.slice(0, 20).join(', ')} 等 ${matched.length} 个`
      : matched.join(', ');
    const matchedCell = row.reserve_url && matched.length
      ? `<a href="${escapeHtml(row.reserve_url)}" target="_blank" rel="noreferrer">${escapeHtml(matchedText)}</a>`
      : escapeHtml(matchedText);
    return `<tr>
      <td data-label="创建时间">${escapeHtml(row.created_at)}</td>
      <td data-label="日期">${escapeHtml(row.day)}</td>
      <td data-label="时段">${escapeHtml(row.start_time)}-${escapeHtml(row.end_time)}</td>
      <td data-label="房间">${escapeHtml(row.room_name || row.room_id)}</td>
      <td data-label="筛选">${escapeHtml(filters)}</td>
      <td data-label="状态"><span class="status-pill ${statusClass}">${escapeHtml(row.status_label)}</span>${error}</td>
      <td data-label="上次检查">${escapeHtml(row.last_checked_at || '')}</td>
      <td data-label="命中座位">${matchedCell}</td>
      ${includeActions ? `<td data-label="操作"><span class="table-actions">${actions}</span></td>` : ''}
    </tr>`;
  }).join('');
}

function renderWatchCardRows(items, includeActions = true) {
  return items.map(row => {
    const statusClass = row.status === 'matched' ? 'done' : row.status === 'running' ? '' : 'stop';
    const actions = includeActions && row.status === 'running'
      ? `<button class="text-button danger" type="button" onclick="cancelWatchTask(${row.id})">取消</button>`
      : '';
    const filters = [
      row.ignore_no_power ? '无电源' : '',
      row.ignore_sunny ? '太阳晒' : ''
    ].filter(Boolean).join('，') || '无';
    const matched = row.matched_seats || [];
    const matchedText = matched.length > 20
      ? `${matched.slice(0, 20).join(', ')} 等 ${matched.length} 个`
      : matched.join(', ') || '-';
    const matchedMeta = row.reserve_url && matched.length
      ? `<a class="mobile-meta-item wide wrap" href="${escapeHtml(row.reserve_url)}" target="_blank" rel="noreferrer"><span>命中</span><b>${escapeHtml(matchedText)}</b></a>`
      : `<div class="mobile-meta-item wide wrap"><span>命中</span><b>${escapeHtml(matchedText)}</b></div>`;
    return `<article class="mobile-card">
      <div class="mobile-card-head">
        <div>
          <div class="mobile-title">${escapeHtml(row.room_name || row.room_id)}<small>开启：${escapeHtml(row.created_at || '-')}</small></div>
          <div class="mobile-subtitle">${escapeHtml(row.day)} · ${escapeHtml(row.start_time)}-${escapeHtml(row.end_time)}</div>
        </div>
        <span class="status-pill ${statusClass}">${escapeHtml(row.status_label)}</span>
      </div>
      <div class="mobile-meta">
        ${matchedMeta}
        <div class="mobile-meta-item wide wrap"><span>筛选</span><b>${escapeHtml(filters)}</b></div>
        <div class="mobile-meta-item wide wrap"><span>检查</span><b>${escapeHtml(row.last_checked_at || '-')}</b></div>
      </div>
      ${row.last_error ? `<div class="bad">${escapeHtml(row.last_error)}</div>` : ''}
      ${includeActions && actions ? `<div class="mobile-card-foot">${actions}</div>` : ''}
    </article>`;
  }).join('');
}

function renderWatchRows() {
  const visibleWatch = watchPageItems();
  watchRows.innerHTML = renderWatchTableRows(visibleWatch, true) || '<tr><td colspan="9">暂无任务</td></tr>';
  watchCards.innerHTML = renderWatchCardRows(visibleWatch, true) || '<div class="mobile-empty">暂无任务</div>';
  toggleWatchBtn.classList.toggle('hidden', watchItems.length === 0);
  toggleWatchBtn.textContent = '查看历史';
}

function renderWatchHistoryRows() {
  const visibleWatch = showAllHistory ? watchItems : watchItems.slice(0, DEFAULT_HISTORY_LIMIT);
  watchHistoryRows.innerHTML = renderWatchTableRows(visibleWatch, false) || '<tr><td colspan="8">暂无蹲座历史</td></tr>';
  watchHistoryCards.innerHTML = renderWatchCardRows(visibleWatch, false) || '<div class="mobile-empty">暂无蹲座历史</div>';
  renderHistoryControls();
}

function renderHistoryControls() {
  const total = activeHistoryKind === 'watch' ? watchItems.length : historyItems.length;
  toggleHistoryBtn.classList.toggle('hidden', total <= DEFAULT_HISTORY_LIMIT);
  toggleHistoryBtn.textContent = showAllHistory ? '收起' : `显示全部 ${total} 条`;
  deleteSelectedHistoryBtn.classList.toggle('hidden', activeHistoryKind !== 'query');
}

function setHistoryKind(kind) {
  activeHistoryKind = kind;
  showAllHistory = false;
  document.querySelectorAll('[data-history-kind]').forEach(button => {
    button.classList.toggle('active', button.dataset.historyKind === kind);
  });
  queryHistoryPanel.classList.toggle('hidden', kind !== 'query');
  watchHistoryPanel.classList.toggle('hidden', kind !== 'watch');
  renderHistoryRows();
  renderWatchHistoryRows();
}

function renderResults(result) {
  latestResult = result;
  resultsEmpty.classList.add('hidden');
  resultsContent.classList.remove('hidden');
  document.querySelector('#statTotal').textContent = result.summary.total;
  document.querySelector('#statAvailable').textContent = result.summary.available;
  document.querySelector('#statOccupied').textContent = result.summary.occupied;
  document.querySelector('#statFiltered').textContent = result.summary.filtered || 0;
  document.querySelector('#statPairs').textContent = result.summary.pairs;
  renderSeatLists(result, seatGrid, pairGrid);
  setTab('seats');
  switchAppView('resultsPanel');
}

function renderSeatLists(result, seatTarget, pairTarget) {
  seatTarget.innerHTML = result.available.map(item =>
    `<a class="seat" href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">${escapeHtml(item.seat)}</a>`
  ).join('') || '<div class="muted">没有可预约座位</div>';
  pairTarget.innerHTML = result.pairs.map(item =>
    `<a class="pair" href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">${escapeHtml(item.seats.join(' + '))}</a>`
  ).join('') || '<div class="muted">没有双人连排</div>';
}

async function viewHistory(id) {
  try {
    const data = await api(`/api/history/${id}`, { method: 'GET', headers: {} });
    if (data.missing) {
      openHistoryMissing(data.message);
      return;
    }
    openHistoryModal(data.result);
  } catch (error) {
    openHistoryMissing(error.message);
  }
}

function openHistoryModal(result) {
  historyModalTitle.textContent = '历史查询结果';
  historyModalMeta.textContent = `${result.room_name || result.room_id} · ${result.day} ${result.start_time}-${result.end_time}`;
  historyModalEmpty.classList.add('hidden');
  historyModalEmpty.textContent = '';
  historyModalContent.classList.remove('hidden');
  document.querySelector('#modalStatTotal').textContent = result.summary.total;
  document.querySelector('#modalStatAvailable').textContent = result.summary.available;
  document.querySelector('#modalStatOccupied').textContent = result.summary.occupied;
  document.querySelector('#modalStatFiltered').textContent = result.summary.filtered || 0;
  document.querySelector('#modalStatPairs').textContent = result.summary.pairs;
  renderSeatLists(result, modalSeatGrid, modalPairGrid);
  setModalTab('seats');
  historyModal.classList.remove('hidden');
}

function openHistoryMissing(message) {
  historyModalTitle.textContent = '历史查询结果';
  historyModalMeta.textContent = '';
  historyModalContent.classList.add('hidden');
  historyModalEmpty.textContent = message;
  historyModalEmpty.classList.remove('hidden');
  historyModal.classList.remove('hidden');
}

async function deleteHistory(id) {
  if (!window.confirm('确定删除这条查询记录吗？')) return;
  try {
    await api(`/api/history/${id}/delete`, { method: 'POST', body: '{}' });
    await loadHistory();
  } catch (error) {
    setMessage(queryMessage, error.message);
  }
}

function selectedHistoryIds() {
  return Array.from(new Set(
    Array.from(document.querySelectorAll('.history-check:checked')).map(item => Number(item.value))
  ));
}

async function deleteSelectedHistory() {
  const ids = selectedHistoryIds();
  if (!ids.length) {
    setMessage(queryMessage, '请选择要删除的查询记录');
    return;
  }
  if (!window.confirm(`确定删除选中的 ${ids.length} 条查询记录吗？`)) return;
  try {
    await api('/api/history/delete', {
      method: 'POST',
      body: JSON.stringify({ ids })
    });
    await loadHistory();
  } catch (error) {
    setMessage(queryMessage, error.message);
  }
}

async function cancelWatchTask(id) {
  if (!window.confirm('确定取消这个蹲座位任务吗？')) return;
  try {
    const data = await api(`/api/watch-tasks/${id}/cancel`, { method: 'POST', body: '{}' });
    watchRows.innerHTML = '';
    await loadWatchTasks();
    setMessage(watchMessage, '已取消', true);
  } catch (error) {
    setMessage(watchMessage, error.message);
  }
}

function setTab(tab) {
  document.querySelectorAll('[data-tab]').forEach(button => button.classList.toggle('active', button.dataset.tab === tab));
  seatGrid.classList.toggle('hidden', tab !== 'seats');
  pairGrid.classList.toggle('hidden', tab !== 'pairs');
}

function setModalTab(tab) {
  document.querySelectorAll('[data-modal-tab]').forEach(button => {
    button.classList.toggle('active', button.dataset.modalTab === tab);
  });
  modalSeatGrid.classList.toggle('hidden', tab !== 'seats');
  modalPairGrid.classList.toggle('hidden', tab !== 'pairs');
}

authForm.addEventListener('submit', async event => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(authForm).entries());
  const accepted = await confirmDisclaimer();
  if (!accepted) {
    setMessage(authMessage, '需要同意使用说明后才能登录');
    return;
  }
  setMessage(authMessage, '正在登录学习通...');
  try {
    await api('/api/chaoxing/login', {
      method: 'POST',
      body: JSON.stringify(payload)
    });
    authForm.password.value = '';
    setMessage(authMessage, '', true);
    await loadMe();
    await loadHistory();
    await loadWatchTasks();
  } catch (error) {
    setMessage(authMessage, error.message);
  }
});

document.querySelector('#logoutBtn').addEventListener('click', async () => {
  await api('/api/logout', { method: 'POST' });
  location.reload();
});

historySelectAll.addEventListener('change', () => {
  document.querySelectorAll('.history-check').forEach(item => {
    item.checked = historySelectAll.checked;
  });
});

historyRows.addEventListener('change', event => {
  if (!event.target.classList.contains('history-check')) return;
  const checks = Array.from(document.querySelectorAll('.history-check'));
  historySelectAll.checked = checks.length > 0 && checks.every(item => item.checked);
});

deleteSelectedHistoryBtn.addEventListener('click', deleteSelectedHistory);
toggleHistoryBtn.addEventListener('click', () => {
  showAllHistory = !showAllHistory;
  if (activeHistoryKind === 'watch') {
    renderWatchHistoryRows();
  } else {
    renderHistoryRows();
  }
});
toggleWatchBtn.addEventListener('click', () => {
  setHistoryKind('watch');
  switchAppView('historySection');
});
dockItems.forEach(item => {
  item.addEventListener('click', () => switchAppView(item.dataset.dockTarget));
});
document.querySelectorAll('[data-history-kind]').forEach(button => {
  button.addEventListener('click', () => setHistoryKind(button.dataset.historyKind));
});
refreshWatchBtn.addEventListener('click', async () => {
  try {
    await loadWatchTasks();
    setMessage(watchMessage, '已刷新', true);
  } catch (error) {
    setMessage(watchMessage, error.message);
  }
});
watchForm.webhook_url.addEventListener('input', updateWebhookSummary);
watchForm.save_webhook.addEventListener('change', updateWebhookSummary);
startWatchCancel.addEventListener('click', () => resolveStartWatch(false));
startWatchContinue.addEventListener('click', () => resolveStartWatch(true));
disclaimerCancel.addEventListener('click', () => resolveDisclaimer(false));
disclaimerAccept.addEventListener('click', () => resolveDisclaimer(true));

queryForm.addEventListener('input', updateOfficialLink);
queryForm.addEventListener('change', event => {
  updateOfficialLink();
});
queryForm.start_time.addEventListener('change', () => syncEndTimeOptions());
queryForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (!queryForm.start_time.value || !queryForm.end_time.value) {
    setMessage(queryMessage, '请选择开始时间和结束时间');
    return;
  }
  if (queryForm.end_time.value <= queryForm.start_time.value) {
    setMessage(queryMessage, '结束时间必须晚于开始时间');
    return;
  }
  setMessage(queryMessage, '正在查询...');
  const payload = Object.fromEntries(new FormData(queryForm).entries());
  try {
    const result = await api('/api/seats/query', { method: 'POST', body: JSON.stringify(payload) });
    renderResults(result);
    setMessage(queryMessage, '查询完成', true);
    await loadHistory();
  } catch (error) {
    setMessage(queryMessage, error.message);
  }
});

watchForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (!queryForm.start_time.value || !queryForm.end_time.value) {
    setMessage(watchMessage, '请选择开始时间和结束时间');
    return;
  }
  if (queryForm.end_time.value <= queryForm.start_time.value) {
    setMessage(watchMessage, '结束时间必须晚于开始时间');
    return;
  }
  const confirmed = await confirmStartWatch();
  if (!confirmed) {
    setMessage(watchMessage, '');
    return;
  }
  const queryPayload = Object.fromEntries(new FormData(queryForm).entries());
  const watchPayload = Object.fromEntries(new FormData(watchForm).entries());
  const payload = { ...queryPayload, ...watchPayload };
  setMessage(watchMessage, '正在创建任务...');
  try {
    const data = await api('/api/watch-tasks', {
      method: 'POST',
      body: JSON.stringify(payload)
    });
    watchRows.innerHTML = '';
    await loadWatchTasks();
    setMessage(watchMessage, '蹲座位任务已开启', true);
  } catch (error) {
    setMessage(watchMessage, error.message);
  }
});

document.querySelectorAll('[data-tab]').forEach(button => {
  button.addEventListener('click', () => setTab(button.dataset.tab));
});

document.querySelectorAll('[data-modal-tab]').forEach(button => {
  button.addEventListener('click', () => setModalTab(button.dataset.modalTab));
});

historyModalClose.addEventListener('click', () => {
  historyModal.classList.add('hidden');
});

historyModal.addEventListener('click', event => {
  if (event.target === historyModal) {
    historyModal.classList.add('hidden');
  }
});

watchAlertCancel.addEventListener('click', () => {
  if (activeWatchAlert) dismissedAlertIds.add(Number(activeWatchAlert.id));
  closeWatchAlert();
  showNextWatchAlert();
});

watchAlertReserve.addEventListener('click', async () => {
  try {
    await ackWatchAlert('reserve');
  } catch (error) {
    setMessage(watchMessage, error.message);
  }
});

watchAlertConfirm.addEventListener('click', async () => {
  try {
    await ackWatchAlert('confirm');
  } catch (error) {
    setMessage(watchMessage, error.message);
  }
});

setTimeOptions();
loadMe().then(async data => {
  if (!authPanel.classList.contains('hidden')) return;
  await loadHistory();
  await loadWatchTasks();
}).catch(() => {});
setInterval(() => {
  if (!authPanel.classList.contains('hidden')) return;
  checkWatchAlerts();
}, WATCH_ALERT_POLL_INTERVAL_MS);
</script>
</body>
</html>
"""


def main() -> None:
    config.ensure_secret()
    database.init_db()
    start_watch_worker()
    server = ThreadingHTTPServer((config.APP_HOST, config.APP_PORT), AppHandler)
    print(f"Search Seat 已启动：http://127.0.0.1:{config.APP_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
