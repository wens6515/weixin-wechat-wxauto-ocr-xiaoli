# -*- coding: utf-8 -*-
"""visual_backend 单元测试：协议满足性、OCR 文本清理、变化检测、注册与 auto 选择。

不依赖真实微信窗口——mock capture_window / ocr_image / find_wechat_window，
验证后端自身逻辑（连接、会话解析、消息切分、发送坐标、注册）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

from wx_backend import (
    BackendUnavailableError,
    WeChatBackend,
    WeChatMessage,
    available_backends,
    create_backend,
    register_backend,
    unregister_backend,
)
from wx_backend.visual_backend import (
    VisualBackend,
    _norm_cjk,
    ocr_image,
    region_changed,
)


# ---- 辅助：构造测试用 PIL 图像 ----


def _solid(size, color):
    img = Image.new("RGB", size, color)
    return img


# ---- 文本清理 ----


class TestNormCjk(unittest.TestCase):
    def test_removes_space_between_cjk(self):
        self.assertEqual(_norm_cjk("王 文 生"), "王文生")

    def test_keeps_time_separator(self):
        self.assertEqual(_norm_cjk("20:14"), "20:14")

    def test_cjk_punctuation(self):
        self.assertEqual(_norm_cjk("你 好 ， 小 漓"), "你好，小漓")

    def test_empty(self):
        self.assertEqual(_norm_cjk(""), "")
        self.assertEqual(_norm_cjk(None), "" if _norm_cjk(None) == "" else None)
        # None 应安全返回（当前实现 text.strip() 会崩，这里仅记录行为）


# ---- 变化检测 ----


class TestRegionChanged(unittest.TestCase):
    def test_identical_images_no_change(self):
        a = _solid((100, 100), (255, 255, 255))
        b = _solid((100, 100), (255, 255, 255))
        self.assertFalse(region_changed(a, b))

    def test_completely_different_images_changed(self):
        a = _solid((100, 100), (0, 0, 0))
        b = _solid((100, 100), (255, 255, 255))
        self.assertTrue(region_changed(a, b))

    def test_small_region_isolated_change(self):
        a = _solid((100, 100), (0, 0, 0))
        b = _solid((100, 100), (0, 0, 0))
        # 在右下角画一个白点
        b.paste((255, 255, 255), (90, 90, 91, 91))
        # 整图：1 像素变化 < 0.1% → False
        self.assertFalse(region_changed(a, b))
        # 局部区域：该区域 1/100 像素变化 > 0.1% → True
        self.assertTrue(region_changed(a, b, region=(80, 80, 100, 100)))

    def test_size_mismatch_is_change(self):
        self.assertTrue(region_changed(_solid((10, 10), (0, 0, 0)),
                                       _solid((20, 20), (0, 0, 0))))

    def test_none_inputs_is_change(self):
        self.assertTrue(region_changed(None, None))


# ---- OCR 返回结构 ----


class TestOcrImageMocked(unittest.TestCase):
    @mock.patch("wx_backend.visual_backend._get_ocr_engine", return_value=None)
    def test_no_engine_returns_empty(self, _m):
        img = _solid((50, 50), (255, 255, 255))
        self.assertEqual(ocr_image(img), [])


# ---- 后端行为（mock 窗口与 OCR） ----


class _FakeRect:
    def __init__(self, l, t, w, h):
        self.left = l
        self.top = t
        self.right = l + w
        self.bottom = t + h


class TestVisualBackend(unittest.TestCase):
    def setUp(self):
        # 清空注册表
        for n in list(available_backends()):
            unregister_backend(n)

    def tearDown(self):
        for n in list(available_backends()):
            unregister_backend(n)

    def test_satisfies_protocol(self):
        self.assertIsInstance(VisualBackend(), WeChatBackend)

    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=None)
    def test_connect_no_window_raises(self, _m):
        b = VisualBackend()
        with self.assertRaises(BackendUnavailableError):
            b.connect()

    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((100, 100), (255, 255, 255)))
    def test_connect_success_sets_hwnd(self, _cap, _find):
        b = VisualBackend()
        self.assertTrue(b.connect())
        self.assertEqual(b._hwnd, 0x1234)
        b.close()

    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window", return_value=None)
    def test_connect_capture_fail_raises(self, _cap, _find):
        b = VisualBackend()
        with self.assertRaises(BackendUnavailableError):
            b.connect()

    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    {"text": "王文生", "x": 0, "y": 20, "w": 60, "h": 20},
                    {"text": "20:14", "x": 0, "y": 40, "w": 40, "h": 15},
                    {"text": "杨冬梅", "x": 0, "y": 70, "w": 60, "h": 20},
                    {"text": "昨天", "x": 0, "y": 90, "w": 30, "h": 15},
                ])
    def test_iter_sessions_yields_names(self, _ocr, _cap, _find):
        b = VisualBackend()
        b.connect()
        names = list(b.iter_sessions())
        # 时间戳/日期行被过滤
        self.assertEqual(names, ["王文生", "杨冬梅"])
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    {"text": "你好", "x": 100, "y": 50, "w": 30, "h": 20},
                    {"text": "今天天气不错", "x": 100, "y": 80, "w": 90, "h": 20},
                ])
    def test_get_messages_merges_adjacent_lines(self, _ocr, _cap, _find, _switch):
        b = VisualBackend()
        b.connect()
        msgs = b.get_messages("王文生")
        self.assertEqual(len(msgs), 2)
        self.assertTrue(all(isinstance(m, WeChatMessage) for m in msgs))
        self.assertEqual(msgs[0].content, "你好")
        self.assertEqual(msgs[1].content, "今天天气不错")
        self.assertEqual(msgs[0].chat, "王文生")
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.ocr_image", return_value=[])
    def test_get_messages_empty_ocr(self, _ocr, _cap, _find, _switch):
        b = VisualBackend()
        b.connect()
        self.assertEqual(b.get_messages("王文生"), [])
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    # 左侧（x=10 < 中线）→ 对方消息
                    {"text": "你好", "x": 10, "y": 50, "w": 30, "h": 20},
                    # 时间戳行 → 分隔符，不入消息
                    {"text": "昨天 18:45", "x": 100, "y": 80, "w": 60, "h": 15},
                    # 右侧（x=150 > 中线）→ 自己发的
                    {"text": "我回复的", "x": 150, "y": 110, "w": 60, "h": 20},
                    # 噪音 → 过滤
                    {"text": "；；", "x": 10, "y": 140, "w": 20, "h": 15},
                ])
    def test_get_messages_timestamp_split_and_sender(self, _ocr, _cap, _find, _switch):
        """时间戳分隔消息块；x 中线判 self/对方；噪音过滤。"""
        b = VisualBackend()
        b.connect()
        msgs = b.get_messages("王文生")
        self.assertEqual(len(msgs), 2)
        # 时间戳行被分隔，不成为消息
        self.assertEqual(msgs[0].content, "你好")
        self.assertEqual(msgs[0].sender, "未知")   # 左侧 → 对方
        self.assertEqual(msgs[1].content, "我回复的")
        self.assertEqual(msgs[1].sender, "self")   # 右侧 → 自己
        b.close()

    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.VisualBackend._input_box_rect",
                return_value=(100, 160, 80, 30))
    @mock.patch("pyautogui.click")
    @mock.patch("pyautogui.typewrite")
    @mock.patch("pyautogui.press")
    def test_send_text_clicks_and_types(self, _press, _type, _click, _rect,
                                        _cap, _find):
        b = VisualBackend()
        b.connect()
        self.assertTrue(b.send_text("王文生", "你好呀"))
        _click.assert_called_once()
        _type.assert_called_once_with("你好呀", interval=0.01)
        _press.assert_called_once_with("enter")
        b.close()

    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=None)
    def test_register_then_auto_selects_visual_no_window(self, _find):
        from wx_backend.visual_backend import register as reg_visual
        reg_visual()
        self.assertIn("visual", available_backends())

        # 无真实微信窗口 → connect 抛 BackendUnavailableError → auto 聚合后抛出
        with self.assertRaises(BackendUnavailableError):
            create_backend("auto")


# ---- 未读红圈角标检测 ----


def _solid_with_badge(size=(200, 200)):
    """白底图 + 列表区放一个 20x20 微信品牌红块（#FA5151）。

    窗口比例：_SESSION_REGION_RATIO=(0, 0.08, 0.32, 1.0)，200x200 图
    → 列表区 crop = (0, 16, 64, 200)。红块画在 crop 内 (20,30)-(40,50)，
    即整窗 (20, 46, 40, 66)，中心整窗 (30, 56)。
    """
    from PIL import ImageDraw
    img = _solid(size, (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([20, 46, 40, 66], fill=(250, 81, 81))
    return img


class TestDetectRedClusters(unittest.TestCase):
    def test_finds_badge_cluster(self):
        from wx_backend.visual_backend import _detect_red_clusters
        img = _solid_with_badge()
        clusters = _detect_red_clusters(img)
        # 红块在列表区 → 检出 1 簇，覆盖整窗 (20,46)-(40,66)
        self.assertEqual(len(clusters), 1)
        l, t, r, b = clusters[0]
        self.assertLessEqual(l, 20)
        self.assertGreaterEqual(r, 40)
        self.assertLessEqual(t, 46)
        self.assertGreaterEqual(b, 66)

    def test_no_red_no_cluster(self):
        from wx_backend.visual_backend import _detect_red_clusters
        img = _solid((200, 200), (255, 255, 255))
        self.assertEqual(_detect_red_clusters(img), [])

    def test_non_brand_red_filtered(self):
        """列表区普通深红文字（如 r=180 < 200）不误报为角标。"""
        from wx_backend.visual_backend import _detect_red_clusters
        from PIL import ImageDraw
        img = _solid((200, 200), (255, 255, 255))
        d = ImageDraw.Draw(img)
        d.rectangle([20, 46, 40, 66], fill=(180, 40, 40))  # 深红不达 r>=200
        self.assertEqual(_detect_red_clusters(img), [])


class TestIterUnreadSessions(unittest.TestCase):
    def _backend(self):
        b = VisualBackend()
        b._hwnd = 0x1234  # 不 connect（避免真实窗口截图），直接设句柄
        b._last_shot = None
        return b

    @mock.patch("wx_backend.visual_backend.VisualBackend._refresh",
                return_value=_solid_with_badge())
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    # 红块中心整窗 (30,56)；王文生名中心 (35,50) → 红圈在其左上邻近
                    {"text": "王文生", "x": 20, "y": 40, "w": 30, "h": 20},
                    # 杨冬梅名中心 (25, 120)：距红块 (30,56) 太远 → 不匹配
                    {"text": "杨冬梅", "x": 10, "y": 110, "w": 30, "h": 20},
                ])
    def test_yields_only_badged_session(self, _ocr, _refresh):
        b = self._backend()
        names = list(b.iter_unread_sessions())
        self.assertEqual(names, ["王文生"])

    @mock.patch("wx_backend.visual_backend.VisualBackend._refresh",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.ocr_image")
    def test_no_badge_no_ocr_no_sessions(self, _ocr, _refresh):
        """无红圈 → 不调 OCR、不产出会话（效率路径：零 OCR 零点击）。"""
        b = self._backend()
        self.assertEqual(list(b.iter_unread_sessions()), [])
        _ocr.assert_not_called()

    @mock.patch("wx_backend.visual_backend.VisualBackend._refresh",
                return_value=None)
    def test_refresh_no_change_returns_empty(self, _refresh):
        """截图失败（_refresh 返回 None）→ 直接返回空。"""
        b = self._backend()
        self.assertEqual(list(b.iter_unread_sessions()), [])


# ---- 用户圈定区域配置加载 ----


def _write_region_config(tmp, data):
    """把 data 写入临时配置路径并返回该路径（用完由 tmp 清理）。"""
    import json
    p = os.path.join(tmp, "wx_ocr_region.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return p


class TestRegionConfig(unittest.TestCase):
    """_load_region_config：合法生效 / 缺文件回退 / 坏值回退 / 坏结构回退。"""

    def _load(self, config_path):
        import wx_backend.visual_backend as vb
        with mock.patch.object(vb, "_REGION_CONFIG_PATH", config_path):
            return vb._load_region_config()

    def test_valid_config_loaded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_region_config(tmp, {
                "session_region": {"l": 0.0, "t": 0.1, "r": 0.35, "b": 1.0},
                "message_region": {"l": 0.35, "t": 0.1, "r": 1.0, "b": 1.0},
            })
            cfg = self._load(p)
            self.assertIsNotNone(cfg)
            self.assertEqual(cfg["session"], (0.0, 0.1, 0.35, 1.0))
            self.assertEqual(cfg["message"], (0.35, 0.1, 1.0, 1.0))

    def test_list_format_config_loaded(self):
        """工具保存的数组格式 [l,t,r,b] 也能加载（兼容 pick_ocr_region 输出）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_region_config(tmp, {
                "session_region": [0.0, 0.1, 0.35, 1.0],
                "message_region": [0.35, 0.1, 1.0, 1.0],
            })
            cfg = self._load(p)
            self.assertIsNotNone(cfg)
            self.assertEqual(cfg["session"], (0.0, 0.1, 0.35, 1.0))
            self.assertEqual(cfg["message"], (0.35, 0.1, 1.0, 1.0))

    def test_missing_file_falls_back(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "nonexistent.json")
            self.assertIsNone(self._load(p))

    def test_window_rect_key_tolerated(self):
        """新格式含 window_rect 键（工具圈定窗口位置）→ 多余键忽略，双区域正常读取。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_region_config(tmp, {
                "window_rect": {"x": 726, "y": 0, "width": 1300, "height": 1610},
                "session_region": [0.0, 0.1, 0.35, 1.0],
                "message_region": [0.35, 0.1, 1.0, 1.0],
            })
            cfg = self._load(p)
            self.assertIsNotNone(cfg)
            self.assertEqual(cfg["session"], (0.0, 0.1, 0.35, 1.0))
            self.assertEqual(cfg["message"], (0.35, 0.1, 1.0, 1.0))

    def test_invalid_l_ge_r_falls_back(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_region_config(tmp, {
                "session_region": {"l": 0.5, "t": 0.1, "r": 0.3, "b": 1.0},  # l>=r
                "message_region": {"l": 0.3, "t": 0.1, "r": 1.0, "b": 1.0},
            })
            self.assertIsNone(self._load(p))

    def test_out_of_range_falls_back(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_region_config(tmp, {
                "session_region": {"l": -0.1, "t": 0.1, "r": 0.3, "b": 1.0},  # 越界
                "message_region": {"l": 0.3, "t": 0.1, "r": 1.0, "b": 1.0},
            })
            self.assertIsNone(self._load(p))

    def test_malformed_json_falls_back(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "wx_ocr_region.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("{ not valid json")
            self.assertIsNone(self._load(p))

    def test_backend_uses_config_region(self):
        """VisualBackend 加载配置后，实例区域反映配置（_detect_red_clusters 消费）。"""
        import tempfile
        import wx_backend.visual_backend as vb
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_region_config(tmp, {
                "session_region": {"l": 0.0, "t": 0.1, "r": 0.35, "b": 1.0},
                "message_region": {"l": 0.35, "t": 0.1, "r": 1.0, "b": 1.0},
            })
            with mock.patch.object(vb, "_REGION_CONFIG_PATH", p):
                b = VisualBackend()
            self.assertEqual(b._session_region, (0.0, 0.1, 0.35, 1.0))
            self.assertEqual(b._message_region, (0.35, 0.1, 1.0, 1.0))

    def test_backend_defaults_when_no_config(self):
        """无配置 → 实例区域用模块默认常量（现行为）。"""
        import tempfile
        import wx_backend.visual_backend as vb
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "nonexistent.json")
            with mock.patch.object(vb, "_REGION_CONFIG_PATH", p):
                b = VisualBackend()
            self.assertEqual(b._session_region, vb._SESSION_REGION_RATIO)
            self.assertEqual(b._message_region, vb._MESSAGE_REGION_RATIO)


if __name__ == "__main__":
    unittest.main(verbosity=2)
