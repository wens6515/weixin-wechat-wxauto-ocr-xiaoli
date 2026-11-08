# -*- coding: utf-8 -*-
"""新手引导后端测试：状态清单、完成/重置标记、文档链接、密钥不外泄。"""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app.webbridge import BridgeApi, DOCS_URL


def _bridge(tmp):
    cfg = {
        "onboarding_done": False,
        "active_card_id": "xiaoli",
        "bot_nickname": "小漓",
        "chat_model": "deepseek:deepseek-flash",
        "file_storage_path": tmp,
        "providers": [{"id": "deepseek", "name": "DeepSeek",
                       "base_url": "https://api.deepseek.com/v1",
                       "api_key": "sk-onboarding-secret-42",
                       "models": ["deepseek:deepseek-flash"]}],
    }
    ctx = SimpleNamespace(cfg=cfg, cfg_path=os.path.join(tmp, "config.json"),
                          cards_dir=os.path.join(tmp, "cards"),
                          engine=SimpleNamespace(state="idle", bot=None))
    return BridgeApi(ctx)


class TestOnboardingStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onb_")
        self.b = _bridge(self.tmp)
        self.b._active_card = lambda: {
            "id": "xiaoli", "name": "小漓", "nickname": "小漓",
            "chat_provider": "deepseek", "chat_model": "deepseek:deepseek-flash",
        }

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_status_has_no_key_and_docs_link(self):
        report = {"wechat": {"ok": True, "detail": "检测到微信窗口"},
                  "scale": {"ok": True, "detail": "175% ✓"}}
        with mock.patch("xiaoli_app.webbridge.setup.check_environment",
                        return_value=report), \
                mock.patch("wx_backend.visual_win32.find_wechat_window",
                           return_value=None):
            st = self.b.onboarding_status()
        self.assertTrue(st["ok"])
        self.assertEqual(st["docs_url"], DOCS_URL)
        self.assertTrue(st["api"]["ok"])
        self.assertNotIn("sk-onboarding-secret-42", json.dumps(st, ensure_ascii=False))
        self.assertTrue(st["files_dir"]["ok"])
        self.assertFalse(st["onboarding_done"])
        self.assertTrue(st["card"]["ok"])

    def test_missing_key_marks_model_not_ready(self):
        self.b.ctx.cfg["providers"][0]["api_key"] = ""
        with mock.patch("xiaoli_app.webbridge.setup.check_environment",
                        return_value={"wechat": {"ok": True, "detail": ""},
                                      "scale": {"ok": True, "detail": ""}}), \
                mock.patch("wx_backend.visual_win32.find_wechat_window",
                           return_value=None):
            st = self.b.onboarding_status()
        self.assertFalse(st["api"]["ok"])
        self.assertIn("API Key", st["api"]["detail"])


class TestOnboardingFlags(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onb_flag_")
        self.b = _bridge(self.tmp)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_complete_and_reset(self):
        r = self.b.onboarding_complete()
        self.assertTrue(r["ok"])
        self.assertTrue(self.b.ctx.cfg["onboarding_done"])
        with open(self.b.ctx.cfg_path, "r", encoding="utf-8") as f:
            disk = json.load(f)
        self.assertTrue(disk["onboarding_done"])

        r = self.b.onboarding_reset()
        self.assertTrue(r["ok"])
        self.assertFalse(self.b.ctx.cfg["onboarding_done"])

    def test_get_config_exposes_flag_and_docs(self):
        self.b.onboarding_complete()
        cfg = self.b.get_config()
        self.assertTrue(cfg["ok"])
        self.assertTrue(cfg["onboarding_done"])
        self.assertEqual(cfg["docs_url"], DOCS_URL)


class TestOnboardingMigration(unittest.TestCase):
    def test_fresh_config_not_done(self):
        from xiaoli_app import config_store
        tmp = tempfile.mkdtemp(prefix="onb_fresh_")
        try:
            cfg = config_store.load_config_store(
                os.path.join(tmp, "config.json"), os.path.join(tmp, "cards"))
            self.assertFalse(cfg["onboarding_done"])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_existing_config_marked_done(self):
        """升级用户：配置文件已存在且无 onboarding_done → 静默视为完成。"""
        from xiaoli_app import config_store
        tmp = tempfile.mkdtemp(prefix="onb_old_")
        try:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"ai_api_key": "sk-old-user",
                           "chat_model": "deepseek-chat"}, f)
            cfg = config_store.load_config_store(
                path, os.path.join(tmp, "cards"))
            self.assertTrue(cfg["onboarding_done"])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
