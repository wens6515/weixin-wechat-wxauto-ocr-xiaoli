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
    def test_ascii_chars_count_weighted(self):
        # 英文按 ~0.3 token/字符加权（实际 ~4 字符/token，保守取 0.3）
        self.assertEqual(estimate_tokens("hello"), 2)  # int(5*0.3)+1

    def test_cjk_chars_count_weighted(self):
        # 中文按 ~0.8 token/字加权（实际 ~0.6，保守上界）
        self.assertEqual(estimate_tokens("你好世界"), 4)  # int(4*0.8)+1

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
        # 消息加长到 2000 字符（加权后 ~600 token/条），确保超出预算触发裁剪
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "a" * 2000},   # 旧
            {"role": "assistant", "content": "b" * 2000},
            {"role": "user", "content": "c" * 2000},   # 新
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

    def test_multimodal_block_list_preserved_within_budget(self):
        """多模态块列表（vision user 消息：text + image_url 块）在预算内
        必须原样保留结构——content 仍是 list[dict]，image_url 的 base64
        完整，绝不被 str() 转成 repr 字符串。
        RED：修复前 fit_messages_in_budget 对 content 做 str(...)，list 被
        repr 化 → vision payload 结构破坏（图片丢失）。"""
        blocks = [
            {"type": "text", "text": "描述"},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,iVBORy1mYWtl"}},
        ]
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": blocks}]
        out = fit_messages_in_budget(msgs, budget=100000)
        self.assertIsInstance(out[-1]["content"], list,
                              "多模态块列表必须保留 list 结构")
        self.assertEqual(out[-1]["content"], blocks,
                         "预算内多模态块原样保留（不被 repr 化）")

    def test_multimodal_block_list_truncates_text_keeps_image(self):
        """多模态块列表超预算 → 只截断 text 块文本，image_url 块保留原样
        （截断 base64 会损坏图片）；content 仍为 list[dict]。
        RED：修复前整个 list 被 repr 化并按字符截断，base64 被截断
        → 模型拿到损坏图片。"""
        blocks = [
            {"type": "text", "text": "x" * 100000},
            {"type": "image_url",
             "image_url": {"url": "data:image/png;base64,iVBORy1mYWtl"}},
        ]
        msgs = [{"role": "system", "content": "sys"},
                {"role": "user", "content": blocks}]
        out = fit_messages_in_budget(msgs, budget=1000)
        c = out[-1]["content"]
        self.assertIsInstance(c, list, "截断后 content 仍为块列表")
        text_block = next(b for b in c if b["type"] == "text")
        self.assertLess(len(text_block["text"]), 100000,
                        "超预算的 text 块必须被截断")
        img_block = next(b for b in c if b["type"] == "image_url")
        self.assertEqual(img_block["image_url"]["url"],
                         "data:image/png;base64,iVBORy1mYWtl",
                         "image_url 块必须原样保留（base64 不可截断）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
