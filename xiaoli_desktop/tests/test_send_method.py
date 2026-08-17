# -*- coding: utf-8 -*-
"""回传发送分支行为锚定：剪贴板优先 + send_file 兜底。

wxauto 后端移除后，成果文件发送恒走剪贴板（_send_file_clipboard）优先，
失败才兜底协议 send_file（visual 后端未实现，返回 False）。
本测试锚定该语义，防止后续改动破坏「剪贴板主用」路径。
"""
import os
import sys
import json
import shutil
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_bot import AgentBot


class TestSendMethodConverged(unittest.TestCase):

    def _make_bot(self, tasks_dir):
        """绕过 __init__ 构造 AgentBot（避免真连微信），只暴露回传所需属性。"""
        bot = AgentBot.__new__(AgentBot)
        bot.task_enabled = True
        bot.tasks_dir = tasks_dir
        bot._sending_lock = False
        bot._switch_to_chat = mock.Mock()
        bot._send_text = mock.Mock()
        bot._send_file_clipboard = mock.Mock(return_value=True)
        bot.wx = mock.Mock()
        bot.wx.send_file = mock.Mock(return_value=True)
        bot._register_sent_back = mock.Mock()
        bot._refresh_file_snapshot = mock.Mock()
        bot._remember_task_result = mock.Mock()
        return bot

    def _make_task(self, tasks_dir, name="task001", files=("report.pdf",)):
        task_dir = os.path.join(tasks_dir, name)
        os.makedirs(task_dir)
        with open(os.path.join(task_dir, "task.json"), "w", encoding="utf-8") as f:
            json.dump({"chat_name": "小明"}, f)
        with open(os.path.join(task_dir, "result.json"), "w", encoding="utf-8") as f:
            json.dump({"reply_text": "完成", "files": list(files)}, f)
        for fn in files:
            with open(os.path.join(task_dir, fn), "w", encoding="utf-8") as f:
                f.write("dummy")
        return task_dir

    def _poll(self, bot):
        bot._poll_outbox()

    def test_clipboard_success_skips_send_file(self):
        """剪贴板成功 → 不尝试 send_file；登记排除 + 刷新快照 + 写记忆。"""
        tasks = tempfile.mkdtemp(prefix="send_method_")
        try:
            self._make_task(tasks)
            bot = self._make_bot(tasks)
            self._poll(bot)
            bot._send_file_clipboard.assert_called_once()
            bot.wx.send_file.assert_not_called()
            bot._register_sent_back.assert_called_once()
            bot._refresh_file_snapshot.assert_called_once()
            bot._remember_task_result.assert_called_once()
        finally:
            shutil.rmtree(tasks, ignore_errors=True)

    def test_clipboard_fail_falls_back_to_send_file(self):
        """剪贴板失败 → send_file 兜底；兜底成功同样登记排除。"""
        tasks = tempfile.mkdtemp(prefix="send_method_")
        try:
            self._make_task(tasks)
            bot = self._make_bot(tasks)
            bot._send_file_clipboard.return_value = False
            self._poll(bot)
            bot.wx.send_file.assert_called_once()
            bot._register_sent_back.assert_called_once()
        finally:
            shutil.rmtree(tasks, ignore_errors=True)

    def test_both_fail_keeps_file_no_register(self):
        """剪贴板 + send_file 均失败 → 不登记排除（文件保留）；记忆仍记录任务产出。"""
        tasks = tempfile.mkdtemp(prefix="send_method_")
        try:
            self._make_task(tasks)
            bot = self._make_bot(tasks)
            bot._send_file_clipboard.return_value = False
            bot.wx.send_file.return_value = False
            self._poll(bot)
            bot._register_sent_back.assert_not_called()
            bot._refresh_file_snapshot.assert_not_called()
            bot._remember_task_result.assert_called_once()
        finally:
            shutil.rmtree(tasks, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
