#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import datetime, timezone, timedelta
import unittest
from unittest.mock import MagicMock, patch

from services import sync_service

BEIJING_TZ = timezone(timedelta(hours=8))


class AutoSyncTests(unittest.TestCase):
    def test_is_in_sync_window(self):
        # 07:59 不在窗口内
        dt_early = datetime(2026, 9, 16, 7, 59, 0, tzinfo=BEIJING_TZ)
        self.assertFalse(sync_service.is_in_sync_window(dt_early))

        # 08:00 在窗口内
        dt_start = datetime(2026, 9, 16, 8, 0, 0, tzinfo=BEIJING_TZ)
        self.assertTrue(sync_service.is_in_sync_window(dt_start))

        # 中午 12:30 在窗口内
        dt_noon = datetime(2026, 9, 16, 12, 30, 0, tzinfo=BEIJING_TZ)
        self.assertTrue(sync_service.is_in_sync_window(dt_noon))

        # 22:45 在窗口内
        dt_night = datetime(2026, 9, 16, 22, 45, 0, tzinfo=BEIJING_TZ)
        self.assertTrue(sync_service.is_in_sync_window(dt_night))

        # 23:00:00 在窗口内（结束边界点）
        dt_end = datetime(2026, 9, 16, 23, 0, 0, tzinfo=BEIJING_TZ)
        self.assertTrue(sync_service.is_in_sync_window(dt_end))

        # 23:00:01 超过窗口
        dt_end_past = datetime(2026, 9, 16, 23, 0, 1, tzinfo=BEIJING_TZ)
        self.assertFalse(sync_service.is_in_sync_window(dt_end_past))

        # 23:15:00 超过窗口
        dt_late = datetime(2026, 9, 16, 23, 15, 0, tzinfo=BEIJING_TZ)
        self.assertFalse(sync_service.is_in_sync_window(dt_late))

        # 凌晨 00:00 不在窗口内
        dt_midnight = datetime(2026, 9, 17, 0, 0, 0, tzinfo=BEIJING_TZ)
        self.assertFalse(sync_service.is_in_sync_window(dt_midnight))

    def test_get_sync_slot_key(self):
        # 08:00 命中
        dt1 = datetime(2026, 9, 16, 8, 0, 0, tzinfo=BEIJING_TZ)
        self.assertEqual(sync_service.get_sync_slot_key(dt1), "2026-09-16 08:00")

        # 08:07 未命中
        dt2 = datetime(2026, 9, 16, 8, 7, 0, tzinfo=BEIJING_TZ)
        self.assertIsNone(sync_service.get_sync_slot_key(dt2))

        # 08:15 命中
        dt3 = datetime(2026, 9, 16, 8, 15, 0, tzinfo=BEIJING_TZ)
        self.assertEqual(sync_service.get_sync_slot_key(dt3), "2026-09-16 08:15")

        # 08:30 命中
        dt4 = datetime(2026, 9, 16, 8, 30, 0, tzinfo=BEIJING_TZ)
        self.assertEqual(sync_service.get_sync_slot_key(dt4), "2026-09-16 08:30")

        # 08:45 命中
        dt5 = datetime(2026, 9, 16, 8, 45, 0, tzinfo=BEIJING_TZ)
        self.assertEqual(sync_service.get_sync_slot_key(dt5), "2026-09-16 08:45")

        # 23:00 命中
        dt6 = datetime(2026, 9, 16, 23, 0, 0, tzinfo=BEIJING_TZ)
        self.assertEqual(sync_service.get_sync_slot_key(dt6), "2026-09-16 23:00")

        # 23:15 超过窗口未命中
        dt7 = datetime(2026, 9, 16, 23, 15, 0, tzinfo=BEIJING_TZ)
        self.assertIsNone(sync_service.get_sync_slot_key(dt7))

        # 07:45 窗口外未命中
        dt8 = datetime(2026, 9, 16, 7, 45, 0, tzinfo=BEIJING_TZ)
        self.assertIsNone(sync_service.get_sync_slot_key(dt8))

    @patch("services.sync_service.database.fetch_all")
    def test_fetch_users_for_auto_sync(self, mock_fetch_all):
        mock_fetch_all.return_value = [
            {"id": 1, "username": "alice", "cookies_json": '[{"name": "a"}]'},
            {"id": 2, "username": "bob", "cookies_json": '[{"name": "b"}]'},
        ]
        users = sync_service.fetch_users_for_auto_sync()
        self.assertEqual(len(users), 2)
        mock_fetch_all.assert_called_once()
        sql = mock_fetch_all.call_args[0][0]
        self.assertIn("disabled_at IS NULL", sql)
        self.assertIn("session_valid = 1", sql)

    @patch("services.sync_service.user_service.fetch_current_reserves_from_cookies")
    def test_sync_single_user_success(self, mock_fetch):
        mock_fetch.return_value = {"reserves": [{"id": 101}], "error": ""}
        ok = sync_service.sync_single_user({"id": 1, "username": "alice", "cookies_json": "[]"})
        self.assertTrue(ok)
        mock_fetch.assert_called_once_with(1, "[]", force=True)

    @patch("services.sync_service.user_service.fetch_current_reserves_from_cookies")
    def test_sync_single_user_failure(self, mock_fetch):
        mock_fetch.return_value = {"reserves": [], "error": "Cookie已失效"}
        ok = sync_service.sync_single_user({"id": 1, "username": "alice", "cookies_json": "[]"})
        self.assertFalse(ok)

        mock_fetch.side_effect = RuntimeError("网络异常")
        ok_exc = sync_service.sync_single_user({"id": 1, "username": "alice", "cookies_json": "[]"})
        self.assertFalse(ok_exc)

    @patch("services.sync_service.fetch_users_for_auto_sync")
    @patch("services.sync_service.user_service.fetch_current_reserves_from_cookies")
    def test_sync_all_users_reserves(self, mock_fetch_reserves, mock_fetch_users):
        mock_fetch_users.return_value = [
            {"id": 10, "username": "user1", "cookies_json": '[{"name":"c1"}]'},
            {"id": 20, "username": "user2", "cookies_json": '[{"name":"c2"}]'},
        ]
        mock_fetch_reserves.return_value = {"reserves": [], "error": ""}

        res = sync_service.sync_all_users_reserves(max_workers=2)
        self.assertEqual(res["total"], 2)
        self.assertEqual(res["success"], 2)
        self.assertEqual(res["failed"], 0)
        self.assertEqual(mock_fetch_reserves.call_count, 2)

    @patch("services.sync_service.fetch_users_for_auto_sync")
    @patch("services.sync_service.user_service.fetch_current_reserves_from_cookies")
    def test_sync_all_users_handles_partial_failure(self, mock_fetch_reserves, mock_fetch_users):
        mock_fetch_users.return_value = [
            {"id": 10, "username": "user1", "cookies_json": '[{"name":"c1"}]'},
            {"id": 20, "username": "user2", "cookies_json": '[{"name":"c2"}]'},
        ]

        def _side_effect(user_id, cookies_json, force=False):
            if user_id == 10:
                return {"reserves": [{"id": 1}], "error": ""}
            else:
                return {"reserves": [], "error": "学习通接口失败"}

        mock_fetch_reserves.side_effect = _side_effect

        res = sync_service.sync_all_users_reserves(max_workers=2)
        self.assertEqual(res["total"], 2)
        self.assertEqual(res["success"], 1)
        self.assertEqual(res["failed"], 1)

    def test_sync_worker_lock_prevents_reentry(self):
        sync_service.SYNC_WORKER_LOCK.acquire()
        try:
            res = sync_service.sync_all_users_reserves()
            self.assertTrue(res.get("skipped"))
            self.assertEqual(res.get("reason"), "already_running")
        finally:
            sync_service.SYNC_WORKER_LOCK.release()

    @patch("services.sync_service.threading.Thread")
    def test_start_auto_sync_worker_idempotent(self, mock_thread):
        sync_service.AUTO_SYNC_WORKER_STARTED = True
        sync_service.start_auto_sync_worker()
        mock_thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
