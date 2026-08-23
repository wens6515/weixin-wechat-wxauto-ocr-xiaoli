# -*- coding: utf-8 -*-
"""引擎线程 clear_memory：清空运行中 bot 的记忆（内存 memory_db + 落盘）。

历史缺陷：GUI「清空全部记忆」按钮只写空文件、不动 bot 内存 memory_db，
bot 节流写盘（_schedule_save_memory/_flush_memory）把旧记忆覆盖回磁盘。
修复：按钮改走 engine.clear_memory() → bot.clear_history()（清内存 + 落盘），
bot 未就绪/清空失败时回退直接清文件。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app.engine import EngineThread


class TestEngineClearMemory(unittest.TestCase):
    def _make_engine(self, bot):
        eng = EngineThread(bot_factory=lambda **k: bot)
        eng.bot = bot
        return eng

    def test_clear_memory_calls_bot_clear_history(self):
        from unittest import mock
        bot = mock.MagicMock()
        eng = self._make_engine(bot)
        self.assertTrue(eng.clear_memory())
        bot.clear_history.assert_called_once_with()

    def test_clear_memory_returns_false_when_bot_none(self):
        eng = EngineThread(bot_factory=lambda **k: None)
        eng.bot = None
        self.assertFalse(eng.clear_memory())

    def test_clear_memory_returns_false_when_clear_history_raises(self):
        from unittest import mock
        bot = mock.MagicMock()
        bot.clear_history.side_effect = RuntimeError("boom")
        eng = self._make_engine(bot)
        self.assertFalse(eng.clear_memory())


if __name__ == "__main__":
    unittest.main(verbosity=2)
