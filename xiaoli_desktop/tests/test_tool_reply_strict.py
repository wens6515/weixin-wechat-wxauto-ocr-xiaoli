# -*- coding: utf-8 -*-
"""严格工具化回复：文本只能经 send_message 发出。

用户定案：模型对外的每一种动作都必须经工具调用——直接输出的文字一律
丢弃（群聊全接收语义推广到全部链路）。本文件覆盖：
- 工具声明：strict 全链路声明 send_message；compat 私聊不声明（防重发）
- 文本处理：strict 丢弃 + 可配置纠正一次；tool_only（群聊全接收）不纠偏
- 发送闸：动作已在工具循环内发出，上层只入史/收尾，绝不重发
- hybrid @：没调工具就不回复（引擎不兜底发文本）
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_bot import AgentBot
from wechat_bot import TOOL_REPLY_RETRY_HINT, REPLY_MAX_TOKENS


def _make_bot(strict=True, retry=True, no_reply=False):
    """AgentBot 直构桩：只装 call_vision_api / 分流链路用到的属性。"""
    bot = AgentBot.__new__(AgentBot)
    bot.nickname = "小漓"
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
    bot.max_context_tokens = 100000
    bot._model_lock = threading.RLock()
    bot.voice_mode = "off"
    bot.tts_endpoint = ""
    bot.voice_profiles = []
    bot.active_voice_profile_id = ""
    bot.web_search_enabled = False
    bot.no_reply_enabled = no_reply
    bot.model_trigger_manage = "off"
    bot.sticker_mode = "off"
    bot.media_send_enabled = False
    bot.global_memory_enabled = False
    bot.persona_state_enabled = False
    bot.task_enabled = False
    bot.reminders = None
    bot._pending_placeholders = {}
    bot._group_seen = {}
    bot._chat_is_group = {}
    bot.tool_reply_mode = "strict" if strict else "compat"
    bot.tool_reply_retry = retry
    bot._send_parts = mock.Mock(return_value=1)
    bot._deliver_reply = mock.Mock()
    bot._after_reply_sent = mock.Mock()
    bot._add_history = mock.Mock()
    bot._handle_no_reply = mock.Mock(return_value=True)
    bot._clear_group_cursor = mock.Mock()
    bot._ensure_chat_active = mock.Mock(return_value=True)
    bot._get_history = lambda chat: []
    bot._important_block = lambda chat: None
    bot._global_memory_block = lambda: None
    bot._match_global_memory = lambda text: None
    bot._persona_state_block = lambda chat: None
    bot._match_related_memory = lambda chat, text: None
    return bot


def _text_resp(text):
    return {"choices": [{"message": {"content": text}}]}


def _tool_resp(name, args):
    return {"choices": [{"message": {
        "content": "",
        "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }],
    }}]}


class TestToolDeclaration(unittest.TestCase):
    def test_strict_private_declares_send_message(self):
        bot = _make_bot(strict=True)
        names = [t["function"]["name"] for t in bot._vision_tools("联系人丁")]
        self.assertIn("send_message", names)

    def test_compat_private_does_not_declare_send_message(self):
        """compat 下不声明：否则工具发出 + 文本再发一次 = 重复消息。"""
        bot = _make_bot(strict=False)
        names = [t["function"]["name"] for t in bot._vision_tools("联系人丁")]
        self.assertNotIn("send_message", names)

    def test_strict_proactive_declares_send_message_and_no_reply(self):
        bot = _make_bot(strict=True)
        names = [t["function"]["name"] for t in bot._vision_tools(
            "联系人丁", force_no_reply=True)]
        self.assertIn("send_message", names)
        self.assertIn("no_reply", names)


class TestTextHandling(unittest.TestCase):
    def test_strict_text_only_repairs_once_then_sends_via_tool(self):
        bot = _make_bot(strict=True, retry=True)
        bot._post_chat_completions = mock.Mock(side_effect=[
            _text_resp("你好呀"), _tool_resp("send_message", {"text": "你好呀"})])
        resp = bot.call_vision_api([{"type": "text", "text": "在吗"}],
                                   chat_id="联系人丁")
        self.assertEqual(resp["kind"], "text")
        self.assertEqual(resp["content"], "")
        self.assertEqual(resp["actions"], [{"type": "text", "content": "你好呀"}])
        bot._send_parts.assert_called_once()
        # 第二次请求带纠偏提示
        second = bot._post_chat_completions.call_args_list[1][0][2]
        self.assertTrue(any(TOOL_REPLY_RETRY_HINT in str(m.get("content"))
                            for m in second["messages"]))

    def test_strict_retry_off_drops_text(self):
        bot = _make_bot(strict=True, retry=False)
        bot._post_chat_completions = mock.Mock(
            side_effect=[_text_resp("你好呀")])
        resp = bot.call_vision_api([{"type": "text", "text": "在吗"}],
                                   chat_id="联系人丁")
        self.assertEqual(resp["content"], "")
        self.assertEqual(resp["dropped_text"], "你好呀")
        self.assertEqual(resp["actions"], [])
        bot._send_parts.assert_not_called()
        self.assertEqual(bot._post_chat_completions.call_count, 1)

    def test_strict_retry_exhausted_drops(self):
        bot = _make_bot(strict=True, retry=True)
        bot._post_chat_completions = mock.Mock(side_effect=[
            _text_resp("你好呀"), _text_resp("你好呀")])
        resp = bot.call_vision_api([{"type": "text", "text": "在吗"}],
                                   chat_id="联系人丁")
        self.assertEqual(resp["content"], "")
        self.assertEqual(resp["dropped_text"], "你好呀")
        bot._send_parts.assert_not_called()

    def test_tool_only_text_is_not_repaired(self):
        """群聊全接收：沉默是正常态，纯文本不纠偏（否则 auto 变全回复）。"""
        bot = _make_bot(strict=True, retry=True)
        bot._post_chat_completions = mock.Mock(
            side_effect=[_text_resp("随便说点")])
        resp = bot.call_vision_api([{"type": "text", "text": "群里聊天"}],
                                   chat_id="闲聊集团", tool_only=True)
        self.assertEqual(resp["content"], "")
        self.assertEqual(resp["dropped_text"], "随便说点")
        self.assertEqual(bot._post_chat_completions.call_count, 1)

    def test_trailing_text_after_send_is_not_requested(self):
        """发送成功后直接收尾：不再多要一轮（尾随文字连请求都不会发生）。"""
        bot = _make_bot(strict=True, retry=True)
        bot._post_chat_completions = mock.Mock(side_effect=[
            _tool_resp("send_message", {"text": "好呀"}),
        ])
        resp = bot.call_vision_api([{"type": "text", "text": "在吗"}],
                                   chat_id="联系人丁")
        self.assertEqual(resp["content"], "")
        self.assertEqual(resp["actions"], [{"type": "text", "content": "好呀"}])
        self.assertEqual(bot._post_chat_completions.call_count, 1)

    def test_compat_text_is_returned(self):
        bot = _make_bot(strict=False)
        bot._post_chat_completions = mock.Mock(
            side_effect=[_text_resp("你好呀")])
        resp = bot.call_vision_api([{"type": "text", "text": "在吗"}],
                                   chat_id="联系人丁")
        self.assertEqual(resp["kind"], "text")
        self.assertEqual(resp["content"], "你好呀")

    def test_user_prompt_carries_send_instruction(self):
        """严格模式下「必须调 send_message」也写进本轮 user 文本。

        任务桥开启时路由 prompt 里有「普通闲聊……不要调用任何工具」，与发送
        纪律冲突——真机因此频繁只输出文字（不回消息）。"""
        bot = _make_bot(strict=True)
        bot._post_chat_completions = mock.Mock(
            side_effect=[_tool_resp("send_message", {"text": "好"})])
        bot.call_vision_api([{"type": "text", "text": "在吗"}],
                            chat_id="联系人丁")
        payload = bot._post_chat_completions.call_args_list[0][0][2]
        user = [m for m in payload["messages"] if m.get("role") == "user"][-1]
        self.assertIn("必须调用 send_message", str(user.get("content")))

    def test_repair_passes_reasoning_content_back(self):
        """纠偏轮的 assistant 必须带 reasoning_content。

        DeepSeek thinking 模型拒绝缺少该字段的 assistant 消息（真机 400：
        The `reasoning_content` in the thinking mode must be passed back），
        修复前纠偏 100% 失败 → 用户看到的是完全不回消息。"""
        bot = _make_bot(strict=True)
        first = {"choices": [{"message": {
            "content": "你好呀", "reasoning_content": "先想想要不要回"}}]}
        bot._post_chat_completions = mock.Mock(side_effect=[
            first, _tool_resp("send_message", {"text": "你好呀"})])
        resp = bot.call_vision_api([{"type": "text", "text": "在吗"}],
                                   chat_id="联系人丁")
        self.assertTrue(resp["sent_any"])
        payload = bot._post_chat_completions.call_args_list[1][0][2]
        # payload["messages"] 是同一个列表引用（后续轮次会继续 append），
        # 这里按内容找纠偏轮那条 assistant，而不是取最后一条（最后一条是
        # send_message 的 tool_calls assistant）。
        assistants = [m for m in payload["messages"]
                      if m.get("role") == "assistant"
                      and m.get("content") == "你好呀"]
        self.assertTrue(assistants)
        self.assertEqual(assistants[-1].get("reasoning_content"),
                         "先想想要不要回")


class TestCommitGate(unittest.TestCase):
    def test_actions_commit_history_without_resending(self):
        bot = _make_bot(strict=True)
        resp = {"kind": "text", "content": "",
                "actions": [{"type": "text", "content": "好呀"}]}
        ok = bot._apply_vision_result("联系人丁", "王", resp,
                                      user_text="私聊 - 王：在吗")
        self.assertTrue(ok)
        bot._deliver_reply.assert_not_called()
        self.assertEqual(bot._send_parts.call_count, 0)
        roles = [c[0][1] for c in bot._add_history.call_args_list]
        self.assertEqual(roles, ["user", "assistant"])
        bot._after_reply_sent.assert_called_once_with("联系人丁", "好呀")

    def test_no_actions_is_silence(self):
        bot = _make_bot(strict=True)
        resp = {"kind": "text", "content": "hi"}
        bot._apply_vision_result("联系人丁", "王", resp, user_text="在吗")
        bot._deliver_reply.assert_not_called()
        bot._handle_no_reply.assert_called_once()

    def test_hybrid_at_without_tool_does_not_reply(self):
        """@必回也走工具：没调 send_message 就不发，引擎不兜底。"""
        bot = _make_bot(strict=True)
        bot._apply_group_result("闲聊集团", {"kind": "text", "content": "来了"},
                                force_text_send=True)
        bot._deliver_reply.assert_not_called()
        bot._send_parts.assert_not_called()


if __name__ == "__main__":
    unittest.main()
