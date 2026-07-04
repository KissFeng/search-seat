#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import base64
import hashlib
import hmac
import os
import time
from http.cookies import SimpleCookie
from typing import Optional

import config
import database


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 260000)
    return "pbkdf2_sha256$260000${}${}".format(
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def get_or_create_external_user(username: str) -> dict:
    username = username.strip()
    if not username:
        raise ValueError("账号不能为空")

    user = database.fetch_one(
        "SELECT id, username, disabled_at FROM users WHERE username = %s",
        (username,),
    )
    if user:
        if user.get("disabled_at"):
            raise PermissionError("账号已被禁用")
        database.execute("UPDATE users SET last_login_at = NOW() WHERE id = %s", (user["id"],))
        return {"id": user["id"], "username": user["username"]}

    user_id = database.execute(
        "INSERT INTO users (username, password_hash, last_login_at) VALUES (%s, %s, NOW())",
        (username, hash_password(base64.b64encode(os.urandom(24)).decode("ascii"))),
    )
    return {"id": user_id, "username": username}


def sign_session(user_id: int, expires_at: int) -> str:
    body = f"{user_id}:{expires_at}"
    signature = hmac.new(config.APP_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    token = f"{body}:{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(token).decode("ascii")


def make_session_cookie(user_id: int) -> str:
    expires_at = int(time.time() + config.SESSION_DAYS * 86400)
    token = sign_session(user_id, expires_at)
    max_age = config.SESSION_DAYS * 86400
    return (
        f"{config.SESSION_COOKIE}={token}; Path=/; Max-Age={max_age}; "
        "HttpOnly; SameSite=Lax"
    )


def clear_session_cookie() -> str:
    return f"{config.SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"


def sign_admin_session(username: str, expires_at: int) -> str:
    body = f"admin:{username}:{expires_at}"
    signature = hmac.new(config.APP_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    token = f"{body}:{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(token).decode("ascii")


def make_admin_session_cookie(username: str) -> str:
    expires_at = int(time.time() + config.ADMIN_SESSION_HOURS * 3600)
    token = sign_admin_session(username, expires_at)
    max_age = config.ADMIN_SESSION_HOURS * 3600
    return (
        f"{config.ADMIN_SESSION_COOKIE}={token}; Path=/; Max-Age={max_age}; "
        "HttpOnly; SameSite=Lax"
    )


def clear_admin_session_cookie() -> str:
    return f"{config.ADMIN_SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"


def current_admin_from_header(cookie_header: str) -> Optional[dict]:
    cookie = SimpleCookie()
    cookie.load(cookie_header or "")
    morsel = cookie.get(config.ADMIN_SESSION_COOKIE)
    if not morsel:
        return None

    try:
        raw = base64.urlsafe_b64decode(morsel.value.encode("ascii")).decode("utf-8")
        prefix, username, expires_at, signature = raw.split(":", 3)
        if prefix != "admin":
            return None
        body = f"{prefix}:{username}:{expires_at}"
        expected = hmac.new(
            config.APP_SECRET.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        if int(expires_at) < int(time.time()):
            return None
    except Exception:
        return None

    return {"username": username}


def current_user_from_header(cookie_header: str) -> Optional[dict]:
    cookie = SimpleCookie()
    cookie.load(cookie_header or "")
    morsel = cookie.get(config.SESSION_COOKIE)
    if not morsel:
        return None

    try:
        raw = base64.urlsafe_b64decode(morsel.value.encode("ascii")).decode("utf-8")
        user_id, expires_at, signature = raw.split(":", 2)
        body = f"{user_id}:{expires_at}"
        expected = hmac.new(
            config.APP_SECRET.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        if int(expires_at) < int(time.time()):
            return None
    except Exception:
        return None

    user = database.fetch_one(
        "SELECT id, username, created_at, last_login_at, disabled_at FROM users WHERE id = %s",
        (int(user_id),),
    )
    if not user or user.get("disabled_at"):
        return None
    return {"id": user["id"], "username": user["username"]}
