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


if __name__ == "__main__":
    unittest.main()
