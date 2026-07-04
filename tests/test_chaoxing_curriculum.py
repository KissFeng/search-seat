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

    def test_parse_login_failure_raises_auth_error(self):
        with self.assertRaises(chaoxing.ChaoxingAuthError) as ctx:
            chaoxing.parse_login_result({"status": False, "msg2": "用户名或密码错误"})

        self.assertEqual(str(ctx.exception), "学习通登录失败：用户名或密码错误")

    def test_parse_timetable_page_params(self):
        params = chaoxing.parse_timetable_page_params(
            "https://course.chaoxing.com/svcourse/new/showTable/myTable?"
            "taskId=134621&type=4&userId=1269250&isMyTable=true&tableType=7"
        )

        self.assertEqual(
            params,
            {
                "taskId": "134621",
                "type": "4",
                "userId": "1269250",
                "tableType": "7",
            },
        )

    def test_parse_timetable_page_params_rejects_missing_user_id(self):
        with self.assertRaises(ValueError) as ctx:
            chaoxing.parse_timetable_page_params(
                "https://course.chaoxing.com/svcourse/new/showTable/myTable?"
                "taskId=134621&type=4&tableType=7"
            )

        self.assertEqual(str(ctx.exception), "课表页面缺少 userId 参数")


if __name__ == "__main__":
    unittest.main()
