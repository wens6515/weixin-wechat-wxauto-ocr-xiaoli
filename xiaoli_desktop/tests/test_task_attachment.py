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

from xiaoli_bot import AgentBot, _looks_like_file_text
from wx_backend.models import MessageType


def _text_msg(sender, content, mid):
    return SimpleNamespace(sender=sender, content=content, id=mid,
                           type=MessageType.TEXT)


class _FakeWx:
    """最小 wx 桩：一个会话、一条文本消息（新协议）。"""

    def __init__(self, msgs):
        self._msgs = msgs

    def iter_unread_sessions(self):
        return iter(["小明"])

    def get_messages(self, chat):
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


# ---- 文件文本识别 + 任务判断输入格式 + 图片预览驱动 ----


class TestLooksLikeFileText(unittest.TestCase):
    def test_file_text_true(self):
        """含文档扩展名的 OCR 文本 = 文件消息（含合并文本 + 图标杂字符）。"""
        self.assertTrue(_looks_like_file_text(
            "新宣传.docx 部门简介+纳新宣传.docx W"))
        self.assertTrue(_looks_like_file_text("报告.xlsx"))
        self.assertTrue(_looks_like_file_text("演示文稿.PPTX"))

    def test_instruction_false(self):
        """真实用户指令不含扩展名 → 不是文件文本。"""
        self.assertFalse(_looks_like_file_text(
            "把这个做成一个赛博朋克风格的网页"))
        self.assertFalse(_looks_like_file_text(""))
        self.assertFalse(_looks_like_file_text("帮我总结一下"))


class TestFileTaskClassifyInput(unittest.TestCase):
    def test_classify_input_includes_filename(self):
        """任务判断输入 = '[文件]<文件名> <处理要求>'——LLM 需要处理对象
        才能判断是否动手类任务；仅指令（'把这个做成网页'）缺对象会误判
        闲聊。RED 复现：旧实现传纯指令，文件名丢失。"""
        bot = _make_bot()
        got = []
        bot._classify_task = lambda t: got.append(t) or {
            "is_task": True, "task": "做网页"}
        bot._dispatch_and_notify = lambda *a, **k: None
        bot._send_text = lambda *a, **k: None
        bot._add_history = lambda *a, **k: None
        bot._process_file_with_instruction(
            "小明", "王", r"D:\recv\部门简介+纳新宣传(6).docx",
            "部门简介+纳新宣传(6).docx", "内容",
            "把这个做成一个赛博朋克风格的网页")
        self.assertEqual(
            got,
            ["[文件]部门简介+纳新宣传(6).docx 把这个做成一个赛博朋克风格的网页"])


class TestImagePreviewDrive(unittest.TestCase):
    def test_preview_image_drives_process_when_msg_flow_self_only(self):
        """消息流全是 self（图片无文字、被 bot 回复顶出视口）但会话列表
        预览 [图片] → 直接触发图片处理（识别信号来自会话列表而非消息区）。
        RED 复现：旧实现 latest=sender=self → 跳过，图片永不处理。"""
        bot = _make_bot()
        bot.wx = _FakeWx([_text_msg("self", "bot回复", "m1")])
        bot.wx._session_types = {"小明": MessageType.IMAGE}
        bot._tick_poll_outbox = lambda: None
        processed = []
        bot._process_image = lambda chat, sender, msg: processed.append(
            (chat, sender))
        bot._send_text = lambda *a, **k: None
        bot.process_new_messages()
        self.assertTrue(processed, "预览 [图片] 应触发图片处理")
        self.assertEqual(processed[0][0], "小明")

    def test_preview_text_does_not_drive_image(self):
        """预览不是 [图片] 时不得触发图片处理（保持原跳过行为）。"""
        bot = _make_bot()
        bot.wx = _FakeWx([_text_msg("self", "bot回复", "m1")])
        bot.wx._session_types = {"小明": MessageType.TEXT}
        bot._tick_poll_outbox = lambda: None
        processed = []
        bot._process_image = lambda chat, sender, msg: processed.append(1)
        bot._send_text = lambda *a, **k: None
        bot.process_new_messages()
        self.assertFalse(processed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
