import unittest

import app


class AdminUserSummaryTests(unittest.TestCase):
    def test_public_admin_user_includes_running_watch_count(self):
        user = app.public_admin_user(
            {
                "id": 1,
                "username": "13800138000",
                "running_watch_count": 2,
                "watch_count": 5,
                "query_count": 3,
            }
        )

        self.assertEqual(user["running_watch_count"], 2)
        self.assertEqual(user["watch_count"], 5)

    def test_timetable_week_defaults_to_empty(self):
        self.assertEqual(app.timetable_week_num_from_payload({}), "")

    def test_public_current_reserve_formats_active_reservation(self):
        item = {
            "id": 187309719,
            "roomId": 12818,
            "seatNum": "344",
            "secondLevelName": "2F",
            "thirdLevelName": "阅览区",
            "startTime": 1783152000000,
            "endTime": 1783166400000,
            "status": 1,
            "today": "2026-07-04",
        }

        reserve = app.public_current_reserve(item)

        self.assertEqual(reserve["seat_num"], "344")
        self.assertEqual(reserve["room_name"], "2F-阅览区")
        self.assertEqual(reserve["status_label"], "使用中")
        self.assertEqual(reserve["day"], "2026-07-04")
        self.assertEqual(reserve["time_range"], "16:00-20:00")
        self.assertEqual(reserve["reserve_url"], "/reserve?room_id=12818&day=2026-07-04&seat=344")

    def test_public_current_reserve_formats_pending_reservation(self):
        item = {
            "roomId": 12818,
            "seatNum": "339",
            "secondLevelName": "2F",
            "thirdLevelName": "阅览区",
            "startTime": 1783238400000,
            "endTime": 1783252800000,
            "status": 0,
            "today": "2026-07-05",
        }

        reserve = app.public_current_reserve(item)

        self.assertEqual(reserve["status_label"], "待履约")
        self.assertEqual(reserve["time_range"], "16:00-20:00")

    def test_public_current_reserve_marks_expired_pending_as_violation(self):
        item = {
            "roomId": 12818,
            "seatNum": "327",
            "secondLevelName": "2F",
            "thirdLevelName": "阅览区",
            "startTime": 1782028800000,
            "endTime": 1782043200000,
            "expireTime": 1782029700000,
            "status": 0,
            "today": "2026-06-21",
        }

        reserve = app.public_current_reserve(item, now_ms=1783164938000)

        self.assertEqual(reserve["status_label"], "违约")
        self.assertEqual(reserve["time_range"], "16:00-20:00")

    def test_public_reserve_records_limits_to_recent_ten(self):
        result = {
            "data": {
                "reserveList": [
                    {
                        "id": index,
                        "roomId": 12818,
                        "seatNum": str(index),
                        "secondLevelName": "2F",
                        "thirdLevelName": "阅览区",
                        "startTime": 1783238400000,
                        "endTime": 1783252800000,
                        "status": 2,
                        "today": "2026-07-05",
                    }
                    for index in range(12)
                ]
            }
        }

        records = app.public_reserve_records(result)

        self.assertEqual(len(records), 10)
        self.assertEqual(records[0]["seat_num"], "0")
        self.assertEqual(records[-1]["seat_num"], "9")


if __name__ == "__main__":
    unittest.main()
