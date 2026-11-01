# -*- coding: utf-8 -*-
"""A1 原子落盘测试：_atomic_write_json（tmp+replace + .bak）与损坏回退加载。"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app.config_store import save_config
from xiaoli_app.memory_store import (MemoryStore, _atomic_write_json,
                                     _load_json_with_bak)


class TestAtomicWrite(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "data.json")

    def test_basic_write_and_no_tmp_leftover(self):
        _atomic_write_json(self.path, {"a": 1})
        self.assertEqual(json.load(open(self.path, encoding="utf-8")), {"a": 1})
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_bak_holds_previous_content(self):
        _atomic_write_json(self.path, {"gen": 1})
        _atomic_write_json(self.path, {"gen": 2})
        self.assertEqual(json.load(open(self.path, encoding="utf-8")),
                         {"gen": 2})
        self.assertEqual(json.load(open(self.path + ".bak", encoding="utf-8")),
                         {"gen": 1})

    def test_indent_preserved(self):
        _atomic_write_json(self.path, {"a": 1}, indent=4)
        with open(self.path, encoding="utf-8") as f:
            self.assertIn('\n    "a"', f.read())


class TestLoadWithBak(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "data.json")

    def test_corrupt_main_falls_back_to_bak(self):
        with open(self.path + ".bak", "w", encoding="utf-8") as f:
            json.dump({"ok": True}, f)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{corrupt!!!")
        self.assertEqual(_load_json_with_bak(self.path), {"ok": True})

    def test_missing_everything_returns_none(self):
        self.assertIsNone(_load_json_with_bak(self.path))

    def test_good_main_beats_bak(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"src": "main"}, f)
        with open(self.path + ".bak", "w", encoding="utf-8") as f:
            json.dump({"src": "bak"}, f)
        self.assertEqual(_load_json_with_bak(self.path), {"src": "main"})


class TestMemoryStoreAtomic(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _store(self):
        return MemoryStore(memory_file=os.path.join(self.dir, "memory.json"))

    def test_save_writes_both_files_atomically(self):
        st = self._store()
        st.add("林小满", "user", "你好", deep_enabled=False)
        st.global_add("小漓是鲸鱼娘")
        st.save()
        self.assertEqual(
            json.load(open(st.memory_file, encoding="utf-8"))["林小满"]
            ["recent"][0]["content"], "你好")
        self.assertEqual(
            json.load(open(st._global_path, encoding="utf-8"))
            ["important"][0]["content"], "小漓是鲸鱼娘")
        self.assertFalse(os.path.exists(st.memory_file + ".tmp"))

    def test_second_save_creates_bak(self):
        st = self._store()
        st.global_add("第一条")
        st.save()
        st.global_add("第二条")
        st.save()
        bak = json.load(open(st._global_path + ".bak", encoding="utf-8"))
        self.assertEqual(bak["important"][0]["content"], "第一条")

    def test_load_recovers_from_bak_when_main_corrupt(self):
        st = self._store()
        st.global_add("要保住的事实")
        st.save()
        with open(st._global_path, "w", encoding="utf-8") as f:
            f.write("{broken")
        st2 = self._store()
        st2.load()
        self.assertEqual(st2._global["important"][0]["content"],
                         "要保住的事实")


class TestConfigAtomic(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "config.json")

    def test_save_config_roundtrip_and_bak(self):
        save_config({"ai_api_key": "sk-abc", "providers": []}, self.path)
        save_config({"ai_api_key": "sk-def", "providers": []}, self.path)
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        bak = json.load(open(self.path + ".bak", encoding="utf-8"))
        self.assertIn("ai_api_key", bak)


if __name__ == "__main__":
    unittest.main()
