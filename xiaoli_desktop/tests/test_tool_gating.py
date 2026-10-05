# -*- coding: utf-8 -*-
"""工具注入开关测试：send_voice（voice_mode 三态 + 档案校验 + 情绪枚举动态
生成）与 web_search/web_fetch（web_search_enabled）。

call_vision_api 打桩 _post_chat_completions 捕获 payload，断言 tools 清单。
"""
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_bot import REPLY_MAX_TOKENS, WeChatBot

PROFILE = {
    "id": "p1", "name": "三月七",
    "refs": {
        "通用": {"ref_audio_path": "/srv/ref.wav", "prompt_text": "通用文本"},
        "开心": {"ref_audio_path": "/srv/happy.wav", "prompt_text": "开心文本"},
        "生气": {"ref_audio_path": "/srv/angry.wav", "prompt_text": "生气文本"},
    },
}


def _make_bot(voice_mode="off", profile=None, web_search=True):
    bot = WeChatBot.__new__(WeChatBot)
    bot.memory_file = os.path.join(tempfile.mkdtemp(), "memory.json")
    bot.system_prompt = "人设"
    bot.nickname = "小漓"
    bot.api_url = "https://api.test/v1/chat/completions"
    bot.api_key = "k"
    bot.vision_api_url = bot.api_url
    bot.vision_api_key = "k"
    bot.chat_model = "test-model"
    bot.chat_temperature = 0.7
    bot.reply_max_tokens = REPLY_MAX_TOKENS
    bot._model_lock = threading.RLock()
    bot.memory_compress_enabled = False
    bot.memory_deep_enabled = False
    bot.reminders = None
    bot.voice_mode = voice_mode
    bot.tts_endpoint = "http://127.0.0.1:9880/tts"
    bot.voice_profiles = [profile] if profile else []
    bot.active_voice_profile_id = (profile or {}).get("id", "")
    bot.web_search_enabled = web_search
    bot._post_chat_completions = mock.Mock(
        return_value={"choices": [{"message": {"content": "好"}}]})
    return bot


def _tool_names(bot):
    bot.call_vision_api([{"type": "text", "text": "hi"}])
    payload = bot._post_chat_completions.call_args.args[2]
    return [t["function"]["name"] for t in payload["tools"]]


def _voice_tool(bot):
    bot.call_vision_api([{"type": "text", "text": "hi"}])
    payload = bot._post_chat_completions.call_args.args[2]
    for t in payload["tools"]:
        if t["function"]["name"] == "send_voice":
            return t["function"]
    return None


class TestVoiceToolInjection(unittest.TestCase):
    def test_auto_with_valid_profile_injects_tool(self):
        bot = _make_bot(voice_mode="auto", profile=PROFILE)
        fn = _voice_tool(bot)
        self.assertIsNotNone(fn)
        self.assertIn("send_voice", _tool_names(bot))

    def test_emotion_enum_from_profile_refs(self):
        """emotion 枚举从档案 refs 键动态生成（不硬编码模型包情绪集）。"""
        bot = _make_bot(voice_mode="auto", profile=PROFILE)
        fn = _voice_tool(bot)
        self.assertEqual(fn["parameters"]["properties"]["emotion"]["enum"],
                         ["通用", "开心", "生气"])
        self.assertEqual(fn["parameters"]["required"], ["text"])

    def test_single_ref_profile_has_no_emotion_param(self):
        profile = {"id": "p1", "name": "x", "refs": PROFILE["refs"].copy()}
        del profile["refs"]["开心"], profile["refs"]["生气"]
        bot = _make_bot(voice_mode="auto", profile=profile)
        fn = _voice_tool(bot)
        self.assertNotIn("emotion", fn["parameters"]["properties"])

    def test_always_mode_injects_tool_for_emotion(self):
        """always 模式也注入工具——它是**强制回复通道**，模型每次都借 emotion
        挑情绪。描述按模式区分。"""
        bot = _make_bot(voice_mode="always", profile=PROFILE)
        fn = _voice_tool(bot)
        self.assertIsNotNone(fn)
        self.assertIn("send_voice", _tool_names(bot))
        self.assertIn("每一条回复", fn["description"])
        # auto 模式描述是「何时发语音」口径，两者必须可区分
        auto_fn = _voice_tool(_make_bot(voice_mode="auto", profile=PROFILE))
        self.assertNotEqual(fn["description"], auto_fn["description"])

    def test_always_mode_injects_voice_rules_system_message(self):
        """always 模式把「必须经 send_voice 回复」的纪律以 system 消息注入
        消息序列（工具描述之外的二次强调）；auto/off 不注入。"""
        bot = _make_bot(voice_mode="always", profile=PROFILE)
        bot.call_vision_api([{"type": "text", "text": "hi"}])
        payload = bot._post_chat_completions.call_args.args[2]
        self.assertTrue(any(
            m["role"] == "system" and "send_voice" in m.get("content", "")
            for m in payload["messages"]))

        bot = _make_bot(voice_mode="auto", profile=PROFILE)
        bot.call_vision_api([{"type": "text", "text": "hi"}])
        payload = bot._post_chat_completions.call_args.args[2]
        self.assertFalse(any(
            m["role"] == "system" and "send_voice" in m.get("content", "")
            for m in payload["messages"]))

    def test_off_mode_no_tool(self):
        bot = _make_bot(voice_mode="off", profile=PROFILE)
        self.assertNotIn("send_voice", _tool_names(bot))

    def test_missing_endpoint_no_tool(self):
        bot = _make_bot(voice_mode="auto", profile=PROFILE)
        bot.tts_endpoint = ""
        self.assertNotIn("send_voice", _tool_names(bot))

    def test_profile_missing_common_ref_no_tool(self):
        profile = {"id": "p1", "name": "坏档案", "refs": {
            "开心": PROFILE["refs"]["开心"]}}
        bot = _make_bot(voice_mode="auto", profile=profile)
        self.assertNotIn("send_voice", _tool_names(bot))


class TestVoiceProfileGating(unittest.TestCase):
    def test_mode_off_returns_none(self):
        bot = _make_bot(voice_mode="off", profile=PROFILE)
        self.assertIsNone(bot._voice_profile())

    def test_valid_config_returns_profile(self):
        bot = _make_bot(voice_mode="auto", profile=PROFILE)
        self.assertEqual(bot._voice_profile(), PROFILE)

    def test_active_id_not_found_returns_none(self):
        bot = _make_bot(voice_mode="auto", profile=PROFILE)
        bot.active_voice_profile_id = "不存在"
        self.assertIsNone(bot._voice_profile())


class TestWebSearchGating(unittest.TestCase):
    def test_enabled_declares_search_tools(self):
        bot = _make_bot(web_search=True)
        names = _tool_names(bot)
        self.assertIn("web_search", names)
        self.assertIn("web_fetch", names)

    def test_disabled_omits_search_tools(self):
        """关闭后工具整体不声明——模型看不到就不会调用。"""
        bot = _make_bot(web_search=False)
        names = _tool_names(bot)
        self.assertNotIn("web_search", names)
        self.assertNotIn("web_fetch", names)


if __name__ == "__main__":
    unittest.main()
