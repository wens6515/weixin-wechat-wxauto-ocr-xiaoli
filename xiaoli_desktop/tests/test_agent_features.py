# -*- coding: utf-8 -*-
"""AgentBot 新功能单测：媒体防抖双模式 / 主动追尾调度与护栏 / 每日预算闸。"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_bot import AgentBot


def make_bot(**attrs):
    bot = AgentBot.__new__(AgentBot)   # 不跑 __init__（会连微信）
    bot.followup_enabled = False
    bot.followup_private_only = True
    bot.followup_delay_min = 45
    bot.followup_delay_max = 90
    bot.followup_cooldown_minutes = 120
    bot.followup_daily_max = 10
    bot._followup_pending = []
    bot._followup_last = {}
    bot._followup_day = {"date": time.strftime("%Y-%m-%d"), "count": 0}
    bot.daily_cost_limit_enabled = False
    bot.daily_cost_limit = 10.0
    bot._budget_day = {"date": None, "flagged": False}
    bot._budget_cache = {"at": 0.0, "out": False}
    bot.media_wait_mode = "fixed"
    bot.media_wait_seconds = 10
    bot.media_wait_quiet_seconds = 5
    bot._pending_placeholders = {}
    bot.paused = False
    bot._sending_lock = False
    bot.sent = []
    bot._send_trigger_reply = lambda text, chat: bot.sent.append((chat, text))
    for k, v in attrs.items():
        setattr(bot, k, v)
    return bot


class TestMediaDebounce(unittest.TestCase):
    def test_fixed_mode_single_sleep(self):
        bot = make_bot()
        with mock.patch("xiaoli_bot.time.sleep") as sl:
            bot._media_debounce("林小满", False)
        sl.assert_called_once_with(10)

    def test_smart_mode_returns_after_quiet(self):
        """智能模式：块数稳定 → 连续静默达标即返回（约 2 次轮询）。"""
        bot = make_bot(media_wait_mode="smart", media_wait_quiet_seconds=5)
        bot.wx = mock.Mock()
        bot.wx.analyze_window.return_value = {"other_blocks": [1, 2]}
        with mock.patch("xiaoli_bot.time.sleep") as sl:
            bot._media_debounce("林小满", False)
        self.assertEqual(sl.call_count, 3)   # 2s + 2s + 2s ≥ 5s 静默
        self.assertEqual(bot.wx.analyze_window.call_count, 3)

    def test_smart_mode_resets_on_new_block(self):
        """对方还在连发：块数增长会重置静默计时，直到上限 12s 兜底。"""
        bot = make_bot(media_wait_mode="smart", media_wait_quiet_seconds=5)
        bot.wx = mock.Mock()
        bot.wx.analyze_window.side_effect = [
            {"other_blocks": [1, 2]},
            {"other_blocks": [1, 2, 3]},
            {"other_blocks": [1, 2, 3]},
            {"other_blocks": [1, 2, 3, 4]},
            {"other_blocks": [1, 2, 3, 4]},
            {"other_blocks": [1, 2, 3, 4]},
        ]
        with mock.patch("xiaoli_bot.time.sleep") as sl:
            bot._media_debounce("林小满", False)
        self.assertEqual(sl.call_count, 6)   # 打满 12s 硬上限


class TestFollowupSchedule(unittest.TestCase):
    def _hot_hist(self, n=8):
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 60))
        return [{"role": "user", "content": "x", "time": now} for _ in range(n)]

    def test_disabled_never_schedules(self):
        bot = make_bot(followup_enabled=False)
        bot._get_history = lambda c: self._hot_hist()
        bot._followup_schedule("林小满")
        self.assertEqual(bot._followup_pending, [])

    def test_low_heat_never_schedules(self):
        bot = make_bot(followup_enabled=True)
        bot._get_history = lambda c: self._hot_hist(2)
        bot._followup_schedule("林小满")
        self.assertEqual(bot._followup_pending, [])

    def test_group_blocked_when_private_only(self):
        bot = make_bot(followup_enabled=True, followup_private_only=True)
        bot._get_history = lambda c: self._hot_hist()
        bot._followup_schedule("摸鱼集团", is_group=True)
        self.assertEqual(bot._followup_pending, [])

    def test_happy_path_schedules_within_window(self):
        bot = make_bot(followup_enabled=True)
        bot._get_history = lambda c: self._hot_hist()
        bot._followup_schedule("林小满")
        (p,) = bot._followup_pending
        self.assertEqual(p["chat"], "林小满")
        self.assertGreaterEqual(p["due_at"] - p["scheduled_at"], 45)
        self.assertLessEqual(p["due_at"] - p["scheduled_at"], 90)

    def test_cooldown_blocks_reschedule(self):
        bot = make_bot(followup_enabled=True)
        bot._get_history = lambda c: self._hot_hist()
        bot._followup_last["林小满"] = time.time() - 60   # 1 分钟前刚追尾
        bot._followup_schedule("林小满")
        self.assertEqual(bot._followup_pending, [])

    def test_daily_cap_blocks_schedule(self):
        bot = make_bot(followup_enabled=True)
        bot._get_history = lambda c: self._hot_hist()
        bot._followup_day["count"] = 10
        bot._followup_schedule("林小满")
        self.assertEqual(bot._followup_pending, [])


class TestFollowupDrain(unittest.TestCase):
    def _due(self, chat="林小满", age=200):
        now = time.time()
        return {"chat": chat, "due_at": now - 1, "scheduled_at": now - age}

    def test_user_spoke_during_window_cancels(self):
        """固定护栏：等待期间对方又发消息 → 取消（不可配置）。"""
        bot = make_bot(followup_enabled=True)
        recent = time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(time.time() - 100))
        bot._get_history = lambda c: [
            {"role": "user", "content": "还在吗", "time": recent}]
        bot._followup_generate = lambda chat: "对了"
        bot._followup_pending = [self._due()]
        bot._drain_followups()
        self.assertEqual(bot.sent, [])

    def test_happy_path_sends_and_counts(self):
        bot = make_bot(followup_enabled=True)
        bot._get_history = lambda c: []
        bot._chat_last_user_at = lambda c: 0.0
        bot._add_history = lambda c, r, t: None
        bot._followup_generate = lambda chat: "对了，那个文件你看了吗"
        bot._followup_pending = [self._due()]
        bot._drain_followups()
        self.assertEqual(bot.sent, [("林小满", "对了，那个文件你看了吗")])
        self.assertEqual(bot._followup_day["count"], 1)

    def test_no_output_skips(self):
        bot = make_bot(followup_enabled=True)
        bot._get_history = lambda c: []
        bot._chat_last_user_at = lambda c: 0.0
        bot._followup_generate = lambda chat: None   # [不补]/失败
        bot._followup_pending = [self._due()]
        bot._drain_followups()
        self.assertEqual(bot.sent, [])
        self.assertEqual(bot._followup_day["count"], 0)


class TestBudgetGate(unittest.TestCase):
    def test_disabled_always_false(self):
        bot = make_bot(daily_cost_limit_enabled=False)
        self.assertFalse(bot._budget_exhausted())

    def test_over_limit_true(self):
        bot = make_bot(daily_cost_limit_enabled=True, daily_cost_limit=5.0)
        fake = mock.Mock(return_value=[{"ts": time.time()}])
        with mock.patch("xiaoli_app.usage_store.UsageStore.load_records", fake), \
             mock.patch("xiaoli_app.pricing.estimate_records",
                        return_value={"today": 6.0}):
            self.assertTrue(bot._budget_exhausted())

    def test_under_limit_false(self):
        bot = make_bot(daily_cost_limit_enabled=True, daily_cost_limit=5.0)
        with mock.patch("xiaoli_app.usage_store.UsageStore.load_records",
                        return_value=[]), \
             mock.patch("xiaoli_app.pricing.estimate_records",
                        return_value={"today": 1.2}):
            self.assertFalse(bot._budget_exhausted())

    def test_result_cached(self):
        bot = make_bot(daily_cost_limit_enabled=True, daily_cost_limit=5.0)
        with mock.patch("xiaoli_app.usage_store.UsageStore.load_records",
                        return_value=[]) as load, \
             mock.patch("xiaoli_app.pricing.estimate_records",
                        return_value={"today": 6.0}):
            self.assertTrue(bot._budget_exhausted())
            self.assertTrue(bot._budget_exhausted())
        self.assertEqual(load.call_count, 1)   # 第二次走 60s 缓存

    def test_zero_limit_disabled(self):
        bot = make_bot(daily_cost_limit_enabled=True, daily_cost_limit=0.0)
        self.assertFalse(bot._budget_exhausted())


if __name__ == "__main__":
    unittest.main()


class TestNewDefaults(unittest.TestCase):
    def test_reply_max_tokens_default_40000(self):
        from xiaoli_app.config_store import AI_DEFAULTS
        self.assertEqual(AI_DEFAULTS["reply_max_tokens"], 40000)
