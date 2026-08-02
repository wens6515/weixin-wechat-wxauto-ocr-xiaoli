# -*- coding: utf-8 -*-
"""引擎线程与事件总线测试：生命周期状态机 idle→initialized→running⇄paused→stopped（不连微信）"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app.engine import EngineBus, EngineThread, BUS_STATUS, BUS_ERROR


class FakeBot:
    """最小鸭子类型 bot：process_new_messages + paused + apply_role"""

    def __init__(self):
        self.paused = False
        self.calls = 0
        self.role_applied = 0

    def process_new_messages(self):
        self.calls += 1

    def apply_role(self, card, providers=None):
        self.role_applied += 1


def wait_state(eng, target, timeout=3.0):
    """轮询等待引擎状态到达目标。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if eng.state == target:
            return True
        time.sleep(0.02)
    return eng.state == target


class TestEngineBus(unittest.TestCase):
    def test_emit_drain(self):
        bus = EngineBus()
        bus.emit("a", {"x": 1})
        bus.emit("b")
        events = bus.drain()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0], ("a", {"x": 1}))
        self.assertEqual(events[1], ("b", {}))
        self.assertEqual(bus.drain(), [])

    def test_drain_limit(self):
        bus = EngineBus()
        for i in range(300):
            bus.emit("n", {"i": i})
        events = bus.drain(limit=100)
        self.assertEqual(len(events), 100)


class TestEngineLifecycle(unittest.TestCase):
    def _make(self, factory=None, bus=None):
        return EngineThread(factory or (lambda: FakeBot()), bus=bus, poll_interval=0.01)

    def test_initial_state_idle(self):
        eng = self._make()
        self.assertEqual(eng.state, "idle")
        self.assertIsNone(eng.bot)

    def test_initialize_creates_bot(self):
        bus = EngineBus()
        eng = self._make(bus=bus)
        eng.initialize()
        self.assertTrue(wait_state(eng, "initialized"))
        self.assertIsNotNone(eng.bot)
        self.assertEqual(eng.bot.calls, 0, "初始化不跑主循环")
        # 事件流包含 initialized
        states = [p.get("state") for k, p in bus.drain() if k == BUS_STATUS]
        self.assertIn("initialized", states)
        eng.stop()

    def test_initialize_failure_emits_error(self):
        bus = EngineBus()

        def boom():
            raise RuntimeError("connect failed")

        eng = self._make(factory=boom, bus=bus)
        eng.initialize()
        self.assertTrue(wait_state(eng, "error"))
        errors = [p for k, p in bus.drain() if k == BUS_ERROR]
        self.assertTrue(errors)
        self.assertIn("connect failed", errors[0]["message"])
        self.assertIn("初始化失败", eng.error)
        eng.stop()

    def test_double_initialize_noop(self):
        eng = self._make()
        eng.initialize()
        self.assertTrue(wait_state(eng, "initialized"))
        self.assertFalse(eng.initialize(), "重复初始化应拒绝")
        eng.stop()

    def test_start_bot_runs_loop(self):
        eng = self._make()
        eng.initialize()
        self.assertTrue(wait_state(eng, "initialized"))
        self.assertTrue(eng.start_bot())
        self.assertTrue(wait_state(eng, "running"))
        time.sleep(0.15)
        n = eng.bot.calls
        time.sleep(0.1)
        self.assertGreater(eng.bot.calls, n, "主循环应持续运行")
        eng.stop()

    def test_start_bot_before_initialize_rejected(self):
        eng = self._make()
        self.assertFalse(eng.start_bot(), "未初始化不能启动")
        eng.stop()

    def test_pause_resume(self):
        eng = self._make()
        eng.initialize()
        self.assertTrue(wait_state(eng, "initialized"))
        eng.start_bot()
        self.assertTrue(wait_state(eng, "running"))
        time.sleep(0.1)
        n = eng.bot.calls
        eng.pause()
        self.assertTrue(wait_state(eng, "paused"))
        time.sleep(0.15)
        self.assertLessEqual(eng.bot.calls, n + 2, "暂停后主循环应停止")
        eng.resume()
        self.assertTrue(wait_state(eng, "running"))
        time.sleep(0.1)
        self.assertGreater(eng.bot.calls, n, "恢复后主循环继续")
        eng.stop()

    def test_stop_terminates(self):
        eng = self._make()
        eng.initialize()
        self.assertTrue(wait_state(eng, "initialized"))
        eng.start_bot()
        self.assertTrue(wait_state(eng, "running"))
        t0 = time.time()
        self.assertTrue(eng.stop(timeout=3))
        self.assertLess(time.time() - t0, 2)
        self.assertFalse(eng.is_alive())

    def test_apply_role_delegates(self):
        eng = self._make()
        eng.initialize()
        self.assertTrue(wait_state(eng, "initialized"))
        self.assertTrue(eng.apply_role({"id": "x"}, []))
        self.assertEqual(eng.bot.role_applied, 1)
        eng.stop()


class TestWeChatBotRunStop(unittest.TestCase):
    def test_run_respects_stop_event(self):
        """WeChatBot.run(stop_event) 应可被外部停止（不连微信，paused 时 process_new_messages 立即返回）"""
        from wechat_bot import WeChatBot
        bot = WeChatBot.__new__(WeChatBot)
        bot.paused = True
        evt = threading.Event()
        t = threading.Thread(target=bot.run, args=(evt,), kwargs={"poll_interval": 0.01})
        t.start()
        time.sleep(0.1)
        self.assertTrue(t.is_alive())
        evt.set()
        t.join(3)
        self.assertFalse(t.is_alive())

    def test_run_without_stop_event_keeps_legacy_behavior(self):
        import inspect
        from wechat_bot import WeChatBot
        sig = inspect.signature(WeChatBot.run)
        params = sig.parameters
        self.assertIn("stop_event", params)
        self.assertIn("poll_interval", params)
        self.assertEqual(params["poll_interval"].default, 2.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
