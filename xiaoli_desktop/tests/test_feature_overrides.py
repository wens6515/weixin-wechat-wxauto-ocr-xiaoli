# -*- coding: utf-8 -*-
"""per-chat 功能覆盖（三态例外）测试：跟随全局 / 强制开 / 强制关 的查询
矩阵，与各生效点的 fail-closed 拒绝路径。

钉住的定案：
- 全局开关是总闸，未设置的聊天（含新聊天）跟随全局
- web_search/task/state_watch 强制开优先于全局关（覆盖 = 单聊放开）
- voice 强制关 → off（任何基线）；强制开 → 全局 off 且配置齐全时升 auto，
  配置不齐开不出能力维持 off
- dispatch_task / send_voice 工具在该聊天功能关闭时被拒绝（降级普通聊天）
- 状态监视未开启（全局与按聊天均未放开）→ 友好告知而非凭空答应
"""
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app import config_store
from xiaoli_app.reminders_store import RemindersStore
from xiaoli_bot import (AgentBot, VISION_CHAT_PROMPT, VISION_NEUTRAL_PROMPT,
                        VISION_ROUTE_PROMPT)
from wechat_bot import WeChatBot

PROFILE = {
    "id": "p1", "name": "测试音色",
    "refs": {"通用": {"ref_audio_path": "/srv/ref.wav",
                      "prompt_text": "通用文本"}},
}


def _make_bot(**attrs):
    """AgentBot 测试桩（__new__ 直构，不连微信）。reminders 用临时路径——
    默认路径指向真实数据目录，条目存在会让轮询闸断言失真。"""
    tmp = tempfile.mkdtemp(prefix="xiaoli_fovr_")
    b = AgentBot.__new__(AgentBot)
    b.chat_feature_overrides = {}
    b.web_search_enabled = True
    b.task_enabled = True
    b.state_watch_enabled = False
    b.voice_mode = "off"
    b.tts_endpoint = ""
    b.voice_profiles = []
    b.active_voice_profile_id = ""
    b.tts_timeout = 60
    b.voice_max_seconds = 55
    b.nickname = "小漓"
    b.api_url = "http://x"
    b.api_key = "k"
    b.chat_model = "m"
    b._pending_placeholders = {}
    b._pending_files = {}
    b.memory_db = {}
    b.memory_file = os.path.join(tmp, "mem.json")
    b.max_history = 1000
    b._memory_lock = threading.RLock()
    b.memory_deep_enabled = False
    b.memory_compress_enabled = False
    b.memory_keep_recent = 30
    b._deep_count = {}
    b._deep_dir = ""
    b.reminders = RemindersStore(path=os.path.join(tmp, "reminders.json"))
    for k, v in attrs.items():
        setattr(b, k, v)
    return b


class TestFeatureEnabled(unittest.TestCase):
    """feature_enabled 三态矩阵（web_search / task / state_watch）。"""

    def test_follows_global_by_default(self):
        b = _make_bot()
        for feat, base in (("web_search", True), ("task", True),
                           ("state_watch", False)):
            self.assertEqual(b.feature_enabled("林小满", feat), base,
                             f"{feat} 无覆盖时应跟随全局")

    def test_global_off_override_on_wins(self):
        """全局关 + 单聊强制开 = 该聊天放开（覆盖语义：单聊单独放开）。"""
        b = _make_bot(web_search_enabled=False, task_enabled=False)
        b.chat_feature_overrides = {
            "林小满": {"web_search": True, "task": True}}
        self.assertTrue(b.feature_enabled("林小满", "web_search"))
        self.assertTrue(b.feature_enabled("林小满", "task"))
        # 其他聊天仍跟随全局关
        self.assertFalse(b.feature_enabled("别人", "task"))

    def test_global_on_override_off_wins(self):
        """全局开 + 单聊强制关 = 该聊天关闭。"""
        b = _make_bot(task_enabled=True, web_search_enabled=True)
        b.chat_feature_overrides = {
            "林小满": {"web_search": False, "task": False}}
        self.assertFalse(b.feature_enabled("林小满", "web_search"))
        self.assertFalse(b.feature_enabled("林小满", "task"))
        self.assertTrue(b.feature_enabled("别人", "task"))

    def test_key_normalized_against_ocr_variants(self):
        """覆盖键走 memory 口径：OCR 引号/空白变体不分裂同一会话。"""
        b = _make_bot(task_enabled=True)
        b.chat_feature_overrides = {"摸鱼集团": {"task": False}}
        self.assertFalse(b.feature_enabled("“摸鱼”集团", "task"))
        self.assertFalse(b.feature_enabled("摸鱼 ” 集团", "task"))

    def test_empty_chat_id_falls_back_to_global(self):
        b = _make_bot(task_enabled=False)
        self.assertFalse(b.feature_enabled(None, "task"))
        self.assertFalse(b.feature_enabled("", "task"))


class TestVoiceState(unittest.TestCase):
    """voice_state 三态矩阵（off/auto/always × 配置齐全与否）。"""

    def test_global_off_default_off(self):
        b = _make_bot()
        self.assertEqual(b.voice_state("林小满"), "off")

    def test_override_on_without_config_stays_off(self):
        """强制开但 TTS 配置不齐 → 开不出能力，维持 off。"""
        b = _make_bot()
        b.chat_feature_overrides = {"林小满": {"voice": True}}
        self.assertEqual(b.voice_state("林小满"), "off")

    def test_override_on_with_config_becomes_auto(self):
        """全局 off + 配置齐全 + 单聊强制开 = 该聊天 auto（单独给某个
        朋友开语音的核心场景）。"""
        b = _make_bot(tts_endpoint="http://127.0.0.1:9880/tts",
                      voice_profiles=[PROFILE],
                      active_voice_profile_id="p1")
        b.chat_feature_overrides = {"林小满": {"voice": True}}
        self.assertEqual(b.voice_state("林小满"), "auto")
        # 其他聊天仍 off
        self.assertEqual(b.voice_state("别人"), "off")

    def test_global_always_override_off(self):
        """全局 always + 单聊强制关 = 该聊天回退文本。"""
        b = _make_bot(voice_mode="always",
                      tts_endpoint="http://x",
                      voice_profiles=[PROFILE],
                      active_voice_profile_id="p1")
        b.chat_feature_overrides = {"林小满": {"voice": False}}
        self.assertEqual(b.voice_state("林小满"), "off")
        self.assertEqual(b.voice_state("别人"), "always")

    def test_global_auto_kept_when_override_on(self):
        b = _make_bot(voice_mode="auto",
                      tts_endpoint="http://x",
                      voice_profiles=[PROFILE],
                      active_voice_profile_id="p1")
        b.chat_feature_overrides = {"林小满": {"voice": True}}
        self.assertEqual(b.voice_state("林小满"), "auto")


class TestApplyVisionResultGating(unittest.TestCase):
    """生效点 fail-closed：功能关闭时工具调用被拒绝。"""

    def test_dispatch_task_rejected_when_task_off(self):
        b = _make_bot()
        b.chat_feature_overrides = {"林小满": {"task": False}}
        result = {"kind": "tool_call", "name": "dispatch_task",
                  "arguments": '{"task": "做个网站"}'}
        sent = []
        b._send_text = lambda text, chat, placeholder=False: \
            sent.append((chat, text))
        self.assertIsNone(b._apply_vision_result("林小满", "王", result))
        self.assertEqual(sent, [])

    def test_dispatch_task_allowed_when_task_on(self):
        b = _make_bot()
        dispatched = []
        b._dispatch_and_notify = \
            lambda chat, sender, desc, attachment_paths=None, extra=None: \
            dispatched.append((chat, desc))
        result = {"kind": "tool_call", "name": "dispatch_task",
                  "arguments": '{"task": "做个网站"}'}
        self.assertTrue(
            b._apply_vision_result("林小满", "王", result, user_text="做个网站"))
        self.assertEqual(dispatched, [("林小满", "做个网站")])

    def test_send_voice_rejected_when_voice_off(self):
        b = _make_bot()
        b.chat_feature_overrides = {"林小满": {"voice": False}}
        result = {"kind": "tool_call", "name": "send_voice",
                  "arguments": '{"text": "你好呀"}'}
        self.assertIsNone(b._handle_send_voice("林小满", result, user_text=None))

    def test_condition_watch_tells_user_when_disabled(self):
        """状态监视未开启（全局关 + 无覆盖）→ 友好告知，不凭空答应。"""
        b = _make_bot()
        sent = []
        b._send_text = lambda text, chat, placeholder=False: \
            sent.append((chat, text))
        args = {"url": "https://example.com", "condition": "下雨",
                "judge": "local", "met_keywords": ["雨"]}
        r = b._create_condition_watch(
            "林小满", args, user_text="下雨了提醒我")
        self.assertTrue(r)
        self.assertEqual(len(sent), 1)
        self.assertIn("状态监视", sent[0][1])

    def test_condition_watch_creates_with_chat_override_on(self):
        """全局关 + 单聊强制开 → 该聊天可创建监视（轮询靠条目存在驱动）。"""
        tmp = tempfile.mkdtemp(prefix="xiaoli_fovr_rem_")
        b = _make_bot(reminders=RemindersStore(
            path=os.path.join(tmp, "reminders.json")))
        b.chat_feature_overrides = {"林小满": {"state_watch": True}}
        args = {"url": "https://example.com", "condition": "下雨",
                "judge": "local", "met_keywords": ["雨"]}
        r = b._create_condition_watch("林小满", args, user_text="下雨了提醒我")
        self.assertTrue(r)
        self.assertEqual(len(b.reminders.list_conditions()), 1)

    def test_state_watch_polling_active_with_entries_only(self):
        """全局关但存在监视条目 → 轮询保持活跃（单聊强制开的条目即事实源）。"""
        b = _make_bot()
        self.assertFalse(b._state_watch_polling_active())
        b.reminders.add_condition("林小满", "", "https://example.com", "下雨")
        self.assertTrue(b._state_watch_polling_active())


class TestRoutePromptVariant(unittest.TestCase):
    """任务桥关闭时 vision prompt 换变体：其他工具有效（语音/搜索/闹钟）
    → 中性变体（不点名 dispatch_task，也不说「不要调用任何工具」——那会
    压制已声明工具）；一个工具都没有 → 纯聊天变体（防幻觉句保留）。"""

    def test_prompt_constant_mentions_no_tool(self):
        self.assertNotIn("dispatch_task", VISION_CHAT_PROMPT)
        self.assertNotIn("dispatch_task", VISION_NEUTRAL_PROMPT)
        self.assertIn("dispatch_task", VISION_ROUTE_PROMPT)

    def test_vision_route_uses_neutral_prompt_when_task_off_with_tools(self):
        """任务桥关 + 闹钟工具可用（reminders 存在）→ 中性变体。"""
        b = _make_bot()
        b.chat_feature_overrides = {"林小满": {"task": False}}
        captured = {}

        def fake_call_vision_api(content, chat_id=None, related_memory=None):
            captured["text"] = content[0]["text"]
            return {"kind": "text", "content": "嗯嗯"}

        b.call_vision_api = fake_call_vision_api
        b._deliver_reply = lambda chat, text, trigger=False: None
        r = b._vision_route("林小满", "王", "在吗")
        self.assertTrue(r)
        self.assertIn(VISION_NEUTRAL_PROMPT, captured["text"])
        self.assertNotIn("不要调用任何工具", captured["text"],
                         "其他工具可用时不得注入抑制句（会压制 set_reminder 等）")
        self.assertNotIn(VISION_ROUTE_PROMPT, captured["text"])

    def test_vision_route_uses_chat_prompt_when_no_tools(self):
        """任务桥关 + 其他工具全不可用（无 reminders/语音/搜索/深层记忆）
        → 纯聊天变体（防幻觉句保留）。"""
        b = _make_bot(reminders=None, web_search_enabled=False,
                      memory_deep_enabled=False, voice_mode="off",
                      tts_endpoint="")
        b.chat_feature_overrides = {"林小满": {"task": False}}
        captured = {}

        def fake_call_vision_api(content, chat_id=None, related_memory=None):
            captured["text"] = content[0]["text"]
            return {"kind": "text", "content": "嗯嗯"}

        b.call_vision_api = fake_call_vision_api
        b._deliver_reply = lambda chat, text, trigger=False: None
        b._vision_route("林小满", "王", "在吗")
        self.assertIn(VISION_CHAT_PROMPT, captured["text"])

    def test_vision_route_uses_route_prompt_when_task_on(self):
        b = _make_bot()
        captured = {}

        def fake_call_vision_api(content, chat_id=None, related_memory=None):
            captured["text"] = content[0]["text"]
            return {"kind": "text", "content": "嗯嗯"}

        b.call_vision_api = fake_call_vision_api
        b._deliver_reply = lambda chat, text, trigger=False: None
        b._vision_route("林小满", "王", "在吗")
        self.assertIn(VISION_ROUTE_PROMPT, captured["text"])


class TestSanitizeFeatureOverrides(unittest.TestCase):
    """config_store 清洗：键归一化、白名单、bool-only、空条目删除。"""

    def test_sanitize_normalizes_and_filters(self):
        cfg = {"chat_feature_overrides": {
            "“摸鱼”集团 ": {"task": False, "bogus": True},
            "林小满": {"voice": "yes"},          # 非 bool → 丢弃 → 空条目删除
            "": {"task": True},                  # 空键丢弃
            "小明": "bad",                        # 非 dict 丢弃
        }}
        out = config_store.sanitize_feature_overrides(cfg)
        table = out["chat_feature_overrides"]
        self.assertEqual(table, {"摸鱼集团": {"task": False}})

    def test_sanitize_resets_non_dict(self):
        out = config_store.sanitize_feature_overrides(
            {"chat_feature_overrides": "bad"})
        self.assertEqual(out["chat_feature_overrides"], {})

    def test_load_config_store_fills_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"bot_nickname": "x"}, f)
            cfg = config_store.load_config_store(
                path, os.path.join(tmp, "cards"))
            self.assertEqual(cfg["chat_feature_overrides"], {})
            self.assertIn("chat_feature_overrides", cfg)


if __name__ == "__main__":
    unittest.main()
