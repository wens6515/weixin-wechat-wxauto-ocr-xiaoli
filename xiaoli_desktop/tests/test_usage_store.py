# -*- coding: utf-8 -*-
"""用量统计存储单测：落盘/坏行容错/按天与按模型聚合/时间窗过滤。"""
import json
import os
import tempfile
import time
import unittest

from xiaoli_app.usage_store import UsageStore


class TestUsageStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "usage.jsonl")
        self.store = UsageStore(self.path)

    def test_record_appends_and_loads(self):
        self.store.record(kind="chat", model="m1", prompt_tokens=10,
                          completion_tokens=5, ok=True, status=200, latency_ms=123.4)
        recs = self.store._load()
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec["kind"], "chat")
        self.assertEqual(rec["model"], "m1")
        self.assertEqual(rec["prompt_tokens"], 10)
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["latency_ms"], 123)
        self.assertIsInstance(rec["ts"], float)

    def test_clear_removes_file_and_allows_rerecord(self):
        self.store.record(kind="chat", model="m1", ok=True)
        self.assertTrue(self.store.clear())
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(self.store._load(), [])
        # 清空后继续记录（record 自动重建文件）
        self.store.record(kind="chat", model="m2", ok=True)
        self.assertEqual(len(self.store._load()), 1)

    def test_clear_missing_file_returns_false(self):
        self.assertFalse(self.store.clear())

    def test_missing_file_summary_empty(self):
        s = self.store.summary(days=7)
        self.assertEqual(s["total"]["calls"], 0)
        self.assertEqual(s["by_day"], {})

    def test_summary_groups_by_day_and_model(self):
        # 锚定正午构造时间戳：直接用 time.time() 时，午夜前后运行会让
        # now-3600 跨到前一天，by_day 断言随机翻车
        now = time.time()
        local_noon = time.mktime(time.localtime(now)[:3] + (12, 0, 0, 0, 0, -1))
        now = local_noon
        rows = [
            {"ts": now, "kind": "chat", "model": "m1", "prompt_tokens": 100,
             "completion_tokens": 20, "ok": True, "status": 200, "latency_ms": 50},
            {"ts": now - 3600, "kind": "vision", "model": "m1",
             "prompt_tokens": 200, "completion_tokens": 10, "ok": True,
             "status": 200, "latency_ms": 60},
            {"ts": now - 86400, "kind": "chat", "model": "m2",
             "prompt_tokens": None, "completion_tokens": None, "ok": False,
             "status": 500, "latency_ms": None},
            {"ts": now - 30 * 86400, "kind": "chat", "model": "m1",
             "prompt_tokens": 999, "completion_tokens": 999, "ok": True,
             "status": 200, "latency_ms": 1},
        ]
        with open(self.path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.write("这不是JSON\n")
        s = self.store.summary(days=7)
        self.assertEqual(s["total"]["calls"], 3)      # 30 天前那行被时间窗滤掉
        self.assertEqual(s["total"]["ok"], 2)
        self.assertEqual(s["total"]["fail"], 1)
        self.assertEqual(s["total"]["prompt"], 300)   # None 按 0 计
        today_key = UsageStore._day_key(now)
        yesterday_key = UsageStore._day_key(now - 86400)
        self.assertEqual(s["by_day"][today_key]["calls"], 2)
        self.assertEqual(s["by_day"][yesterday_key]["calls"], 1)
        self.assertEqual(s["by_day"][yesterday_key]["prompt"], 0)
        self.assertEqual(list(s["by_model"].keys()), ["m1", "m2"])  # 按调用数降序
        self.assertEqual(s["by_model"]["m1"]["completion"], 30)
        self.assertEqual(s["today"]["calls"], 2)

    def test_summary_today_empty_bucket(self):
        s = self.store.summary(days=7)
        self.assertEqual(s["today"], {"calls": 0, "ok": 0, "fail": 0,
                                      "prompt": 0, "completion": 0,
                                      "latency_sum": 0.0, "cache_hit": 0,
                                      "cache_miss": 0, "reasoning": 0,
                                      "total": 0})

    def test_record_cache_fields_and_hit_ratio(self):
        """缓存字段透传 + 命中率聚合；无缓存数据显示「无数据」（None）而非 0%。"""
        from xiaoli_app.usage_store import hit_ratio
        self.store.record(kind="chat", model="m1", prompt_tokens=1000,
                          completion_tokens=100, ok=True,
                          cache_hit=800, cache_miss=200, reasoning=40,
                          total_tokens=1140, src="api")
        self.store.record(kind="chat", model="m1", prompt_tokens=500,
                          completion_tokens=50, ok=True,
                          cache_hit=250, cache_miss=250, src="api")
        s = self.store.summary(days=7)
        b = s["total"]
        self.assertEqual(b["cache_hit"], 1050)
        self.assertEqual(b["cache_miss"], 450)
        self.assertEqual(b["reasoning"], 40)
        self.assertEqual(b["total"], 1140)
        self.assertAlmostEqual(hit_ratio(b), 1050 / 1500)
        self.assertAlmostEqual(s["by_model"]["m1"]["cache_hit"], 1050)
        # 旧记录（无缓存字段）不参与命中率——None 而非 0%
        self.store.record(kind="chat", model="old", ok=True)
        s2 = self.store.summary(days=7)
        self.assertIsNone(hit_ratio(s2["by_model"]["old"]))
        self.assertIsNone(hit_ratio({"calls": 1, "cache_hit": 0, "cache_miss": 0}))

    def test_reply_records_separate_from_calls(self):
        """kind=reply 是端到端回复耗时记录：不计入调用/token 各桶，单独聚
        合进 reply_by_model 供「平均回复耗时」列。"""
        self.store.record(kind="chat", model="m1", prompt_tokens=100,
                          completion_tokens=10, ok=True, latency_ms=500)
        self.store.record(kind="reply", model="m1", ok=True, latency_ms=15300)
        self.store.record(kind="reply", model="m1", ok=True, latency_ms=2700)
        s = self.store.summary(days=7)
        self.assertEqual(s["total"]["calls"], 1)          # reply 不算调用
        self.assertEqual(s["total"]["prompt"], 100)
        self.assertEqual(s["by_day"][UsageStore._day_key(time.time())]["calls"], 1)
        rep = s["reply_by_model"]["m1"]
        self.assertEqual(rep["count"], 2)
        self.assertAlmostEqual(rep["latency_sum"], 18000.0)  # 平均 9s

    def test_default_path_under_data_dir(self):
        from xiaoli_app.usage_store import default_usage_path
        from xiaoli_app.config_store import default_data_dir
        self.assertEqual(os.path.dirname(default_usage_path()),
                         default_data_dir())


if __name__ == "__main__":
    unittest.main()
