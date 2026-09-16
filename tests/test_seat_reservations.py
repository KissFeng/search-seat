#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from unittest.mock import MagicMock, patch

import chaoxing
from services.seat_service import (
    reserve_status_label,
    reserve_status_badge_type,
    reserve_checkin_window,
    build_occupied_reserves,
    save_occupied_reservations,
    query_seat_reservations,
    query_user_reservations,
)
from services.chaoxing_group_service import save_group_members, get_group_contact_stats


class SeatReservationsTests(unittest.TestCase):
    def test_parse_seat_reserves(self):
        sample_api_result = {
            "success": True,
            "data": {
                "seatReserves": [
                    {
                        "id": 1001,
                        "seatNum": "8",
                        "roomId": 12818,
                        "startTime": "08:00",
                        "endTime": "12:00",
                        "status": 1,
                        "uid": "307004632",
                        "today": "2026-09-16",
                        "rDeptId": 1035,
                    },
                    {
                        "id": 1002,
                        "seatNum": 15,
                        "roomId": "12818",
                        "startTime": "14:00",
                        "endTime": "18:00",
                        "status": 0,
                        "uid": "999888777",
                        "today": "2026-09-16",
                    },
                ]
            },
        }
        reserves = chaoxing.parse_seat_reserves(sample_api_result, seat_width=3)
        self.assertEqual(len(reserves), 2)

        r1 = reserves[0]
        self.assertEqual(r1["id"], 1001)
        self.assertEqual(r1["seat_num"], "008")
        self.assertEqual(r1["seatNum"], "008")
        self.assertEqual(r1["room_id"], "12818")
        self.assertEqual(r1["start_time"], "08:00")
        self.assertEqual(r1["end_time"], "12:00")
        self.assertEqual(r1["status"], 1)
        self.assertEqual(r1["uid"], "307004632")

        r2 = reserves[1]
        self.assertEqual(r2["id"], 1002)
        self.assertEqual(r2["seat_num"], "015")
        self.assertEqual(r2["status"], 0)

    def test_reserve_status_labels_and_badges(self):
        self.assertEqual(reserve_status_label(1), "使用中")
        self.assertEqual(reserve_status_badge_type("使用中"), "success")

        self.assertEqual(reserve_status_label(3), "暂离中")
        self.assertEqual(reserve_status_badge_type("暂离中"), "info")

        self.assertEqual(reserve_status_label(5), "监督中")
        self.assertEqual(reserve_status_badge_type("监督中"), "purple")

        self.assertEqual(reserve_status_badge_type("待履约"), "warning")
        self.assertEqual(reserve_status_badge_type("违约"), "danger")

    def test_reserve_checkin_window_supervised(self):
        # When status == 5 (监督中), checkin button should always be available
        item_supervised = {
            "status": 5,
            "startTime": 1789468800000,
            "endTime": 1789476000000,
        }
        window = reserve_checkin_window(item_supervised)
        self.assertTrue(window["available"])

    def test_build_occupied_reserves_with_profile_matching(self):
        raw_reserves = [
            {
                "id": 2001,
                "seat_num": "050",
                "seatNum": "050",
                "room_id": "12818",
                "day": "2026-09-16",
                "start_time": "09:00",
                "end_time": "12:00",
                "status": 1,
                "uid": "307004632",
            }
        ]
        mock_profiles = {
            "307004632": {
                "uid": "307004632",
                "real_name": "张三",
                "account": "13800000000",
                "avatar_url": "https://photo.chaoxing.com/p/307004632_80",
            }
        }
        with patch("services.seat_service.fetch_user_profiles_by_uids", return_value=mock_profiles):
            occupied_details, occupied_by_seat = build_occupied_reserves(raw_reserves, "12818", "2026-09-16")
            self.assertEqual(len(occupied_details), 1)
            item = occupied_details[0]
            self.assertEqual(item["user_name"], "张三")
            self.assertEqual(item["avatar_url"], "https://photo.chaoxing.com/p/307004632_80")
            self.assertEqual(item["status_label"], "使用中")
            self.assertIn("050", occupied_by_seat)

    def test_save_and_query_seat_reservations(self):
        mock_db = MagicMock()
        mock_db.fetch_all.return_value = [
            {
                "id": 1,
                "reserve_id": 1001,
                "room_id": "12818",
                "seat_num": "008",
                "day": "2026-09-16",
                "start_time": "08:00",
                "end_time": "12:00",
                "status": 1,
                "uid": "307004632",
                "real_name": "张三",
                "avatar_url": "https://photo.chaoxing.com/p/307004632_80",
                "created_at": "2026-09-16 08:00:00",
                "updated_at": "2026-09-16 08:00:00",
            }
        ]
        with patch("services.seat_service.database", mock_db):
            saved = save_occupied_reservations("12818", "2026-09-16", [
                {
                    "id": 1001,
                    "seat_num": "008",
                    "start_time": "08:00",
                    "end_time": "12:00",
                    "status": 1,
                    "uid": "307004632",
                }
            ])
            self.assertEqual(saved, 1)
            mock_db.execute.assert_called()

            results = query_seat_reservations("12818", "2026-09-16", "008")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["seat_num"], "008")
            self.assertEqual(results[0]["user_name"], "张三")

    def test_query_user_reservations(self):
        mock_db = MagicMock()
        mock_db.fetch_all.return_value = [
            {
                "id": 2,
                "reserve_id": 2002,
                "room_id": "12818",
                "seat_num": "012",
                "day": "2026-09-16",
                "start_time": "13:00",
                "end_time": "17:00",
                "status": 1,
                "uid": "307004632",
                "real_name": "张三",
                "created_at": "2026-09-16 12:00:00",
                "updated_at": "2026-09-16 12:00:00",
            }
        ]
        with patch("services.seat_service.database", mock_db):
            results = query_user_reservations(keyword="张三")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["user_name"], "张三")
            self.assertEqual(results[0]["status_label"], "使用中")


if __name__ == "__main__":
    unittest.main()
