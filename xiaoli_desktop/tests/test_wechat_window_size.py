# -*- coding: utf-8 -*-
"""微信窗口尺寸固定：旧键迁移（wechat_window_rect → wechat_window_size）、
初始化只套用大小不移动位置（resize_window_visible 保左上角）、设置页桥接
方法（wechat_window_info / set_wechat_window_size）。"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wx_backend import visual_win32 as vwin
from wechat_bot import WeChatBot


# ---------- 旧键迁移 ----------

class TestWindowSizeMigration(unittest.TestCase):
    """wechat_window_rect（含位置，空=自动右半屏）→ wechat_window_size
    （只有尺寸，程序不移动位置）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="winsize_test_")
        self.cfg_path = os.path.join(self.tmp, "config.json")
        self.cards_dir = os.path.join(self.tmp, "cards")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self, extra):
        from xiaoli_app import config_store
        cfg = {"bot_nickname": "小漓", "ai_api_key": "k",
               "tasks_dir": "d", "file_storage_path": "d"}
        cfg.update(extra)
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        return config_store.load_config_store(self.cfg_path, self.cards_dir)

    def test_rect_list_migrates_to_size_only(self):
        out = self._load({"wechat_window_rect": [50, 50, 900, 920]})
        self.assertEqual(out["wechat_window_size"], [900, 920])
        self.assertNotIn("wechat_window_rect", out)

    def test_rect_off_migrates_to_none(self):
        out = self._load({"wechat_window_rect": "off"})
        self.assertIsNone(out.get("wechat_window_size"))
        self.assertNotIn("wechat_window_rect", out)

    def test_rect_empty_migrates_to_none(self):
        """旧空值 = 自动摆右半屏；新语义程序不移动窗口 → 不设尺寸。"""
        out = self._load({"wechat_window_rect": None})
        self.assertIsNone(out.get("wechat_window_size"))
        self.assertNotIn("wechat_window_rect", out)

    def test_existing_new_key_wins(self):
        out = self._load({"wechat_window_rect": [50, 50, 900, 920],
                          "wechat_window_size": [1300, 1610]})
        self.assertEqual(out["wechat_window_size"], [1300, 1610])

    def test_invalid_rect_dropped(self):
        out = self._load({"wechat_window_rect": [10, 20, 30]})
        self.assertIsNone(out.get("wechat_window_size"))

    def test_default_size_is_none_for_new_install(self):
        out = self._load({})
        self.assertIsNone(out.get("wechat_window_size"))


# ---------- Win32 层：只改大小不移动 ----------

class TestResizeWindowVisible(unittest.TestCase):
    def test_keeps_topleft_and_expands_margins(self):
        """x/y 取窗口当前值（位置不动），尺寸按可见内容语义外扩不可见外沿。"""
        with mock.patch.object(vwin, "window_rect", return_value=(100, 200, 1300, 1610)):
            with mock.patch.object(vwin, "visible_frame_margins",
                                   return_value=(10, 0, 8, 0)):
                with mock.patch.object(vwin, "position_window",
                                       return_value=True) as pos:
                    ok = vwin.resize_window_visible(0x1, 1200, 1500)
        self.assertTrue(ok)
        pos.assert_called_once_with(0x1, 100, 200, 1200 + 18, 1500)

    def test_no_window_returns_false(self):
        with mock.patch.object(vwin, "window_rect", return_value=None):
            self.assertFalse(vwin.resize_window_visible(0x1, 800, 600))

    def test_visible_window_size_minus_margins(self):
        with mock.patch.object(vwin, "window_rect", return_value=(0, 0, 1300, 1610)):
            with mock.patch.object(vwin, "visible_frame_margins",
                                   return_value=(10, 0, 10, 0)):
                self.assertEqual(vwin.visible_window_size(0x1), (1280, 1610))


# ---------- bot 初始化套用 ----------

def _make_bot(size):
    bot = WeChatBot.__new__(WeChatBot)
    bot.wechat_window_size = size
    return bot


class TestInitApplyWindowSize(unittest.TestCase):
    def test_no_size_no_action(self):
        bot = _make_bot(None)
        with mock.patch("wechat_bot.find_window_by_title") as find:
            with mock.patch("wechat_bot.resize_window_visible") as rz:
                bot._init_apply_window_size()
        find.assert_not_called()
        rz.assert_not_called()

    def test_size_applied_without_position(self):
        bot = _make_bot([1300, 1610])
        with mock.patch("wechat_bot.find_window_by_title", return_value=0x9):
            with mock.patch("wechat_bot.ensure_window_visible"):
                with mock.patch("wechat_bot.ctypes") as ct:
                    ct.windll.user32.IsZoomed.return_value = 0
                    with mock.patch("wechat_bot.resize_window_visible",
                                    return_value=True) as rz:
                        bot._init_apply_window_size()
        rz.assert_called_once_with(0x9, 1300, 1610)

    def test_maximized_restored_first(self):
        bot = _make_bot([1300, 1610])
        with mock.patch("wechat_bot.find_window_by_title", return_value=0x9):
            with mock.patch("wechat_bot.ensure_window_visible"):
                with mock.patch("wechat_bot.time.sleep"):
                    with mock.patch("wechat_bot.ctypes") as ct:
                        ct.windll.user32.IsZoomed.return_value = 1
                        with mock.patch("wechat_bot.resize_window_visible",
                                        return_value=True):
                            bot._init_apply_window_size()
        ct.windll.user32.ShowWindow.assert_called_once_with(0x9, 9)

    def test_invalid_size_skipped(self):
        bot = _make_bot(["大", "小"])
        with mock.patch("wechat_bot.find_window_by_title", return_value=0x9):
            with mock.patch("wechat_bot.ensure_window_visible"):
                with mock.patch("wechat_bot.ctypes") as ct:
                    ct.windll.user32.IsZoomed.return_value = 0
                    with mock.patch("wechat_bot.resize_window_visible") as rz:
                        bot._init_apply_window_size()
        rz.assert_not_called()

    def test_too_small_size_skipped(self):
        bot = _make_bot([100, 100])
        with mock.patch("wechat_bot.find_window_by_title", return_value=0x9):
            with mock.patch("wechat_bot.ensure_window_visible"):
                with mock.patch("wechat_bot.ctypes") as ct:
                    ct.windll.user32.IsZoomed.return_value = 0
                    with mock.patch("wechat_bot.resize_window_visible") as rz:
                        bot._init_apply_window_size()
        rz.assert_not_called()

    def test_window_missing_skipped(self):
        bot = _make_bot([1300, 1610])
        with mock.patch("wechat_bot.find_window_by_title", return_value=None):
            with mock.patch("wechat_bot.resize_window_visible") as rz:
                bot._init_apply_window_size()
        rz.assert_not_called()


# ---------- 桥接方法 ----------

class _BridgeBase(unittest.TestCase):
    def setUp(self):
        from xiaoli_app.webbridge import BridgeApi

        class _Ctx:
            cfg = {"wechat_window_size": [1300, 1610]}
            cfg_path = "unused.json"
            cards_dir = "unused"
            engine = None

        self.bridge = BridgeApi(_Ctx())
        p = mock.patch("xiaoli_app.webbridge.config_store.save_config")
        self.save = p.start()
        self.addCleanup(p.stop)


class TestWechatWindowInfo(_BridgeBase):
    def test_reports_configured_and_current(self):
        import wx_backend.visual_backend as vb
        with mock.patch.object(vb, "find_wechat_window", return_value=0x9):
            with mock.patch.object(vb, "visible_window_size",
                                   return_value=(1280, 1610)):
                r = self.bridge.wechat_window_info()
        self.assertTrue(r["ok"])
        self.assertEqual(r["configured"], [1300, 1610])
        self.assertEqual(r["current"], [1280, 1610])

    def test_no_window_current_none(self):
        import wx_backend.visual_backend as vb
        with mock.patch.object(vb, "find_wechat_window", return_value=None):
            r = self.bridge.wechat_window_info()
        self.assertTrue(r["ok"])
        self.assertIsNone(r["current"])


class TestSetWechatWindowSize(_BridgeBase):
    def test_save_and_apply_now(self):
        import wx_backend.visual_backend as vb
        with mock.patch.object(vb, "find_wechat_window", return_value=0x9):
            with mock.patch.object(vb, "ensure_window_visible"):
                with mock.patch.object(vb, "resize_window_visible",
                                       return_value=True) as rz:
                    r = self.bridge.set_wechat_window_size(1200, 1500)
        self.assertTrue(r["ok"])
        self.assertEqual(r["configured"], [1200, 1500])
        self.assertTrue(r["applied"])
        rz.assert_called_once_with(0x9, 1200, 1500)
        self.assertEqual(self.bridge.ctx.cfg["wechat_window_size"], [1200, 1500])
        self.save.assert_called_once()

    def test_no_window_saves_only(self):
        import wx_backend.visual_backend as vb
        with mock.patch.object(vb, "find_wechat_window", return_value=None):
            r = self.bridge.set_wechat_window_size(1200, 1500)
        self.assertTrue(r["ok"])
        self.assertFalse(r["applied"])
        self.assertIn("微信", r["error"])

    def test_zero_clears(self):
        r = self.bridge.set_wechat_window_size(0, 0)
        self.assertTrue(r["ok"])
        self.assertIsNone(r["configured"])
        self.assertIsNone(self.bridge.ctx.cfg["wechat_window_size"])

    def test_too_small_rejected(self):
        r = self.bridge.set_wechat_window_size(100, 100)
        self.assertFalse(r["ok"])

    def test_clamped_to_screen(self):
        import wx_backend.visual_backend as vb
        with mock.patch.object(vb, "find_wechat_window", return_value=None):
            with mock.patch("xiaoli_app.webbridge.ctypes") as ct:
                ct.windll.user32.GetSystemMetrics.side_effect = [1920, 1080]
                r = self.bridge.set_wechat_window_size(9000, 9000)
        self.assertTrue(r["ok"])
        self.assertEqual(r["configured"], [1920, 1080])


if __name__ == "__main__":
    unittest.main()
