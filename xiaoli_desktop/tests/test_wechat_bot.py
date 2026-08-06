# -*- coding: utf-8 -*-
"""wechat_bot 基础能力测试：群聊判定集中点、去重集合限界、微信连接重试可停止、
模型端点拼接、图片发送前压缩。"""
import os
import sys
import threading
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_bot import WeChatBot, is_group_chat, models_endpoint


class TestIsGroupChat(unittest.TestCase):
    def test_group_by_name(self):
        self.assertTrue(is_group_chat("产品讨论群"))
        self.assertTrue(is_group_chat("XX集团内部"))

    def test_private_chat(self):
        self.assertFalse(is_group_chat("小明"))
        self.assertFalse(is_group_chat("Alice"))

    def test_edge_inputs(self):
        self.assertFalse(is_group_chat(""))
        self.assertFalse(is_group_chat(None))


class TestRememberRecent(unittest.TestCase):
    def _make(self):
        bot = WeChatBot.__new__(WeChatBot)
        bot.recent_msg_ids = set()
        bot._RECENT_MAX = 10
        return bot

    def test_dedup_no_growth(self):
        bot = self._make()
        bot._remember_recent("x")
        bot._remember_recent("x")
        self.assertEqual(len(bot.recent_msg_ids), 1)
        self.assertEqual(len(bot._recent_order), 1)

    def test_caps_drops_oldest(self):
        """RED 复现：recent_msg_ids 只增不减会无限膨胀。
        超限（_RECENT_MAX=10）后每次丢弃最旧一半，保留最近消息。"""
        bot = self._make()
        for i in range(20):
            bot._remember_recent(f"k{i}")
        self.assertEqual(len(bot.recent_msg_ids), 10, "集合必须被限界")
        self.assertIn("k19", bot.recent_msg_ids, "最新消息必须保留")
        self.assertNotIn("k0", bot.recent_msg_ids, "最旧消息应被丢弃")
        self.assertNotIn("k9", bot.recent_msg_ids)

    def test_within_cap_kept_all(self):
        bot = self._make()
        for i in range(8):
            bot._remember_recent(f"k{i}")
        self.assertEqual(len(bot.recent_msg_ids), 8)


class TestConnectWx(unittest.TestCase):
    def _make(self, stop_event=None, max_retries=None, interval=0):
        bot = WeChatBot.__new__(WeChatBot)
        bot._stop_event = stop_event
        bot._max_connect_retries = max_retries
        bot._connect_retry_interval = interval
        return bot

    def test_stop_event_aborts(self):
        """RED 复现：微信未开时 _connect_wx 无限重试，GUI 无法取消初始化。
        引擎 stop_event 已设置 → 立即抛异常（可被 _do_initialize 捕获 → error）。"""
        evt = threading.Event()
        evt.set()
        bot = self._make(stop_event=evt)
        with self.assertRaises(RuntimeError):
            bot._connect_wx()

    def test_retry_limit_exhausted(self):
        """重试达上限（GUI 场景）→ 抛异常，不再无限循环。"""
        import wechat_bot as wb
        orig = wb.WeChat
        wb.WeChat = lambda **k: (_ for _ in ()).throw(RuntimeError("no wechat"))
        try:
            bot = self._make(max_retries=1, interval=0)
            with self.assertRaises(RuntimeError):
                bot._connect_wx()
        finally:
            wb.WeChat = orig

    def test_no_limit_keeps_retrying(self):
        """CLI 模式（不传上限）连接失败后继续重试——观察若干次调用确认不退出。"""
        import time
        import wechat_bot as wb
        orig = wb.WeChat
        calls = {"n": 0}
        stop = threading.Event()

        def fake_connect(**k):
            calls["n"] += 1
            raise RuntimeError("no wechat")

        wb.WeChat = fake_connect
        try:
            bot = self._make(max_retries=None, interval=0)
            bot._stop_event = stop  # 测试用停止信号（业务上 CLI 模式不设）
            t = threading.Thread(target=bot._connect_wx, daemon=True)
            t.start()
            time.sleep(0.3)  # 给几次重试机会
            self.assertGreaterEqual(calls["n"], 2, "无限重试模式应持续重试")
            self.assertTrue(t.is_alive(), "重试循环应持续运行（未达上限不退出）")
            stop.set()  # 停止信号 → 线程退出（防测试线程泄漏）
            t.join(2)
            self.assertFalse(t.is_alive(), "停止信号应能终止重试循环")
        finally:
            wb.WeChat = orig


class TestModelsEndpoint(unittest.TestCase):
    def test_standard_url(self):
        self.assertEqual(
            models_endpoint("https://api.deepseek.com/v1/chat/completions"),
            "https://api.deepseek.com/v1/models")

    def test_no_chat_completions_unchanged(self):
        # 自定义端点不含该子串 → 原样返回（保持旧行为）
        self.assertEqual(models_endpoint("https://x.example/v1/chat"),
                         "https://x.example/v1/chat")

    def test_only_last_occurrence_replaced(self):
        # rsplit 只替换最后一处（str.replace 会替换所有出现处——拼接错误）
        self.assertEqual(
            models_endpoint("https://x/chat/completions/chat/completions"),
            "https://x/chat/completions/models")

    def test_empty(self):
        self.assertEqual(models_endpoint(""), "")
        self.assertIsNone(models_endpoint(None))


class TestImageCompress(unittest.TestCase):
    def _make(self):
        return WeChatBot.__new__(WeChatBot)

    def test_large_screenshot_scaled_and_jpeg(self):
        """RED 复现：4K 屏幕截图 base64 直发撑爆视觉 API 体积上限。
        压缩后最长边 ≤ MAX_IMAGE_EDGE 且为 JPEG。"""
        from PIL import Image
        bot = self._make()
        img = Image.new("RGB", (4000, 3000), "red")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "shot.jpg")
            size = bot._save_screenshot_compressed(img, p)
            self.assertGreater(size, 0, "压缩产物必须落盘")
            with Image.open(p) as out:
                self.assertLessEqual(max(out.size), bot.MAX_IMAGE_EDGE,
                                     "最长边必须缩放到上限内")
                self.assertEqual(out.format, "JPEG")

    def test_small_image_kept_size(self):
        from PIL import Image
        bot = self._make()
        img = Image.new("RGB", (800, 600), "blue")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "small.jpg")
            bot._save_screenshot_compressed(img, p)
            with Image.open(p) as out:
                self.assertEqual(out.size, (800, 600), "小图不应被放大")

    def test_pil_failure_falls_back_png(self):
        """PIL 不可用时退回原样保存（图片处理链路不中断）。"""
        bot = self._make()

        class FakeImage:
            """无 convert/resize 的伪截图对象（模拟 PIL 缺失/异常场景）。"""

            def convert(self, *a):
                raise ImportError("no PIL")

            def save(self, path, **kw):
                with open(path, "wb") as f:
                    f.write(b"png-fallback")

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "shot.jpg")
            size = bot._save_screenshot_compressed(FakeImage(), p)
            self.assertGreater(size, 0, "退回保存也必须落盘")
            with open(p, "rb") as f:
                self.assertEqual(f.read(), b"png-fallback")


if __name__ == "__main__":
    unittest.main(verbosity=2)