import unittest

import app


class ChatMessageTests(unittest.TestCase):
    def test_chat_message_text_must_be_non_empty_text(self):
        with self.assertRaisesRegex(ValueError, "请输入聊天内容"):
            app.chat_message_text_from_payload({"content": "   "})

    def test_chat_message_text_limits_length(self):
        with self.assertRaisesRegex(ValueError, "聊天内容不能超过 500 字"):
            app.chat_message_text_from_payload({"content": "字" * 501})

    def test_public_chat_message_prefers_real_name(self):
        message = app.public_chat_message(
            {
                "id": 7,
                "user_id": 3,
                "username": "13800138000",
                "cx_user_name": "张三",
                "content": "有人在图书馆吗？",
                "created_at": "2026-07-04 12:30:00",
            }
        )

        self.assertEqual(message["author_name"], "张三")
        self.assertEqual(message["content"], "有人在图书馆吗？")
        self.assertEqual(message["avatar"], "default")

    def test_public_chat_message_falls_back_to_username(self):
        message = app.public_chat_message(
            {
                "id": 8,
                "user_id": 4,
                "username": "13800138001",
                "cx_user_name": "",
                "content": "在",
                "created_at": "2026-07-04 12:31:00",
            }
        )

        self.assertEqual(message["author_name"], "13800138001")

    def test_admin_chat_delete_scope_accepts_ids(self):
        scope = app.admin_chat_delete_scope({"ids": [1, "2", "bad", 2]})

        self.assertEqual(scope["mode"], "ids")
        self.assertEqual(scope["ids"], [1, 2])

    def test_admin_chat_delete_scope_accepts_time_range(self):
        scope = app.admin_chat_delete_scope(
            {
                "start_at": "2026-07-04T08:00",
                "end_at": "2026-07-04 12:30",
            }
        )

        self.assertEqual(scope["mode"], "time")
        self.assertEqual(scope["start_at"], "2026-07-04 08:00:00")
        self.assertEqual(scope["end_at"], "2026-07-04 12:30:00")

    def test_admin_chat_delete_scope_requires_ids_or_time_range(self):
        with self.assertRaisesRegex(ValueError, "请选择要删除的消息或时间段"):
            app.admin_chat_delete_scope({})


if __name__ == "__main__":
    unittest.main()
