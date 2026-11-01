# -*- coding: utf-8 -*-
"""A5 一键更新测试：安装包资产识别与定位（网络层 mock，不发真实请求）。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app import update_check

RELEASE_JSON = {
    "tag_name": "v3.5.0",
    "html_url": "https://github.com/x/releases/tag/v3.5.0",
    "assets": [
        {"name": "Source code (zip)", "size": 10, "browser_download_url": "https://x/src.zip"},
        {"name": "xiaoli-setup-v3.5.0.exe", "size": 241492467,
         "browser_download_url": "https://x/xiaoli-setup-v3.5.0.exe"},
    ],
}


class TestIsSetupAsset(unittest.TestCase):
    def test_setup_exe_hit(self):
        self.assertTrue(update_check.is_setup_asset("xiaoli-setup-v3.4.0.exe"))

    def test_other_assets_miss(self):
        self.assertFalse(update_check.is_setup_asset("Source code (zip)"))
        self.assertFalse(update_check.is_setup_asset("xiaoli-setup-v3.4.0.zip"))
        self.assertFalse(update_check.is_setup_asset("校验和.txt"))
        self.assertFalse(update_check.is_setup_asset(""))
        self.assertFalse(update_check.is_setup_asset(None))


class TestFetchReleaseAsset(unittest.TestCase):
    def test_found(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = RELEASE_JSON
        with mock.patch.object(update_check.requests, "get",
                               return_value=resp) as get:
            asset = update_check.fetch_release_asset("3.5.0")
        self.assertEqual(asset["name"], "xiaoli-setup-v3.5.0.exe")
        self.assertEqual(asset["size"], 241492467)
        self.assertEqual(asset["url"],
                         "https://x/xiaoli-setup-v3.5.0.exe")
        self.assertIn("v3.5.0", get.call_args.args[0])

    def test_skips_non_setup_assets(self):
        """仓库挂了别的附件（如校验和）也不能拿错——按名字过滤。"""
        data = dict(RELEASE_JSON)
        data["assets"] = [
            {"name": "checksums.txt", "size": 1, "browser_download_url": "https://x/c.txt"},
            {"name": "Source code (zip)", "size": 10, "browser_download_url": "https://x/src.zip"},
        ]
        resp = mock.Mock(status_code=200)
        resp.json.return_value = data
        with mock.patch.object(update_check.requests, "get", return_value=resp):
            self.assertIsNone(update_check.fetch_release_asset("3.5.0"))

    def test_http_error_returns_none(self):
        resp = mock.Mock(status_code=404)
        with mock.patch.object(update_check.requests, "get", return_value=resp):
            self.assertIsNone(update_check.fetch_release_asset("9.9.9"))

    def test_network_error_returns_none(self):
        import requests as rq
        with mock.patch.object(
                update_check.requests, "get",
                side_effect=rq.exceptions.ConnectionError("boom")):
            self.assertIsNone(update_check.fetch_release_asset("3.5.0"))

    def test_empty_version_returns_none(self):
        self.assertIsNone(update_check.fetch_release_asset(""))

    def test_tag_prefix_normalized(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = RELEASE_JSON
        with mock.patch.object(update_check.requests, "get",
                               return_value=resp) as get:
            update_check.fetch_release_asset("v3.5.0")
        # 不出现 vV3.5.0 双前缀
        self.assertIn("/releases/tags/v3.5.0", get.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
