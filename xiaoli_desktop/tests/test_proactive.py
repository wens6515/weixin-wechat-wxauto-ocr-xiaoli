# -*- coding: utf-8 -*-
"""主动发起对话测试：时长文案、调度到期、安静时段、判定与发送护栏。"""
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_bot import AgentBot
from xiaoli_app.bot_daemons import ProactiveScheduler


def _ts_ago(seconds):
    return time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(time.time() - seconds))


def _make_bot(**kw):
    bot = AgentBot.__new__(AgentBot)
    bot.nickname = "小漓"
    bot.paused = False
    bot._sending_lock = False
    bot.tasks_dir = tempfile.mkdtemp()
    bot.proactive_enabled = True
    bot.proactive_interval_min = 10
    bot.proactive_interval_max = 20
    bot.proactive_idle_minutes = 10
    bot.proactive_cooldown_minutes = 30
    bot.proactive_private_only = True
    bot.proactive_active_days = 7
    bot.proactive_quiet_enabled = False
    bot.proactive_quiet_start = "23:00"
    bot.proactive_quiet_end = "08:00"
    bot._proactive_queue = queue.Queue(maxsize=50)
    bot._proactive_sched = mock.Mock()
    bot._budget_exhausted = mock.Mock(return_value=False)
    bot._get_history = mock.Mock()
    bot._add_history = mock.Mock()
    bot._send_trigger_reply = mock.Mock(return_value=True)
    bot._after_reply_sent = mock.Mock()
    bot._handle_send_voice = mock.Mock(return_value=True)
    bot._dispatch_and_notify = mock.Mock()
    bot._pending_placeholders = {}
    bot._chat_is_group = {}
    bot._group_seen = {}
    for k, v in kw.items():
        setattr(bot, k, v)
    return bot


class TestGapText(unittest.TestCase):
    def test_units_and_decimals(self):
        now = 1000000.0
        self.assertEqual(AgentBot._proactive_gap_text(now - 30 * 60, now),
                         "30分钟")
        self.assertEqual(AgentBot._proactive_gap_text(now - 3 * 3600, now),
                         "3小时")
        self.assertEqual(AgentBot._proactive_gap_text(now - int(3.4 * 3600), now),
                         "3.4小时")
        self.assertEqual(AgentBot._proactive_gap_text(now - 36 * 3600, now),
                         "1.5天")
        self.assertEqual(AgentBot._proactive_gap_text(now - 48 * 3600, now),
                         "2天")


class TestScheduler(unittest.TestCase):
    def _bot(self):
        bot = _make_bot()
        delattr(bot, "_get_history")  # 用真实的 MemoryStore 读取
        bot.memory_file = os.path.join(tempfile.mkdtemp(), "memory.json")
        bot.max_history = 1000
        bot.memory_keep_recent = 30
        bot.memory_rolling_step = 30
        bot.memory_deep_enabled = False
        bot.chat_card_params = {}
        bot._chat_msg_time = AgentBot._chat_msg_time
        return bot

    def test_first_scan_only_schedules(self):
        bot = self._bot()
        bot._mem.add("小明", "assistant", "在", deep_enabled=False)
        sched = ProactiveScheduler(bot)
        sched._scan_once(time.time())
        self.assertIsNotNone(sched.next_due("小明"))
        self.assertTrue(bot._proactive_queue.empty())

    def test_due_enqueues(self):
        bot = self._bot()
        bot._mem.add("小明", "assistant", "在", deep_enabled=False)
        # 把最后一条时间改成 1 小时前，满足 idle
        bot._mem.memory_db["小明"]["recent"][-1]["time"] = _ts_ago(3600)
        sched = ProactiveScheduler(bot)
        sched._next_due["小明"] = 0
        sched._scan_once(time.time())
        self.assertFalse(bot._proactive_queue.empty())
        self.assertEqual(bot._proactive_queue.get_nowait()["chat"], "小明")

    def test_group_excluded_when_private_only(self):
        bot = self._bot()
        bot._mem.add("摸鱼集团", "assistant", "在", deep_enabled=False)
        bot._mem.memory_db["摸鱼集团"]["recent"][-1]["time"] = _ts_ago(3600)
        bot._chat_is_group["摸鱼集团"] = True
        sched = ProactiveScheduler(bot)
        sched._next_due["摸鱼集团"] = 0
        sched._scan_once(time.time())
        self.assertTrue(bot._proactive_queue.empty())

    def test_unanswered_user_message_skipped(self):
        bot = self._bot()
        bot._mem.add("小明", "user", "在吗", deep_enabled=False)
        bot._mem.memory_db["小明"]["recent"][-1]["time"] = _ts_ago(3600)
        sched = ProactiveScheduler(bot)
        sched._next_due["小明"] = 0
        sched._scan_once(time.time())
        self.assertTrue(bot._proactive_queue.empty())

    def test_quiet_hours(self):
        bot = self._bot()
        bot.proactive_quiet_enabled = True
        t = time.localtime()
        bot.proactive_quiet_start = f"{t.tm_hour:02d}:00"
        bot.proactive_quiet_end = f"{(t.tm_hour + 1) % 24:02d}:00"
        sched = ProactiveScheduler(bot)
        self.assertTrue(sched._quiet_now())

    def test_cooldown_blocks_evaluation(self):
        bot = self._bot()
        bot._mem.add("小明", "assistant", "在", deep_enabled=False)
        bot._mem.memory_db["小明"]["recent"][-1]["time"] = _ts_ago(3600)
        sched = ProactiveScheduler(bot)
        sched._next_due["小明"] = 0
        sched._last_sent["小明"] = time.time()
        sched._scan_once(time.time())
        self.assertTrue(bot._proactive_queue.empty())


class TestDrainProactive(unittest.TestCase):
    def _bot(self, hist_role="assistant", gap=3600):
        bot = _make_bot()
        bot._get_history.return_value = [{
            "role": hist_role, "content": "上一条",
            "time": _ts_ago(gap)}]
        bot.call_vision_api = mock.Mock()
        return bot

    def _run(self, bot, resp):
        bot.call_vision_api.return_value = resp
        bot._proactive_queue.put({"chat": "小明"})
        with mock.patch("xiaoli_bot.has_active_tasks", return_value=False):
            bot._drain_proactive()

    def test_no_reply_is_silent_and_unrecorded(self):
        bot = self._bot()
        self._run(bot, {"kind": "tool_call", "name": "no_reply",
                        "arguments": "{}"})
        bot._add_history.assert_not_called()
        bot._send_trigger_reply.assert_not_called()
        bot._proactive_sched.mark_sent.assert_not_called()

    def test_text_sent_and_recorded(self):
        bot = self._bot()
        self._run(bot, {"kind": "text", "content": "在忙吗", "actions": [],
                        "sent_any": False})
        bot._send_trigger_reply.assert_called_once_with("在忙吗", "小明")
        bot._add_history.assert_called_once_with("小明", "assistant", "在忙吗")
        bot._proactive_sched.mark_sent.assert_called_once_with("小明")

    def test_send_failure_does_not_record(self):
        bot = self._bot()
        bot._send_trigger_reply.return_value = False
        self._run(bot, {"kind": "text", "content": "在忙吗", "actions": [],
                        "sent_any": False})
        bot._add_history.assert_not_called()
        bot._proactive_sched.mark_sent.assert_not_called()

    def test_tail_system_and_forced_no_reply(self):
        bot = self._bot()
        self._run(bot, {"kind": "tool_call", "name": "no_reply",
                        "arguments": "{}"})
        args, kwargs = bot.call_vision_api.call_args
        self.assertIsNone(args[0])
        self.assertTrue(kwargs["force_no_reply"])
        self.assertIn("距上次聊天 1小时", kwargs["system_tail"])
        self.assertIn("no_reply", kwargs["system_tail"])

    def test_unanswered_last_message_skipped(self):
        bot = self._bot(hist_role="user")
        self._run(bot, {"kind": "text", "content": "在吗"})
        bot.call_vision_api.assert_not_called()

    def test_actions_count_as_sent(self):
        bot = self._bot()
        self._run(bot, {"kind": "text", "content": "",
                        "actions": [{"type": "sticker", "file": "猫猫.png"}],
                        "sent_any": True})
        bot._add_history.assert_called_once_with(
            "小明", "assistant", "[发送了表情包: 猫猫.png]")
        bot._proactive_sched.mark_sent.assert_called_once_with("小明")
        bot._send_trigger_reply.assert_not_called()


class TestProactiveCallShape(unittest.TestCase):
    def test_no_user_message_and_system_tail_last(self):
        bot = _make_bot()
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
        bot.reply_max_tokens = 4000
        bot.max_context_tokens = 100000
        bot._model_lock = threading.RLock()
        bot.voice_mode = "off"
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
        bot._get_history = mock.Mock(return_value=[])
        bot._post_chat_completions = mock.Mock(return_value={
            "choices": [{"message": {"content": ""}}]})
        bot.call_vision_api(
            None, chat_id="小明",
            system_tail="距上次聊天 3.4小时，可以决定是否给用户发消息；"
                        "不决定发消息请调用 no_reply 工具。",
            force_no_reply=True)
        payload = bot._post_chat_completions.call_args.args[2]
        msgs = payload["messages"]
        self.assertEqual(msgs[-1]["role"], "system")
        self.assertIn("距上次聊天 3.4小时", msgs[-1]["content"])
        self.assertFalse(any(m["role"] == "user" for m in msgs))
        names = [t["function"]["name"] for t in payload["tools"]]
        self.assertIn("no_reply", names)


if __name__ == "__main__":
    unittest.main()
