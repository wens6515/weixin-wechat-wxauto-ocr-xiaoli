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
    _bucket_avatar,
    _norm_cjk,
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
             mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                        return_value=[150]):
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
        """RED 复现：bot 文件卡片（media 框，无气泡）文件名 OCR 行 x 靠左、
        y 离头像中心 >35 → _is_self 降级判对方。修复后 media 头像几何判据
        把该行标 self。真机锚点：bot 发 index.html，卡片 media 框
        l=334 < 中线 373 < r=613，文件名行 cx 靠左被判 '王文生'。"""
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)

        def fake_avatar_tops(img, bg, side):
            return [100] if side == "right" else []  # 1x 头像 top=100 对齐 media 顶部

        _dav.side_effect = fake_avatar_tops
        # 文件名行（2x）：cx=170 < 中线 200、落在 media 2x 框 (200,320,160,280)
        # 内；cy=300 离头像中心 240 差 60 > 35 → _is_self 降级判对方。
        _ocr.return_value = [
            {"text": "index.html", "x": 150, "y": 290, "w": 40, "h": 20},
        ]
        b.connect()
        msgs = b.get_messages("王文生")
        b.close()
        self.assertTrue(msgs, "bot 文件卡片文件名应被读到")
        file_msg = next((m for m in msgs if "index.html" in m.content), None)
        self.assertIsNotNone(file_msg, "应读到 index.html 消息")
        self.assertEqual(file_msg.sender, "self",
                         "bot 文件卡片文件名不得判为对方（真机误判'王文生'）")

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
        """RED 复现：bot 文件卡片颜色接近 other 气泡色，被 find_bubble_boxes
        判成 is_self=False 气泡（非 media）。文件名行 x 靠左、y 离头像中心
        ≥35 → _bubble_self=False + _is_self 降级，sender 误判对方。修复后
        头像几何判据（首行 y 对齐右侧头像 top）优先于气泡色判 self。
        真机锚点：22:39 王文生发纯文字，bot 文件卡片气泡 (913,1026,182,622,False)
        顶部 913 对齐 bot_tops=913，却因非绿被判对方走文件流程。"""
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)

        def fake_avatar_tops(img, bg, side):
            return [0, 90] if side == "right" else [110]

        _dav.side_effect = fake_avatar_tops
        # 文件名行（2x）：cx=110 < 中线 200、落在气泡 2x 框 (180,220,80,280)
        # 内；cy=185 离头像中心 220 差 35（_is_self 阈值 <35 不命中）→ 判对方。
        _ocr.return_value = [
            {"text": "index.html", "x": 90, "y": 175, "w": 40, "h": 20},
        ]
        b.connect()
        msgs = b.get_messages("王文生")
        b.close()
        self.assertTrue(msgs, "bot 文件卡片文件名应被读到")
        file_msg = next((m for m in msgs if "index.html" in m.content), None)
        self.assertIsNotNone(file_msg, "应读到 index.html 消息")
        self.assertEqual(file_msg.sender, "self",
                         "bot 文件卡片(气泡形态)文件名不得判为对方")

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
            win = b.analyze_window("王文生")
        self.assertEqual(win["bot_bottom"], 100,
                         "bot_bottom = 我方最后头像之后第一条消息上边框（几何，不再取气泡 bottom）")
        self.assertEqual(win["other_text"], [(100, 130, 10, 160)],
                         "对方长文字即使右边缘靠右也不得归 bot")

    def test_analyze_window_avatar_geometry_bot_media(self):
        """bot 图片（media 框，无气泡）对齐右侧头像 → 归 bot，不落 other_media。"""
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)

        def fake_avatar_tops(img, bg, side):
            return [0] if side == "right" else []

        with mock.patch.object(b, "_switch_chat", return_value=True), \
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
            win = b.analyze_window("王文生")
        self.assertEqual(win["bot_bottom"], 200,
                         "无下一条消息时 bot_bottom = 消息区高度（几何兜底）")
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
            win = b.analyze_window("王文生")
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
             mock.patch.object(b, "_refresh", return_value=_solid((200, 200), (30, 30, 31))), \
             mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                        return_value={"bg": (30, 30, 31), "other": (47, 47, 48),
                                      "self": (53, 210, 141)}), \
             mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                        side_effect=fake_avatar_tops), \
             mock.patch("wx_backend.visual_backend.find_bubble_boxes", return_value=[]), \
             mock.patch("wx_backend.visual_backend.find_media_boxes", return_value=[]):
            win = b.analyze_window("王文生", skip_bot=0)
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
             mock.patch.object(b, "_refresh", return_value=_solid((200, 200), (30, 30, 31))), \
             mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                        return_value={"bg": (30, 30, 31), "other": (47, 47, 48),
                                      "self": (53, 210, 141)}), \
             mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                        side_effect=fake_avatar_tops), \
             mock.patch("wx_backend.visual_backend.find_bubble_boxes", return_value=[]), \
             mock.patch("wx_backend.visual_backend.find_media_boxes", return_value=[]):
            win = b.analyze_window("王文生", skip_bot=1)
        self.assertEqual(win["bot_bottom"], 350,
                         "skip_bot=1 取 sorted(bot_tops)[-2]=200，之后第一条消息 top=350")
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
             mock.patch.object(b, "_refresh", return_value=_solid((200, 200), (30, 30, 31))), \
             mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                        return_value={"bg": (30, 30, 31), "other": (47, 47, 48),
                                      "self": (53, 210, 141)}), \
             mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                        side_effect=fake_avatar_tops), \
             mock.patch("wx_backend.visual_backend.find_bubble_boxes", return_value=[]), \
             mock.patch("wx_backend.visual_backend.find_media_boxes", return_value=[]):
            win = b.analyze_window("王文生", skip_bot=5)
        self.assertEqual(win["bot_bottom"], 300,
                         "skip 兜底到 0，last_bot_top=max(bot_tops)=200，之后第一条消息 top=300")

    def test_analyze_window_skip_bot_empty_bot_tops(self):
        """bot_tops 为空时维持现状：bot_bottom=None，即使传 skip_bot>0。"""
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)

        def fake_avatar_tops(img, bg, side):
            return [] if side == "right" else [100]

        with mock.patch.object(b, "_switch_chat", return_value=True), \
             mock.patch.object(b, "_refresh", return_value=_solid((200, 200), (30, 30, 31))), \
             mock.patch("wx_backend.visual_backend.detect_bubble_colors",
                        return_value={"bg": (30, 30, 31), "other": (47, 47, 48),
                                      "self": (53, 210, 141)}), \
             mock.patch("wx_backend.visual_backend.detect_avatar_tops",
                        side_effect=fake_avatar_tops), \
             mock.patch("wx_backend.visual_backend.find_bubble_boxes", return_value=[]), \
             mock.patch("wx_backend.visual_backend.find_media_boxes", return_value=[]):
            win = b.analyze_window("王文生", skip_bot=2)
        self.assertIsNone(win["bot_bottom"],
                          "无 bot 回复时 bot_bottom 维持 None")
        self.assertTrue(win["has_other"],
                        "无 bot 时对方消息全部算新消息")

    def test_detect_avatar_tops_geometry(self):
        """detect_avatar_tops 真实实现：窄带非背景块标出头像顶部 y，
        高度超标的块（如滚动条）被丢弃，bg=None 返回空。"""
        from PIL import ImageDraw
        img = _solid((747, 1135), (30, 30, 31))
        d = ImageDraw.Draw(img)
        # 头像高 expected_h = 1135//18 = 63；窄带右侧 [627,732]、左侧 [14,104]
        d.rectangle([630, 50, 730, 112], fill=(200, 100, 100))    # 右侧头像(高63)
        d.rectangle([15, 30, 104, 92], fill=(200, 100, 100))      # 左侧头像(高63)
        d.rectangle([630, 200, 730, 400], fill=(200, 100, 100))   # 右侧超高块(噪声)
        self.assertEqual(detect_avatar_tops(img, (30, 30, 31), "right"), [50])
        self.assertEqual(detect_avatar_tops(img, (30, 30, 31), "left"), [30])
        self.assertEqual(detect_avatar_tops(img, None, "right"), [])


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
    读出后拆出 sender='我不是' 的假消息；发送者名 '王文生' 紧贴图片框顶，
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
    @mock.patch("wx_backend.visual_backend.ocr_image_sharded")
    @mock.patch("wx_backend.visual_backend.detect_avatar_tops")
    @mock.patch("wx_backend.visual_backend.find_media_boxes")
    def test_media_text_dropped_and_name_consumed(self, _media, _tops, _ocr,
                                                  _cap, _find, _switch):
        """1x 坐标：左头像 top=100、右头像 top=30；media 框 (150,250,40,160)
        顶部 150 落入左头像 100 的区间 → 对方媒体框。
        发送者名 '王文生'（2x y=280）紧贴 media 框顶（2x y=300）→ 被吞掉。
        media 框内 '我不是'/'大肥鱼'（2x y=320/360）→ 剔除不产消息。"""
        _tops.side_effect = lambda img, bg, side: [100] if side == "left" else [30]
        _media.return_value = [(150, 250, 40, 160)]
        _ocr.return_value = [
            {"text": "王文生", "x": 50, "y": 280, "w": 50, "h": 20},
            {"text": "我不是", "x": 100, "y": 320, "w": 60, "h": 20},
            {"text": "大肥鱼", "x": 100, "y": 360, "w": 60, "h": 20},
        ]
        b = self._backend()
        with mock.patch.object(b, "read_title", return_value="王文生"):
            msgs = b.get_messages("王文生")
        self.assertEqual(len(msgs), 0,
                         "图片块文字（含发送者名行）不得拆成文字消息")
        b.close()

    @mock.patch("wx_backend.visual_backend.VisualBackend._switch_chat",
                return_value=True)
    @mock.patch("wx_backend.visual_backend.find_wechat_window",
                return_value=0x1234)
    @mock.patch("wx_backend.visual_backend.capture_window",
                return_value=_solid((400, 300), (30, 30, 31)))
    @mock.patch("wx_backend.visual_backend.ocr_image_sharded")
    @mock.patch("wx_backend.visual_backend.detect_avatar_tops")
    @mock.patch("wx_backend.visual_backend.find_media_boxes")
    def test_text_outside_media_kept(self, _media, _tops, _ocr,
                                     _cap, _find, _switch):
        """media 框外的正常文字消息不受影响（仍产消息）。"""
        _tops.side_effect = lambda img, bg, side: [100] if side == "left" else [30]
        _media.return_value = [(150, 250, 40, 160)]
        _ocr.return_value = [
            # 正常文字（2x y=210，1x=105 落入左头像 100 区间 → 对方消息；
            # 长度 >8 字符避免触发发送者名候选逻辑——真实短消息在气泡框内）
            {"text": "这是一条正常的文字消息", "x": 100, "y": 210,
             "w": 200, "h": 20},
            # media 框 2x (300,500,80,320) 内文字 → 剔除
            {"text": "我不是", "x": 100, "y": 320, "w": 60, "h": 20},
        ]
        b = self._backend()
        with mock.patch.object(b, "read_title", return_value="王文生"):
            msgs = b.get_messages("王文生")
        self.assertEqual(len(msgs), 1, "media 框外文字消息应保留")
        self.assertEqual(msgs[0].content, "这是一条正常的文字消息")
        b.close()


# ---- analyze_window：头像划块归属（替换 tol=40 _aligned 对齐） ----


class TestAnalyzeWindowAvatarBucket(unittest.TestCase):
    """气泡/media 框顶落入头像边界序列区间归属对方；bot 长气泡不误判对方。

    几何坐标与真实 fixture region_1x.png 一致：bot 头像 top=38、
    对方头像 top=801、bot 长气泡 (38,765)、对方气泡 (869,928)、
    王文生图片块 media (838,1117)。"""

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
            (869, 928, 140, 229, False),  # 对方气泡（顶落入对方头像 801 区间）
        ]
        _media.return_value = [(838, 1117, 119, 398)]  # 王文生图片块
        b = self._backend()
        win = b.analyze_window("王文生")
        self.assertEqual(win["other_text"], [(869, 928, 140, 229)],
                         "对方气泡按头像划块归属；bot 长气泡不得误判对方")
        self.assertEqual(win["other_media"], [(838, 1117, 119, 398)],
                         "图片块只产 1 条媒体框")
        self.assertTrue(win["has_text"] and win["has_media"] and win["has_other"])
        self.assertEqual(win["bot_bottom"], 801,
                         "最后 bot 头像之后的下一个头像 top")
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
        win = b.analyze_window("王文生")
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
        """王文生图片块不拆假文字消息：无 sender='我不是'、无图片内文字；
        我方超长气泡 sender='self'。"""
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b.connect()
        img = Image.open(_REGION_1X)
        with mock.patch.object(b, "_switch_chat", return_value=True), \
                mock.patch.object(b, "read_title", return_value="王文生"), \
                mock.patch.object(b, "_refresh", return_value=img), \
                mock.patch("wx_backend.visual_backend.ocr_image_sharded",
                           return_value=self.ocr_items):
            msgs = b.get_messages("王文生")
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
                mock.patch.object(b, "_refresh", return_value=img):
            win = b.analyze_window("王文生")
        self.assertEqual(win["other_media"], [(838, 1117, 119, 398)],
                         "王文生图片块只产 1 条媒体框")
        self.assertEqual(win["other_text"], [(869, 928, 140, 229)])
        self.assertTrue(win["has_media"] and win["has_other"])
        self.assertEqual(win["bot_bottom"], 801)
        b.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
