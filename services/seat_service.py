#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import datetime, timedelta, timezone
from json import loads
import time
from urllib.parse import urlencode

import chaoxing
import config
import database

CHECKIN_WINDOW_MINUTES = 15
RESERVE_STATUS_LABELS = {
    0: "待履约",
    1: "使用中",
    2: "已完成",
    3: "已取消",
}


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


def reserve_status_label(item: dict, now_ms: int = None) -> str:
    status = item.get("status")
    if status == 0:
        current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        deadline = millisecond_value(item.get("expireTime")) or millisecond_value(item.get("endTime"))
        if deadline and deadline < current_ms:
            return "违约"
    return RESERVE_STATUS_LABELS.get(status, str(status))


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
        "available": item.get("status") == 0 and start_window_ms <= current_ms <= end_window_ms,
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
