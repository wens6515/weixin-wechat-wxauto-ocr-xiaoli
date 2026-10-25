# -*- coding: utf-8 -*-
"""状态层：persona_state.json 存取 + 定量输出解析 + 注入正文组装。"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app import persona_state
from xiaoli_app.persona_state import (PersonaStateStore, build_state_block,
                                      parse_state_json)


class TestParseStateJson(unittest.TestCase):
    def test_ok(self):
        mood, energy = parse_state_json('{"mood": "开心", "energy": 80}')
        self.assertEqual(mood, "开心")
        self.assertEqual(energy, 80)

    def test_code_fence(self):
        mood, energy = parse_state_json('```json\n{"mood": "有点累", "energy": 30}\n```')
        self.assertEqual(mood, "有点累")
        self.assertEqual(energy, 30)

    def test_bad_json_keeps_none(self):
        self.assertEqual(parse_state_json("今天很开心"), (None, None))
        self.assertEqual(parse_state_json(""), (None, None))

    def test_energy_out_of_range_none(self):
        mood, energy = parse_state_json('{"mood": "平静", "energy": 150}')
        self.assertEqual(mood, "平静")
        self.assertIsNone(energy)


class TestStore(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            st = PersonaStateStore(os.path.join(d, "persona_state.json"))
            self.assertIsNone(st.get("林小满"))
            st.set("林小满", "开心", 80)
            got = st.get("林小满")
            self.assertEqual(got["mood"], "开心")
            self.assertEqual(got["energy"], 80)
            self.assertGreater(got["ts"], 0)

    def test_quote_variant_same_key(self):
        """OCR 引号变体（「“摸鱼”集团」vs「摸鱼”集团」）落同一状态。"""
        with tempfile.TemporaryDirectory() as d:
            st = PersonaStateStore(os.path.join(d, "persona_state.json"))
            st.set("“摸鱼”集团", "热闹", 60)
            self.assertEqual(st.get("摸鱼”集团")["mood"], "热闹")


class _Bot:
    """build_state_block 的最小桩：memory_file + recent 历史 + store 路径。"""

    def __init__(self, tmp, hist, now=None):
        import threading
        self.memory_file = os.path.join(tmp, "memory.json")
        self._model_lock = threading.RLock()
        self._hist = hist
        if now is not None:
            self._now = now

    def _get_history(self, chat):
        return self._hist


class TestBuildStateBlock(unittest.TestCase):
    TODAY = time.strftime("%Y-%m-%d")

    def test_local_parts_always_present(self):
        with tempfile.TemporaryDirectory() as d:
            bot = _Bot(d, [])
            text = build_state_block(bot, "林小满")
            self.assertIn("背景感受", text)
            self.assertIn("今天还没怎么聊", text)

    def test_heat_counted_from_history(self):
        hist = [{"role": "user", "content": "x", "time": f"{self.TODAY} 10:00:00"}
                for _ in range(8)]
        with tempfile.TemporaryDirectory() as d:
            bot = _Bot(d, hist)
            self.assertIn("聊了 8 条", build_state_block(bot, "林小满"))

    def test_fresh_mood_injected(self):
        with tempfile.TemporaryDirectory() as d:
            bot = _Bot(d, [])
            persona_state._store(bot).set("林小满", "聊得起劲", 70)
            text = build_state_block(bot, "林小满")
            self.assertIn("聊得起劲", text)

    def test_stale_mood_omitted(self):
        with tempfile.TemporaryDirectory() as d:
            bot = _Bot(d, [])
            # 直接写过期状态（新鲜度窗口外）
            store = persona_state._store(bot)
            with store.lock:
                import json
                data = {"林小满": {"mood": "旧情绪", "energy": 10,
                                  "ts": time.time() - 8 * 3600}}
                with open(store.path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
            text = build_state_block(bot, "林小满")
            self.assertNotIn("旧情绪", text)
            self.assertNotIn("精力有点跟不上了", text)

    def test_low_energy_noted(self):
        with tempfile.TemporaryDirectory() as d:
            bot = _Bot(d, [])
            persona_state._store(bot).set("林小满", "平静", 30)
            self.assertIn("精力有点跟不上了",
                          build_state_block(bot, "林小满"))


if __name__ == "__main__":
    unittest.main()


class TestPeriodAndSnapshot(unittest.TestCase):
    def test_period_label_boundaries(self):
        f = persona_state.period_label
        import calendar
        # 固定时刻：今天 02:00 / 08:00 / 12:30 / 16:00 / 20:00 / 23:30
        lt = time.localtime()
        def at(hour, minute=0):
            return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                hour, minute, 0, 0, 0, -1))
        self.assertEqual(f(at(2)), "深夜")
        self.assertEqual(f(at(8)), "早上")
        self.assertEqual(f(at(12, 30)), "中午")
        self.assertEqual(f(at(16)), "下午")
        self.assertEqual(f(at(20)), "晚上")
        self.assertEqual(f(at(23, 30)), "深夜")

    def test_snapshot_returns_copy(self):
        with tempfile.TemporaryDirectory() as d:
            st = PersonaStateStore(os.path.join(d, "persona_state.json"))
            st.set("林小满", "开心", 80)
            snap = st.snapshot()
            self.assertEqual(snap["林小满"]["mood"], "开心")
            snap["林小满"]["mood"] = "改不动原件"
            self.assertEqual(st.get("林小满")["mood"], "开心")

    def test_snapshot_skips_bad_entries(self):
        with tempfile.TemporaryDirectory() as d:
            st = PersonaStateStore(os.path.join(d, "persona_state.json"))
            with st.lock:
                import json
                with open(st.path, "w", encoding="utf-8") as f:
                    json.dump({"a": {"mood": "x", "energy": 1, "ts": 1},
                               "b": "脏数据"}, f)
            snap = st.snapshot()
            self.assertEqual(set(snap), {"a"})
