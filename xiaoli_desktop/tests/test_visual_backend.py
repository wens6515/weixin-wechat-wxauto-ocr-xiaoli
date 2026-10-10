# -*- coding: utf-8 -*-
"""visual_backend 单元测试：协议满足性、OCR 文本清理、变化检测、注册与 auto 选择。

不依赖真实微信窗口——mock capture_window / ocr_image / find_wechat_window，
验证后端自身逻辑（连接、会话解析、消息切分、发送坐标、注册）。
"""
import json
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
    _bucket_avatar,
    _norm_cjk,
    filter_media_boxes,
    ocr_image,
    region_changed,
    detect_bubble_colors,
    detect_avatar_tops,
    find_bubble_boxes,
    find_media_boxes,
    ensure_window_visible,
    find_window_by_title,
    window_rect,
)

# 真实群聊截图 fixture（.rivet/scratch/probe_group/，tools 探针抓取）
_FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".rivet", "scratch", "probe_group")
_REGION_1X = os.path.join(_FIXTURE_DIR, "region_1x.png")
_OCR_CACHE = os.path.join(_FIXTURE_DIR, "ocr_cache.json")
from wx_backend.models import MessageType


# ---- 辅助：构造测试用 PIL 图像 ----


def _solid(size, color):
    img = Image.new("RGB", size, color)
    return img


# ---- 文本清理 ----


class TestNormCjk(unittest.TestCase):
    def test_removes_space_between_cjk(self):
        self.assertEqual(_norm_cjk("林 小 满"), "林小满")

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

    @mock.patch("wx_backend.visual_backend._get_ocr_engine")
    def test_rapidocr_maps_to_dict_contract(self, _m):
        """RapidOCROutput（.txts/.scores/.boxes）→ ocr_image 输出 {text,x,y,w,h}。

        boxes 用 numpy 数组模拟真实返回——numpy 禁真值判断（`or` 会 raise），
        适配层必须逐字段 None 检查后 zip。"""
        import numpy as np

        class _FakeOut:
            txts = ("林小满", "[图片]")
            scores = (0.99, 0.95)
            boxes = np.array([
                [[10, 20], [100, 20], [100, 50], [10, 50]],
                [[5, 80], [60, 80], [60, 100], [5, 100]],
            ])

        class _FakeEngine:
            def __call__(self, img):
                return _FakeOut()

        _m.return_value = _FakeEngine()
        img = _solid((200, 200), (255, 255, 255))
        items = ocr_image(img)
        self.assertEqual(items, [
            {"text": "林小满", "x": 10, "y": 20, "w": 90, "h": 30},
            {"text": "[图片]", "x": 5, "y": 80, "w": 55, "h": 20},
        ])

    def test_engine_construction_locks_model_and_threads(self):
        """契约：引擎构造必须限线程（CPU 治理）并锁 PP-OCRv5 mobile 模型，防回退。"""
        from wx_backend import visual_backend as vb

        old = vb._OCR_ENGINE
        vb._OCR_ENGINE = None
        try:
            with mock.patch("rapidocr.RapidOCR") as m_cls:
                m_cls.return_value = object()
                vb._get_ocr_engine()
                _args, kwargs = m_cls.call_args
                params = kwargs["params"]
                self.assertEqual(
                    params["EngineConfig.onnxruntime.intra_op_num_threads"], 2)
                self.assertEqual(params["Det.ocr_version"].name, "PPOCRV5")
                self.assertEqual(params["Det.model_type"].name, "MOBILE")
                self.assertEqual(params["Rec.ocr_version"].name, "PPOCRV5")
                self.assertEqual(params["Rec.model_type"].name, "MOBILE")
                self.assertEqual(params["Det.limit_side_len"], 224)
        finally:
            vb._OCR_ENGINE = old


# ---- 气泡色探测 + 连通域分气泡 ----


def _dark_msg_region():
    """模拟深色主题消息区：深黑背景 + 深灰对方气泡 + 绿色自己气泡。"""
    from PIL import ImageDraw
    img = Image.new("RGB", (400, 300), (30, 30, 31))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([40, 30, 240, 110], radius=8, fill=(47, 47, 48))      # 对方气泡
    d.rounded_rectangle([160, 150, 360, 210], radius=8, fill=(53, 210, 141))  # 自己气泡
    return img


def _dark_msg_region_other_bottom():
    """深色主题消息区：自己（绿）气泡在上、对方（深灰）气泡在下。

    本轮对方新消息在最后（bot 最后一条回复之上）——头像锚定读取范围要求
    「对方头像在 bot 最后头像之后」，测试场景必须按时间顺序排。"""
    from PIL import ImageDraw
    img = Image.new("RGB", (400, 300), (30, 30, 31))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([160, 30, 360, 90], radius=8, fill=(53, 210, 141))   # 自己气泡（上）
    d.rounded_rectangle([40, 150, 240, 230], radius=8, fill=(47, 47, 48))    # 对方气泡（下）
    return img


class TestBubbleDetection(unittest.TestCase):
    def test_detect_bubble_colors_dark(self):
        colors = detect_bubble_colors(_dark_msg_region())
        self.assertIsNotNone(colors["bg"])
        self.assertIsNotNone(colors["self"])
        self.assertIsNotNone(colors["other"])
        # 背景接近深黑
        self.assertLess(abs(colors["bg"][0] - 30), 8)
        # 自己气泡是绿色（G 显著高、R 低）
        self.assertGreater(colors["self"][1], 150)
        self.assertLess(colors["self"][0], 100)
        # 对方气泡深灰（各分量接近 47）
        for c in colors["other"]:
            self.assertLess(abs(c - 47), 10)

    def test_find_bubble_boxes_dark(self):
        img = _dark_msg_region()
        colors = detect_bubble_colors(img)
        boxes = find_bubble_boxes(img, colors)
        self.assertEqual(len(boxes), 2, "应找到 2 个气泡（对方 + 自己）")
        flags = sorted(b[4] for b in boxes)
        self.assertEqual(flags, [False, True])

    def test_detect_on_solid_returns_none(self):
        """纯色/mock 截图探测不到气泡色 → 返回 None（get_messages 回退 y 阈值）。"""
        colors = detect_bubble_colors(_solid((200, 200), (255, 255, 255)))
        self.assertIsNone(colors["self"])
        self.assertIsNone(colors["other"])

    def test_find_media_boxes_detects_image(self):
        """媒体内容（图片）是「非背景、非气泡色」的大块，应被检测为矩形。"""
        from PIL import ImageDraw
        img = Image.new("RGB", (400, 300), (30, 30, 31))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([40, 30, 240, 110], radius=8, fill=(47, 47, 48))  # 对方气泡
        d.rectangle([40, 130, 240, 260], fill=(120, 80, 200))                 # 图片内容
        colors = detect_bubble_colors(img)
        boxes = find_media_boxes(img, colors)
        self.assertEqual(len(boxes), 1, "应检测到 1 个媒体矩形（图片）")
        t, b, l, r = boxes[0]
        self.assertLess(abs(t - 130), 10)
        self.assertLess(abs(b - 260), 10)


class TestFilterMediaBoxes(unittest.TestCase):
    """media_screen_boxes 的纯几何过滤：min_top 下沿阈值 + exclude_rows
    行区间剔除（文件卡片类型图标碎片与文件名行同块相交）。"""

    # 真机探针实测几何（林小满窗口，文件+图片同轮）：
    # bot 侧 docx 图标碎片 / 对方真图 / 对方 PDF 卡图标碎片
    BOXES = [(320, 389, 533, 588), (565, 844, 119, 398), (918, 987, 470, 525)]

    def test_min_top_excludes_bot_side_history(self):
        """bot_bottom=565：bot 侧图标碎片 (320,389) 被阈值排除。"""
        out = filter_media_boxes(self.BOXES, min_top=565)
        self.assertEqual(out, [(565, 844, 119, 398), (918, 987, 470, 525)])

    def test_exclude_rows_drops_file_card_icon(self):
        """文件行 y=935 的相交带 (915,985) 剔除同块的 PDF 图标碎片 (918,987)，
        真实图片块 (565,844) 与文件行分属不同消息块必不相交 → 保留。"""
        out = filter_media_boxes(self.BOXES, min_top=565,
                                 exclude_rows=[(915, 985)])
        self.assertEqual(out, [(565, 844, 119, 398)],
                         "只剩真实图片块，图标碎片被剔除")

    def test_no_filters_passthrough(self):
        """min_top=None 且无排除行 → 原样返回（拷贝，不共享列表）。"""
        out = filter_media_boxes(self.BOXES)
        self.assertEqual(out, self.BOXES)
        self.assertIsNot(out, self.BOXES)


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
                    {"text": "林小满", "x": 0, "y": 20, "w": 60, "h": 20},
                    {"text": "20:14", "x": 0, "y": 40, "w": 40, "h": 15},
                    {"text": "周雨桐", "x": 0, "y": 70, "w": 60, "h": 20},
                    {"text": "昨天", "x": 0, "y": 90, "w": 30, "h": 15},
                ])
    def test_iter_sessions_yields_names(self, _ocr, _cap, _find):
        b = VisualBackend()
        b.connect()
        names = list(b.iter_sessions())
        # 时间戳/日期行被过滤
        self.assertEqual(names, ["林小满", "周雨桐"])
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                side_effect=lambda region, bg, side:
                    [] if side == "right" else [50, 150])
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    {"text": "你好", "x": 100, "y": 50, "w": 30, "h": 20},
                    # 真实微信消息块间距 ≥150px（真机内容-内容最小 186px），
                    # 短消息不会与下一条消息紧贴——避免被误判为发送者名
                    {"text": "今天天气不错", "x": 100, "y": 150, "w": 90, "h": 20},
                ])
    def test_get_messages_merges_adjacent_lines(self, _ocr, _tav, _cap, _find,
                                                _switch):
        """一条消息 = 一个头像（用户定案）：两个对方头像各锚定一条消息。"""
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b.connect()
        msgs = b.get_messages("林小满")
        self.assertEqual(len(msgs), 2)
        self.assertTrue(all(isinstance(m, WeChatMessage) for m in msgs))
        self.assertEqual(msgs[0].content, "你好")
        self.assertEqual(msgs[1].content, "今天天气不错")
        self.assertEqual(msgs[0].chat, "林小满")
        b.close()

    def test_get_messages_union_force_reswitch_on_empty_title(self):
        """RED 复现：点击后标题区空（toggle 取消选中/黑图）→ 联合 OCR
        必须 force 重切（原 analyze_window 防线迁入 get_messages 联合路径）。
        否则读的是空消息区（has_other=False 判空跳过），红圈不消导致
        5 轮循环漏消息——真机日志：林小满新消息 5 轮未处理。"""
        b = VisualBackend()
        b._current_chat = "林小满"  # assume 路径前提：analyze 刚完成切换
        # 坐标与配置解耦：标题带 = y<10，消息带坐标不平移（确定性）
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b._title_region = (0.0, 0.0, 0.0, 0.05)
        # 只有消息带条目（标题带空）→ 触发 force 重切
        items = [{"text": "在吗", "x": 40, "y": 120, "w": 60, "h": 20}]
        with mock.patch.object(b, "_switch_chat", wraps=lambda chat, force=False: True) as m_switch, \
             mock.patch("wx_backend.visual_backend.find_wechat_window",
                        return_value=0x1234), \
             mock.patch("wx_backend.visual_backend.capture_window",
                        return_value=_solid((200, 200), (30, 30, 31))), \
             mock.patch("wx_backend.visual_backend.ocr_image", return_value=items), \
             mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                        return_value={}), \
             mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                        side_effect=lambda region, bg, side:
                            [] if side == "right" else [120]), \
             mock.patch("wx_backend.visual_backend.find_media_boxes",
                        return_value=[]):
            b._hwnd = 0x1234
            b._last_shot = None
            msgs = b.get_messages("林小满", assume_switched=True)
        force_calls = [c for c in m_switch.call_args_list if c.kwargs.get("force")]
        self.assertTrue(force_calls, "标题区空（联合 OCR）应触发 force 重切")
        self.assertTrue(any(m.content == "在吗" for m in msgs),
                        "重切后消息应正常产出")
        b.close()

    def test_get_messages_union_name_mismatch_no_reswitch(self):
        """RED 复现：标题非空但与目标会话名不匹配（群名 emoji/全半角 OCR
        差异，如 chat='🎉庆祝群'、标题读成 '庆祝群(5)'）→ 不得 force 重切。
        旧逻辑 startswith 失败 → 白点 force 点击已选中会话 → toggle 取消
        选中 → 消息区读空死循环（真机日志群聊名字后多带（数字））。
        联合 OCR 契约：标题非空即信任已选中，名字差异只进缓存不触发重切。"""
        b = VisualBackend()
        b._current_chat = "🎉庆祝群"
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b._title_region = (0.0, 0.0, 0.0, 0.05)
        items = [
            {"text": "庆祝群(5)", "x": 40, "y": 4, "w": 90, "h": 8},
            {"text": "你们好呀", "x": 40, "y": 120, "w": 90, "h": 20},
        ]
        with mock.patch.object(b, "_switch_chat", wraps=lambda chat, force=False: True) as m_switch, \
             mock.patch("wx_backend.visual_backend.find_wechat_window",
                        return_value=0x1234), \
             mock.patch("wx_backend.visual_backend.capture_window",
                        return_value=_solid((200, 200), (30, 30, 31))), \
             mock.patch("wx_backend.visual_backend.ocr_image", return_value=items), \
             mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                        return_value={}), \
             mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                        side_effect=lambda region, bg, side:
                            [] if side == "right" else [120]), \
             mock.patch("wx_backend.visual_backend.find_media_boxes",
                        return_value=[]):
            b._hwnd = 0x1234
            b._last_shot = None
            msgs = b.get_messages("🎉庆祝群", assume_switched=True)
        force_calls = [c for c in m_switch.call_args_list if c.kwargs.get("force")]
        self.assertFalse(force_calls, "标题非空时名字不匹配不得 force 重切")
        self.assertTrue(any(m.content == "你们好呀" for m in msgs))
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                side_effect=lambda region, bg, side:
                    [] if side == "right" else [50])
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    # 同一气泡多行（行距 22px，微信气泡内换行）→ 应合并为一条
                    {"text": "@小漓 测试，生成10秒视频：镜头1：缓慢推镜，昏暗",
                     "x": 50, "y": 50, "w": 150, "h": 11},
                    {"text": "镜头2：微微特写少年侧脸，眼神平静",
                     "x": 50, "y": 72, "w": 130, "h": 11},
                    {"text": "镜头3：镜头缓缓拉远，整个安静的房间",
                     "x": 50, "y": 94, "w": 140, "h": 11},
                ])
    def test_get_messages_merges_multiline_bubble(self, _ocr, _tav, _cap, _find,
                                                 _switch):
        """同一头像区间内多行换行应合并为一条消息，不得拆散。

        RED 复现：真机长任务指令（含镜头1/2/3 多行）被拆成 12 条独立消息，
        只有带 @ 前缀的第一行成为任务，其余行被 [跳过]——task.json raw_message
        只剩「测试，生成10秒视频」。现在文字范围 = [该消息头像上边界, 块下
        边界]，块内所有行合成一条。
        """
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b.connect()
        msgs = b.get_messages("林小满")
        self.assertEqual(len(msgs), 1, "同一消息块多行应合并为一条消息")
        self.assertIn("镜头2", msgs[0].content)
        self.assertIn("镜头3", msgs[0].content)
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window")
    @mock.patch("wx_backend.visual_backend.ocr_image")
    def test_get_messages_groups_by_bubble(self, _ocr, _cap, _find, _switch):
        """分块按头像：对方头像区间内的多行合并为一条；自己气泡不进结果。

        用户定案：一条消息 = 一个头像，归属只看头像。绿色气泡（自己发的）
        不再产出消息——分析区只覆盖「bot 最后一条消息之后的下一条对方头像
        上边界」以下，bot 自己的历史回复（含自己气泡）不进读取范围。
        """
        _cap.return_value = _dark_msg_region_other_bottom()  # 自己(上)+对方(下)气泡
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b.connect()
        with mock.patch.object(b, "read_title", return_value="林小满"), \
             mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                        side_effect=lambda region, bg, side:
                            [30] if side == "right" else [150]):
            # OCR 文字（1x 坐标）：自己气泡 [160,30,360,90]（bot 历史回复、
            # 在 bot 最后头像 30 之上）→ 不读；对方气泡 [40,150,240,230]
            # 锚定对方头像 150 → 多行合并为一条
            _ocr.return_value = [
                {"text": "镜头1：缓慢推镜", "x": 200, "y": 40, "w": 100, "h": 15},
                {"text": "镜头2：特写侧脸", "x": 60, "y": 160, "w": 100, "h": 15},
                {"text": "镜头3：缓缓拉远", "x": 60, "y": 190, "w": 100, "h": 15},
            ]
            msgs = b.get_messages("林小满")
        self.assertEqual(len(msgs), 1, "只产出对方新消息一条（自己气泡不读）")
        self.assertIn("镜头2", msgs[0].content)
        self.assertIn("镜头3", msgs[0].content)
        self.assertNotIn("镜头1", msgs[0].content, "bot 自己的历史消息不读")
        self.assertEqual(msgs[0].sender, "林小满", "私聊 sender=会话名")
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window")
    @mock.patch("wx_backend.visual_backend.ocr_image")
    def test_get_messages_excludes_avatar_text(self, _ocr, _cap, _find, _switch):
        """头像区域内的 OCR 文字（头像图片上的字）按几何剔除，不当消息内容。

        用户补充定案：头像区（左右窄带 ∩ 头像竖直区间）识别出的文字一律剔除
        ——真机幻影行「用户已无生命体征」就悬在头像上、且落在该条消息的文字
        范围内，不剔除会混进文件名 OCR。
        """
        _cap.return_value = _dark_msg_region_other_bottom()  # 自己(上)+对方(下)
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b.connect()
        with mock.patch.object(b, "read_title", return_value="林小满"), \
             mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                        side_effect=lambda region, bg, side:
                            [30] if side == "right" else [150]):
            _ocr.return_value = [
                # 对方气泡内容（对方气泡 1x [40,150,240,230] 内）
                {"text": "镜头1：缓慢推镜", "x": 60, "y": 160, "w": 100, "h": 15},
                # 头像文字（落在自己头像矩形：400 宽 → 右窄带 [336,392] ×
                # 头像竖直区间 [30,70]）
                {"text": "蓝色大肥鱼", "x": 355, "y": 40, "w": 30, "h": 10},
            ]
            msgs = b.get_messages("林小满")
        self.assertEqual(len(msgs), 1, "头像文字应被排除，只剩气泡内容")
        self.assertIn("镜头1", msgs[0].content)
        self.assertNotIn("蓝色大肥鱼", msgs[0].content)
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
        self.assertEqual(b.get_messages("林小满"), [])
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                return_value={"bg": (255, 255, 255), "other": (240, 240, 240),
                              "self": (53, 210, 141)})
    @mock.patch("wx_backend.visual_backend.find_bubble_boxes",
                return_value=[
                    (34, 66, 80, 160, True),   # 自己气泡（bot 历史回复，在上）
                    (104, 136, 0, 60, False),  # 对方气泡（本轮新消息，在下）
                ])
    @mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                side_effect=lambda region, bg, side:
                    [34] if side == "right" else [104])
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    # 自己消息（在自己气泡内 → bot 历史，不读）
                    {"text": "我回复的", "x": 80, "y": 50, "w": 30, "h": 20},
                    # 块间悬浮行（UI 时间戳分隔：不在任何消息块内）→ 剔除
                    {"text": "昨天 18:45", "x": 45, "y": 80, "w": 25, "h": 15},
                    # 对方消息（在对方气泡内 → 读）
                    {"text": "你好", "x": 2, "y": 110, "w": 30, "h": 20},
                    # 噪音 → 过滤
                    {"text": "；；", "x": 2, "y": 140, "w": 20, "h": 15},
                ])
    def test_get_messages_timestamp_split_and_sender(self, _ocr, _tops, _fbb, _dc,
                                                     _cap, _find, _switch):
        """消息块外的行（UI 时间戳、自己气泡）不产出消息；块内文字成条；噪音过滤。

        用户定案：读取范围只有「本轮对方新消息」，自己的消息与悬空的时间/
        日期分隔行都不在块内 → 不产出（旧实现把自己气泡标 sender='self' 交给
        上层过滤，现在像素层直接不读）。
        """
        b = VisualBackend()
        b.connect()
        msgs = b.get_messages("林小满")
        self.assertEqual(len(msgs), 1, "只剩对方块内一条（时间戳/自己气泡/噪音都不读）")
        self.assertEqual(msgs[0].content, "你好")
        self.assertEqual(msgs[0].sender, "林小满")   # 对方消息（私聊发送人=会话名）
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                return_value={"bg": (255, 255, 255), "other": (240, 240, 240),
                              "self": (53, 210, 141)})
    @mock.patch("wx_backend.visual_backend.find_bubble_boxes",
                return_value=[(32, 66, 30, 140, False)])  # 回复所在对方气泡
    @mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                side_effect=lambda region, bg, side:
                    [] if side == "right" else [32])
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    # UI 时间分隔行（气泡外、不在头像区间 [150,190]）→ 剔除
                    {"text": "昨天 18:45", "x": 60, "y": 10, "w": 60, "h": 14},
                    # 用户场景：对方回复「晚上 21:30」——时间形状的真消息，
                    # 在气泡内 → 必须读到（历史事故：文本过滤把它当时间戳吞掉）
                    {"text": "晚上 21:30", "x": 40, "y": 40, "w": 80, "h": 20},
                ])
    def test_get_messages_time_shaped_reply_kept(self, _ocr, _tops, _fbb, _dc,
                                                 _cap, _find, _switch):
        """时间形状的真回复（对方答「晚上 21:30」）不得被过滤，且分隔行不重复。"""
        b = VisualBackend()
        b.connect()
        msgs = b.get_messages("林小满")
        self.assertEqual(len(msgs), 1, "分隔行应剔除，只剩回复一条")
        self.assertEqual(msgs[0].content, "晚上 21:30")
        self.assertEqual(msgs[0].sender, "林小满")
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                side_effect=lambda region, bg, side:
                    [] if side == "right" else [60])
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    # 用户真实消息（左侧）→ 应保留
                    {"text": "你好", "x": 100, "y": 60, "w": 30, "h": 20},
                    # 输入框"发送"按钮（消息区右下角，x/y 均贴近边缘）
                    # → 会被 OCR 读进来且 x 靠右判成 self，顶掉真实最新消息
                    {"text": "发送", "x": 370, "y": 380, "w": 25, "h": 15},
                ])
    def test_get_messages_filters_input_box_send_button(self, _ocr, _tav, _cap,
                                                        _find, _switch):
        """输入框"发送"按钮（右下角固定位置）不应成为消息。

        RED 复现：真机日志出现 latest sender='self' content='发送'——OCR 把
        输入框发送按钮读成消息且 x 靠右判 self，process_new_messages 直接跳过
        整个会话，用户真实消息永远不被处理。
        """
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)  # 全窗，与配置解耦、确定性
        b.connect()
        msgs = b.get_messages("林小满")
        self.assertEqual(len(msgs), 1, "发送按钮应被过滤，只剩用户真实消息")
        self.assertEqual(msgs[0].content, "你好")
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                side_effect=lambda region, bg, side:
                    [] if side == "right" else [50])
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    # 左侧消息（锚定左侧头像）→ 私聊发送人 = 会话名
                    {"text": "你好", "x": 100, "y": 50, "w": 30, "h": 20},
                ])
    def test_get_messages_private_chat_sender_is_chat_name(self, _ocr, _tav,
                                                           _cap, _find, _switch):
        """私聊（非群聊）时消息区左侧的发送人就是会话名本身。

        RED 复现：真机日志 [最新消息] sender='未知'——私聊林小满会话里
        左侧消息的 sender 硬编码"未知"，上层拿不到发送人。私聊场景
        sender 应为 chat（会话名=发送人），群聊才是消息区气泡名。
        """
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b.connect()
        msgs = b.get_messages("林小满")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].sender, "林小满",
                         "私聊左侧消息 sender 应为会话名，而非'未知'")
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                return_value={"bg": (255, 255, 255), "other": (240, 240, 240),
                              "self": (53, 210, 141)})
    @mock.patch("wx_backend.visual_backend.find_bubble_boxes",
                return_value=[(100, 124, 60, 160, False)])  # 只包住内容行
    @mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                side_effect=lambda region, bg, side:
                    [] if side == "right" else [40])
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    # 群聊 UI 时间分隔行（气泡外、不在头像区间 [40,80]）→ 剔除
                    {"text": "10月5日 18:45", "x": 60, "y": 10, "w": 60, "h": 14},
                    # 群聊发送者名：短文本独立行，紧贴内容上方（y 差 ~53），
                    # 在头像上边界之下的区间内（cy=56 ∈ [40,80]）→ 有效候选
                    {"text": "哆拉A萝", "x": 50, "y": 50, "w": 40, "h": 12},
                    # 消息内容（气泡内）
                    {"text": "豆包有学生优惠了", "x": 70, "y": 103,
                     "w": 75, "h": 12},
                ])
    def test_get_messages_group_chat_sender_is_author(self, _ocr, _tops, _fbb,
                                                      _dc, _cap, _find, _switch):
        """群聊时消息区气泡上方有发送者名（短文本行紧贴内容），
        sender 应为发送者名，且名字行本身不得成为一条消息；
        气泡外无归属的 UI 时间分隔行必须剔除（不得混入内容/污染发送者）。

        RED 复现：真机读「摸鱼”集团」群聊，OCR 读到 '哆拉A萝'（发送者）
        与 '豆包有学生优惠了'（内容）两条独立项——当前实现把名字行
        当成独立消息（sender=群名），上层拿不到发送者。
        真机 y 差（1x）：名字-内容 53px，内容块间最小 93px → 可区分。
        """
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b.connect()
        msgs = b.get_messages("摸鱼”集团")
        self.assertEqual(len(msgs), 1, "发送者名行与时间分隔行都不应成为独立消息"
                                       "，也不得并入内容")
        self.assertEqual(msgs[0].sender, "哆拉A萝", "群聊 sender 应为发送者名")
        self.assertEqual(msgs[0].content, "豆包有学生优惠了")
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.ocr_image", return_value=[])
    def test_get_messages_retries_title_when_deselected(self, _ocr, _cap,
                                                        _find, _switch):
        """read_title 返回 None（toggle 取消选中）→ force 重切后再读标题。

        RED 复现（真机探针）：同一会话 force 连续点击第 2 次会 toggle 取消选中，
        标题区空 read_title 返回 None——旧实现不重试，_current_is_group 保持旧值
        导致群聊判定错。修复：None 时 force 重切再读一次。
        """
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b.connect()
        with mock.patch.object(b, "read_title",
                               side_effect=[None, "林小满"]) as _rt:
            b.get_messages("林小满")
        self.assertEqual(_rt.call_count, 2, "read_title None 后应重试一次")
        force_calls = [c for c in _switch.call_args_list
                       if c == mock.call("林小满", force=True)]
        self.assertTrue(force_calls, "None 后应 force 重切会话恢复选中")
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                side_effect=lambda region, bg, side:
                    [] if side == "right" else [50])
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    {"text": "你好", "x": 100, "y": 50, "w": 30, "h": 20},
                ])
    def test_get_messages_name_mismatch_no_reswitch(self, _ocr, _tav, _cap,
                                                    _find, _switch):
        """RED 复现：标题非空但解析名与目标会话不匹配（群名 emoji/全半角
        OCR 差异，chat='🎉庆祝群'、标题读成 '庆祝群(5)'）→ 不得 force 重切。
        旧逻辑 startswith 失败 → 白点 force 点击已选中会话 → toggle 取消选中
        → 读 0 条 → 再 force……群聊新消息反复点击（真机日志：私聊不带数字、
        群聊名字后多带（数字））。新逻辑：标题为空才 force 重切。"""
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b.connect()
        with mock.patch.object(b, "read_title", return_value="庆祝群(5)") as _rt:
            msgs = b.get_messages("🎉庆祝群")
        self.assertEqual(_rt.call_count, 1, "标题非空时只读一次，不重切重读")
        force_calls = [c for c in _switch.call_args_list
                       if c == mock.call("🎉庆祝群", force=True)]
        self.assertFalse(force_calls, "标题非空时名字不匹配不得 force 重切")
        self.assertTrue(msgs, "名字不匹配不得影响消息读取")
        self.assertIn("你好", msgs[0].content)
        b.close()

    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.VisualBackend._input_box_rect",
                return_value=(100, 160, 80, 30))
    @mock.patch("pyperclip.copy")
    @mock.patch("pyautogui.click")
    @mock.patch("pyautogui.hotkey")
    @mock.patch("pyautogui.press")
    def test_send_text_uses_clipboard_paste(self, _press, _hotkey, _click,
                                            _copy, _rect, _cap, _find):
        """发送中文必须走剪贴板粘贴，不能 typewrite 逐键模拟。

        RED 复现：真机日志 🤖→[林小满] 显示正常回复，但微信输入框实际
        只出现'（）'——pyautogui.typewrite 逐键模拟对非 ASCII（中文）
        无法映射键位，按键序列被中文输入法拦截成括号。
        """
        b = VisualBackend()
        b.connect()
        self.assertTrue(b.send_text("林小满", "你好呀"))
        _click.assert_called_once()
        _copy.assert_called_once_with("你好呀")
        _hotkey.assert_called_once_with("ctrl", "v")
        _press.assert_called_once_with("enter")
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

    def test_extract_session_names_excludes_top_title(self):
        """顶部标题（x 落在 [region[2]*w, (region[2]+0.17)*w) 区间，如置顶会话
        在窗口顶部的标题）不应被误当会话列表条目——真机实测：置顶林小满时
        顶部标题 x=543（0.418w）被 +0.17 容差纳入，导致红圈 y 差 66>60 匹配失败。"""
        b = VisualBackend()
        b._hwnd = 0x1234
        b._session_region = (0.09, 0.0855, 0.4165, 0.993)  # 固定 region[2]=0.4165
        shot = _solid((1300, 1610), (255, 255, 255))
        with mock.patch("wx_backend.visual_backend.ocr_image", return_value=[
            # 顶部标题：x=543 = 0.4177w，在 region[2]=0.4165w 之外、+0.17 之内
            {"text": "干立牛", "x": 543, "y": 85, "w": 71, "h": 20},
            # 列表条目：x=217 = 0.167w，正常会话名
            {"text": "摸鱼集团", "x": 217, "y": 293, "w": 131, "h": 20},
        ]):
            coords = b._extract_session_names(shot)
        self.assertNotIn("干立牛", coords)
        self.assertIn("摸鱼集团", coords)

    def test_extract_session_names(self):
        """会话名提取：整窗 OCR 聚类出会话名 + 坐标（预览类型已移除）。"""
        b = VisualBackend()
        b._hwnd = 0x1234
        b._session_region = (0.09, 0.0855, 0.4165, 0.993)
        shot = _solid((1300, 1610), (255, 255, 255))
        with mock.patch("wx_backend.visual_backend.ocr_image", return_value=[
            {"text": "林小满", "x": 214, "y": 163, "w": 73, "h": 20},
            {"text": "[文件] 部门简介.docx", "x": 220, "y": 203, "w": 291, "h": 20},
            {"text": "周雨桐", "x": 215, "y": 393, "w": 73, "h": 20},
            {"text": "[图片]", "x": 217, "y": 430, "w": 52, "h": 20},
            {"text": "陈嘉禾", "x": 216, "y": 506, "w": 73, "h": 20},
        ]):
            coords = b._extract_session_names(shot)
        self.assertIn("林小满", coords)
        self.assertIn("周雨桐", coords)
        self.assertIn("陈嘉禾", coords)

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                return_value={"bg": (30, 30, 31), "other": (30, 35, 38),
                              "self": (53, 210, 141)})
    @mock.patch("wx_backend.visual_backend.find_bubble_boxes",
                return_value=[
                    # self 绿气泡（bot 长回复）
                    (0, 60, 80, 190, True),
                    # other 气泡误检在右侧：left=170（other 色接近背景时
                    # find_bubble_boxes 把右侧背景误连成 other 框）
                    (0, 60, 170, 199, False),
                ])
    @mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                side_effect=lambda region, bg, side:
                    [] if side == "right" else [40])
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    {"text": "18:47", "x": 60, "y": 10, "w": 40, "h": 15},
                    {"text": "你好", "x": 40, "y": 40, "w": 30, "h": 20},
                ])
    def test_get_messages_rightside_other_bubble_not_avatar(self, _ocr, _tav,
                                                            _fb, _dc, _cap,
                                                            _find, _switch):
        """RED 复现：other 气泡框被误检在右侧（left 靠近右缘）时，消息读取
        不得被清空（真机根因：19:10 林小满已选中读 0 条，OCR 17 行全被
        _in_avatar 丢弃，other_avatar_x_max 被右侧误检框污染成 1220）。

        新实现里归属只看头像——气泡框误检在右侧与归属/剔除无关，消息照读。
        """
        b = VisualBackend()
        b.connect()
        msgs = b.get_messages("林小满")
        self.assertTrue(msgs, "右侧误检 other 框不得导致消息读空")
        self.assertTrue(any("你好" in m.content for m in msgs),
                        "「你好」应被读到（归属只看头像）")
        self.assertFalse(any("18:47" in m.content for m in msgs),
                         "块外的时间分隔行不读")
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (30, 30, 31)))
    @mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                return_value={"bg": (30, 30, 31), "other": (47, 47, 48),
                              "self": (53, 210, 141)})
    @mock.patch("wx_backend.visual_backend.find_bubble_boxes",
                return_value=[])
    @mock.patch("wx_backend.visual_backend.find_media_boxes",
                return_value=[(100, 160, 80, 140)])  # 1x：bot 文件卡片 media 框，横跨中线 100
    @mock.patch("wx_backend.visual_backend.detect_avatar_tops")
    @mock.patch("wx_backend.visual_backend.ocr_image")
    def test_get_messages_bot_file_card_sender_self(self, _ocr, _dav, _fmb, _fbb,
                                                     _dc, _cap, _find, _switch):
        """bot 文件卡片（media 框，无气泡）在本轮读取范围之外——不返回。

        旧实现把 bot 文件卡片文件名读成 sender='self' 交给上层过滤；用户定案
        后像素层只读「bot 最后一条消息之后、下一条对方头像上边界以下」的对方
        新消息，bot 自己的卡片（含文件名 OCR 行）不在任何对方消息块内 → 不读。
        真机锚点：bot 发 index.html，卡片 media 框横跨中线、文件名行 x 靠左。
        """
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)

        def fake_avatar_tops(img, bg, side):
            return [30] if side == "right" else [150]  # bot 卡片 / 对方新消息

        _dav.side_effect = fake_avatar_tops
        _fmb.return_value = [(30, 90, 80, 140)]   # bot 文件卡片 media 框
        _ocr.return_value = [
            # bot 卡片文件名（在 bot 卡片块内，不在对方块 [150, ...] 内）
            {"text": "index.html", "x": 90, "y": 35, "w": 40, "h": 20},
            # 对方新消息（锚定对方头像 150）
            {"text": "帮我改一下", "x": 50, "y": 150, "w": 60, "h": 20},
        ]
        b.connect()
        msgs = b.get_messages("林小满")
        b.close()
        self.assertEqual(len(msgs), 1, "只返回对方新消息")
        self.assertEqual(msgs[0].content, "帮我改一下")
        self.assertEqual(msgs[0].sender, "林小满")
        self.assertFalse(any("index.html" in m.content for m in msgs),
                         "bot 自己的文件卡片文字不得混进对方消息")

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (30, 30, 31)))
    @mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                return_value={"bg": (30, 30, 31), "other": (47, 47, 48),
                              "self": (53, 210, 141)})
    @mock.patch("wx_backend.visual_backend.find_bubble_boxes",
                return_value=[(90, 110, 40, 140, False)])  # 1x：bot 文件卡片被判非 self 气泡
    @mock.patch("wx_backend.visual_backend.find_media_boxes",
                return_value=[])  # 这次文件卡片没被 media 检测抓到
    @mock.patch("wx_backend.visual_backend.detect_avatar_tops")
    @mock.patch("wx_backend.visual_backend.ocr_image")
    def test_get_messages_bot_file_card_bubble_sender_self(self, _ocr, _dav, _fmb, _fbb,
                                                            _dc, _cap, _find, _switch):
        """文件卡片判据（用户截图定义）：面板色与文字气泡一致 + 面板内右侧
        一个实心小图标；图标颜色不参与判定。

        真机事故：Excel 图标是绿色，与微信自己气泡绿只差 ~55 色阶 → 落进
        self 通道被吞掉、面板又被判普通气泡 → 文件卡片退化成文字消息。新判据
        在面板内部找「实心小色块」（真机标定：图标 69x55 填充率 0.96，
        文字行高 ≤31 填充率 ≤0.48），绿图标/蓝图标一视同仁。
        """
        from PIL import ImageDraw
        img = _solid((400, 300), (30, 30, 31))
        d = ImageDraw.Draw(img)
        d.rectangle([40, 100, 300, 200], fill=(47, 47, 48))       # 文件卡片面板
        d.rectangle([240, 115, 292, 167], fill=(6, 174, 86))      # 右侧图标（Excel 绿）
        _cap.return_value = img
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)

        def fake_avatar_tops(img, bg, side):
            return [] if side == "right" else [100]

        _dav.side_effect = fake_avatar_tops
        _fbb.return_value = [(100, 200, 40, 300, False)]  # 卡片面板判成气泡框
        _fmb.return_value = []                            # 图标不出现在 media 通道
        _ocr.return_value = [
            {"text": "面试评分表.xlsx", "x": 50, "y": 110, "w": 120, "h": 20},
            {"text": "19.6K", "x": 50, "y": 170, "w": 40, "h": 15},
        ]
        b.connect()
        msgs = b.get_messages("林小满")
        b.close()
        self.assertEqual(len(msgs), 1, "文件卡片合成一条消息")
        self.assertIs(msgs[0].type, MessageType.FILE,
                      "面板 + 右侧实心图标 → 文件消息（type=FILE）")
        self.assertIn("面试评分表.xlsx", msgs[0].content)
        self.assertIn("19.6K", msgs[0].content, "文件块文字保留（文件名/大小）")

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((400, 300), (30, 30, 31)))
    @mock.patch("wx_backend.visual_backend.find_media_boxes", return_value=[])
    @mock.patch("wx_backend.visual_backend.detect_avatar_tops")
    @mock.patch("wx_backend.visual_backend.ocr_image")
    def test_text_bubble_without_icon_is_not_file(self, _ocr, _dav, _fmb,
                                                  _cap, _find, _switch):
        """反向：面板内没有实心图标（纯文字气泡）→ 仍是文字消息，不误判文件。"""
        from PIL import ImageDraw
        img = _solid((400, 300), (30, 30, 31))
        d = ImageDraw.Draw(img)
        d.rectangle([40, 100, 300, 160], fill=(47, 47, 48))  # 纯文字气泡（无图标）
        # 文字笔画：多条细横线（模拟字形笔画；实心块才会被判图标）。
        # 行距 4px < y_gap 6 → 连通域会把相邻行并成一块，但笔画填充率低
        # （真机文字行 ≤0.48）——判据靠填充率而不是高度分开图标。
        for y in range(112, 156, 4):
            d.rectangle([55, y, 220, y + 1], fill=(230, 230, 230))
        with mock.patch("wx_backend.visual_backend.capture_window",
                        return_value=img):
            b = VisualBackend()
            b._message_region = (0.0, 0.0, 1.0, 1.0)

            def fake_avatar_tops(img, bg, side):
                return [] if side == "right" else [100]

            _dav.side_effect = fake_avatar_tops
            with mock.patch("wx_backend.visual_backend.find_bubble_boxes",
                            return_value=[(100, 160, 40, 300, False)]):
                _ocr.return_value = [
                    {"text": "这是一条普通文字消息", "x": 50, "y": 110,
                     "w": 160, "h": 40},
                ]
                b.connect()
                msgs = b.get_messages("林小满")
        b.close()
        self.assertEqual(len(msgs), 1)
        self.assertIs(msgs[0].type, MessageType.TEXT,
                      "无右侧实心图标 → 文字消息，不得误判为文件")

    def test_analyze_window_avatar_geometry_sender(self):
        """sender 判定改头像几何：bot 文件卡片（无绿气泡、右边缘靠右）归 bot，
        对方长文字（右边缘靠右、旧 r>0.75 判据会误判）归对方。
        真机锚点：bot 文件 r/w=0.83、对方长文字 r/w=0.80，宽度阈值切不开，
        但头像一右一左，天然分离。"""
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)

        def fake_avatar_tops(img, bg, side):
            return [0, 50] if side == "right" else [100]

        with mock.patch.object(b, "_switch_chat", return_value=True), \
         mock.patch.object(b, "read_title", return_value="林小满"), \
             mock.patch.object(b, "_refresh", return_value=_solid((200, 200), (30, 30, 31))), \
             mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                        return_value={"bg": (30, 30, 31), "other": (47, 47, 48),
                                      "self": (53, 210, 141)}), \
             mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                        side_effect=fake_avatar_tops), \
             mock.patch("wx_backend.visual_backend.find_bubble_boxes",
                        return_value=[
                            (0, 40, 20, 160, True),      # bot 绿气泡
                            (50, 80, 30, 160, False),    # bot 文件卡片(右对齐)
                            (100, 130, 10, 160, False),  # 对方长文字(右边缘靠右 r=160)
                        ]), \
             mock.patch("wx_backend.visual_backend.find_media_boxes",
                        return_value=[]):
            win = b.analyze_window("林小满")
        self.assertEqual(win["bot_bottom"], 100,
                         "bot_bottom = 我方最后头像之后第一条消息上边框（几何，不再取气泡 bottom）")
        self.assertEqual(win["other_text"], [(100, 130, 10, 160)],
                         "对方长文字即使右边缘靠右也不得归 bot")

    def test_analyze_window_avatar_geometry_bot_media(self):
        """bot 图片（media 框，无气泡）对齐右侧头像 → 归 bot，不落 other_media；
        没有对方头像 = 没有对方新消息（用户定案：不做任何降级）。"""
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)

        def fake_avatar_tops(img, bg, side):
            return [0] if side == "right" else []

        with mock.patch.object(b, "_switch_chat", return_value=True), \
         mock.patch.object(b, "read_title", return_value="林小满"), \
             mock.patch.object(b, "_refresh", return_value=_solid((200, 200), (30, 30, 31))), \
             mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                        return_value={"bg": (30, 30, 31), "other": (47, 47, 48),
                                      "self": (53, 210, 141)}), \
             mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                        side_effect=fake_avatar_tops), \
             mock.patch("wx_backend.visual_backend.find_bubble_boxes",
                        return_value=[]), \
             mock.patch("wx_backend.visual_backend.find_media_boxes",
                        return_value=[(0, 100, 100, 180)]):
            win = b.analyze_window("林小满")
        self.assertIsNone(win["bot_bottom"],
                          "无对方头像 = 无对方新消息（分析区上沿 None，不兜底）")
        self.assertFalse(win["has_other"])
        self.assertEqual(win["other_media"], [],
                         "bot 图片不得落入窗口内对方媒体")

    def test_analyze_window_has_other_geometry(self):
        """几何判据：气泡/media 漏检时，has_other 仍靠头像几何判「有对方新消息」。
        真机锚点：深色图片+文字混排，气泡色漂移导致 find_bubble_boxes 漏检
        文字气泡、find_media_boxes 只截到图片中间段(顶部不对齐头像)，旧逻辑
        other_text/other_media 全空误判「窗口空」。新逻辑 other_new_tops=
        [490,588,1055] 纯几何判 has_other。"""
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)

        def fake_avatar_tops(img, bg, side):
            return [85, 392] if side == "right" else [0, 250, 490, 588, 1055]

        with mock.patch.object(b, "_switch_chat", return_value=True), \
         mock.patch.object(b, "read_title", return_value="林小满"), \
             mock.patch.object(b, "_refresh", return_value=_solid((747, 1135), (30, 30, 31))), \
             mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                        return_value={"bg": (30, 30, 31), "other": (47, 47, 48),
                                      "self": (53, 210, 141)}), \
             mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                        side_effect=fake_avatar_tops), \
             mock.patch("wx_backend.visual_backend.find_bubble_boxes",
                        return_value=[(85, 214, 131, 621, True),
                                      (392, 454, 131, 621, True)]), \
             mock.patch("wx_backend.visual_backend.find_media_boxes",
                        return_value=[(733, 891, 160, 319)]):  # 只截图片中间段
            win = b.analyze_window("林小满")
        self.assertTrue(win["has_other"],
                        "气泡/media 漏检时，头像几何仍应判有对方新消息")
        self.assertEqual(win["bot_bottom"], 490,
                         "bot_bottom = 下一条消息头像 top（几何）")

    def test_analyze_window_skip_bot_zero_equals_old(self):
        """skip_bot=0 时 last_bot_top = max(bot_tops)，与旧行为完全一致：
        bot_bottom = 最后一条 bot 头像之后第一条消息上边框。"""
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)

        def fake_avatar_tops(img, bg, side):
            return [50, 200, 350] if side == "right" else [100, 400]

        with mock.patch.object(b, "_switch_chat", return_value=True), \
         mock.patch.object(b, "read_title", return_value="林小满"), \
             mock.patch.object(b, "_refresh", return_value=_solid((200, 200), (30, 30, 31))), \
             mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                        return_value={"bg": (30, 30, 31), "other": (47, 47, 48),
                                      "self": (53, 210, 141)}), \
             mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                        side_effect=fake_avatar_tops), \
             mock.patch("wx_backend.visual_backend.find_bubble_boxes", return_value=[]), \
             mock.patch("wx_backend.visual_backend.find_media_boxes", return_value=[]):
            win = b.analyze_window("林小满", skip_bot=0)
        self.assertEqual(win["bot_bottom"], 400,
                         "skip_bot=0 时 bot_bottom = max(bot_tops)=350 之后第一条消息 top")

    def test_analyze_window_skip_bot_one_skips_last_bot(self):
        """skip_bot=1 时 last_bot_top = sorted(bot_tops)[-2]（跳过最近一条 bot
        占位回复），other_new_tops 随之上移。"""
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)

        def fake_avatar_tops(img, bg, side):
            return [50, 200, 350] if side == "right" else [100, 400]

        with mock.patch.object(b, "_switch_chat", return_value=True), \
         mock.patch.object(b, "read_title", return_value="林小满"), \
             mock.patch.object(b, "_refresh", return_value=_solid((200, 200), (30, 30, 31))), \
             mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                        return_value={"bg": (30, 30, 31), "other": (47, 47, 48),
                                      "self": (53, 210, 141)}), \
             mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                        side_effect=fake_avatar_tops), \
             mock.patch("wx_backend.visual_backend.find_bubble_boxes", return_value=[]), \
             mock.patch("wx_backend.visual_backend.find_media_boxes", return_value=[]):
            win = b.analyze_window("林小满", skip_bot=1)
        self.assertEqual(win["bot_bottom"], 400,
                         "skip_bot=1 取 sorted(bot_tops)[-2]=200；分析区上沿 = 之后"
                         "第一条**对方**头像 top=400（bot 头像 350 不算对方新消息）")
        self.assertEqual(win["other_text"], [],
                         "100 在 last_bot_top=200 之前，不再算对方新消息")
        self.assertTrue(win["has_other"],
                        "400 在 last_bot_top=200 之后保留，other_new_tops 随之上移")

    def test_analyze_window_skip_bot_boundary_clamp(self):
        """bot_tops 不足时 skip = min(skip_bot, len(bot_tops)-1) 兜底不越界：
        仅 1 条 bot 时传 skip_bot=5，skip 收敛到 0，等效旧行为（不越界）。"""
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)

        def fake_avatar_tops(img, bg, side):
            return [200] if side == "right" else [300]

        with mock.patch.object(b, "_switch_chat", return_value=True), \
         mock.patch.object(b, "read_title", return_value="林小满"), \
             mock.patch.object(b, "_refresh", return_value=_solid((200, 200), (30, 30, 31))), \
             mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                        return_value={"bg": (30, 30, 31), "other": (47, 47, 48),
                                      "self": (53, 210, 141)}), \
             mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                        side_effect=fake_avatar_tops), \
             mock.patch("wx_backend.visual_backend.find_bubble_boxes", return_value=[]), \
             mock.patch("wx_backend.visual_backend.find_media_boxes", return_value=[]):
            win = b.analyze_window("林小满", skip_bot=5)
        self.assertEqual(win["bot_bottom"], 300,
                         "skip 兜底到 0，last_bot_top=max(bot_tops)=200，之后第一条消息 top=300")

    def test_analyze_window_skip_bot_empty_bot_tops(self):
        """bot_tops 为空（窗口里没有 bot 消息）→ 全部对方消息都算新消息：
        分析区上沿 = 第一条对方头像 top。"""
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)

        def fake_avatar_tops(img, bg, side):
            return [] if side == "right" else [100]

        with mock.patch.object(b, "_switch_chat", return_value=True), \
         mock.patch.object(b, "read_title", return_value="林小满"), \
             mock.patch.object(b, "_refresh", return_value=_solid((200, 200), (30, 30, 31))), \
             mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                        return_value={"bg": (30, 30, 31), "other": (47, 47, 48),
                                      "self": (53, 210, 141)}), \
             mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                        side_effect=fake_avatar_tops), \
             mock.patch("wx_backend.visual_backend.find_bubble_boxes", return_value=[]), \
             mock.patch("wx_backend.visual_backend.find_media_boxes", return_value=[]):
            win = b.analyze_window("林小满", skip_bot=2)
        self.assertEqual(win["bot_bottom"], 100,
                         "无 bot 消息时分析区上沿 = 第一条对方头像 top（全部算新）")
        self.assertTrue(win["has_other"],
                        "无 bot 时对方消息全部算新消息")

    def test_get_messages_union_parses_group_title(self):
        """联合 OCR（assume_switched）：标题带括号人数 → _current_is_group=True，
        标题拆段按 x 拼接，消息行从消息带产出（标题行不漏进消息）。

        原 analyze_window 读标题判定群聊；合并后由 get_messages 联合 OCR
        的标题解析刷新缓存（用户定案：事件内 OCR 两次封顶）。"""
        b = VisualBackend()
        b._current_chat = "摸鱼”集团"  # assume 路径前提
        # 坐标与配置解耦：标题带 = y<10，消息带坐标不平移（确定性）
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b._title_region = (0.0, 0.0, 0.0, 0.05)
        items = [
            # 标题带（联合区，center-y < 标题区下沿 10px）：拆段标题
            {"text": '"摸鱼"', "x": 40, "y": 4, "w": 80, "h": 8},
            {"text": "集团(5)", "x": 130, "y": 4, "w": 60, "h": 8},
            # 消息带（center-y >= 标题区下沿）
            {"text": "你们好呀", "x": 40, "y": 120, "w": 90, "h": 20},
        ]
        with mock.patch.object(b, "_switch_chat", return_value=True), \
             mock.patch("wx_backend.visual_backend.find_wechat_window",
                        return_value=0x1234), \
             mock.patch("wx_backend.visual_backend.capture_window",
                        return_value=_solid((200, 200), (30, 30, 31))), \
             mock.patch("wx_backend.visual_backend.ocr_image", return_value=items), \
             mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                        return_value={}), \
             mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                        side_effect=lambda region, bg, side:
                            [] if side == "right" else [120]), \
             mock.patch("wx_backend.visual_backend.find_media_boxes",
                        return_value=[]):
            b._hwnd = 0x1234
            b._last_shot = None
            msgs = b.get_messages("摸鱼”集团", assume_switched=True)
        self.assertIs(b._current_is_group, True,
                      "群聊标题带括号人数 → 联合 OCR 刷新 is_group=True")
        self.assertTrue(any(m.content == "你们好呀" for m in msgs),
                        "消息带条目应产出消息")
        self.assertFalse(any("集团" in m.content for m in msgs),
                         "标题行不得漏进消息")
        b.close()

    def test_get_messages_union_parses_private_title(self):
        """私聊标题（无括号人数）→ 联合 OCR 刷新 is_group=False。"""
        b = VisualBackend()
        b._current_chat = "林小满"
        b._current_is_group = True  # 上一轮群聊残留 → 本事件必须刷新为 False
        items = [
            {"text": "林小满", "x": 40, "y": 4, "w": 70, "h": 8},
            {"text": "在吗", "x": 40, "y": 120, "w": 50, "h": 20},
        ]
        with mock.patch.object(b, "_switch_chat", return_value=True), \
             mock.patch("wx_backend.visual_backend.find_wechat_window",
                        return_value=0x1234), \
             mock.patch("wx_backend.visual_backend.capture_window",
                        return_value=_solid((200, 200), (30, 30, 31))), \
             mock.patch("wx_backend.visual_backend.ocr_image", return_value=items), \
             mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                        return_value={}), \
             mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                        return_value=[]), \
             mock.patch("wx_backend.visual_backend.find_media_boxes",
                        return_value=[]):
            b._hwnd = 0x1234
            b._last_shot = None
            b.get_messages("林小满", assume_switched=True)
        self.assertIs(b._current_is_group, False,
                      "私聊标题无括号人数 → 联合 OCR 刷新 is_group=False")
        b.close()

    def test_detect_avatar_tops_geometry(self):
        """detect_avatar_tops 真实实现：窄带非背景块标出头像顶部 y，
        高度超标的块（如滚动条）被丢弃，bg=None 返回空。"""
        from PIL import ImageDraw
        img = _solid((747, 1135), (30, 30, 31))
        d = ImageDraw.Draw(img)
        # 头像高 = 固定 63px（真机标定）；窄带右侧 [627,732]、左侧 [14,104]
        d.rectangle([630, 50, 730, 112], fill=(200, 100, 100))    # 右侧头像(高63)
        d.rectangle([15, 30, 104, 92], fill=(200, 100, 100))      # 左侧头像(高63)
        d.rectangle([630, 200, 730, 400], fill=(200, 100, 100))   # 右侧超高块(噪声)
        self.assertEqual(detect_avatar_tops(img, (30, 30, 31), "right"), [50])
        self.assertEqual(detect_avatar_tops(img, (30, 30, 31), "left"), [30])
        self.assertEqual(detect_avatar_tops(img, None, "right"), [])

    def test_detect_avatar_tops_short_region(self):
        """矮消息区（任务栏可见 → 窗口按工作区收口后区域更矮）下头像仍检得出。

        期望头像高是固定 63px（UI 固定元素，与窗口尺寸无关）。旧实现按
        "区域高÷18"算：区域高 600 时期望 33px → 上限 46px，真实 62px 的头像
        被整列丢掉（消息读不到）。"""
        from PIL import ImageDraw
        img = _solid((747, 600), (30, 30, 31))
        d = ImageDraw.Draw(img)
        d.rectangle([630, 50, 730, 112], fill=(200, 100, 100))   # 高 62 的头像
        self.assertEqual(detect_avatar_tops(img, (30, 30, 31), "right"), [50])


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


def _solid_with_highlight_rows(size=(200, 200)):
    """白底图 + y∈[40,70] 一条浅灰选中高亮行（微信列表选中背景 ≈ #F0F0F0）。

    其余行保持纯白（未选中背景）——用于「高亮行 vs 非高亮行」两种背景断言。
    """
    from PIL import ImageDraw
    img = _solid(size, (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 40, 200, 70], fill=(240, 240, 240))  # 选中高亮行
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

    def test_orange_noise_filtered(self):
        """emoji 级橙红像素（B 远低于 G）不得判为角标——需 G/B 双下限。

        真机事故根因：列表区消息预览里的 emoji 边缘有 9 个橙红像素
        (237,112,37)，旧阈值只卡 b<=130 而无下限，橙色（b=37）蒙混过关
        → bot 对同一会话无限循环「发现新消息」。品牌红特征 G≈B
        （#FA5151=250,81,81），据此补 G/B 下限。
        """
        from wx_backend.visual_backend import _detect_red_clusters
        from PIL import ImageDraw
        img = _solid((200, 200), (255, 255, 255))
        d = ImageDraw.Draw(img)
        d.rectangle([20, 46, 40, 66], fill=(237, 112, 37))  # 橙红：b 远低于 g
        self.assertEqual(_detect_red_clusters(img), [])

    def test_oversized_red_filtered(self):
        """整块大红（远超角标尺寸，如红色头像）不得判为角标——需面积上限。

        微信 PC 角标尺寸固定（约 26x27 ≈ 518px，不随未读数变宽），
        面积上限据此设，用于排除红色头像/大块红色图形。
        """
        from wx_backend.visual_backend import _detect_red_clusters
        from PIL import ImageDraw
        img = _solid((200, 200), (255, 255, 255))
        d = ImageDraw.Draw(img)
        d.rectangle([20, 46, 70, 96], fill=(250, 81, 81))  # 51x51 ≈ 2600px
        self.assertEqual(_detect_red_clusters(img), [])


class TestSessionNameAnchor(unittest.TestCase):
    """会话名获取 v3：红圈锚定（几何）+ 保留原始符号。

    真机实测依据（.rivet/scratch/dump_session_geo.py / verify_anchor2d.py，
    窗口 1300x1610，列表区 426x1452 局部坐标）：

        y     x    w    h   text
        4    65   36   34   '0'                     ← 角标数字
       22    97   83   32   '林小满'                 ← 名字
       24   349   55   26   '20:33'                 ← 时间戳
      134   113  142   38   '“摸鱼”集团'            ← 名字（引号完整）
      139   349   55   25   '19:03'
      172    97  286   31   '哆菈A夢：就这样吧😄，我还…'  ← 预览

    关键：名字与时间戳的 y 只差 2px，仅按 y 会翻车；红圈在头像位
    （x≈82）与名字相邻，与时间戳（x≈349）差一个数量级 → 必须二维距离。
    """

    def test_pick_block_near_badge_beats_timestamp(self):
        """名字块离红圈最近：时间戳(Δy=2)与角标数字不得胜出。"""
        from wx_backend.visual_backend import _pick_block_near_badge
        blocks = [
            {"text": "0", "x": 65, "y": 4, "w": 36, "h": 34},
            {"text": "林小满", "x": 97, "y": 22, "w": 83, "h": 32},
            {"text": "20:33", "x": 349, "y": 24, "w": 55, "h": 26},
        ]
        got = _pick_block_near_badge((82, 22), blocks)
        self.assertIsNotNone(got)
        self.assertEqual(got["text"], "林小满")

    def test_pick_block_near_badge_keeps_quotes(self):
        """带引号会话名原样保留——不切碎、不剥符号。"""
        from wx_backend.visual_backend import _pick_block_near_badge
        blocks = [
            {"text": "“摸鱼”集团", "x": 113, "y": 134, "w": 142, "h": 38},
            {"text": "19:03", "x": 349, "y": 139, "w": 55, "h": 25},
            {"text": "哆菈A夢：就这样吧😄，我还…", "x": 97, "y": 172, "w": 286, "h": 31},
        ]
        got = _pick_block_near_badge((82, 134), blocks)
        self.assertIsNotNone(got)
        self.assertEqual(got["text"], "“摸鱼”集团")

    def test_pick_block_near_badge_too_far_returns_none(self):
        """所有块都离红圈过远 → None（宁可不处理，不拿错名去操作）。"""
        from wx_backend.visual_backend import _pick_block_near_badge
        blocks = [{"text": "某某", "x": 300, "y": 900, "w": 80, "h": 30}]
        self.assertIsNone(_pick_block_near_badge((10, 10), blocks))

    def test_pick_main_name_keeps_leading_symbols(self):
        """名字开头的引号属于名字本身，不得被清洗掉。

        历史缺陷：_pick_main_name 用 re.sub(r"^[^汉字A-Za-z0-9]+", ...)
        剥首符号，把 OCR 读对的「“摸鱼”集团」削成「摸鱼”集团」，
        再经 memory_key 归一化成「摸鱼集团」→ 用户在记忆页看到残缺名。
        """
        from wx_backend.visual_backend import VisualBackend
        lines = [{"text": "“摸鱼”集团", "x": 113, "y": 134, "w": 142, "h": 38}]
        picked = VisualBackend._pick_main_name(lines)
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "“摸鱼”集团")


class TestIterUnreadSessions(unittest.TestCase):
    def _backend(self):
        b = VisualBackend()
        b._hwnd = 0x1234  # 不 connect（避免真实窗口截图），直接设句柄
        b._last_shot = None
        return b

    @mock.patch("wx_backend.visual_backend.VisualBackend._refresh",
                return_value=_solid_with_badge())
    @mock.patch("wx_backend.visual_backend.VisualBackend._is_row_selected",
                side_effect=[False, True])   # 判定「未选中」→ 点击后复验「已选中」
    @mock.patch("pyautogui.click")
    @mock.patch("time.sleep")
    def test_yields_only_badged_session(self, _sleep, _click, _sel, _refresh):
        """只产出带红圈的条目；**全程零 OCR**（会话名由下游联合 OCR 给出）。

        语义已从「列表区 OCR + 几何锚定」改为几何驱动（红圈定位 + 点击 +
        像素复验），断言口径同步——见 iter_unread_sessions 的 docstring。
        """
        b = self._backend()
        entries = list(b.iter_unread_sessions())
        self.assertEqual(entries, [(30, 56)], "产出红圈中心位置条目")

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

    def test_parse_title_group_has_member_count(self):
        """标题带括号人数 = 群聊（视觉权威信号，替代名称启发式）。"""
        from wx_backend.visual_backend import parse_title
        self.assertEqual(parse_title('摸鱼"集团(5)'), ("摸鱼\"集团", True, 5))
        self.assertEqual(parse_title("林小满"), ("林小满", False, None))
        self.assertEqual(parse_title(""), ("", False, None))
        self.assertEqual(parse_title(None), ("", False, None))

    @mock.patch("wx_backend.visual_backend.VisualBackend._refresh",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    {"text": "X", "x": 300, "y": 10, "w": 10, "h": 10},
                    {"text": '"摸鱼"', "x": 100, "y": 10, "w": 80, "h": 20},
                    {"text": "集团(5)", "x": 185, "y": 10, "w": 60, "h": 20},
                ])
    def test_read_title_joins_split_segments(self, _ocr, _refresh):
        """标题被 OCR 拆成多段时按 x 拼接（真机：'"摸鱼"' + '集团(5)'）。

        RED 复现：真机 read_title 只读到'集团(5)'——OCR 把带引号的
        '摸鱼'与'集团(5)'分成两个独立项，旧实现只取最长单行导致标题缺左半。
        """
        b = self._backend()
        b._title_region = (0.0, 0.0, 1.0, 1.0)
        self.assertEqual(b.read_title(), '"摸鱼"集团(5)')

    @mock.patch("wx_backend.visual_backend.VisualBackend._refresh",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.ocr_image", return_value=[])
    def test_unread_poll_refresh_without_foreground(self, _ocr, _refresh):
        """红圈轮询截图不应置前微信——后台静默像素检测，不打断用户。

        RED 复现：用户反馈 bot 一直把微信拉到前台（真机日志每轮轮询
        _refresh 都 _foreground）。红圈像素检测（iter_unread）是只读
        轮询，必须在后台进行；只有检测到新消息后的点击/读取/发送才置前。
        """
        b = self._backend()
        list(b.iter_unread_sessions())
        self.assertEqual(_refresh.call_args, mock.call(force=True, foreground=False))

    @mock.patch("wx_backend.visual_backend.VisualBackend._refresh",
                return_value=_solid_with_badge())
    @mock.patch("wx_backend.visual_backend.VisualBackend._is_row_selected",
                side_effect=[False, True])
    @mock.patch("pyautogui.click")
    @mock.patch("time.sleep")
    def test_iter_unread_yields_position_entries(self, _sleep, _click, _sel,
                                                 _refresh):
        """红圈几何链路产出**位置条目**（此刻已知位置、尚无名字）——会话名
        由后续联合 OCR 的标题带给出，位置条目同时是主循环的退避键。"""
        b = self._backend()
        entries = list(b.iter_unread_sessions())
        self.assertEqual(entries, [(30, 56)])
        # 红圈整窗中心 (30,56)，窗口偏移 0 → 屏幕坐标 (30,56)


class TestSelectedRowHighlight(unittest.TestCase):
    """自学习选中高亮检测：点击成功后采样条目行背景色缓存为选中色，
    _is_row_selected 命中缓存 → 已选中不点击（防 toggle 重复点击取消选中）。"""

    def _backend(self):
        b = VisualBackend()
        b._hwnd = 0x1234  # 不 connect，直接设句柄（GetWindowRect 失败 rect 零值）
        b._last_shot = None
        b._session_coords = {"林小满": (30, 50)}  # 屏幕坐标（窗口偏移 0 = 截图坐标）
        return b

    @mock.patch("pyautogui.click")
    @mock.patch("wx_backend.visual_backend.VisualBackend._refresh",
                return_value=_solid_with_highlight_rows())
    @mock.patch("wx_backend.visual_backend.VisualBackend.read_title",
                return_value="林小满")
    def test_switch_chat_learns_selected_color(self, _rt, _refresh, _click):
        """点击成功且标题非空 → 采样该条目行背景色缓存为选中色；
        _is_row_selected 对高亮行 True、对非高亮行 False（两种背景断言）。"""
        b = self._backend()
        self.assertTrue(b._switch_chat("林小满", force=True))
        _click.assert_called_once_with(30, 50)
        self.assertEqual(b._selected_row_color, (240, 240, 240),
                         "点击成功后应采样该行背景色缓存为选中色")
        self.assertTrue(b._is_row_selected(50), "高亮行（选中）应命中缓存选中色")
        self.assertFalse(b._is_row_selected(150), "非高亮行（未选中）不应命中")

    @mock.patch("pyautogui.click")
    @mock.patch("wx_backend.visual_backend.VisualBackend._refresh",
                return_value=_solid_with_highlight_rows())
    def test_switch_chat_highlight_hit_skips_click(self, _refresh, _click):
        """高亮命中缓存选中色 → 已选中，不点击直接返回 True（防 toggle 重复点击）。"""
        b = self._backend()
        b._selected_row_color = (240, 240, 240)
        self.assertTrue(b._switch_chat("林小满"))
        _click.assert_not_called()

    @mock.patch("pyautogui.click")
    @mock.patch("wx_backend.visual_backend.VisualBackend._refresh",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.VisualBackend.read_title",
                return_value="林小满")
    def test_switch_chat_fallback_title_when_no_highlight(self, _rt, _refresh,
                                                          _click):
        """高亮缓存缺失 → 回退 UI 标题比较：标题=目标会话 → 已选中不点击。"""
        b = self._backend()  # _selected_row_color 默认 None（缓存缺失）
        self.assertTrue(b._switch_chat("林小满"))
        _click.assert_not_called()

    @mock.patch("wx_backend.visual_backend.VisualBackend.iter_sessions")
    @mock.patch("wx_backend.visual_backend.VisualBackend.resolve_chat_coord",
                return_value=(30, 50))
    @mock.patch("pyautogui.click")
    @mock.patch("wx_backend.visual_backend.VisualBackend._refresh",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.VisualBackend.read_title",
                return_value="周雨桐")
    def test_switch_chat_resolve_coord_fallback(self, _rt, _refresh, _click,
                                                _resolve, _iter):
        """缓存无坐标 → 现读列表区 OCR 定位（resolve_chat_coord，触发式发送
        主路径），不再先跑整窗 iter_sessions 重建（慢路径白花 1.2s）。"""
        b = self._backend()
        b._session_coords = {}
        self.assertTrue(b._switch_chat("周雨桐", force=True))
        _resolve.assert_called_once_with("周雨桐")
        _iter.assert_not_called()
        _click.assert_called_once_with(30, 50)
        self.assertEqual(b._current_chat, "周雨桐")


# ---- 区域恒为内置常量（用户自定义标定已撤回） ----


class TestRegionDefaults(unittest.TestCase):
    """三区域恒用模块内置标定（运行期不再读任何用户标定文件）。

    撤回理由（真机实证）：头像窄带 = 框宽 2%~14%、气泡/媒体闸同样是框的比例
    ——只在「框 == 聊天区」这一前提下成立；用户手画的大框/小框会让这些常量
    整体漂移（事故：自设区域 + 非默认窗口尺寸 → 对方头像整列检不出、消息读
    不到，自己侧漏检 → 自家图片被当对方消息）。

    注：区域由 `regions_for_window(标定窗口)` 现算（竖向锚点分顶/底，见
    visual_regions），与模块常量的书写值（四位小数）差 < 1e-4。
    """

    def _assert_regions_match(self, b, vb):
        for got, want in ((b._session_region, vb._SESSION_REGION_RATIO),
                          (b._message_region, vb._MESSAGE_REGION_RATIO),
                          (b._title_region, vb._TITLE_REGION_RATIO)):
            for g, w in zip(got, want):
                self.assertAlmostEqual(g, w, delta=1e-4)

    def test_backend_uses_builtin_regions(self):
        """新建后端实例 → 三区域 = 模块标定常量。"""
        import wx_backend.visual_backend as vb
        b = VisualBackend()
        self._assert_regions_match(b, vb)

    def test_user_region_file_is_not_consumed(self):
        """标定文件躺在它原来的位置也不生效（加载/保存/热更新接口全部移除）。"""
        import tempfile
        import wx_backend.visual_backend as vb
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "wx_ocr_region.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"message_region": [0.35, 0.1, 1.0, 1.0],
                           "session_region": [0.0, 0.1, 0.35, 1.0]}, f)
            b = VisualBackend()
            for g, w in zip(b._message_region, vb._MESSAGE_REGION_RATIO):
                self.assertAlmostEqual(g, w, delta=1e-4)
        for name in ("_load_region_config", "_read_region_config",
                     "save_region_config", "_region_config_paths"):
            self.assertFalse(hasattr(vb, name), f"{name} 应随标定撤回一并移除")
        self.assertFalse(hasattr(VisualBackend, "reload_regions"))


# ---- 窗口查找 / 矩形（Win32 mock） ----


class TestWindowLookup(unittest.TestCase):
    """微信 4.1.12 窗口类名是 Qt51514QWindowIcon，uiautomation 类名搜索与
    BoundingRectangle 均不可靠——窗口定位改标题精确匹配 + GetWindowRect。"""

    def test_find_window_by_title_exact_and_visible(self):
        """标题精确匹配 + 仅可见窗口；'微信' 不误匹配 '微信文件'。"""
        titles = {0x111: "图片和视频", 0x222: "微信", 0x333: "微信文件"}
        visible = {0x111: True, 0x222: False, 0x333: True}
        hwnds = list(titles.keys())

        def fake_enum(cb, _lparam):
            for h in hwnds:
                if not cb(h, _lparam):
                    break
            return True

        def fake_gettext(hwnd, buf, _n):
            buf.value = titles.get(hwnd, "")
            return len(buf.value)

        with mock.patch("wx_backend.visual_backend.u32.EnumWindows", side_effect=fake_enum), \
                mock.patch("wx_backend.visual_backend.u32.GetWindowTextW", side_effect=fake_gettext), \
                mock.patch("wx_backend.visual_backend.u32.IsWindowVisible",
                           side_effect=lambda h: visible.get(h, False)):
            self.assertEqual(find_window_by_title("图片和视频"), 0x111)
            self.assertIsNone(find_window_by_title("微信"))  # 不可见 → 跳过
            self.assertEqual(find_window_by_title("微信文件"), 0x333)

    def test_find_window_by_title_empty(self):
        self.assertIsNone(find_window_by_title(""))
        self.assertIsNone(find_window_by_title(None))

    def test_window_rect(self):
        rects = {0x1234: (10, 20, 510, 320)}  # left, top, right, bottom

        def fake_getrect(hwnd, out):
            v = rects.get(hwnd)
            if v is None:
                return 0
            r = out._obj  # CArgObject 包装的真实 RECT
            r.left, r.top, r.right, r.bottom = v
            return 1

        with mock.patch("wx_backend.visual_backend.u32.GetWindowRect", side_effect=fake_getrect):
            self.assertEqual(window_rect(0x1234), (10, 20, 500, 300))
            self.assertIsNone(window_rect(0x999))  # GetWindowRect 失败
        self.assertIsNone(window_rect(None))

    def test_ensure_window_visible_restores_iconic(self):
        """最小化窗口先 SW_RESTORE 再返回 True；正常窗口不动。"""
        with mock.patch("wx_backend.visual_backend.u32.IsIconic", side_effect=lambda h: h == 0x111), \
                mock.patch("wx_backend.visual_backend.u32.ShowWindow") as show:
            self.assertTrue(ensure_window_visible(0x111))
            show.assert_called_once_with(0x111, 9)  # SW_RESTORE
            show.reset_mock()
            self.assertTrue(ensure_window_visible(0x222))  # 非最小化 → 不调用 ShowWindow
            show.assert_not_called()
        self.assertFalse(ensure_window_visible(None))


# ---- 头像划块归属（_bucket_avatar） ----


class TestAvatarBucket(unittest.TestCase):
    """头像划块：消息框顶部落入排序头像边界序列的哪个区间，归属该区间头像。"""

    def test_interval_membership(self):
        tops = [38, 801]
        self.assertEqual(_bucket_avatar(38, tops), 38)    # 区间下边界自身
        self.assertEqual(_bucket_avatar(400, tops), 38)   # [38, 801) → 38
        self.assertEqual(_bucket_avatar(800, tops), 38)   # 边界前最后一头像
        self.assertEqual(_bucket_avatar(801, tops), 801)  # [801, ∞) → 801
        self.assertEqual(_bucket_avatar(2000, tops), 801)

    def test_below_first_returns_none(self):
        self.assertIsNone(_bucket_avatar(29, [38, 801]))
        self.assertIsNone(_bucket_avatar(37, [38, 801]))

    def test_unsorted_and_duplicates(self):
        self.assertEqual(_bucket_avatar(100, [801, 38, 38]), 38)
        self.assertEqual(_bucket_avatar(900, [801, 38]), 801)

    def test_empty_tops(self):
        self.assertIsNone(_bucket_avatar(100, []))


# ---- _in_media：媒体框内文字剔除（图片/文件只产 1 条媒体消息） ----


class TestGetMessagesInMedia(unittest.TestCase):
    """media 框内 OCR 文字不得拆成假文字消息；紧贴框顶的发送者名被吞掉。

    真实群聊缺陷（region_1x.png）：图片块内文字 '我不是'/'大肥鱼' 被 OCR
    读出后拆出 sender='我不是' 的假消息；发送者名 '林小满' 紧贴图片框顶，
    不得成为独立文字消息——图片消息只产 1 条（由 analyze_window 媒体框承载）。
    """

    def _backend(self):
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b.connect()
        return b

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window",
                return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((400, 300), (30, 30, 31)))
    @mock.patch("wx_backend.visual_backend.ocr_image")
    @mock.patch("wx_backend.visual_backend.detect_avatar_tops")
    @mock.patch("wx_backend.visual_backend.find_media_boxes")
    def test_media_text_dropped_and_name_consumed(self, _media, _tops, _ocr,
                                                  _cap, _find, _switch):
        """1x 坐标：左头像 top=100、右头像 top=30；media 框 (150,250,40,160)
        顶部 150 落入左头像 100 的区间 → 对方媒体框。
        发送者名 '林小满'（y=140）紧贴 media 框顶（y=150）→ 被吞掉。
        media 框内 '我不是'/'大肥鱼'（y=160/180）→ 剔除不产消息。"""
        _tops.side_effect = lambda img, bg, side: [100] if side == "left" else [30]
        _media.return_value = [(150, 250, 40, 160)]
        _ocr.return_value = [
            {"text": "林小满", "x": 25, "y": 140, "w": 25, "h": 10},
            {"text": "我不是", "x": 50, "y": 160, "w": 30, "h": 10},
            {"text": "大肥鱼", "x": 50, "y": 180, "w": 30, "h": 10},
        ]
        b = self._backend()
        with mock.patch.object(b, "read_title", return_value="林小满"):
            msgs = b.get_messages("林小满")
        self.assertEqual(len(msgs), 0,
                         "图片块文字（含发送者名行）不得拆成文字消息")
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window",
                return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((400, 300), (30, 30, 31)))
    @mock.patch("wx_backend.visual_backend.ocr_image")
    @mock.patch("wx_backend.visual_backend.detect_avatar_tops")
    @mock.patch("wx_backend.visual_backend.find_media_boxes")
    def test_text_outside_media_kept(self, _media, _tops, _ocr,
                                     _cap, _find, _switch):
        """图片块只丢自己的字：另一条（别的头像锚定的）文字消息照常产出。"""
        _tops.side_effect = lambda img, bg, side: [100, 250] if side == "left" else [30]
        _media.return_value = [(100, 200, 40, 160)]   # 头像 100 的消息 = 图片
        _ocr.return_value = [
            # 图片块内文字（头像 100 的块）→ 剔除
            {"text": "我不是", "x": 50, "y": 160, "w": 30, "h": 10},
            # 另一条正常文字消息（锚定头像 250，媒体框在其上方）→ 保留
            {"text": "这是一条正常的文字消息", "x": 50, "y": 255,
             "w": 100, "h": 10},
        ]
        b = self._backend()
        with mock.patch.object(b, "read_title", return_value="林小满"):
            msgs = b.get_messages("林小满")
        self.assertEqual(len(msgs), 1, "图片块文字剔除、另一条文字消息保留")
        self.assertEqual(msgs[0].content, "这是一条正常的文字消息")
        b.close()

    def test_message_region_uses_single_shot_ocr(self):
        """消息区 OCR 走单片 ocr_image（v2.1.3 废弃分片：分片切边界把整字
        切成两半误识，真机「排」被切左半成「非」、探针稳定复现碎片「丙」
        「马」；单片读 40+ 字超长行一字不差）。锁死 get_messages 不再走
        分片路径。"""
        b = self._backend()
        b._last_shot = None  # 尺寸哨兵基线清空：capture_window 桩返回 400x300，
        # 与 connect() 时真实窗口截图尺寸无关（哨兵只防真实占位残帧）
        with mock.patch.object(b, "_switch_chat", return_value=True), \
                mock.patch("wx_backend.visual_backend.find_wechat_window",
                           return_value=0x1234), \
                mock.patch("wx_backend.visual_backend.capture_window",
                           return_value=_solid((400, 300), (255, 255, 255))), \
                mock.patch("wx_backend.visual_backend.ocr_image",
                           return_value=[{"text": "单行消息", "x": 20, "y": 30,
                                          "w": 120, "h": 30}]) as m_ocr, \
                mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                           side_effect=lambda region, bg, side:
                               [] if side == "right" else [30]), \
                mock.patch("wx_backend.visual_backend.find_media_boxes",
                           return_value=[]), \
                mock.patch.object(b, "read_title", return_value="测试"):
            msgs = b.get_messages("测试")
        m_ocr.assert_called_once()
        self.assertNotIn(
            "ocr_image_sharded",
            sys.modules["wx_backend.visual_backend"].__dict__,
            "分片函数已删除，消息区不得再走分片")
        self.assertTrue(msgs, "消息区 OCR items 应正常产出消息")
        self.assertEqual(msgs[0].content, "单行消息")
        b.close()


# ---- analyze_window：头像划块归属（替换 tol=40 _aligned 对齐） ----


class TestAnalyzeWindowAvatarBucket(unittest.TestCase):
    """气泡/media 框顶落入头像边界序列区间归属对方；bot 长气泡不误判对方。

    几何坐标与真实 fixture region_1x.png 一致：bot 头像 top=38、
    对方头像 top=801、bot 长气泡 (38,765)、对方气泡 (869,928)、
    林小满图片块 media (838,1117)。"""

    def _backend(self):
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b.connect()
        return b

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.VisualBackend._refresh")
    @mock.patch("wx_backend.visual_backend.detect_avatar_tops")
    @mock.patch("wx_backend.visual_backend.find_bubble_boxes")
    @mock.patch("wx_backend.visual_backend.find_media_boxes")
    def test_bucket_assigns_other_boxes(self, _media, _bubbles, _tops,
                                        _refresh, _switch):
        _refresh.return_value = _solid((747, 1135), (30, 30, 31))
        _tops.side_effect = lambda img, bg, side: [38] if side == "right" else [801]
        _bubbles.return_value = [
            (38, 765, 131, 621, True),    # bot 长气泡（顶 = bot 头像顶 38）
            (869, 928, 140, 229, False),  # 图片内容里与气泡色相近的色块
        ]
        _media.return_value = [(838, 1117, 119, 398)]  # 林小满图片块
        b = self._backend()
        win = b.analyze_window("林小满")
        # 869~928 完全落在图片块 838~1117 内 → 是图片内容里的色块，不是消息
        self.assertEqual(win["other_text"], [],
                         "图片内容内的气泡色块不得当文字消息")
        self.assertEqual(win["other_media"], [(838, 1117, 119, 398)],
                         "图片块只产 1 条媒体框")
        self.assertTrue(win["has_media"] and win["has_other"])
        self.assertEqual(win["other_blocks"][0]["kind"], "image",
                         "该头像本轮消息 = 图片")
        self.assertEqual(win["bot_bottom"], 801,
                         "分析区上沿 = 最后 bot 头像之后的第一条对方头像 top")
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.VisualBackend._refresh")
    @mock.patch("wx_backend.visual_backend.detect_avatar_tops")
    @mock.patch("wx_backend.visual_backend.find_bubble_boxes")
    @mock.patch("wx_backend.visual_backend.find_media_boxes")
    def test_no_other_when_only_bot(self, _media, _bubbles, _tops,
                                    _refresh, _switch):
        """只有 bot 消息（无对方头像）→ has_other=False，other_* 为空。"""
        _refresh.return_value = _solid((747, 1135), (30, 30, 31))
        _tops.side_effect = lambda img, bg, side: [38] if side == "right" else []
        _bubbles.return_value = [(38, 765, 131, 621, True)]
        _media.return_value = []
        b = self._backend()
        win = b.analyze_window("林小满")
        self.assertFalse(win["has_other"])
        self.assertEqual(win["other_text"], [])
        self.assertEqual(win["other_media"], [])
        b.close()


# ---- 真实群聊截图 fixture 集成验证 ----


class TestRealFixtureRegion(unittest.TestCase):
    """region_1x.png（真实群聊截图）+ 真实 OCR 引擎缓存 的集成验证。

    几何检测（头像/气泡/media）在真实像素上真跑，OCR 文本用真实引擎输出
    缓存（ocr_cache.json）——确定性且不引入 ~9s OCR 延迟。
    """

    @classmethod
    def setUpClass(cls):
        if not (os.path.exists(_REGION_1X) and os.path.exists(_OCR_CACHE)):
            raise unittest.SkipTest(
                "fixture 缺失：.rivet/scratch/probe_group/ 下需 region_1x.png "
                "与 ocr_cache.json（tools 探针抓取）")
        import json
        with open(_OCR_CACHE, encoding="utf-8") as f:
            cls.ocr_items = json.load(f)

    def test_get_messages_no_fake_media_text(self):
        """林小满图片块不拆假文字消息：无 sender='我不是'、无图片内文字；
        我方超长气泡 sender='self'。"""
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b.connect()
        img = Image.open(_REGION_1X)
        with mock.patch.object(b, "_switch_chat", return_value=True), \
                mock.patch.object(b, "read_title", return_value="林小满"), \
                mock.patch.object(b, "_refresh", return_value=img), \
                mock.patch("wx_backend.visual_backend.ocr_image",
                           return_value=self.ocr_items):
            msgs = b.get_messages("林小满")
        self.assertEqual(len(msgs), 1,
                         "图片块文字被剔除后只剩我方超长气泡一条")
        self.assertEqual(msgs[0].sender, "self", "我方超长气泡 sender='self'")
        contents = " ".join(m.content for m in msgs)
        for noise in ("我不是", "你这吃白饭的", "蓝色大肥鱼"):
            self.assertNotIn(noise, contents,
                             f"media 框内文字 {noise!r} 不得成为消息")
        b.close()

    def test_analyze_window_media_single(self):
        """图片块只产 1 条媒体消息（other_media 恰 1 框）；对方气泡 1 条。"""
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b.connect()
        img = Image.open(_REGION_1X)
        with mock.patch.object(b, "_switch_chat", return_value=True), \
         mock.patch.object(b, "read_title", return_value="林小满"), \
                mock.patch.object(b, "_refresh", return_value=img):
            win = b.analyze_window("林小满")
        self.assertEqual(win["other_media"], [(838, 1117, 119, 398)],
                         "林小满图片块只产 1 条媒体框")
        self.assertEqual(win["other_text"], [(869, 928, 140, 229)])
        self.assertTrue(win["has_media"] and win["has_other"])
        self.assertEqual(win["bot_bottom"], 801)
        b.close()

if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestDefaultRightHalfRect(unittest.TestCase):
    def test_uses_workarea(self):
        """默认定位取主屏工作区（SPI_GETWORKAREA）：自动扣任务栏，
        自动隐藏任务栏时工作区=全屏 → 窗口打满不留缝。"""
        from wx_backend import visual_backend as vb

        def fake_spi(action, param, rect_ptr, flags):
            import ctypes as _ct
            rect = _ct.cast(rect_ptr, _ct.POINTER(vb.wt.RECT)).contents
            rect.left, rect.top, rect.right, rect.bottom = 0, 0, 1920, 1040
            return True

        with mock.patch.object(vb.u32, "SystemParametersInfoW",
                               side_effect=fake_spi):
            self.assertEqual(vb.default_right_half_rect(), (960, 0, 960, 1040))

    def test_workarea_failure_falls_back_to_screen(self):
        from wx_backend import visual_backend as vb
        with mock.patch.object(vb.u32, "SystemParametersInfoW",
                               return_value=False), \
             mock.patch.object(vb.u32, "GetSystemMetrics",
                               side_effect=[1920, 1080]):
            self.assertEqual(vb.default_right_half_rect(), (960, 0, 960, 1080))

    def test_autohide_taskbar_treated_as_fullscreen(self):
        """自动隐藏任务栏仅留 ~2px 呼出条 → 视为全屏打满（不留细缝）"""
        from wx_backend import visual_backend as vb

        def fake_spi(action, param, rect_ptr, flags):
            import ctypes as _ct
            rect = _ct.cast(rect_ptr, _ct.POINTER(vb.wt.RECT)).contents
            rect.left, rect.top, rect.right, rect.bottom = 0, 0, 1920, 1078
            return True

        with mock.patch.object(vb.u32, "SystemParametersInfoW",
                               side_effect=fake_spi), \
             mock.patch.object(vb.u32, "GetSystemMetrics",
                               side_effect=[1920, 1080]):
            self.assertEqual(vb.default_right_half_rect(), (960, 0, 960, 1080))

    def test_fixed_taskbar_keeps_deduction(self):
        """固定任务栏差值超容差 → 正常扣减"""
        from wx_backend import visual_backend as vb

        def fake_spi(action, param, rect_ptr, flags):
            import ctypes as _ct
            rect = _ct.cast(rect_ptr, _ct.POINTER(vb.wt.RECT)).contents
            rect.left, rect.top, rect.right, rect.bottom = 0, 0, 1920, 1040
            return True

        with mock.patch.object(vb.u32, "SystemParametersInfoW",
                               side_effect=fake_spi), \
             mock.patch.object(vb.u32, "GetSystemMetrics",
                               side_effect=[1920, 1080]):
            self.assertEqual(vb.default_right_half_rect(), (960, 0, 960, 1040))

    def test_position_window_visible_expands_frame_margins(self):
        """可见内容语义定位：自动外扩 DWM 不可见边框——目标可见区
        (1280,0,1280,1600) + 外沿 (10,0,10,10) → 窗口矩形 (1270,0,1300,1610)
        （与用户手动拖到打满的系统行为一致）"""
        from wx_backend import visual_backend as vb
        import ctypes as _ct

        def fake_dwm(hwnd, attr, ptr, size):
            rect = _ct.cast(ptr, _ct.POINTER(vb.wt.RECT)).contents
            rect.left, rect.top, rect.right, rect.bottom = 1272, 0, 2560, 1600
            return 0

        def fake_get_rect(hwnd, ptr):
            rect = _ct.cast(ptr, _ct.POINTER(vb.wt.RECT)).contents
            rect.left, rect.top, rect.right, rect.bottom = 1262, 0, 2570, 1610
            return True

        captured = {}

        def fake_setpos(hwnd, after, x, y, w, h, flags):
            captured["rect"] = (x, y, w, h)
            return True

        with mock.patch.object(vb.dwm, "DwmGetWindowAttribute", side_effect=fake_dwm), \
             mock.patch.object(vb.u32, "GetWindowRect", side_effect=fake_get_rect), \
             mock.patch.object(vb.u32, "SetWindowPos", side_effect=fake_setpos):
            ok = vb.position_window_visible(0x1234, 1280, 0, 1280, 1600)
        self.assertTrue(ok)
        self.assertEqual(captured["rect"], (1270, 0, 1300, 1610))
