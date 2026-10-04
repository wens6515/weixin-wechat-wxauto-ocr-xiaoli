# -*- coding: utf-8 -*-
"""记忆全量导出测试：export_chat_data 四层组装（近期/重要/索引/深层）+
Markdown 渲染。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app.memory_store import MemoryStore, render_export_markdown


def _make_store(tmp):
    st = MemoryStore(memory_file=os.path.join(tmp, "memory.json"))
    st.load(deep_enabled=True)
    return st


class TestExportChatData(unittest.TestCase):
    def test_assembles_all_layers(self):
        tmp = tempfile.mkdtemp(prefix="xiaoli_exp_")
        st = _make_store(tmp)
        st.add("林小满", "user", "你好")
        st.add("林小满", "assistant", "在呀")
        st.append_deep("林小满", {"role": "user",
                                  "content": "很久以前的话",
                                  "time": "2026-01-01 00:00:00"})
        st.memory_db["林小满"]["important"] = [{"content": "喜欢蓝色"}]
        st.memory_db["林小满"]["index"] = [{"kw": ["颜色"], "mem": "喜欢蓝色"}]
        data = st.export_chat_data("林小满")
        self.assertEqual(data["chat"], "林小满")
        self.assertEqual(len(data["recent"]), 2)
        self.assertEqual(data["recent"][-1]["content"], "在呀")
        self.assertEqual(data["deep_count"], 1)
        self.assertEqual(data["deep"][0]["content"], "很久以前的话")
        self.assertEqual(data["important"][0]["content"], "喜欢蓝色")
        self.assertEqual(data["index"][0]["kw"], ["颜色"])

    def test_recent_snapshot_is_copy(self):
        """导出返回的是锁内拷贝：改动导出结果不影响 store 内状态。"""
        tmp = tempfile.mkdtemp(prefix="xiaoli_exp_")
        st = _make_store(tmp)
        st.add("林小满", "user", "你好")
        data = st.export_chat_data("林小满")
        data["recent"].clear()
        self.assertEqual(len(st.recent("林小满")), 1)


class TestRenderExportMarkdown(unittest.TestCase):
    def test_render_contains_all_sections(self):
        data = {"chat": "林小满",
                "recent": [{"role": "user", "content": "你好", "time": "t1"},
                           {"role": "assistant", "content": "在呀\n（笑）",
                            "time": "t2"}],
                "important": [{"content": "喜欢蓝色"}],
                "index": [{"kw": ["颜色", "蓝色"], "mem": "喜欢蓝色系"}],
                "deep": [{"role": "user", "content": "旧话", "time": "t0"}],
                "deep_count": 1}
        md = render_export_markdown(data)
        self.assertIn("# 小漓聊天记忆导出 · 林小满", md)
        self.assertIn("近期窗口：2 条", md)
        self.assertIn("## 近期对话", md)
        self.assertIn("[t1] 用户: 你好", md)
        self.assertIn("[t2] 小漓: 在呀　（笑）", md)  # 换行→全角空格不断行
        self.assertIn("## 重要记忆", md)
        self.assertIn("- 喜欢蓝色", md)
        self.assertIn("颜色、蓝色：喜欢蓝色系", md)
        self.assertIn("## 深层存档", md)
        self.assertIn("[t0] 用户: 旧话", md)

    def test_render_empty_data(self):
        md = render_export_markdown({"chat": "x", "recent": [],
                                     "important": [], "index": [],
                                     "deep": [], "deep_count": 0})
        self.assertIn("近期窗口：0 条", md)
        self.assertIn("深层存档：0 条", md)


if __name__ == "__main__":
    unittest.main()
