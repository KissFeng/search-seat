#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
超星（chaoxing）图书馆座位查询脚本
接口: POST https://office.chaoxing.com/data/apps/seat/getusedseatnums

用法:
    python3 main.py --account 学习通账号 --password 学习通密码 --day 2026-07-03 --start 15:00 --end 16:00
"""

import argparse
import base64
import os
from urllib.parse import unquote

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

# ------------------ 配置区 ------------------

ROOM_ID = "12818"                 # 自习室/房间 ID，从预约页面 URL 的 id 参数里能看到
FID_ENC = "087075e03ab2e001"      # 单位加密 ID，同样能在预约页面 URL 里找到 fidEnc 参数
SEAT_MIN = 1                      # 当前房间最小座位号
SEAT_MAX = 371                    # 当前房间最大座位号
SEAT_WIDTH = 3                    # 座位号补零宽度，例如 1 -> 001

URL = "https://office.chaoxing.com/data/apps/seat/getusedseatnums"
LOGIN_URL = "https://passport2.chaoxing.com/fanyalogin"
LOGIN_REFER = "https%3A%2F%2Fi.chaoxing.com"
LOGIN_TRANSFER_KEY = "u2oh6Vu^HWe4_AES"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


def get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def require_value(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(f"缺少 {name}")
    return value


def parse_cookie(raw: str) -> dict:
    """把 'a=1; b=2' 格式的字符串转成字典"""
    cookies = {}
    for item in raw.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        k, v = item.split("=", 1)
        cookies[k.strip()] = v.strip()
    return cookies


def encrypt_by_aes(message: str, key: str = LOGIN_TRANSFER_KEY) -> str:
    """复刻登录页 CryptoJS AES-CBC-PKCS7 加密，Key 和 IV 相同。"""
    key_bytes = key.encode("utf-8")
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv=key_bytes)
    encrypted = cipher.encrypt(pad(message.encode("utf-8"), AES.block_size))
    return base64.b64encode(encrypted).decode("utf-8")


def build_cookie_header(cookies: dict) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


def build_base_headers() -> dict:
    return {"User-Agent": USER_AGENT}


def build_referer(room_id: str, fid_enc: str, day: str) -> str:
    return (
        "https://office.chaoxing.com/front/third/apps/seat/select"
        f"?deptIdEnc={fid_enc}&id={room_id}&day={day}&backLevel=2&fidEnc={fid_enc}"
    )


def build_headers(room_id: str, fid_enc: str, day: str) -> dict:
    return {
        "User-Agent": USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://office.chaoxing.com",
        "Referer": build_referer(room_id, fid_enc, day),
    }


def build_all_seats(seat_min: int = SEAT_MIN, seat_max: int = SEAT_MAX,
                    width: int = SEAT_WIDTH) -> list:
    """生成当前房间的完整座位号列表，例如 001-371。"""
    return [str(i).zfill(width) for i in range(seat_min, seat_max + 1)]


def normalize_seat_num(value, width: int = SEAT_WIDTH) -> str:
    """统一座位号格式，接口偶尔可能返回数字或字符串。"""
    text = str(value).strip()
    return text.zfill(width) if text.isdigit() else text


def login(account: str, password: str) -> requests.Session:
    """使用账号密码登录学习通，并返回带登录 Cookie 的 Session。"""
    session = requests.Session()
    headers = {
        "User-Agent": USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://passport2.chaoxing.com",
        "Referer": f"https://passport2.chaoxing.com/login?refer={LOGIN_REFER}",
    }
    data = {
        "fid": "-1",
        "uname": encrypt_by_aes(account),
        "password": encrypt_by_aes(password),
        "refer": LOGIN_REFER,
        "t": "true",
        "forbidotherlogin": "0",
        "validate": "",
        "doubleFactorLogin": "0",
        "independentId": "0",
        "independentNameId": "0",
    }

    resp = session.post(LOGIN_URL, headers=headers, data=data, timeout=10)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("status"):
        message = result.get("msg2") or result.get("msg") or result
        raise RuntimeError(f"登录失败：{message}")
    if result.get("containTwoFactorLogin"):
        raise RuntimeError("登录需要二次验证，当前脚本无法自动完成")

    redirect_url = unquote(result.get("url", ""))
    if redirect_url.startswith("http"):
        session.get(redirect_url, headers=build_base_headers(), timeout=10)

    return session


def build_session(cookie_raw: str = "", account: str = "",
                  password: str = "") -> requests.Session:
    """优先使用现成 Cookie；没有 Cookie 时用账号密码自动登录。"""
    session = requests.Session()
    if cookie_raw:
        cookies = parse_cookie(cookie_raw)
        session.headers.update({"Cookie": build_cookie_header(cookies)})
        return session

    account = require_value("学习通账号", account)
    password = require_value("学习通密码", password)
    return login(account, password)


def prepare_office_session(session: requests.Session, room_id: str,
                           fid_enc: str, day: str) -> None:
    """访问座位页面，补齐 office.chaoxing.com 业务 Cookie。"""
    session.get(
        build_referer(room_id, fid_enc, day),
        headers=build_base_headers(),
        timeout=10,
        allow_redirects=True,
    )


def query_seats(room_id=ROOM_ID, fid_enc=FID_ENC, day="",
                 start_time="", end_time="", session=None):
    """查询指定房间/时段的座位占用情况。"""
    session = session or build_session()
    prepare_office_session(session, room_id, fid_enc, day)

    data = {
        "roomId": room_id,
        "startTime": start_time,
        "endTime": end_time,
        "day": day,
        "fidEnc": fid_enc,
    }

    headers = build_headers(room_id, fid_enc, day)
    resp = session.post(URL, headers=headers, data=data, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_occupied_seats(result: dict, seat_width: int = SEAT_WIDTH) -> set:
    """接口返回的是该时间段已被占用/已预约的座位记录，按 seatNum 去重。"""
    seat_reserves = result.get("data", {}).get("seatReserves", [])
    return {
        normalize_seat_num(item.get("seatNum"), seat_width)
        for item in seat_reserves
        if item.get("seatNum") is not None
    }


def get_available_seats(result: dict, all_seats: list,
                        seat_width: int = SEAT_WIDTH) -> list:
    """完整座位列表减去占用列表，得到当前时间段可预约座位。"""
    occupied = get_occupied_seats(result, seat_width)
    return [seat for seat in all_seats if seat not in occupied]


def parse_args():
    parser = argparse.ArgumentParser(description="查询超星图书馆指定时段可预约座位")
    parser.add_argument("--account", default="", help="学习通账号")
    parser.add_argument("--password", default="", help="学习通密码")
    parser.add_argument("--cookie", default="", help="浏览器里的完整 Cookie，可替代账号密码")
    parser.add_argument("--room-id", default=ROOM_ID, help="房间 ID")
    parser.add_argument("--fid-enc", default=FID_ENC, help="fidEnc / deptIdEnc")
    parser.add_argument("--day", required=True, help="查询日期，格式 YYYY-MM-DD")
    parser.add_argument("--start", required=True, help="开始时间，格式 HH:MM")
    parser.add_argument("--end", required=True, help="结束时间，格式 HH:MM")
    parser.add_argument("--seat-min", type=int, default=SEAT_MIN, help="最小座位号")
    parser.add_argument("--seat-max", type=int, default=SEAT_MAX, help="最大座位号")
    parser.add_argument("--seat-width", type=int, default=SEAT_WIDTH, help="座位号补零宽度")
    parser.add_argument("--show-occupied", action="store_true", help="同时输出不可预约座位号")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    session = build_session(args.cookie, args.account, args.password)
    result = query_seats(
        room_id=args.room_id,
        fid_enc=args.fid_enc,
        day=args.day,
        start_time=args.start,
        end_time=args.end,
        session=session,
    )

    if not result.get("success"):
        print("请求失败，返回内容：", result)
    else:
        all_seats = build_all_seats(args.seat_min, args.seat_max, args.seat_width)
        seat_reserves = result.get("data", {}).get("seatReserves", [])
        occupied = sorted(get_occupied_seats(result, args.seat_width))
        available = get_available_seats(result, all_seats, args.seat_width)

        print(f"房间 {args.room_id}，{args.day} {args.start}-{args.end}")
        print(f"接口返回预约记录：{len(seat_reserves)} 条")
        print(f"座位总数：{len(all_seats)}")
        print(f"不可预约/已占用：{len(occupied)} 个")
        print(f"可预约：{len(available)} 个\n")

        print("可预约座位号：")
        print(available)

        if args.show_occupied:
            print("\n不可预约/已占用座位号：")
            print(occupied)
