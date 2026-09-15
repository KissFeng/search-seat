import time
import unittest
from unittest.mock import MagicMock, patch

import app
import chaoxing
from services import user_service

COOKIE_JSON = '[{"name": "SESSION", "value": "abc"}]'


class CurrentReservesSWRTests(unittest.TestCase):
    def setUp(self):
        user_service.invalidate_user_reserves_cache(101)
        user_service.invalidate_user_reserves_cache(102)

    def tearDown(self):
        user_service.invalidate_user_reserves_cache(101)
        user_service.invalidate_user_reserves_cache(102)

    @patch("services.user_service.chaoxing.fetch_seat_index")
    @patch("services.user_service.database.execute")
    @patch("services.user_service.database.fetch_one", return_value=None)
    def test_fresh_cache_hit_does_not_call_chaoxing(self, mock_db_one, mock_db_exec, mock_fetch_cx):
        mock_fetch_cx.return_value = {
            "success": True,
            "data": {
                "curReserves": [
                    {
                        "id": 123,
                        "roomId": 12818,
                        "roomName": "2F-阅览区",
                        "seatNum": "100",
                        "firstReserveTime": "1773532800000",
                        "secondReserveTime": "1773536400000",
                        "todayStatus": 1,
                    }
                ]
            },
        }

        # 首次查询，穿透
        first = user_service.fetch_current_reserves_from_cookies(101, COOKIE_JSON)
        self.assertEqual(len(first["reserves"]), 1)
        self.assertEqual(first["reserves"][0]["seat_num"], "100")
        self.assertEqual(mock_fetch_cx.call_count, 1)

        # 紧接着在 Fresh TTL 内查询，应直接命中内存缓存，不再调用 fetch_seat_index
        second = user_service.fetch_current_reserves_from_cookies(101, COOKIE_JSON)
        self.assertEqual(second["reserves"][0]["seat_num"], "100")
        self.assertEqual(mock_fetch_cx.call_count, 1)

    @patch("services.user_service.chaoxing.fetch_seat_index")
    @patch("services.user_service.database.execute")
    @patch("services.user_service.database.fetch_one", return_value=None)
    def test_force_refresh_bypasses_cache(self, mock_db_one, mock_db_exec, mock_fetch_cx):
        mock_fetch_cx.return_value = {
            "success": True,
            "data": {
                "curReserves": [
                    {
                        "id": 123,
                        "roomId": 12818,
                        "roomName": "2F-阅览区",
                        "seatNum": "100",
                        "firstReserveTime": "1773532800000",
                        "secondReserveTime": "1773536400000",
                        "todayStatus": 1,
                    }
                ]
            },
        }

        user_service.fetch_current_reserves_from_cookies(101, COOKIE_JSON)
        self.assertEqual(mock_fetch_cx.call_count, 1)

        # force=True 强制刷新，应当再次调用
        user_service.fetch_current_reserves_from_cookies(101, COOKIE_JSON, force=True)
        self.assertEqual(mock_fetch_cx.call_count, 2)

    @patch("services.user_service.chaoxing.fetch_seat_index")
    @patch("services.user_service.database.execute")
    @patch("services.user_service.database.fetch_one", return_value=None)
    def test_invalidate_user_reserves_cache_clears_memory(self, mock_db_one, mock_db_exec, mock_fetch_cx):
        mock_fetch_cx.return_value = {
            "success": True,
            "data": {"curReserves": []},
        }

        user_service.fetch_current_reserves_from_cookies(101, COOKIE_JSON)
        self.assertEqual(mock_fetch_cx.call_count, 1)

        user_service.invalidate_user_reserves_cache(101)

        user_service.fetch_current_reserves_from_cookies(101, COOKIE_JSON)
        self.assertEqual(mock_fetch_cx.call_count, 2)

    @patch("services.user_service.chaoxing.fetch_seat_index")
    @patch("services.user_service.database.execute")
    @patch("services.user_service.database.fetch_one", return_value=None)
    def test_stale_cache_returns_immediately_and_revalidates(self, mock_db_one, mock_db_exec, mock_fetch_cx):
        mock_fetch_cx.return_value = {
            "success": True,
            "data": {"curReserves": []},
        }

        # 先注入一条 30 秒前的缓存
        with user_service._CURRENT_RESERVES_CACHE_LOCK:
            user_service._CURRENT_RESERVES_CACHE[101] = {
                "timestamp": time.time() - 30,  # 超过 FRESH_TTL_SECONDS(20s)，但在 STALE_TTL 内
                "data": {"reserves": [{"seat_num": "200"}], "error": "", "updated_at": "test", "stale": False},
            }

        result = user_service.fetch_current_reserves_from_cookies(101, COOKIE_JSON)
        # 立即返回 stale 数据
        self.assertTrue(result.get("stale"))
        self.assertEqual(result["reserves"][0]["seat_num"], "200")

    def test_fetch_admin_current_reserve_rows_handles_multiple_rows_parallel(self):
        rows = [
            {"id": 1, "cookies_json": COOKIE_JSON},
            {"id": 2, "cookies_json": COOKIE_JSON},
        ]
        mock_reserve = [{"seat_num": "101", "status_label": "预约成功"}]

        with patch.object(app.database, "fetch_all", return_value=rows), patch.object(
            app,
            "fetch_current_reserves_from_cookies",
            return_value={"reserves": mock_reserve, "error": "", "updated_at": "", "stale": False},
        ) as fetch_reserves:
            results = app.fetch_admin_current_reserve_rows()

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["current_reserves"], mock_reserve)
        self.assertEqual(results[1]["current_reserves"], mock_reserve)
        self.assertEqual(fetch_reserves.call_count, 2)

    @patch("chaoxing.prepare_office_index_session")
    def test_chaoxing_fetch_seat_index_fast_path_avoids_prepare_on_success(self, mock_prepare):
        session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"success": True, "data": {}}
        session.get.return_value = mock_resp

        result = chaoxing.fetch_seat_index(session, "test_fid")
        self.assertTrue(result["success"])
        mock_prepare.assert_not_called()
