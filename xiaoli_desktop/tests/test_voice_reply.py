# -*- coding: utf-8 -*-
"""语音回复链路测试：分段合成逐段发送、fail-closed 回退语义、纯文本记忆、
always 模式统一回复出口。

钉住的用户定案：
- 任一段合成失败 / 单段超上限 → 整条回退文本（未发出任何语音前绝不半途换模）
- 发送阶段某段 send_voice False（确认未发出）→ 剩余段回退文本
- 语音内容以纯文本写记忆，不加 [语音] 标记
- 语音段间无 2s 间隔（节奏由语音条自身时长承载）
- always 模式只覆盖模型对话回复；语音关/失败一律文本兜底
"""
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_bot import WeChatBot
from xiaoli_bot import AgentBot

PROFILE = {
    "id": "p1", "name": "三月七",
    "refs": {
        "通用": {"ref_audio_path": "/srv/ref.wav", "prompt_text": "通用文本"},
        "开心": {"ref_audio_path": "/srv/happy.wav", "prompt_text": "开心文本"},
    },
}


def _wav_file(seconds=0.5):
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))
    return buf.getvalue()


class _VoiceWx:
    """假后端：记录 send_voice / send_text；可指定第 N 次语音返回 False。"""

    def __init__(self, fail_idx=None, with_voice=True):
        self.voice_calls = []
        self.sent = []
        self.holds = []
        self.fail_idx = fail_idx
        if with_voice:
            self.send_voice = self._send_voice

    def _send_voice(self, chat, path):
        self.voice_calls.append((chat, path))
        if self.fail_idx is not None and len(self.voice_calls) - 1 == self.fail_idx:
            return False
        return True

    def send_text(self, chat, text, hold_after_paste=0.0):
        self.sent.append((chat, text))
        self.holds.append(hold_after_paste)


class FakeClient:
    """替身 GptSovitsClient：记录 (text, 参考路径)，返回真实小 wav。"""

    instances = []

    def __init__(self, endpoint, timeout=120):
        self.endpoint = endpoint
        self.timeout = timeout
        self.calls = []
        FakeClient.instances.append(self)

    def synthesize(self, text, ref):
        self.calls.append((text, ref["ref_audio_path"]))
        return _wav_file(0.5)


def _make_bot(mode="auto", profile=True, wx=None):
    bot = WeChatBot.__new__(WeChatBot)
    bot.voice_mode = mode
    bot.tts_endpoint = "http://127.0.0.1:9880/tts"
    bot.tts_timeout = 60
    bot.voice_max_seconds = 55
    bot.voice_profiles = [PROFILE] if profile else []
    bot.active_voice_profile_id = "p1" if profile else ""
    bot._pending_placeholders = {}
    bot.wx = wx if wx is not None else _VoiceWx()
    bot.sent_fallback = []
    bot._send_text = lambda text, chat, placeholder=False: \
        bot.sent_fallback.append((chat, text))
    return bot


class TestSendVoiceReply(unittest.TestCase):
    def setUp(self):
        FakeClient.instances = []
        self._patches = [
            unittest.mock.patch("wechat_bot.GptSovitsClient", FakeClient),
            unittest.mock.patch("wechat_bot.wav_duration_seconds",
                                lambda b: 0.5),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_voice_off_returns_false_without_synth(self):
        """无语音能力（配置缺）→ False 零合成。模式把关在调用方
        voice_state（能力层只认配置——per-chat 强制开时全局 mode 可 off）。"""
        bot = _make_bot(mode="off", profile=False)
        self.assertFalse(bot._send_voice_reply("小明", "你好"))
        self.assertEqual(FakeClient.instances, [])
        self.assertEqual(bot.wx.sent, [])
        self.assertEqual(bot.sent_fallback, [])

    def test_profile_missing_common_ref_disables_voice(self):
        bot = _make_bot()
        bot.voice_profiles = [{"id": "p1", "name": "坏档案", "refs": {
            "开心": PROFILE["refs"]["开心"]}}]
        self.assertIsNone(bot._voice_profile())
        self.assertFalse(bot._send_voice_reply("小明", "你好"))

    def test_splits_and_sends_per_part(self):
        """按回复分段逐段合成逐段发送；语音段间无额外间隔。"""
        bot = _make_bot()
        self.assertTrue(bot._send_voice_reply("小明", "甲。\n乙"))
        client = FakeClient.instances[-1]
        self.assertEqual(client.calls, [("甲", "/srv/ref.wav"),
                                        ("乙", "/srv/ref.wav")])
        self.assertEqual(len(bot.wx.voice_calls), 2)
        self.assertEqual(bot.sent_fallback, [])

    def test_emotion_picked_from_refs(self):
        bot = _make_bot()
        self.assertTrue(bot._send_voice_reply("小明", "你好", emotion="开心"))
        self.assertEqual(FakeClient.instances[-1].calls,
                         [("你好", "/srv/happy.wav")])

    def test_kaomoji_stripped_before_synth(self):
        """合成输入剥掉颜文字（真机实测：颜文字被 TTS 读成乱语）。"""
        bot = _make_bot()
        self.assertTrue(bot._send_voice_reply(
            "小明", "那就好，下回别趴着压肚子了(｡･ω･｡)"))
        self.assertEqual(FakeClient.instances[-1].calls,
                         [("那就好，下回别趴着压肚子了", "/srv/ref.wav")])

    def test_pure_kaomoji_reply_falls_back_to_text(self):
        """纯颜文字回复：剥完无可读文本 → 原文回退文本（对方至少看得到）；
        不发生任何合成请求。"""
        bot = _make_bot()
        self.assertFalse(bot._send_voice_reply("小明", "(≧▽≦)"))
        self.assertEqual(FakeClient.instances, [])
        self.assertEqual(bot.sent_fallback, [("小明", "(≧▽≦)")])

    def test_synth_failure_real(self):
        """合成阶段失败（真实触发路径）：整条回退**原文**（_send_text 自己
        再做拆段/剥句号），零语音发出。"""
        bot = _make_bot()

        class _Boom(FakeClient):
            def synthesize(self, text, ref):
                from xiaoli_app.tts import TtsError
                raise TtsError("端点炸了")

        with unittest.mock.patch("wechat_bot.GptSovitsClient", _Boom):
            self.assertFalse(bot._send_voice_reply("小明", "第一段。\n第二段"))
        self.assertEqual(bot.wx.voice_calls, [])
        self.assertEqual(bot.sent_fallback, [("小明", "第一段。\n第二段")])

    def test_segment_too_long_falls_back_whole(self):
        """单段超上限（60s 硬上限前的保险丝）→ 整条回退文本。"""
        bot = _make_bot()
        with unittest.mock.patch("wechat_bot.wav_duration_seconds",
                                 lambda b: 99.0):
            self.assertFalse(bot._send_voice_reply("小明", "一段\n二段"))
        self.assertEqual(bot.wx.voice_calls, [])
        self.assertEqual(bot.sent_fallback, [("小明", "一段\n二段")])

    def test_send_false_midway_falls_back_remaining(self):
        """第 2 段 send_voice False（该段确认未发出）→ 剩余段回退文本，
        已发出的语音段保持。"""
        bot = _make_bot(wx=_VoiceWx(fail_idx=1))
        self.assertFalse(bot._send_voice_reply("小明", "甲\n乙\n丙"))
        self.assertEqual(len(bot.wx.voice_calls), 2)
        self.assertEqual(bot.sent_fallback, [("小明", "乙\n丙")])

    def test_backend_without_voice_capability_falls_back(self):
        bot = _make_bot(wx=_VoiceWx(with_voice=False))
        self.assertFalse(bot._send_voice_reply("小明", "你好"))
        self.assertEqual(bot.sent_fallback, [("小明", "你好")])

    def test_trigger_fallback_uses_callback(self):
        """触发器语义：回退走注入的 text_fallback（旁路占位归零），不走 _send_text。"""
        bot = _make_bot(wx=_VoiceWx(with_voice=False))
        used = []
        self.assertFalse(bot._send_voice_reply(
            "小明", "你好", text_fallback=lambda t: used.append(t)))
        self.assertEqual(used, ["你好"])
        self.assertEqual(bot.sent_fallback, [])


class TestVoiceMemory(unittest.TestCase):
    """语音内容以纯文本写记忆（用户定案：不加 [语音] 标记）。"""

    def _mem_bot(self, tmp):
        bot = AgentBot.__new__(AgentBot)
        bot.memory_file = os.path.join(tmp, "memory.json")
        bot.memory_db = {}
        bot.max_history = 1000
        bot._memory_lock = threading.RLock()
        bot.memory_deep_enabled = False
        bot.memory_compress_enabled = False
        bot.memory_keep_recent = 30
        bot._deep_count = {}
        bot._deep_dir = ""
        # 语音能力齐全 + auto：_handle_send_voice 的 voice_state 闸要求有效
        # 模式非 off 才走到记忆写入（本组测试只钉记忆语义）
        bot.voice_mode = "auto"
        bot.tts_endpoint = "http://127.0.0.1:9880/tts"
        bot.voice_profiles = [PROFILE]
        bot.active_voice_profile_id = "p1"
        bot.chat_feature_overrides = {}
        return bot

    def test_handle_send_voice_writes_plain_text_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._mem_bot(tmp)
            recorded = []
            bot._send_voice_reply = (
                lambda chat, text, emotion=None, text_fallback=None:
                recorded.append((chat, text, emotion)) or True)
            result = {"kind": "tool_call", "name": "send_voice",
                      "arguments": json.dumps(
                          {"text": "啦啦啦～我是一条蓝色大肥鱼", "emotion": "开心"})}
            self.assertTrue(bot._handle_send_voice("小明", result,
                                                   user_text="给我发个语音"))
            recent = bot.memory_db["小明"]["recent"]
            self.assertEqual([m["content"] for m in recent],
                             ["给我发个语音", "啦啦啦～我是一条蓝色大肥鱼"])
            # assistant 条目 = 原话本身（用户消息里带「语音」二字属正常提问）
            self.assertEqual(recent[-1]["role"], "assistant")
            self.assertNotIn("[语音]", recent[-1]["content"])
            self.assertEqual(recorded, [("小明", "啦啦啦～我是一条蓝色大肥鱼", "开心")])

    def test_handle_send_voice_missing_text_degrades(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._mem_bot(tmp)
            result = {"kind": "tool_call", "name": "send_voice",
                      "arguments": json.dumps({"emotion": "开心"})}
            self.assertIsNone(bot._handle_send_voice("小明", result, None))
            self.assertEqual(bot.memory_db, {})


class TestDeliverReply(unittest.TestCase):
    """always 模式统一回复出口：语音成功不重发文本；失败回退文本；
    auto/off 恒走文本；trigger 语义分流。"""

    def _bot(self, mode, voice_ok):
        bot = AgentBot.__new__(AgentBot)
        bot.voice_mode = mode
        rec = {"voice": [], "text": [], "trigger": []}

        def _voice(chat, text, emotion=None, text_fallback=None):
            rec["voice"].append((chat, text))
            return voice_ok

        bot._send_voice_reply = _voice
        bot._send_text = lambda text, chat: rec["text"].append((chat, text))
        bot._send_trigger_reply = lambda text, chat: \
            rec["trigger"].append((chat, text))
        return bot, rec

    def test_always_voice_success_no_double_send(self):
        bot, rec = self._bot("always", voice_ok=True)
        bot._deliver_reply("小明", "语音说给你听")
        self.assertEqual(rec["voice"], [("小明", "语音说给你听")])
        self.assertEqual(rec["text"], [])

    def test_always_voice_failure_falls_back_text(self):
        bot, rec = self._bot("always", voice_ok=False)
        bot._deliver_reply("小明", "文本兜底")
        self.assertEqual(rec["text"], [("小明", "文本兜底")])

    def test_always_trigger_voice_failure_falls_back_trigger_semantics(self):
        bot, rec = self._bot("always", voice_ok=False)
        bot._deliver_reply("小明", "到点啦", trigger=True)
        self.assertEqual(rec["trigger"], [("小明", "到点啦")])
        self.assertEqual(rec["text"], [])

    def test_auto_and_off_go_text(self):
        for mode in ("auto", "off"):
            bot, rec = self._bot(mode, voice_ok=True)
            bot._deliver_reply("小明", "普通回复")
            self.assertEqual(rec["voice"], [])
            self.assertEqual(rec["text"], [("小明", "普通回复")])


if __name__ == "__main__":
    unittest.main()
