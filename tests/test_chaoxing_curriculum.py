import unittest
import json

import chaoxing


class CurriculumProfileTests(unittest.TestCase):
    def test_extracts_user_name(self):
        result = {
            "result": 1,
            "data": {
                "curriculum": {
                    "userName": "赵杰",
                },
            },
        }

        self.assertEqual(chaoxing.parse_curriculum_user_name(result), "赵杰")

    def test_rejects_missing_user_name(self):
        with self.assertRaises(RuntimeError):
            chaoxing.parse_curriculum_user_name({"result": 1, "data": {"curriculum": {}}})

    def test_rejects_failed_result(self):
        with self.assertRaises(RuntimeError):
            chaoxing.parse_curriculum_user_name({"result": 0, "msg": "未登录"})

    def test_cookie_json_to_header(self):
        cookies_json = json.dumps(
            [
                {"name": "UID", "value": "123"},
                {"name": "vc3", "value": "abc"},
                {"name": "", "value": "ignored"},
            ]
        )

        self.assertEqual(chaoxing.cookie_json_to_header(cookies_json), "UID=123; vc3=abc")


if __name__ == "__main__":
    unittest.main()
