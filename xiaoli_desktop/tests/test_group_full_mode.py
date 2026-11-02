# -*- coding: utf-8 -*-
"""群聊三模式测试：全接收批处理、逐条入史、游标判重、发送闸（只有工具能发）。"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wx_backend.models import MessageType, WeChatMessage
from wx_backend.visual_backend import _media_sender, _region_fingerprint
from xiaoli_bot import AgentBot
from wechat_bot import REPLY_MAX_TOKENS


def _make_bot(mode="auto", voice="off"):
    """AgentBot 直构桩：只装全接收链路用到的属性（不连微信）。"""
    bot = AgentBot.__new__(AgentBot)
    bot.nickname = "小漓"
    bot.group_reply_mode = mode
    bot.memory_file = os.path.join(tempfile.mkdtemp(), "memory.json")
    bot.max_history = 1000
    bot.memory_keep_recent = 30
    bot.memory_rolling_step = 30
    bot.memory_deep_enabled = False
    bot.memory_compress_enabled = False
    bot.chat_card_params = {}
    bot.chat_card_bindings = {}
    bot.chat_feature_overrides = {}
    bot.system_prompt = "人设"
    bot.api_url = "https://api.test/v1/chat/completions"
    bot.api_key = "k"
    bot.vision_api_url = bot.api_url
    bot.vision_api_key = "k"
    bot.chat_model = "test-model"
    bot.chat_temperature = 0.7
    bot.chat_top_p = 0.9
    bot.reply_max_tokens = REPLY_MAX_TOKENS
    bot._model_lock = threading.RLock()
    bot.voice_mode = voice
    bot.tts_endpoint = ""
    bot.voice_profiles = []
    bot.active_voice_profile_id = ""
    bot.web_search_enabled = False
    bot.no_reply_enabled = False
    bot.model_trigger_manage = "off"
    bot.sticker_mode = "off"
    bot.media_send_enabled = False
    bot.image_gen_endpoint = ""
    bot.image_gen_model = ""
    bot.image_gen_api_key = ""
    bot.global_memory_enabled = False
    bot.persona_state_enabled = False
    bot.reminders = None
    bot._pending_files = {}
    bot._pending_placeholders = {}
    bot._group_seen = {}
    bot._chat_is_group = {}
    bot._send_parts = mock.Mock(return_value=1)
    bot._deliver_reply = mock.Mock()
    bot._after_reply_sent = mock.Mock()
    bot._dispatch_and_notify = mock.Mock()
    bot._capture_media_rects = mock.Mock(return_value=[])
    bot._capture_emoji_images = mock.Mock(return_value=[])
    bot._box_screen_rect_safe = mock.Mock(return_value=None)
    bot._mark_session_read = mock.Mock()
    bot.file_storage_path = "/tmp"
    bot._find_file_by_display_name = mock.Mock(return_value="/tmp/报告.docx")
    bot._extract_file_text = mock.Mock(return_value="正文")
    bot._proactive_sched = mock.Mock()
    return bot


def _msg(sender, content, mtype=MessageType.TEXT, y=0):
    return WeChatMessage(id=f"visual_{y}", chat="摸鱼集团", sender=sender,
                         content=content, type=mtype, y=y)


def _blocks():
    return [
        {"top": 10, "kind": "text", "panel": (1, 2, 3, 4)},
        {"top": 50, "kind": "image", "media": (50, 120, 100, 200),
         "fingerprint": 111},
        {"top": 140, "kind": "text", "panel": None, "media": None,
         "fingerprint": 222},
        {"top": 200, "kind": "file", "panel": (200, 260, 100, 300)},
    ]


def _window_msgs():
    return [
        _msg("哆拉A萝", "诶诶 @小漓，你知道1+1等于几吗", y=10),
        _msg("哆拉A萝", "[图片]", MessageType.IMAGE, y=50),
        _msg("林小满", "[表情]", MessageType.EMOJI, y=140),
        _msg("林小满", "报告.docx", MessageType.FILE, y=200),
    ]


class TestGroupItems(unittest.TestCase):
    def test_items_keep_sender_and_placeholders(self):
        bot = _make_bot()
        items = bot._group_items("摸鱼集团", _window_msgs(), {"other_blocks": _blocks()})
        self.assertEqual([it["kind"] for it in items],
                         ["text", "image", "emoji", "file"])
        self.assertEqual(items[0]["content"],
                         "诶诶 @小漓，你知道1+1等于几吗")  # @ 原样保留
        self.assertEqual(items[1]["content"], "[图片]")
        self.assertEqual(items[2]["content"], "[表情]")
        self.assertEqual(items[3]["content"], "[文件: 报告.docx]")
        self.assertEqual([it["sender"] for it in items],
                         ["哆拉A萝", "哆拉A萝", "林小满", "林小满"])
        # 媒体签名用块指纹（滚动不变），文字签名用归一化正文
        self.assertEqual(items[1]["sig"][2], 111)
        self.assertEqual(items[2]["sig"][2], 222)

    def test_sender_name_only_text_becomes_emoji(self):
        bot = _make_bot()
        msgs = [_msg("哆拉A萝", "哆拉A萝", y=10)]
        blocks = [{"top": 10, "kind": "text", "panel": None, "media": None,
                   "fingerprint": 9}]
        items = bot._group_items("摸鱼集团", msgs, {"other_blocks": blocks})
        self.assertEqual(items[0]["kind"], "emoji")
        self.assertEqual(items[0]["content"], "[表情]")


class TestGroupCursor(unittest.TestCase):
    def _items(self):
        bot = _make_bot()
        return bot, bot._group_items("摸鱼集团", _window_msgs(),
                                     {"other_blocks": _blocks()})

    def test_only_new_suffix(self):
        bot, items = self._items()
        self.assertEqual(bot._group_peek_new("摸鱼集团", items), items)
        bot._group_commit("摸鱼集团", items)
        # 同一批再来：全部已见
        self.assertEqual(bot._group_peek_new("摸鱼集团", items), [])
        # 追加一条新消息：只返回新增段
        more = items + [bot._group_items(
            "摸鱼集团", [_msg("林小满", "新消息", y=260)],
            {"other_blocks": [{"top": 260, "kind": "text",
                               "panel": (1, 2, 3, 4)}]})[0]]
        self.assertEqual([it["content"] for it in
                          bot._group_peek_new("摸鱼集团", more)],
                         ["新消息"])

    def test_cursor_seeded_from_history_after_restart(self):
        """重启后内存游标为空：用历史里的 user 条目剥掉屏幕上的旧消息。"""
        bot = _make_bot()
        bot._add_history("摸鱼集团", "user", "哆拉A萝：昨晚聊到很晚")
        msgs = [_msg("哆拉A萝", "昨晚聊到很晚", y=10),
                _msg("林小满", "新消息", y=30)]
        blocks = [{"top": 10, "kind": "text", "panel": (1, 2, 3, 4)},
                  {"top": 30, "kind": "text", "panel": (1, 2, 3, 4)}]
        items = bot._group_items("摸鱼集团", msgs, {"other_blocks": blocks})
        new = bot._group_peek_new("摸鱼集团", items)
        self.assertEqual([it["content"] for it in new], ["新消息"])

    def test_repeated_short_message_keeps_new_copy(self):
        bot = _make_bot()
        one = bot._group_items(
            "摸鱼集团", [_msg("林小满", "哈哈", y=10)],
            {"other_blocks": [{"top": 10, "kind": "text",
                               "panel": (1, 2, 3, 4)}]})
        bot._group_commit("摸鱼集团", one)
        # 用户真发了两条「哈哈」：旧的是前缀，新的在后面 → 只留新的那条
        two = bot._group_items(
            "摸鱼集团", [_msg("林小满", "哈哈", y=10),
                         _msg("林小满", "哈哈", y=30)],
            {"other_blocks": [{"top": 10, "kind": "text",
                               "panel": (1, 2, 3, 4)},
                              {"top": 30, "kind": "text",
                               "panel": (1, 2, 3, 4)}]})
        self.assertEqual(len(bot._group_peek_new("摸鱼集团", two)), 1)
        # 同一条（只有一份、已入史）→ 不重复喂（否则沉默期间会无限重调）
        self.assertEqual(bot._group_peek_new("摸鱼集团", one), [])


class TestGroupSendGate(unittest.TestCase):
    def test_auto_text_without_action_is_silent(self):
        bot = _make_bot(mode="auto")
        bot._add_history = mock.Mock()
        bot._apply_group_result(
            "摸鱼集团", {"kind": "text", "content": "这句不该发",
                         "actions": [], "sent_any": False}, False)
        bot._deliver_reply.assert_not_called()
        bot._send_parts.assert_not_called()
        bot._add_history.assert_not_called()

    def test_auto_action_wins_over_trailing_text(self):
        bot = _make_bot(mode="auto")
        bot._add_history = mock.Mock()
        bot._apply_group_result(
            "摸鱼集团", {"kind": "text", "content": "尾部文本",
                         "actions": [{"type": "text", "content": "真正发出"}],
                         "sent_any": True}, False)
        bot._add_history.assert_called_once_with(
            "摸鱼集团", "assistant", "真正发出")
        bot._deliver_reply.assert_not_called()
        self.assertEqual(bot._group_seen, {})

    def test_hybrid_at_batch_sends_text(self):
        bot = _make_bot(mode="hybrid")
        bot._add_history = mock.Mock()
        bot._apply_group_result(
            "摸鱼集团", {"kind": "text", "content": "在呀",
                         "actions": [], "sent_any": False}, True)
        bot._add_history.assert_called_once_with(
            "摸鱼集团", "assistant", "在呀")
        bot._deliver_reply.assert_called_once_with("摸鱼集团", "在呀")

    def test_hybrid_at_batch_sticker_plus_text(self):
        bot = _make_bot(mode="hybrid")
        bot._add_history = mock.Mock()
        bot._apply_group_result(
            "摸鱼集团", {"kind": "text", "content": "顺便说一句",
                         "actions": [{"type": "sticker", "file": "猫猫.png"}],
                         "sent_any": True}, True)
        calls = [c.args for c in bot._add_history.call_args_list]
        self.assertIn(("摸鱼集团", "assistant", "[发送了表情包: 猫猫.png]"), calls)
        self.assertIn(("摸鱼集团", "assistant", "顺便说一句"), calls)
        bot._deliver_reply.assert_called_once_with("摸鱼集团", "顺便说一句")

    def test_no_reply_never_writes(self):
        bot = _make_bot(mode="auto")
        bot._add_history = mock.Mock()
        bot._apply_group_result(
            "摸鱼集团", {"kind": "tool_call", "name": "no_reply",
                         "arguments": "{}"}, False)
        bot._add_history.assert_not_called()


class TestToolDeclaration(unittest.TestCase):
    def test_tool_only_declares_send_message_not_no_reply(self):
        bot = _make_bot(mode="auto")
        bot.no_reply_enabled = True
        names = [t["function"]["name"] for t in bot._vision_tools(
            "摸鱼集团", tool_only=True)]
        self.assertIn("send_message", names)
        self.assertNotIn("no_reply", names)

    def test_force_no_reply_declared(self):
        bot = _make_bot(mode="auto")
        names = [t["function"]["name"] for t in bot._vision_tools(
            "某人", force_no_reply=True)]
        self.assertIn("no_reply", names)
        self.assertNotIn("send_message", names)

    def test_voice_always_drops_send_message(self):
        bot = _make_bot(mode="auto", voice="always")
        bot.tts_endpoint = "http://tts.test"
        bot.active_voice_profile_id = "v"
        bot.voice_profiles = [{"id": "v", "refs": {"通用": {
            "ref_audio_path": "/x.wav", "prompt_text": "hi"}}}]
        names = [t["function"]["name"] for t in bot._vision_tools(
            "摸鱼集团", tool_only=True)]
        self.assertNotIn("send_message", names)
        self.assertIn("send_voice", names)

    def test_voice_always_without_tts_keeps_send_message(self):
        # 语音配置不齐时 always 会回退文本：必须保留可用的文字通道
        bot = _make_bot(mode="auto", voice="always")
        names = [t["function"]["name"] for t in bot._vision_tools(
            "摸鱼集团", tool_only=True)]
        self.assertIn("send_message", names)


class TestVisionLoopSendMessage(unittest.TestCase):
    def test_send_message_executes_in_loop_and_returns_actions(self):
        bot = _make_bot(mode="auto")
        bot._exec_send_message = mock.Mock(return_value=(True, "已发送（1 段）"))
        bot._post_chat_completions = mock.Mock(side_effect=[
            {"choices": [{"message": {"tool_calls": [{
                "id": "t1", "function": {"name": "send_message",
                                          "arguments": '{"text": "在呀"}'}}]}}]},
            {"choices": [{"message": {"content": "（这句不该再发）"}}]},
        ])
        resp = bot.call_vision_api(
            [{"type": "text", "text": "hi"}], chat_id="摸鱼集团",
            tool_only=True)
        bot._exec_send_message.assert_called_once()
        self.assertTrue(resp["sent_any"])
        self.assertEqual(resp["actions"],
                         [{"type": "text", "content": "在呀"}])
        self.assertEqual(resp["content"], "（这句不该再发）")


class TestGroupBatch(unittest.TestCase):
    def test_silent_batch_records_every_message_without_assistant_mark(self):
        bot = _make_bot(mode="auto")
        bot.call_vision_api = mock.Mock(return_value={
            "kind": "text", "content": "", "actions": [], "sent_any": False})
        win = {"other_blocks": _blocks(), "bot_bottom": 0, "skip_bot": 0}
        handled = bot._handle_group_batch(
            "摸鱼集团", "林小满", win, _window_msgs(), "auto", "@小漓")
        self.assertTrue(handled)
        hist = bot._get_history("摸鱼集团")
        self.assertEqual([h["role"] for h in hist], ["user"] * 4)
        self.assertEqual(hist[0]["content"],
                         "哆拉A萝：诶诶 @小漓，你知道1+1等于几吗")
        self.assertEqual(hist[1]["content"], "哆拉A萝：[图片]")
        self.assertEqual(hist[2]["content"], "林小满：[表情]")
        self.assertEqual(hist[3]["content"], "林小满：[文件: 报告.docx]")
        bot._mark_session_read.assert_called_once()
        bot._deliver_reply.assert_not_called()
        # 游标已提交：同一批再来不会重复入史、也不会再调模型
        bot.call_vision_api.reset_mock()
        win2 = {"other_blocks": _blocks(), "bot_bottom": 0, "skip_bot": 0}
        # 无新内容 = 已处理（清圈），不是失败（不落 8s 退避）
        self.assertTrue(bot._handle_group_batch(
            "摸鱼集团", "林小满", win2, _window_msgs(), "auto", "@小漓"))
        bot.call_vision_api.assert_not_called()
        self.assertEqual(len(bot._get_history("摸鱼集团")), 4)

    def test_api_failure_does_not_record_or_advance_cursor(self):
        bot = _make_bot(mode="auto")
        bot.call_vision_api = mock.Mock(return_value=None)
        win = {"other_blocks": _blocks(), "bot_bottom": 0, "skip_bot": 0}
        self.assertIsNone(bot._handle_group_batch(
            "摸鱼集团", "林小满", win, _window_msgs(), "auto", "@小漓"))
        self.assertEqual(bot._get_history("摸鱼集团"), [])
        self.assertEqual(bot._group_seen, {})

    def test_non_at_file_not_extracted(self):
        bot = _make_bot(mode="auto")
        bot.call_vision_api = mock.Mock(return_value={
            "kind": "text", "content": "", "actions": [], "sent_any": False})
        msgs = [_msg("林小满", "看这个", y=10),
                _msg("林小满", "报告.docx", MessageType.FILE, y=200)]
        blocks = [{"top": 10, "kind": "text", "panel": (1, 2, 3, 4)},
                  {"top": 200, "kind": "file", "panel": (200, 260, 100, 300)}]
        win = {"other_blocks": blocks, "bot_bottom": 0, "skip_bot": 0}
        bot._handle_group_batch("摸鱼集团", "林小满", win, msgs,
                                "auto", "@小漓")
        # 只定位登记（供之后 @ 指令关联），不提取正文
        bot._find_file_by_display_name.assert_called_once_with("报告.docx")
        bot._extract_file_text.assert_not_called()
        self.assertIn("林小满", bot._pending_files)
        prompt = bot.call_vision_api.call_args.args[0][0]["text"]
        self.assertNotIn("正文", prompt)
        self.assertIn("林小满：[文件: 报告.docx]", prompt)


class _FakeWx:
    """最小视觉后端桩：一个群聊、两条消息、无媒体。"""

    _current_title = "摸鱼集团"
    _current_is_group = True

    def __init__(self, msgs, blocks):
        self._msgs = msgs
        self._blocks = blocks
        self.read = False

    def analyze_window(self, chat, foreground=True, skip_bot=0,
                       assume_switched=False):
        return {"bot_bottom": 0, "skip_bot": skip_bot, "has_other": True,
                "has_text": True, "has_media": False,
                "other_blocks": self._blocks}

    def get_messages(self, chat, assume_switched=False, skip_bot=0,
                     include_media=False):
        return self._msgs

    def mark_session_read(self):
        self.read = True


class TestFullModeBranch(unittest.TestCase):
    def test_session_dispatch_reaches_full_mode_without_at(self):
        bot = _make_bot(mode="auto")
        blocks = [{"top": 10, "kind": "text", "panel": (1, 2, 3, 4)},
                  {"top": 50, "kind": "text", "panel": (1, 2, 3, 4)}]
        msgs = [_msg("哆拉A萝", "今晚开黑吗", y=10),
                _msg("林小满", "我在", y=50)]
        bot.wx = _FakeWx(msgs, blocks)
        bot.call_vision_api = mock.Mock(return_value={
            "kind": "text", "content": "", "actions": [], "sent_any": False})
        handled = bot._handle_unread_session((100, 200), switched=True)
        self.assertTrue(handled)
        # 没 @ 也进模型（全接收），且走 tool_only
        self.assertTrue(bot.call_vision_api.call_args.kwargs["tool_only"])
        hist = bot._get_history("摸鱼集团")
        self.assertEqual([h["content"] for h in hist],
                         ["哆拉A萝：今晚开黑吗", "林小满：我在"])

    def test_hybrid_at_batch_allows_plain_text(self):
        bot = _make_bot(mode="hybrid")
        blocks = [{"top": 10, "kind": "text", "panel": (1, 2, 3, 4)}]
        msgs = [_msg("哆拉A萝", "诶诶 @小漓，1+1等于几", y=10)]
        bot.wx = _FakeWx(msgs, blocks)
        bot.call_vision_api = mock.Mock(return_value={
            "kind": "text", "content": "等于2呀", "actions": [],
            "sent_any": False})
        self.assertTrue(bot._handle_unread_session((100, 200), switched=True))
        self.assertFalse(bot.call_vision_api.call_args.kwargs["tool_only"])
        # @ 原文保留进模型，不剥 @
        prompt = bot.call_vision_api.call_args.args[0][0]["text"]
        self.assertIn("诶诶 @小漓，1+1等于几", prompt)
        bot._deliver_reply.assert_called_once_with("摸鱼集团", "等于2呀")


class TestVisualHelpers(unittest.TestCase):
    def test_media_sender_uses_line_above_media_only(self):
        lines = [
            {"text": "哆拉A萝", "x": 10, "y": 10, "w": 40, "h": 12},
            {"text": "图片里的字", "x": 20, "y": 60, "w": 60, "h": 12},
        ]
        # 媒体框 top=50：上方那行才是发送者，图片内部的字丢弃
        self.assertEqual(_media_sender(lines, (50, 120, 0, 100), "群"),
                         "哆拉A萝")
        self.assertEqual(_media_sender([], None, "群"), "群")

    def test_fingerprint_stable_and_content_sensitive(self):
        from PIL import Image, ImageDraw
        a = Image.new("RGB", (32, 32), "white")
        ImageDraw.Draw(a).rectangle((4, 4, 18, 18), fill="black")
        b = Image.new("RGB", (32, 32), "white")
        ImageDraw.Draw(b).rectangle((14, 14, 28, 28), fill="black")
        box = (0, 32, 0, 32)
        self.assertIsNotNone(_region_fingerprint(a, box))
        self.assertEqual(_region_fingerprint(a, box),
                         _region_fingerprint(a.copy(), box))
        self.assertNotEqual(_region_fingerprint(a, box),
                            _region_fingerprint(b, box))
        self.assertIsNone(_region_fingerprint(a, None))


class TestFailBackoff(unittest.TestCase):
    def test_exponential_capped(self):
        bot = AgentBot.__new__(AgentBot)
        bot._fail_backoff = 8.0
        self.assertEqual(bot._fail_delay(0), 8.0)
        self.assertEqual(bot._fail_delay(1), 8.0)
        self.assertEqual(bot._fail_delay(2), 16.0)
        self.assertEqual(bot._fail_delay(3), 32.0)
        self.assertEqual(bot._fail_delay(4), 60.0)
        self.assertEqual(bot._fail_delay(10), 60.0)


if __name__ == "__main__":
    unittest.main()
