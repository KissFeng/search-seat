#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import base64
import json
import time
from urllib.parse import parse_qs, urlencode, unquote, urlparse

import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

import config


CHAOXING_CURRICULUM_URL = "https://kb.chaoxing.com/pc/curriculum/getMyLessons"
CHAOXING_TIMETABLE_REDIRECT_URL = "https://i.chaoxing.com/wfw/space/redirectUrl"
CHAOXING_TIMETABLE_API_URL = "https://course.chaoxing.com/svcourse/new/timetable/getIssuedCourseInfo"


class ChaoxingAuthError(RuntimeError):
    pass


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


def build_timetable_headers(referer: str) -> dict:
    return {
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
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


def build_timetable_entry_url() -> str:
    return CHAOXING_TIMETABLE_REDIRECT_URL + "?" + urlencode(
        {
            "fid": config.CHAOXING_TIMETABLE_FID,
            "type": config.CHAOXING_TIMETABLE_REDIRECT_TYPE,
            "mAppId": config.CHAOXING_TIMETABLE_MAPP_ID,
        }
    )


def parse_timetable_page_params(url: str) -> dict:
    query = parse_qs(urlparse(url).query)
    mapping = {
        "taskId": "taskId",
        "type": "type",
        "userId": "userId",
        "tableType": "tableType",
    }
    params = {}
    for source, target in mapping.items():
        value = (query.get(source) or [""])[0].strip()
        if not value:
            raise ValueError(f"课表页面缺少 {source} 参数")
        params[target] = value
    return params


def fetch_timetable_response(session: requests.Session, week_num: str = "") -> dict:
    entry_response = session.get(
        build_timetable_entry_url(),
        headers=build_base_headers(),
        timeout=15,
        allow_redirects=True,
    )
    entry_response.raise_for_status()
    page_url = entry_response.url
    page_params = parse_timetable_page_params(page_url)
    api_params = {
        "type": page_params["type"],
        "taskId": page_params["taskId"],
        "parameter": page_params["userId"],
        "weekNum": str(week_num or ""),
        "weeks": "",
        "custom": "false",
        "actual": "true",
        "needShowByCampus": "1",
        "tableType": page_params["tableType"],
    }
    api_response = session.get(
        CHAOXING_TIMETABLE_API_URL,
        headers=build_timetable_headers(page_url),
        params=api_params,
        timeout=15,
    )
    api_response.raise_for_status()
    result = api_response.json()
    return {
        "entry_url": build_timetable_entry_url(),
        "page_url": page_url,
        "api_url": api_response.url,
        "params": page_params,
        "api_params": api_params,
        "response": result,
    }


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


def build_office_index_url(fid_enc: str) -> str:
    return "https://office.chaoxing.com/front/apps/seat/index?" + urlencode({"fidEnc": fid_enc})


def build_reserve_list_url(fid_enc: str) -> str:
    return "https://office.chaoxing.com/front/third/apps/seat/reserve/list?" + urlencode({"deptIdEnc": fid_enc})


def build_office_index_headers(fid_enc: str) -> dict:
    return {
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": build_office_index_url(fid_enc),
    }


def build_reserve_list_headers(fid_enc: str) -> dict:
    return {
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": build_reserve_list_url(fid_enc),
    }


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


def parse_login_result(result: dict) -> dict:
    if not result.get("status"):
        message = result.get("msg2") or result.get("msg") or "账号或密码不正确"
        raise ChaoxingAuthError(f"学习通登录失败：{message}")
    if result.get("containTwoFactorLogin"):
        raise ChaoxingAuthError("学习通登录需要二次验证，当前项目无法自动完成")
    return result


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
    result = parse_login_result(resp.json())

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


def prepare_office_index_session(session: requests.Session, fid_enc: str) -> None:
    session.get(
        build_office_index_url(fid_enc),
        headers=build_base_headers(),
        timeout=12,
        allow_redirects=True,
    )


def fetch_seat_index(session: requests.Session, fid_enc: str = config.FID_ENC) -> dict:
    prepare_office_index_session(session, fid_enc)
    resp = session.get(
        config.CHAOXING_SEAT_INDEX_URL,
        headers=build_office_index_headers(fid_enc),
        params={"fidEnc": fid_enc},
        timeout=12,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_reserve_list(session: requests.Session, fid_enc: str = config.FID_ENC, page_size: int = 10) -> dict:
    session.get(
        build_reserve_list_url(fid_enc),
        headers=build_base_headers(),
        timeout=12,
        allow_redirects=True,
    )
    resp = session.get(
        config.CHAOXING_SEAT_RESERVE_LIST_URL,
        headers=build_reserve_list_headers(fid_enc),
        params={
            "indexId": "0",
            "pageSize": str(page_size),
            "type": "-1",
            "fidEnc": fid_enc,
            "showQrCode": "1",
        },
        timeout=12,
    )
    resp.raise_for_status()
    return resp.json()


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
