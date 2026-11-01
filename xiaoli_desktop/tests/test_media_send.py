# -*- coding: utf-8 -*-
"""B1 图片/文件发送测试：工具声明开关、路径安全边界、画图执行与回执。"""
import base64
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_bot import REPLY_MAX_TOKENS, WeChatBot, resolve_under_root

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def _make_bot(media_send=False, endpoint="", model=""):
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
    bot.chat_top_p = 0.9
    bot.reply_max_tokens = REPLY_MAX_TOKENS
    bot._model_lock = threading.RLock()
    bot.memory_compress_enabled = False
    bot.memory_deep_enabled = False
    bot.global_memory_enabled = False
    bot.persona_state_enabled = False
    bot.reminders = None
    bot.voice_mode = "off"
    bot.tts_endpoint = ""
    bot.voice_profiles = []
    bot.active_voice_profile_id = ""
    bot.web_search_enabled = True
    bot.no_reply_enabled = False
    bot.model_trigger_manage = "off"
    bot.sticker_mode = "off"
    bot.media_send_enabled = media_send
    bot.image_gen_endpoint = endpoint
    bot.image_gen_model = model
    bot.image_gen_api_key = ""
    bot._post_chat_completions = mock.Mock(
        return_value={"choices": [{"message": {"content": "好"}}]})
    return bot


def _tool_names(bot):
    bot.call_vision_api([{"type": "text", "text": "hi"}])
    payload = bot._post_chat_completions.call_args.args[2]
    return [t["function"]["name"] for t in payload["tools"]]


class TestMediaSendToolGating(unittest.TestCase):
    def test_off_not_declared(self):
        names = _tool_names(_make_bot(media_send=False))
        self.assertNotIn("send_image", names)
        self.assertNotIn("send_file", names)

    def test_on_declares_both(self):
        names = _tool_names(_make_bot(media_send=True,
                                      endpoint="http://x/img",
                                      model="sd"))
        self.assertIn("send_image", names)
        self.assertIn("send_file", names)

    def test_on_without_endpoint_still_declares(self):
        """端点未配置也声明（执行时回执告知）——与语音「配置齐全才注入」
        不同：发文件不依赖端点，且模型需要看到能力才能自然转告用户去开。"""
        names = _tool_names(_make_bot(media_send=True))
        self.assertIn("send_image", names)


class TestResolveUnderRoot(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.sub = os.path.join(self.root, "attachments")
        os.makedirs(self.sub)
        with open(os.path.join(self.sub, "报告.docx"), "wb") as f:
            f.write(b"x")

    def test_relative_hit(self):
        p = resolve_under_root(self.root, "attachments/报告.docx")
        self.assertIsNotNone(p)
        self.assertTrue(os.path.isfile(p))

    def test_directory_resolved_to_root_rejected(self):
        """「.」解析回根目录本身（且是目录）——不是文件，拒绝。"""
        self.assertIsNone(resolve_under_root(self.root, "."))

    def test_escape_rejected(self):
        self.assertIsNone(resolve_under_root(self.root, ".."))

    def test_absolute_path_rejected(self):
        outside = os.path.join(tempfile.mkdtemp(), "memory.json")
        with open(outside, "wb") as f:
            f.write(b"{}")
        self.assertIsNone(resolve_under_root(self.root, outside))

    def test_missing_file_rejected(self):
        self.assertIsNone(resolve_under_root(self.root, "attachments/不存在.pdf"))

    def test_empty_rejected(self):
        self.assertIsNone(resolve_under_root(self.root, ""))
        self.assertIsNone(resolve_under_root("", "x"))


class TestExecSendImage(unittest.TestCase):
    def _bot(self, **kw):
        from xiaoli_bot import AgentBot
        bot = AgentBot.__new__(AgentBot)
        bot.image_gen_endpoint = kw.get("endpoint", "http://x/img")
        bot.image_gen_model = kw.get("model", "sd")
        bot.image_gen_api_key = ""
        bot.tasks_dir = tempfile.mkdtemp()
        bot._send_sticker_file = mock.Mock(return_value=True)
        return bot

    def test_missing_endpoint_receipt(self):
        bot = self._bot(endpoint="", model="")
        r = bot._exec_send_image("林小满", json.dumps({"prompt": "鲸鱼"}))
        self.assertIn("画图端点", r)

    def test_missing_prompt_receipt(self):
        bot = self._bot()
        self.assertIn("prompt", bot._exec_send_image("林小满", "{}"))

    def test_success_sends_and_cleans_temp(self):
        bot = self._bot()
        tmp = os.path.join(tempfile.mkdtemp(), "gen.png")
        with open(tmp, "wb") as f:
            f.write(PNG_1PX)
        with mock.patch("xiaoli_app.image_gen.generate_image_file",
                        return_value=tmp) as gen:
            r = bot._exec_send_image("林小满",
                                     json.dumps({"prompt": "一只鲸鱼"}))
        self.assertIn("已发送", r)
        gen.assert_called_once()
        self.assertFalse(os.path.exists(tmp))   # 发送后临时文件即删
        bot._send_sticker_file.assert_called_once_with(tmp)

    def test_generation_failure_is_receipt_not_raise(self):
        bot = self._bot()
        with mock.patch("xiaoli_app.image_gen.generate_image_file",
                        side_effect=Exception("boom")):
            r = bot._exec_send_image("林小满",
                                     json.dumps({"prompt": "鲸鱼"}))
        self.assertIn("没画出来", r)


class TestExecSendFile(unittest.TestCase):
    def _bot(self):
        from xiaoli_bot import AgentBot
        bot = AgentBot.__new__(AgentBot)
        bot.tasks_dir = tempfile.mkdtemp()
        bot._send_file_clipboard = mock.Mock(return_value=True)
        return bot

    def test_success(self):
        bot = self._bot()
        with open(os.path.join(bot.tasks_dir, "成果.docx"), "wb") as f:
            f.write(b"x")
        r = bot._exec_send_file("林小满", json.dumps({"file": "成果.docx"}))
        self.assertIn("已发送", r)
        bot._send_file_clipboard.assert_called_once()

    def test_outside_root_rejected(self):
        bot = self._bot()
        r = bot._exec_send_file("林小满",
                                json.dumps({"file": "../memory.json"}))
        self.assertIn("发不了", r)
        bot._send_file_clipboard.assert_not_called()

    def test_send_failure_receipt(self):
        bot = self._bot()
        with open(os.path.join(bot.tasks_dir, "a.txt"), "wb") as f:
            f.write(b"x")
        bot._send_file_clipboard.return_value = False
        r = bot._exec_send_file("林小满", json.dumps({"file": "a.txt"}))
        self.assertIn("失败", r)


if __name__ == "__main__":
    unittest.main()
