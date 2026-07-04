#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import base64
import json
import time
from urllib.parse import urlencode, unquote

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

import config


CHAOXING_CURRICULUM_URL = "https://kb.chaoxing.com/pc/curriculum/getMyLessons"


def encrypt_by_aes(message: str, key: str = config.CHAOXING_LOGIN_TRANSFER_KEY) -> str:
    key_bytes = key.encode("utf-8")
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv=key_bytes)
    encrypted = cipher.encrypt(pad(message.encode("utf-8"), AES.block_size))
    return base64.b64encode(encrypted).decode("utf-8")


def parse_cookie(raw: str) -> dict:
    cookies = {}
    for item in raw.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def build_base_headers() -> dict:
    return {"User-Agent": config.USER_AGENT}


def build_curriculum_headers() -> dict:
    return {
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://kb.chaoxing.com/res/pc/curriculum/schedule.html",
    }


def parse_curriculum_user_name(result: dict) -> str:
    if not result.get("result"):
        raise RuntimeError(result.get("msg") or "课程接口返回失败")
    user_name = result.get("data", {}).get("curriculum", {}).get("userName")
    user_name = str(user_name or "").strip()
    if not user_name:
        raise RuntimeError("课程接口没有返回用户姓名")
    return user_name


def fetch_curriculum_user_name(session: requests.Session) -> str:
    response = session.get(
        CHAOXING_CURRICULUM_URL,
        headers=build_curriculum_headers(),
        params={"curTime": int(time.time() * 1000)},
        timeout=12,
    )
    response.raise_for_status()
    return parse_curriculum_user_name(response.json())


def build_select_url(room_id: str, fid_enc: str, day: str) -> str:
    query = urlencode(
        {
            "deptIdEnc": fid_enc,
            "id": room_id,
            "day": day,
            "backLevel": "2",
            "fidEnc": fid_enc,
        }
    )
    return f"{config.CHAOXING_SELECT_URL}?{query}"


def build_reserve_url(room_id: str, fid_enc: str, day: str, seat_num: str = "") -> str:
    url = build_select_url(room_id, fid_enc, day)
    return f"{url}#seat={seat_num}" if seat_num else url


def build_headers(room_id: str, fid_enc: str, day: str) -> dict:
    return {
        "User-Agent": config.USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://office.chaoxing.com",
        "Referer": build_select_url(room_id, fid_enc, day),
    }


def build_all_seats(seat_min: int = config.SEAT_MIN, seat_max: int = config.SEAT_MAX, width: int = config.SEAT_WIDTH) -> list:
    return [str(i).zfill(width) for i in range(seat_min, seat_max + 1)]


def normalize_seat_num(value, width: int = config.SEAT_WIDTH) -> str:
    text = str(value).strip()
    return text.zfill(width) if text.isdigit() else text


def cookie_jar_to_json(session: requests.Session) -> str:
    records = []
    for cookie in session.cookies:
        records.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": bool(cookie.secure),
            }
        )
    return json.dumps(records, ensure_ascii=False)


def session_from_cookie_json(cookies_json: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(build_base_headers())
    for item in json.loads(cookies_json or "[]"):
        kwargs = {
            "path": item.get("path") or "/",
            "secure": bool(item.get("secure")),
        }
        if item.get("domain"):
            kwargs["domain"] = item["domain"]
        session.cookies.set(item["name"], item["value"], **kwargs)
    return session


def cookie_json_to_header(cookies_json: str) -> str:
    pairs = []
    for item in json.loads(cookies_json or "[]"):
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        if not name:
            continue
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def session_from_cookie_header(cookie_raw: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(build_base_headers())
    for key, value in parse_cookie(cookie_raw).items():
        session.cookies.set(key, value)
    return session


def login(account: str, password: str) -> requests.Session:
    session = requests.Session()
    headers = {
        "User-Agent": config.USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://passport2.chaoxing.com",
        "Referer": f"https://passport2.chaoxing.com/login?refer={config.CHAOXING_LOGIN_REFER}",
    }
    data = {
        "fid": "-1",
        "uname": encrypt_by_aes(account),
        "password": encrypt_by_aes(password),
        "refer": config.CHAOXING_LOGIN_REFER,
        "t": "true",
        "forbidotherlogin": "0",
        "validate": "",
        "doubleFactorLogin": "0",
        "independentId": "0",
        "independentNameId": "0",
    }

    resp = session.post(config.CHAOXING_LOGIN_URL, headers=headers, data=data, timeout=12)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("status"):
        message = result.get("msg2") or result.get("msg") or result
        raise RuntimeError(f"学习通登录失败：{message}")
    if result.get("containTwoFactorLogin"):
        raise RuntimeError("学习通登录需要二次验证，当前项目无法自动完成")

    redirect_url = unquote(result.get("url", ""))
    if redirect_url.startswith("http"):
        session.get(redirect_url, headers=build_base_headers(), timeout=12)
    return session


def prepare_office_session(session: requests.Session, room_id: str, fid_enc: str, day: str) -> None:
    session.get(
        build_select_url(room_id, fid_enc, day),
        headers=build_base_headers(),
        timeout=12,
        allow_redirects=True,
    )


def query_seats(
    session: requests.Session,
    room_id: str,
    fid_enc: str,
    day: str,
    start_time: str,
    end_time: str,
) -> dict:
    prepare_office_session(session, room_id, fid_enc, day)
    data = {
        "roomId": room_id,
        "startTime": start_time,
        "endTime": end_time,
        "day": day,
        "fidEnc": fid_enc,
    }
    resp = session.post(config.CHAOXING_USED_SEATS_URL, headers=build_headers(room_id, fid_enc, day), data=data, timeout=12)
    resp.raise_for_status()
    return resp.json()


def get_occupied_seats(result: dict, seat_width: int = config.SEAT_WIDTH) -> set:
    seat_reserves = result.get("data", {}).get("seatReserves", [])
    return {
        normalize_seat_num(item.get("seatNum"), seat_width)
        for item in seat_reserves
        if item.get("seatNum") is not None
    }


def get_available_seats(result: dict, all_seats: list, seat_width: int = config.SEAT_WIDTH) -> list:
    occupied = get_occupied_seats(result, seat_width)
    return [seat for seat in all_seats if seat not in occupied]


def find_adjacent_pairs(available_seats: list, width: int = config.SEAT_WIDTH) -> list:
    available = {normalize_seat_num(seat, width) for seat in available_seats}
    pairs = []
    for seat in sorted(available):
        if not seat.isdigit():
            continue
        next_seat = str(int(seat) + 1).zfill(width)
        if next_seat in available:
            pairs.append([seat, next_seat])
    return pairs
