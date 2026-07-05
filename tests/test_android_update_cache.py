import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN_ACTIVITY = ROOT / "android-webview/app/src/main/java/com/searchseat/webview/MainActivity.java"


class AndroidUpdateCacheTests(unittest.TestCase):
    def test_update_download_uses_versioned_apk_filename(self):
        source = MAIN_ACTIVITY.read_text(encoding="utf-8")

        self.assertIn("updateApkFile", source)
        self.assertIn("search-seat-update-\" + update.optInt(\"version_code\"", source)
        self.assertNotIn('new File(getCacheDir(), "search-seat-update.apk")', source)


if __name__ == "__main__":
    unittest.main()
