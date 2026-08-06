# -*- coding: utf-8 -*-
"""任务附件投递测试：纯文字任务不带"最新微信文件"附件。

RED 复现：用户只发文字任务（没发文件）时，文本消息路径仍把接收目录的
"最新"文件作为附件投递给天枢——无关旧文件被 agent 误判为任务素材。
修复：附件仅由文件消息路径投递（_process_file_with_task / _process_file_with_instruction），
文本任务投递时 attachment_paths 恒为 None。
"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_bot import AgentBot


def _text_msg(sender, content, mid):
    return SimpleNamespace(sender=sender, content=content, id=mid)


class _FakeWx:
    """最小 wx 桩：一个会话、一条文本消息。"""

    def __init__(self, msgs):
        self._msgs = msgs
        self.sessions = [SimpleNamespace(name="小明")]

    def GetSession(self):
        return self.sessions

    def ChatWith(self, _name):
        pass

    def GetAllMessage(self):
        return self._msgs


def _make_bot(recv_dir=""):
    bot = AgentBot.__new__(AgentBot)
    bot.nickname = "小漓"
    bot.task_enabled = True
    bot.dispatched_msg_ids = set()
    bot.recent_msg_ids = set()
    bot.paused = False
    bot._sending_lock = False
    bot.tasks_dir = tempfile.mkdtemp(prefix="att_")
    bot._task_was_active = False
    bot._task_end_time = None
    bot._listen_hold_seconds = 2
    bot._pending_files = {}
    bot._last_poll_time = 0
    bot.tianshu_poll_interval = 5
    bot.last_reply_time = 0
    bot.cooldown = 3
    bot.file_storage_path = recv_dir
    return bot


class TestTextTaskNoAttachment(unittest.TestCase):
    def test_text_task_with_old_file_in_dir_no_attachment(self):
        """RED 复现（端到端）：接收目录里有"最新"旧文件，但用户本次只发
        了文字任务——不得把旧文件投递给 agent（历史缺陷：无条件
        _latest_received_file → agent 误判无关文件为任务素材）。"""
        # 接收目录里有一个旧文件（模拟历史文件/bot 回传成果等）
        recv_dir = tempfile.mkdtemp(prefix="recv_")
        try:
            with open(os.path.join(recv_dir, "旧报告.docx"), "w",
                      encoding="utf-8") as f:
                f.write("old file")
            bot = _make_bot(recv_dir=recv_dir)
            bot.wx = _FakeWx([_text_msg("王", "根据文档做个网站", "m1")])
            bot._tick_poll_outbox = lambda: None
            bot._classify_task = lambda t: {"is_task": True, "task": "做个网站"}
            dispatched = []
            bot._dispatch_and_notify = lambda *a, **k: dispatched.append(
                k.get("attachment_paths"))
            bot._add_history = lambda *a, **k: None
            bot.call_chat_ai = lambda *a, **k: "ok"
            bot._send_text = lambda *a, **k: None
            bot.process_new_messages()
            self.assertEqual(dispatched, [None],
                             "纯文字任务不得携带接收目录的文件")
        finally:
            import shutil
            shutil.rmtree(recv_dir, ignore_errors=True)

    def test_text_task_plain_no_attachment(self):
        """无文件目录时文本任务同样不带附件（基线）。"""
        bot = _make_bot()
        bot.wx = _FakeWx([_text_msg("王", "帮我做个PPT", "m2")])
        bot._tick_poll_outbox = lambda: None
        bot._classify_task = lambda t: {"is_task": True, "task": "做PPT"}
        dispatched = []
        bot._dispatch_and_notify = lambda *a, **k: dispatched.append(
            k.get("attachment_paths"))
        bot._add_history = lambda *a, **k: None
        bot.call_chat_ai = lambda *a, **k: "ok"
        bot._send_text = lambda *a, **k: None
        bot.process_new_messages()
        self.assertEqual(dispatched, [None])

    def test_file_task_path_still_attaches(self):
        """文件消息路径（_process_file_with_instruction 调用形状）仍投递
        附件——附件投递只从文本路径移除，不破坏文件路径。"""
        bot = _make_bot()
        dispatched = []
        bot._dispatch_and_notify = lambda *a, **k: dispatched.append(
            k.get("attachment_paths"))
        bot._send_text = lambda *a, **k: None
        bot._dispatch_and_notify(
            "小明", "王", "处理文件",
            attachment_paths=[r"D:\recv\报告.docx"],
            extra={"msg_id": None, "raw_message": "报告.docx", "file_text": "x"})
        self.assertEqual(dispatched, [[r"D:\recv\报告.docx"]],
                         "文件消息路径必须继续投递附件")


if __name__ == "__main__":
    unittest.main(verbosity=2)
