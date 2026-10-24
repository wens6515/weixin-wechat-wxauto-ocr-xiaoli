# -*- coding: utf-8 -*-
"""新记忆能力与工具开关的单测：

- MemoryStore 注入/编辑（重要记忆 pinned 截尾豁免、深层手动注入、四类编辑）
- 全局记忆（独立文件持久化、注入块、索引命中、编辑/删除、软上限）
- 共享记忆池 recall_all（来源标注、排除当前聊天、含不在 memory_db 的深层文件）
- no_reply 静默分支（记忆标注 / 占位归零 / 清红圈）
- 表情包动作写入对话历史
- save_global_memory 执行回执
- recall_memory scope 门控（池未开时 scope=全部 不放大能力）
- 工具声明开关（no_reply / save_global_memory / recall scope 参数随开关出现）
- manage_reminder modify 放开 content
- 触发回递元数据（[id]/repeat/内容 + 主动调整提示）
- _trigger_generate_reply 三态（reply / silent / fallback）
"""
import json
import os
import tempfile
import time
import unittest

from xiaoli_bot import AgentBot
from xiaoli_app.memory_store import MemoryStore
from xiaoli_app.reminders_store import RemindersStore


class _FakeWx:
    """记录发送与已读标记的假后端。"""

    def __init__(self):
        self.sent = []
        self.read_marks = 0

    def send_text(self, chat, text, hold_after_paste=0.0):
        self.sent.append((chat, text))
        return True

    def mark_session_read(self):
        self.read_marks += 1
        return True


def _store(dirpath):
    m = MemoryStore(os.path.join(dirpath, "memory.json"))
    m.load(deep_enabled=True)
    return m


def _bot(dirpath, **attrs):
    """AgentBot 最小桩：真 MemoryStore（历史可断言）+ 可控 wx。"""
    b = AgentBot.__new__(AgentBot)
    b.wx = _FakeWx()
    b.__dict__["_mem_store"] = _store(dirpath)
    b.memory_deep_enabled = True
    b.memory_compress_enabled = False
    b._pending_placeholders = {}
    b.reminders = RemindersStore(os.path.join(dirpath, "rem.json"))
    b.nickname = "小漓"
    b._pending_files = {}
    b.chat_feature_overrides = {}
    b.web_search_enabled = True
    b.task_enabled = True
    b.state_watch_enabled = False
    b.voice_mode = "off"
    b.tts_endpoint = ""
    b.voice_profiles = []
    b.active_voice_profile_id = ""
    b.model_trigger_manage = "off"
    b.sticker_mode = "off"
    b.no_reply_enabled = False
    b.global_memory_enabled = False
    b.shared_pool_enabled = False
    for k, v in attrs.items():
        setattr(b, k, v)
    return b


def _recent(b, chat):
    return b._mem.recent(chat)


# ---------- MemoryStore 注入 / 编辑 ----------


class TestMemoryInjectEdit(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="meminj_")

    def test_add_important_pinned_survives_compression_trim(self):
        m = _store(self.dir)
        self.assertTrue(m.add_important("林小满", "主人爱喝美式"))
        for i in range(25):
            m.commit_compression("林小满", 0,
                                 [{"content": f"压缩条目{i}"}], [], important_max=5)
        st = m.memory_db["林小满"]
        contents = [x["content"] for x in st["important"]]
        self.assertIn("主人爱喝美式", contents)  # pinned 豁免截尾
        self.assertTrue(st["important"][0].get("pinned"))

    def test_append_deep_manual_counts_and_recalls(self):
        m = _store(self.dir)
        m.add("王美晨", "user", "你好")
        self.assertTrue(m.append_deep_manual("王美晨", "她弟弟在福大读金融"))
        self.assertEqual(m.deep_count["王美晨"], 1)
        out = m.recall("王美晨", "弟弟")
        self.assertIn("弟弟在福大读金融", out)

    def test_edit_important_keeps_pinned_and_time(self):
        m = _store(self.dir)
        m.add_important("林小满", "旧内容")
        st = m.memory_db["林小满"]
        old_time = st["important"][0]["time"]
        self.assertTrue(m.edit_important("林小满", 1, "新内容"))
        e = st["important"][0]
        self.assertEqual(e["content"], "新内容")
        self.assertEqual(e["time"], old_time)
        self.assertTrue(e.get("pinned"))
        self.assertFalse(m.edit_important("林小满", 9, "越界"))

    def test_edit_recent_content_and_role(self):
        m = _store(self.dir)
        m.add("林小满", "user", "原话")
        self.assertTrue(m.edit_recent("林小满", 1, content="改过的话"))
        self.assertEqual(_recent_via(m, "林小满")[0]["content"], "改过的话")
        self.assertTrue(m.edit_recent("林小满", 1, role="assistant"))
        self.assertEqual(_recent_via(m, "林小满")[0]["role"], "assistant")

    def test_edit_index_kw_and_mem(self):
        m = _store(self.dir)
        m.commit_compression("林小满", 0, [], [{"kw": ["雨"], "mem": "带伞"}])
        self.assertTrue(m.edit_index("林小满", 1, kw=["下雨", "降雨"], mem="记得带伞"))
        e = m.memory_db["林小满"]["index"][0]
        self.assertEqual(e["kw"], ["下雨", "降雨"])
        self.assertEqual(e["mem"], "记得带伞")

    def test_edit_deep_message_keeps_line_count(self):
        m = _store(self.dir)
        m.append_deep_manual("林小满", "第一条")
        m.append_deep_manual("林小满", "第二条")
        self.assertTrue(m.edit_deep_message("林小满", 2, "第二条（改）"))
        msgs = list(m.iter_deep("林小满"))
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[1]["content"], "第二条（改）")
        self.assertEqual(msgs[1]["role"], "user")


def _recent_via(m, chat):
    return m.memory_db[chat]["recent"]


# ---------- 全局记忆 ----------


class TestGlobalMemory(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="memglob_")

    def test_block_and_match(self):
        m = _store(self.dir)
        self.assertIsNone(m.global_block())
        m.global_add("小漓的生日是主人生日的前一天")
        m.global_add_index(["弟弟", "福大"], "王美晨的弟弟在福大读金融")
        block = m.global_block()
        self.assertIn("生日是主人生日的前一天", block)
        hit = m.global_match("你弟弟在哪读书")
        self.assertIn("福大读金融", hit)
        self.assertIsNone(m.global_match("完全不相关的话"))

    def test_persist_across_save_load(self):
        m = _store(self.dir)
        m.global_add("全局事实")
        m.flush()
        m2 = _store(self.dir)
        self.assertIn("全局事实", m2.global_block())

    def test_edit_and_delete(self):
        m = _store(self.dir)
        m.global_add("旧全局")
        self.assertTrue(m.global_edit("important", 1, content="新全局"))
        self.assertIn("新全局", m.global_block())
        self.assertTrue(m.global_delete("important", 1))
        self.assertIsNone(m.global_block())
        self.assertFalse(m.global_delete("important", 1))

    def test_soft_cap_100(self):
        m = _store(self.dir)
        for i in range(105):
            m.global_add(f"条目{i}")
        d = m.global_detail()
        self.assertEqual(len(d["important"]), 100)
        self.assertIn("条目104", d["important"][-1]["content"])


# ---------- 共享记忆池 recall_all ----------


class TestRecallAll(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mempool_")

    def test_cross_chat_with_source_and_exclusion(self):
        m = _store(self.dir)
        m.add("王美晨", "user", "我弟弟在福大读金融")
        m.add("杨冬梅", "user", "今天做了帽子")
        m.add_important("林小满", "林小满怕黑")
        out = m.recall_all("弟弟", exclude_chat="林小满")
        self.assertIn("王美晨", out)
        self.assertIn("弟弟在福大读金融", out)
        self.assertNotIn("帽子", out)          # 未命中不出现
        self.assertNotIn("怕黑", out)          # exclude_chat 的记忆被排除

    def test_includes_deep_only_chats(self):
        m = _store(self.dir)
        m.append_deep_manual("老朋友", "一起爬过鼓山")
        m.memory_db.pop("老朋友", None)  # 只剩深层文件、不在 memory_db
        m.flush()
        out = m.recall_all("鼓山", exclude_chat="林小满")
        self.assertIn("老朋友", out)
        self.assertIn("鼓山", out)


# ---------- no_reply 静默分支 ----------


class TestNoReply(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="norep_")

    def test_history_placeholder_and_mark_read(self):
        b = _bot(self.dir)
        b._pending_placeholders["林小满"] = 2
        out = b._handle_no_reply(
            "林小满", {"kind": "tool_call", "name": "no_reply"},
            user_text="私聊 - 林小满：唉")
        self.assertTrue(out)
        rec = _recent(b, "林小满")
        self.assertEqual(rec[-2]["content"], "私聊 - 林小满：唉")
        self.assertEqual(rec[-1]["content"], "[已读未回复]")
        self.assertNotIn("林小满", b._pending_placeholders)
        self.assertEqual(b.wx.read_marks, 1)
        self.assertEqual(b.wx.sent, [])  # 静默 = 零发送


# ---------- 表情包动作写入历史 ----------


class TestStickerHistory(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="stkhst_")
        self.b = _bot(self.dir)
        self.b._deliver_reply = lambda chat, reply, trigger=False: \
            self.b.wx.sent.append((chat, reply))

    def test_sticker_plus_text_records_action_before_reply(self):
        out = self.b._apply_vision_result(
            "林小满", "林小满",
            {"kind": "text", "content": "去吃饭呀",
             "sticker_sent": True, "stickers": ["吃饭1.png"]},
            user_text="私聊 - 林小满：还没吃")
        self.assertTrue(out)
        rec = _recent(self.b, "林小满")
        self.assertEqual(rec[-3]["content"], "私聊 - 林小满：还没吃")
        self.assertEqual(rec[-2]["content"], "[发送了表情包: 吃饭1.png]")
        self.assertEqual(rec[-1]["content"], "去吃饭呀")

    def test_pure_sticker_records_filename(self):
        self.b._pending_placeholders["林小满"] = 1
        out = self.b._apply_vision_result(
            "林小满", "林小满",
            {"kind": "text", "content": "",
             "sticker_sent": True, "stickers": ["猫猫.png"]})
        self.assertTrue(out)
        rec = _recent(self.b, "林小满")
        self.assertEqual(rec[-1]["content"], "[发送了表情包: 猫猫.png]")
        self.assertNotIn("林小满", self.b._pending_placeholders)


# ---------- save_global_memory / recall scope ----------


class TestGlobalToolAndRecallScope(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gtool_")

    def test_exec_save_global_memory_receipt(self):
        b = _bot(self.dir)
        receipt = b._exec_save_global_memory("主人只喝美式")
        self.assertIn("已存入全局记忆", receipt)
        self.assertIn("美式", b._mem.global_block())
        self.assertIn("没有保存", b._exec_save_global_memory("  "))

    def test_recall_scope_gated_by_pool_switch(self):
        b = _bot(self.dir)
        b._mem.add("王美晨", "user", "弟弟在福大")
        b._mem.add("林小满", "user", "林小满的事")
        # 池关：scope=全部 也不放大能力（只查本聊天）
        out = b._recall_memory("林小满", "弟弟", scope="全部")
        self.assertIn("没有找到相关记忆", out)
        # 池开：跨聊天命中带来源
        b.shared_pool_enabled = True
        out = b._recall_memory("林小满", "弟弟", scope="全部")
        self.assertIn("王美晨", out)
        self.assertIn("福大", out)
        # 缺省 scope 仍只查本聊天
        out = b._recall_memory("林小满", "弟弟")
        self.assertIn("没有找到相关记忆", out)


# ---------- 工具声明开关 ----------


class TestToolDeclarations(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tooldec_")

    def _names(self, b, chat="林小满"):
        return [t["function"]["name"] for t in b._vision_tools(chat)]

    def test_no_reply_switch(self):
        b = _bot(self.dir)
        self.assertNotIn("no_reply", self._names(b))
        b.no_reply_enabled = True
        self.assertIn("no_reply", self._names(b))
        # 半句话场景写进描述（真机事故：模型判断出「话说到一半」却仍接话）
        tools = {t["function"]["name"]: t for t in b._vision_tools("林小满")}
        self.assertIn("没说完", tools["no_reply"]["function"]["description"])

    def test_save_global_memory_switch(self):
        b = _bot(self.dir)
        self.assertNotIn("save_global_memory", self._names(b))
        b.global_memory_enabled = True
        self.assertIn("save_global_memory", self._names(b))

    def test_recall_scope_param_switch(self):
        b = _bot(self.dir)
        tools = {t["function"]["name"]: t for t in b._vision_tools("林小满")}
        self.assertNotIn("scope", tools["recall_memory"]["function"]
                         ["parameters"]["properties"])
        # 池关：无授权指令（模型只看到本聊天检索语义）
        self.assertNotIn("主人已明确开启",
                         tools["recall_memory"]["function"]["description"])
        b.shared_pool_enabled = True
        tools = {t["function"]["name"]: t for t in b._vision_tools("林小满")}
        self.assertIn("scope", tools["recall_memory"]["function"]
                      ["parameters"]["properties"])
        # 池开：授权指令随声明注入——治模型拿「保密」推脱、从不检索的病
        desc = tools["recall_memory"]["function"]["description"]
        self.assertIn("主人已明确开启共享记忆池", desc)
        self.assertIn("scope=全部", desc)


# ---------- get_config 回显（真机事故：开关保存生效但界面永远显示关） ----------


class TestGetConfigEchoesNewSwitches(unittest.TestCase):
    def test_misc_contains_new_switch_keys(self):
        from types import SimpleNamespace
        from xiaoli_app.webbridge import BridgeApi
        cfg = {"no_reply_enabled": True, "global_memory_enabled": True,
               "shared_pool_enabled": True}
        ctx = SimpleNamespace(cfg=cfg, cfg_path="unused.json",
                              cards_dir=tempfile.mkdtemp(prefix="bri_"),
                              engine=None)
        r = BridgeApi(ctx).get_config()
        misc = r.get("misc") or {}
        self.assertIs(misc.get("no_reply_enabled"), True)
        self.assertIs(misc.get("global_memory_enabled"), True)
        self.assertIs(misc.get("shared_pool_enabled"), True)


# ---------- 私聊多消息合并不再逐行加发送者名（方案 A） ----------


class TestPrivateMultiMessageJoin(unittest.TestCase):
    """真机事故：私聊一轮攒下多条消息时，旧逻辑逐行加「王文生：」再被
    外层「私聊 - 王文生：」包一次，记忆里存下双层嵌套。前缀只在群聊且
    发送者确实不止一人时加。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pmjoin_")

    def _bot(self, is_group, msgs):
        from wx_backend.models import MessageType, WeChatMessage
        b = _bot(self.dir)
        win = {"has_other": True, "has_text": True, "has_media": False,
               "other_blocks": [{"top": 900, "bottom": 1106, "kind": "text"}],
               "bot_bottom": 900, "skip_bot": 0}
        b.wx._current_title = "王文生"
        b.wx._current_is_group = is_group
        b.wx.get_messages = lambda chat, assume_switched=False, skip_bot=0: [
            WeChatMessage(id=f"v{i}", chat=chat, sender=s, content=c,
                          type=MessageType.TEXT, y=y)
            for i, (s, c, y) in enumerate(msgs)]
        b.wx.analyze_window = \
            lambda chat, skip_bot=0, assume_switched=False: dict(win)
        captured = []
        b._handle_text = lambda chat, sender, content, msg_id=None, \
            multi_sender=False: (captured.append((content, multi_sender))
                                 or True)
        return b, captured

    def test_private_multi_message_no_per_line_prefix(self):
        b, cap = self._bot(False, [
            ("王文生", "话说", 946),
            ("王文生", "小漓你怎么会和我妈聊吃的呢", 1044)])
        self.assertTrue(b._handle_unread_session((189, 173), switched=True))
        self.assertEqual(cap[-1],
                         ("话说\n小漓你怎么会和我妈聊吃的呢", False))

    def test_group_same_sender_multi_also_plain(self):
        # 群聊消息需带 @小漓 才过群聊过滤；content 原样传给 _handle_text
        b, cap = self._bot(True, [
            ("王文生", "@小漓 话说", 946),
            ("王文生", "@小漓 别水了", 1044)])
        self.assertTrue(b._handle_unread_session((189, 173), switched=True))
        self.assertEqual(cap[-1], ("@小漓 话说\n@小漓 别水了", False))

    def test_group_distinct_senders_keep_prefix(self):
        b, cap = self._bot(True, [
            ("王文生", "@小漓 话说", 946),
            ("林小满", "@小漓 别水了", 1044)])
        self.assertTrue(b._handle_unread_session((189, 173), switched=True))
        self.assertEqual(cap[-1],
                         ("王文生：@小漓 话说\n林小满：@小漓 别水了", True))


# ---------- 空 emoji 块（微信自带小表情）路由 ----------


class TestEmojiBlocks(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="emoji_")

    def test_process_emoji_blocks_crops_and_replies(self):
        b = _bot(self.dir)
        made = []

        def fake_crop(ml, mt, mr, mb):
            fd, p = tempfile.mkstemp(suffix=".jpg")
            os.write(fd, b"x")
            os.close(fd)
            made.append(p)
            return p
        b._crop_media_region = fake_crop
        b.wx.block_screen_rect = lambda top, bottom: (10, 20, 30, 40)
        b.call_vision_api = lambda content, chat_id=None, related_memory=None: \
            {"kind": "text", "content": "收到爱心啦"}
        b._route_vision_result = lambda chat, sender, result, img_paths=None: True
        out = b._process_emoji_blocks("林小满", [{"top": 100, "bottom": 180}])
        self.assertTrue(out)
        self.assertEqual(len(made), 1)
        self.assertFalse(os.path.exists(made[0]))  # 临时文件已清理

    def test_process_emoji_blocks_no_capture_returns_none(self):
        b = _bot(self.dir)
        b.wx.block_screen_rect = lambda top, bottom: None
        self.assertIsNone(
            b._process_emoji_blocks("林小满", [{"top": 1, "bottom": 2}]))

    def test_dispatch_empty_round_routes_to_emoji(self):
        """空块轮（has_other、无文字无媒体）→ 小表情分支；识别失败也回一句
        清红圈（旧行为：整条丢弃 + 红圈滞留 8s 退避循环——真机事故）。"""
        b = _bot(self.dir)
        win = {"has_other": True, "has_text": False, "has_media": False,
               "other_blocks": [{"top": 100, "bottom": 180, "kind": "text"}],
               "bot_bottom": 100, "skip_bot": 0}
        b.wx._current_title = "林小满"
        b.wx._current_is_group = False
        b.wx.get_messages = lambda chat, assume_switched=False, skip_bot=0: []
        b.wx.analyze_window = \
            lambda chat, skip_bot=0, assume_switched=False: dict(win)
        called = []
        b._process_emoji_blocks = lambda chat, blocks: called.append(blocks) or True
        self.assertTrue(b._handle_unread_session((189, 173), switched=True))
        self.assertEqual(len(called), 1)
        # 识别失败路径：回一句清圈，不再静默丢
        b._process_emoji_blocks = lambda chat, blocks: None
        self.assertTrue(b._handle_unread_session((189, 173), switched=True))
        self.assertEqual(b.wx.sent[-1][0], "林小满")


# ---------- manage_reminder modify 放开 content ----------


class TestModifyContent(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="modcon_")

    def test_modify_time_trigger_content(self):
        b = _bot(self.dir, model_trigger_manage="lazy")
        item = b.reminders.add("王美晨", "问晚饭", time.time() + 3600,
                               "daily", source="ui")
        args = {"action": "modify", "id": item["id"],
                "content": "下班问候", "time":
                    time.strftime("%Y-%m-%d %H:%M",
                                  time.localtime(time.time() + 7200))}
        out = b._exec_manage_reminder("王美晨", json.dumps(args))
        self.assertIn("已修改", out)
        rec = b.reminders.list()[0]
        self.assertEqual(rec["content"], "下班问候")


# ---------- 触发回递元数据与三态 ----------


class TestTriggerTextAndGenerate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="trigt_")

    def test_ui_source_text_carries_meta_and_hint(self):
        item = {"id": "abc123", "repeat": "daily",
                "content": "每天晚上问晚饭", "source": "ui"}
        text = AgentBot._reminder_trigger_text("王美晨", item, "10-24 19:00")
        self.assertIn("[abc123]", text)
        self.assertIn("daily", text)
        self.assertIn("每天晚上问晚饭", text)
        self.assertIn("manage_reminder", text)  # 主动调整提示

    def test_model_source_text_carries_meta_and_hint(self):
        item = {"id": "def456", "repeat": "once", "content": "", "source": "model"}
        text = AgentBot._reminder_trigger_text("林小满", item, "10-25 09:00")
        self.assertIn("[def456]", text)
        self.assertIn("manage_reminder", text)

    def test_generate_reply_modes(self):
        b = _bot(self.dir)
        # reply：text 结果 → 手写历史 + 返回回复
        b.call_vision_api = lambda content, chat_id=None, related_memory=None: \
            {"kind": "text", "content": "晚上好呀"}
        mode, reply = b._trigger_generate_reply("林小满", "[定时触发] …")
        self.assertEqual((mode, reply), ("reply", "晚上好呀"))
        rec = _recent(b, "林小满")
        self.assertEqual(rec[-2]["role"], "user")
        self.assertEqual(rec[-1]["content"], "晚上好呀")
        # silent：no_reply 工具 → 只记历史不发送
        b.call_vision_api = lambda content, chat_id=None, related_memory=None: \
            {"kind": "tool_call", "name": "no_reply"}
        mode, _ = b._trigger_generate_reply("林小满", "[定时触发] …")
        self.assertEqual(mode, "silent")
        self.assertEqual(_recent(b, "林小满")[-1]["content"], "[触发未回复]")
        # fallback：vision 失败（None）→ 交调用方降级
        b.call_vision_api = lambda content, chat_id=None, related_memory=None: None
        mode, _ = b._trigger_generate_reply("林小满", "[定时触发] …")
        self.assertEqual(mode, "fallback")


if __name__ == "__main__":
    unittest.main()
