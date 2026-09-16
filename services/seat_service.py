#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import datetime, timedelta, timezone
from json import loads
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import chaoxing
import config
import database

CHECKIN_WINDOW_MINUTES = 15
RESERVE_STATUS_LABELS = {
    0: "待履约",
    1: "使用中",
    2: "已完成",
    3: "暂离中",
    4: "已取消",
    5: "监督中",
    -1: "不可选",
    -2: "已锁定",
    -3: "使用中",
}


def reserve_status_badge_type(label: str) -> str:
    if label == "使用中":
        return "success"
    if label == "待履约":
        return "warning"
    if label in ("违约", "已取消", "已锁定", "不可选"):
        return "danger"
    if label == "暂离中":
        return "info"
    if label == "监督中":
        return "purple"
    return "neutral"



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


def millisecond_time(value) -> str:
    try:
        timestamp = int(value) / 1000
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8))).strftime("%H:%M")


def millisecond_value(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def reserve_status_label(item: Any, now_ms: int = None) -> str:
    if isinstance(item, dict):
        status = item.get("status")
        item_dict = item
    else:
        try:
            status = int(item)
            item_dict = {"status": status}
        except (TypeError, ValueError):
            return "未知"

    if status == 0:
        current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        start_ms = millisecond_value(item_dict.get("startTime"))
        deadline = millisecond_value(item_dict.get("expireTime")) or (
            start_ms + CHECKIN_WINDOW_MINUTES * 60 * 1000 if start_ms else 0
        )
        if deadline and deadline < current_ms:
            return "违约"
    return RESERVE_STATUS_LABELS.get(status, str(status) if status is not None else "未知")



def timetable_week_num_from_payload(payload: dict) -> str:
    week_num = str((payload or {}).get("week_num") or config.CHAOXING_TIMETABLE_DEFAULT_WEEK).strip()
    if week_num and (not week_num.isdigit() or not 1 <= int(week_num) <= 30):
        raise ValueError("周次必须是 1 到 30 之间的数字")
    return week_num


def room_seat_config(room: dict) -> tuple:
    seat_min = int(room.get("seat_min") or config.SEAT_MIN)
    seat_max = int(room.get("seat_max") or config.SEAT_MAX)
    seat_width = int(room.get("seat_width") or config.SEAT_WIDTH)
    if seat_min < 1 or seat_max < seat_min:
        raise ValueError("座位号范围不正确")
    if seat_width < 1 or seat_width > 8:
        raise ValueError("座位号补零宽度不正确")
    return seat_min, seat_max, seat_width


def public_room(room: dict) -> dict:
    seat_min, seat_max, seat_width = room_seat_config(room)
    return {
        "label": room["label"],
        "room_id": str(room["room_id"]),
        "seat_min": seat_min,
        "seat_max": seat_max,
        "seat_width": seat_width,
        "seat_count": seat_max - seat_min + 1,
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


def room_seat_width(room_id: str) -> int:
    for room in config.ROOMS:
        if str(room.get("room_id")) == str(room_id):
            return int(room.get("seat_width") or config.SEAT_WIDTH)
    return int(config.SEAT_WIDTH)


def public_seat_num(room_id: str, value) -> str:
    seat_num = str(value or "").strip()
    if not seat_num:
        return ""
    if seat_num.isdigit() and int(seat_num) > 0:
        return seat_num.zfill(room_seat_width(room_id))
    return seat_num


def checkin_url(room_id: str, seat_num: str) -> str:
    if not room_id or not seat_num:
        return ""
    return "https://office.chaoxing.com/front/apps/seat/code?" + urlencode(
        {"id": room_id, "seatNum": seat_num}
    )


def reserve_checkin_window(item: dict, now_ms: int = None) -> dict:
    status = item.get("status")
    # 监督中（status == 5）允许立即签到以解除监督状态
    if status == 5:
        return {"start_time": "", "end_time": "", "available": True}
    start_ms = millisecond_value(item.get("startTime"))
    if not start_ms:
        return {"start_time": "", "end_time": "", "available": False}
    window_ms = CHECKIN_WINDOW_MINUTES * 60 * 1000
    start_window_ms = start_ms - window_ms
    end_window_ms = start_ms + window_ms
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return {
        "start_time": millisecond_time(start_window_ms),
        "end_time": millisecond_time(end_window_ms),
        "available": status == 0 and start_window_ms <= current_ms <= end_window_ms,
    }


def local_reserve_path(room_id: str, day: str, seat: str = "") -> str:
    query = {"room_id": room_id, "day": day}
    if seat:
        query["seat"] = seat
    return "/reserve?" + urlencode(query)


def official_seat_index_url(fid_enc: str = "") -> str:
    return "https://office.chaoxing.com/front/apps/seat/index?" + urlencode(
        {"fidEnc": fid_enc or config.FID_ENC}
    )


def public_current_reserve(item: dict, now_ms: int = None) -> dict:
    room_id = str(item.get("roomId") or "")
    seat_num = public_seat_num(room_id, item.get("seatNum"))
    day = str(item.get("today") or "").strip()
    room_parts = [
        str(item.get("secondLevelName") or "").strip(),
        str(item.get("thirdLevelName") or "").strip(),
    ]
    room_name = "-".join(part for part in room_parts if part) or room_label(room_id)
    status = item.get("status")
    start_time = millisecond_time(item.get("startTime"))
    end_time = millisecond_time(item.get("endTime"))
    time_range = f"{start_time}-{end_time}" if start_time and end_time else ""
    checkin = reserve_checkin_window(item, now_ms)
    return {
        "id": item.get("id"),
        "room_id": room_id,
        "room_name": room_name,
        "seat_num": seat_num,
        "day": day,
        "start_time": start_time,
        "end_time": end_time,
        "time_range": time_range,
        "status": status,
        "status_label": reserve_status_label(item, now_ms),
        "reserve_url": local_reserve_path(room_id, day, seat_num) if room_id and day else "",
        "checkin_url": checkin_url(room_id, seat_num),
        "checkin_available": checkin["available"],
        "checkin_time_range": (
            f"{checkin['start_time']}-{checkin['end_time']}"
            if checkin["start_time"] and checkin["end_time"]
            else ""
        ),
    }


def public_current_reserves(result: dict) -> list:
    reserves = (result or {}).get("data", {}).get("curReserves", [])
    if not isinstance(reserves, list):
        return []
    now_ms = int(time.time() * 1000)
    return [public_current_reserve(item, now_ms) for item in reserves if isinstance(item, dict)]


def public_reserve_records(result: dict, limit: int = 10) -> list:
    records = (result or {}).get("data", {}).get("reserveList", [])
    if not isinstance(records, list):
        return []
    now_ms = int(time.time() * 1000)
    return [
        public_current_reserve(item, now_ms)
        for item in records[:limit]
        if isinstance(item, dict)
    ]


def seat_range(start: int, end: int, width: int) -> set:
    return {str(value).zfill(width) for value in range(start, end + 1)}


def filter_unwanted_seats(room_id: str, available: list, width: int, payload: dict) -> tuple:
    filtered = set()
    applied = []

    if (payload or {}).get("ignore_no_power") and room_id == "12818":
        seats = seat_range(168, 211, width)
        filtered.update(seats)
        applied.append({"name": "忽略无电源座位", "count": len(seats & set(available))})

    if (payload or {}).get("ignore_sunny"):
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


def fetch_user_profiles_by_uids(uids: list) -> dict:
    if not uids:
        return {}
    clean_uids = list({int(u) for u in uids if str(u).isdigit()})
    if not clean_uids:
        return {}
    format_strings = ",".join(["%s"] * len(clean_uids))
    rows = database.fetch_all(
        f"SELECT uid, real_name, account, avatar_url FROM seat_user_profiles WHERE uid IN ({format_strings})",
        tuple(clean_uids),
    )
    return {row["uid"]: row for row in rows}


def save_or_update_user_profile(
    uid: int,
    real_name: str = None,
    account: str = None,
    source: str = "sync",
) -> None:
    if not uid:
        return
    avatar_url = f"https://photo.chaoxing.com/p/{uid}_80"
    database.execute(
        """
        INSERT INTO seat_user_profiles (uid, real_name, account, avatar_url, source)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            real_name = COALESCE(%s, real_name),
            account = COALESCE(%s, account),
            avatar_url = COALESCE(avatar_url, VALUES(avatar_url)),
            source = %s
        """,
        (uid, real_name, account, avatar_url, source, real_name, account, source),
    )


def save_occupied_reservations(room_id: str, day: str, reserves: list) -> int:
    if not reserves:
        return 0
    uids = [item.get("uid") for item in reserves if item.get("uid")]
    profiles = fetch_user_profiles_by_uids(uids)

    count = 0
    for item in reserves:
        reserve_id = item.get("id")
        if not reserve_id:
            continue
        seat_num = str(item.get("seatNum") or "")
        start_ms = millisecond_value(item.get("startTime"))
        end_ms = millisecond_value(item.get("endTime"))
        start_t = millisecond_time(start_ms)
        end_t = millisecond_time(end_ms)
        status = item.get("status") if item.get("status") is not None else 0
        uid = int(item.get("uid")) if str(item.get("uid", "")).isdigit() else 0
        user_name = profiles.get(uid, {}).get("real_name") if uid else None

        database.execute(
            """
            INSERT INTO seat_reservations
                (reserve_id, room_id, seat_num, day, start_time, end_time,
                 start_time_ms, end_time_ms, status, uid, user_name)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                room_id = VALUES(room_id),
                seat_num = VALUES(seat_num),
                day = VALUES(day),
                start_time = VALUES(start_time),
                end_time = VALUES(end_time),
                start_time_ms = VALUES(start_time_ms),
                end_time_ms = VALUES(end_time_ms),
                status = VALUES(status),
                uid = VALUES(uid),
                user_name = COALESCE(VALUES(user_name), user_name)
            """,
            (
                reserve_id,
                room_id,
                seat_num,
                day,
                start_t,
                end_t,
                start_ms,
                end_ms,
                status,
                uid,
                user_name,
            ),
        )
        count += 1
    return count


def build_occupied_reserves(reserves: list, room_id: str, day: str) -> tuple:
    uids = [item.get("uid") for item in reserves if item.get("uid")]
    profiles = fetch_user_profiles_by_uids(uids)
    now_ms = int(time.time() * 1000)

    occupied_details = []
    occupied_by_seat = {}

    for item in reserves:
        reserve_id = item.get("id")
        seat_num = str(item.get("seatNum") or "")
        start_ms = millisecond_value(item.get("startTime"))
        end_ms = millisecond_value(item.get("endTime"))
        start_t = millisecond_time(start_ms)
        end_t = millisecond_time(end_ms)
        status = item.get("status") if item.get("status") is not None else 0
        status_label = reserve_status_label(item, now_ms)
        badge_type = reserve_status_badge_type(status_label)
        uid = int(item.get("uid")) if str(item.get("uid", "")).isdigit() else 0
        profile = profiles.get(uid) or profiles.get(str(uid)) or {}
        user_name = profile.get("real_name") or (f"超星用户 {uid}" if uid else "未知用户")
        avatar_url = f"https://photo.chaoxing.com/p/{uid}_80" if uid else ""

        detail = {
            "reserve_id": reserve_id,
            "room_id": room_id,
            "room_name": room_label(room_id),
            "seat_num": seat_num,
            "day": day,
            "start_time": start_t,
            "end_time": end_t,
            "time_range": f"{start_t}-{end_t}" if start_t and end_t else "",
            "status": status,
            "status_label": status_label,
            "badge_type": badge_type,
            "uid": uid,
            "user_name": user_name,
            "account": profile.get("account") or "",
            "avatar_url": avatar_url,
        }
        occupied_details.append(detail)
        occupied_by_seat.setdefault(seat_num, []).append(detail)

    return occupied_details, occupied_by_seat


def build_seat_response(
    room: dict,
    day: str,
    start_time: str,
    end_time: str,
    result: dict,
    payload: dict,
    include_pairs: bool = False,
    include_occupied_details: bool = False,
) -> dict:
    room_id = str(room["room_id"])
    seat_min, seat_max, seat_width = room_seat_config(room)
    all_seats = chaoxing.build_all_seats(seat_min, seat_max, seat_width)
    raw_reserves = chaoxing.parse_seat_reserves(result, seat_width)
    occupied = sorted({item["seatNum"] for item in raw_reserves if item.get("seatNum")})
    raw_available = [seat for seat in all_seats if seat not in set(occupied)]
    available, filtered_seats, applied_filters = filter_unwanted_seats(room_id, raw_available, seat_width, payload)

    pairs = []
    if include_pairs:
        raw_pairs = chaoxing.find_adjacent_pairs(available, seat_width)
        pairs = [
            {
                "seats": pair,
                "url": local_reserve_path(room_id, day, "-".join(pair)),
            }
            for pair in raw_pairs
        ]

    occupied_details = []
    occupied_by_seat = {}
    if include_occupied_details:
        occupied_details, occupied_by_seat = build_occupied_reserves(raw_reserves, room_id, day)

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
        "occupied_details": occupied_details,
        "occupied_by_seat": occupied_by_seat,
        "filtered": filtered_seats,
        "filters": applied_filters,
        "pairs": pairs,
        "summary": {
            "total": len(all_seats),
            "available": len(available),
            "occupied": len(occupied),
            "filtered": len(filtered_seats),
            "pairs": len(pairs),
        },
    }


def query_seat_reservations(
    room_id: str = None,
    day: str = None,
    seat_num: str = None,
    limit: int = 200,
) -> list:
    conditions = []
    params = []
    if room_id:
        conditions.append("r.room_id = %s")
        params.append(room_id)
    if day:
        conditions.append("r.day = %s")
        params.append(day)
    if seat_num:
        conditions.append("r.seat_num = %s")
        params.append(seat_num)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"""
        SELECT r.id, r.reserve_id, r.room_id, r.seat_num, r.day, r.start_time, r.end_time,
               r.start_time_ms, r.end_time_ms, r.status, r.uid,
               COALESCE(p.real_name, r.user_name) AS real_name,
               p.account, r.created_at, r.updated_at
        FROM seat_reservations r
        LEFT JOIN seat_user_profiles p ON r.uid = p.uid
        {where_clause}
        ORDER BY r.day DESC, r.start_time_ms DESC, r.seat_num ASC
        LIMIT %s
    """
    params.append(limit)
    rows = database.fetch_all(sql, tuple(params))
    now_ms = int(time.time() * 1000)

    results = []
    for row in rows:
        status_label = reserve_status_label(
            {"status": row.get("status"), "startTime": row.get("start_time_ms"), "endTime": row.get("end_time_ms")},
            now_ms,
        )
        results.append(
            {
                "id": row.get("id"),
                "reserve_id": row.get("reserve_id"),
                "room_id": row.get("room_id"),
                "room_name": room_label(row.get("room_id")),
                "seat_num": row.get("seat_num"),
                "day": str(row.get("day") or ""),
                "start_time": row.get("start_time"),
                "end_time": row.get("end_time"),
                "time_range": f"{row.get('start_time')}-{row.get('end_time')}",
                "status": row.get("status"),
                "status_label": status_label,
                "badge_type": reserve_status_badge_type(status_label),
                "uid": row.get("uid"),
                "user_name": row.get("real_name") or row.get("user_name") or f"超星用户 {row.get('uid') or ''}",
                "account": row.get("account") or "",
                "avatar_url": f"https://photo.chaoxing.com/p/{row['uid']}_80" if row.get("uid") else "",
                "updated_at": str(row.get("updated_at") or ""),
            }
        )
    return results


def query_user_reservations(keyword: str, day: str = None, limit: int = 100) -> list:
    cleaned = str(keyword or "").strip()
    if not cleaned:
        return []
    conditions = []
    params = []
    if cleaned.isdigit():
        conditions.append("(r.uid = %s OR p.account LIKE %s)")
        params.extend([int(cleaned), f"%{cleaned}%"])
    else:
        conditions.append("(p.real_name LIKE %s OR r.user_name LIKE %s)")
        params.extend([f"%{cleaned}%", f"%{cleaned}%"])

    if day:
        conditions.append("r.day = %s")
        params.append(day)

    where_clause = f"WHERE {' AND '.join(conditions)}"
    sql = f"""
        SELECT r.id, r.reserve_id, r.room_id, r.seat_num, r.day, r.start_time, r.end_time,
               r.start_time_ms, r.end_time_ms, r.status, r.uid,
               COALESCE(p.real_name, r.user_name) AS real_name,
               p.account, r.created_at, r.updated_at
        FROM seat_reservations r
        LEFT JOIN seat_user_profiles p ON r.uid = p.uid
        {where_clause}
        ORDER BY r.day DESC, r.start_time_ms DESC
        LIMIT %s
    """
    params.append(limit)
    rows = database.fetch_all(sql, tuple(params))
    now_ms = int(time.time() * 1000)

    results = []
    for row in rows:
        status_label = reserve_status_label(
            {"status": row.get("status"), "startTime": row.get("start_time_ms"), "endTime": row.get("end_time_ms")},
            now_ms,
        )
        results.append(
            {
                "id": row.get("id"),
                "reserve_id": row.get("reserve_id"),
                "room_id": row.get("room_id"),
                "room_name": room_label(row.get("room_id")),
                "seat_num": row.get("seat_num"),
                "day": str(row.get("day") or ""),
                "start_time": row.get("start_time"),
                "end_time": row.get("end_time"),
                "time_range": f"{row.get('start_time')}-{row.get('end_time')}",
                "status": row.get("status"),
                "status_label": status_label,
                "badge_type": reserve_status_badge_type(status_label),
                "uid": row.get("uid"),
                "user_name": row.get("real_name") or row.get("user_name") or f"超星用户 {row.get('uid') or ''}",
                "account": row.get("account") or "",
                "avatar_url": f"https://photo.chaoxing.com/p/{row['uid']}_80" if row.get("uid") else "",
                "updated_at": str(row.get("updated_at") or ""),
            }
        )
    return results


def fetch_seat_user_profiles(keyword: str = None, limit: int = 50) -> list:
    if keyword:
        cleaned = str(keyword).strip()
        if cleaned.isdigit():
            sql = "SELECT uid, real_name, account, avatar_url, source, updated_at FROM seat_user_profiles WHERE uid = %s OR account LIKE %s ORDER BY updated_at DESC LIMIT %s"
            params = (int(cleaned), f"%{cleaned}%", limit)
        else:
            sql = "SELECT uid, real_name, account, avatar_url, source, updated_at FROM seat_user_profiles WHERE real_name LIKE %s ORDER BY updated_at DESC LIMIT %s"
            params = (f"%{cleaned}%", limit)
    else:
        sql = "SELECT uid, real_name, account, avatar_url, source, updated_at FROM seat_user_profiles ORDER BY updated_at DESC LIMIT %s"
        params = (limit,)
    rows = database.fetch_all(sql, params)
    for r in rows:
        r["updated_at"] = str(r["updated_at"])
    return rows



def save_query_history(user_id: int, payload: dict) -> int:
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


def delete_query_history(user_id: int, history_id: int = None) -> int:
    if history_id:
        return database.execute(
            "DELETE FROM seat_query_history WHERE id = %s AND user_id = %s",
            (history_id, user_id),
        )
    return database.execute(
        "DELETE FROM seat_query_history WHERE user_id = %s",
        (user_id,),
    )


def fetch_user_query_history(user_id: int, limit: int = 20) -> list:
    rows = database.fetch_all(
        """
        SELECT id, room_id, fid_enc, day, start_time, end_time,
               available_count, occupied_count, pair_count, created_at
        FROM seat_query_history
        WHERE user_id = %s
        ORDER BY created_at DESC, id DESC
        LIMIT %s
        """,
        (user_id, limit),
    )
    history = []
    for row in rows:
        history.append(
            {
                "id": row["id"],
                "room_id": row["room_id"],
                "room_name": room_label(row["room_id"]),
                "day": str(row["day"]),
                "start_time": row["start_time"],
                "end_time": row["end_time"],
                "available_count": row["available_count"],
                "occupied_count": row["occupied_count"],
                "pair_count": row["pair_count"],
                "created_at": str(row["created_at"]),
                "detail_url": f"/api/history/{row['id']}",
            }
        )
    return history
