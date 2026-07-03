#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
学习通座位命令行查询入口。

用法:
    python3 main.py --account 学习通账号 --password 学习通密码 --day 2026-07-03 --start 15:00 --end 16:00
"""

import argparse

import chaoxing
import config


def build_session(cookie_raw: str = "", account: str = "", password: str = ""):
    if cookie_raw:
        return chaoxing.session_from_cookie_header(cookie_raw)
    if not account:
        raise RuntimeError("缺少学习通账号")
    if not password:
        raise RuntimeError("缺少学习通密码")
    return chaoxing.login(account, password)


def parse_args():
    parser = argparse.ArgumentParser(description="查询学习通图书馆指定时段可预约座位")
    parser.add_argument("--account", default="", help="学习通账号")
    parser.add_argument("--password", default="", help="学习通密码")
    parser.add_argument("--cookie", default="", help="浏览器里的完整 Cookie，可替代账号密码")
    parser.add_argument("--room-id", default=config.ROOM_ID, help="房间 ID")
    parser.add_argument("--fid-enc", default=config.FID_ENC, help="fidEnc / deptIdEnc")
    parser.add_argument("--day", required=True, help="查询日期，格式 YYYY-MM-DD")
    parser.add_argument("--start", required=True, help="开始时间，格式 HH:MM")
    parser.add_argument("--end", required=True, help="结束时间，格式 HH:MM")
    parser.add_argument("--seat-min", type=int, default=config.SEAT_MIN, help="最小座位号")
    parser.add_argument("--seat-max", type=int, default=config.SEAT_MAX, help="最大座位号")
    parser.add_argument("--seat-width", type=int, default=config.SEAT_WIDTH, help="座位号补零宽度")
    parser.add_argument("--show-occupied", action="store_true", help="同时输出不可预约座位号")
    parser.add_argument("--show-pairs", action="store_true", help="同时输出双人连排座")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    session = build_session(args.cookie, args.account, args.password)
    result = chaoxing.query_seats(
        session=session,
        room_id=args.room_id,
        fid_enc=args.fid_enc,
        day=args.day,
        start_time=args.start,
        end_time=args.end,
    )

    if not result.get("success"):
        print("请求失败，返回内容：", result)
        return

    all_seats = chaoxing.build_all_seats(args.seat_min, args.seat_max, args.seat_width)
    seat_reserves = result.get("data", {}).get("seatReserves", [])
    occupied = sorted(chaoxing.get_occupied_seats(result, args.seat_width))
    available = chaoxing.get_available_seats(result, all_seats, args.seat_width)
    pairs = chaoxing.find_adjacent_pairs(available, args.seat_width)

    print(f"房间 {args.room_id}，{args.day} {args.start}-{args.end}")
    print(f"接口返回预约记录：{len(seat_reserves)} 条")
    print(f"座位总数：{len(all_seats)}")
    print(f"不可预约/已占用：{len(occupied)} 个")
    print(f"可预约：{len(available)} 个")
    print(f"双人连排：{len(pairs)} 组\n")

    print("可预约座位号：")
    print(available)

    if args.show_pairs:
        print("\n双人连排座：")
        print(pairs)

    if args.show_occupied:
        print("\n不可预约/已占用座位号：")
        print(occupied)


if __name__ == "__main__":
    main()
