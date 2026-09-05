# -*- coding: utf-8 -*-
"""任务暂停门控测试：成果回传期间（_sending_lock=True）process_new_messages
必须彻底暂停——不轮询任务目录、不遍历会话（防止 ChatWith 切走窗口打断发送）。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_bot import AgentBot, has_active_tasks, should_resume_listen


def make_bot(**over):
    """轻量构造 AgentBot（不连微信、不初始化），仅用于测 process_new_messages 门控。"""
    bot = AgentBot.__new__(AgentBot)
    bot.paused = False
    bot._sending_lock = False
    bot.tasks_dir = tempfile.mkdtemp(prefix="task_pause_")
    bot._task_was_active = False
    bot._task_end_time = None
    bot._listen_hold_seconds = 2
    bot._pending_files = {}
    bot._last_poll_time = 0
    bot.tianshu_poll_interval = 5
    bot.last_reply_time = 0
    bot.cooldown = 3
    bot.file_storage_path = ""
    for k, v in over.items():
        setattr(bot, k, v)
    return bot


class TestSendingLockPause(unittest.TestCase):
    def test_sending_lock_skips_polling(self):
        """RED 复现：成果回传期间（_sending_lock=True）不得轮询任务目录——
        原实现把 _tick_poll_outbox 放在 _sending_lock 检查之前，回传中仍会
        轮询（可能触发 ChatWith 切窗打断发送）。"""
        bot = make_bot()
        bot._sending_lock = True
        polled = []
        bot._tick_poll_outbox = lambda: polled.append("poll")
        bot.process_new_messages()
        self.assertEqual(polled, [], "_sending_lock 期间不得轮询任务目录")

    def test_sending_lock_skips_session_scan(self):
        """回传期间也不得遍历会话（GetSession/ChatWith）。"""
        bot = make_bot()
        bot._sending_lock = True
        scanned = []
        bot._tick_poll_outbox = lambda: polled_append(scanned)
        bot.wx = _NoScanWx(scanned)
        bot.process_new_messages()
        self.assertEqual(scanned, [], "_sending_lock 期间不得遍历会话")

    def test_normal_flow_still_polls(self):
        """非回传期间正常轮询任务目录（基线：门控不误伤正常路径）。"""
        bot = make_bot()
        polled = []
        bot._tick_poll_outbox = lambda: polled.append("poll")
        # has_active_tasks 为 False + 无 pending → 走到会话遍历；wx 为空则异常前已 poll
        bot.wx = _NoScanWx([])
        try:
            bot.process_new_messages()
        except Exception:
            pass
        self.assertEqual(polled, ["poll"], "非回传期间应正常轮询")


def polled_append(lst):
    lst.append("poll")


class _NoScanWx:
    """最小 wx 桩：iter_sessions 记一次即返回空（模拟无新会话）。"""

    def __init__(self, log):
        self._log = log

    def iter_sessions(self):
        self._log.append("scan")
        return iter([])

    def get_messages(self, chat, assume_switched=False):
        return []


class _ProtocolProbeWx:
    """同时暴露新旧 API 的探针桩：记录 process_new_messages 实际调用的是
    旧 wxauto API（GetSession）还是新协议（iter_unread_sessions）。"""

    def __init__(self, log):
        self._log = log

    def GetSession(self):
        self._log.append("get_session")
        return []

    def iter_unread_sessions(self):
        self._log.append("unread")
        return iter([])

    def iter_sessions(self):
        self._log.append("sessions")
        return iter([])

    def get_messages(self, chat, assume_switched=False):
        return []


class TestProcessNewMessagesProtocol(unittest.TestCase):
    def test_uses_new_protocol_not_old_api(self):
        """RED 复现：AgentBot.process_new_messages 仍调用旧 wxauto API
        （GetSession）而非新协议（iter_unread_sessions）——协议化后端
        （visual/wxauto_backend）没有 GetSession，会 AttributeError 被
        except 静默吞掉，表现为"启动 bot 后收到新消息毫无反应"。
        修复后应走 iter_unread_sessions，不再触碰 GetSession。"""
        log = []
        bot = make_bot()
        bot.wx = _ProtocolProbeWx(log)
        bot._tick_poll_outbox = lambda: None
        bot.process_new_messages()
        self.assertNotIn("get_session", log, "不得再调用旧 GetSession API")
        self.assertIn("unread", log, "应调用新协议 iter_unread_sessions")


if __name__ == "__main__":
    unittest.main()
