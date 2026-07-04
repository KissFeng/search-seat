import time
import unittest

import auth
import config


class AdminAuthTests(unittest.TestCase):
    def setUp(self):
        self.original_secret = config.APP_SECRET
        config.APP_SECRET = "test-secret"

    def tearDown(self):
        config.APP_SECRET = self.original_secret

    def test_admin_cookie_round_trips(self):
        cookie = auth.make_admin_session_cookie("admin")
        token = cookie.split("=", 1)[1].split(";", 1)[0]

        admin = auth.current_admin_from_header(f"{config.ADMIN_SESSION_COOKIE}={token}")

        self.assertEqual(admin, {"username": "admin"})

    def test_invalid_admin_cookie_returns_none(self):
        admin = auth.current_admin_from_header(f"{config.ADMIN_SESSION_COOKIE}=bad-token")

        self.assertIsNone(admin)

    def test_expired_admin_signature_returns_none(self):
        token = auth.sign_admin_session("admin", int(time.time()) - 1)

        admin = auth.current_admin_from_header(f"{config.ADMIN_SESSION_COOKIE}={token}")

        self.assertIsNone(admin)


if __name__ == "__main__":
    unittest.main()
