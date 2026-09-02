# -*- coding: utf-8 -*-
"""定时消息单测：store 到期/宽限/错过/滚动、调度线程入队去重、
主循环消费发送（旁路占位计数）、节点 I set_reminder 工具分支。"""
import json
import os
import queue
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from xiaoli_app.reminders_store import RemindersStore, GRACE_SECONDS
from xiaoli_bot import AgentBot, ReminderScheduler
import wechat_bot as wb


class TestRemindersStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rem_")
        self.store = RemindersStore(os.path.join(self.dir, "rem.json"))

    def test_add_list_remove(self):
        r = self.store.add("王文生", "查成绩", time.time() + 600, "once")
        self.assertEqual(len(self.store.list()), 1)
        self.assertTrue(self.store.remove(r["id"]))
        self.assertEqual(self.store.list(), [])
        self.assertFalse(self.store.remove(r["id"]))

    def test_due_pending_and_grace(self):
        now = time.time()
        self.store.add("A", "刚到期", now - 1)
        self.store.add("B", "刚过期未超宽限", now - GRACE_SECONDS + 5)
        self.store.add("C", "未来", now + 600)
        pending, missed = self.store.due(now)
        chats = {r["chat"] for r in pending}
        self.assertEqual(chats, {"A", "B"})
        self.assertEqual(missed, 0)

    def test_due_missed_single_disabled(self):
        now = time.time()
        r = self.store.add("A", "太久错过", now - GRACE_SECONDS - 10)
        pending, missed = self.store.due(now)
        self.assertEqual(pending, [])
        self.assertEqual(missed, 1)
        rec = [x for x in self.store.list() if x["id"] == r["id"]][0]
        self.assertFalse(rec["enabled"])
        self.assertTrue(rec["missed"])

    def test_due_missed_periodic_rolls_forward(self):
        now = time.time()
        r = self.store.add("A", "每日", now - GRACE_SECONDS - 3600, "daily")
        pending, _ = self.store.due(now)
        self.assertEqual(pending, [])
        rec = [x for x in self.store.list() if x["id"] == r["id"]][0]
        self.assertTrue(rec["enabled"])
        self.assertGreater(rec["fire_at"], now - GRACE_SECONDS)

    def test_mark_fired_single_and_daily(self):
        now = time.time()
        r1 = self.store.add("A", "单次", now + 600)
        self.store.mark_fired(r1["id"], now)
        rec1 = [x for x in self.store.list() if x["id"] == r1["id"]][0]
        self.assertFalse(rec1["enabled"])
        self.assertEqual(rec1["last_fired"], now)
        r2 = self.store.add("B", "每日", now + 100, "daily")
        self.store.mark_fired(r2["id"], now)
        rec2 = [x for x in self.store.list() if x["id"] == r2["id"]][0]
        self.assertTrue(rec2["enabled"])
        self.assertGreater(rec2["fire_at"], now + 86300)

    def test_set_enabled(self):
        r = self.store.add("A", "内容", time.time() + 600)
        self.store.set_enabled(r["id"], False)
        pending, _ = self.store.due(time.time() + 700)
        self.assertEqual(pending, [])


class _FakeWx:
    def __init__(self):
        self.sent = []

    def send_text(self, chat, text):
        self.sent.append((chat, text))


def make_rem_bot(dirpath):
    bot = AgentBot.__new__(AgentBot)
    bot.reminders = RemindersStore(os.path.join(dirpath, "rem.json"))
    bot._reminder_queue = queue.Queue()
    bot._condition_queue = queue.Queue()
    bot.wx = _FakeWx()
    bot._add_history = lambda *a, **k: None
    bot._send_text = lambda text, chat: bot.wx.sent.append((chat, text))
    # 触发器火线（用户定案）：到点/达成/到期都回递 API 生成角色内回复——
    # 测试桩直接返回固定回复，记录 (chat, trigger) 供断言
    bot._chats = []

    def fake_chat_ai(chat_id, user_msg, **kw):
        bot._chats.append((chat_id, user_msg))
        return f"回复[{chat_id}]"
    bot.call_chat_ai = fake_chat_ai
    return bot


class TestSchedulerAndDrain(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sched_")

    def test_scheduler_enqueues_once(self):
        store = RemindersStore(os.path.join(self.dir, "rem.json"))
        store.add("王文生", "到期了", time.time() - 1)
        q = queue.Queue()
        stop = threading.Event()
        sched = ReminderScheduler(store, q, stop_event=stop, scan_seconds=0.05)
        sched.start()
        try:
            item = q.get(timeout=2)
            self.assertEqual(item["chat"], "王文生")
        finally:
            stop.set()
            sched.join(timeout=2)
        time.sleep(0.15)  # 再扫几轮也不得重复入队（claimed 去重）
        self.assertTrue(q.empty())

    def test_drain_calls_api_and_sends_reply(self):
        """火线统一：到点回递 API（call_chat_ai）生成角色内回复后直发，
        不再发固定文案【定时提醒】，也不注入预写提醒事项（content 已废）。"""
        bot = make_rem_bot(self.dir)
        fire_at = time.time() - 1
        r = bot.reminders.add("王文生", "", fire_at)
        bot._reminder_queue.put(dict(r))
        bot._drain_reminders()
        self.assertEqual(len(bot._chats), 1)
        chat, trigger = bot._chats[0]
        self.assertEqual(chat, "王文生")
        self.assertIn("定时触发", trigger)
        self.assertIn("指定时间", trigger)
        self.assertEqual(bot.wx.sent, [("王文生", "回复[王文生]")])
        rec = [x for x in bot.reminders.list() if x["id"] == r["id"]][0]
        self.assertFalse(rec["enabled"])  # 单次触发后关闭
        self.assertEqual(len(bot._reminder_queue.queue), 0)

    def test_drain_over_grace_skips_send(self):
        bot = make_rem_bot(self.dir)
        r = bot.reminders.add("王文生", "太久之前", time.time() - GRACE_SECONDS - 60)
        bot._reminder_queue.put(dict(r))
        bot._drain_reminders()
        self.assertEqual(bot.wx.sent, [])


class TestSetReminderTool(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="setrem_")
        self.bot = make_rem_bot(self.dir)
        self.bot.nickname = "小漓"

    def test_valid_tool_call_creates_reminder_and_replies(self):
        fire = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 3600))
        result = {"kind": "tool_call", "name": "set_reminder",
                  "arguments": json.dumps({"time": fire, "content": "查成绩",
                                           "repeat": "once"})}
        out = self.bot._apply_vision_result("王文生", "王文生", result,
                                            user_text="明天提醒我查成绩")
        self.assertTrue(out)
        recs = self.bot.reminders.list()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["chat"], "王文生")
        self.assertEqual(recs[0]["kind"], "time")
        self.assertEqual(self.bot.wx.sent[-1][0], "王文生")
        # 无预写文案（用户定案）：确认回复只确认时间，不带「提醒事项」
        self.assertIn("记住啦", self.bot.wx.sent[-1][1])

    def test_time_without_content_ok(self):
        """time 模式不填 content 也能创建（到点回递 API 自行回复）。"""
        fire = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() + 3600))
        result = {"kind": "tool_call", "name": "set_reminder",
                  "arguments": json.dumps({"time": fire})}
        self.assertTrue(
            self.bot._apply_vision_result("王文生", "王文生", result,
                                          user_text="明天下午三点叫我"))
        self.assertEqual(len(self.bot.reminders.list()), 1)

    def test_bad_time_degrades_to_chat(self):
        result = {"kind": "tool_call", "name": "set_reminder",
                  "arguments": json.dumps({"time": "not-a-time",
                                           "content": "x"})}
        self.assertIsNone(
            self.bot._apply_vision_result("王文生", "王文生", result))
        self.assertEqual(self.bot.reminders.list(), [])

    def test_past_time_degrades_to_chat(self):
        fire = time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() - 3600))
        result = {"kind": "tool_call", "name": "set_reminder",
                  "arguments": json.dumps({"time": fire, "content": "x"})}
        self.assertIsNone(
            self.bot._apply_vision_result("王文生", "王文生", result))
        self.assertEqual(self.bot.reminders.list(), [])

    def test_vision_payload_declares_tool_only_with_store(self):
        # 有 reminders 存储 → payload 声明 set_reminder；没有 → 不声明
        def capture_post(bot, captured):
            def fake(url, headers=None, payload=None, timeout=None,
                     label="api", meta=None):
                captured.append(payload)
                return {"choices": []}
            return fake

        bot_with = make_rem_bot(self.dir)
        bot_with.vision_api_url = "https://x"
        bot_with.vision_api_key = "k"
        bot_with.system_prompt = "p"
        bot_with.chat_model = "m"
        bot_with.chat_temperature = 0.7
        bot_with.vision_max_tokens = 100
        bot_with._model_lock = threading.RLock()
        bot_with._memory_lock = threading.RLock()
        bot_with._deep_count = {}
        bot_with.memory_deep_enabled = False
        bot_with.memory_compress_enabled = False
        bot_with._deep_dir = ""
        bot_with._get_history = lambda chat_id: []
        captured = []
        with mock.patch.object(bot_with, "_post_chat_completions",
                               side_effect=capture_post(bot_with, captured)):
            bot_with.call_vision_api([{"type": "text", "text": "hi"}])
        names = [t["function"]["name"] for t in captured[0]["tools"]]
        self.assertIn("set_reminder", names)

        bot_without = AgentBot.__new__(AgentBot)
        bot_without.vision_api_url = "https://x"
        bot_without.vision_api_key = "k"
        bot_without.system_prompt = "p"
        bot_without.chat_model = "m"
        bot_without.chat_temperature = 0.7
        bot_without.vision_max_tokens = 100
        bot_without._model_lock = threading.RLock()
        bot_without._memory_lock = threading.RLock()
        bot_without._deep_count = {}
        bot_without.memory_deep_enabled = False
        bot_without.memory_compress_enabled = False
        bot_without._deep_dir = ""
        bot_without._get_history = lambda chat_id: []
        captured2 = []
        with mock.patch.object(bot_without, "_post_chat_completions",
                               side_effect=capture_post(bot_without, captured2)):
            bot_without.call_vision_api([{"type": "text", "text": "hi"}])
        names2 = [t["function"]["name"] for t in captured2[0]["tools"]]
        self.assertNotIn("set_reminder", names2)
        self.assertIn("dispatch_task", names2)


if __name__ == "__main__":
    unittest.main()
