import hashlib
import unittest

import app


class AppUpdateTests(unittest.TestCase):
    def test_android_version_code_from_params_accepts_positive_integer(self):
        value = app.android_version_code_from_params({"version_code": ["6"]})

        self.assertEqual(value, 6)

    def test_android_version_code_from_params_rejects_invalid_values(self):
        with self.assertRaisesRegex(ValueError, "version_code 必须是正整数"):
            app.android_version_code_from_params({"version_code": ["0"]})
        with self.assertRaisesRegex(ValueError, "version_code 必须是正整数"):
            app.android_version_code_from_params({"version_code": ["bad"]})

    def test_safe_apk_filename_accepts_apk_basename(self):
        self.assertEqual(app.safe_apk_filename("SearchSeat-7.apk"), "SearchSeat-7.apk")

    def test_safe_apk_filename_rejects_path_traversal_and_non_apk(self):
        with self.assertRaisesRegex(ValueError, "APK 文件名不正确"):
            app.safe_apk_filename("../bad.apk")
        with self.assertRaisesRegex(ValueError, "APK 文件名不正确"):
            app.safe_apk_filename("bad\r.apk")
        with self.assertRaisesRegex(ValueError, "只支持上传 APK 文件"):
            app.safe_apk_filename("bad.txt")

    def test_public_app_version_payload_includes_download_url(self):
        row = {
            "id": 3,
            "platform": "android",
            "version_code": 7,
            "version_name": "1.6",
            "apk_filename": "search-seat-7.apk",
            "apk_size": 123,
            "apk_sha256": "a" * 64,
            "release_notes": "修复通知",
            "force_update": 1,
            "published": 1,
            "created_at": "2026-07-05 18:00:00",
        }

        payload = app.public_app_version(row, "http://127.0.0.1:8000")

        self.assertEqual(payload["version_code"], 7)
        self.assertEqual(payload["version_name"], "1.6")
        self.assertEqual(payload["apk_url"], "http://127.0.0.1:8000/downloads/apks/search-seat-7.apk")
        self.assertTrue(payload["force_update"])

    def test_app_update_payload_returns_false_when_no_newer_version(self):
        payload = app.app_update_payload(None, 7, "http://127.0.0.1:8000")

        self.assertEqual(payload, {"update": False})

    def test_app_update_payload_returns_newer_version(self):
        row = {
            "id": 3,
            "platform": "android",
            "version_code": 8,
            "version_name": "1.7",
            "apk_filename": "search-seat-8.apk",
            "apk_size": 456,
            "apk_sha256": "b" * 64,
            "release_notes": "",
            "force_update": 0,
            "published": 1,
            "created_at": "2026-07-05 18:00:00",
        }

        payload = app.app_update_payload(row, 7, "http://127.0.0.1:8000")

        self.assertTrue(payload["update"])
        self.assertEqual(payload["version_code"], 8)

    def test_sha256_hex_hashes_bytes(self):
        digest = app.sha256_hex(b"abc")

        self.assertEqual(digest, hashlib.sha256(b"abc").hexdigest())

    def test_apk_upload_metadata_builds_stable_filename(self):
        metadata = app.apk_upload_metadata(
            original_filename="Search Seat.apk",
            version_code=9,
            version_name="1.8",
            release_notes="优化更新",
            force_update=True,
            data=b"apk bytes",
        )

        self.assertEqual(metadata["platform"], "android")
        self.assertEqual(metadata["version_code"], 9)
        self.assertEqual(metadata["version_name"], "1.8")
        self.assertEqual(metadata["apk_filename"], "search-seat-9.apk")
        self.assertEqual(metadata["apk_size"], 9)
        self.assertEqual(metadata["apk_sha256"], hashlib.sha256(b"apk bytes").hexdigest())
        self.assertEqual(metadata["release_notes"], "优化更新")
        self.assertEqual(metadata["force_update"], 1)

    def test_apk_upload_metadata_requires_apk_bytes(self):
        with self.assertRaisesRegex(ValueError, "APK 文件不能为空"):
            app.apk_upload_metadata("x.apk", 10, "1.9", "", False, b"")


if __name__ == "__main__":
    unittest.main()
