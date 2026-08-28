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
from unittest import mock

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

    def analyze_window(self, chat, foreground=True, skip_bot=0):
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
    bot._pending_placeholders = {}
    bot._last_poll_time = 0
    bot.tianshu_poll_interval = 5
    bot.last_reply_time = 0
    bot.cooldown = 3
    bot.file_storage_path = recv_dir
    bot.tianshu_window_title = ""
    bot.tianshu_trigger_command = "开始处理"
    bot.wx = object()  # 仅用于 _handle_text 的 is_group 探测（无 _current_is_group → 回退标题判定）
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
            bot.call_vision_api = lambda content, chat_id=None: {"kind": "tool_call",
                                                   "name": "dispatch_task",
                                                   "arguments": '{"task": "做个网站"}'}
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
        bot.call_vision_api = lambda content, chat_id=None: {"kind": "tool_call",
                                               "name": "dispatch_task",
                                               "arguments": '{"task": "做PPT"}'}
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
        bot.call_vision_api = lambda content, chat_id=None: got.append(content) or {
            "kind": "tool_call", "name": "dispatch_task",
            "arguments": '{"task": "做网页"}'}
        bot._dispatch_and_notify = lambda *a, **k: None
        bot._send_text = lambda *a, **k: None
        bot._add_history = lambda *a, **k: None
        bot._process_file_with_instruction(
            "小明", "王", r"D:\recv\部门简介+纳新宣传(6).docx",
            "部门简介+纳新宣传(6).docx",
            "把这个做成一个赛博朋克风格的网页")
        self.assertEqual(len(got), 1)
        text_block = got[0][0]
        self.assertEqual(text_block["type"], "text")
        self.assertIn(
            "[文件]部门简介+纳新宣传(6).docx 把这个做成一个赛博朋克风格的网页",
            text_block["text"])


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
        bot.call_vision_api = lambda content, chat_id=None: {"kind": "tool_call",
                                               "name": "dispatch_task",
                                               "arguments": '{"task": "做网页"}'}
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


class TestPlaceholderFlag(unittest.TestCase):
    """占位回复接线：占位类发送（'收到任务啦…' / '天枢窗口没找到…'）必须传
    placeholder=True（计数 +1），且只发生在 _dispatch_and_notify 这两处。"""

    def _dispatch(self, bot, trigger_ok=True):
        sent = []
        bot._send_text = lambda *a, **k: sent.append((a, k))
        with mock.patch("xiaoli_bot.dispatch_task", return_value="T1"), \
             mock.patch("xiaoli_app.config_store.grant_tasks_dir_to_tianshu"), \
             mock.patch("xiaoli_app.setup.resolve_cli_window",
                        return_value=("天枢", "")), \
             mock.patch("xiaoli_bot.send_trigger_to_window",
                        return_value=trigger_ok):
            bot._dispatch_and_notify("小明", "王", "做个网站",
                                     extra={"msg_id": "m1"})
        return sent

    def test_dispatch_placeholder_flag(self):
        """'收到任务啦，正在处理中，稍等一下哦～' 必须传 placeholder=True。"""
        bot = _make_bot()
        sent = self._dispatch(bot, trigger_ok=True)
        self.assertTrue(
            any("收到任务啦" in a[0] and k.get("placeholder") is True
                for a, k in sent),
            f"占位回复必须传 placeholder=True，实际: {sent}")

    def test_window_not_found_placeholder_flag(self):
        """'天枢窗口没找到，不过任务已经记下了…' 同样传 placeholder=True。"""
        bot = _make_bot()
        sent = self._dispatch(bot, trigger_ok=False)
        self.assertEqual(
            [k.get("placeholder") for a, k in sent], [True, True],
            f"两处占位发送都必须传 placeholder=True，实际: {sent}")


class TestVisionRoute(unittest.TestCase):
    """vision-exp 单调用分流：tool_call → 投递（不回复）；text → 直接回复；
    None → 降级回退。"""

    def _bot(self):
        bot = _make_bot()
        bot.call_vision_api = lambda content, chat_id=None: {"kind": "text", "content": "好的"}
        bot._dispatch_and_notify = lambda *a, **k: None
        bot._add_history = lambda *a, **k: None
        bot.call_chat_ai = lambda *a, **k: "降级回复"
        bot._send_text = lambda *a, **k: None
        return bot

    def test_tool_call_dispatches_no_reply(self):
        """kind=tool_call → 投递天枢（纯文字任务 attachment_paths=None），
        且不发送任何回复文本。"""
        bot = self._bot()
        bot.call_vision_api = lambda content, chat_id=None: {"kind": "tool_call",
                                               "name": "dispatch_task",
                                               "arguments": '{"task": "做个网站"}'}
        dispatched, sent = [], []
        bot._dispatch_and_notify = lambda *a, **k: dispatched.append((a, k))
        bot._send_text = lambda *a, **k: sent.append((a, k))
        bot._handle_text("小明", "王", "根据文档做个网站", "m1")
        self.assertEqual(len(dispatched), 1)
        a, k = dispatched[0]
        self.assertEqual((a[0], a[1], a[2]), ("小明", "王", "做个网站"))
        self.assertIsNone(k["attachment_paths"], "纯文字任务不得带附件")
        self.assertEqual(k["extra"]["msg_id"], "m1")
        self.assertEqual(k["extra"]["raw_message"], "根据文档做个网站")
        self.assertEqual(sent, [], "tool_call 分支不发送任何回复文本")

    def test_text_reply_sent(self):
        """kind=text → 直接回复（实质回复，不传 placeholder → 归零）。"""
        bot = self._bot()
        sent = []
        bot._send_text = lambda *a, **k: sent.append((a, k))
        bot._handle_text("小明", "王", "在吗", None)
        self.assertEqual(sent, [(("好的", "小明"), {})])

    def test_none_falls_back_to_chat(self):
        """call_vision_api 返回 None（API 失败）→ 降级回退普通聊天回复。"""
        bot = self._bot()
        bot.call_vision_api = lambda content, chat_id=None: None
        sent = []
        bot._send_text = lambda *a, **k: sent.append((a, k))
        bot._handle_text("小明", "王", "在吗", None)
        self.assertEqual(sent, [(("降级回复", "小明"), {})])

    def test_image_with_text_content_blocks(self):
        """图片+文字：content 块 = text + image_url(data:image base64)，
        一次 vision 调用完成判断+回复。"""
        bot = self._bot()
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.write(b"fake-jpeg-bytes")
        tmp.close()
        got = []
        bot.call_vision_api = lambda content, chat_id=None: got.append(content) or {
            "kind": "text", "content": "看到了"}
        bot._capture_latest_image = lambda chat: tmp.name
        try:
            bot._handle_image_with_text("小明", "王", "这是什么")
        finally:
            os.unlink(tmp.name)
        self.assertEqual(len(got), 1)
        content = got[0]
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        url = content[1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))
        import base64 as _b64
        self.assertEqual(_b64.b64decode(url.split(",", 1)[1]),
                         b"fake-jpeg-bytes")

    def test_image_text_none_falls_back_to_text(self):
        """图片+文字 vision 失败（None）→ 降级回退纯文字处理（保持现状）。"""
        bot = self._bot()
        bot.call_vision_api = lambda content, chat_id=None: None
        bot._capture_latest_image = lambda chat: None
        handled = []
        bot._handle_text = lambda chat, sender, content, msg_id=None, multi_sender=False: \
            handled.append(content)
        bot._handle_image_with_text("小明", "王", "在吗")
        self.assertEqual(handled, ["在吗"])


class TestSkipBotPassing(unittest.TestCase):
    """analyze_window 必须传 skip_bot=_pending_placeholders[chat]（占位计数
    跳过），按 chat_name 隔离、无记录时默认 0（等价旧行为）。"""

    def _run(self, bot, chat_msgs, pending):
        bot._pending_placeholders = pending
        seen = []

        class Wx(_FakeWx):
            def analyze_window(self, chat, foreground=True, skip_bot=0):
                seen.append((chat, skip_bot))
                return super().analyze_window(chat)

        bot.wx = Wx(chat_msgs)
        bot._tick_poll_outbox = lambda: None
        bot.call_vision_api = lambda content, chat_id=None: {"kind": "tool_call",
                                               "name": "dispatch_task",
                                               "arguments": '{"task": "做个网站"}'}
        bot._dispatch_and_notify = lambda *a, **k: None
        bot._add_history = lambda *a, **k: None
        bot.call_chat_ai = lambda *a, **k: "ok"
        bot._send_text = lambda *a, **k: None
        bot.process_new_messages()
        return seen

    def test_skip_bot_passed_from_placeholders(self):
        """_pending_placeholders['小明']=2 → analyze_window 收到 skip_bot=2
        （占位回复从窗口边界剔除）。"""
        bot = _make_bot()
        seen = self._run(bot, [_text_msg("王", "根据文档做个网站", "m1")],
                         {"小明": 2, "小红": 1})
        self.assertTrue(any(chat == "小明" and skip == 2
                            for chat, skip in seen),
                        f"应传 skip_bot=2，实际: {seen}")

    def test_skip_bot_zero_default(self):
        """无占位记录 → skip_bot=0（与旧行为完全一致）。"""
        bot = _make_bot()
        seen = self._run(bot, [_text_msg("王", "你好", "m1")], {})
        self.assertTrue(any(chat == "小明" and skip == 0
                            for chat, skip in seen),
                        f"默认 skip_bot=0，实际: {seen}")


class TestRouteVisionResultHook(unittest.TestCase):
    """AgentBot 覆写 _route_vision_result：纯图路径（_process_image 系列）vision
    单调用结果分流——tool_call → 投递天枢（带图附件）；text → 直接回复；None →
    降级。复用 _vision_route 分流核心（提取公共方法），hook 签名逐字对齐
    (self, chat_name, sender, result, img_path=None)。"""

    def _bot(self):
        bot = _make_bot()
        bot._add_history = lambda *a, **k: None
        bot._send_text = lambda *a, **k: None
        return bot

    def test_tool_call_dispatches_with_img_attachment(self):
        """tool_call + dispatch_task → 投递天枢（attachment_paths=[img_path]），
        不发送任何回复文本。"""
        bot = self._bot()
        dispatched, sent = [], []
        bot._dispatch_and_notify = lambda *a, **k: dispatched.append((a, k))
        bot._send_text = lambda *a, **k: sent.append((a, k))
        ret = bot._route_vision_result(
            "小明", "王",
            {"kind": "tool_call", "name": "dispatch_task",
             "arguments": '{"task": "根据这张图做网站"}'},
            img_path=r"D:\tmp\shot.jpg")
        self.assertTrue(ret)
        self.assertEqual(len(dispatched), 1)
        a, k = dispatched[0]
        self.assertEqual((a[0], a[1], a[2]), ("小明", "王", "根据这张图做网站"))
        self.assertEqual(k["attachment_paths"], [r"D:\tmp\shot.jpg"],
                         "纯图任务必须带截图附件投递")
        self.assertEqual(sent, [], "tool_call 分支不发送任何回复文本")

    def test_tool_call_no_img_no_attachment(self):
        """tool_call 但无 img_path → attachment_paths=None（不产生假附件）。"""
        bot = self._bot()
        dispatched = []
        bot._dispatch_and_notify = lambda *a, **k: dispatched.append((a, k))
        ret = bot._route_vision_result(
            "小明", "王",
            {"kind": "tool_call", "name": "dispatch_task",
             "arguments": '{"task": "做PPT"}'},
            img_path=None)
        self.assertTrue(ret)
        self.assertIsNone(dispatched[0][1]["attachment_paths"])

    def test_tool_call_json_fail_falls_back_raw(self):
        """arguments 非 JSON → task 降级用 arguments 原文（不丢失任务信息）。"""
        bot = self._bot()
        dispatched = []
        bot._dispatch_and_notify = lambda *a, **k: dispatched.append((a, k))
        ret = bot._route_vision_result(
            "小明", "王",
            {"kind": "tool_call", "name": "dispatch_task",
             "arguments": "不是json"},
            img_path=None)
        self.assertTrue(ret)
        self.assertEqual(dispatched[0][0][2], "不是json")

    def test_unknown_tool_name_returns_none(self):
        """tool_call 但 name != dispatch_task → 返回 None（调用方降级）。"""
        bot = self._bot()
        dispatched = []
        bot._dispatch_and_notify = lambda *a, **k: dispatched.append(1)
        ret = bot._route_vision_result(
            "小明", "王",
            {"kind": "tool_call", "name": "other_tool", "arguments": "{}"},
            img_path="x.jpg")
        self.assertIsNone(ret)
        self.assertEqual(dispatched, [], "未知工具不得投递")

    def test_text_replies_directly(self):
        """kind=text → _send_text 直接回复（实质回复，不传 placeholder → 占位归零）。"""
        bot = self._bot()
        sent = []
        bot._send_text = lambda *a, **k: sent.append((a, k))
        ret = bot._route_vision_result(
            "小明", "王", {"kind": "text", "content": "图片真好看"})
        self.assertTrue(ret)
        self.assertEqual(sent, [(("图片真好看", "小明"), {})],
                         "实质回复不传 placeholder（占位归零）")

    def test_none_result_returns_none(self):
        """result=None → 返回 None（调用方降级）。"""
        bot = self._bot()
        self.assertIsNone(bot._route_vision_result("小明", "王", None))

    def test_reuses_vision_route_core(self):
        """复用 _vision_route 分流核心：同一 tool_call dict 走同一投递路径
        （_vision_route 与 hook 对相同结果分流一致，不重复实现）。"""
        bot = self._bot()
        dispatched = []
        bot._dispatch_and_notify = lambda *a, **k: dispatched.append((a, k))
        result = {"kind": "tool_call", "name": "dispatch_task",
                  "arguments": '{"task": "做PPT"}'}
        bot.call_vision_api = lambda content, chat_id=None: result
        bot._vision_route("小明", "王", "做PPT")
        n_via_route = len(dispatched)
        bot._route_vision_result("小明", "王", result, img_path=None)
        self.assertEqual(len(dispatched), n_via_route + 1,
                         "hook 应复用与 _vision_route 相同的投递路径")


class TestGroupNameChain(unittest.TestCase):
    """群聊名字全链路保真：process_new_messages → _handle_unread_session →
    _handle_text → call_chat_ai(sender_name=发送者名)。

    RED 复现：历史缺陷 sender 在 _window_msgs 过滤/合并中丢失或回落群聊名，
    call_chat_ai 的 decorated 退化 f"群聊：{user_msg}" 无任何名字。
    """

    def test_group_at_msg_sender_reaches_call_chat_ai(self):
        """群聊 @小漓 消息：发送者名必须作为 sender_name 传到 call_chat_ai，
        is_group=True（decorated 走群聊格式）。"""
        bot = _make_bot()
        bot.wx = _FakeWx([_text_msg("哆拉A萝", "豆包有学生优惠了 @小漓", "m1")])
        bot.wx._current_is_group = True
        bot._tick_poll_outbox = lambda: None
        bot.call_vision_api = lambda content, chat_id=None: None  # vision 降级 → 回退 call_chat_ai
        calls = {}
        bot.call_chat_ai = lambda chat_id, user_msg, sender_name=None, is_group=False, multi_sender=False: \
            calls.update(chat_id=chat_id, user_msg=user_msg,
                         sender_name=sender_name, is_group=is_group,
                         multi_sender=multi_sender) or "ok"
        bot._add_history = lambda *a, **k: None
        bot._send_text = lambda *a, **k: None
        with mock.patch("xiaoli_bot.should_resume_listen",
                        return_value=(True, False, None)):
            bot.process_new_messages()
        self.assertEqual(calls.get("chat_id"), "小明")
        self.assertEqual(calls.get("user_msg"), "豆包有学生优惠了")
        self.assertEqual(calls.get("sender_name"), "哆拉A萝",
                         "发送者名必须传到 call_chat_ai")
        self.assertTrue(calls.get("is_group"),
                        "群聊消息 is_group 必须为 True（decorated 走群聊格式）")

    def test_group_multi_sender_merge_passes_flag(self):
        """多发送者合并全链路：_handle_unread_session 判定 text_candidates>1
        → _handle_text → call_chat_ai 收到 multi_sender=True 与已装饰合并文本
        （每条带各自发送者名，只包群聊名前缀在 call_chat_ai 完成）。

        RED 复现：旧实现 multi_sender 信号不存在，call_chat_ai 把最后一条
        sender 再包一层 → '群聊：群名 王文生：哆拉A萝：内容\n王文生：在吗'。"""
        bot = _make_bot()
        bot.wx = _FakeWx([
            _text_msg("哆拉A萝", "豆包有学生优惠了 @小漓", "m1"),
            _text_msg("王文生", "在吗 @小漓", "m2"),
        ])
        bot.wx._current_is_group = True
        bot._tick_poll_outbox = lambda: None
        bot.call_vision_api = lambda content, chat_id=None: None  # vision 降级 → 回退 call_chat_ai
        calls = {}
        bot.call_chat_ai = lambda chat_id, user_msg, sender_name=None, is_group=False, multi_sender=False: \
            calls.update(chat_id=chat_id, user_msg=user_msg,
                         sender_name=sender_name, is_group=is_group,
                         multi_sender=multi_sender) or "ok"
        bot._add_history = lambda *a, **k: None
        bot._send_text = lambda *a, **k: None
        with mock.patch("xiaoli_bot.should_resume_listen",
                        return_value=(True, False, None)):
            bot.process_new_messages()
        self.assertTrue(calls.get("is_group"),
                        "群聊消息 is_group 必须为 True")
        self.assertTrue(calls.get("multi_sender"),
                        "多发送者必须透传 multi_sender=True 到 call_chat_ai")
        user_msg = calls.get("user_msg", "")
        self.assertIn("哆拉A萝：豆包有学生优惠了", user_msg,
                      "合并文本第一条带各自发送者名")
        self.assertIn("王文生：在吗", user_msg,
                      "合并文本第二条带各自发送者名")


class TestGroupNameDecoratedFinal(unittest.TestCase):
    """群聊名字·最终 decorated 全链路回归（不 mock 中间层）。

    瑶光盲区补强：wave1 测试（TestGroupMultiSenderText）mock 掉 _handle_text
    只覆盖合并层，未验证最终 decorated；本类走 process_new_messages →
    _handle_unread_session → _handle_text → call_chat_ai 全链路真实执行，
    只 mock 网络层（requests.post / vision 降级），直接断言发送给 AI 的
    最终 decorated 文本——middle 层（_handle_text / _handle_unread_session /
    call_chat_ai）一个都不 mock。

    RED 复现（wave1 缺陷）：多发送者时 call_chat_ai 把最后一条 sender 再包
    一层 → '群聊：群名 王文生：哆拉A萝：内容\n王文生：在吗' 双层嵌套。
    """

    def _final_decorated(self, msgs, is_group=True):
        """全链路跑一遍 process_new_messages，捕获 call_chat_ai 最终发给
        AI 的 user content（真实执行 call_chat_ai，不 mock 中间层）。"""
        import threading

        bot = _make_bot()
        bot.wx = _FakeWx(msgs)
        bot.wx._current_is_group = is_group
        bot._tick_poll_outbox = lambda: None
        bot.call_vision_api = lambda content, chat_id=None: None  # vision 降级 → 回退 call_chat_ai
        # call_chat_ai 真实执行所需属性（不 mock call_chat_ai 本身）
        bot.api_url = "https://api.test/v1/chat/completions"
        bot.api_key = "test-key"
        bot.chat_model = "test-model"
        bot.chat_temperature = 0.7
        bot.chat_top_p = 0.9
        bot.api_retry = 0
        bot.api_timeout = 5
        bot.system_prompt = "你是小漓"
        bot._model_lock = threading.RLock()
        bot._get_history = lambda chat_id: []
        bot._add_history = lambda *a, **k: None
        bot._send_text = lambda *a, **k: None

        sent = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            sent["json"] = json
            return SimpleNamespace(
                status_code=200, text="{}",
                json=lambda: {"choices": [{"message": {"content": "ok"}}]},
            )

        with mock.patch("wechat_bot.requests.post", side_effect=fake_post), \
             mock.patch("xiaoli_bot.should_resume_listen",
                        return_value=(True, False, None)):
            bot.process_new_messages()
        self.assertIn("json", sent, "call_chat_ai 必须真实执行并发出请求")
        content = sent["json"]["messages"][-1]["content"]
        # 首条消息（桩历史恒为空）注入【角色沉浸要求】——decorated 格式
        # 断言前断言注入存在并剥掉尾缀
        import wechat_bot as wb
        suffix = "\n\n" + wb.ROLE_IMMERSION_PROMPT
        self.assertTrue(content.endswith(suffix), "首条消息必须注入角色沉浸要求")
        return content[:-len(suffix)]

    def test_single_group_msg_decorated(self):
        """单条群聊 @：最终 decorated = 群聊名 + 发送者名 + 内容。"""
        content = self._final_decorated([
            _text_msg("哆拉A萝", "豆包有学生优惠了@小漓", "m1"),
        ])
        self.assertEqual(content, "群聊：小明 哆拉A萝：豆包有学生优惠了",
                         "单条群聊最终 decorated 必须为「群聊名+发送者名+内容」")

    def test_multi_sender_decorated_no_double_wrap(self):
        """多发送者合并全链路：最终 decorated = 群聊名一次 + 每条 sender
        各自在内容前 + 无双层嵌套。
        RED 复现：旧实现把最后一条 sender 再包一层 →
        '群聊：群名 王文生：哆拉A萝：内容\n王文生：在吗'。"""
        content = self._final_decorated([
            _text_msg("哆拉A萝", "豆包有学生优惠了@小漓", "m1"),
            _text_msg("王文生", "在吗@小漓", "m2"),
        ])
        self.assertEqual(content,
                         "群聊：小明 哆拉A萝：豆包有学生优惠了\n王文生：在吗",
                         "多发送者最终 decorated：群聊名只一次、每条 sender 各自")
        self.assertEqual(content.count("群聊：小明"), 1, "群聊名只能出现一次")
        self.assertNotIn("王文生：哆拉A萝", content, "不得出现双层嵌套")

    def test_group_no_sender_fallback_chat_name(self):
        """sender 缺失（空字符串，视觉层未读到）：最终 decorated 用群聊名
        兜底（群聊名+内容），并打 warning，不退化无名字分支。"""
        with self.assertLogs("xiaoli", level="WARNING") as logs:
            content = self._final_decorated([
                _text_msg("", "豆包有学生优惠了@小漓", "m1"),
            ])
        self.assertEqual(content, "群聊：小明：豆包有学生优惠了",
                         "sender 缺失时用群聊名兜底")
        self.assertTrue(
            any("视觉层未读到发送者名" in line for line in logs.output),
            "sender 缺失必须打 warning 日志")

    def test_private_format_unchanged(self):
        """私聊全链路：格式保持「私聊 - 发送者名：内容」不变。"""
        content = self._final_decorated([
            _text_msg("王文生", "在吗", "m1"),
        ], is_group=False)
        self.assertEqual(content, "私聊 - 王文生：在吗",
                         "私聊格式保持不变")
