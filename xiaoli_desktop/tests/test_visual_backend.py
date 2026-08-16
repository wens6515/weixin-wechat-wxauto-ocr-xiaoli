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
    detect_bubble_colors,
    find_bubble_boxes,
    find_media_boxes,
    ensure_window_visible,
    find_window_by_title,
    window_rect,
)
from wx_backend.models import MessageType


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

    @mock.patch("wx_backend.visual_backend._get_ocr_engine")
    def test_rapidocr_maps_to_dict_contract(self, _m):
        """RapidOCR 返回 [box, text, score] → ocr_image 输出 {text,x,y,w,h}。"""
        class _FakeEngine:
            def __call__(self, img):
                return [
                    ([[10, 20], [100, 20], [100, 50], [10, 50]], "王文生", 0.99),
                    ([[5, 80], [60, 80], [60, 100], [5, 100]], "[图片]", 0.95),
                ], [0.1, 0.1, 0.1]
        _m.return_value = _FakeEngine()
        img = _solid((200, 200), (255, 255, 255))
        items = ocr_image(img)
        self.assertEqual(items, [
            {"text": "王文生", "x": 10, "y": 20, "w": 90, "h": 30},
            {"text": "[图片]", "x": 5, "y": 80, "w": 55, "h": 20},
        ])


# ---- 气泡色探测 + 连通域分气泡 ----


def _dark_msg_region():
    """模拟深色主题消息区：深黑背景 + 深灰对方气泡 + 绿色自己气泡。"""
    from PIL import ImageDraw
    img = Image.new("RGB", (400, 300), (30, 30, 31))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([40, 30, 240, 110], radius=8, fill=(47, 47, 48))      # 对方气泡
    d.rounded_rectangle([160, 150, 360, 210], radius=8, fill=(53, 210, 141))  # 自己气泡
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
                    # 真实微信消息块间距 ≥150px（真机内容-内容最小 186px），
                    # 短消息不会与下一条消息紧贴——避免被误判为发送者名
                    {"text": "今天天气不错", "x": 100, "y": 250, "w": 90, "h": 20},
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
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    # 同一气泡多行（行距 44px，2x 后微信气泡内换行）→ 应合并为一条
                    {"text": "@小漓 测试，生成10秒视频：镜头1：缓慢推镜，昏暗",
                     "x": 100, "y": 100, "w": 300, "h": 22},
                    {"text": "镜头2：微微特写少年侧脸，眼神平静",
                     "x": 100, "y": 144, "w": 260, "h": 22},
                    {"text": "镜头3：镜头缓缓拉远，整个安静的房间",
                     "x": 100, "y": 188, "w": 280, "h": 22},
                ])
    def test_get_messages_merges_multiline_bubble(self, _ocr, _cap, _find,
                                                 _switch):
        """同一气泡内多行换行（行距小）应合并为一条消息，不得拆散。

        RED 复现：真机长任务指令（含镜头1/2/3 多行）被拆成 12 条独立消息，
        只有带 @ 前缀的第一行成为任务，其余行被 [跳过]——task.json raw_message
        只剩「测试，生成10秒视频」。根因：y 阈值 18px 小于气泡内换行距，
        且续行无 append 分支被静默丢弃。
        """
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b.connect()
        msgs = b.get_messages("王文生")
        self.assertEqual(len(msgs), 1, "同一气泡多行应合并为一条消息")
        self.assertIn("镜头2", msgs[0].content)
        self.assertIn("镜头3", msgs[0].content)
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window")
    @mock.patch("wx_backend.visual_backend.ocr_image")
    def test_get_messages_groups_by_bubble(self, _ocr, _cap, _find, _switch):
        """连通域分气泡：同一气泡内多行合并、不同气泡分开，优先于 y 阈值。

        RED 方向：y 阈值靠猜行距，换主题/字体就失效。连通域用气泡背景色
        （绿色=自己、深灰/白=对方）直接框出气泡边界，sender 判定也不再依赖
        x 中线。
        """
        _cap.return_value = _dark_msg_region()  # 400x300 深色图（对方+自己气泡）
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b.connect()
        with mock.patch.object(b, "read_title", return_value="王文生"):
            # OCR 文字（2x 坐标）：对方气泡 2x [80,60,480,220]、
            # 自己气泡 2x [320,300,720,420]
            _ocr.return_value = [
                {"text": "镜头1：缓慢推镜", "x": 100, "y": 80, "w": 200, "h": 30},
                {"text": "镜头2：特写侧脸", "x": 100, "y": 140, "w": 200, "h": 30},
                {"text": "收到任务啦", "x": 340, "y": 320, "w": 200, "h": 30},
            ]
            msgs = b.get_messages("王文生")
        self.assertEqual(len(msgs), 2, "应分组成 2 条（对方气泡 + 自己气泡）")
        self.assertIn("镜头1", msgs[0].content)
        self.assertIn("镜头2", msgs[0].content)
        self.assertEqual(msgs[0].sender, "王文生", "对方气泡 → 私聊 sender=会话名")
        self.assertEqual(msgs[1].content, "收到任务啦")
        self.assertEqual(msgs[1].sender, "self", "绿色气泡 → self")
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window")
    @mock.patch("wx_backend.visual_backend.ocr_image")
    def test_get_messages_excludes_avatar_text(self, _ocr, _cap, _find, _switch):
        """头像区域内的 OCR 文字（头像图片上的字）应被排除，不当消息内容。"""
        _cap.return_value = _dark_msg_region()  # 深色图（对方+自己气泡）
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b.connect()
        with mock.patch.object(b, "read_title", return_value="王文生"), \
             mock.patch.object(b, "_detect_self_avatar_boxes",
                               return_value=[(700, 300, 80, 80)]):
            _ocr.return_value = [
                # 气泡内容（对方气泡 2x [80,60,480,220] 内）
                {"text": "镜头1：缓慢推镜", "x": 100, "y": 80, "w": 200, "h": 30},
                # 头像文字（落在自己头像矩形 (700,300,80,80) 内）
                {"text": "蓝色大肥鱼", "x": 710, "y": 310, "w": 60, "h": 20},
            ]
            msgs = b.get_messages("王文生")
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
        self.assertEqual(msgs[0].sender, "王文生")   # 左侧 → 对方（私聊发送人=会话名）
        self.assertEqual(msgs[1].content, "我回复的")
        self.assertEqual(msgs[1].sender, "self")   # 右侧 → 自己
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    # 用户真实消息（左侧）→ 应保留
                    {"text": "你好", "x": 100, "y": 60, "w": 30, "h": 20},
                    # 输入框"发送"按钮（消息区右下角，x/y 均贴近边缘）
                    # → 会被 OCR 读进来且 x 靠右判成 self，顶掉真实最新消息
                    {"text": "发送", "x": 370, "y": 380, "w": 25, "h": 15},
                ])
    def test_get_messages_filters_input_box_send_button(self, _ocr, _cap, _find,
                                                        _switch):
        """输入框"发送"按钮（右下角固定位置）不应成为消息。

        RED 复现：真机日志出现 latest sender='self' content='发送'——OCR 把
        输入框发送按钮读成消息且 x 靠右判 self，process_new_messages 直接跳过
        整个会话，用户真实消息永远不被处理。
        """
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)  # 全窗，与配置解耦、确定性
        b.connect()
        msgs = b.get_messages("王文生")
        self.assertEqual(len(msgs), 1, "发送按钮应被过滤，只剩用户真实消息")
        self.assertEqual(msgs[0].content, "你好")
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    # 左侧消息（x < 中线）→ 私聊发送人 = 会话名
                    {"text": "你好", "x": 100, "y": 50, "w": 30, "h": 20},
                ])
    def test_get_messages_private_chat_sender_is_chat_name(self, _ocr, _cap,
                                                           _find, _switch):
        """私聊（非群聊）时消息区左侧的发送人就是会话名本身。

        RED 复现：真机日志 [最新消息] sender='未知'——私聊王文生会话里
        左侧消息的 sender 硬编码"未知"，上层拿不到发送人。私聊场景
        sender 应为 chat（会话名=发送人），群聊才是消息区气泡名。
        """
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b.connect()
        msgs = b.get_messages("王文生")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].sender, "王文生",
                         "私聊左侧消息 sender 应为会话名，而非'未知'")
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window", return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    # 群聊发送者名：短文本独立行，紧贴内容上方（y 差 ~100）
                    {"text": "哆拉A萝", "x": 100, "y": 100, "w": 80, "h": 24},
                    # 消息内容
                    {"text": "豆包有学生优惠了", "x": 140, "y": 206,
                     "w": 150, "h": 24},
                ])
    def test_get_messages_group_chat_sender_is_author(self, _ocr, _cap,
                                                      _find, _switch):
        """群聊时消息区气泡上方有发送者名（短文本行紧贴内容），
        sender 应为发送者名，且名字行本身不得成为一条消息。

        RED 复现：真机读「强盗”集团」群聊，OCR 读到 '哆拉A萝'（发送者）
        与 '豆包有学生优惠了'（内容）两条独立项——当前实现把名字行
        当成独立消息（sender=群名），上层拿不到发送者。
        真机 y 差：名字-内容 106px，内容块间最小 186px → 可区分。
        """
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b.connect()
        msgs = b.get_messages("强盗”集团")
        self.assertEqual(len(msgs), 1, "发送者名行不应成为独立消息")
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
                               side_effect=[None, "王文生"]) as _rt:
            b.get_messages("王文生")
        self.assertEqual(_rt.call_count, 2, "read_title None 后应重试一次")
        force_calls = [c for c in _switch.call_args_list
                       if c == mock.call("王文生", force=True)]
        self.assertTrue(force_calls, "None 后应 force 重切会话恢复选中")
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

        RED 复现：真机日志 🤖→[王文生] 显示正常回复，但微信输入框实际
        只出现'（）'——pyautogui.typewrite 逐键模拟对非 ASCII（中文）
        无法映射键位，按键序列被中文输入法拦截成括号。
        """
        b = VisualBackend()
        b.connect()
        self.assertTrue(b.send_text("王文生", "你好呀"))
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
        在窗口顶部的标题）不应被误当会话列表条目——真机实测：置顶王文生时
        顶部标题 x=543（0.418w）被 +0.17 容差纳入，导致红圈 y 差 66>60 匹配失败。"""
        b = VisualBackend()
        b._hwnd = 0x1234
        b._session_region = (0.09, 0.0855, 0.4165, 0.993)  # 固定 region[2]=0.4165
        shot = _solid((1300, 1610), (255, 255, 255))
        with mock.patch("wx_backend.visual_backend.ocr_image", return_value=[
            # 顶部标题：x=543 = 0.4177w，在 region[2]=0.4165w 之外、+0.17 之内
            {"text": "干立牛", "x": 543, "y": 85, "w": 71, "h": 20},
            # 列表条目：x=217 = 0.167w，正常会话名
            {"text": "强盗集团", "x": 217, "y": 293, "w": 131, "h": 20},
        ]):
            coords = b._extract_session_names(shot)
        self.assertNotIn("干立牛", coords)
        self.assertIn("强盗集团", coords)

    def test_extract_session_names(self):
        """会话名提取：整窗 OCR 聚类出会话名 + 坐标（预览类型已移除）。"""
        b = VisualBackend()
        b._hwnd = 0x1234
        b._session_region = (0.09, 0.0855, 0.4165, 0.993)
        shot = _solid((1300, 1610), (255, 255, 255))
        with mock.patch("wx_backend.visual_backend.ocr_image", return_value=[
            {"text": "王文生", "x": 214, "y": 163, "w": 73, "h": 20},
            {"text": "[文件] 部门简介.docx", "x": 220, "y": 203, "w": 291, "h": 20},
            {"text": "杨冬梅", "x": 215, "y": 393, "w": 73, "h": 20},
            {"text": "[图片]", "x": 217, "y": 430, "w": 52, "h": 20},
            {"text": "王美晨", "x": 216, "y": 506, "w": 73, "h": 20},
        ]):
            coords = b._extract_session_names(shot)
        self.assertIn("王文生", coords)
        self.assertIn("杨冬梅", coords)
        self.assertIn("王美晨", coords)

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
                    (0, 490, 262, 1226, True),
                    # other 气泡误检在右侧：left=1220 > 中线 747（other 色
                    # 接近背景时 find_bubble_boxes 把右侧背景误连成 other 框）
                    (0, 490, 1220, 1492, False),
                ])
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    {"text": "18:47", "x": 679, "y": 1989, "w": 40, "h": 15},
                    {"text": "你好", "x": 276, "y": 2144, "w": 30, "h": 20},
                ])
    def test_get_messages_rightside_other_bubble_not_avatar(self, _ocr, _fb,
                                                            _dc, _cap, _find,
                                                            _switch):
        """RED 复现：other 气泡框被误检在右侧（left>中线）时，头像排除
        不得把整个消息区当头像区——否则所有消息被丢弃，get_messages 读空
        （真机根因：19:10 王文生已选中读 0 条，OCR 17 行全被 _in_avatar
        丢弃，other_avatar_x_max 被右侧误检框污染成 1220）。"""
        b = VisualBackend()
        b.connect()
        msgs = b.get_messages("王文生")
        self.assertTrue(msgs, "右侧误检 other 框不得导致消息全被头像区排除")
        self.assertTrue(any("你好" in m.content for m in msgs),
                        "「你好」应被读到（不被误判头像区）")
        b.close()

    def test_analyze_window_rightside_other_bubble_is_bot(self):
        """RED 复现：bot 回传的成果文件卡片无绿色气泡、被判 other 气泡，
        但右对齐（r>0.75w）→ 应归 bot 消息，不得落入窗口内对方消息
        （否则 bot 回传的 html 成果被误当王文生新发的文件，见真机日志
        13:03 '判断为文件消息：就业服务部_赛博朋克宣传页.html'）。"""
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        with mock.patch.object(b, "_switch_chat", return_value=True), \
             mock.patch.object(b, "_refresh", return_value=_solid((200, 200), (30, 30, 31))), \
             mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                        return_value={"bg": (30, 30, 31), "other": (47, 47, 48),
                                      "self": (53, 210, 141)}), \
             mock.patch("wx_backend.visual_backend.find_bubble_boxes",
                        return_value=[
                            (0, 40, 20, 160, True),     # bot 绿气泡
                            (50, 80, 30, 160, False),   # bot 文件卡片(右对齐 r=160>150)
                            (100, 130, 10, 80, False),  # 对方消息(左对齐 r=80<150)
                        ]), \
             mock.patch("wx_backend.visual_backend.find_media_boxes",
                        return_value=[]):
            win = b.analyze_window("王文生")
        self.assertEqual(win["bot_bottom"], 80,
                         "bot 文件卡片(右对齐 other)下边界应计入 bot_bottom")
        self.assertEqual(win["other_text"], [(100, 130, 10, 80)],
                         "右对齐文件卡片不得落入窗口内对方消息")


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

    def test_parse_title_group_has_member_count(self):
        """标题带括号人数 = 群聊（视觉权威信号，替代名称启发式）。"""
        from wx_backend.visual_backend import parse_title
        self.assertEqual(parse_title('强盗"集团(5)'), ("强盗\"集团", True, 5))
        self.assertEqual(parse_title("王文生"), ("王文生", False, None))
        self.assertEqual(parse_title(""), ("", False, None))
        self.assertEqual(parse_title(None), ("", False, None))

    @mock.patch("wx_backend.visual_backend.VisualBackend._refresh",
                return_value=_solid((200, 200), (255, 255, 255)))
    @mock.patch("wx_backend.visual_backend.ocr_image",
                return_value=[
                    {"text": "X", "x": 300, "y": 10, "w": 10, "h": 10},
                    {"text": '"强盗"', "x": 100, "y": 10, "w": 80, "h": 20},
                    {"text": "集团(5)", "x": 185, "y": 10, "w": 60, "h": 20},
                ])
    def test_read_title_joins_split_segments(self, _ocr, _refresh):
        """标题被 OCR 拆成多段时按 x 拼接（真机：'"强盗"' + '集团(5)'）。

        RED 复现：真机 read_title 只读到'集团(5)'——OCR 把带引号的
        '强盗'与'集团(5)'分成两个独立项，旧实现只取最长单行导致标题缺左半。
        """
        b = self._backend()
        b._title_region = (0.0, 0.0, 1.0, 1.0)
        self.assertEqual(b.read_title(), '"强盗"集团(5)')

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
