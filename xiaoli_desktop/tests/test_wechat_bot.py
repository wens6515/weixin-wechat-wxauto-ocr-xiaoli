# -*- coding: utf-8 -*-
"""wechat_bot 基础能力测试：群聊判定集中点、去重集合限界、微信连接重试可停止、
模型端点拼接、图片发送前压缩。"""
import os
import sys
import threading
import tempfile
import time
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

            def analyze_window(self, chat, skip_bot=0):
                return {"bot_bottom": None, "other_text": [], "other_media": [],
                        "has_text": True, "has_media": False, "width": 747, "height": 1135}

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
        bot.nickname = "小漓"
        bot._pending_files = {}
        bot._pending_placeholders = {}
        bot.tasks_dir = tempfile.mkdtemp(prefix="xiaoli_test_")
        bot._task_was_active = False
        bot._task_end_time = None
        bot._listen_hold_seconds = 10
        bot._handle_text = lambda chat, sender, content, msg_id=None, multi_sender=False: \
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

            def analyze_window(self, chat, skip_bot=0):
                return {"bot_bottom": None, "other_text": [], "other_media": [],
                        "has_text": True, "has_media": False, "width": 747, "height": 1135}

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
        bot.nickname = "小漓"
        bot._pending_files = {}
        bot._pending_placeholders = {}
        bot.tasks_dir = tempfile.mkdtemp(prefix="xiaoli_test_")
        bot._task_was_active = False
        bot._task_end_time = None
        bot._listen_hold_seconds = 10
        bot._handle_text = lambda chat, sender, content, msg_id=None, multi_sender=False: \
            handled.append((sender, content))
        with _mock.patch("xiaoli_bot.should_resume_listen",
                         return_value=(True, False, None)), \
             _mock.patch.object(bot, "_tick_poll_outbox"):
            bot.process_new_messages()
        self.assertEqual(handled, [], "群聊未 @ 消息不应回复")

    def test_private_msg_after_group_not_filtered_by_at(self):
        """私聊在群聊之后处理：_current_is_group 缓存残留 True 不得让私聊走 @ 过滤。

        回归（实测日志）：bot 处理完群聊后，私聊「王文生」被判群聊走
        「群聊消息未 @小漓」跳过。根因：_handle_unread 在 analyze_window
        （read_title 刷新 _current_is_group 为本次会话权威值）之前读取旧缓存
        判定 is_group，局部变量不随刷新更新。
        """
        from unittest import mock as _mock

        from wx_backend.models import MessageType, WeChatMessage
        from xiaoli_bot import AgentBot

        handled = []

        class FakeWx:
            _current_is_group = True  # 上一轮群聊残留

            def iter_unread_sessions(self):
                return iter(["王文生"])

            def analyze_window(self, chat, skip_bot=0):
                # 模拟 read_title：私聊标题「王文生」→ 本次会话权威值刷新为 False
                self._current_is_group = False
                return {"bot_bottom": None, "other_text": [], "other_media": [],
                        "has_text": True, "has_media": False, "width": 747, "height": 1135}

            def get_messages(self, chat):
                return [
                    WeChatMessage(id="v1", chat=chat, sender="王文生",
                                  content="在吗", type=MessageType.TEXT),
                ]

        bot = AgentBot.__new__(AgentBot)
        bot.paused = False
        bot._sending_lock = False
        bot.last_reply_time = 0.0
        bot.cooldown = 0.0
        bot.wx = FakeWx()
        bot.nickname = "小漓"
        bot._pending_files = {}
        bot._pending_placeholders = {}
        bot.tasks_dir = tempfile.mkdtemp(prefix="xiaoli_test_")
        bot._task_was_active = False
        bot._task_end_time = None
        bot._listen_hold_seconds = 10
        bot._handle_text = lambda chat, sender, content, msg_id=None, multi_sender=False: \
            handled.append((sender, content))
        with _mock.patch("xiaoli_bot.should_resume_listen",
                         return_value=(True, False, None)), \
             _mock.patch.object(bot, "_tick_poll_outbox"):
            bot.process_new_messages()
        self.assertEqual(handled, [("王文生", "在吗")],
                         "私聊不应被上一轮群聊的 _current_is_group 残留误判为群聊")

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

            def analyze_window(self, chat, skip_bot=0):
                return {"bot_bottom": None, "other_text": [], "other_media": [],
                        "has_text": True, "has_media": False, "width": 747, "height": 1135}

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
        bot.nickname = "小漓"
        bot._pending_files = {}
        bot._pending_placeholders = {}
        bot.tasks_dir = tempfile.mkdtemp(prefix="xiaoli_test_")
        bot._task_was_active = False
        bot._task_end_time = None
        bot._listen_hold_seconds = 10
        bot._handle_text = lambda chat, sender, content, msg_id=None, multi_sender=False: \
            handled.append((sender, content))
        with _mock.patch("xiaoli_bot.should_resume_listen",
                         return_value=(True, False, None)), \
             _mock.patch.object(bot, "_tick_poll_outbox"):
            bot.process_new_messages()
        self.assertEqual(handled, [("哆拉A萝", "@小漓 在吗")],
                         "群聊 @ 消息应正常处理")


class TestCallChatAiGroupNameFormat(unittest.TestCase):
    """群聊名字保真：call_chat_ai 的 decorated 必须含「群聊名+发送者名+内容」
    （用户原话格式：群聊：XXX XXX：消息内容），私聊保持现有格式不变。

    RED 复现：旧实现 f"群聊 - {sender_name}：{user_msg}" 无群聊名；
    sender_name 缺失时退化 f"群聊：{user_msg}" 完全无名字。
    """

    def _make(self):
        import threading

        import wechat_bot as wb

        bot = wb.WeChatBot.__new__(wb.WeChatBot)
        bot.api_url = "https://api.test/v1/chat/completions"
        bot.api_key = "test-key"
        bot.chat_model = "test-model"
        bot.chat_temperature = 0.7
        bot.chat_top_p = 0.9
        bot.api_retry = 0
        bot.api_timeout = 5
        bot.system_prompt = "你是小漓"
        bot._model_lock = threading.RLock()
        bot._get_history = lambda chat_id: []
        bot._add_history = lambda *a, **k: None
        return bot

    def _user_msg_content(self, bot, user_msg="豆包有学生优惠了", **kwargs):
        from types import SimpleNamespace
        from unittest import mock

        sent = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            sent["json"] = json
            return SimpleNamespace(
                status_code=200,
                text="{}",
                json=lambda: {"choices": [{"message": {"content": "ok"}}]},
            )

        with mock.patch("wechat_bot.requests.post", side_effect=fake_post):
            reply = bot.call_chat_ai("强盗”集团", user_msg, **kwargs)
        self.assertEqual(reply, "ok")
        return sent["json"]["messages"][-1]["content"]

    def test_group_with_sender(self):
        bot = self._make()
        content = self._user_msg_content(
            bot, sender_name="哆拉A萝", is_group=True)
        self.assertEqual(content, "群聊：强盗”集团 哆拉A萝：豆包有学生优惠了",
                         "群聊 decorated 必须为「群聊名+发送者名+内容」")

    def test_group_without_sender_falls_back_chat_name(self):
        """极端 OCR 失败：sender_name 缺失 → 群聊名兜底并打日志，
        不退化 f"群聊：{user_msg}" 无名字分支。"""
        bot = self._make()
        content = self._user_msg_content(bot, sender_name=None, is_group=True)
        self.assertEqual(content, "群聊：强盗”集团：豆包有学生优惠了",
                         "群聊无发送者名时用群聊名兜底，不得退化为无名字")

    def test_group_multi_sender_no_double_wrap(self):
        """多发送者已装饰文本：只包『群聊：群名』前缀，不再重包 sender。
        RED 复现：旧实现把最后一条 sender 再包一层 →
        '群聊：群名 王文生：哆拉A萝：内容\n王文生：在吗' 双层嵌套。"""
        bot = self._make()
        content = self._user_msg_content(
            bot, "哆拉A萝：豆包有学生优惠了\n王文生：在吗",
            sender_name="王文生", is_group=True, multi_sender=True)
        self.assertEqual(content,
                         "群聊：强盗”集团 哆拉A萝：豆包有学生优惠了\n王文生：在吗",
                         "多发送者只包群聊名前缀，不得重包 sender")

    def test_group_multi_sender_branch_wins_over_sender_name(self):
        """多发送者分支优先于 sender_name：即使 sender_name 缺失
        （视觉层只给最后一条）也不得重包或退化兜底。"""
        bot = self._make()
        content = self._user_msg_content(
            bot, "哆拉A萝：你好\n王文生：在吗",
            sender_name=None, is_group=True, multi_sender=True)
        self.assertEqual(content, "群聊：强盗”集团 哆拉A萝：你好\n王文生：在吗",
                         "多发送者分支不依赖 sender_name")

    def test_group_single_multiline_not_multi_sender(self):
        """单条多行文本（user_msg 含换行）不得被隐式判定为多发送者——
        multi_sender 是唯一显式信号，禁换行启发式（单条多行会误判）。"""
        bot = self._make()
        content = self._user_msg_content(
            bot, "第一行\n第二行", sender_name="哆拉A萝", is_group=True)
        self.assertEqual(content, "群聊：强盗”集团 哆拉A萝：第一行\n第二行",
                         "单条多行仍走单条分支（群聊名+sender+内容）")

    def test_private_format_unchanged_with_sender(self):
        bot = self._make()
        content = self._user_msg_content(
            bot, sender_name="王文生", is_group=False)
        self.assertEqual(content, "私聊 - 王文生：豆包有学生优惠了",
                         "私聊格式保持现状不变")

    def test_private_format_unchanged_without_sender(self):
        bot = self._make()
        content = self._user_msg_content(bot, sender_name=None, is_group=False)
        self.assertEqual(content, "私聊：豆包有学生优惠了",
                         "私聊无发送者名时保持现状退化分支不变")


class TestGroupMultiSenderText(unittest.TestCase):
    """群聊 @ 后多条不同发送者消息：text_content 每条必须带各自发送者名
    （不整批只带最后一条的 sender）。

    RED 复现：旧实现 text_parts 只取 content 合并，多条消息的发送者名
    全部丢失，AI 只能看到最后一条的 sender。
    """

    def _run(self, msgs):
        from unittest import mock as _mock

        from wx_backend.models import MessageType, WeChatMessage
        from xiaoli_bot import AgentBot

        handled = []

        class FakeWx:
            _current_is_group = True

            def iter_unread_sessions(self):
                return iter(["强盗”集团"])

            def analyze_window(self, chat, skip_bot=0):
                return {"bot_bottom": None, "other_text": [], "other_media": [],
                        "has_text": True, "has_media": False, "width": 747, "height": 1135}

            def get_messages(self, chat):
                return [
                    WeChatMessage(id=m["id"], chat=chat, sender=m["sender"],
                                  content=m["content"], type=MessageType.TEXT)
                    for m in msgs
                ]

        bot = AgentBot.__new__(AgentBot)
        bot.paused = False
        bot._sending_lock = False
        bot.last_reply_time = 0.0
        bot.cooldown = 0.0
        bot.wx = FakeWx()
        bot.nickname = "小漓"
        bot._pending_files = {}
        bot._pending_placeholders = {}
        bot.tasks_dir = tempfile.mkdtemp(prefix="xiaoli_test_")
        bot._task_was_active = False
        bot._task_end_time = None
        bot._listen_hold_seconds = 10
        bot._handle_text = lambda chat, sender, content, msg_id=None, multi_sender=False: \
            handled.append((sender, content))
        with _mock.patch("xiaoli_bot.should_resume_listen",
                         return_value=(True, False, None)), \
             _mock.patch.object(bot, "_tick_poll_outbox"):
            bot.process_new_messages()
        return handled

    def test_multi_sender_each_keeps_name(self):
        """多条不同发送者 @ 消息：合并文本每条带各自发送者名。"""
        handled = self._run([
            {"id": "v1", "sender": "哆拉A萝", "content": "豆包有学生优惠了 @小漓"},
            {"id": "v2", "sender": "王五", "content": "真的假的 @小漓"},
        ])
        self.assertEqual(len(handled), 1)
        self.assertEqual(handled[0][0], "王五", "sender 应为最后一条消息的发送者")
        self.assertIn("哆拉A萝：豆包有学生优惠了", handled[0][1],
                      "第一条消息必须带自己的发送者名")
        self.assertIn("王五：真的假的", handled[0][1],
                      "第二条消息必须带自己的发送者名")

    def test_single_msg_no_repeated_sender(self):
        """单条 @ 消息：text_content 不带 sender 前缀（decorated 负责带
        sender_name），避免「发送者名：发送者名：内容」重复。"""
        handled = self._run([
            {"id": "v1", "sender": "哆拉A萝", "content": "@小漓 在吗"},
        ])
        self.assertEqual(handled, [("哆拉A萝", "@小漓 在吗")])

    def test_group_emoji_is_skipped(self):
        """群聊表情消息（EMOJI）不处理——即使 @ 了小漓也不读表情。"""
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
                                  content="@小漓 [动画表情]", type=MessageType.EMOJI),
                ]

        bot = AgentBot.__new__(AgentBot)
        bot.paused = False
        bot._sending_lock = False
        bot.last_reply_time = 0.0
        bot.cooldown = 0.0
        bot.wx = FakeWx()
        bot.nickname = "小漓"
        bot._pending_files = {}
        bot.tasks_dir = tempfile.mkdtemp(prefix="xiaoli_test_")
        bot._task_was_active = False
        bot._task_end_time = None
        bot._listen_hold_seconds = 10
        bot._handle_text = lambda chat, sender, content, msg_id=None, multi_sender=False: \
            handled.append((sender, content))
        with _mock.patch("xiaoli_bot.should_resume_listen",
                         return_value=(True, False, None)), \
             _mock.patch.object(bot, "_tick_poll_outbox"):
            bot.process_new_messages()
        self.assertEqual(handled, [], "群聊表情消息不应处理（即使 @ 了）")


class TestImageProcessing(unittest.TestCase):
    def test_process_image_visual_uses_media_box(self):
        """visual 图片消息用媒体矩形检测定位 + 点击，而非整屏降级。"""
        from unittest import mock as _mock
        from wx_backend.models import WeChatMessage, MessageType

        bot = WeChatBot.__new__(WeChatBot)
        bot.image_click_offset = [0, 0]
        bot.vision_prompt = "描述这张图片"
        bot.wx = _mock.MagicMock()
        bot.wx.media_screen_boxes.return_value = [(100, 200)]

        msg = WeChatMessage(id="v1", chat="王文生", sender="王文生",
                            content="", type=MessageType.IMAGE)

        with _mock.patch("wechat_bot.pyautogui") as pg, \
             _mock.patch("uiautomation.WindowControl") as wc, \
             _mock.patch.object(bot, "call_vision_api",
                                return_value={"kind": "tool_call",
                                              "name": "dispatch_task",
                                              "arguments": '{"task":"画图"}'}), \
             _mock.patch.object(bot, "_save_screenshot_compressed"), \
             _mock.patch.object(bot, "_route_vision_result",
                                return_value=True) as route, \
             _mock.patch.object(bot, "_process_image_visual_fallback",
                                return_value=True) as fallback:
            preview = _mock.MagicMock()
            preview.Exists.return_value = False  # 无预览 → 主窗口截图分支
            wechat_win = _mock.MagicMock()
            wechat_win.BoundingRectangle.left = 0
            wechat_win.BoundingRectangle.top = 0
            wechat_win.BoundingRectangle.width.return_value = 400
            wechat_win.BoundingRectangle.height.return_value = 400
            wc.side_effect = [preview, wechat_win]
            pg.screenshot.return_value = _mock.MagicMock()

            bot._process_image("王文生", "王文生", msg)

            pg.click.assert_called_once_with(100, 200)
            fallback.assert_not_called()
            # 契约：call_vision_api 的 dict 返回原样路由给 _route_vision_result，
            # 不压文本、不二次转述（tool_call 语义不丢失）
            route.assert_called_once()
            _args, _kwargs = route.call_args
            self.assertEqual(_kwargs.get("img_path") is not None, True,
                             "img_path 必须透传给 _route_vision_result")
            _result = _args[2] if len(_args) > 2 else None
            self.assertEqual(_result,
                             {"kind": "tool_call",
                              "name": "dispatch_task",
                              "arguments": '{"task":"画图"}'},
                             "tool_call dict 必须原样传给 _route_vision_result")


# ---- 文件显示名提取 + 目录快照增量（微信保留源时间戳的应对） ----


class TestFileDisplayNameAndSnapshot(unittest.TestCase):
    """Bug2 回归：视觉后端 FILE 消息 content 可能是合并文本（多条文件消息
    OCR 并成一个文本块 + 图标杂字符），必须拆出真实文件名；目录兜底扫描
    用快照增量识别「新下载」，不按 mtime/ctime 猜（微信保留源时间戳）。"""

    @staticmethod
    def _obj():
        import wechat_bot
        return object.__new__(wechat_bot.WeChatBot)

    def test_extract_display_name_from_merged_text(self):
        """合并文本（两个文件名 + 图标杂字符 W）→ 拆出第一个真实文件名。"""
        from wx_backend.models import WeChatMessage, MessageType
        obj = self._obj()
        m = WeChatMessage(id="x", chat="c", sender="s",
                          content="新宣传.docx 部门简介+纳新宣传.docx W",
                          type=MessageType.FILE)
        self.assertEqual(obj._extract_file_display_name(m), "新宣传.docx")

    def test_extract_display_name_single(self):
        """干净单文件名原样返回（含 + 号）。"""
        from wx_backend.models import WeChatMessage, MessageType
        obj = self._obj()
        m = WeChatMessage(id="x", chat="c", sender="s",
                          content="部门简介+纳新宣传.docx", type=MessageType.FILE)
        self.assertEqual(obj._extract_file_display_name(m), "部门简介+纳新宣传.docx")

    def test_extract_display_name_other_ext(self):
        """xlsx/pdf 等扩展名同样可拆。"""
        from wx_backend.models import WeChatMessage, MessageType
        obj = self._obj()
        m = WeChatMessage(id="x", chat="c", sender="s",
                          content="名单.xlsx 说明.pdf W", type=MessageType.FILE)
        self.assertEqual(obj._extract_file_display_name(m), "名单.xlsx")

    def test_snapshot_incremental_finds_new_download(self):
        """快照增量：首次建基线返回 None；新文件（即使 mtime 是旧的——
        微信保留源时间戳）被识别为增量并按重名编号取最新；重启后无新文件
        返回 None。"""
        obj = self._obj()
        with tempfile.TemporaryDirectory() as tmp:
            obj._file_snapshot_path = os.path.join(tmp, "snap.json")
            obj._file_snapshot = {}
            obj._sent_back_files = {}
            obj._sent_back_stems = {}
            d = os.path.join(tmp, "files")
            os.makedirs(d)
            old = os.path.join(d, "部门简介+纳新宣传(1).docx")
            with open(old, "w") as f:
                f.write("old")
            t_past = time.time() - 86400
            os.utime(old, (t_past, t_past))
            # 首次：建基线，不返回（无从判断增量）
            self.assertIsNone(obj._find_user_file(d))
            # 新文件落盘：重名编号大（(5)），mtime 仍是旧源时间戳
            newf = os.path.join(d, "部门简介+纳新宣传(5).docx")
            with open(newf, "w") as f:
                f.write("new")
            os.utime(newf, (t_past, t_past))
            got = obj._find_user_file(d)
            self.assertEqual(os.path.basename(got), "部门简介+纳新宣传(5).docx")
            # 重启模拟：重新加载快照，无新增 → None
            obj._file_snapshot = obj._load_file_snapshot()
            self.assertIsNone(obj._find_user_file(d))


    def test_find_file_by_display_name_excludes_sent_back(self):
        """成果副本（stem 匹配 + ctime 在发送时刻附近）在文件名锚定路径
        也被排除——用户把成果转回时消息文件名与成果相同，不排除会误选。"""
        obj = self._obj()
        with tempfile.TemporaryDirectory() as tmp:
            obj.file_storage_path = tmp
            obj._sent_back_stems = {}
            f = os.path.join(tmp, "部门简介+纳新宣传(3).docx")
            with open(f, "w") as fp:
                fp.write("x")
            # 未登记成果前：能按显示名定位
            self.assertEqual(
                obj._find_file_by_display_name("部门简介+纳新宣传.docx"), f)
            # 登记成果（发送时刻 = 现在；副本 ctime ≈ 现在，落在 ±300s 窗口）
            obj._sent_back_stems["部门简介+纳新宣传"] = time.time()
            self.assertIsNone(
                obj._find_file_by_display_name("部门简介+纳新宣传.docx"))

    def test_refresh_file_snapshot(self):
        """任务回传后刷新快照：扫描目录固化已见集合；目录不可用静默跳过。"""
        import json
        obj = self._obj()
        with tempfile.TemporaryDirectory() as tmp:
            obj.file_storage_path = tmp
            obj._file_snapshot = {}
            obj._file_snapshot_path = os.path.join(tmp, "snap.json")
            f = os.path.join(tmp, "成果.docx")
            with open(f, "w") as fp:
                fp.write("x")
            obj._refresh_file_snapshot(wait=0)
            with open(obj._file_snapshot_path, encoding="utf-8") as fp:
                snap = json.load(fp)
            self.assertIn(f, snap)
        # 目录不可用 → 不抛异常
        obj2 = self._obj()
        obj2.file_storage_path = None
        obj2._refresh_file_snapshot(wait=0)


# ---- call_vision_api 单调用形态 + _send_text 占位计数 + vision 模型默认迁移 ----


class TestCallVisionApi(unittest.TestCase):
    """call_vision_api 单参契约：唯一参数 content 为块列表 list[dict]
    [{"type":"text","text":...}, {"type":"image_url","image_url":{"url":"data:image/..."}}]
    （图片块可选，无图时只含 text 块）；payload 声明 dispatch_task 工具
    （tool_choice=auto）；响应解析 tool_calls → 结构化标记 / 仅 content →
    文本 dict / 失败 → None。"""

    TEXT_ONLY = [{"type": "text", "text": "描述"}]

    def _make(self):
        import wechat_bot
        bot = object.__new__(wechat_bot.WeChatBot)
        bot.vision_api_url = "https://api.deepseek.com/v1/chat/completions"
        bot.vision_api_key = "test-key"
        # 单模型化：call_vision_api 的 model 取 chat_model（无独立 vision_model）
        bot.chat_model = "deepseek-v4-flash"
        bot.vision_temp = 0.7
        bot.vision_max_tokens = 10000
        bot._model_lock = threading.RLock()
        return bot

    def _post(self, payload):
        from unittest import mock as _mock
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["json"] = json
            resp = _mock.MagicMock()
            resp.status_code = payload.get("status", 200)
            resp.json.return_value = payload.get("body", {})
            resp.text = payload.get("text", "")
            return resp

        return captured, _mock.patch("wechat_bot.requests.post", side_effect=fake_post)

    def test_payload_single_user_message_text_then_image(self):
        """payload：唯一一条 user 消息，content 为调用方传入的块列表原样透传
        （text 在前 image_url 在后，image_url 为 base64 data URL，图片仅出现一次）。"""
        from unittest import mock as _mock
        bot = self._make()
        captured = {}
        content = [
            {"type": "text", "text": "描述这张图片"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,iVBORy1mYWtl"}},
        ]

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["json"] = json
            resp = _mock.MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
            return resp

        with _mock.patch("wechat_bot.requests.post", side_effect=fake_post):
            bot.call_vision_api(content)

        msgs = captured["json"]["messages"]
        self.assertEqual(len(msgs), 1, "messages 只应有一条（图片仅随最初 user 消息传入一次）")
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["content"], content,
                         "content 应为调用方传入的块列表原样透传")
        blocks = msgs[0]["content"]
        self.assertEqual([b["type"] for b in blocks], ["text", "image_url"],
                         "content 块顺序应为 [text, image_url]")
        self.assertEqual(blocks[0]["text"], "描述这张图片")
        url = blocks[1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"),
                        "image_url 应为 base64 data URL")
        self.assertIn("iVBORy1mYWtl", url, "base64 应编码原始图片字节")

    def test_payload_declares_dispatch_task_tool(self):
        """payload 必须声明 dispatch_task 工具 + tool_choice=auto：
        没有 tools 数组模型永不输出 tool_calls，任务消息无法分流。"""
        bot = self._make()
        captured, patcher = self._post(
            {"body": {"choices": [{"message": {"content": "ok"}}]}})
        with patcher:
            bot.call_vision_api(self.TEXT_ONLY)
        payload = captured["json"]
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertEqual(payload["model"], "deepseek-v4-flash",
                         "单模型化：视觉 model 取 chat_model（无独立 vision_model）")
        self.assertEqual(payload["tools"], [{
            "type": "function",
            "function": {
                "name": "dispatch_task",
                "description": "判断用户消息是否为任务，是则投递天枢处理",
                "parameters": {
                    "type": "object",
                    "properties": {"task": {"type": "string", "description": "任务描述"}},
                    "required": ["task"],
                },
            },
        }])

    def test_text_content_returns_kind_text(self):
        """仅 message.content → {'kind': 'text', 'content': ...}（strip 空白）。"""
        bot = self._make()
        captured, patcher = self._post(
            {"body": {"choices": [{"message": {"content": "  图片里有只猫  "}}]}})
        with patcher:
            result = bot.call_vision_api([{"type": "text", "text": "描述"}])
        self.assertEqual(result, {"kind": "text", "content": "图片里有只猫"})

    def test_tool_calls_returns_kind_tool_call(self):
        """message.tool_calls 存在（dispatch_task 工具调用）→ 结构化标记，name/arguments 透传。"""
        bot = self._make()
        captured, patcher = self._post(
            {"body": {"choices": [{"message": {
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "dispatch_task",
                                 "arguments": '{"task": "根据文档做网站"}'},
                }],
            }}]}})
        with patcher:
            result = bot.call_vision_api([{"type": "text", "text": "描述"}])
        self.assertEqual(result, {"kind": "tool_call", "name": "dispatch_task",
                                  "arguments": '{"task": "根据文档做网站"}'})

    def test_no_choices_returns_none(self):
        bot = self._make()
        captured, patcher = self._post({"body": {"choices": []}})
        with patcher:
            self.assertIsNone(bot.call_vision_api([{"type": "text", "text": "描述"}]))

    def test_http_error_returns_none(self):
        bot = self._make()
        captured, patcher = self._post({"status": 429, "text": "rate limited",
                                        "body": {}})
        with patcher:
            self.assertIsNone(bot.call_vision_api([{"type": "text", "text": "描述"}]))

    def test_blank_content_returns_none(self):
        """content 空白且无 tool_calls → None（旧'（无有效描述）'占位文本移除）。"""
        bot = self._make()
        captured, patcher = self._post(
            {"body": {"choices": [{"message": {"content": "   "}}]}})
        with patcher:
            self.assertIsNone(bot.call_vision_api([{"type": "text", "text": "描述"}]))


class TestSendTextPlaceholder(unittest.TestCase):
    """_send_text 占位计数：按 chat_name 隔离（非全局），placeholder=True 递增 /
    默认（False）归零；发送失败不计。"""

    def _make(self):
        from unittest import mock as _mock
        import wechat_bot
        bot = object.__new__(wechat_bot.WeChatBot)
        bot._pending_placeholders = {}
        bot.wx = _mock.MagicMock()
        return bot

    def test_placeholder_increments_per_chat(self):
        bot = self._make()
        bot._send_text("正在处理中", "小明", placeholder=True)
        bot._send_text("正在处理中", "小明", placeholder=True)
        bot._send_text("正在处理中", "产品讨论群", placeholder=True)
        self.assertEqual(bot._pending_placeholders, {"小明": 2, "产品讨论群": 1})

    def test_default_resets_to_zero(self):
        bot = self._make()
        bot._send_text("占位", "小明", placeholder=True)
        bot._send_text("占位", "小明", placeholder=True)
        bot._send_text("结果来了", "小明")  # 非占位 → 删除键
        self.assertNotIn("小明", bot._pending_placeholders,
                         "归零分支应删除键而非留 0（防 dict 膨胀）")

    def test_chats_are_isolated(self):
        bot = self._make()
        bot._send_text("占位", "小明", placeholder=True)
        bot._send_text("结果", "小明")  # 小明删除键
        self.assertNotIn("小明", bot._pending_placeholders)
        self.assertEqual(bot._pending_placeholders.get("产品讨论群"), None,
                         "B 聊天未被 A 的计数影响")

    def test_send_failure_not_counted(self):
        bot = self._make()
        bot.wx.send_text.side_effect = RuntimeError("send failed")
        bot._send_text("占位", "小明", placeholder=True)
        self.assertEqual(bot._pending_placeholders.get("小明"), None,
                         "发送失败不应计入占位计数")


class TestVisionResultRouting(unittest.TestCase):
    """vision 单调用收尾契约：_route_vision_result 可覆写 hook 存在且基类
    默认返回 None（未处理）；两段式残留 _reply_with_vision / _vision_result_text
    必须删除；_describe_image / _process_image_visual_fallback 的 call_vision_api
    dict 返回直接路由，不压文本不转述（tool_call 语义不丢失）。"""

    @staticmethod
    def _obj():
        import wechat_bot
        return object.__new__(wechat_bot.WeChatBot)

    def test_route_vision_result_hook_exists_default_none(self):
        """基类 _route_vision_result 存在，任何输入默认返回 None（未处理）。"""
        bot = self._obj()
        self.assertTrue(callable(getattr(bot, "_route_vision_result", None)),
                        "_route_vision_result hook 必须存在")
        self.assertIsNone(bot._route_vision_result("群", "小明", None))
        self.assertIsNone(bot._route_vision_result(
            "群", "小明", {"kind": "text", "content": "描述"}))
        self.assertIsNone(bot._route_vision_result(
            "群", "小明", {"kind": "tool_call", "name": "x", "arguments": "{}"},
            img_path="/tmp/x.jpg"))

    def test_reply_with_vision_removed(self):
        """两段式残留 _reply_with_vision 必须删除（基类无此方法）。"""
        import wechat_bot
        self.assertFalse(hasattr(wechat_bot.WeChatBot, "_reply_with_vision"),
                         "_reply_with_vision 两段式残留必须删除")

    def test_vision_result_text_removed(self):
        """_vision_result_text 改路由后无调用方，必须一并删除。"""
        import wechat_bot
        self.assertFalse(hasattr(wechat_bot.WeChatBot, "_vision_result_text"),
                         "_vision_result_text 无调用方应删除")

    def test_visual_fallback_routes_dict_not_text(self):
        """降级路径：call_vision_api 返回 tool_call dict → 原样路由给
        _route_vision_result（不压 arguments 字符串、不写 [图片描述] 转述）。"""
        from unittest import mock as _mock
        bot = self._obj()
        bot.vision_prompt = "描述"
        bot.wx = _mock.MagicMock()
        tool_call = {"kind": "tool_call", "name": "dispatch_task",
                     "arguments": '{"task":"画一张图"}'}
        with _mock.patch("wechat_bot.pyautogui") as pg, \
             _mock.patch.object(bot, "_save_screenshot_compressed"), \
             _mock.patch.object(bot, "call_vision_api", return_value=tool_call), \
             _mock.patch.object(bot, "_route_vision_result",
                                return_value=True) as route:
            pg.screenshot.return_value = _mock.MagicMock()
            ret = bot._process_image_visual_fallback("群", "小明")
            self.assertTrue(ret)
            route.assert_called_once()
            _args, _kwargs = route.call_args
            self.assertEqual(_kwargs.get("img_path") is not None, True)
            self.assertEqual(_args[2], tool_call,
                             "tool_call dict 必须原样路由，不压文本")

    def test_describe_image_routes_dict_not_text(self):
        """_describe_image：tool_call dict 原样路由，不压 arguments 字符串。"""
        from unittest import mock as _mock
        bot = self._obj()
        bot.vision_prompt = "描述"
        bot.wx = _mock.MagicMock()
        tool_call = {"kind": "tool_call", "name": "dispatch_task",
                     "arguments": '{"task":"做PPT"}'}
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            img_path = tf.name
            tf.write(b"fake-image-bytes")
        try:
            with _mock.patch.object(bot, "_capture_latest_image",
                                    return_value=img_path), \
                 _mock.patch.object(bot, "call_vision_api",
                                    return_value=tool_call), \
                 _mock.patch.object(bot, "_route_vision_result",
                                    return_value=True) as route:
                ret = bot._describe_image("群")
                self.assertTrue(ret)
                route.assert_called_once()
                _args, _kwargs = route.call_args
                self.assertEqual(_args[2], tool_call,
                                 "tool_call dict 必须原样路由，不压文本")
        finally:
            if os.path.exists(img_path):
                os.unlink(img_path)


class TestVisionModelDefault(unittest.TestCase):
    """单模型化：视觉统一走 chat_model，load_config 不再补/迁移独立 vision_model
    默认值（default_cfg 已删该键）；chat_model 缺键用默认补齐，已有配置不被覆盖。"""

    def test_config_missing_key_filled_with_default(self):
        import json
        import wechat_bot
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"bot_nickname": "小漓"}, f)
            cfg = wechat_bot.load_config(path)
            self.assertEqual(cfg["chat_model"], "deepseek:deepseek-v4-flash",
                             "缺键时应补默认 chat_model")
            self.assertNotIn("vision_model", cfg,
                             "单模型化：load_config 不再补独立 vision_model 键（视觉走 chat_model）")

    def test_existing_config_not_overwritten(self):
        import json
        import wechat_bot
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"chat_model": "zhipu:glm-4v-flash"}, f)
            cfg = wechat_bot.load_config(path)
            self.assertEqual(cfg["chat_model"], "zhipu:glm-4v-flash",
                             "用户已有配置不应被默认值覆盖")


if __name__ == "__main__":
    unittest.main(verbosity=2)