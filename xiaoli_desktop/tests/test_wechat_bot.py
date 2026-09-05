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

            def get_messages(self, chat, assume_switched=False):
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

            def get_messages(self, chat, assume_switched=False):
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
                        "has_text": True, "has_media": False,
                        "is_group": False, "width": 747, "height": 1135}
                return {"bot_bottom": None, "other_text": [], "other_media": [],
                        "has_text": True, "has_media": False,
                        "is_group": True, "width": 747, "height": 1135}

            def get_messages(self, chat, assume_switched=False):
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
        bot._chat_fail_at = {}   # 失败退避表（process_new_messages 消费）
        bot._fail_backoff = 8.0
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
            read_marks = []           # mark_session_read 调用记录

            def mark_session_read(self):
                self.read_marks.append(1)

            def iter_unread_sessions(self):
                return iter(["强盗”集团"])

            def analyze_window(self, chat, skip_bot=0):
                return {"bot_bottom": None, "other_text": [], "other_media": [],
                        "has_text": True, "has_media": False,
                        "is_group": True, "width": 747, "height": 1135}

            def get_messages(self, chat, assume_switched=False):
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
        bot._chat_fail_at = {}   # 失败退避表（process_new_messages 消费）
        bot._fail_backoff = 8.0
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
        self.assertEqual(FakeWx.read_marks, [1],
                         "无 @ 跳过时必须点输入框标记已读，防红圈滞留 8s 循环")

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
                # 合并后 analyze_window 纯像素（不读标题）
                return {"bot_bottom": None, "other_text": [], "other_media": [],
                        "has_text": True, "has_media": False,
                        "is_group": False, "width": 747, "height": 1135}

            def get_messages(self, chat, assume_switched=False):
                # 联合 OCR 契约：读取消息时解析标题刷新 _current_is_group
                # （私聊标题「王文生」无括号人数 → False）
                self._current_is_group = False
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
        bot._chat_fail_at = {}   # 失败退避表（process_new_messages 消费）
        bot._fail_backoff = 8.0
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
                        "has_text": True, "has_media": False,
                        "is_group": True, "width": 747, "height": 1135}

            def get_messages(self, chat, assume_switched=False):
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
        bot._chat_fail_at = {}   # 失败退避表（process_new_messages 消费）
        bot._fail_backoff = 8.0
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
        bot._memory_lock = threading.RLock()
        bot._deep_count = {}
        bot.memory_deep_enabled = False
        bot.memory_compress_enabled = False
        bot._deep_dir = ""
        bot._get_history = lambda chat_id: []
        bot._add_history = lambda *a, **k: None
        return bot

    def _user_msg_content(self, bot, user_msg="豆包有学生优惠了", **kwargs):
        from types import SimpleNamespace
        from unittest import mock

        import wechat_bot as wb

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
        content = sent["json"]["messages"][-1]["content"]
        # 沉浸要求已随默认人设内置（运行时注入机制已删）——user 消息即原文
        return content

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

    def test_immersion_baked_into_default_template(self):
        """沉浸要求改为默认人设内置（运行时注入机制已删）：AI_DEFAULTS 与
        CARD_TEMPLATE 的人设必须自带【角色沉浸要求】全文。"""
        from xiaoli_app.config_store import AI_DEFAULTS, CARD_TEMPLATE
        for prompt in (AI_DEFAULTS["system_prompt"], CARD_TEMPLATE["system_prompt"]):
            self.assertIn("【角色沉浸要求】", prompt)
            self.assertIn("禁止用emoji", prompt)
            self.assertIn("永远热爱探索", prompt)

    def test_reply_strips_private_chat_prefix(self):
        """模型复读 [私聊 - 名字] 前缀 → call_chat_ai 返回前剥除（不污染历史）。"""
        from types import SimpleNamespace
        from unittest import mock

        bot = self._make()

        def fake_post(url, headers=None, json=None, timeout=None):
            return SimpleNamespace(
                status_code=200,
                text="{}",
                json=lambda: {"choices": [{"message": {
                    "content": "[私聊 - 王文生] 你好呀"}}]},
            )

        with mock.patch("wechat_bot.requests.post", side_effect=fake_post):
            out = bot.call_chat_ai("王文生", "在吗", sender_name="王文生")
        self.assertEqual(out, "你好呀", "回复开头的 [私聊 - 名字] 前缀应被剥除")


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
                        "has_text": True, "has_media": False,
                        "is_group": True, "width": 747, "height": 1135}

            def get_messages(self, chat, assume_switched=False):
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
        bot._chat_fail_at = {}   # 失败退避表（process_new_messages 消费）
        bot._fail_backoff = 8.0
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

            def get_messages(self, chat, assume_switched=False):
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


class TestImageCaptureBranches(unittest.TestCase):
    """图片/表情统一走 Ctrl+C 路径（_process_image 已删），多张媒体逐张捕获
    （_capture_media_images 返回列表，时间正序）。点击媒体后以「图片和视频」
    查看器是否打开为分界——开了=真图片（Ctrl+C 原图，ESC 关查看器，全库
    唯一 ESC 点）；没开=表情包（绝不碰 ESC/Ctrl+C，裁剪媒体矩形送视觉模型）。
    剪贴板为空也落表情路线（先关查看器防遮挡）。min_top 阈值透传后端过滤
    对方本轮新图（排除 bot 文件卡片/历史媒体）。"""

    @staticmethod
    def _bot():
        from unittest import mock as _mock
        bot = WeChatBot.__new__(WeChatBot)
        bot.wx = _mock.MagicMock()
        bot.wx.media_screen_boxes.return_value = [(100, 200, 220, 320)]
        return bot

    def test_viewer_open_copies_via_clipboard(self):
        """查看器打开（真图片）→ 复制 helper + 恰好一次 ESC，不裁剪。"""
        from unittest import mock as _mock
        bot = self._bot()
        with _mock.patch("wechat_bot.pyautogui") as pg, \
             _mock.patch("wechat_bot.find_window_by_title",
                         return_value=object()) as fw, \
             _mock.patch.object(bot, "_copy_image_from_viewer",
                                return_value="/tmp/orig.jpg") as copy, \
             _mock.patch.object(bot, "_crop_media_region") as crop:
            out = bot._capture_media_images("王文生")
        self.assertEqual(out, ["/tmp/orig.jpg"])
        pg.click.assert_called_once_with(160, 260)  # 媒体矩形中心
        fw.assert_called_with("图片和视频")
        copy.assert_called_once()
        pg.hotkey.assert_not_called()               # Ctrl+C 在 copy helper 内
        pg.press.assert_called_once_with("esc")     # 查看器确认存在才按
        crop.assert_not_called()

    def test_viewer_absent_sticker_route_no_esc(self):
        """查看器没开（表情包）→ 绝不 ESC/Ctrl+C，按媒体矩形裁剪。"""
        from unittest import mock as _mock
        bot = self._bot()
        with _mock.patch("wechat_bot.pyautogui") as pg, \
             _mock.patch("wechat_bot.find_window_by_title", return_value=None), \
             _mock.patch.object(bot, "_copy_image_from_viewer") as copy, \
             _mock.patch.object(bot, "_crop_media_region",
                                return_value="/tmp/sticker.jpg") as crop:
            out = bot._capture_media_images("王文生")
        self.assertEqual(out, ["/tmp/sticker.jpg"])
        pg.hotkey.assert_not_called()   # 表情路线绝不 Ctrl+C
        pg.press.assert_not_called()    # 绝不 ESC——否则关掉微信主窗口
        copy.assert_not_called()
        crop.assert_called_once_with(100, 200, 220, 320)

    def test_copy_failure_falls_to_sticker_route_after_esc(self):
        """查看器开了但剪贴板为空 → 先 ESC 关查看器（防遮挡）再裁剪。"""
        from unittest import mock as _mock
        bot = self._bot()
        with _mock.patch("wechat_bot.pyautogui") as pg, \
             _mock.patch("wechat_bot.find_window_by_title",
                         return_value=object()), \
             _mock.patch.object(bot, "_copy_image_from_viewer",
                                return_value=None), \
             _mock.patch.object(bot, "_crop_media_region",
                                return_value="/tmp/f.jpg") as crop:
            out = bot._capture_media_images("王文生")
        self.assertEqual(out, ["/tmp/f.jpg"])
        pg.press.assert_called_once_with("esc")
        crop.assert_called_once_with(100, 200, 220, 320)

    def test_multi_media_captured_in_order(self):
        """多张媒体 → 逐张走查看器分支，按框序（时间正序）返回列表。"""
        from unittest import mock as _mock
        bot = self._bot()
        bot.wx.media_screen_boxes.return_value = [(100, 200, 220, 320),
                                                  (100, 400, 220, 520)]
        with _mock.patch("wechat_bot.pyautogui") as pg, \
             _mock.patch("wechat_bot.find_window_by_title",
                         return_value=object()), \
             _mock.patch.object(bot, "_copy_image_from_viewer",
                                side_effect=["/tmp/a.jpg", "/tmp/b.jpg"]), \
             _mock.patch.object(bot, "_crop_media_region") as crop:
            out = bot._capture_media_images("王文生")
        self.assertEqual(out, ["/tmp/a.jpg", "/tmp/b.jpg"])
        pg.click.assert_any_call(160, 260)  # 第一张中心
        pg.click.assert_any_call(160, 460)  # 第二张中心
        crop.assert_not_called()

    def test_min_top_passthrough_to_backend(self):
        """min_top 与 exclude_rows 原样透传后端（图标碎片过滤在视觉层执行）。"""
        from unittest import mock as _mock
        bot = self._bot()
        with _mock.patch("wechat_bot.pyautogui"), \
             _mock.patch("wechat_bot.find_window_by_title", return_value=None):
            bot._capture_media_images("王文生", min_top=456,
                                      exclude_rows=[(915, 985)])
        bot.wx.media_screen_boxes.assert_called_once_with(
            min_top=456, exclude_rows=[(915, 985)])

    def test_single_failure_skips_not_aborts(self):
        """单张失败（裁剪返回 None/异常）→ 跳过该张，其余照常捕获。"""
        from unittest import mock as _mock
        bot = self._bot()
        bot.wx.media_screen_boxes.return_value = [(100, 200, 220, 320),
                                                  (100, 400, 220, 520)]
        with _mock.patch("wechat_bot.pyautogui"), \
             _mock.patch("wechat_bot.find_window_by_title", return_value=None), \
             _mock.patch.object(bot, "_copy_image_from_viewer"), \
             _mock.patch.object(bot, "_crop_media_region",
                                side_effect=[None, "/tmp/b.jpg"]) as crop:
            out = bot._capture_media_images("王文生")
        self.assertEqual(out, ["/tmp/b.jpg"])
        self.assertEqual(crop.call_count, 2)

    def test_crop_media_region_translates_screen_to_window(self):
        """表情裁剪：屏幕矩形平移到主窗口图内坐标（窗口 (1000,500) 起）。"""
        from unittest import mock as _mock
        bot = self._bot()
        shot = _mock.MagicMock()
        shot.width, shot.height = 800, 1000
        crop_img = _mock.MagicMock()
        shot.crop.return_value = crop_img
        with _mock.patch("wechat_bot.pyautogui") as pg, \
             _mock.patch("wechat_bot.find_window_by_title", return_value=1), \
             _mock.patch("wechat_bot.ensure_window_visible"), \
             _mock.patch("wechat_bot.window_rect",
                         return_value=(1000, 500, 800, 1000)), \
             _mock.patch.object(bot, "_save_screenshot_compressed",
                                return_value=123):
            pg.screenshot.return_value = shot
            out = bot._crop_media_region(1040, 560, 1160, 680)
        self.assertIsNotNone(out)
        pg.screenshot.assert_called_once_with(region=(1000, 500, 800, 1000))
        shot.crop.assert_called_once_with((40, 60, 160, 180))


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

    def test_find_file_by_display_name_picks_largest_dup(self):
        """同名文件重复落盘 → (N) 重名编号最大（最近下载）优先，ctime 平局。
        快照/成果登记方案已删：显示名锚定查找不再排除任何候选。"""
        obj = self._obj()
        with tempfile.TemporaryDirectory() as tmp:
            obj.file_storage_path = tmp
            old = os.path.join(tmp, "h1_1_m_报告.txt")
            with open(old, "w") as fp:
                fp.write("first")
            new = os.path.join(tmp, "h2_2_m_报告(1).txt")
            with open(new, "w") as fp:
                fp.write("second")
            self.assertEqual(
                obj._find_file_by_display_name("报告.txt"), new,
                "(N) 编号大 = 最近下载，必须选中")
            # 只有单候选时直接命中（含 hash 前缀的下载名）
            self.assertEqual(
                obj._find_file_by_display_name("报告.txt"), new)
            # 无同名候选 → None（不乱选其他文件）
            self.assertIsNone(obj._find_file_by_display_name("不存在.docx"))

    def test_find_file_by_display_name_returns_bot_resent_file(self):
        """用户把 bot 发过的文件发回来：回传下载（hash 前缀、ctime 更新）
        必须命中——旧登记方案在发送后 300s 内会把回传误杀成「文件下载失败」，
        已随快照/登记机制删除。"""
        obj = self._obj()
        with tempfile.TemporaryDirectory() as tmp:
            obj.file_storage_path = tmp
            artifact = os.path.join(tmp, "小漓深海小剧场.html")  # bot 发送副本（干净原名）
            with open(artifact, "w") as fp:
                fp.write("bot sent")
            t_past = time.time() - 3600
            os.utime(artifact, (t_past, t_past))
            returned = os.path.join(
                tmp, "c4fc4da4edbb4f3b87cfbe02e564a2d3_1_m_小漓深海小剧场.html")
            with open(returned, "w") as fp:
                fp.write("returned")  # ctime = 现在（回传下载时刻）
            got = obj._find_file_by_display_name("小漓深海小剧场.html")
            self.assertEqual(got, returned, "回传文件必须被选中，不得被排除")

    def test_find_file_by_display_name_ocr_separator_tolerant(self):
        """OCR 漏读文件名分隔符仍能命中（真机事故：磁盘名「小漓_深海小剧场
        .html」OCR 成「小漓深海小剧场.html」——下划线在基线上被 RapidOCR
        漏掉，包含匹配被断开 → 误报「文件下载失败」）。匹配前剥掉两侧的
        下划线/连字符/空白。"""
        obj = self._obj()
        with tempfile.TemporaryDirectory() as tmp:
            obj.file_storage_path = tmp
            f = os.path.join(tmp, "小漓_深海小剧场.html")
            with open(f, "w") as fp:
                fp.write("x")
            self.assertEqual(
                obj._find_file_by_display_name("小漓深海小剧场.html"), f,
                "OCR 丢下划线的显示名必须命中带下划线的磁盘文件")
            # 反向：磁盘无分隔符、OCR 名带分隔符，同样命中
            g = os.path.join(tmp, "报告终稿.docx")
            with open(g, "w") as fp:
                fp.write("y")
            self.assertEqual(obj._find_file_by_display_name("报告-终稿.docx"), g)


# ---- call_vision_api 单调用形态 + _send_text 占位计数 + vision 模型默认迁移 ----


class TestClearHistory(unittest.TestCase):
    """清空记忆语义：clear_history() 必须同时清内存 memory_db 与磁盘文件，
    GUI「清空全部记忆」按钮依赖此方法（历史缺陷：按钮只写空文件、不动
    memory_db，bot 节流写盘把旧记忆覆盖回来 → 点按钮后 bot 仍有记忆）。"""

    def _make(self):
        import threading
        import wechat_bot
        bot = object.__new__(wechat_bot.WeChatBot)
        bot._memory_dirty = False
        bot._last_memory_save = 0.0
        bot._memory_lock = threading.RLock()
        bot._deep_count = {}
        bot.memory_deep_enabled = False
        bot.memory_compress_enabled = False
        bot._deep_dir = ""
        return bot

    def test_clear_history_clears_ram_and_disk_and_flush_does_not_resurrect(self):
        import json
        import os

        bot = self._make()
        with tempfile.TemporaryDirectory() as tmp:
            bot.memory_file = os.path.join(tmp, "memory.json")
            bot.memory_db = {"王文生": [
                {"role": "user", "content": "旧记忆", "time": "2026-06-14 10:00:00"}]}
            bot.clear_history()
            self.assertEqual(bot.memory_db, {}, "内存记忆应清空")
            with open(bot.memory_file, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {}, "磁盘文件应清空")
            # 模拟节流 flush：清空后即使有脏标记，flush 也不得复活旧记忆
            bot._memory_dirty = True
            bot._flush_memory()
            with open(bot.memory_file, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {}, "节流 flush 后不复活旧记忆")


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
        bot.chat_temperature = 0.7
        bot.vision_max_tokens = 10000
        bot._model_lock = threading.RLock()
        bot._memory_lock = threading.RLock()
        bot._deep_count = {}
        bot.memory_deep_enabled = False
        bot.memory_compress_enabled = False
        bot._deep_dir = ""
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
        self.assertEqual(len(msgs), 2,
                         "messages 应为 [当前时间 system, user]（当前时间无条件注入）")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("当前时间：", msgs[0]["content"],
                      "persona 为空时当前时间 system 仍注入（messages 不空）")
        self.assertEqual(msgs[1]["role"], "user")
        self.assertEqual(msgs[1]["content"], content,
                         "content 应为调用方传入的块列表原样透传")
        blocks = msgs[1]["content"]
        self.assertEqual([b["type"] for b in blocks], ["text", "image_url"],
                         "content 块顺序应为 [text, image_url]")
        self.assertEqual(blocks[0]["text"], "描述这张图片")
        url = blocks[1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"),
                        "image_url 应为 base64 data URL")
        self.assertIn("iVBORy1mYWtl", url, "base64 应编码原始图片字节")

    def test_system_persona_prepended_when_set(self):
        """persona 非空时 messages 为 [system(纯文本人设), user(块列表原样)]；
        图片块只出现在 user（DeepSeek 限制：system 不放图片）。"""
        bot = self._make()
        bot.system_prompt = "你叫小漓，是蓝色大肥鱼。"
        captured, patcher = self._post(
            {"body": {"choices": [{"message": {"content": "ok"}}]}})
        content = [
            {"type": "text", "text": "描述这张图片"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,iVBORy1mYWtl"}},
        ]
        with patcher:
            bot.call_vision_api(content)
        msgs = captured["json"]["messages"]
        self.assertEqual(len(msgs), 3,
                         "messages 应为 [system(人设), system(当前时间), user(块列表)]")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], "你叫小漓，是蓝色大肥鱼。",
                         "system 只放纯文本人设")
        self.assertEqual(msgs[1]["role"], "system")
        self.assertIn("当前时间：", msgs[1]["content"],
                      "当前时间 system 紧跟人设之后")
        self.assertEqual(msgs[2]["role"], "user")
        self.assertEqual(msgs[2]["content"], content, "user 块列表原样透传（含图片）")

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
        self.assertEqual([t["function"]["name"] for t in payload["tools"]],
                         ["dispatch_task", "web_search", "web_fetch"])
        self.assertEqual(payload["tools"][0], {
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
        })

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

    def test_chat_id_injects_history_with_timestamp_prefix(self):
        """传 chat_id 时在 system 人设之后、最后 user 多模态块之前注入
        _get_history(chat_id) 历史，每条带 [time] 前缀（逐字对齐 call_chat_ai
        语义，不重排：_get_history 返回序即注入序）。RED：修复前 call_vision_api
        无 chat_id 参数，payload 不含任何历史 → 模型答"我上一条说的什么"时
        无记忆可查。"""
        bot = self._make()
        bot.system_prompt = "你是小漓"
        history = [
            {"role": "user", "content": "我上一条说的什么",
             "time": "2026-06-14 10:00:00"},
            {"role": "assistant", "content": "你上一条说的是吃饭",
             "time": "2026-06-14 10:00:05"},
        ]
        bot._get_history = lambda chat_id: history
        captured, patcher = self._post(
            {"body": {"choices": [{"message": {"content": "ok"}}]}})
        with patcher:
            bot.call_vision_api(self.TEXT_ONLY, chat_id="王文生")
        msgs = captured["json"]["messages"]
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], "你是小漓", "人设仍为第一条 system")
        hist_msgs = msgs[1:-2]
        self.assertEqual([m["role"] for m in hist_msgs], ["user", "assistant"],
                         "历史紧跟人设（稳定前缀区），保持 _get_history 原序")
        self.assertIn("当前时间：", msgs[-2]["content"],
                      "当前时间 system 紧贴当前消息（历史之后，缓存前缀不断）")
        self.assertEqual(hist_msgs[0]["content"],
                         "[2026-06-14 10:00:00] 我上一条说的什么",
                         "历史消息带 [time] 前缀")
        self.assertEqual(hist_msgs[1]["content"],
                         "[2026-06-14 10:00:05] 你上一条说的是吃饭",
                         "assistant 历史同样带 [time] 前缀")
        self.assertEqual(msgs[-1]["role"], "user")
        self.assertEqual(msgs[-1]["content"], self.TEXT_ONLY,
                         "最后一条 user 为多模态块列表原样透传")

    def test_payload_injects_current_time_system_without_chat_id(self):
        """chat_id=None（图片/文件描述路径）时 payload messages 也必须含
        「当前时间：」system 消息（逐字对齐 call_chat_ai:1321-1324 的
        time.strftime('%Y-%m-%d %H:%M:%S') 格式；无条件注入，与 chat_id
        无关、persona 为空也注入——顺带解决空 messages 隐患）。
        RED：修复前 call_vision_api 只透传 persona，payload 无任何时间
        信息，bot 不知道当前时间。"""
        import re
        bot = self._make()
        captured, patcher = self._post(
            {"body": {"choices": [{"message": {"content": "ok"}}]}})
        with patcher:
            bot.call_vision_api(self.TEXT_ONLY)
        msgs = captured["json"]["messages"]
        time_msgs = [m for m in msgs
                     if m["role"] == "system"
                     and m["content"].startswith("当前时间：")]
        self.assertEqual(len(time_msgs), 1,
                         "payload 必须且只含一条「当前时间：」system 消息")
        self.assertTrue(
            re.match(r"^当前时间：\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$",
                     time_msgs[0]["content"]),
            "时间格式必须与 call_chat_ai 逐字对齐 "
            "time.strftime('%Y-%m-%d %H:%M:%S')，实际: "
            f"{time_msgs[0]['content']}")
        self.assertEqual(msgs[0]["role"], "system",
                         "persona 为空时当前时间 system 仍注入，messages 不空")

    def test_current_time_system_between_persona_and_history(self):
        """chat_id 非空时「当前时间：」system 在 persona system 之后、历史
        注入之前（逐字对齐 call_chat_ai 的 system 序列：人设 → 当前时间 →
        历史 → user；且不含时间格式之外的杂讯）。"""
        import re
        bot = self._make()
        bot.system_prompt = "你是小漓"
        bot._get_history = lambda chat_id: [
            {"role": "user", "content": "我上一条说的什么",
             "time": "2026-06-14 10:00:00"},
        ]
        captured, patcher = self._post(
            {"body": {"choices": [{"message": {"content": "ok"}}]}})
        with patcher:
            bot.call_vision_api(self.TEXT_ONLY, chat_id="王文生")
        msgs = captured["json"]["messages"]
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], "你是小漓", "人设仍为第一条 system")
        self.assertEqual(msgs[1]["content"],
                         "[2026-06-14 10:00:00] 我上一条说的什么",
                         "历史紧跟 persona（稳定前缀区），[ts] 前缀行为不变")
        self.assertTrue(re.match(r"^当前时间：\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$",
                                 msgs[-2]["content"]),
                        "当前时间 system 紧贴当前消息（历史之后），格式对齐 "
                        "time.strftime('%Y-%m-%d %H:%M:%S')，实际: "
                        f"{msgs[-2]['content']}")
        self.assertEqual(msgs[-1]["role"], "user")

    def test_history_without_time_field_passes_content_raw(self):
        """历史条目无 time 字段 → 不加前缀原样注入（对齐 call_chat_ai 的
        else 分支）。"""
        bot = self._make()
        bot._get_history = lambda chat_id: [
            {"role": "user", "content": "没时间戳的历史"},
        ]
        captured, patcher = self._post(
            {"body": {"choices": [{"message": {"content": "ok"}}]}})
        with patcher:
            bot.call_vision_api(self.TEXT_ONLY, chat_id="王文生")
        msgs = captured["json"]["messages"]
        # 无 persona → 历史为第一条，当前时间 system 紧贴当前消息
        self.assertEqual(msgs[0]["content"], "没时间戳的历史",
                         "无 time 字段的历史原样注入，不加前缀")
        self.assertIn("当前时间：", msgs[-2]["content"],
                      "无 persona 时当前时间 system 仍无条件注入")

    def test_text_reply_strips_timestamp_prefix(self):
        """text 响应 content 带时间戳前缀 → 返回前过滤（逐字复用
        call_chat_ai 的 re.sub 正则：模型偶发复读 [ts] 前缀时去除）。"""
        bot = self._make()
        bot._get_history = lambda chat_id: []
        captured, patcher = self._post(
            {"body": {"choices": [{"message": {
                "content": "[2026-06-14 10:00:00] 你上一条说的是吃饭"}}]}})
        with patcher:
            result = bot.call_vision_api(self.TEXT_ONLY, chat_id="王文生")
        self.assertEqual(result, {"kind": "text", "content": "你上一条说的是吃饭"})

    def test_text_reply_strips_private_chat_prefix(self):
        """text 响应 content 带 [私聊 - 名字] 前缀 → 返回前过滤。"""
        bot = self._make()
        bot._get_history = lambda chat_id: []
        captured, patcher = self._post(
            {"body": {"choices": [{"message": {
                "content": "[私聊 - 王文生] 你上一条说的是吃饭"}}]}})
        with patcher:
            result = bot.call_vision_api(self.TEXT_ONLY, chat_id="王文生")
        self.assertEqual(result, {"kind": "text", "content": "你上一条说的是吃饭"})

    def test_history_trimmed_by_budget_before_payload(self):
        """传 chat_id 时在 user 多模态块 append 之后、payload 构造之前调用
        fit_messages_in_budget（逐字对齐 call_chat_ai:1263-1264 的上下文预算
        裁剪：budget 取 getattr(self, 'max_context_tokens', 100000)）。
        RED：修复前 call_vision_api 不裁剪，超长历史/文件全文会撑爆模型
        上下文上限（实测请求 272 万 token → API 400 "maximum context length"）。"""
        from unittest import mock as _mock
        bot = self._make()
        bot.system_prompt = "你是小漓"
        bot.max_context_tokens = 8000
        history = [
            {"role": "user", "content": "历史一", "time": "2026-06-14 10:00:00"},
            {"role": "assistant", "content": "历史二", "time": "2026-06-14 10:00:05"},
        ]
        bot._get_history = lambda chat_id: history
        captured, patcher = self._post(
            {"body": {"choices": [{"message": {"content": "ok"}}]}})
        calls = []

        def fake_fit(messages, **kw):
            calls.append((messages, kw.get("budget")))
            return messages

        with _mock.patch("wechat_bot.fit_messages_in_budget", side_effect=fake_fit), \
             patcher:
            bot.call_vision_api(self.TEXT_ONLY, chat_id="王文生")

        self.assertEqual(len(calls), 1,
                         "payload 构造前必须调用一次 fit_messages_in_budget")
        msgs, budget = calls[0]
        self.assertEqual(budget, 8000,
                         "budget 取 getattr(self, 'max_context_tokens', 100000)")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], "你是小漓", "裁剪收到 system 人设")
        self.assertEqual([m["role"] for m in msgs[1:-2]], ["user", "assistant"],
                         "裁剪收到历史（人设之后、时间 system 之前）")
        self.assertEqual(msgs[-2]["role"], "system")
        self.assertIn("当前时间：", msgs[-2]["content"],
                      "当前时间 system 紧贴当前消息（历史之后，缓存前缀不断）")
        self.assertEqual(msgs[-1]["content"], self.TEXT_ONLY,
                         "裁剪收到最后一条 user 多模态块（append 之后才裁剪）")


class TestSendTextPlaceholder(unittest.TestCase):
    """_send_text 占位计数：按 chat_name 隔离（非全局），placeholder=True 递增 /
    默认（False）归零；发送失败不计。"""

    def _make(self):
        from unittest import mock as _mock
        import wechat_bot
        bot = object.__new__(wechat_bot.WeChatBot)
        bot._pending_placeholders = {}
        bot._chat_fail_at = {}   # 失败退避表（process_new_messages 消费）
        bot._fail_backoff = 8.0
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


class TestMemoryFlushDue(unittest.TestCase):
    """memory 节流兜底：脏数据超 1s 必落盘（引擎轮询顶部检查）——稀疏对话下
    memory.json 查看不再滞后到下一条消息。"""

    @staticmethod
    def _bot(dirpath, dirty=True, last_save=None):
        import time as _time

        import wechat_bot as wb
        bot = wb.WeChatBot.__new__(wb.WeChatBot)
        bot.memory_db = {"小明": [{"role": "assistant", "content": "最新回复",
                                   "time": "2026-01-01 00:00:00"}]}
        bot.memory_file = os.path.join(dirpath, "memory.json")
        bot.max_history = 1000
        bot._memory_dirty = dirty
        bot._last_memory_save = (last_save if last_save is not None
                                 else _time.time() - 10)
        return bot

    def test_due_flushes_to_disk(self):
        import time as _time
        d = tempfile.mkdtemp()
        bot = self._bot(d, last_save=_time.time() - 10)
        bot._flush_memory_if_due()
        self.assertFalse(bot._memory_dirty)
        with open(bot.memory_file, encoding="utf-8") as f:
            self.assertIn("最新回复", f.read())

    def test_not_due_keeps_dirty(self):
        import time as _time
        d = tempfile.mkdtemp()
        bot = self._bot(d, last_save=_time.time())  # 刚写过，未到期
        bot._flush_memory_if_due()
        self.assertTrue(bot._memory_dirty)
        self.assertFalse(os.path.exists(bot.memory_file))

    def test_agentbot_poll_flushes_when_paused(self):
        """process_new_messages 顶部兜底：暂停态轮询也落盘（CLI 暂停挂机场景）。"""
        from xiaoli_bot import AgentBot

        d = tempfile.mkdtemp()
        bot = AgentBot.__new__(AgentBot)
        bot.memory_db = {"小明": [{"role": "assistant", "content": "回复",
                                   "time": "t"}]}
        bot.memory_file = os.path.join(d, "memory.json")
        bot.max_history = 1000
        bot._memory_dirty = True
        bot._last_memory_save = 0.0
        bot.paused = True
        bot.process_new_messages()
        self.assertFalse(bot._memory_dirty)
        self.assertTrue(os.path.isfile(bot.memory_file))


class TestVisionResultRouting(unittest.TestCase):
    """vision 单调用收尾契约：_route_vision_result 可覆写 hook 存在且基类
    默认返回 None（未处理）；两段式残留 _reply_with_vision / _vision_result_text
    与旧 _process_image 路径必须删除；_process_pure_image 的 call_vision_api
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
            img_paths=["/tmp/x.jpg"]))

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

    def test_pure_image_routes_dict_not_text(self):
        """_process_pure_image：tool_call dict 原样路由，不压 arguments 字符串。"""
        from unittest import mock as _mock
        bot = self._obj()
        bot.wx = _mock.MagicMock()
        tool_call = {"kind": "tool_call", "name": "dispatch_task",
                     "arguments": '{"task":"做PPT"}'}
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            img_path = tf.name
            tf.write(b"fake-image-bytes")
        try:
            with _mock.patch.object(bot, "_capture_media_images",
                                    return_value=[img_path]), \
                 _mock.patch.object(bot, "call_vision_api",
                                    return_value=tool_call), \
                 _mock.patch.object(bot, "_route_vision_result",
                                    return_value=True) as route:
                ret = bot._process_pure_image("群")
                self.assertTrue(ret)
                route.assert_called_once()
                _args, _kwargs = route.call_args
                self.assertEqual(_args[2], tool_call,
                                 "tool_call dict 必须原样路由，不压文本")
                self.assertEqual(_kwargs.get("img_paths"), [img_path],
                                 "捕获列表必须经 img_paths 传给路由 hook")
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


class TestVisionRouteImmersion(unittest.TestCase):
    """沉浸要求已随默认人设内置，运行时注入机制（_with_immersion）已删：
    _vision_route 的 user 消息必须是 decorated 原文，不拼接任何注入后缀。"""

    def test_injection_mechanism_removed(self):
        import wechat_bot as wb
        self.assertFalse(hasattr(wb.WeChatBot, "_with_immersion"),
                         "运行时沉浸注入机制必须删除（人设已内置）")

    def test_vision_route_user_msg_is_raw_decorated(self):
        from xiaoli_bot import AgentBot

        bot = AgentBot.__new__(AgentBot)
        bot.system_prompt = "你叫小漓"
        bot.nickname = "小漓"
        bot.vision_api_url = "https://api.test/v1/chat/completions"
        bot.vision_api_key = "test-key"
        bot.chat_model = "test-model"
        bot.chat_temperature = 0.7
        bot.vision_max_tokens = 10000
        bot._model_lock = threading.RLock()
        bot._memory_lock = threading.RLock()
        bot._deep_count = {}
        bot.memory_deep_enabled = False
        bot.memory_compress_enabled = False
        bot._deep_dir = ""
        bot.memory_db = {}
        bot._get_history = lambda chat_id: []
        bot._add_history = lambda chat_id, role, content: None
        bot._send_text = lambda *a, **k: None
        sent = {}

        def fake_vision(content, chat_id=None, related_memory=None):
            sent["text"] = content[0]["text"]
            return {"kind": "text", "content": "嗨"}

        bot.call_vision_api = fake_vision
        bot._vision_route("王文生", "王文生", "你好呀")
        self.assertTrue(sent["text"].endswith("用户消息：\n私聊 - 王文生：你好呀"),
                        "user 消息必须是 decorated 原文（无注入后缀）")
        self.assertNotIn("【角色沉浸要求】", sent["text"],
                         "运行时不得再拼接沉浸要求（人设已内置）")


class TestVisionRoutePersona(unittest.TestCase):
    """方案二契约：人设由 call_vision_api 的 system 消息承载（不重复注入
    user prompt）；_vision_route 的 user prompt 只含路由指令 + 用户消息。

    契约（DeepSeek 限制）：system 只放纯文本人设；图片块只在 user 的
    content 里，绝不进 system 消息。
    """

    def _agent(self, system_prompt):
        from xiaoli_bot import AgentBot

        bot = AgentBot.__new__(AgentBot)
        bot.system_prompt = system_prompt
        bot.nickname = "小漓"
        bot.vision_api_url = "https://api.deepseek.com/v1/chat/completions"
        bot.vision_api_key = "test-key"
        bot.chat_model = "deepseek-v4-flash"
        bot.chat_temperature = 0.7
        bot.vision_max_tokens = 10000
        bot._model_lock = threading.RLock()
        bot._memory_lock = threading.RLock()
        bot._deep_count = {}
        bot.memory_deep_enabled = False
        bot.memory_compress_enabled = False
        bot._deep_dir = ""
        bot.memory_db = {}  # __new__ 绕过 __init__，需补齐 _get_history 依赖
        return bot

    def _capture_payload(self, bot):
        """mock wechat_bot.requests.post，捕获真实 call_vision_api 的 payload。"""
        from unittest import mock as _mock
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["json"] = json
            resp = _mock.MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"choices": [{"message": {"content": "嗨"}}]}
            return resp

        return captured, _mock.patch("wechat_bot.requests.post", side_effect=fake_post)

    def test_persona_goes_to_system_message(self):
        """人设进 call_vision_api 的 messages[0]（system 角色，content 含人设）；
        user 消息仍含路由指令 + 用户文本，且不重复注入人设（不变量）。"""
        bot = self._agent("你叫小漓，是蓝色大肥鱼。不要用 emoji，用颜文字。")
        bot._apply_vision_result = lambda *a, **k: True
        captured, patcher = self._capture_payload(bot)
        with patcher:
            bot._vision_route("王文生", "王文生", "你好呀")

        msgs = captured["json"]["messages"]
        self.assertEqual(msgs[0]["role"], "system", "人设必须由 system 消息承载")
        self.assertIn("你叫小漓", msgs[0]["content"], "system content 含人设")
        self.assertIn("不要用 emoji，用颜文字", msgs[0]["content"],
                      "颜文字指令必须在 system 人设里")
        self.assertEqual(msgs[1]["role"], "system")
        self.assertIn("当前时间：", msgs[1]["content"],
                      "当前时间 system 紧跟人设之后（逐字对齐 call_chat_ai）")
        self.assertEqual(msgs[2]["role"], "user")
        user_text = msgs[2]["content"][0]["text"]
        self.assertIn("判断用户的消息", user_text, "user 消息仍含路由指令")
        self.assertIn("用户消息：\n私聊 - 王文生：你好呀", user_text,
                      "sender 必须进 prompt（修复：vision 单调用分流丢失发送者信息）")
        self.assertNotIn("你叫小漓", user_text,
                         "不变量：_vision_route 的 user prompt 不重复注入人设")

    def test_empty_persona_keeps_route_instruction(self):
        """system_prompt 为空时不报错、不插入空 system 消息（防空消息 API 400）；
        user 消息仍含路由指令（防御空人设）。"""
        bot = self._agent("")
        bot._apply_vision_result = lambda *a, **k: True
        captured, patcher = self._capture_payload(bot)
        with patcher:
            result = bot._vision_route("王文生", "王文生", "你好")

        self.assertTrue(result, "空人设时调用链路不报错、正常返回")
        msgs = captured["json"]["messages"]
        self.assertEqual(len(msgs), 2,
                         "空人设时注入当前时间 system（messages 不空），user 紧随")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("当前时间：", msgs[0]["content"],
                      "persona 为空时当前时间 system 仍注入")
        self.assertEqual(msgs[1]["role"], "user")
        user_text = msgs[1]["content"][0]["text"]
        self.assertIn("判断用户的消息", user_text, "路由指令必须保留")
        self.assertIn("用户消息：\n私聊 - 王文生：你好", user_text,
                      "私聊 decorated 带 sender（空人设不破坏 sender 分流）")

    def test_group_single_sender_goes_to_user_text(self):
        """群聊单条：user prompt 含「群聊：群名 发送者：内容」——sender 和群聊名
        都必须进 prompt（RED：修复前 prompt 只含 text，模型不知道谁发的）。"""
        bot = self._agent("")
        bot._apply_vision_result = lambda *a, **k: True
        captured, patcher = self._capture_payload(bot)
        with patcher:
            bot._vision_route("强盗\"集团", "王文生", "我是谁",
                              is_group=True, multi_sender=False)
        msgs = captured["json"]["messages"]
        user_text = msgs[1]["content"][0]["text"]
        self.assertIn("用户消息：\n群聊：强盗\"集团 王文生：我是谁", user_text,
                      "群聊名与发送者必须都进 prompt")

    def test_group_multi_sender_no_double_wrap(self):
        """多发送者：只包「群聊：群名」前缀、不重包 sender（防「群聊：群名
        王文生：王文生：」双层嵌套——text 里每条已自带发送者名）。"""
        bot = self._agent("")
        bot._apply_vision_result = lambda *a, **k: True
        captured, patcher = self._capture_payload(bot)
        with patcher:
            bot._vision_route(
                "强盗\"集团", "王文生", "王文生：内容A\n李四：内容B",
                is_group=True, multi_sender=True)
        msgs = captured["json"]["messages"]
        user_text = msgs[1]["content"][0]["text"]
        self.assertIn("用户消息：\n群聊：强盗\"集团 王文生：内容A\n李四：内容B",
                      user_text, "多发送者只包群名前缀")
        self.assertNotIn("王文生：王文生", user_text,
                         "不重包 sender（防双层嵌套）")


if __name__ == "__main__":
    unittest.main(verbosity=2)