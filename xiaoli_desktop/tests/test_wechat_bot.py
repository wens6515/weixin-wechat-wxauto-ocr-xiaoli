# -*- coding: utf-8 -*-
"""wechat_bot 基础能力测试：群聊判定集中点、去重集合限界、微信连接重试可停止、
模型端点拼接、图片发送前压缩。"""
import os
import sys
import threading
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_bot import WeChatBot, is_group_chat, models_endpoint


class TestIsGroupChat(unittest.TestCase):
    def test_group_by_name(self):
        self.assertTrue(is_group_chat("产品讨论群"))
        self.assertTrue(is_group_chat("XX集团内部"))

    def test_private_chat(self):
        self.assertFalse(is_group_chat("小明"))
        self.assertFalse(is_group_chat("Alice"))

    def test_edge_inputs(self):
        self.assertFalse(is_group_chat(""))
        self.assertFalse(is_group_chat(None))

    def test_group_by_title_with_member_count(self):
        """标题带括号人数 = 群聊（视觉后端权威信号，替代名称启发式）。

        普通群名（如'哆菈A夢'）不含'群/集团'字，名称启发式会漏判；
        title 参数来自右侧会话标题（'强盗"集团(5)'），可靠。
        """
        self.assertTrue(is_group_chat("任意名", '强盗"集团(5)'))
        self.assertFalse(is_group_chat("任意名", "王文生"))
        self.assertFalse(is_group_chat("任意名", ""))
        # title 缺省时回退名称启发式（兼容旧路径/单测）
        self.assertTrue(is_group_chat("产品讨论群"))
        self.assertFalse(is_group_chat("小明"))


class TestRememberRecent(unittest.TestCase):
    def _make(self):
        bot = WeChatBot.__new__(WeChatBot)
        bot.recent_msg_ids = set()
        bot._RECENT_MAX = 10
        return bot

    def test_dedup_no_growth(self):
        bot = self._make()
        bot._remember_recent("x")
        bot._remember_recent("x")
        self.assertEqual(len(bot.recent_msg_ids), 1)
        self.assertEqual(len(bot._recent_order), 1)

    def test_caps_drops_oldest(self):
        """RED 复现：recent_msg_ids 只增不减会无限膨胀。
        超限（_RECENT_MAX=10）后每次丢弃最旧一半，保留最近消息。"""
        bot = self._make()
        for i in range(20):
            bot._remember_recent(f"k{i}")
        self.assertEqual(len(bot.recent_msg_ids), 10, "集合必须被限界")
        self.assertIn("k19", bot.recent_msg_ids, "最新消息必须保留")
        self.assertNotIn("k0", bot.recent_msg_ids, "最旧消息应被丢弃")
        self.assertNotIn("k9", bot.recent_msg_ids)

    def test_within_cap_kept_all(self):
        bot = self._make()
        for i in range(8):
            bot._remember_recent(f"k{i}")
        self.assertEqual(len(bot.recent_msg_ids), 8)


class TestConnectWx(unittest.TestCase):
    def _make(self, stop_event=None, max_retries=None, interval=0):
        bot = WeChatBot.__new__(WeChatBot)
        bot._stop_event = stop_event
        bot._max_connect_retries = max_retries
        bot._connect_retry_interval = interval
        return bot

    def test_stop_event_aborts(self):
        """RED 复现：微信未开时 _connect_wx 无限重试，GUI 无法取消初始化。
        引擎 stop_event 已设置 → 立即抛异常（可被 _do_initialize 捕获 → error）。"""
        evt = threading.Event()
        evt.set()
        bot = self._make(stop_event=evt)
        with self.assertRaises(RuntimeError):
            bot._connect_wx()

    def test_retry_limit_exhausted(self):
        """重试达上限（GUI 场景）→ 抛异常，不再无限循环。"""
        import wechat_bot as wb
        orig = wb.create_backend
        wb.create_backend = lambda **k: (_ for _ in ()).throw(RuntimeError("no wechat"))
        try:
            bot = self._make(max_retries=1, interval=0)
            with self.assertRaises(RuntimeError):
                bot._connect_wx()
        finally:
            wb.create_backend = orig

    def test_no_limit_keeps_retrying(self):
        """CLI 模式（不传上限）连接失败后继续重试——观察若干次调用确认不退出。"""
        import time
        import wechat_bot as wb
        orig = wb.create_backend
        calls = {"n": 0}
        stop = threading.Event()

        def fake_connect(*a, **k):
            calls["n"] += 1
            raise RuntimeError("no wechat")

        wb.create_backend = fake_connect
        try:
            bot = self._make(max_retries=None, interval=0)
            bot._stop_event = stop  # 测试用停止信号（业务上 CLI 模式不设）
            t = threading.Thread(target=bot._connect_wx, daemon=True)
            t.start()
            time.sleep(0.3)  # 给几次重试机会
            self.assertGreaterEqual(calls["n"], 2, "无限重试模式应持续重试")
            self.assertTrue(t.is_alive(), "重试循环应持续运行（未达上限不退出）")
            stop.set()  # 停止信号 → 线程退出（防测试线程泄漏）
            t.join(2)
            self.assertFalse(t.is_alive(), "停止信号应能终止重试循环")
        finally:
            wb.create_backend = orig


class TestModelsEndpoint(unittest.TestCase):
    def test_standard_url(self):
        self.assertEqual(
            models_endpoint("https://api.deepseek.com/v1/chat/completions"),
            "https://api.deepseek.com/v1/models")

    def test_no_chat_completions_unchanged(self):
        # 自定义端点不含该子串 → 原样返回（保持旧行为）
        self.assertEqual(models_endpoint("https://x.example/v1/chat"),
                         "https://x.example/v1/chat")

    def test_only_last_occurrence_replaced(self):
        # rsplit 只替换最后一处（str.replace 会替换所有出现处——拼接错误）
        self.assertEqual(
            models_endpoint("https://x/chat/completions/chat/completions"),
            "https://x/chat/completions/models")

    def test_empty(self):
        self.assertEqual(models_endpoint(""), "")
        self.assertIsNone(models_endpoint(None))


class TestImageCompress(unittest.TestCase):
    def _make(self):
        return WeChatBot.__new__(WeChatBot)

    def test_large_screenshot_scaled_and_jpeg(self):
        """RED 复现：4K 屏幕截图 base64 直发撑爆视觉 API 体积上限。
        压缩后最长边 ≤ MAX_IMAGE_EDGE 且为 JPEG。"""
        from PIL import Image
        bot = self._make()
        img = Image.new("RGB", (4000, 3000), "red")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "shot.jpg")
            size = bot._save_screenshot_compressed(img, p)
            self.assertGreater(size, 0, "压缩产物必须落盘")
            with Image.open(p) as out:
                self.assertLessEqual(max(out.size), bot.MAX_IMAGE_EDGE,
                                     "最长边必须缩放到上限内")
                self.assertEqual(out.format, "JPEG")

    def test_small_image_kept_size(self):
        from PIL import Image
        bot = self._make()
        img = Image.new("RGB", (800, 600), "blue")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "small.jpg")
            bot._save_screenshot_compressed(img, p)
            with Image.open(p) as out:
                self.assertEqual(out.size, (800, 600), "小图不应被放大")

    def test_pil_failure_falls_back_png(self):
        """PIL 不可用时退回原样保存（图片处理链路不中断）。"""
        bot = self._make()
        class FakeImage:
            """无 convert/resize 的伪截图对象（模拟 PIL 缺失/异常场景）。"""

            def convert(self, *a):
                raise ImportError("no PIL")

            def save(self, path, **kw):
                with open(path, "wb") as f:
                    f.write(b"png-fallback")

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "shot.jpg")
            size = bot._save_screenshot_compressed(FakeImage(), p)
            self.assertGreater(size, 0, "退回保存也必须落盘")
            with open(p, "rb") as f:
                self.assertEqual(f.read(), b"png-fallback")


class TestProcessNewMessagesUnreadDrive(unittest.TestCase):
    """process_new_messages 的会话获取分支：visual 后端走 iter_unread_sessions
    （红圈驱动），旧后端降级走 iter_sessions（行为不变）。"""

    def _make(self, wx):
        bot = WeChatBot.__new__(WeChatBot)
        bot.paused = False
        bot.last_reply_time = 0.0
        bot.cooldown = 0.0
        bot.wx = wx
        bot.recent_msg_ids = set()
        bot.processed_ids = set()
        bot.nickname = "小漓"
        return bot

    def test_uses_unread_when_available(self):
        """后端有 iter_unread_sessions → 只走红圈驱动，不遍历全量会话。"""
        calls = {"unread": 0, "sessions": 0}

        class FakeWx:
            def iter_unread_sessions(self):
                calls["unread"] += 1
                return iter(["王文生"])

            def iter_sessions(self):
                calls["sessions"] += 1
                return iter(["王文生", "杨冬梅"])

            def get_messages(self, chat):
                return []  # 无消息 → 不触发后续处理

        bot = self._make(FakeWx())
        bot.process_new_messages()
        self.assertEqual(calls["unread"], 1, "应调用 iter_unread_sessions")
        self.assertEqual(calls["sessions"], 0, "有 unread 能力时不应遍历全量")

    def test_falls_back_to_iter_sessions(self):
        """后端无 iter_unread_sessions（wxauto 等）→ 降级走 iter_sessions。"""
        calls = {"sessions": 0}

        class FakeWx:
            def iter_sessions(self):
                calls["sessions"] += 1
                return iter(["王文生"])

            def get_messages(self, chat):
                return []

        bot = self._make(FakeWx())
        bot.process_new_messages()
        self.assertEqual(calls["sessions"], 1, "无 unread 能力时应降级 iter_sessions")

    def test_latest_takes_last_foreign_when_last_is_self_noise(self):
        """输入框"发送"按钮被 OCR 读成 self 消息且排最下时，
        latest 应取最后一条非 self 消息（用户真实消息），不得跳过。

        RED 复现：真机日志 [最新消息] sender='self' content='发送' 后
        [跳过] 是自己或空——用户发的消息被输入框按钮噪声顶掉，整会话跳过。
        缺陷在 AgentBot 覆写的 process_new_messages（xiaoli_bot.py），
        基类 WeChatBot 逐条遍历无此问题，故本测试必须建 AgentBot 实例。
        """
        from unittest import mock as _mock

        from wx_backend.models import MessageType, WeChatMessage
        from xiaoli_bot import AgentBot

        handled = []

        class FakeWx:
            def iter_unread_sessions(self):
                return iter(["王文生"])

            def get_messages(self, chat):
                return [
                    WeChatMessage(id="v1", chat=chat, sender="未知",
                                  content="你好", type=MessageType.TEXT),
                    WeChatMessage(id="v2", chat=chat, sender="self",
                                  content="发送", type=MessageType.TEXT),
                ]

        bot = AgentBot.__new__(AgentBot)
        bot.paused = False
        bot._sending_lock = False
        bot.last_reply_time = 0.0
        bot.cooldown = 0.0
        bot.wx = FakeWx()
        bot.recent_msg_ids = set()
        bot.nickname = "小漓"
        bot._pending_files = {}
        bot.tasks_dir = tempfile.mkdtemp(prefix="xiaoli_test_")
        bot._task_was_active = False
        bot._task_end_time = None
        bot._listen_hold_seconds = 10
        bot._handle_text = lambda chat, sender, content, msg_id=None: \
            handled.append((sender, content))
        with _mock.patch("xiaoli_bot.should_resume_listen",
                         return_value=(True, False, None)), \
             _mock.patch.object(bot, "_tick_poll_outbox"):
            bot.process_new_messages()
        self.assertEqual(handled, [("未知", "你好")],
                         "应处理最后一条非 self 消息，而非被 self 噪声跳过")

    def test_group_message_without_at_is_skipped(self):
        """群聊消息未 @小漓 → 不处理（latest 兜底路径）。

        用户期望：群聊只有 @ 小漓 的消息才回复，无 @ 不打扰。
        群聊判定走标题（FakeWx._current_is_group），私聊不受限。
        """
        from unittest import mock as _mock

        from wx_backend.models import MessageType, WeChatMessage
        from xiaoli_bot import AgentBot

        handled = []

        class FakeWx:
            _current_is_group = True  # 标题 '强盗"集团(5)' 判定

            def iter_unread_sessions(self):
                return iter(["强盗”集团"])

            def get_messages(self, chat):
                return [
                    WeChatMessage(id="v1", chat=chat, sender="哆拉A萝",
                                  content="豆包有学生优惠了", type=MessageType.TEXT),
                ]

        bot = AgentBot.__new__(AgentBot)
        bot.paused = False
        bot._sending_lock = False
        bot.last_reply_time = 0.0
        bot.cooldown = 0.0
        bot.wx = FakeWx()
        bot.recent_msg_ids = set()
        bot.nickname = "小漓"
        bot._pending_files = {}
        bot.tasks_dir = tempfile.mkdtemp(prefix="xiaoli_test_")
        bot._task_was_active = False
        bot._task_end_time = None
        bot._listen_hold_seconds = 10
        bot._handle_text = lambda chat, sender, content, msg_id=None: \
            handled.append((sender, content))
        with _mock.patch("xiaoli_bot.should_resume_listen",
                         return_value=(True, False, None)), \
             _mock.patch.object(bot, "_tick_poll_outbox"):
            bot.process_new_messages()
        self.assertEqual(handled, [], "群聊未 @ 消息不应回复")

    def test_group_message_with_at_is_handled(self):
        """群聊消息 @小漓 → 正常处理并回复。"""
        from unittest import mock as _mock

        from wx_backend.models import MessageType, WeChatMessage
        from xiaoli_bot import AgentBot

        handled = []

        class FakeWx:
            _current_is_group = True

            def iter_unread_sessions(self):
                return iter(["强盗”集团"])

            def get_messages(self, chat):
                return [
                    WeChatMessage(id="v1", chat=chat, sender="哆拉A萝",
                                  content="@小漓 在吗", type=MessageType.TEXT),
                ]

        bot = AgentBot.__new__(AgentBot)
        bot.paused = False
        bot._sending_lock = False
        bot.last_reply_time = 0.0
        bot.cooldown = 0.0
        bot.wx = FakeWx()
        bot.recent_msg_ids = set()
        bot.nickname = "小漓"
        bot._pending_files = {}
        bot.tasks_dir = tempfile.mkdtemp(prefix="xiaoli_test_")
        bot._task_was_active = False
        bot._task_end_time = None
        bot._listen_hold_seconds = 10
        bot._handle_text = lambda chat, sender, content, msg_id=None: \
            handled.append((sender, content))
        with _mock.patch("xiaoli_bot.should_resume_listen",
                         return_value=(True, False, None)), \
             _mock.patch.object(bot, "_tick_poll_outbox"):
            bot.process_new_messages()
        self.assertEqual(handled, [("哆拉A萝", "@小漓 在吗")],
                         "群聊 @ 消息应正常处理")


if __name__ == "__main__":
    unittest.main(verbosity=2)