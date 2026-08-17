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

from xiaoli_bot import AgentBot, _looks_like_file_text, poll_outbox
from wx_backend.models import MessageType


def _text_msg(sender, content, mid):
    return SimpleNamespace(sender=sender, content=content, id=mid,
                           type=MessageType.TEXT)


class _FakeWx:
    """最小 wx 桩：一个会话、消息列表（窗口边界协议）。"""

    def __init__(self, msgs, has_media=False, bot_bottom=None):
        self._msgs = msgs
        self._has_media = has_media
        self._bot_bottom = bot_bottom
        self._session_types = {}

    def iter_unread_with_type(self):
        return iter([("小明", MessageType.TEXT)])

    def iter_unread_sessions(self):
        return iter(["小明"])

    def analyze_window(self, chat):
        return {
            "bot_bottom": self._bot_bottom,
            "other_text": [],
            "other_media": [],
            "has_text": True,
            "has_media": self._has_media,
            "width": 747, "height": 1135,
        }

    def get_messages(self, chat):
        return self._msgs


def _make_bot(recv_dir=""):
    bot = AgentBot.__new__(AgentBot)
    bot.nickname = "小漓"
    bot.task_enabled = True
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
            "部门简介+纳新宣传(6).docx",
            "把这个做成一个赛博朋克风格的网页")
        self.assertEqual(
            got,
            ["[文件]部门简介+纳新宣传(6).docx 把这个做成一个赛博朋克风格的网页"])


class TestImageMediaDrive(unittest.TestCase):
    def test_media_drives_process_when_msg_flow_self_only(self):
        """消息流全是 self（图片无文字、被 bot 回复顶出视口）但窗口媒体
        矩形检测到媒体 → 触发图片处理（识别信号来自媒体矩形而非消息区）。"""
        bot = _make_bot()
        bot.wx = _FakeWx([_text_msg("self", "bot回复", "m1")], has_media=True)
        bot._tick_poll_outbox = lambda: None
        processed = []
        bot._process_image = lambda chat, sender, msg: processed.append(
            (chat, sender))
        bot._send_text = lambda *a, **k: None
        bot.process_new_messages()
        self.assertTrue(processed, "媒体矩形应触发图片处理")
        self.assertEqual(processed[0][0], "小明")

    def test_no_media_does_not_drive_image(self):
        """无媒体矩形且无对方文字时不得触发图片处理（窗口空 → 跳过）。"""
        bot = _make_bot()
        bot.wx = _FakeWx([_text_msg("self", "bot回复", "m1")], has_media=False)
        bot._tick_poll_outbox = lambda: None
        processed = []
        bot._process_image = lambda chat, sender, msg: processed.append(1)
        bot._send_text = lambda *a, **k: None
        bot.process_new_messages()
        self.assertFalse(processed)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestPollOutboxIdempotent(unittest.TestCase):
    """poll_outbox 幂等：归档是 deliver 的前置条件，绝不二次投递（真机根因：
    15:03 同一任务 20260816150016f8cc 重复回传两次，成果重复发送）。"""

    def _make_task(self, tasks, name):
        import json as _json
        task_dir = os.path.join(tasks, name)
        os.makedirs(task_dir)
        with open(os.path.join(task_dir, "task.json"), "w", encoding="utf-8") as f:
            _json.dump({"chat_name": "小明"}, f)
        with open(os.path.join(task_dir, "result.json"), "w", encoding="utf-8") as f:
            _json.dump({"reply_text": "完成"}, f)
        return task_dir

    def test_already_archived_skips_deliver(self):
        """sent 下已有同名归档目录 → 跳过 deliver 并清理残留顶层目录。"""
        tasks = tempfile.mkdtemp(prefix="poll_")
        try:
            task_dir = self._make_task(tasks, "20260816150016f8cc")
            sent_dir = os.path.join(tasks, "sent")
            os.makedirs(os.path.join(sent_dir, "20260816150016f8cc"))
            delivered = []
            poll_outbox(tasks, lambda d, i, r: delivered.append(1))
            self.assertEqual(delivered, [], "已归档任务不得二次 deliver")
            self.assertFalse(os.path.isdir(task_dir), "残留顶层目录应被清理")
        finally:
            import shutil
            shutil.rmtree(tasks, ignore_errors=True)

    def test_move_fail_no_deliver(self):
        """move 失败（文件被占用）→ 不 deliver，抛异常回滚重试。"""
        from unittest import mock as _mock
        tasks = tempfile.mkdtemp(prefix="poll_")
        try:
            self._make_task(tasks, "t1")
            delivered = []
            with _mock.patch("xiaoli_bot.shutil.move",
                             side_effect=OSError("文件被占用")):
                poll_outbox(tasks, lambda d, i, r: delivered.append(1))
            self.assertEqual(delivered, [], "move 失败不得 deliver")
        finally:
            import shutil
            shutil.rmtree(tasks, ignore_errors=True)


class TestFileTaskNoFileText(unittest.TestCase):
    def test_task_dispatch_no_file_text(self):
        """任务分支只投文件本体，不提取文字、不写 file_text（真机根因：
        task.json 里出现多余的 file_text 字段）。"""
        bot = _make_bot()
        dispatched = []
        bot._classify_task = lambda t: {"is_task": True, "task": "做网页"}
        bot._dispatch_and_notify = lambda *a, **k: dispatched.append(k)
        bot._add_history = lambda *a, **k: None
        bot._send_text = lambda *a, **k: None

        def _boom_extract(p):
            raise AssertionError("任务分支不得提取文件文字")

        bot._extract_file_text = _boom_extract
        bot._process_file_with_instruction(
            "小明", "王", r"D:\recv\报告.docx", "报告.docx", "把这个做成网页")
        self.assertEqual(len(dispatched), 1)
        k = dispatched[0]
        self.assertNotIn("file_text", k["extra"], "任务分支不得写 file_text")
        self.assertEqual(k["attachment_paths"], [r"D:\recv\报告.docx"])

    def test_file_with_media_passes_extra_attachments(self):
        """文件+图片组合：图片截图作为 extra_attachments 传给文件处理，
        最终与文件一并投递（真机根因：file_text 分支吞掉图片）。"""
        recv_dir = tempfile.mkdtemp(prefix="recv_")
        try:
            bot = _make_bot(recv_dir=recv_dir)
            got = []
            bot._find_file_by_display_name = lambda name: r"D:\recv\报告.docx"
            bot._extract_file_text = lambda p: "内容"
            bot._process_file_with_instruction = lambda *a, **k: got.append(
                k.get("extra_attachments"))
            bot._send_text = lambda *a, **k: None
            bot._handle_file_message(
                "小明", "王", "报告.docx", "处理一下",
                extra_attachments=[r"C:\tmp\img.jpg"])
            self.assertEqual(got, [[r"C:\tmp\img.jpg"]],
                             "文件+图片组合时图片截图应传给文件处理")
        finally:
            import shutil
            shutil.rmtree(recv_dir, ignore_errors=True)
