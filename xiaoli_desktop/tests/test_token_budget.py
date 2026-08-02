# -*- coding: utf-8 -*-
"""上下文 token 预算裁剪测试。

RED 复现：微信文件识别把整个 xlsx 转文本塞进 prompt（272 万 token），
DeepSeek 上限 104 万 → API 400 "maximum context length"。
修复：请求构造时按 token 预算裁剪（估 token = 字符数，保守），
超预算从最旧历史丢弃，单条超限消息截断。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_bot import fit_messages_in_budget, estimate_tokens


class TestEstimateTokens(unittest.TestCase):
    def test_ascii_chars_count_as_tokens(self):
        self.assertEqual(estimate_tokens("hello"), 5)

    def test_cjk_chars_count_roughly_one(self):
        # 中文按 1 字符 ≈ 1 token 保守估算
        self.assertEqual(estimate_tokens("你好世界"), 4)

    def test_empty(self):
        self.assertEqual(estimate_tokens(""), 0)


class TestFitMessagesInBudget(unittest.TestCase):
    def test_within_budget_unchanged(self):
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "你好"}]
        out = fit_messages_in_budget(msgs, budget=1000)
        self.assertEqual(out, msgs, "预算内原样返回")

    def test_drops_oldest_history_over_budget(self):
        # RED 复现：历史过长时从最旧开始丢，保留 system + 最近消息
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "a" * 500},   # 旧
            {"role": "assistant", "content": "b" * 500},
            {"role": "user", "content": "c" * 500},   # 新
        ]
        out = fit_messages_in_budget(msgs, budget=600)
        total = sum(estimate_tokens(m["content"]) for m in out)
        self.assertLessEqual(total, 600,
                             f"裁剪后必须在预算内，实际 {total}")
        self.assertEqual(out[0]["role"], "system", "system 必须保留")
        self.assertTrue(out[-1]["content"].startswith("c"),
                        "最新消息必须保留（可截断但不可丢弃）")
        self.assertNotIn("a", "".join(m["content"] for m in out[1:]),
                         "最旧历史消息应被丢弃")

    def test_single_huge_message_truncated(self):
        # 文件全文单条超限 → 截断到预算比例，而不是整体丢弃
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "x" * 100000}]
        out = fit_messages_in_budget(msgs, budget=1000)
        total = sum(estimate_tokens(m["content"]) for m in out)
        self.assertLessEqual(total, 1000)
        self.assertTrue(out[-1]["content"].endswith("…"),
                        "截断的消息应带省略号标记")

    def test_empty_messages_ok(self):
        self.assertEqual(fit_messages_in_budget([], budget=100), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
