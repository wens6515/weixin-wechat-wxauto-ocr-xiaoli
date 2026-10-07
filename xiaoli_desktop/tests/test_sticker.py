# -*- coding: utf-8 -*-
"""表情包功能测试：
- StickerStore：文件名播种（零 API 即刻可用）/ manifest 往返 / 清单行 /
  关键词搜索排序 / resolve 路径安全（逃逸拒绝）/ reindex 打标写回；
- 工具声明三态（off 不声明 / catalog 仅 send_sticker / query 双工具）；
- catalog 清单注入；send_sticker 循环执行（成功置 sticker_sent；空文本
  = 纯表情包回复不降级；带文本 = 表情+文字）；发送失败不置位。
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_bot import REPLY_MAX_TOKENS, WeChatBot
from xiaoli_app.sticker_store import (StickerStore, _stem_desc,
                                      parse_sticker_desc)


def _make_lib(files=("生气1.png", "开心《哈哈》.gif", "猫猫.png")):
    d = tempfile.mkdtemp()
    for f in files:
        with open(os.path.join(d, f), "wb") as fh:
            fh.write(b"\x89PNG\r\n" if f.endswith("png") else b"GIF89a")
    return d


def _make_bot(mode="off"):
    bot = WeChatBot.__new__(WeChatBot)
    bot.memory_file = os.path.join(tempfile.mkdtemp(), "memory.json")
    bot.system_prompt = "人设"
    bot.nickname = "小漓"
    bot.api_url = "https://api.test/v1/chat/completions"
    bot.api_key = "k"
    bot.vision_api_url = bot.api_url
    bot.vision_api_key = "k"
    bot.chat_model = "test-model"
    bot.chat_temperature = 0.7
    bot.chat_top_p = 0.9
    bot.reply_max_tokens = REPLY_MAX_TOKENS
    bot._model_lock = threading.RLock()
    bot.memory_compress_enabled = False
    bot.memory_deep_enabled = False
    bot.reminders = None
    bot.model_trigger_manage = "off"
    bot.sticker_mode = mode
    bot.web_search_enabled = False
    bot.task_enabled = False
    bot.voice_mode = "off"
    bot.tts_endpoint = ""
    bot.voice_profiles = []
    bot.active_voice_profile_id = ""
    bot._post_chat_completions = mock.Mock(
        return_value={"choices": [{"message": {"content": "好"}}]})
    bot.__dict__.pop("_sticker_store_inst", None)
    return bot


class _LibBotTest(unittest.TestCase):
    """表情包库 patch 基类：default_stickers_dir 指向本测试的临时库。"""

    def setUp(self):
        self.lib = _make_lib()
        p = mock.patch("xiaoli_app.sticker_store.default_stickers_dir",
                       return_value=self.lib)
        p.start()
        self.addCleanup(p.stop)

    def _bot(self, mode):
        return _make_bot(mode=mode)


def _tool_names(bot):
    bot.call_vision_api([{"type": "text", "text": "hi"}], chat_id="林小满")
    payload = bot._post_chat_completions.call_args.args[2]
    return [t["function"]["name"] for t in payload.get("tools", [])]


def _run_tool(bot, name, args, final_content=""):
    """把工具调用喂进 call_vision_api 循环，返回 (最终 result, tool 回执)。"""
    def _resp(name_, args_):
        return {"choices": [{"message": {"tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": name_,
                         "arguments": json.dumps(args_, ensure_ascii=False)},
        }]}}]}
    bot._post_chat_completions = mock.Mock(side_effect=[
        _resp(name, args),
        {"choices": [{"message": {"content": final_content}}]},
    ])
    result = bot.call_vision_api([{"type": "text", "text": "hi"}],
                                 chat_id="林小满")
    tool_msgs = [m for m in
                 bot._post_chat_completions.call_args.args[2]["messages"]
                 if m.get("role") == "tool"]
    return result, (tool_msgs[-1]["content"] if tool_msgs else "")


class TestStemDesc(unittest.TestCase):
    def test_seed_rules(self):
        self.assertEqual(_stem_desc("生气1.png"), "生气")
        self.assertEqual(_stem_desc("《呜》.png"), "呜")
        self.assertEqual(_stem_desc("骄傲2《最肥》.png"), "骄傲2《最肥")
        self.assertEqual(_stem_desc("猫猫.png"), "猫猫")


class TestParseStickerDesc(unittest.TestCase):
    def test_json_output(self):
        d = parse_sticker_desc('{"tags": ["生气", "愤怒"], "desc": "气到跳脚"}')
        self.assertEqual(d["tags"], ["生气", "愤怒"])
        self.assertEqual(d["desc"], "气到跳脚")

    def test_non_json_falls_back_to_text(self):
        d = parse_sticker_desc("这是张生气的猫")
        self.assertEqual(d["desc"], "这是张生气的猫")
        self.assertEqual(d["tags"], [])

    def test_empty(self):
        self.assertEqual(parse_sticker_desc(""), {"tags": [], "desc": ""})


class TestStickerStore(unittest.TestCase):
    def test_seed_from_filenames_without_manifest(self):
        st = StickerStore(_make_lib())
        ents = st.entries()
        self.assertEqual(len(ents), 3)
        by_file = {e["file"]: e for e in ents}
        self.assertEqual(by_file["生气1.png"]["desc"], "生气")
        self.assertEqual(by_file["开心《哈哈》.gif"]["desc"], "开心《哈哈")

    def test_manifest_roundtrip_and_deleted_file_dropped(self):
        d = _make_lib()
        st = StickerStore(d)
        st._save([{"file": "生气1.png", "desc": "气鼓鼓", "tags": ["生气"]},
                  {"file": "已删除.png", "desc": "x", "tags": []}])
        ents = st.entries()
        by_file = {e["file"]: e for e in ents}
        self.assertEqual(by_file["生气1.png"]["desc"], "气鼓鼓")  # manifest 描述优先
        self.assertNotIn("已删除.png", by_file)  # 文件是事实源，已删不出现
        self.assertIn("猫猫.png", by_file)  # 未描述文件播种照常出现

    def test_catalog_lines_format(self):
        st = StickerStore(_make_lib())
        lines = st.catalog_lines()
        self.assertIn("生气1.png｜生气", lines)

    def test_search_ranking(self):
        d = _make_lib(("生气1.png", "生气2.png", "猫猫.png"))
        st = StickerStore(d)
        st._save([
            {"file": "生气1.png", "desc": "气到冒烟", "tags": ["生气", "愤怒"]},
            {"file": "生气2.png", "desc": "轻轻生气", "tags": ["生气"]},
            {"file": "猫猫.png", "desc": "可爱猫猫", "tags": ["猫"]},
        ])
        hits = st.search("生气 愤怒")
        self.assertEqual(hits[0]["file"], "生气1.png")  # 双关键词命中排前
        self.assertEqual(len(hits), 2)
        self.assertEqual(st.search("猫猫")[0]["file"], "猫猫.png")
        self.assertEqual(st.search("zzz"), [])

    def test_resolve_safety(self):
        d = _make_lib()
        st = StickerStore(d)
        self.assertTrue(st.resolve("生气1.png").endswith("生气1.png"))
        self.assertIsNone(st.resolve("../memory.json"))
        self.assertIsNone(st.resolve("不存在.png"))
        self.assertIsNone(st.resolve("manifest.json"))
        self.assertIsNone(st.resolve("memory.json"))
        self.assertIsNone(st.resolve(""))

    def test_reindex_writes_manifest_and_survives_failure(self):
        d = _make_lib(("a.png", "b.png"))
        st = StickerStore(d)

        def describe(path):
            if os.path.basename(path) == "b.png":
                raise RuntimeError("API 挂了")
            return {"tags": ["测试"], "desc": "描述" + os.path.basename(path)}

        ok, total = st.reindex(describe)
        self.assertEqual((ok, total), (1, 2))
        by_file = {e["file"]: e for e in st.entries()}
        self.assertEqual(by_file["a.png"]["tags"], ["测试"])
        self.assertEqual(by_file["b.png"]["desc"], "b")  # 失败回退文件名播种

    def test_set_entry_manual_edit(self):
        d = _make_lib()
        st = StickerStore(d)
        self.assertTrue(st.set_entry("生气1.png", "气到冒烟", "生气, 愤怒 干饭"))
        row = {e["file"]: e for e in st.entries()}["生气1.png"]
        self.assertEqual(row["desc"], "气到冒烟")
        self.assertEqual(row["tags"], ["生气", "愤怒", "干饭"])  # 字符串拆分
        # 其他条目不受影响，且 desc 留空回退文件名播种
        self.assertTrue(st.set_entry("猫猫.png", "", []))
        row2 = {e["file"]: e for e in st.entries()}["猫猫.png"]
        self.assertEqual(row2["desc"], "猫猫")
        self.assertEqual(len(st.entries()), 3)

    def test_set_entry_rejects_missing_or_escape(self):
        st = StickerStore(_make_lib())
        self.assertFalse(st.set_entry("不存在.png", "x", []))
        self.assertFalse(st.set_entry("../memory.json", "x", []))
        self.assertFalse(st.set_entry("manifest.json", "x", []))

    def test_catalog_text_roundtrip(self):
        from xiaoli_app.sticker_store import catalog_text
        self.assertEqual(catalog_text([]), "")
        text = catalog_text(["a.png｜生气"])
        self.assertTrue(text.startswith("你可以发送表情包"))
        self.assertIn("a.png｜生气", text)


class TestToolDeclaration(_LibBotTest):
    def test_off_no_tools(self):
        self.assertNotIn("send_sticker", _tool_names(self._bot("off")))
        self.assertNotIn("search_stickers", _tool_names(self._bot("off")))

    def test_catalog_mode_send_only(self):
        names = _tool_names(self._bot("catalog"))
        self.assertIn("send_sticker", names)
        self.assertNotIn("search_stickers", names)

    def test_query_mode_both_tools(self):
        names = _tool_names(self._bot("query"))
        self.assertIn("send_sticker", names)
        self.assertIn("search_stickers", names)

    def test_missing_lib_no_tools_even_when_enabled(self):
        """库目录不存在 = 整体不激活（模型看不到工具）。"""
        p = mock.patch("xiaoli_app.sticker_store.default_stickers_dir",
                       return_value=os.path.join(tempfile.mkdtemp(), "不存在"))
        p.start()
        self.addCleanup(p.stop)
        self.assertNotIn("send_sticker", _tool_names(self._bot("catalog")))

    def test_catalog_injects_listing(self):
        bot = self._bot("catalog")
        bot.call_vision_api([{"type": "text", "text": "hi"}], chat_id="林小满")
        msgs = bot._post_chat_completions.call_args.args[2]["messages"]
        self.assertTrue(any(m["role"] == "system" and "send_sticker" in m.get("content", "")
                            for m in msgs))

    def test_query_mode_no_catalog_injection(self):
        bot = self._bot("query")
        bot.call_vision_api([{"type": "text", "text": "hi"}], chat_id="林小满")
        msgs = bot._post_chat_completions.call_args.args[2]["messages"]
        self.assertFalse(any(m["role"] == "system" and "send_sticker" in m.get("content", "")
                             for m in msgs))


class TestToolExecution(_LibBotTest):
    def test_send_ok_sets_sticker_sent(self):
        bot = self._bot("catalog")
        bot._send_sticker_file = mock.Mock(return_value=True)
        result, receipt = _run_tool(bot, "send_sticker",
                                    {"file": "生气1.png"})
        self.assertTrue(bot._send_sticker_file.called)
        self.assertIn("已发出", receipt)
        self.assertTrue(result["sticker_sent"])

    def test_sticker_then_empty_text_is_pure_sticker(self):
        """发送成功 + 模型不再输出文字 = 纯表情包回复（不降级）。"""
        bot = self._bot("catalog")
        bot._send_sticker_file = mock.Mock(return_value=True)
        result, _ = _run_tool(bot, "send_sticker", {"file": "生气1.png"},
                              final_content="")
        self.assertEqual(result["kind"], "text")
        self.assertEqual(result["content"], "")
        self.assertTrue(result["sticker_sent"])

    def test_sticker_then_text_carries_flag(self):
        bot = self._bot("catalog")
        bot._send_sticker_file = mock.Mock(return_value=True)
        result, _ = _run_tool(bot, "send_sticker", {"file": "生气1.png"},
                              final_content="别生气啦")
        self.assertEqual(result["content"], "别生气啦")
        self.assertTrue(result["sticker_sent"])

    def test_send_failure_no_flag_no_fake_success(self):
        bot = self._bot("catalog")
        bot._send_sticker_file = mock.Mock(return_value=False)
        result, receipt = _run_tool(bot, "send_sticker",
                                    {"file": "生气1.png"},
                                    final_content="那你休息吧")
        self.assertFalse(result.get("sticker_sent"))
        self.assertIn("没发出去", receipt)

    def test_unknown_file_rejected(self):
        bot = self._bot("catalog")
        bot._send_sticker_file = mock.Mock()
        _, receipt = _run_tool(bot, "send_sticker", {"file": "不存在.png"})
        self.assertFalse(bot._send_sticker_file.called)
        self.assertIn("没有", receipt)

    def test_off_mode_rejects(self):
        bot = self._bot("off")
        bot._send_sticker_file = mock.Mock()
        _, receipt = _run_tool(bot, "send_sticker", {"file": "生气1.png"})
        self.assertIn("未开启", receipt)

    def test_search_returns_candidates(self):
        bot = self._bot("query")
        result, receipt = _run_tool(bot, "search_stickers", {"query": "生气"},
                                    final_content="我看看哈")
        self.assertIn("生气1.png", receipt)
        self.assertEqual(result["content"], "我看看哈")
        self.assertFalse(result.get("sticker_sent"))

    def test_search_empty_query_returns_full_catalog(self):
        """query 留空 = 完整清单（模型自己决定搜还是全览，搜索不中的兜底）。"""
        bot = self._bot("query")
        result, receipt = _run_tool(bot, "search_stickers", {},
                                    final_content="我挑挑")
        for f in ("生气1.png", "猫猫.png", "开心《哈哈》.gif"):
            self.assertIn(f, receipt)
        self.assertIn("完整清单", receipt)
        self.assertEqual(result["content"], "我挑挑")
        self.assertFalse(result.get("sticker_sent"))

    def test_search_stickers_schema_query_optional(self):
        """工具 schema：query 不再必填——留空拿全清单是模型可选项。"""
        bot = self._bot("query")
        bot.call_vision_api([{"type": "text", "text": "hi"}], chat_id="林小满")
        payload = bot._post_chat_completions.call_args.args[2]
        fn = next(t["function"] for t in payload["tools"]
                  if t["function"]["name"] == "search_stickers")
        self.assertNotIn("query", fn["parameters"].get("required", []))
        self.assertIn("留空", fn["description"])


class TestApplyVisionResultSticker(unittest.TestCase):
    """纯表情包回复的上层分流：记记忆 + 占位归零，绝不降级。"""

    def _mini_agent(self):
        from xiaoli_bot import AgentBot

        class Mini(AgentBot):
            def __init__(self):
                self._pending_placeholders = {}
                self.history = []
                self.delivered = []

            def _add_history(self, chat, role, content):
                self.history.append((chat, role, content))

            def _deliver_reply(self, chat, text, trigger=False):
                self.delivered.append((chat, text))
                # 镜像真实 _send_text 语义：实质回复发出即占位归零
                self._pending_placeholders.pop(chat, None)

        return Mini()

    def test_pure_sticker_records_history_and_resets_placeholder(self):
        ag = self._mini_agent()
        ag._pending_placeholders["林小满"] = 1
        r = ag._apply_vision_result(
            "林小满", "林小满",
            {"kind": "text", "content": "", "sticker_sent": True},
            user_text="气死我了")
        self.assertTrue(r)
        self.assertEqual(ag.delivered, [])
        self.assertEqual(ag.history, [
            ("林小满", "user", "气死我了"),
            ("林小满", "assistant", "[发送了表情包]"),
        ])
        self.assertEqual(ag._pending_placeholders, {})

    def test_sticker_plus_text_delivers_text(self):
        ag = self._mini_agent()
        ag._pending_placeholders["林小满"] = 1
        r = ag._apply_vision_result(
            "林小满", "林小满",
            {"kind": "text", "content": "消消气", "sticker_sent": True},
            user_text="气死我了")
        self.assertTrue(r)
        self.assertEqual(ag.delivered, [("林小满", "消消气")])
        self.assertEqual(ag._pending_placeholders, {})

    def test_empty_without_sticker_still_degrades(self):
        ag = self._mini_agent()
        r = ag._apply_vision_result("林小满", "林小满",
                                    {"kind": "text", "content": ""})
        self.assertIsNone(r)


if __name__ == "__main__":
    unittest.main()
