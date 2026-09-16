#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional
import requests

import chaoxing
import database

logger = logging.getLogger(__name__)


def init_contacts_session(session: requests.Session) -> bool:
    try:
        r_base = session.get("https://i.chaoxing.com/base", timeout=8)
        if r_base.status_code != 200:
            return False
        match = re.search(r'https://contactsyd\.chaoxing\.com/pc/contacts/home\?s=([a-zA-Z0-9]+)', r_base.text)
        if not match:
            return False
        r_home = session.get(match.group(0), timeout=8)
        return r_home.status_code == 200
    except Exception as exc:
        logger.warning("Failed to init contacts session: %s", exc)
        return False


def fetch_chatgroups_for_session(session: requests.Session, max_pages: int = 10) -> List[Dict[str, Any]]:
    groups = []
    seen_ids = set()
    for page in range(1, max_pages + 1):
        try:
            r = session.get(
                "https://contactsyd.chaoxing.com/pc/contacts/getJoinedChatgroups",
                params={"page": page, "pageSize": 50},
                timeout=10,
            )
            if r.status_code != 200:
                break
            data = r.json()
            items = data.get("msg", {}).get("list", [])
            if not items:
                break
            for item in items:
                gid = item.get("groupid")
                if gid and gid not in seen_ids:
                    seen_ids.add(gid)
                    groups.append({
                        "groupid": gid,
                        "groupname": item.get("groupname") or "未命名群聊",
                        "pic": item.get("pic") or "",
                    })
            if len(items) < 50:
                break
        except Exception as exc:
            logger.warning("Error fetching chatgroups page %d: %s", page, exc)
            break
    return groups


def fetch_chatgroup_members(session: requests.Session, group_id: str, max_pages: int = 10) -> List[Dict[str, Any]]:
    members = []
    seen_puids = set()
    for page in range(1, max_pages + 1):
        try:
            r = session.get(
                "https://contactsyd.chaoxing.com/pc/contacts/getChatgroupMembers",
                params={"chatgroupId": group_id, "page": page, "pageSize": 100},
                timeout=10,
            )
            if r.status_code != 200:
                break
            data = r.json()
            items = data.get("msg", {}).get("list", [])
            if not items:
                break
            for item in items:
                puid = item.get("puid")
                name = (item.get("name") or "").strip()
                if puid and puid not in seen_puids:
                    seen_puids.add(puid)
                    members.append({
                        "uid": int(puid),
                        "real_name": name,
                        "avatar_url": item.get("pic") or f"https://photo.chaoxing.com/p/{puid}_80",
                    })
            if len(items) < 100:
                break
        except Exception as exc:
            logger.warning("Error fetching members for group %s page %d: %s", group_id, page, exc)
            break
    return members


def save_group_members(group_id: str, group_name: str, members: List[Dict[str, Any]], source_account: str = "") -> int:
    if not members:
        return 0
    saved_count = 0
    with database.connection() as conn:
        with conn.cursor() as cursor:
            for m in members:
                uid = m.get("uid")
                name = m.get("real_name")
                avatar = m.get("avatar_url") or f"https://photo.chaoxing.com/p/{uid}_80"
                if not uid or not name:
                    continue
                # 1. Upsert into seat_group_members
                cursor.execute(
                    """
                    INSERT INTO seat_group_members (group_id, group_name, uid, real_name, avatar_url, source_account)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        group_name = VALUES(group_name),
                        real_name = VALUES(real_name),
                        avatar_url = VALUES(avatar_url),
                        source_account = VALUES(source_account)
                    """,
                    (group_id, group_name, uid, name, avatar, source_account),
                )
                # 2. Upsert into seat_user_profiles
                cursor.execute(
                    """
                    INSERT INTO seat_user_profiles (uid, real_name, avatar_url, source)
                    VALUES (%s, %s, %s, 'chatgroup')
                    ON DUPLICATE KEY UPDATE
                        real_name = CASE
                            WHEN real_name IS NULL OR real_name = '' OR LEFT(real_name, 4) = '超星用户' THEN VALUES(real_name)
                            ELSE real_name
                        END,
                        avatar_url = COALESCE(NULLIF(avatar_url, ''), VALUES(avatar_url))
                    """,
                    (uid, name, avatar),
                )
                saved_count += 1
    return saved_count


def harvest_session_groups(user_id: int, cookies_json: str, account: str = "") -> Dict[str, Any]:
    session = chaoxing.session_from_cookie_json(cookies_json)
    if not init_contacts_session(session):
        return {"ok": False, "error": "无法初始化通讯录会话（Cookie 可能失效或无通讯录权限）"}
    
    groups = fetch_chatgroups_for_session(session)
    total_saved = 0
    unique_uids = set()

    for g in groups:
        gid = g["groupid"]
        gname = g["groupname"]
        members = fetch_chatgroup_members(session, gid)
        for m in members:
            unique_uids.add(m["uid"])
        count = save_group_members(gid, gname, members, account)
        total_saved += count
        time.sleep(0.05)

    return {
        "ok": True,
        "group_count": len(groups),
        "member_records": total_saved,
        "unique_users": len(unique_uids),
    }


def harvest_all_sessions_groups() -> Dict[str, Any]:
    sessions = database.fetch_all(
        "SELECT user_id, cx_account, cookies_json FROM chaoxing_sessions WHERE session_valid = 1 ORDER BY last_login_at DESC"
    )
    total_groups = 0
    total_records = 0
    all_unique_uids = set()
    processed_accounts = 0

    for s in sessions:
        account = s.get("cx_account") or ""
        cookies = s.get("cookies_json")
        if not cookies:
            continue
        try:
            res = harvest_session_groups(s["user_id"], cookies, account)
            if res.get("ok"):
                processed_accounts += 1
                total_groups += res.get("group_count", 0)
                total_records += res.get("member_records", 0)
        except Exception as exc:
            logger.error("Harvesting failed for account %s: %s", account, exc)

    stats = get_group_contact_stats()
    return {
        "ok": True,
        "processed_accounts": processed_accounts,
        "total_groups_scanned": total_groups,
        "total_records_saved": total_records,
        "stats": stats,
    }


def trigger_harvest_background(user_id: int, cookies_json: str, account: str = "") -> None:
    threading.Thread(
        target=harvest_session_groups,
        args=(user_id, cookies_json, account),
        daemon=True,
    ).start()


def get_group_contact_stats() -> Dict[str, Any]:
    row_groups = database.fetch_one("SELECT COUNT(DISTINCT group_id) AS cnt FROM seat_group_members")
    row_members = database.fetch_one("SELECT COUNT(*) AS cnt, COUNT(DISTINCT uid) AS unique_cnt FROM seat_group_members")
    row_profiles = database.fetch_one("SELECT COUNT(*) AS cnt FROM seat_user_profiles")

    return {
        "total_groups": row_groups.get("cnt", 0) if row_groups else 0,
        "total_records": row_members.get("cnt", 0) if row_members else 0,
        "unique_group_users": row_members.get("unique_cnt", 0) if row_members else 0,
        "total_profiles": row_profiles.get("cnt", 0) if row_profiles else 0,
    }
