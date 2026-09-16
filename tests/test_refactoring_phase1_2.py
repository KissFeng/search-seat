#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import io
import os
import unittest
from unittest.mock import MagicMock, patch

import database
from services.template_service import get_template
from services.chat_service import delete_chat_messages
from services.update_service import parse_multipart_form, stream_apk_file, MAX_UPLOAD_SIZE
from services.transcript_service import save_academic_transcript, fetch_recent_academic_transcripts
from services.seat_service import build_seat_response


class RefactoringPhase1And2Tests(unittest.TestCase):
    def test_database_connection_pool_acquire_and_release(self):
        pool = database.ConnectionPool(max_connections=2)
        mock_conn = MagicMock()
        mock_conn.open = True
        mock_conn.ping.return_value = None

        with patch("pymysql.connect", return_value=mock_conn):
            conn1 = pool.get_connection()
            self.assertIsNotNone(conn1)
            self.assertEqual(pool._created, 1)

            pool.release_connection(conn1)
            self.assertEqual(pool._pool.qsize(), 1)

            # Re-acquire should reuse from pool
            conn2 = pool.get_connection()
            self.assertEqual(conn1, conn2)
            pool.release_connection(conn2)
            pool.close_all()

    def test_database_connection_context_manager(self):
        mock_conn = MagicMock()
        mock_conn.open = True
        mock_conn.ping.return_value = None
        mock_pool = MagicMock()
        mock_pool.get_connection.return_value = mock_conn

        with patch("database.get_pool", return_value=mock_pool):
            with database.connection() as conn:
                self.assertEqual(conn, mock_conn)
            mock_pool.release_connection.assert_called_once_with(mock_conn)

    def test_delete_chat_messages_empty_list_returns_false_without_sql(self):
        mock_db = MagicMock()
        with patch("services.chat_service.database", mock_db):
            result = delete_chat_messages({"mode": "ids", "ids": []})
            self.assertEqual(result, 0)
            mock_db.execute.assert_not_called()

    def test_save_academic_transcript_stores_student_info(self):
        mock_db = MagicMock()
        mock_db.execute.return_value = 101
        mock_db.fetch_one.return_value = {
            "id": 101,
            "user_id": 1,
            "pdf_url": "https://example.com/t.pdf",
            "student_name": "张三",
            "student_no": "2023001",
            "student_class": "软件1班",
            "student_major": "软件工程",
            "student_college": "计算机学院",
            "created_at": "2026-09-15 12:00:00",
        }
        mock_db.fetch_all.return_value = []

        student_info = {
            "xsjbxx_xh": "2023001",
            "xsjbxx_xm": "张三",
            "xsjbxx_bjxx": "软件1班",
            "xsjbxx_zy": "软件工程",
            "xsjbxx_yx": "计算机学院",
        }

        with patch("services.transcript_service._get_db", return_value=mock_db):
            res = save_academic_transcript(
                user_id=1,
                pdf_url="https://example.com/t.pdf",
                student_info=student_info,
            )

            self.assertEqual(res["id"], 101)
            self.assertEqual(res["student_no"], "2023001")
            self.assertEqual(res["student_name"], "张三")

            # Verify insert SQL received the student parameters
            insert_call = mock_db.execute.call_args_list[0]
            params = insert_call[0][1]
            self.assertEqual(params[1], "张三")
            self.assertEqual(params[2], "2023001")
            self.assertEqual(params[3], "软件1班")
            self.assertEqual(params[4], "计算机学院")
            self.assertEqual(params[5], "软件工程")

    def test_fetch_recent_academic_transcripts_includes_student_fields(self):
        mock_db = MagicMock()
        mock_db.fetch_all.return_value = [
            {
                "id": 101,
                "pdf_url": "https://example.com/t.pdf",
                "student_no": "2023001",
                "student_name": "张三",
                "student_class": "软件1班",
                "student_major": "软件工程",
                "student_college": "计算机学院",
                "created_at": "2026-09-15 12:00:00",
            }
        ]

        with patch("services.transcript_service._get_db", return_value=mock_db):
            items = fetch_recent_academic_transcripts(user_id=1, limit=5)
            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual(item["student_no"], "2023001")
            self.assertEqual(item["student_college"], "计算机学院")

    def test_parse_multipart_form_size_limit(self):
        handler = MagicMock()
        handler.headers = {
            "Content-Type": "multipart/form-data; boundary=----WebKitFormBoundary7MA4YWxkTrZu0gW",
            "Content-Length": str(MAX_UPLOAD_SIZE + 100),
        }
        with self.assertRaisesRegex(ValueError, "上传文件体积过大"):
            parse_multipart_form(handler)

    def test_stream_apk_file(self):
        fake_content = b"A" * 150000
        handler = MagicMock()
        handler.wfile = io.BytesIO()
        with patch("os.path.getsize", return_value=len(fake_content)), \
             patch("builtins.open", return_value=io.BytesIO(fake_content)):
            stream_apk_file("/fake/path/app.apk", handler, chunk_size=65536)
            handler.send_response.assert_called_once_with(200)
            self.assertEqual(handler.wfile.getvalue(), fake_content)

    def test_get_template_index_and_admin(self):
        index_html = get_template("index.html")
        self.assertIn("<!doctype html>", index_html.lower())
        self.assertIn("工职大座位雷达", index_html)

        admin_html = get_template("admin.html")
        self.assertIn("<!doctype html>", admin_html.lower())
        self.assertIn("座位雷达管理后台", admin_html)

    def test_build_seat_response_default(self):
        room = {"room_id": "12818", "label": "2F-阅览区", "seat_min": 1, "seat_max": 5, "seat_width": 3}
        mock_result = {
            "success": True,
            "data": {
                "seatReservationList": []
            }
        }
        payload = {"ignore_no_power": False, "ignore_sunny": False}
        response = build_seat_response(room, "2026-09-15", "08:00", "12:00", mock_result, payload)
        self.assertEqual(response["room_id"], "12818")
        self.assertEqual(len(response["available"]), 5)
        self.assertEqual(response["pairs"], [])
        self.assertEqual(response["occupied_details"], [])
        self.assertEqual(response["summary"]["total"], 5)
        self.assertEqual(response["summary"]["available"], 5)
        self.assertEqual(response["summary"]["occupied"], 0)
        self.assertEqual(response["summary"]["filtered"], 0)

    def test_build_seat_response_with_pairs(self):
        room = {"room_id": "12818", "label": "2F-阅览区", "seat_min": 1, "seat_max": 5, "seat_width": 3}
        mock_result = {
            "success": True,
            "data": {
                "seatReservationList": []
            }
        }
        payload = {"ignore_no_power": False, "ignore_sunny": False}
        response = build_seat_response(room, "2026-09-15", "08:00", "12:00", mock_result, payload, include_pairs=True)
        self.assertEqual(response["room_id"], "12818")
        self.assertEqual(len(response["available"]), 5)
        self.assertIsInstance(response["pairs"], list)
        self.assertGreater(len(response["pairs"]), 0)
        first_pair = response["pairs"][0]
        self.assertIn("seats", first_pair)
        self.assertIn("url", first_pair)
        self.assertIn("-", first_pair["url"])


if __name__ == "__main__":
    unittest.main()
