#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import concurrent.futures
from datetime import datetime, timedelta, timezone
import threading
import time
from typing import Optional

import config
import database
from services import user_service

BEIJING_TZ = timezone(timedelta(hours=8))
SYNC_WORKER_LOCK = threading.Lock()
AUTO_SYNC_WORKER_STARTED = False


def is_in_sync_window(
    dt: datetime,
    start_hour: int = None,
    end_hour: int = None,
) -> bool:
    """
    判断给定时间是否处于自动同步的时间窗口内（默认 08:00 到 23:00）。
    包含 08:00:00，至 23:00:00 结束（23:00 之后不再触发）。
    """
    if start_hour is None:
        start_hour = config.AUTO_SYNC_START_HOUR
    if end_hour is None:
        end_hour = config.AUTO_SYNC_END_HOUR

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BEIJING_TZ)
    else:
        dt = dt.astimezone(BEIJING_TZ)

    if dt.hour < start_hour or dt.hour > end_hour:
        return False
    if dt.hour == end_hour and (dt.minute > 0 or dt.second > 0 or dt.microsecond > 0):
        return False
    return True


def get_sync_slot_key(
    dt: datetime,
    interval_minutes: int = None,
    start_hour: int = None,
    end_hour: int = None,
) -> Optional[str]:
    """
    若给定时间匹配到自动同步的时间槽位（如 08:00, 08:15, 08:30 ... 23:00），
    则返回该槽位标识字符串，例如 '2026-09-16 08:15'；否则返回 None。
    """
    if interval_minutes is None:
        interval_minutes = config.AUTO_SYNC_INTERVAL_MINUTES

    if not is_in_sync_window(dt, start_hour=start_hour, end_hour=end_hour):
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BEIJING_TZ)
    else:
        dt = dt.astimezone(BEIJING_TZ)

    if dt.minute % interval_minutes != 0:
        return None

    return f"{dt.strftime('%Y-%m-%d %H')}:{dt.minute:02d}"


def fetch_users_for_auto_sync() -> list:
    """
    获取系统中所有需要自动同步预约的用户：
    未被禁用且学习通有效（session_valid = 1 且有 cookies_json）。
    """
    return database.fetch_all(
        """
        SELECT u.id, u.username, s.cookies_json
        FROM users u
        JOIN chaoxing_sessions s ON s.user_id = u.id
        WHERE u.disabled_at IS NULL
          AND s.session_valid = 1
          AND s.cookies_json IS NOT NULL
          AND s.cookies_json != ''
          AND s.cookies_json != '[]'
        ORDER BY u.id ASC
        """
    )


def sync_single_user(user: dict) -> bool:
    """
    同步单个用户的预约信息。成功返回 True，失败返回 False。
    """
    user_id = int(user["id"])
    cookies_json = user.get("cookies_json") or ""
    try:
        res = user_service.fetch_current_reserves_from_cookies(user_id, cookies_json, force=True)
        if res.get("error") and not res.get("reserves"):
            return False
        return True
    except Exception as exc:
        print(f"[AutoSync] 用户 {user.get('username') or user_id} 同步异常: {exc}")
        return False


def sync_all_users_reserves(max_workers: int = 3) -> dict:
    """
    对所有有效用户执行预约自动同步。
    使用线程池控制并发，避免瞬间大量请求触发超星风控限制。
    """
    if not SYNC_WORKER_LOCK.acquire(blocking=False):
        return {"skipped": True, "reason": "already_running"}

    start_time = time.time()
    try:
        users = fetch_users_for_auto_sync()
        total = len(users)
        if total == 0:
            return {"total": 0, "success": 0, "failed": 0, "elapsed": 0.0}

        success_count = 0
        failed_count = 0

        worker_count = max(1, min(max_workers, total))
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            results = list(executor.map(sync_single_user, users))

        for ok in results:
            if ok:
                success_count += 1
            else:
                failed_count += 1

        elapsed = time.time() - start_time
        print(f"[AutoSync] 自动同步完成: 共 {total} 个用户，成功 {success_count} 个，失败 {failed_count} 个，耗时 {elapsed:.2f} 秒")
        return {
            "total": total,
            "success": success_count,
            "failed": failed_count,
            "elapsed": round(elapsed, 2),
        }
    finally:
        SYNC_WORKER_LOCK.release()


def auto_sync_worker_loop(poll_interval: int = 10) -> None:
    """
    自动同步后台守护循环。
    每隔若干秒检查一次当前北京时间是否匹配到了新的整 15 分钟槽位。
    """
    last_executed_slot = None
    while True:
        try:
            if config.AUTO_SYNC_RESERVES_ENABLED:
                now = datetime.now(BEIJING_TZ)
                slot_key = get_sync_slot_key(now)
                if slot_key and slot_key != last_executed_slot:
                    last_executed_slot = slot_key
                    sync_all_users_reserves()
        except Exception as exc:
            print(f"[AutoSync] 调度循环异常: {exc}")
        time.sleep(poll_interval)


def start_auto_sync_worker() -> None:
    """
    启动后台自动同步守护线程（若配置开启）。
    """
    global AUTO_SYNC_WORKER_STARTED
    if not config.AUTO_SYNC_RESERVES_ENABLED:
        return
    if AUTO_SYNC_WORKER_STARTED:
        return
    AUTO_SYNC_WORKER_STARTED = True
    threading.Thread(target=auto_sync_worker_loop, name="seat-auto-sync-worker", daemon=True).start()
