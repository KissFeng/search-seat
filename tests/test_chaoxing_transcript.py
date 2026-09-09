#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from unittest.mock import MagicMock, patch

import chaoxing


class ChaoxingTranscriptTestCase(unittest.TestCase):
    def test_fetch_shelf_token_success(self):
        session = MagicMock()
        resp = MagicMock()
        resp.url = "https://grayshelf.chaoxing.com/#/sign-in?key=scoreQueryStudentMobile&token=mock_jwt_token_123&roleType=2"
        session.get.return_value = resp

        token = chaoxing.fetch_shelf_token(session, fid="1035", mapp_id="18690790")
        self.assertEqual(token, "mock_jwt_token_123")
        session.get.assert_called_once()

    def test_fetch_shelf_token_forbidden(self):
        session = MagicMock()
        resp = MagicMock()
        resp.url = "https://i.chaoxing.com/errorTips-403.html"
        session.get.return_value = resp

        with self.assertRaises(RuntimeError) as ctx:
            chaoxing.fetch_shelf_token(session, fid="1035", mapp_id="18690790")
        self.assertIn("403", str(ctx.exception))

    def test_search_academic_student(self):
        session = MagicMock()
        resp = MagicMock()
        resp.json.return_value = {
            "code": 200,
            "success": True,
            "data": {
                "rows": [
                    {
                        "xsjbxx_xh": "202335810165",
                        "xsjbxx_xm": "徐爱姣",
                        "xsjbxx_bjxx": "23软件工程技术1班",
                    }
                ]
            },
        }
        session.post.return_value = resp

        data = chaoxing.search_academic_student(session, token="test_token", student_no="202335810165")
        self.assertTrue(data["success"])
        self.assertEqual(data["data"]["rows"][0]["xsjbxx_xm"], "徐爱姣")

    def test_export_academic_score_pdf(self):
        session = MagicMock()
        resp = MagicMock()
        resp.json.return_value = {
            "code": 200,
            "success": True,
            "data": "https://s3.cldisk.com/mock/transcript.pdf",
        }
        session.post.return_value = resp

        url = chaoxing.export_academic_score_pdf(session, token="test_token", student_no="202335810165")
        self.assertEqual(url, "https://s3.cldisk.com/mock/transcript.pdf")

    def test_fetch_transcript_pdf_url_full_flow(self):
        session = MagicMock()
        with patch.object(chaoxing, "fetch_shelf_token", return_value="jwt_token_abc"), \
             patch.object(chaoxing, "search_academic_student", return_value={
                 "code": 200,
                 "success": True,
                 "data": {
                     "rows": [
                         {
                             "xsjbxx_xh": "202335810165",
                             "xsjbxx_xm": "测试学生",
                         }
                     ]
                 }
             }), \
             patch.object(chaoxing, "export_academic_score_pdf", return_value="https://s3.cldisk.com/mock/student.pdf"):

            result = chaoxing.fetch_transcript_pdf_url(session=session)
            self.assertTrue(result["ok"])
            self.assertEqual(result["pdf_url"], "https://s3.cldisk.com/mock/student.pdf")
            self.assertEqual(result["student_no"], "202335810165")
            self.assertEqual(result["student_name"], "测试学生")
            self.assertEqual(result["token"], "jwt_token_abc")


if __name__ == "__main__":
    unittest.main()
