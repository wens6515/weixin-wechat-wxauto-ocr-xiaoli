# -*- coding: utf-8 -*-
"""状态监视（set_reminder kind=condition）单测：store 条件条目 CRUD、
本地/api 判定、scope 切片（防未来预报误触发）、异常计次判 dead、
固定截止到期回递、创建参数校验、功能开关、触发回递链路。"""
import json
import os
import queue
import tempfile
import threading
import time
import unittest
from unittest import mock

from xiaoli_app.reminders_store import (RemindersStore, WATCH_INTERVAL_MIN,
                                        WATCH_DEFAULT_TTL)
from xiaoli_bot import (AgentBot, ConditionWatcher, parse_watch_json,
                        WATCH_MAX_FAILS, WATCH_MIN_SLICE_CHARS)


class TestConditionStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cond_")
        self.store = RemindersStore(os.path.join(self.dir, "rem.json"))

    def test_add_condition_normalizes(self):
        item = self.store.add_condition(
            "王文生", "拿快递", "https://x/weather", "下雨了", judge="api",
            match_type="weird", met_keywords=["雨"], scope_start="今天",
            scope_end="", interval_seconds=1, expire_at=123.5)
        self.assertEqual(item["kind"], "condition")
        self.assertEqual(item["interval_seconds"], WATCH_INTERVAL_MIN)  # 1 → 下限 10
        self.assertEqual(item["match_type"], "present")  # 非法值兜底
        self.assertEqual(item["scope_end"], None)        # 空串归 None
        self.assertEqual(item["fail_count"], 0)
        self.assertIsNone(item["done"])

    def test_list_conditions_active_filter(self):
        self.store.add_condition("A", "c", "https://x", "cond")
        self.store.add("B", "t", time.time() + 60)  # time 条目不混入
        self.assertEqual(len(self.store.list_conditions()), 1)
        rid = self.store.list_conditions()[0]["id"]
        self.store.finish_condition(rid, "met", "命中")
        self.assertEqual(self.store.list_conditions(), [])
        all_rows = self.store.list_conditions(active_only=False)
        self.assertEqual(len(all_rows), 1)
        self.assertEqual(all_rows[0]["done"], "met")
        self.assertEqual(all_rows[0]["evidence"], "命中")

    def test_schedule_next_and_record_fail(self):
        item = self.store.add_condition("A", "c", "https://x", "cond")
        self.assertTrue(self.store.schedule_next(item["id"], 1234.0,
                                                 reset_fail=False))
        self.assertEqual(self.store.list_conditions()[0]["next_check_at"], 1234.0)
        self.assertEqual(self.store.record_fail(item["id"]), 1)
        self.assertEqual(self.store.record_fail(item["id"]), 2)
        self.store.schedule_next(item["id"], 0, reset_fail=True)
        self.assertEqual(self.store.list_conditions()[0]["fail_count"], 0)

    def test_due_skips_condition_rows(self):
        """time 扫描（due）不得把条件条目当错过处理（旧条目无 kind 字段
        按 time 处理 = 兼容旧文件）。"""
        self.store.add_condition("A", "c", "https://x", "cond")
        pending, missed = self.store.due(time.time() + 9999)
        self.assertEqual((pending, missed), ([], 0))
        self.assertTrue(self.store.list_conditions()[0]["enabled"])


class TestParseWatchJson(unittest.TestCase):
    def test_valid_and_fenced(self):
        met, reason = parse_watch_json('{"met": true, "reason": "页面显示晴"}')
        self.assertIs(met, True)
        self.assertEqual(reason, "页面显示晴")
        met2, _ = parse_watch_json('```json\n{"met": false, "reason": "r"}\n```')
        self.assertIs(met2, False)

    def test_garbage_returns_none(self):
        self.assertEqual(parse_watch_json(""), (None, ""))
        self.assertEqual(parse_watch_json("完全不是JSON"), (None, ""))
        self.assertEqual(parse_watch_json('{"met": "yes"}'), (None, ""))


def make_watch_bot(dirpath, enabled=True, paused=False):
    bot = AgentBot.__new__(AgentBot)
    bot.reminders = RemindersStore(os.path.join(dirpath, "rem.json"))
    bot._condition_queue = queue.Queue()
    bot.state_watch_enabled = enabled
    bot.paused = paused
    bot.chat_model = "deepseek-v4-flash"
    bot.api_key = "k"
    bot.api_url = "https://x/v1/chat/completions"
    bot.wx = type("W", (), {"send_text": staticmethod(lambda *a: None)})()
    bot._chats = []
    bot.call_chat_ai = lambda chat, msg, **kw: bot._chats.append((chat, msg)) or "好"
    bot._add_history = lambda *a, **k: None
    bot._send_text = lambda text, chat: None
    return bot


def make_watcher(bot):
    return ConditionWatcher(bot, stop_event=threading.Event(), scan_seconds=0.05)


# 真机实验同构页面：当前时段暴雨、未来时段晴天——整页匹配必误判，
# scope 切片后只有当前时段参与判定（用户点名的场景）
MIXED_PAGE_RAIN = "福州天气 今天 7天 2日（今天）暴雨 26℃ 3日（明天）大雨 30℃ 4日（后天）中雨转多云 5日（周六）晴 36℃"
MIXED_PAGE_SUNNY = "福州天气 今天 7天 2日（今天）晴 26℃ 3日（明天）大雨 30℃ 4日（后天）中雨转多云 5日（周六）晴 36℃"


class TestConditionWatcher(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="watch_")
        self.bot = make_watch_bot(self.dir)
        self.w = make_watcher(self.bot)

    def _add(self, **kw):
        base = dict(chat="王文生", content="拿快递", url="https://x/w",
                    condition="雨停了", judge="local", match_type="absent",
                    met_keywords=["雨", "降雨"], scope_start="今天",
                    scope_end="明天", interval_seconds=60,
                    expire_at=time.time() + 3600)
        base.update(kw)
        return self.bot.reminders.add_condition(**base)

    def _drain_events(self):
        out = []
        while True:
            try:
                out.append(self.bot._condition_queue.get_nowait())
            except queue.Empty:
                break
        return out

    def test_absent_scope_rain_present_not_met(self):
        """等雨停：当前时段仍有雨 → 未达成（未来预报里的晴不得误触发）。"""
        item = self._add()
        with mock.patch("xiaoli_bot.web_fetch", return_value=MIXED_PAGE_RAIN):
            self.w._scan_once()
        self.assertEqual(self._drain_events(), [])
        rec = self.bot.reminders.list_conditions(active_only=False)[0]
        self.assertTrue(rec["enabled"])           # 仍监视中
        self.assertGreater(rec["next_check_at"], time.time() - 1)
        self.assertEqual(rec["fail_count"], 0)    # 正常轮询重置失败计数

    def test_absent_scope_rain_gone_met(self):
        item = self._add()
        with mock.patch("xiaoli_bot.web_fetch", return_value=MIXED_PAGE_SUNNY):
            self.w._scan_once()
        evs = self._drain_events()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["type"], "met")
        self.assertEqual(evs[0]["chat"], "王文生")
        rec = self.bot.reminders.list_conditions(active_only=False)[0]
        self.assertFalse(rec["enabled"])
        self.assertEqual(rec["done"], "met")
        self.assertIn("雨", rec["evidence"])      # 证据含关键词说明

    def test_scope_marker_missing_counts_as_fail_then_dead(self):
        """切片标记找不到 = 异常（绝不整页兜底），连续失败达阈值判 dead。
        直接驱动 _check_once（异常后 next_check_at 被正确节流，扫描循环
        间隔内不会重试——这里推进时钟模拟连续多轮失败）。"""
        item = self._add()
        now = time.time()
        with mock.patch("xiaoli_bot.web_fetch", return_value="改版后的页面没有时间标记"):
            for i in range(WATCH_MAX_FAILS):
                rows = self.bot.reminders.list_conditions()
                self.w._check_once(rows[0], now + i * 120)
        evs = self._drain_events()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["type"], "dead")
        rec = self.bot.reminders.list_conditions(active_only=False)[0]
        self.assertEqual(rec["done"], "dead")

    def test_fetch_error_counts_as_fail(self):
        item = self._add()
        import requests
        with mock.patch("xiaoli_bot.web_fetch",
                        side_effect=requests.exceptions.ConnectTimeout()):
            self.w._scan_once()
        rec = self.bot.reminders.list_conditions()[0]
        self.assertEqual(rec["fail_count"], 1)

    def test_present_local_met(self):
        item = self._add(condition="有货", match_type="present",
                         met_keywords=["有货"], scope_start=None, scope_end=None)
        with mock.patch("xiaoli_bot.web_fetch",
                        return_value="商品页 现货充足 有货 下单吧"):
            self.w._scan_once()
        evs = self._drain_events()
        self.assertEqual(evs[0]["type"], "met")
        self.assertIn("有货", evs[0]["evidence"])

    def test_absent_short_page_is_anomaly(self):
        """absent 判定的空壳页守卫：片段过短不得判「已消失」（防验证页
        误触发达成）。"""
        item = self._add(scope_start=None, scope_end=None)
        with mock.patch("xiaoli_bot.web_fetch", return_value="加载中"):
            self.w._scan_once()
        self.assertEqual(self._drain_events(), [])
        self.assertEqual(
            self.bot.reminders.list_conditions()[0]["fail_count"], 1)

    def test_api_judge_via_post(self):
        """api 判定走唯一链路（_post_chat_completions，label=watch）。"""
        item = self._add(judge="api", met_keywords=None)
        captured = {}

        def fake_post(url, headers=None, payload=None, timeout=None,
                      label="api", meta=None):
            captured["label"] = label
            captured["payload"] = payload
            return {"choices": [{"message": {"content":
                    '{"met": true, "reason": "页面显示晴"}'}}]}

        self.bot._post_chat_completions = fake_post
        with mock.patch("xiaoli_bot.web_fetch", return_value=MIXED_PAGE_SUNNY):
            self.w._scan_once()
        self.assertEqual(captured["label"], "watch")
        self.assertIn("雨停了", captured["payload"]["messages"][1]["content"])
        evs = self._drain_events()
        self.assertEqual(evs[0]["type"], "met")
        self.assertEqual(evs[0]["evidence"], "页面显示晴")

    def test_api_judge_bad_output_is_fail(self):
        item = self._add(judge="api", met_keywords=None)
        self.bot._post_chat_completions = lambda *a, **k: {
            "choices": [{"message": {"content": "不是JSON"}}]}
        with mock.patch("xiaoli_bot.web_fetch", return_value=MIXED_PAGE_SUNNY):
            self.w._scan_once()
        self.assertEqual(self._drain_events(), [])
        self.assertEqual(
            self.bot.reminders.list_conditions()[0]["fail_count"], 1)

    def test_expired_fixed_deadline(self):
        """固定截止（非倒计时）：expire_at 一过即到期回递，最后尽力抓一次
        页面当「到期时的状况」。"""
        item = self._add(expire_at=time.time() - 1)
        with mock.patch("xiaoli_bot.web_fetch", return_value=MIXED_PAGE_RAIN):
            self.w._scan_once()
        evs = self._drain_events()
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["type"], "expired")
        self.assertIn("暴雨", evs[0]["evidence"])  # 到期时状况
        rec = self.bot.reminders.list_conditions(active_only=False)[0]
        self.assertEqual(rec["done"], "expired")

    @staticmethod
    def _run_briefly(bot, seconds=0.2):
        """run() 循环跑一段有限时间（独立 stop 事件，超时由 stopper 置位）。"""
        stop = threading.Event()
        w = ConditionWatcher(bot, stop_event=stop, scan_seconds=0.05)

        def stopper():
            time.sleep(seconds)
            stop.set()
        threading.Thread(target=stopper, daemon=True).start()
        w.run()

    def test_disabled_not_polled(self):
        """开关关闭：run 循环只等待，零抓取零判定。"""
        self._add()
        self.bot.state_watch_enabled = False
        with mock.patch("xiaoli_bot.web_fetch") as mf:
            self._run_briefly(self.bot)
        mf.assert_not_called()
        # 手动驱动 _scan_once（绕过 run 的闸门）确认数据本身可扫
        with mock.patch("xiaoli_bot.web_fetch", return_value=MIXED_PAGE_RAIN):
            self.w._scan_once()
        self.assertEqual(len(self.bot.reminders.list_conditions()), 1)

    def test_paused_not_polled(self):
        """暂停：轮询照停（不烧 API）；expire_at 固定不顺延，恢复后已过
        截止即走到期回递（由 _scan_once 的过期分支保证，另测）。"""
        self._add()
        self.bot.paused = True
        with mock.patch("xiaoli_bot.web_fetch") as mf:
            self._run_briefly(self.bot)
        mf.assert_not_called()


class TestHandleSetReminderCondition(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="setcond_")
        self.bot = make_watch_bot(self.dir)
        self.bot.nickname = "小漓"

    def _call(self, args, user_text="下雨了提醒我拿快递"):
        result = {"kind": "tool_call", "name": "set_reminder",
                  "arguments": json.dumps(args)}
        return self.bot._apply_vision_result("王文生", "王文生", result,
                                             user_text=user_text)

    def test_disabled_sends_hint_no_entry(self):
        self.bot.state_watch_enabled = False
        out = self._call({"kind": "condition", "content": "拿快递",
                          "url": "https://x/w", "condition": "下雨",
                          "judge": "local", "met_keywords": ["雨"]})
        self.assertTrue(out)
        self.assertEqual(self.bot.reminders.list(), [])
        self.assertEqual(len(self.bot.reminders.list_conditions()), 0)

    def test_valid_creates_with_defaults(self):
        self.bot.state_watch_enabled = True
        before = time.time()
        out = self._call({"kind": "condition", "content": "拿快递",
                          "url": "https://x/w", "condition": "雨停",
                          "match_type": "absent",
                          "met_keywords": ["雨", "降雨"],
                          "interval_seconds": 10})
        self.assertTrue(out)
        rows = self.bot.reminders.list_conditions()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["url"], "https://x/w")
        self.assertEqual(r["interval_seconds"], 10)
        # expire_at 未填 → 默认创建时刻 + 7 天（容忍执行耗时）
        self.assertGreaterEqual(r["expire_at"], before + WATCH_DEFAULT_TTL - 5)
        self.assertLessEqual(r["expire_at"], time.time() + WATCH_DEFAULT_TTL + 5)

    def test_local_missing_keywords_degrades(self):
        self.bot.state_watch_enabled = True
        self.assertIsNone(self._call({"kind": "condition", "content": "x",
                                      "url": "https://x", "condition": "雨停",
                                      "judge": "local"}))
        self.assertEqual(self.bot.reminders.list_conditions(), [])

    def test_bad_url_degrades(self):
        self.bot.state_watch_enabled = True
        self.assertIsNone(self._call({"kind": "condition", "content": "x",
                                      "url": "不是网址", "condition": "雨停",
                                      "judge": "api"}))
        self.assertEqual(self.bot.reminders.list_conditions(), [])

    def test_condition_without_content_ok(self):
        """condition 模式不填 content 也能创建（达成后 API 自行回复）。"""
        self.bot.state_watch_enabled = True
        out = self._call({"kind": "condition", "url": "https://x/w",
                          "condition": "雨停", "match_type": "absent",
                          "met_keywords": ["雨"]})
        self.assertTrue(out)
        rows = self.bot.reminders.list_conditions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["content"], "")

    def test_legacy_time_call_still_works(self):
        """旧格式（无 kind，只有 time）→ 定时分支，兼容模型旧行为。"""
        fire = time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(time.time() + 3600))
        out = self._call({"time": fire, "content": "查成绩"})
        self.assertTrue(out)
        self.assertEqual(len(self.bot.reminders.list()), 1)
        self.assertEqual(self.bot.reminders.list()[0]["kind"], "time")


class TestDrainConditions(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="draincond_")
        self.bot = make_watch_bot(self.dir)

        class _W:
            def __init__(self):
                self.sent = []

            def send_text(self, chat, text):
                self.sent.append((chat, text))
        self.bot.wx = _W()

    def test_met_event_calls_api_and_sends(self):
        self.bot._condition_queue.put({
            "type": "met", "chat": "王文生", "condition": "雨停了",
            "content": "拿快递", "evidence": "页面显示晴"})
        self.bot._drain_conditions()
        self.assertEqual(len(self.bot._chats), 1)
        chat, trigger = self.bot._chats[0]
        self.assertEqual(chat, "王文生")
        self.assertIn("条件达成", trigger)
        self.assertIn("雨停了", trigger)
        self.assertIn("页面显示晴", trigger)
        # content（提醒事项）已随「回递即回复」语义废除——触发消息只带
        # 条件与页面状况，措辞由 API 自行生成（用户定案）
        self.assertNotIn("拿快递", trigger)
        self.assertEqual(self.bot.wx.sent, [("王文生", "好")])
        self.assertTrue(self.bot._condition_queue.empty())

    def test_expired_event_mentions_deadline(self):
        self.bot._condition_queue.put({
            "type": "expired", "chat": "王文生", "condition": "雨停了",
            "content": "拿快递", "evidence": "仍有雨"})
        self.bot._drain_conditions()
        _chat, trigger = self.bot._chats[0]
        self.assertIn("监视到期", trigger)
        self.assertIn("仍有雨", trigger)

    def test_empty_chat_skipped(self):
        self.bot._condition_queue.put({"type": "met", "chat": "",
                                       "condition": "x", "content": "y",
                                       "evidence": ""})
        self.bot._drain_conditions()
        self.assertEqual(self.bot._chats, [])


if __name__ == "__main__":
    unittest.main()
