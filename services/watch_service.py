#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import concurrent.futures
from datetime import datetime, timedelta
from json import dumps, loads
import threading
import time

import chaoxing
import config
import database
from services import push_service, seat_service, user_service

WATCH_STATUS_LABELS = {
    "running": "蹲座中",
    "matched": "已蹲到",
    "expired": "已到点",
    "cancelled": "已取消",
}
WATCH_WORKER_STARTED = False
WATCH_WORKER_LOCK = threading.Lock()


def validate_watch_interval(value) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = config.WATCH_INTERVAL_SECONDS
    if seconds < 10 or seconds > 3600:
        raise ValueError("轮询间隔必须在 10 到 3600 秒之间")
    return seconds


def watch_expires_at(day: str, start_time: str) -> datetime:
    return datetime.strptime(f"{day} {start_time}", "%Y-%m-%d %H:%M")


def public_watch_task(row: dict) -> dict:
    matched_seats = loads(row.get("matched_seats_json") or "[]") if row.get("matched_seats_json") else []
    reserve_url = seat_service.local_reserve_path(row["room_id"], row["day"], matched_seats[0]) if matched_seats else ""
    return {
        "id": row["id"],
        "room_id": row["room_id"],
        "room_name": seat_service.room_label(row["room_id"]),
        "day": str(row["day"]),
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "ignore_no_power": bool(row["ignore_no_power"]),
        "ignore_sunny": bool(row["ignore_sunny"]),
        "interval_seconds": row["interval_seconds"],
        "status": row["status"],
        "status_label": WATCH_STATUS_LABELS.get(row["status"], row["status"]),
        "matched_seats": matched_seats,
        "reserve_url": reserve_url,
        "reminder_ack_at": str(row.get("reminder_ack_at") or ""),
        "reminder_action": row.get("reminder_action") or "",
        "last_checked_at": str(row["last_checked_at"] or ""),
        "next_check_at": str(row["next_check_at"] or ""),
        "expires_at": str(row["expires_at"] or ""),
        "last_error": row.get("last_error") or "",
        "created_at": str(row["created_at"] or ""),
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


def cancel_watch_task(task_id: int, user_id: int) -> int:
    return database.execute(
        """
        UPDATE seat_watch_tasks
        SET status = 'cancelled', next_check_at = NULL, updated_at = NOW()
        WHERE id = %s AND user_id = %s AND status = 'running'
        """,
        (task_id, user_id),
    )


def build_watch_alert(task: dict, matched_seats: list) -> dict:
    return {
        "id": task["id"],
        "room_id": task["room_id"],
        "room_name": seat_service.room_label(task["room_id"]),
        "day": str(task["day"]),
        "start_time": task["start_time"],
        "end_time": task["end_time"],
        "status": "matched",
        "status_label": WATCH_STATUS_LABELS["matched"],
        "matched_seats": matched_seats,
        "reserve_url": seat_service.local_reserve_path(str(task["room_id"]), str(task["day"]), matched_seats[0]) if matched_seats else "",
    }


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

    room = seat_service.get_room(str(task["room_id"]))
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
    response = seat_service.build_seat_response(room, str(task["day"]), task["start_time"], task["end_time"], result, payload)
    user_service.update_chaoxing_cookies(task["user_id"], chaoxing.cookie_jar_to_json(session))
    matched = [item["seat"] for item in response["available"]]
    if matched:
        finish_watch_task(task["id"], "matched", matched)
        alert = build_watch_alert(task, matched)
        push_service.publish_watch_alert(task["user_id"], alert)
        try:
            push_service.send_getui_watch_alert(task["user_id"], alert)
        except Exception as exc:
            database.execute(
                "UPDATE seat_watch_tasks SET last_error = %s, updated_at = NOW() WHERE id = %s",
                (f"个推发送失败：{str(exc)[:1800]}", task["id"]),
            )
        try:
            push_service.send_watch_webhook(
                task,
                matched,
                room_label_fn=seat_service.room_label,
                reserve_url_fn=chaoxing.build_reserve_url,
            )
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
        if not rows:
            return

        def _safe_process(task):
            try:
                process_watch_task(task)
            except Exception as exc:
                schedule_watch_retry(
                    task["id"],
                    int(task.get("interval_seconds") or config.WATCH_INTERVAL_SECONDS),
                    str(exc),
                )

        # 并发执行任务，避免单个任务耗时过长阻塞整批
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(rows))) as executor:
            list(executor.map(_safe_process, rows))
    finally:
        WATCH_WORKER_LOCK.release()


def watch_worker_loop() -> None:
    while True:
        try:
            run_due_watch_tasks()
        except Exception as exc:
            # 记录异常但保持循环活性
            pass
        time.sleep(10)


def start_watch_worker() -> None:
    global WATCH_WORKER_STARTED
    if WATCH_WORKER_STARTED:
        return
    WATCH_WORKER_STARTED = True
    threading.Thread(target=watch_worker_loop, name="seat-watch-worker", daemon=True).start()
