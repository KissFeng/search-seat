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


if __name__ == "__main__":
    unittest.main()
