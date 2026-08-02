# -*- coding: utf-8 -*-
"""UI 冒烟测试（QT_QPA_PLATFORM=offscreen）：构造主窗口与六页、触发刷新、不崩溃。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

# offscreen 平台下模态对话框没有事件循环驱动 exec() → 永久阻塞。
# 冒烟测试只验证构造/刷新/保存逻辑，patch 模态为 no-op。
QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.critical = staticmethod(lambda *a, **k: None)

from xiaoli_app.ui import AppContext
from xiaoli_app.ui.main_window import MainWindow

_app = QApplication.instance() or QApplication(sys.argv)


def make_card(**over):
    base = {
        "id": "xiaoli", "name": "小漓", "emoji": "🐟",
        "system_prompt": "你叫小漓，可爱。",
        "nickname": "小漓",
        "chat_provider": "deepseek", "chat_model": "deepseek-chat",
        "vision_provider": "deepseek", "vision_model": "deepseek-vl",
        "classify_provider": "deepseek", "classify_model": "deepseek-chat",
        "temperature": 0.7, "top_p": 0.9,
        "vision_temp": 0.7, "vision_max_tokens": 10000,
        "max_history": 1000,
    }
    base.update(over)
    return base


class TestUiSmoke(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ui_test_")
        self.cards_dir = os.path.join(self.tmp, "cards")
        os.makedirs(self.cards_dir)
        from xiaoli_app import card_store
        card_store.save_card(self.cards_dir, make_card())
        # 记忆文件
        with open(os.path.join(self.tmp, "memory.json"), "w", encoding="utf-8") as f:
            json.dump({"测试群": [{"role": "user", "content": "hi", "time": "2026-01-01 00:00:00"}]}, f, ensure_ascii=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_ctx(self):
        ctx = AppContext()
        ctx.cfg = {
            "providers": [{
                "id": "deepseek", "name": "DeepSeek",
                "base_url": "https://api.deepseek.com/v1/chat/completions",
                "api_key": "sk-test", "models": ["deepseek-chat"],
            }],
            "active_card_id": "xiaoli",
            "tasks_dir": os.path.join(self.tmp, "tasks"),
            "memory_file": os.path.join(self.tmp, "memory.json"),
            "cooldown": 3,
        }
        ctx.cards_dir = self.cards_dir
        return ctx

    def test_main_window_constructs_and_ticks(self):
        ctx = self._make_ctx()
        win = MainWindow(ctx)
        self.assertEqual(len(win.pages), 6)
        # 触发定时刷新逻辑（bus 为空也应安全）
        win._on_tick()
        # 各页 refresh 不抛异常
        for name, page in win.pages.items():
            refresh = getattr(page, "refresh", None)
            if refresh is not None:
                refresh()
        win.close()

    def test_status_page_reflects_engine(self):
        from xiaoli_app.engine import EngineThread
        ctx = self._make_ctx()
        win = MainWindow(ctx)
        page = win.pages["状态"]
        page.refresh()
        self.assertIn("引擎未启动", page.lbl_state.text())
        self.assertFalse(page.btn_pause.isEnabled())
        # 未启动的引擎 → 启动中
        class FakeBot:
            paused = False
            wx = object()

            def run(self, stop_event=None, poll_interval=2.0):
                while not (stop_event and stop_event.is_set()):
                    import time
                    time.sleep(0.01)

        fake_eng = EngineThread(lambda: FakeBot(), poll_interval=0.01)
        ctx.engine = fake_eng
        ctx.bus = None
        page.refresh()
        self.assertIn("启动中", page.lbl_state.text())
        # 启动后 → 运行中 + 微信已连接
        fake_eng.start()
        import time
        time.sleep(0.15)
        page.refresh()
        self.assertIn("运行中", page.lbl_state.text())
        self.assertIn("已连接", page.lbl_wx.text())
        fake_eng.stop()
        win.close()

    def test_cards_page_load_and_collect(self):
        ctx = self._make_ctx()
        win = MainWindow(ctx)
        page = win.pages["角色卡"]
        page.refresh()
        # 列表里应有预置卡
        self.assertEqual(page.list.count(), 1)
        # 选中 → 表单加载
        page.list.setCurrentRow(0)
        self.assertEqual(page.ed_name.text(), "小漓")
        # 修改名称并保存
        page.ed_name.setText("小漓二号")
        page._save_card()
        from xiaoli_app import card_store
        card = card_store.get_card(self.cards_dir, "xiaoli")
        self.assertEqual(card["name"], "小漓二号")
        win.close()

    def test_models_page_collect_providers(self):
        ctx = self._make_ctx()
        win = MainWindow(ctx)
        page = win.pages["模型"]
        page.refresh()
        rows = page.table.rowCount()
        self.assertGreaterEqual(rows, 1)
        provs = page._collect_providers()
        self.assertEqual(provs[0]["id"], "deepseek")
        win.close()

    def test_tasks_page_scans_dir(self):
        ctx = self._make_ctx()
        os.makedirs(ctx.cfg["tasks_dir"], exist_ok=True)
        win = MainWindow(ctx)
        page = win.pages["任务"]
        page.refresh()  # 空目录不崩溃
        win.close()

    def test_gui_entry_importable(self):
        # 入口模块可导入（不执行 main，避免启动引擎连微信）
        import xiaoli_gui  # noqa: F401
        self.assertTrue(callable(xiaoli_gui.main))


if __name__ == "__main__":
    unittest.main(verbosity=2)
