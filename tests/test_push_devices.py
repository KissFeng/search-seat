import unittest
from unittest.mock import patch

import app


class PushDeviceTests(unittest.TestCase):
    def test_save_push_device_disables_other_devices_for_user(self):
        row = {
            "id": 1,
            "platform": "android",
            "cid": "cid-new",
            "notifications_enabled": 1,
            "enabled": 1,
        }

        with patch.object(app.database, "execute", return_value=1) as execute, patch.object(
            app.database, "fetch_one", return_value=row
        ):
            device = app.save_push_device(7, {"cid": "cid-new", "notifications_enabled": True})

        self.assertEqual(device["cid"], "cid-new")
        self.assertEqual(execute.call_count, 2)
        disable_sql = execute.call_args_list[1].args[0]
        disable_params = execute.call_args_list[1].args[1]
        self.assertIn("cid <> %s", disable_sql)
        self.assertEqual(disable_params, (7, "cid-new"))

    def test_fetch_push_devices_for_users_keeps_latest_per_user(self):
        rows = [
            {"user_id": 1, "cid": "cid-new"},
            {"user_id": 1, "cid": "cid-old"},
            {"user_id": 2, "cid": "cid-2"},
        ]

        with patch.object(app.database, "fetch_all", return_value=rows):
            devices = app.fetch_push_devices_for_users([1, 2])

        self.assertEqual(devices, [{"user_id": 1, "cid": "cid-new"}, {"user_id": 2, "cid": "cid-2"}])

    def test_disable_push_device_ignores_empty_cid(self):
        with patch.object(app.database, "execute") as execute:
            changed = app.disable_push_device(7, "")

        self.assertEqual(changed, 0)
        execute.assert_not_called()

    def test_watch_push_uses_watch_notification_channel(self):
        alert = {
            "id": 3,
            "room_name": "二楼阅览区",
            "day": "2026-07-05",
            "start_time": "16:00",
            "end_time": "20:00",
            "matched_seats": ["344"],
            "reserve_url": "/reserve?seat=344",
        }

        with patch.object(app, "getui_post", return_value={"code": 0}) as getui_post:
            app.push_getui_watch_alert_to_cid("cid-1", alert)

        request = getui_post.call_args.args[1]
        notification = request["push_message"]["notification"]
        self.assertEqual(notification["channel_id"], "seat_match_alerts_v3")
        self.assertEqual(notification["channel_name"], "座位命中提醒")
        self.assertEqual(notification["channel_level"], 4)

    def test_admin_push_uses_admin_notification_channel(self):
        with patch.object(app, "getui_post", return_value={"code": 0}) as getui_post:
            app.push_getui_admin_message_to_cid("cid-1", "标题", "内容", "/")

        request = getui_post.call_args.args[1]
        notification = request["push_message"]["notification"]
        self.assertEqual(notification["channel_id"], "admin_messages_v1")
        self.assertEqual(notification["channel_name"], "后台消息提醒")
        self.assertEqual(notification["channel_level"], 4)


if __name__ == "__main__":
    unittest.main()
