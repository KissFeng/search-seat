#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import secrets
import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


def load_env_file() -> None:
    if not ENV_FILE.exists():
        return

    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env_file()


def get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def get_int_env(name: str, default: int) -> int:
    try:
        return int(get_env(name, str(default)))
    except ValueError:
        return default


APP_HOST = get_env("APP_HOST", "0.0.0.0")
APP_PORT = get_int_env("PORT", 8000)
APP_SECRET = get_env("APP_SECRET", "dev-secret-change-me")

DB_HOST = get_env("DB_HOST", "127.0.0.1")
DB_PORT = get_int_env("DB_PORT", 3306)
DB_USER = get_env("DB_USER", "root")
DB_PASSWORD = get_env("DB_PASSWORD", "123456")
DB_NAME = get_env("DB_NAME", "search_seat")

ROOM_ID = get_env("ROOM_ID", "12818")
FID_ENC = get_env("FID_ENC", "087075e03ab2e001")
SEAT_MIN = get_int_env("SEAT_MIN", 1)
SEAT_MAX = get_int_env("SEAT_MAX", 371)
SEAT_WIDTH = get_int_env("SEAT_WIDTH", 3)

DEFAULT_ROOMS = [
    {"label": "2F-阅览区", "room_id": "12818", "fid_enc": FID_ENC, "seat_min": 1, "seat_max": 371, "seat_width": 3},
    {"label": "3F-阅览区", "room_id": "12819", "fid_enc": FID_ENC, "seat_min": 1, "seat_max": 299, "seat_width": 3},
    {"label": "4F-阅览区", "room_id": "12820", "fid_enc": FID_ENC, "seat_min": 1, "seat_max": 282, "seat_width": 3},
    {"label": "2F-24H借阅空间", "room_id": "11226", "fid_enc": FID_ENC, "seat_min": 1, "seat_max": 117, "seat_width": 3},
]


def load_rooms() -> list:
    raw = get_env("ROOMS_JSON")
    if not raw:
        return DEFAULT_ROOMS
    try:
        rooms = json.loads(raw)
        if isinstance(rooms, list) and rooms:
            return rooms
    except json.JSONDecodeError:
        pass
    return DEFAULT_ROOMS


ROOMS = load_rooms()

SESSION_COOKIE = "search_seat_session"
SESSION_DAYS = get_int_env("SESSION_DAYS", 7)
ADMIN_USERNAME = get_env("ADMIN_USERNAME")
ADMIN_PASSWORD = get_env("ADMIN_PASSWORD")
ADMIN_SESSION_COOKIE = "search_seat_admin_session"
ADMIN_SESSION_HOURS = get_int_env("ADMIN_SESSION_HOURS", 12)
WATCH_INTERVAL_SECONDS = get_int_env("WATCH_INTERVAL_SECONDS", 60)
NOTIFY_WEBHOOK_URL = get_env("NOTIFY_WEBHOOK_URL")

CHAOXING_SELECT_URL = "https://office.chaoxing.com/front/third/apps/seat/select"
CHAOXING_USED_SEATS_URL = "https://office.chaoxing.com/data/apps/seat/getusedseatnums"
CHAOXING_LOGIN_URL = "https://passport2.chaoxing.com/fanyalogin"
CHAOXING_LOGIN_REFER = "https%3A%2F%2Fi.chaoxing.com"
CHAOXING_LOGIN_TRANSFER_KEY = "u2oh6Vu^HWe4_AES"

USER_AGENT = get_env(
    "USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
)


def ensure_secret() -> None:
    global APP_SECRET
    if get_env("APP_SECRET"):
        return
    secret = secrets.token_urlsafe(32)
    APP_SECRET = secret
    if ENV_FILE.exists():
        current = ENV_FILE.read_text(encoding="utf-8")
        if "APP_SECRET=" in current:
            return
        ENV_FILE.write_text(current.rstrip() + f"\nAPP_SECRET={secret}\n", encoding="utf-8")
    else:
        ENV_FILE.write_text(f"APP_SECRET={secret}\n", encoding="utf-8")
