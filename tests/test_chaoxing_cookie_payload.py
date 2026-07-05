import json
import unittest

import app


class ChaoxingCookiePayloadTests(unittest.TestCase):
    def test_cookie_payload_keeps_domain_path_and_secure_fields(self):
        cookies_json = json.dumps(
            [
                {
                    "name": "UID",
                    "value": "123",
                    "domain": ".chaoxing.com",
                    "path": "/",
                    "secure": True,
                },
                {
                    "name": "vc3",
                    "value": "abc",
                    "domain": "passport2.chaoxing.com",
                    "path": "/",
                    "secure": False,
                },
            ]
        )

        payload = app.public_chaoxing_cookie_payload(cookies_json)

        self.assertEqual(
            payload,
            {
                "ok": True,
                "cookies": [
                    {
                        "name": "UID",
                        "value": "123",
                        "domain": ".chaoxing.com",
                        "path": "/",
                        "secure": True,
                    },
                    {
                        "name": "vc3",
                        "value": "abc",
                        "domain": "passport2.chaoxing.com",
                        "path": "/",
                        "secure": False,
                    },
                ],
            },
        )

    def test_cookie_payload_skips_items_without_name(self):
        cookies_json = json.dumps(
            [
                {"name": "", "value": "bad", "domain": ".chaoxing.com"},
                {"value": "bad", "domain": ".chaoxing.com"},
                {"name": "UID", "value": "123", "domain": ".chaoxing.com"},
            ]
        )

        payload = app.public_chaoxing_cookie_payload(cookies_json)

        self.assertEqual(len(payload["cookies"]), 1)
        self.assertEqual(payload["cookies"][0]["name"], "UID")


if __name__ == "__main__":
    unittest.main()
