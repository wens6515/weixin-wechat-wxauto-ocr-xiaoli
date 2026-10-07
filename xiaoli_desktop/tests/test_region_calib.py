# -*- coding: utf-8 -*-
"""微信画面标定：两框+分隔线 ↔ 运行时三区域（derive_regions /
boxes_from_regions）、标定配置 frozen 感知路径与保存/加载、区域热更新、
webbridge 三个标定方法面（start/save/verify）。
"""
import base64
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wx_backend import visual_backend as vb
from wx_backend.visual_regions import (
    _MESSAGE_REGION_RATIO, _SESSION_REGION_RATIO, _TITLE_REGION_RATIO,
    boxes_from_regions, derive_regions,
)

# 开发者真机默认标定（与 visual_regions 常量一致，作为测试基准值）
DEV = {"session": _SESSION_REGION_RATIO, "message": _MESSAGE_REGION_RATIO,
       "title": _TITLE_REGION_RATIO}


class TestDeriveRegions(unittest.TestCase):
    """两框 + 分隔线 → 三区域的派生数学。"""

    def test_derive_basic_math(self):
        r = derive_regions([0.0, 0.0, 0.4, 1.0], [0.4, 0.05, 1.0, 0.85], 0.1)
        self.assertEqual(r["session"], (0.0, 0.0, 0.4, 1.0))
        # 标题区 = 聊天区顶到分隔线；split=0.1 → t_bot = 0.05 + 0.1*0.8
        self.assertEqual(r["title"], (0.4, 0.05, 1.0, 0.05 + 0.08))
        # 消息区 = 分隔线以下
        self.assertEqual(r["message"], (0.4, 0.13, 1.0, 0.85))

    def test_split_clamped(self):
        """split 越界夹取 [0.02, 0.85]——防标题区高度归零被整份拒收。"""
        r = derive_regions([0, 0, 0.4, 1], [0.4, 0.0, 1.0, 1.0], 0.0)
        self.assertAlmostEqual(r["title"][3], 0.02)
        r = derive_regions([0, 0, 0.4, 1], [0.4, 0.0, 1.0, 1.0], 0.99)
        self.assertAlmostEqual(r["title"][3], 0.85)

    def test_invalid_boxes_rejected(self):
        self.assertIsNone(derive_regions([0.5, 0, 0.4, 1], [0.4, 0, 1, 1], 0.1))
        self.assertIsNone(derive_regions([0, 0, 0.4, 1], "坏值", 0.1))
        self.assertIsNone(derive_regions([0, 0, 0.4, 1], [0.4, 0, 1, 1], "x"))

    def test_boxes_from_dev_defaults(self):
        """默认三区域反推两框：聊天区 = 标题∪消息外框，split≈0.0546
        （真机标定换算，标定 UI 预填即此值）。"""
        b = boxes_from_regions(DEV["session"], DEV["message"], DEV["title"])
        self.assertEqual(b["session_box"], DEV["session"])
        self.assertEqual(b["chat_box"],
                         (0.4151, 0.0386, 0.9913, 0.8337))
        self.assertAlmostEqual(
            b["split"],
            (0.082 - 0.0386) / (0.8337 - 0.0386), places=6)

    def test_roundtrip_preserves_split_line(self):
        """反推再派生：分隔线与外框不漂移。派生消息区上沿 = 分隔线
        （原消息区上沿与标题区下沿之间的夹缝带本就归消息带——联合 OCR
        既有语义）；标题带宽度变为聊天区全宽是已知且可接受的行为微差。"""
        b = boxes_from_regions(DEV["session"], DEV["message"], DEV["title"])
        r = derive_regions(b["session_box"], b["chat_box"], b["split"])
        self.assertEqual(r["session"], DEV["session"])
        self.assertAlmostEqual(r["title"][3], DEV["title"][3], places=9)
        self.assertAlmostEqual(r["message"][1], DEV["title"][3], places=9)
        self.assertAlmostEqual(r["message"][3], DEV["message"][3], places=9)


class TestRegionConfigPaths(unittest.TestCase):
    """标定配置路径：源码态 / frozen 态候选序 + 保存读取往返。"""

    def test_source_mode_single_candidate(self):
        with mock.patch.object(vb, "_REGION_CONFIG_PATH", "X:\\a.json"):
            with mock.patch.object(vb.sys, "frozen", False, create=True):
                self.assertEqual(vb._region_config_paths(), ["X:\\a.json"])

    def test_frozen_mode_exe_dir_first(self):
        """打包态 exe 旁优先——__file__ 推导路径在安装包里不存在，
        修复前用户标定从未被打包版读到。"""
        with mock.patch.object(vb, "_REGION_CONFIG_PATH", "X:\\legacy.json"):
            with mock.patch.object(vb.sys, "frozen", True, create=True):
                with mock.patch.object(vb.sys, "executable", "C:\\app\\小漓.exe"):
                    paths = vb._region_config_paths()
        self.assertEqual(paths, ["C:\\app\\wx_ocr_region.json", "X:\\legacy.json"])

    def test_save_load_roundtrip_with_new_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "wx_ocr_region.json")
            with mock.patch.object(vb, "_REGION_CONFIG_PATH", p):
                with mock.patch.object(vb.sys, "frozen", False, create=True):
                    vb.save_region_config({
                        "session_region": [0.1, 0.1, 0.4, 0.9],
                        "message_region": [0.4, 0.1, 0.9, 0.8],
                        "title_region": [0.4, 0.04, 0.8, 0.08],
                        "session_box": [0.1, 0.1, 0.4, 0.9],
                        "chat_box": [0.4, 0.04, 0.9, 0.8],
                        "split": 0.05,
                    })
                    cfg = vb._load_region_config()
        self.assertEqual(cfg["session"], (0.1, 0.1, 0.4, 0.9))
        self.assertEqual(cfg["session_box"], [0.1, 0.1, 0.4, 0.9])
        self.assertEqual(cfg["chat_box"], [0.4, 0.04, 0.9, 0.8])
        self.assertEqual(cfg["split"], 0.05)

    def test_legacy_config_loads_without_new_fields(self):
        """旧格式（三区域、无两框字段）照常加载——老用户升级零变化。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "wx_ocr_region.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"session_region": [0.1, 0.1, 0.4, 0.9],
                           "message_region": [0.4, 0.1, 0.9, 0.8]}, f)
            with mock.patch.object(vb, "_REGION_CONFIG_PATH", p):
                cfg = vb._load_region_config()
        self.assertIsNotNone(cfg)
        self.assertNotIn("session_box", cfg)
        self.assertIsNone(cfg["title"])  # title 缺省：加载层透传 None，消费方回退默认常量

    def test_frozen_load_prefers_exe_dir(self):
        """两处都有配置时 frozen 态取 exe 旁（主路径写位）。"""
        with tempfile.TemporaryDirectory() as tmp:
            exe_json = os.path.join(tmp, "wx_ocr_region.json")
            with open(exe_json, "w", encoding="utf-8") as f:
                json.dump({"session_region": [0.2, 0.1, 0.4, 0.9],
                           "message_region": [0.4, 0.1, 0.9, 0.8]}, f)
            with mock.patch.object(vb, "_REGION_CONFIG_PATH",
                                   os.path.join(tmp, "no.json")):
                with mock.patch.object(vb.sys, "frozen", True, create=True):
                    with mock.patch.object(vb.sys, "executable",
                                           os.path.join(tmp, "小漓.exe")):
                        cfg = vb._load_region_config()
        self.assertEqual(cfg["session"], (0.2, 0.1, 0.4, 0.9))


class TestReloadRegions(unittest.TestCase):
    def test_reload_applies_config(self):
        b = vb.VisualBackend()
        with mock.patch.object(vb, "_load_region_config",
                               return_value={"session": (0.1, 0.1, 0.4, 0.9),
                                             "message": (0.4, 0.2, 0.9, 0.8),
                                             "title": (0.4, 0.05, 0.8, 0.09)}):
            r = b.reload_regions()
        self.assertEqual(b._session_region, (0.1, 0.1, 0.4, 0.9))
        self.assertEqual(r["title"], (0.4, 0.05, 0.8, 0.09))

    def test_reload_without_config_falls_back_to_defaults(self):
        b = vb.VisualBackend()
        b._session_region = (0.5, 0.5, 0.6, 0.6)  # 模拟旧值
        with mock.patch.object(vb, "_load_region_config", return_value=None):
            b.reload_regions()
        self.assertEqual(b._session_region, _SESSION_REGION_RATIO)
        self.assertEqual(b._message_region, _MESSAGE_REGION_RATIO)


class _CalibBridgeTest(unittest.TestCase):
    """webbridge 标定方法面基座：stub ctx + wx_backend 打桩。"""

    def setUp(self):
        from xiaoli_app.webbridge import BridgeApi

        class _Ctx:
            cfg_path = "unused"
            cards_dir = "unused"
            cfg = {}
            engine = None

        self.bridge = BridgeApi(_Ctx())
        self._patches = [
            mock.patch.object(vb, "find_wechat_window", return_value=0x1234),
            mock.patch.object(vb, "ensure_window_visible"),
            mock.patch.object(vb, "window_rect", return_value=(10, 20, 1310, 1630)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _shot(self):
        from PIL import Image
        return Image.new("RGB", (130, 161), (40, 44, 52))


class TestRegionCalibStart(_CalibBridgeTest):
    def test_no_wechat_window(self):
        with mock.patch.object(vb, "find_wechat_window", return_value=None):
            r = self.bridge.region_calib_start()
        self.assertFalse(r["ok"])
        self.assertIn("微信", r["error"])

    def test_start_returns_image_and_default_prefill(self):
        with mock.patch.object(vb, "capture_window", return_value=self._shot()):
            with mock.patch.object(vb, "_load_region_config", return_value=None):
                r = self.bridge.region_calib_start()
        self.assertTrue(r["ok"])
        self.assertTrue(r["image"].startswith("data:image/jpeg;base64,"))
        base64.b64decode(r["image"].split(",", 1)[1])  # 合法 base64
        self.assertEqual((r["width"], r["height"]), (130, 161))
        # 预填 = 默认三区域反推
        b = boxes_from_regions(DEV["session"], DEV["message"], DEV["title"])
        self.assertEqual(r["prefill"]["session_box"], list(b["session_box"]))
        self.assertEqual(r["prefill"]["chat_box"], list(b["chat_box"]))
        self.assertFalse(r["calibrated"])

    def test_start_prefers_saved_two_box_fields(self):
        cfg = {"session": DEV["session"], "message": DEV["message"],
               "title": DEV["title"], "session_box": [0.1, 0.1, 0.4, 0.9],
               "chat_box": [0.4, 0.04, 0.9, 0.8], "split": 0.06}
        with mock.patch.object(vb, "capture_window", return_value=self._shot()):
            with mock.patch.object(vb, "_load_region_config", return_value=cfg):
                r = self.bridge.region_calib_start()
        self.assertTrue(r["calibrated"])
        self.assertEqual(r["prefill"]["session_box"], [0.1, 0.1, 0.4, 0.9])
        self.assertEqual(r["prefill"]["split"], 0.06)


class TestRegionCalibSave(_CalibBridgeTest):
    def test_save_derives_and_persists(self):
        saved = {}

        def fake_save(data):
            saved.update(data)
            return "C:\\out\\wx_ocr_region.json"

        with mock.patch.object(vb, "capture_window", return_value=self._shot()):
            with mock.patch.object(vb, "save_region_config", side_effect=fake_save):
                r = self.bridge.region_calib_save(
                    [0.1, 0.1, 0.4, 0.9], [0.4, 0.04, 0.9, 0.8], 0.06)
        self.assertTrue(r["ok"])
        # 三区域 = derive_regions 派生值
        expect = derive_regions([0.1, 0.1, 0.4, 0.9], [0.4, 0.04, 0.9, 0.8], 0.06)
        self.assertEqual(r["regions"]["title"], list(expect["title"]))
        self.assertEqual(saved["title_region"], list(expect["title"]))
        self.assertEqual(saved["split"], 0.06)
        # window_rect 记录（调试溯源）
        self.assertEqual(saved["window_rect"],
                         {"x": 10, "y": 20, "width": 1310, "height": 1630})
        self.assertFalse(r["hot"])  # engine None → 无热更新

    def test_save_invalid_region_rejected(self):
        r = self.bridge.region_calib_save([0.9, 0.1, 0.4, 0.9],
                                          [0.4, 0.04, 0.9, 0.8], 0.06)
        self.assertFalse(r["ok"])

    def test_save_hot_reloads_running_backend(self):
        wx = mock.Mock()
        bot = mock.Mock(wx=wx)
        self.bridge.ctx.engine = mock.Mock(bot=bot)
        with mock.patch.object(vb, "capture_window", return_value=self._shot()):
            with mock.patch.object(vb, "save_region_config", return_value="p"):
                r = self.bridge.region_calib_save(
                    [0.1, 0.1, 0.4, 0.9], [0.4, 0.04, 0.9, 0.8], 0.06)
        self.assertTrue(r["hot"])
        wx.reload_regions.assert_called_once()


class TestRegionCalibVerify(_CalibBridgeTest):
    def _verify(self, ocr_side):
        with mock.patch.object(vb, "capture_window", return_value=self._shot()):
            with mock.patch.object(vb, "_load_region_config", return_value=None):
                with mock.patch.object(vb, "ocr_image", side_effect=ocr_side):
                    return self.bridge.region_calib_verify()

    def test_verify_reports_three_probes(self):
        def ocr(_img):
            # 依次：标题 / 列表 / 消息（区域均为小图，ocr_image 被裁剪图调用）
            return ocr.side.pop(0)
        ocr.side = [
            [{"text": "“摸鱼”", "x": 10, "y": 0}, {"text": "集团(5)", "x": 60, "y": 0}],
            [{"text": "林小满", "x": 4, "y": 2}, {"text": "：", "x": 4, "y": 20},
             {"text": "周雨桐", "x": 4, "y": 40}],
            [{"text": "明天记得带伞～", "x": 4, "y": 9}],
        ]
        r = self._verify(ocr)
        self.assertTrue(r["ok"])
        by_key = {i["key"]: i for i in r["items"]}
        self.assertTrue(by_key["title"]["ok"])
        self.assertIn("群聊", by_key["title"]["detail"])
        self.assertIn("林小满", by_key["list"]["detail"])
        self.assertIn("带伞", by_key["msg"]["detail"])

    def test_verify_title_miss_marks_bad(self):
        def ocr(_img):
            return ocr.side.pop(0)
        ocr.side = [[], [{"text": "林小满", "x": 4, "y": 2}], []]
        r = self._verify(ocr)
        by_key = {i["key"]: i for i in r["items"]}
        self.assertFalse(by_key["title"]["ok"])
        self.assertIn("分隔线", by_key["title"]["detail"])
        self.assertTrue(by_key["msg"]["ok"])  # 消息区空不算失败

    def test_verify_without_wechat(self):
        with mock.patch.object(vb, "find_wechat_window", return_value=None):
            r = self.bridge.region_calib_verify()
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
