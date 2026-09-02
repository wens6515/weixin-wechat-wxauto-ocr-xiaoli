# -*- coding: utf-8 -*-
"""本地价目估算单测：查价 / 分时段落档（DeepSeek 工作日高峰、硅基流动
凌晨闲时）/ 覆盖 / 估算公式 / 无价目不猜钱 / 记录汇总。"""
import time
import unittest

from xiaoli_app import pricing


def make_ts(hour, wday=None):
    """构造本地时区测试时刻：今天 hour:00，wday 给定时平移到该星期
    （0=周一）。仅用于分档判定，与具体日期无关。"""
    lt = time.localtime()
    base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 0, 0, 0, 0, -1))
    if wday is None:
        return base
    return base + ((wday - lt.tm_wday) % 7) * 86400


# 固定测试档位时刻
PEAK_DS = make_ts(10, wday=2)    # 周三 10:00 → DeepSeek 高峰
PEAK_SF = make_ts(10)            # 每天 10:00 → 硅基流动高峰
OFF_TS = make_ts(3)              # 每天 3:00 → 两家都空闲
WEEKEND = make_ts(10, wday=5)    # 周六 10:00 → DeepSeek 空闲


class TestPriceFor(unittest.TestCase):
    def test_builtin_lookup_strips_prefix_and_case(self):
        p = pricing.price_for("deepseek:DeepSeek-V4-Flash")
        self.assertIsNotNone(p)
        self.assertEqual(p["input"], 3.0)
        self.assertIn("off", p)

    def test_current_models_present(self):
        for m in ("deepseek-v4-pro", "deepseek-v4-flash-vision-exp",
                  "glm-5.3", "glm-5.3-flash", "kimi-k3",
                  "qwen3.8-max", "qwen3.8-flash",
                  "doubao-seed-evolving", "doubao-seed-2.0-mini",
                  "deepseek-ai/deepseek-v4-flash-0731",
                  "deepseek-ai/deepseek-v4-pro"):
            self.assertIsNotNone(pricing.price_for(m), m)

    def test_outdated_models_removed(self):
        """旧型号/表外模型 → 无价目（只计 token 不给估算，用户定案）。"""
        for m in ("deepseek-ai/DeepSeek-V3", "qwen-plus", "qwen-vl-plus",
                  "glm-5.2", "kimi-k2", "doubao-seed-1.6",
                  "totally-unknown-model"):
            self.assertIsNone(pricing.price_for(m), m)

    def test_overrides_partial(self):
        p = pricing.price_for("deepseek-v4-flash",
                              overrides={"deepseek-v4-flash": {"input": 9.9}})
        self.assertEqual(p["input"], 9.9)
        self.assertEqual(p["off"]["input"], 1.5)   # 未覆盖的子档保留内置
        self.assertEqual(p["output"], 9.0)
        p3 = pricing.price_for("deepseek-v4-flash",
                               overrides={"deepseek-v4-flash": {"input": "abc"}})
        self.assertEqual(p3["input"], 3.0)          # 非法覆盖值忽略

    def test_overrides_off_subtier_and_unknown_model(self):
        p = pricing.price_for("deepseek-v4-flash", overrides={
            "deepseek-v4-flash": {"off": {"input": 0.9}}})
        self.assertEqual(p["off"]["input"], 0.9)
        self.assertEqual(p["off"]["output"], 4.5)
        # 表外模型显式给了价目 → 可计（用户显式覆盖优先于「不猜钱」）
        p2 = pricing.price_for("custom-model",
                               overrides={"custom-model": {"input": 1.0,
                                                           "output": 2.0}})
        self.assertEqual(p2["output"], 2.0)


class TestEstimateCost(unittest.TestCase):
    def test_formula_peak_and_offpeak(self):
        # 高峰：miss 1M×3 + hit 1M×0.1 + out 1M×9
        self.assertAlmostEqual(pricing.estimate_cost(
            "deepseek-v4-flash", 2_000_000, 1_000_000,
            cache_hit_tokens=1_000_000, ts=PEAK_DS), 12.1)
        # 空闲：减半 1.5 + 0.05 + 4.5；周末全天空闲
        self.assertAlmostEqual(pricing.estimate_cost(
            "deepseek-v4-flash", 2_000_000, 1_000_000,
            cache_hit_tokens=1_000_000, ts=OFF_TS), 6.05)
        self.assertAlmostEqual(pricing.estimate_cost(
            "deepseek-v4-flash", 2_000_000, 1_000_000,
            cache_hit_tokens=1_000_000, ts=WEEKEND), 6.05)

    def test_siliconflow_tiers_and_uniform_pro(self):
        # SF flash：高峰（每日 0-2、8-24 点）3 + 0.3 + 9
        self.assertAlmostEqual(pricing.estimate_cost(
            "deepseek-ai/deepseek-v4-flash-0731", 2_000_000, 1_000_000,
            cache_hit_tokens=1_000_000, ts=PEAK_SF), 12.3)
        # 空闲（2-8 点）1.5 + 0.15 + 4.5
        self.assertAlmostEqual(pricing.estimate_cost(
            "deepseek-ai/deepseek-v4-flash-0731", 2_000_000, 1_000_000,
            cache_hit_tokens=1_000_000, ts=OFF_TS), 6.15)
        # SF pro 无分档，任意时刻同价（miss 1M×12 + out 1M×24）
        self.assertAlmostEqual(pricing.estimate_cost(
            "deepseek-ai/deepseek-v4-pro", 1_000_000, 1_000_000,
            cache_hit_tokens=0, ts=OFF_TS), 36.0)

    def test_cache_hit_fallback_to_input_price(self):
        """无公开缓存价的模型（doubao-seed-evolving）命中按全价保守计。"""
        self.assertAlmostEqual(pricing.estimate_cost(
            "doubao-seed-evolving", 1_000_000, 0,
            cache_hit_tokens=1_000_000, ts=PEAK_DS), 6.0)

    def test_various_models(self):
        # kimi-k3：miss 1M×20 + hit 1M×2 + out 1M×100
        self.assertAlmostEqual(pricing.estimate_cost(
            "kimi-k3", 2_000_000, 1_000_000,
            cache_hit_tokens=1_000_000, ts=PEAK_DS), 122.0)
        self.assertAlmostEqual(pricing.estimate_cost(
            "qwen3.8-max", 1_000_000, 0, cache_hit_tokens=0,
            ts=PEAK_DS), 12.0)
        # 命中数超过提示数时夹紧（脏数据不产生负费用）
        cost2 = pricing.estimate_cost("deepseek-v4-flash", 100, 0,
                                      cache_hit_tokens=999, ts=OFF_TS)
        self.assertGreaterEqual(cost2, 0)

    def test_unpriced_returns_none(self):
        self.assertIsNone(pricing.estimate_cost("deepseek-ai/DeepSeek-V3",
                                                1000, 100, ts=PEAK_DS))


class TestEstimateRecords(unittest.TestCase):
    def test_mixed_priced_and_unpriced(self):
        rows = [
            {"ts": OFF_TS, "model": "deepseek-v4-flash",
             "prompt_tokens": 1_000_000, "completion_tokens": 0,
             "cache_hit": 0},                                  # 空闲 1.5
            {"ts": OFF_TS, "model": "deepseek-v4-flash",
             "prompt_tokens": 1_000_000, "completion_tokens": 0,
             "cache_hit": 1_000_000},                          # 空闲全命中 0.05
            {"ts": OFF_TS, "model": "no-price-model",
             "prompt_tokens": 999_999, "completion_tokens": 1},
            {"ts": OFF_TS - 40 * 86400, "model": "deepseek-v4-flash",
             "prompt_tokens": 1_000_000, "completion_tokens": 0},  # 时间窗外
        ]
        out = pricing.estimate_records(rows, days=30)
        self.assertAlmostEqual(out["total"], 1.55)
        self.assertEqual(out["priced"], 2)
        self.assertEqual(out["unpriced"], 1)

    def test_all_unpriced_total_is_none(self):
        out = pricing.estimate_records(
            [{"ts": time.time(), "model": "no-price", "prompt_tokens": 10,
              "completion_tokens": 1}], days=30)
        self.assertIsNone(out["total"])
        self.assertIsNone(out["today"])
        self.assertEqual(out["unpriced"], 1)

    def test_empty_records(self):
        out = pricing.estimate_records([], days=30)
        self.assertIsNone(out["total"])
        self.assertEqual(out["by_day"], {})


if __name__ == "__main__":
    unittest.main()
