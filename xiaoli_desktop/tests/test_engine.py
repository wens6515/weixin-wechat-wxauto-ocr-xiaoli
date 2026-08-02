# -*- coding: utf-8 -*-
"""引擎线程与事件总线测试：启动/停止/暂停/恢复/事件流（不连微信）"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app.engine import EngineBus, EngineThread, BUS_STATUS, BUS_ERROR


class FakeBot:
    """最小鸭子类型 bot：run(stop_event, poll_interval) 循环 + paused + apply_role"""

    def __init__(self):
        self.paused = False
        self.calls = 0
        self.role_applied = 0

    def run(self, stop_event=None, poll_interval=2.0):
        while not (stop_event is not None and stop_event.is_set()):
            self.calls += 1
            time.sleep(0.005)

    def apply_role(self, card, providers=None):
        self.role_applied += 1


class TestEngineBus(unittest.TestCase):
    def test_emit_drain(self):
        bus = EngineBus()
        bus.emit("a", {"x": 1})
        bus.emit("b")
        events = bus.drain()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0], ("a", {"x": 1}))
        self.assertEqual(events[1], ("b", {}))
        # drain 后清空
        self.assertEqual(bus.drain(), [])

    def test_drain_limit(self):
        bus = EngineBus()
        for i in range(300):
            bus.emit("n", {"i": i})
        events = bus.drain(limit=100)
        self.assertEqual(len(events), 100)


class TestEngineThread(unittest.TestCase):
    def test_start_runs_loop(self):
        bus = EngineBus()
        eng = EngineThread(lambda: FakeBot(), bus=bus, poll_interval=0.01)
        eng.start()
        time.sleep(0.2)
        self.assertIsNotNone(eng.bot)
        n = eng.bot.calls
        time.sleep(0.1)
        self.assertGreater(eng.bot.calls, n, "主循环应在持续运行")
        self.assertTrue(eng.stop())
        self.assertFalse(eng.is_alive())

    def test_stop_returns_quickly(self):
        eng = EngineThread(lambda: FakeBot(), poll_interval=0.01)
        eng.start()
        time.sleep(0.1)
        t0 = time.time()
        ok = eng.stop(timeout=3)
        self.assertTrue(ok)
        self.assertLess(time.time() - t0, 2)

    def test_pause_resume(self):
        eng = EngineThread(lambda: FakeBot(), poll_interval=0.01)
        eng.start()
        time.sleep(0.1)
        eng.pause()
        self.assertTrue(eng.bot.paused)
        eng.resume()
        self.assertFalse(eng.bot.paused)
        eng.stop()

    def test_apply_role_delegates(self):
        eng = EngineThread(lambda: FakeBot(), poll_interval=0.01)
        eng.start()
        time.sleep(0.1)
        self.assertTrue(eng.apply_role({"id": "x"}, []))
        self.assertEqual(eng.bot.role_applied, 1)
        eng.stop()

    def test_started_event_emitted(self):
        bus = EngineBus()
        eng = EngineThread(lambda: FakeBot(), bus=bus, poll_interval=0.01)
        eng.start()
        deadline = time.time() + 3
        states = []
        while time.time() < deadline:
            for kind, payload in bus.drain():
                if kind == BUS_STATUS:
                    states.append(payload.get("state"))
            if "started" in states:
                break
            time.sleep(0.02)
        self.assertIn("started", states)
        eng.stop()
        # 停止事件
        deadline = time.time() + 3
        stopped = False
        while time.time() < deadline:
            for kind, payload in bus.drain():
                if kind == BUS_STATUS and payload.get("state") == "stopped":
                    stopped = True
            if stopped:
                break
            time.sleep(0.02)
        self.assertTrue(stopped)

    def test_bot_factory_exception_emits_error(self):
        bus = EngineBus()

        def boom():
            raise RuntimeError("connect failed")

        eng = EngineThread(boom, bus=bus, poll_interval=0.01)
        eng.start()
        eng.join(3)
        self.assertFalse(eng.is_alive())
        errors = [p for k, p in bus.drain() if k == BUS_ERROR]
        self.assertTrue(errors, "工厂异常应发出 error 事件")
        self.assertIn("connect failed", errors[0]["message"])


class TestWeChatBotRunStop(unittest.TestCase):
    def test_run_respects_stop_event(self):
        """WeChatBot.run(stop_event) 应可被外部停止（不连微信，paused 时 process_new_messages 立即返回）"""
        from wechat_bot import WeChatBot
        bot = WeChatBot.__new__(WeChatBot)
        bot.paused = True  # process_new_messages 直接 return，不碰 wx
        evt = threading.Event()
        t = threading.Thread(target=bot.run, args=(evt,), kwargs={"poll_interval": 0.01})
        t.start()
        time.sleep(0.1)
        self.assertTrue(t.is_alive(), "循环应运行")
        evt.set()
        t.join(3)
        self.assertFalse(t.is_alive(), "stop_event 置位后循环应退出")

    def test_run_without_stop_event_keeps_legacy_behavior(self):
        """不带 stop_event 时行为与旧版一致（永续循环，poll_interval 生效）——只验证签名兼容"""
        import inspect
        from wechat_bot import WeChatBot
        sig = inspect.signature(WeChatBot.run)
        params = sig.parameters
        self.assertIn("stop_event", params)
        self.assertIn("poll_interval", params)
        self.assertEqual(params["poll_interval"].default, 2.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
