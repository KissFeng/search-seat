#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import datetime

import database


def parse_chat_datetime(value: str, field_name: str) -> str:
    value = str(value or "").strip().replace("T", " ")
    if len(value) == 16:
        value = f"{value}:00"
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError(f"{field_name} 必须是 YYYY-MM-DD HH:MM 格式") from exc
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def chat_message_text_from_payload(payload: dict) -> str:
    content = str((payload or {}).get("content") or "").strip()
    if not content:
        raise ValueError("请输入聊天内容")
    if len(content) > 500:
        raise ValueError("聊天内容不能超过 500 字")
    return content


def public_chat_message(row: dict) -> dict:
    author_name = str(row.get("cx_user_name") or "").strip() or str(row.get("username") or "").strip() or "匿名用户"
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "author_name": author_name,
        "avatar": "default",
        "content": row.get("content") or "",
        "created_at": row.get("created_at") or "",
    }


def admin_chat_delete_scope(payload: dict) -> dict:
    ids = []
    seen = set()
    for item in (payload or {}).get("ids") or []:
        if not str(item).isdigit():
            continue
        message_id = int(item)
        if message_id > 0 and message_id not in seen:
            ids.append(message_id)
            seen.add(message_id)
    if ids:
        return {"mode": "ids", "ids": ids}

    start_at = str((payload or {}).get("start_at") or "").strip()
    end_at = str((payload or {}).get("end_at") or "").strip()
    if start_at and end_at:
        start_at = parse_chat_datetime(start_at, "开始时间")
        end_at = parse_chat_datetime(end_at, "结束时间")
        if end_at < start_at:
            raise ValueError("结束时间必须晚于开始时间")
        return {"mode": "time", "start_at": start_at, "end_at": end_at}

    raise ValueError("请选择要删除的消息或时间段")


def fetch_chat_messages(limit: int = 100) -> list:
    rows = database.fetch_all(
        """
        SELECT m.id, m.user_id, m.content, m.created_at,
               u.username, s.cx_user_name
        FROM chat_messages m
        JOIN users u ON u.id = m.user_id
        LEFT JOIN chaoxing_sessions s ON s.user_id = m.user_id
        ORDER BY m.id DESC
        LIMIT %s
        """,
        (limit,),
    )
    return [public_chat_message(row) for row in reversed(rows)]


def fetch_chat_message(message_id: int):
    row = database.fetch_one(
        """
        SELECT m.id, m.user_id, m.content, m.created_at,
               u.username, s.cx_user_name
        FROM chat_messages m
        JOIN users u ON u.id = m.user_id
        LEFT JOIN chaoxing_sessions s ON s.user_id = m.user_id
        WHERE m.id = %s
        """,
        (message_id,),
    )
    return public_chat_message(row) if row else None


def save_chat_message(user_id: int, content: str) -> dict:
    message_id = database.execute(
        "INSERT INTO chat_messages (user_id, content) VALUES (%s, %s)",
        (user_id, content),
    )
    return fetch_chat_message(message_id)


def delete_chat_messages(scope: dict) -> int:
    if scope["mode"] == "ids":
        if not scope.get("ids"):
            return 0
        placeholders = ",".join(["%s"] * len(scope["ids"]))
        return database.execute(
            f"DELETE FROM chat_messages WHERE id IN ({placeholders})",
            scope["ids"],
        )
    return database.execute(
        "DELETE FROM chat_messages WHERE created_at >= %s AND created_at <= %s",
        (scope["start_at"], scope["end_at"]),
    )
