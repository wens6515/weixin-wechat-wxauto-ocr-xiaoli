# -*- coding: utf-8 -*-
"""A4 诊断导出测试：zip 内容、脱敏（key 不出包）、路径安全。"""
import json
import os
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app.webbridge import BridgeApi

SECRET = "sk-real-secret-1234567890"


def _bridge(tmpdir):
    cfg = {
        "ai_api_key": SECRET,
        "vision_api_key": "",
        "providers": [{"id": "deepseek", "base_url": "https://x",
                       "api_key": SECRET, "models": []}],
        "chat_card_params": {"林小满": {"ai_api_key": SECRET,
                                          "chat_model": "m"}},
        "bot_nickname": "小漓",
    }
    ctx = SimpleNamespace(cfg=cfg, cfg_path=os.path.join(tmpdir, "config.json"),
                          engine=SimpleNamespace(state="idle", bot=None))
    return BridgeApi(ctx)


class TestSanitizeCfg(unittest.TestCase):
    def test_keys_masked_everywhere(self):
        b = _bridge(tempfile.mkdtemp())
        out = b._sanitize_cfg_for_export(b.ctx.cfg)
        self.assertEqual(out["ai_api_key"], "sk-***7890")   # mask_key 口径
        for p in out["providers"]:
            self.assertNotIn(SECRET, json.dumps(p))
        for v in out["chat_card_params"].values():
            self.assertNotIn(SECRET, json.dumps(v))
        # 原配置不动（脱敏只作用于导出副本）
        self.assertEqual(b.ctx.cfg["ai_api_key"], SECRET)


class TestExportDiagnostics(unittest.TestCase):
    def test_zip_members_and_no_secret_leak(self):
        tmpdir = tempfile.mkdtemp()
        b = _bridge(tmpdir)
        with mock.patch("xiaoli_app.webbridge.app_base_dir",
                        return_value=tmpdir):
            r = b.export_diagnostics()
        self.assertTrue(r["ok"])
        self.assertTrue(os.path.isfile(r["path"]))
        self.assertIn("diagnostics", os.path.dirname(r["path"]))
        with zipfile.ZipFile(r["path"]) as zf:
            names = zf.namelist()
            self.assertIn("info.json", names)
            self.assertIn("config.json", names)
            blob = "".join(zf.read(n).decode("utf-8", errors="replace")
                           for n in names)
        self.assertNotIn(SECRET, blob)
        self.assertIn("小漓", blob)

    def test_no_tmp_files_left(self):
        tmpdir = tempfile.mkdtemp()
        b = _bridge(tmpdir)
        with mock.patch("xiaoli_app.webbridge.app_base_dir",
                        return_value=tmpdir):
            r = b.export_diagnostics()
        self.assertEqual([n for n in os.listdir(os.path.dirname(r["path"]))
                          if n.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
