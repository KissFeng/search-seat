#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import JSONDecodeError, dumps, loads
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
               cookies_updated_at, last_login_at
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
               next_check_at, expires_at, last_error, created_at
        FROM seat_watch_tasks
        WHERE user_id = %s
        ORDER BY id DESC
        LIMIT 50
        """,
        (user_id,),
    )
    return [public_watch_task(row) for row in rows]


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

    def current_user(self):
        return auth.current_user_from_header(self.headers.get("Cookie", ""))

    def require_user(self):
        user = self.current_user()
        if not user:
            self.send_json(401, {"error": "请先登录"})
            return None
        return user

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self.send_body(200, INDEX_HTML)
            return
        if path == "/reserve":
            self.handle_reserve_redirect(parsed.query)
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
                self.send_json(200, {"ok": True}, headers=[("Set-Cookie", auth.clear_session_cookie())])
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
            self.send_json(404, {"error": "not found"})
        except JSONDecodeError:
            self.send_json(400, {"error": "JSON 格式不正确"})
        except ValueError as exc:
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:
            self.send_json(500, {"error": str(exc)})

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
        save_chaoxing_session(user["id"], account, chaoxing.cookie_jar_to_json(session))
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


INDEX_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Search Seat</title>
  <style>
    :root { color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    * { box-sizing: border-box; }
    html { width: 100%; overflow-x: hidden; }
    body { margin: 0; background: #f3f5f7; color: #172033; overflow-x: hidden; }
    header { min-height: 56px; background: #ffffff; border-bottom: 1px solid #dfe4ea; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 0 22px; padding-left: max(22px, env(safe-area-inset-left)); padding-right: max(22px, env(safe-area-inset-right)); }
    h1 { margin: 0; font-size: 18px; font-weight: 700; }
    main { width: min(1480px, calc(100vw - 32px)); margin: 0 auto; padding: 20px 0; padding-bottom: max(20px, env(safe-area-inset-bottom)); }
    section { background: #fff; border: 1px solid #dfe4ea; border-radius: 8px; padding: 18px; margin-bottom: 16px; }
    h2 { margin: 0 0 14px; font-size: 16px; }
    form { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; align-items: end; }
    label { display: grid; gap: 6px; color: #4b5563; font-size: 13px; }
    input, select { width: 100%; border: 1px solid #cbd5e1; border-radius: 6px; padding: 9px 10px; font-size: 16px; color: #111827; background: #fff; min-height: 40px; }
    button, .button-link { border: 0; border-radius: 6px; background: #0f766e; color: #fff; padding: 10px 14px; font-size: 14px; cursor: pointer; text-decoration: none; text-align: center; display: inline-flex; align-items: center; justify-content: center; min-height: 40px; }
    button.secondary { background: #334155; }
    button.ghost { background: transparent; color: #334155; border: 1px solid #cbd5e1; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .auth { max-width: 520px; margin: 40px auto; }
    .auth form { grid-template-columns: 1fr; }
    .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; min-width: 0; }
    .muted { color: #64748b; font-size: 13px; }
    .ok { color: #166534; }
    .bad { color: #b91c1c; }
    .wide { grid-column: span 2; }
    .full { grid-column: 1 / -1; }
    .hidden { display: none !important; }
    .statusbar { display: grid; grid-template-columns: 1fr auto; gap: 12px; align-items: center; }
    .stats { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
    .stat { border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px; background: #f8fafc; }
    .stat b { display: block; font-size: 20px; margin-top: 3px; }
    .seat-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(72px, 1fr)); gap: 8px; align-items: stretch; }
    .pair-grid { grid-template-columns: repeat(auto-fill, minmax(128px, 1fr)); }
    .seat, .pair { border: 1px solid #cbd5e1; border-radius: 6px; background: #fff; color: #0f172a; padding: 8px 8px; text-align: center; text-decoration: none; font-variant-numeric: tabular-nums; font-size: 13px; line-height: 1.25; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .seat:hover, .pair:hover { border-color: #0f766e; color: #0f766e; }
    .pair { letter-spacing: 0; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #e5e7eb; padding: 9px 8px; font-size: 13px; text-align: left; }
    th { color: #475569; background: #f8fafc; font-weight: 600; }
    .tabs { display: flex; gap: 8px; margin-bottom: 12px; }
    .tab { background: #fff; color: #334155; border: 1px solid #cbd5e1; }
    .tab.active { background: #0f766e; color: #fff; border-color: #0f766e; }
    .message { min-height: 20px; margin: 10px 0 0; font-size: 13px; }
    .section-head { display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 12px; }
    .section-head h2 { margin: 0; }
    .section-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
    .table-actions { display: inline-flex; gap: 8px; align-items: center; white-space: nowrap; }
    .text-button { min-height: 0; padding: 5px 8px; border: 1px solid #cbd5e1; background: #fff; color: #0f172a; font-size: 12px; }
    .text-button.danger { color: #b91c1c; border-color: #fecaca; }
    .status-pill { display: inline-flex; align-items: center; border-radius: 999px; padding: 3px 8px; background: #e0f2fe; color: #075985; font-size: 12px; white-space: nowrap; }
    .status-pill.done { background: #dcfce7; color: #166534; }
    .status-pill.stop { background: #f1f5f9; color: #475569; }
    .check-cell { width: 38px; text-align: center; }
    .modal { position: fixed; inset: 0; z-index: 50; display: grid; place-items: center; padding: 22px; background: rgba(15, 23, 42, .42); }
    .modal-panel { width: min(1120px, 100%); max-height: min(780px, calc(100vh - 44px)); overflow: auto; background: #fff; border-radius: 8px; border: 1px solid #cbd5e1; box-shadow: 0 24px 80px rgba(15, 23, 42, .24); padding: 18px; }
    .modal-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
    .modal-head h2 { margin: 0 0 6px; }
    .modal-empty { border: 1px solid #fde68a; background: #fffbeb; color: #92400e; border-radius: 8px; padding: 14px; }
    @media (max-width: 820px) {
      header { padding: 8px 14px; padding-left: max(14px, env(safe-area-inset-left)); padding-right: max(14px, env(safe-area-inset-right)); align-items: flex-start; }
      h1 { line-height: 40px; }
      #topUser { justify-content: flex-end; gap: 8px; }
      #username { max-width: 46vw; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      main { width: calc(100vw - 16px); padding: 10px 0; padding-bottom: max(16px, env(safe-area-inset-bottom)); }
      section { padding: 14px; margin-bottom: 10px; }
      form { grid-template-columns: 1fr; gap: 10px; }
      .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
      .stat { padding: 10px; }
      .stat b { font-size: 18px; }
      .wide { grid-column: span 1; }
      .statusbar { grid-template-columns: 1fr; }
      .button-link, form > button { width: 100%; }
      .section-head { align-items: stretch; flex-direction: row; }
      .section-head h2 { align-self: center; }
      .section-actions { justify-content: flex-end; }
      .tabs { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
      .tab { width: 100%; padding-left: 8px; padding-right: 8px; }
      .seat-grid { grid-template-columns: repeat(auto-fill, minmax(58px, 1fr)); gap: 6px; }
      .pair-grid { grid-template-columns: repeat(auto-fill, minmax(104px, 1fr)); }
      .seat, .pair { padding: 9px 6px; min-height: 38px; }
      .responsive-table, .responsive-table tbody, .responsive-table tr, .responsive-table td { display: block; width: 100%; }
      .responsive-table thead { display: none; }
      .responsive-table tr { border: 1px solid #e2e8f0; border-radius: 8px; background: #fff; padding: 8px 10px; margin-bottom: 10px; }
      .responsive-table td { border-bottom: 1px solid #f1f5f9; padding: 8px 0; font-size: 13px; }
      .responsive-table td:last-child { border-bottom: 0; }
      .responsive-table td::before { content: attr(data-label); display: block; margin-bottom: 3px; color: #64748b; font-size: 12px; font-weight: 600; }
      .responsive-table td[data-label=""]::before { display: none; }
      .responsive-table td[colspan] { color: #64748b; }
      .responsive-table td[colspan]::before { display: none; }
      .check-cell { width: auto; text-align: left; }
      .table-actions { display: flex; gap: 8px; }
      .table-actions .text-button { flex: 1; }
      .modal { padding: 0; align-items: stretch; }
      .modal-panel { width: 100%; max-height: 100vh; border-radius: 0; border-left: 0; border-right: 0; padding: 14px; padding-bottom: max(14px, env(safe-area-inset-bottom)); }
      .modal-head { flex-direction: column; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Search Seat</h1>
    <div id="topUser" class="row hidden">
      <span class="muted" id="username"></span>
      <button class="ghost" id="logoutBtn" type="button">退出</button>
    </div>
  </header>

  <main>
    <section id="authPanel" class="auth">
      <h2>学习通登录</h2>
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
      <section>
        <div class="statusbar">
          <div>
            <h2>学习通账号</h2>
            <div id="cxStatus" class="muted"></div>
          </div>
          <a class="button-link" id="officialLink" href="#" target="_blank" rel="noreferrer">打开预约页</a>
        </div>
      </section>

      <section>
        <h2>查询座位</h2>
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
          <button type="submit">查询</button>
        </form>
        <div class="message" id="queryMessage"></div>
      </section>

      <section id="resultsPanel" class="hidden">
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
      </section>

      <section>
        <div class="section-head">
          <h2>蹲座位</h2>
          <div class="section-actions">
            <button class="text-button" id="refreshWatchBtn" type="button">刷新</button>
            <button class="text-button" id="toggleWatchBtn" type="button">显示全部</button>
          </div>
        </div>
        <form id="watchForm">
          <label>轮询间隔
            <input name="interval_seconds" type="number" min="15" max="3600" value="60" required>
          </label>
          <label class="wide">Webhook
            <input name="webhook_url" type="url" placeholder="https://">
          </label>
          <label class="row wide">
            <input name="save_webhook" type="checkbox" style="width: auto;">
            <span>保存 Webhook，下次自动填入</span>
          </label>
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
      </section>

      <section>
        <div class="section-head">
          <h2>查询历史</h2>
          <div class="section-actions">
            <button class="text-button" id="toggleHistoryBtn" type="button">显示全部</button>
            <button class="text-button danger" id="deleteSelectedHistoryBtn" type="button">删除选中</button>
          </div>
        </div>
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
      </section>
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
const historySelectAll = document.querySelector('#historySelectAll');
const deleteSelectedHistoryBtn = document.querySelector('#deleteSelectedHistoryBtn');
const refreshWatchBtn = document.querySelector('#refreshWatchBtn');
const toggleHistoryBtn = document.querySelector('#toggleHistoryBtn');
const toggleWatchBtn = document.querySelector('#toggleWatchBtn');
const resultsPanel = document.querySelector('#resultsPanel');
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
const allowedTimes = Array.from({ length: 15 }, (_, index) => `${String(index + 8).padStart(2, '0')}:00`);
const DEFAULT_HISTORY_LIMIT = 3;
let latestResult = null;
let officialIndexUrl = '';
let historyItems = [];
let watchItems = [];
let showAllHistory = false;
let showAllWatch = false;

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
  officialIndexUrl = defaults.official_index_url || '';
  updateOfficialLink();
}

function renderMe(data) {
  if (!data.user) {
    authPanel.classList.remove('hidden');
    appPanel.classList.add('hidden');
    topUser.classList.add('hidden');
    return;
  }
  authPanel.classList.add('hidden');
  appPanel.classList.remove('hidden');
  topUser.classList.remove('hidden');
  username.textContent = data.user.username;
  fillDefaults(data.defaults);
  if (data.chaoxing.bound) {
    const validText = data.chaoxing.session_valid ? '已保存 Cookie' : 'Cookie 可能已失效';
    cxStatus.innerHTML = `${escapeHtml(data.chaoxing.account)} · ${validText} · ${escapeHtml(data.chaoxing.cookies_updated_at || '')}`;
  } else {
    cxStatus.textContent = '未登录学习通';
  }
  updateOfficialLink();
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
  historySelectAll.checked = false;
  toggleHistoryBtn.classList.toggle('hidden', historyItems.length <= DEFAULT_HISTORY_LIMIT);
  toggleHistoryBtn.textContent = showAllHistory ? '收起' : `显示全部 ${historyItems.length} 条`;
}

async function loadWatchTasks() {
  const data = await api('/api/watch-tasks', { method: 'GET', headers: {} });
  watchItems = data.tasks || [];
  renderWatchRows();
}

function renderWatchRows() {
  const visibleWatch = showAllWatch ? watchItems : watchItems.slice(0, DEFAULT_HISTORY_LIMIT);
  watchRows.innerHTML = visibleWatch.map(row => {
    const statusClass = row.status === 'matched' ? 'done' : row.status === 'running' ? '' : 'stop';
    const actions = row.status === 'running'
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
    return `<tr>
      <td data-label="创建时间">${escapeHtml(row.created_at)}</td>
      <td data-label="日期">${escapeHtml(row.day)}</td>
      <td data-label="时段">${escapeHtml(row.start_time)}-${escapeHtml(row.end_time)}</td>
      <td data-label="房间">${escapeHtml(row.room_name || row.room_id)}</td>
      <td data-label="筛选">${escapeHtml(filters)}</td>
      <td data-label="状态"><span class="status-pill ${statusClass}">${escapeHtml(row.status_label)}</span>${error}</td>
      <td data-label="上次检查">${escapeHtml(row.last_checked_at || '')}</td>
      <td data-label="命中座位">${escapeHtml(matchedText)}</td>
      <td data-label="操作"><span class="table-actions">${actions}</span></td>
    </tr>`;
  }).join('') || '<tr><td colspan="9">暂无任务</td></tr>';
  toggleWatchBtn.classList.toggle('hidden', watchItems.length <= DEFAULT_HISTORY_LIMIT);
  toggleWatchBtn.textContent = showAllWatch ? '收起' : `显示全部 ${watchItems.length} 条`;
}

function renderResults(result) {
  latestResult = result;
  resultsPanel.classList.remove('hidden');
  document.querySelector('#statTotal').textContent = result.summary.total;
  document.querySelector('#statAvailable').textContent = result.summary.available;
  document.querySelector('#statOccupied').textContent = result.summary.occupied;
  document.querySelector('#statFiltered').textContent = result.summary.filtered || 0;
  document.querySelector('#statPairs').textContent = result.summary.pairs;
  renderSeatLists(result, seatGrid, pairGrid);
  setTab('seats');
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
  return Array.from(document.querySelectorAll('.history-check:checked')).map(item => Number(item.value));
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
  document.querySelectorAll('.tab').forEach(button => button.classList.toggle('active', button.dataset.tab === tab));
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
  setMessage(authMessage, '正在登录学习通...');
  const payload = Object.fromEntries(new FormData(authForm).entries());
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
  await api('/api/logout', { method: 'POST', body: '{}' });
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
  renderHistoryRows();
});
toggleWatchBtn.addEventListener('click', () => {
  showAllWatch = !showAllWatch;
  renderWatchRows();
});
refreshWatchBtn.addEventListener('click', async () => {
  try {
    await loadWatchTasks();
    setMessage(watchMessage, '已刷新', true);
  } catch (error) {
    setMessage(watchMessage, error.message);
  }
});

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

setTimeOptions();
loadMe().then(async data => {
  if (!authPanel.classList.contains('hidden')) return;
  await loadHistory();
  await loadWatchTasks();
}).catch(() => {});
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
