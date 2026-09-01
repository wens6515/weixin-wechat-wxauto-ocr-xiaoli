# -*- coding: utf-8 -*-
"""长记忆（v2）单测：recent 溢出归档、v1 迁移、深层检索（recall_memory）、
重要记忆/关键词索引注入、压缩线程触发与边界推进、消息布局（缓存友好：
稳定前缀在前、当前时间紧贴当前消息）。"""
import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import wechat_bot as wb
from xiaoli_bot import MemoryCompressor, parse_compress_json


def make_bot(tmp, **over):
    bot = wb.WeChatBot.__new__(wb.WeChatBot)
    bot.memory_file = os.path.join(tmp, "memory.json")
    bot._deep_dir = os.path.join(tmp, "memory_deep")
    bot._deep_count = {}
    bot._memory_lock = threading.RLock()
    bot.memory_db = {}
    bot.max_history = 1000
    bot.memory_deep_enabled = True
    bot.memory_compress_enabled = True
    bot.memory_keep_recent = 5
    bot.memory_compress_batch = 3
    bot.memory_important_max = 3
    bot.memory_compress_model = ""
    bot._memory_dirty = False
    bot._last_memory_save = 0.0
    bot._save_memory = lambda: None
    for k, v in over.items():
        setattr(bot, k, v)
    return bot


class TestOverflowArchive(unittest.TestCase):
    def test_overflow_appends_to_deep_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            for i in range(7):
                bot._add_history("小明", "user", f"消息{i}")
            st = bot.memory_db["小明"]
            self.assertEqual(len(st["recent"]), 5)      # keep_recent=5
            self.assertEqual(st["recent"][0]["content"], "消息2")
            self.assertEqual(bot._deep_count["小明"], 2)
            deep = list(bot._iter_deep("小明"))
            self.assertEqual([m["content"] for m in deep], ["消息0", "消息1"])
            self.assertTrue(os.path.isfile(bot._deep_path("小明")))

    def test_deep_disabled_drops_overflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp, memory_deep_enabled=False)
            for i in range(7):
                bot._add_history("小明", "user", f"消息{i}")
            self.assertEqual(len(bot.memory_db["小明"]["recent"]), 5)
            self.assertEqual(bot._deep_count, {})
            self.assertFalse(os.path.isdir(bot._deep_dir))

    def test_v1_migration_and_startup_drain(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem_path = os.path.join(tmp, "memory.json")
            with open(mem_path, "w", encoding="utf-8") as f:
                json.dump({"小明": [
                    {"role": "user", "content": f"旧{i}", "time": "t"}
                    for i in range(8)]}, f, ensure_ascii=False)
            bot = make_bot(tmp)
            bot._load_memory()
            st = bot.memory_db["小明"]
            self.assertEqual(len(st["recent"]), 5)
            self.assertEqual(bot._deep_count["小明"], 3)
            self.assertEqual(list(bot._iter_deep("小明"))[0]["content"], "旧0")


class TestRecallMemory(unittest.TestCase):
    def test_recall_hits_deep_and_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            bot._add_history("小明", "user", "我下周去福州大学报到")
            bot._add_history("小明", "assistant", "好耶，到时候拍照片给我看")
            for i in range(6):  # 把报到那条挤进深层
                bot._add_history("小明", "user", f"日常{i}")
            out = bot._recall_memory("小明", "福州大学")
            self.assertIn("福州大学报到", out)
            self.assertIn("[", out)  # 带时间戳前缀
            self.assertIn("用户", out)

    def test_recall_miss_and_empty_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            bot._add_history("小明", "user", "今天吃了火锅")
            self.assertIn("没有找到", bot._recall_memory("小明", "日语考试"))
            self.assertIn("检索词", bot._recall_memory("小明", "  "))

    def test_recall_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp, memory_deep_enabled=False)
            self.assertIn("未启用", bot._recall_memory("小明", "任何词"))

    def test_recall_case_insensitive_multi_term(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            bot._add_history("小明", "user", "我在学 DeepSeek API")
            out = bot._recall_memory("小明", "deepseek")
            self.assertIn("DeepSeek", out)


class TestImportantAndRelated(unittest.TestCase):
    def test_important_block_rendering(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            bot.memory_commit_compression("小明", 0, [
                {"content": "用户对花生过敏"}, {"content": ""}], [])
            block = bot._important_block("小明")
            self.assertIn("1. 用户对花生过敏", block)
            self.assertNotIn("2. ", block)  # 空条目不渲染
            self.assertIsNone(bot._important_block("路人"))

    def test_important_disabled_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp, memory_compress_enabled=False)
            bot.memory_commit_compression("小明", 0, [{"content": "x"}], [])
            self.assertIsNone(bot._important_block("小明"))

    def test_related_memory_keyword_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            bot.memory_commit_compression("小明", 0, [], [
                {"kw": ["报到", "福州大学"], "mem": "3月说要去福州大学报到"},
                {"kw": ["火锅"], "mem": "爱吃火锅"},
                {"kw": ["游戏"], "mem": "a"},
                {"kw": ["电影"], "mem": "b"},
                {"kw": ["音乐"], "mem": "c"},
            ])
            out = bot._match_related_memory("小明", "我报到的事定了吗")
            self.assertIn("福州大学报到", out)
            self.assertNotIn("火锅", out)
            # 最多注入 3 条
            out_all = bot._match_related_memory("小明", "火锅 游戏 电影 音乐")
            self.assertEqual(out_all.count("- "), 3)

    def test_commit_caps_and_shape_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)  # important_max=3
            bot.memory_commit_compression("小明", 10, [
                {"content": "a"}, {"content": "b"}, {"content": "c"},
                {"content": "d"}, "not-dict"], [
                {"kw": ["x"], "mem": "m1"}, {"kw": [], "mem": "无效"},
                {"kw": ["y"], "mem": ""}, "not-dict"])
            st = bot.memory_db["小明"]
            self.assertEqual([x["content"] for x in st["important"]],
                             ["b", "c", "d"])  # 上限裁掉最旧 a
            self.assertEqual(st["index"][0]["mem"], "m1")  # 坏条目被过滤
            self.assertEqual(st["indexed"], 10)


class TestParseCompressJson(unittest.TestCase):
    def test_normal_and_fenced(self):
        imp, idx = parse_compress_json(
            '{"important": [{"content": "对花生过敏"}], '
            '"index": [{"kw": ["花生"], "mem": "过敏史"}]}')
        self.assertEqual(imp, [{"content": "对花生过敏"}])
        self.assertEqual(idx, [{"kw": ["花生"], "mem": "过敏史"}])
        imp2, _ = parse_compress_json(
            '```json\n{"important": ["简写形式"], "index": []}\n```')
        self.assertEqual(imp2, [{"content": "简写形式"}])

    def test_garbage_returns_empty(self):
        self.assertEqual(parse_compress_json("完全不是JSON"), ([], []))
        self.assertEqual(parse_compress_json(""), ([], []))
        self.assertEqual(parse_compress_json('{"important": [{"content": ""}]}'),
                         ([], []))


class TestMemoryCompressor(unittest.TestCase):
    def test_scan_triggers_and_advances_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            for i in range(8):   # deep 3 条 = batch 3
                bot._add_history("小明", "user", f"事件{i}关于考研")
            self.assertEqual(bot._deep_count["小明"], 3)
            captured = []

            def fake_post(url, headers, payload, timeout, label="api", meta=None):
                captured.append(payload)
                result = {
                    "important": [{"content": "用户在准备考研"}],
                    "index": [{"kw": ["考研"], "mem": "用户近期在准备考研"}],
                }
                return {"choices": [{"message": {
                    "content": json.dumps(result, ensure_ascii=False)}}]}

            bot._post_chat_completions = fake_post
            bot.api_url = "https://x"
            bot.api_key = "k"
            bot.chat_model = "m"
            comp = MemoryCompressor(bot, scan_seconds=999)
            comp._scan_once()
            self.assertEqual(bot.memory_db["小明"]["indexed"], 3)
            self.assertEqual(bot.memory_db["小明"]["important"][0]["content"],
                             "用户在准备考研")
            self.assertEqual(captured[0]["model"], "m")  # 空 compress_model → chat_model
            # 边界推进后：同批不再重复压缩
            comp._scan_once()
            self.assertEqual(len(captured), 1)

    def test_scan_disabled_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp, memory_compress_enabled=False)
            for i in range(8):
                bot._add_history("小明", "user", f"消息{i}")
            called = []
            bot._post_chat_completions = lambda *a, **k: called.append(1)
            MemoryCompressor(bot, scan_seconds=999)._scan_once()
            self.assertEqual(called, [])

    def test_api_failure_keeps_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            for i in range(8):
                bot._add_history("小明", "user", f"消息{i}")

            def boom(*a, **k):
                raise RuntimeError("api down")

            bot._post_chat_completions = boom
            bot.api_url = "https://x"
            bot.api_key = "k"
            bot.chat_model = "m"
            MemoryCompressor(bot, scan_seconds=999)._scan_once()
            self.assertEqual(bot.memory_db["小明"]["indexed"], 0)  # 下轮重试同段


class TestMemoryDeletion(unittest.TestCase):
    """记忆页删除通道：重要记忆/索引/深层逐条删除 + indexed 边界回退。"""

    def test_delete_important_and_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            bot.memory_commit_compression("小明", 0, [
                {"content": "a"}, {"content": "b"}, {"content": "c"}], [
                {"kw": ["k1"], "mem": "m1"}, {"kw": ["k2"], "mem": "m2"}])
            self.assertTrue(bot.delete_important("小明", 2))
            self.assertEqual([x["content"] for x in bot.memory_db["小明"]["important"]],
                             ["a", "c"])
            self.assertFalse(bot.delete_important("小明", 9))  # 越界
            self.assertTrue(bot.delete_index_entry("小明", 1))
            self.assertEqual(bot.memory_db["小明"]["index"][0]["kw"], ["k2"])

    def test_delete_deep_message_count_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            for i in range(4):
                bot._add_history("小明", "user", f"深层消息{i}")
            # recent cap 5 → 4 条都在 recent？keep=5，全在 recent。造深层：
            for i in range(4):
                bot._add_history("小明", "user", f"再{i}")
            self.assertEqual(bot._deep_count["小明"], 3)  # 8-5
            self.assertTrue(bot.delete_deep_message("小明", 1))
            self.assertEqual(bot._deep_count["小明"], 2)
            self.assertEqual([m["content"] for m in bot._iter_deep("小明")],
                             ["深层消息1", "深层消息2"])
            self.assertFalse(bot.delete_deep_message("小明", 99))

    def test_delete_deep_shifts_indexed_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            for i in range(8):
                bot._add_history("小明", "user", f"消息{i}")   # deep 3 行
            bot.memory_commit_compression("小明", 1, [], [])  # 边界=1（只压了第 1 行）
            # 删边界之后的行（行号 3）→ 边界不变
            self.assertTrue(bot.delete_deep_message("小明", 3))
            self.assertEqual(bot.memory_db["小明"]["indexed"], 1)
            self.assertEqual(bot._deep_count["小明"], 2)
            # 删边界所在行（行号 1）→ 边界回退为 0
            self.assertTrue(bot.delete_deep_message("小明", 1))
            self.assertEqual(bot.memory_db["小明"]["indexed"], 0)

    def test_memory_overview_and_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = make_bot(tmp)
            bot.memory_commit_compression("小明", 0, [{"content": "过敏"}], [
                {"kw": ["花生"], "mem": "过敏史"}])
            bot._add_history("小明", "user", "你好")
            for i in range(9):
                bot._add_history("小明", "user", f"深层{i}")
            # 10 条总数，recent 5 条（深层4..8），deep 5 行（你好+深层0..3）
            ov = bot.memory_overview()
            self.assertEqual(ov["小明"], {"recent": 5, "deep": 5,
                                          "important": 1, "index": 1})
            d = bot.memory_detail("小明", deep_offset=0, deep_limit=200)
            self.assertEqual(len(d["recent"]), 5)
            self.assertEqual(d["important"][0]["content"], "过敏")
            self.assertEqual(d["deep"][0]["content"], "你好")
            # 分页：第 2 页从深层0 开始
            d2 = bot.memory_detail("小明", deep_offset=1, deep_limit=2)
            self.assertEqual([m["content"] for m in d2["deep"]],
                             ["深层0", "深层1"])
            # 过滤
            dq = bot.memory_detail("小明", deep_query="深层0")
            self.assertEqual((dq["deep_matched"], len(dq["deep"])), (1, 1))
            dq2 = bot.memory_detail("小明", deep_query="不存在")
            self.assertEqual((dq2["deep_matched"], len(dq2["deep"])), (0, 0))


class TestMessageLayout(unittest.TestCase):
    """消息布局：[人设, 重要记忆, 历史(带[ts]), 相关记忆, 当前时间, 当前消息]
    ——稳定前缀在前，每秒变化的当前时间紧贴当前消息（缓存前缀不断）。"""

    def _layout_bot(self, tmp):
        bot = make_bot(tmp)
        bot.system_prompt = "你是小漓"
        bot.chat_model = "m"
        bot.chat_temperature = 0.7
        bot._model_lock = threading.RLock()
        bot.vision_api_url = "https://x"
        bot.vision_api_key = "k"
        bot.vision_max_tokens = 10000
        bot.chat_top_p = 0.9
        bot.api_url = "https://x"
        bot.api_key = "k"
        bot.api_retry = 2
        bot.api_timeout = 5
        bot.api_wall_budget = 45
        bot.nickname = "小漓"
        bot.memory_commit_compression("小明", 0, [{"content": "对花生过敏"}], [
            {"kw": ["考研"], "mem": "用户在准备考研"}])
        bot._add_history("小明", "user", "我在准备考研")
        return bot

    def test_call_chat_ai_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._layout_bot(tmp)
            captured = []
            bot._post_chat_completions = \
                lambda url, h, payload, t, label="api", meta=None: \
                captured.append(payload) or {
                    "choices": [{"message": {"content": "好"}}]}
            bot.call_chat_ai("小明", "考研的事怎么样了")
            msgs = captured[0]["messages"]
            self.assertEqual(msgs[0], {"role": "system", "content": "你是小漓"})
            self.assertIn("对花生过敏", msgs[1]["content"])
            self.assertEqual(msgs[2]["content"], "[{ts}] 我在准备考研".format(
                ts=bot.memory_db["小明"]["recent"][0]["time"]))
            self.assertIn("相关记忆", msgs[3]["content"])
            self.assertIn("准备考研", msgs[3]["content"])
            self.assertIn("当前时间：", msgs[4]["content"])
            self.assertEqual(msgs[5]["role"], "user")
            self.assertIn("考研的事怎么样了", msgs[5]["content"])

    def test_call_vision_api_layout_and_recall_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._layout_bot(tmp)
            payloads = []

            def fake_post(url, headers, payload, timeout, label="api", meta=None):
                payloads.append(payload)
                if len(payloads) == 1:
                    return {"choices": [{"message": {"tool_calls": [
                        {"id": "r1", "type": "function",
                         "function": {"name": "recall_memory",
                                      "arguments": '{"query": "考研"}'}},
                    ]}}]}
                return {"choices": [{"message": {"content": "想起啦"}}]}

            bot._post_chat_completions = fake_post
            related = ("[相关记忆]（历史对话中与本次消息相关的内容，供参考）\n"
                       "- 用户在准备考研")
            out = bot.call_vision_api(
                [{"type": "text", "text": "hi"}], chat_id="小明",
                related_memory=related)
            self.assertEqual(out, {"kind": "text", "content": "想起啦"})
            msgs = payloads[0]["messages"]
            self.assertEqual(msgs[0]["content"], "你是小漓")
            self.assertIn("对花生过敏", msgs[1]["content"])     # 重要记忆
            self.assertIn("准备考研", msgs[2]["content"])         # 历史
            self.assertIn("准备考研", msgs[3]["content"])        # 相关记忆
            self.assertIn("当前时间：", msgs[4]["content"])      # 时间紧贴消息
            self.assertEqual(msgs[5]["role"], "user")
            names = [t["function"]["name"] for t in payloads[0]["tools"]]
            self.assertIn("recall_memory", names)
            tool_msgs = [m for m in payloads[1]["messages"]
                         if m.get("role") == "tool"]
            self.assertIn("准备考研", tool_msgs[0]["content"])   # 检索结果回填

    def test_no_important_or_related_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot = self._layout_bot(tmp)
            bot.memory_db["小明"]["important"] = []
            bot.memory_db["小明"]["index"] = []
            captured = []
            bot._post_chat_completions = \
                lambda url, h, payload, t, label="api", meta=None: \
                captured.append(payload) or {
                    "choices": [{"message": {"content": "好"}}]}
            bot.call_chat_ai("小明", "在吗")
            msgs = captured[0]["messages"]
            roles = [m["role"] for m in msgs]
            self.assertEqual(roles, ["system", "user", "system", "user"])
            self.assertIn("当前时间：", msgs[2]["content"])


if __name__ == "__main__":
    unittest.main()
