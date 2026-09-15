#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from email.parser import BytesParser
from email.policy import default as email_default_policy
import hashlib
import os
from urllib.parse import quote

import config
import database

MAX_UPLOAD_SIZE = 150 * 1024 * 1024  # 150MB


def android_version_code_from_params(params: dict) -> int:
    raw = (params.get("version_code") or [""])[0]
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("version_code 必须是正整数") from exc
    if value <= 0:
        raise ValueError("version_code 必须是正整数")
    return value


def safe_apk_filename(filename: str) -> str:
    name = os.path.basename(str(filename or "").strip())
    if (
        not name
        or name in {".", ".."}
        or name != str(filename or "").strip()
        or "\\" in name
        or any(char in name for char in "\r\n\0")
    ):
        raise ValueError("APK 文件名不正确")
    if not name.lower().endswith(".apk"):
        raise ValueError("只支持上传 APK 文件")
    return name


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def public_app_version(row: dict, base_url: str) -> dict:
    filename = safe_apk_filename(row["apk_filename"])
    return {
        "id": row.get("id"),
        "platform": row.get("platform", "android"),
        "version_code": int(row["version_code"]),
        "version_name": row["version_name"],
        "force_update": bool(row.get("force_update")),
        "apk_url": f"{base_url.rstrip('/')}/downloads/apks/{quote(filename)}",
        "apk_size": int(row["apk_size"]),
        "apk_sha256": row["apk_sha256"],
        "release_notes": row.get("release_notes") or "",
        "created_at": row.get("created_at"),
    }


def app_update_payload(row: dict, current_version_code: int, base_url: str) -> dict:
    if not row or int(row["version_code"]) <= int(current_version_code):
        return {"update": False}
    return {"update": True, **public_app_version(row, base_url)}


def apk_upload_metadata(
    original_filename: str,
    version_code: int,
    version_name: str,
    release_notes: str,
    force_update: bool,
    data: bytes,
) -> dict:
    safe_apk_filename(original_filename)
    if version_code <= 0:
        raise ValueError("版本号必须是正整数")
    version_name = str(version_name or "").strip()
    if not version_name:
        raise ValueError("版本名称不能为空")
    if not data:
        raise ValueError("APK 文件不能为空")
    return {
        "platform": "android",
        "version_code": int(version_code),
        "version_name": version_name,
        "apk_filename": f"search-seat-{int(version_code)}.apk",
        "apk_size": len(data),
        "apk_sha256": sha256_hex(data),
        "release_notes": str(release_notes or "").strip(),
        "force_update": 1 if force_update else 0,
    }


def request_base_url(handler) -> str:
    proto = "https" if handler.headers.get("X-Forwarded-Proto") == "https" else "http"
    host = handler.headers.get("Host") or f"127.0.0.1:{config.APP_PORT}"
    return f"{proto}://{host}"


def parse_multipart_form(handler) -> dict:
    content_type = handler.headers.get("Content-Type", "")
    if "multipart/form-data" not in content_type:
        raise ValueError("请求必须使用 multipart/form-data")
    length = int(handler.headers.get("Content-Length", "0"))
    if length > MAX_UPLOAD_SIZE:
        raise ValueError("上传文件体积过大（最大允许 150MB）")
    raw = handler.rfile.read(length)
    header = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n\r\n"
    ).encode("utf-8")
    message = BytesParser(policy=email_default_policy).parsebytes(header + raw)
    form = {}
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename() or ""
        data = part.get_payload(decode=True) or b""
        form[name] = {
            "filename": filename,
            "data": data,
            "value": "" if filename else data.decode("utf-8", errors="replace"),
        }
    return form


def form_value(form: dict, name: str, default: str = "") -> str:
    item = form.get(name)
    if item is None or item.get("filename"):
        return default
    return str(item.get("value") or "").strip()


def fetch_app_versions(limit: int = 20) -> list:
    return database.fetch_all(
        """
        SELECT id, platform, version_code, version_name, apk_filename, apk_size,
               apk_sha256, release_notes, force_update, published, created_at
        FROM app_versions
        WHERE platform = 'android'
        ORDER BY version_code DESC
        LIMIT %s
        """,
        (limit,),
    )


def fetch_latest_android_version_above(version_code: int):
    return database.fetch_one(
        """
        SELECT id, platform, version_code, version_name, apk_filename, apk_size,
               apk_sha256, release_notes, force_update, published, created_at
        FROM app_versions
        WHERE platform = 'android' AND published = 1 AND version_code > %s
        ORDER BY version_code DESC
        LIMIT 1
        """,
        (version_code,),
    )


def insert_app_version(metadata: dict) -> int:
    return database.execute(
        """
        INSERT INTO app_versions (
            platform, version_code, version_name, apk_filename, apk_size,
            apk_sha256, release_notes, force_update, published
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1)
        """,
        (
            metadata["platform"],
            metadata["version_code"],
            metadata["version_name"],
            metadata["apk_filename"],
            metadata["apk_size"],
            metadata["apk_sha256"],
            metadata["release_notes"],
            metadata["force_update"],
        ),
    )


def stream_apk_file(target_path, handler, chunk_size=65536):
    file_size = os.path.getsize(target_path)
    filename = os.path.basename(target_path)
    handler.send_response(200)
    handler.send_header("Content-Type", "application/vnd.android.package-archive")
    handler.send_header("Content-Length", str(file_size))
    handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.send_header("Accept-Ranges", "bytes")
    handler.end_headers()
    with open(target_path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            handler.wfile.write(chunk)
