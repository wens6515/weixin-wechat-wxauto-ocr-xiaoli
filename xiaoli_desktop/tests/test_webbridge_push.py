# -*- coding: utf-8 -*-
"""桥接 push 的 GUI 就绪闸：webview.start 之前的 evaluate_js 会阻塞，
启动通知/首轮 tick 必须先入队，GUI 起来后补发。"""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app.webbridge import BridgeApi


class _FakeWin:
    def __init__(self):
        self.calls = []

    def evaluate_js(self, js):
        self.calls.append(js)


class TestPushGate(unittest.TestCase):
    def _bridge(self):
        # BridgeApi.__init__ 只保存 ctx，不在这里读配置
        return BridgeApi(SimpleNamespace(cfg={}))

    def test_push_before_gui_is_queued(self):
        bridge = self._bridge()
        win = _FakeWin()
        bridge.bind_window(win)
        bridge.push("remote_status", {"ok": True})
        self.assertEqual(win.calls, [], "GUI 未就绪时不得 evaluate_js")
        bridge.on_gui_ready()
        self.assertEqual(len(win.calls), 1)
        self.assertIn("remote_status", win.calls[0])

    def test_push_after_gui_is_immediate(self):
        bridge = self._bridge()
        win = _FakeWin()
        bridge.bind_window(win)
        bridge.on_gui_ready()
        bridge.push("tick", {"state": "running"})
        self.assertEqual(len(win.calls), 1)
        self.assertIn("tick", win.calls[0])

    def test_prerendered_queue_capped(self):
        bridge = self._bridge()
        win = _FakeWin()
        bridge.bind_window(win)
        for i in range(250):
            bridge.push("tick", {"i": i})
        self.assertEqual(win.calls, [])
        bridge.on_gui_ready()
        self.assertEqual(len(win.calls), 200)


if __name__ == "__main__":
    unittest.main()
