#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import datetime, date
import hashlib
from json import dumps
import queue
import sys
import threading
import time
from urllib.parse import urlparse

import config
import database
import requests


def _get_db():
    app_mod = sys.modules.get("app")
    return getattr(app_mod, "database", database) if app_mod else database


def _get_app_attr(name, default):
    app_mod = sys.modules.get("app")
    return getattr(app_mod, name, default) if app_mod else default

GETUI_TOKEN_LOCK = threading.Lock()
GETUI_TOKEN = ""
GETUI_TOKEN_EXPIRE_MS = 0
GETUI_WATCH_NOTIFICATION_CHANNEL_ID = "seat_match_alerts_v3"
GETUI_ADMIN_NOTIFICATION_CHANNEL_ID = "admin_messages_v1"

WATCH_ALERT_SUBSCRIBERS = {}
WATCH_ALERT_SUBSCRIBERS_LOCK = threading.Lock()

WATCH_STATUS_LABELS = {
    "running": "蹲座中",
    "matched": "已蹲到",
    "expired": "已到点",
    "cancelled": "已取消",
}


def truthy(value) -> bool:
    return value in (True, 1, "1", "true", "on", "yes")


def json_default(value):
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def getui_configured() -> bool:
    return bool(config.GETUI_APP_ID and config.GETUI_APP_KEY and config.GETUI_MASTER_SECRET)


def public_push_device(row: dict) -> dict:
    return {
        "id": row["id"],
        "platform": row.get("platform") or "android",
        "cid": row.get("cid") or "",
        "device_name": row.get("device_name") or "",
        "sdk_version": row.get("sdk_version") or "",
        "app_version_code": row.get("app_version_code"),
        "app_version_name": row.get("app_version_name") or "",
        "notifications_enabled": bool(row.get("notifications_enabled")),
        "enabled": bool(row.get("enabled")),
        "last_seen_at": row.get("last_seen_at") or "",
    }


def save_push_device(user_id: int, payload: dict) -> dict:
    cid = str(payload.get("cid") or "").strip()
    if not cid:
        raise ValueError("缺少个推 CID")
    if len(cid) > 128:
        raise ValueError("个推 CID 过长")

    platform = str(payload.get("platform") or "android").strip().lower()[:20] or "android"
    device_name = str(payload.get("device_name") or "").strip()[:255] or None
    sdk_version = str(payload.get("sdk_version") or "").strip()[:64] or None
    app_version_name = str(payload.get("app_version_name") or "").strip()[:64] or None
    try:
        app_version_code = int(payload.get("app_version_code") or 0) or None
    except (TypeError, ValueError):
        app_version_code = None
    notifications_enabled = 1 if truthy(payload.get("notifications_enabled", True)) else 0

    db = _get_db()
    db.execute(
        """
        INSERT INTO push_devices
            (user_id, platform, cid, device_name, sdk_version, app_version_code,
             app_version_name, notifications_enabled, enabled, last_seen_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, NOW())
        ON DUPLICATE KEY UPDATE
            user_id = VALUES(user_id),
            platform = VALUES(platform),
            device_name = VALUES(device_name),
            sdk_version = VALUES(sdk_version),
            app_version_code = VALUES(app_version_code),
            app_version_name = VALUES(app_version_name),
            notifications_enabled = VALUES(notifications_enabled),
            enabled = 1,
            last_seen_at = NOW(),
            updated_at = NOW()
        """,
        (
            user_id,
            platform,
            cid,
            device_name,
            sdk_version,
            app_version_code,
            app_version_name,
            notifications_enabled,
        ),
    )
    db.execute(
        """
        UPDATE push_devices
        SET enabled = 0,
            updated_at = NOW()
        WHERE user_id = %s
          AND cid <> %s
          AND enabled = 1
        """,
        (user_id, cid),
    )
    row = db.fetch_one("SELECT * FROM push_devices WHERE cid = %s", (cid,))
    return public_push_device(row)


def disable_push_device(user_id: int, cid: str) -> int:
    cid = str(cid or "").strip()
    if not cid:
        return 0
    return _get_db().execute(
        """
        UPDATE push_devices
        SET enabled = 0,
            updated_at = NOW()
        WHERE user_id = %s
          AND cid = %s
          AND enabled = 1
        """,
        (user_id, cid),
    )


def fetch_push_cids(user_id: int) -> list:
    rows = database.fetch_all(
        """
        SELECT cid
        FROM push_devices
        WHERE user_id = %s
          AND platform = 'android'
          AND enabled = 1
          AND notifications_enabled = 1
        ORDER BY last_seen_at DESC
        LIMIT 1
        """,
        (user_id,),
    )
    return [row["cid"] for row in rows if row.get("cid")]


def getui_base_url() -> str:
    return config.GETUI_API_BASE.rstrip("/") + "/v2/" + config.GETUI_APP_ID


def getui_sign(appkey: str, timestamp_ms: str, master_secret: str) -> str:
    return hashlib.sha256(f"{appkey}{timestamp_ms}{master_secret}".encode("utf-8")).hexdigest()


def getui_auth_token(force_refresh: bool = False) -> str:
    global GETUI_TOKEN, GETUI_TOKEN_EXPIRE_MS
    if not getui_configured():
        return ""

    now_ms = int(time.time() * 1000)
    with GETUI_TOKEN_LOCK:
        if not force_refresh and GETUI_TOKEN and GETUI_TOKEN_EXPIRE_MS - 60000 > now_ms:
            return GETUI_TOKEN

        timestamp = str(now_ms)
        response = requests.post(
            getui_base_url() + "/auth",
            json={
                "sign": getui_sign(config.GETUI_APP_KEY, timestamp, config.GETUI_MASTER_SECRET),
                "timestamp": timestamp,
                "appkey": config.GETUI_APP_KEY,
            },
            headers={"Content-Type": "application/json;charset=utf-8"},
            timeout=10,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise RuntimeError(f"个推鉴权失败：{body.get('code')} {body.get('msg')}")
        data = body.get("data") or {}
        GETUI_TOKEN = str(data.get("token") or "")
        GETUI_TOKEN_EXPIRE_MS = int(data.get("expire_time") or 0)
        if not GETUI_TOKEN:
            raise RuntimeError("个推鉴权未返回 token")
        return GETUI_TOKEN


def getui_post(path: str, body: dict) -> dict:
    token = getui_auth_token(False)
    if not token:
        return {"skipped": True, "reason": "getui_not_configured"}

    def post_with_token(active_token: str):
        response = requests.post(
            getui_base_url() + path,
            json=body,
            headers={"Content-Type": "application/json;charset=utf-8", "token": active_token},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    result = post_with_token(token)
    if result.get("code") == 10001:
        result = post_with_token(getui_auth_token(True))
    if result.get("code") != 0:
        raise RuntimeError(f"个推推送失败：{result.get('code')} {result.get('msg')}")
    return result


def watch_alert_title(alert: dict) -> str:
    return f"{alert.get('room_name') or alert.get('room_id') or '座位'} 有可预约座位"


def watch_alert_body(alert: dict) -> str:
    matched = [str(item) for item in (alert.get("matched_seats") or [])]
    shown = matched[:8]
    suffix = f" 等 {len(matched)} 个" if len(matched) > len(shown) else ""
    seats = ", ".join(shown) + suffix if shown else "有空座"
    return f"{alert.get('day')} {alert.get('start_time')}-{alert.get('end_time')} · {seats}"


def getui_request_id(prefix: str, *parts) -> str:
    raw = "|".join(str(part) for part in parts) + f"|{time.time_ns()}"
    return (prefix + hashlib.sha256(raw.encode("utf-8")).hexdigest())[:32]


def getui_watch_payload(alert: dict) -> str:
    payload = {
        "type": "search_seat_watch_alert",
        "target_url": alert.get("reserve_url") or "/",
        "alert": {
            **alert,
            "matched_seats": (alert.get("matched_seats") or [])[:30],
        },
    }
    return dumps(payload, ensure_ascii=False, separators=(",", ":"))[:3000]


def push_getui_watch_alert_to_cid(cid: str, alert: dict) -> dict:
    title = watch_alert_title(alert)
    body = watch_alert_body(alert)
    request = {
        "request_id": getui_request_id("ss", alert.get("id"), cid),
        "settings": {
            "ttl": max(0, int(config.GETUI_PUSH_TTL_MS or 7200000)),
        },
        "audience": {
            "cid": [cid],
        },
        "push_message": {
            "notification": {
                "title": title[:50],
                "body": body[:256],
                "big_text": body[:512],
                "channel_id": GETUI_WATCH_NOTIFICATION_CHANNEL_ID,
                "channel_name": "座位命中提醒",
                "channel_level": 4,
                "click_type": "payload",
                "payload": getui_watch_payload(alert),
            },
        },
    }
    post_fn = _get_app_attr("getui_post", getui_post)
    return post_fn("/push/single/cid", request)


def send_getui_watch_alert(user_id: int, alert: dict) -> list:
    if not getui_configured():
        return []
    results = []
    for cid in fetch_push_cids(user_id):
        results.append(push_getui_watch_alert_to_cid(cid, alert))
    return results


def admin_push_payload(payload: dict) -> dict:
    user_ids = []
    seen = set()
    for item in (payload or {}).get("user_ids") or []:
        if not str(item).isdigit():
            continue
        user_id = int(item)
        if user_id > 0 and user_id not in seen:
            user_ids.append(user_id)
            seen.add(user_id)
    if not user_ids:
        raise ValueError("请选择要推送的用户")
    if len(user_ids) > 200:
        raise ValueError("一次最多选择 200 个用户")

    title = str((payload or {}).get("title") or "").strip() or "座位雷达"
    body = str((payload or {}).get("body") or "").strip()
    target_url = str((payload or {}).get("target_url") or "").strip()
    if not body:
        raise ValueError("请填写推送内容")
    if len(title) > 50:
        raise ValueError("推送标题不能超过 50 字")
    if len(body) > 256:
        raise ValueError("推送内容不能超过 256 字")
    if len(target_url) > 1024:
        raise ValueError("打开链接不能超过 1024 字")
    return {
        "user_ids": user_ids,
        "title": title,
        "body": body,
        "target_url": target_url,
    }


def fetch_push_devices_for_users(user_ids: list) -> list:
    if not user_ids:
        return []
    placeholders = ",".join(["%s"] * len(user_ids))
    rows = _get_db().fetch_all(
        f"""
        SELECT user_id, cid
        FROM push_devices
        WHERE user_id IN ({placeholders})
          AND platform = 'android'
          AND enabled = 1
          AND notifications_enabled = 1
        ORDER BY user_id ASC, last_seen_at DESC
        """,
        user_ids,
    )
    latest = []
    seen = set()
    for row in rows:
        user_id = int(row.get("user_id") or 0)
        if not user_id or user_id in seen:
            continue
        seen.add(user_id)
        latest.append(row)
    return latest


def getui_admin_payload(target_url: str) -> str:
    payload = {
        "type": "admin_message",
        "target_url": target_url or "/",
    }
    return dumps(payload, ensure_ascii=False, separators=(",", ":"))


def push_getui_admin_message_to_cid(cid: str, title: str, body: str, target_url: str) -> dict:
    request = {
        "request_id": getui_request_id("am", cid),
        "settings": {
            "ttl": max(0, int(config.GETUI_PUSH_TTL_MS or 7200000)),
        },
        "audience": {
            "cid": [cid],
        },
        "push_message": {
            "notification": {
                "title": title,
                "body": body,
                "big_text": body,
                "channel_id": GETUI_ADMIN_NOTIFICATION_CHANNEL_ID,
                "channel_name": "后台消息提醒",
                "channel_level": 4,
                "click_type": "payload",
                "payload": getui_admin_payload(target_url),
            },
        },
    }
    post_fn = _get_app_attr("getui_post", getui_post)
    return post_fn("/push/single/cid", request)


def send_admin_push_message(payload: dict) -> dict:
    normalized = admin_push_payload(payload)
    devices = fetch_push_devices_for_users(normalized["user_ids"])
    reachable_user_ids = sorted({int(row["user_id"]) for row in devices})
    result = {
        "configured": getui_configured(),
        "requested_users": len(normalized["user_ids"]),
        "reachable_users": len(reachable_user_ids),
        "target_cids": len(devices),
        "sent": 0,
        "failed": 0,
        "failures": [],
    }
    if not getui_configured():
        result["skipped"] = True
        result["reason"] = "getui_not_configured"
        return result

    for row in devices:
        try:
            push_getui_admin_message_to_cid(
                row["cid"],
                normalized["title"],
                normalized["body"],
                normalized["target_url"],
            )
            result["sent"] += 1
        except Exception as exc:
            result["failed"] += 1
            if len(result["failures"]) < 10:
                result["failures"].append(
                    {
                        "user_id": row["user_id"],
                        "cid": row["cid"],
                        "error": str(exc)[:300],
                    }
                )
    return result


def send_watch_webhook(task: dict, matched_seats: list, room_label_fn=None, reserve_url_fn=None) -> None:
    webhook_url = (task.get("webhook_url") or "").strip()
    if not webhook_url:
        return

    room_name = room_label_fn(task["room_id"]) if room_label_fn else task["room_id"]
    first_seat = matched_seats[0] if matched_seats else ""
    reserve_url = reserve_url_fn(str(task["room_id"]), str(task["fid_enc"]), str(task["day"]), first_seat) if reserve_url_fn else ""
    shown_seats = matched_seats[:30]
    suffix = f" 等 {len(matched_seats)} 个" if len(matched_seats) > len(shown_seats) else ""
    message = (
        "找到可预约座位了\n"
        f"房间：{room_name}\n"
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
                "room_name": room_name,
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
