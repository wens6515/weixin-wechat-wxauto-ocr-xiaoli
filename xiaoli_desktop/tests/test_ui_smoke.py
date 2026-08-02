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

from PySide6.QtWidgets import QApplication, QMessageBox, QTableWidgetItem

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

    def test_pages_filled_on_construct(self):
        """RED 复现：MainWindow 构造后各页必须自动填充数据。

        用户实测：模型添加后保存成功（config.json 已落盘），但重启程序后
        模型页表格空白、设置页编辑框空白。根因：MainWindow 从不调用各页
        refresh()——模型页表格/设置页回填只在 refresh() 里发生，构造后
        从未执行，界面永远显示空数据（保存的内容都在，只是不显示）。
        """
        ctx = self._make_ctx()
        win = MainWindow(ctx)  # 构造即应填充，不手动调 refresh
        # 模型页表格应有数据（_make_ctx 有 deepseek provider）
        models = win.pages["模型"]
        self.assertGreaterEqual(models.table.rowCount(), 1,
                                "MainWindow 构造后模型页表格必须自动填充")
        # 设置页任务目录编辑框应回填
        settings = win.pages["设置"]
        self.assertEqual(settings.ed_tasks.text(), ctx.cfg.get("tasks_dir", ""),
                         "MainWindow 构造后设置页任务目录必须回填")
        win.close()

    def test_prompt_flow_switches_yolo_before_first_prompt(self):
        """RED 复现：初始化发送首轮提示词前必须先切 YOLO。

        用户指出：首轮提示词让天枢去读 tasks_dir\\README.md——读文件是工具
        调用，若会话仍是 Manual 模式，初始化那一刻就弹确认，无人值守断链。
        所以 YOLO 切换必须在初始化（发首轮提示词）时做，而非投递任务时。
        """
        from unittest import mock
        from xiaoli_app import setup as setup_mod
        from xiaoli_app.ui.pages import HomePage
        ctx = self._make_ctx()
        page = HomePage(ctx)
        page._prompt_running = False
        page._prompt_done = False
        calls = []

        def fake_trigger(title, command, hold=0.5, enter_times=1):
            calls.append(("trigger", command))
            return True

        def fake_resolve(cfg):
            return "npm", ""

        with mock.patch.object(setup_mod, "resolve_cli_window", side_effect=fake_resolve), \
             mock.patch.object(setup_mod, "_send_trigger_to_window", side_effect=fake_trigger), \
             mock.patch.object(setup_mod, "build_first_prompt", return_value="首轮提示词内容"):
            page._prompt_worker()
        cmds = [c[1] for c in calls]
        self.assertGreaterEqual(len(cmds), 2,
                                "初始化必须至少发送两次：YOLO 切换 + 首轮提示词")
        self.assertEqual(cmds[0], "/permission yolo confirm",
                         "首轮提示词之前必须先切 YOLO，否则读 README.md 时弹确认")
        self.assertEqual(cmds[1], "首轮提示词内容",
                         "YOLO 切换后发送首轮提示词")

    def test_home_page_button_states(self):
        """状态机主按钮：初始化 → 启动 bot → 暂停运行 → 继续运行（随引擎状态切换）"""
        import time
        from xiaoli_app.engine import EngineThread
        ctx = self._make_ctx()
        win = MainWindow(ctx)
        page = win.pages["首页"]
        page.tick()
        self.assertEqual(page.btn_main.text(), "初始化")
        self.assertEqual(page.lbl_state.text(), "尚未初始化")
        # 初始化（FakeBot 工厂，不连微信）
        class FakeBot:
            paused = False

            def process_new_messages(self):
                pass

        eng = EngineThread(lambda: FakeBot(), poll_interval=0.01)
        ctx.engine = eng
        ctx.bus = None
        eng.initialize()
        deadline = time.time() + 3
        while time.time() < deadline and eng.state != "initialized":
            time.sleep(0.02)
        self.assertEqual(eng.state, "initialized")
        page.tick()
        self.assertEqual(page.btn_main.text(), "启动 bot")
        # 启动 → 运行中
        eng.start_bot()
        time.sleep(0.05)
        page.tick()
        self.assertEqual(page.btn_main.text(), "暂停运行")
        # 暂停
        eng.pause()
        page.tick()
        self.assertEqual(page.btn_main.text(), "继续运行")
        # 恢复
        eng.resume()
        page.tick()
        self.assertEqual(page.btn_main.text(), "暂停运行")
        eng.stop()
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

    def test_save_model_config_persists_providers(self):
        """RED 复现：点「保存模型配置」后 providers 表必须落盘 config.json。

        用户实测：模型页添加模型 → 点保存模型配置 → 关掉程序再打开模型没了。
        根因：_save_model_config 只写角色卡（cards/<id>.json），不写 providers
        表——重启后 load_config_store 从 config.json 读回旧 providers，添加的
        模型/Provider 消失。
        """
        ctx = self._make_ctx()
        win = MainWindow(ctx)
        page = win.pages["模型"]
        page.refresh()
        # 添加一行 provider（模拟用户添加模型）
        page._add_row()
        r = page.table.rowCount() - 1
        page.table.setItem(r, 0, QTableWidgetItem("智谱"))
        page.table.setItem(r, 1, QTableWidgetItem("https://open.bigmodel.cn/api/paas/v4/chat/completions"))
        page.table.setItem(r, 2, QTableWidgetItem("sk-test"))
        page.table.setItem(r, 3, QTableWidgetItem("zhipu:glm-4.6"))
        page.table.setItem(r, 4, QTableWidgetItem("zhipu"))
        # 模拟用户点「保存模型配置」（只保存角色卡模型选择）
        page._save_model_config()
        # config.json 里必须包含新增的 zhipu provider
        with open(ctx.cfg_path, "r", encoding="utf-8") as f:
            disk = json.load(f)
        ids = [p.get("id") for p in disk.get("providers", [])]
        self.assertIn("zhipu", ids,
                      "保存模型配置后 providers 表必须落盘，否则重启模型丢失")
        win.close()

    def test_qss_sets_combobox_popup_color(self):
        """RED 复现：QComboBox 下拉弹层文字颜色必须显式设置。

        用户实测：浅色界面上下拉框文字是白色看不清。根因：QSS 只设置了
        QComboBox 控件本体 color，未设置 QComboBox QAbstractItemView——
        深色系统主题下下拉项继承 palette 白字，与浅色背景撞色。
        """
        import xiaoli_app.ui as ui_mod
        self.assertIn("QComboBox QAbstractItemView", ui_mod.APP_QSS,
                      "QSS 必须设置下拉弹层（QAbstractItemView）颜色")
        # 弹层文字色必须与界面文字一致（深灰 #374151），不能是白色
        import re
        m = re.search(r"QComboBox QAbstractItemView\s*\{([^}]*)\}", ui_mod.APP_QSS)
        self.assertIsNotNone(m, "QComboBox QAbstractItemView 规则必须存在")
        self.assertIn("#374151", m.group(1), "下拉弹层文字必须是深灰（与界面一致）")

    def test_settings_page_has_tasks_dir_entry(self):
        """RED 复现：设置页应有「任务工作目录」编辑入口。

        用户实测：设置页空白——首次启动选的任务工作目录无处查看/修改。
        根因：设置页只有微信文件目录，没有任务工作目录（tasks_dir）入口。
        """
        ctx = self._make_ctx()
        win = MainWindow(ctx)
        page = win.pages["设置"]
        page.refresh()
        self.assertTrue(hasattr(page, "ed_tasks"),
                        "设置页应有任务工作目录编辑框 ed_tasks")
        self.assertEqual(page.ed_tasks.text(), ctx.cfg.get("tasks_dir", ""),
                         "任务工作目录应回填当前配置")
        win.close()

    def test_tasks_page_scans_dir(self):
        ctx = self._make_ctx()
        os.makedirs(ctx.cfg["tasks_dir"], exist_ok=True)
        win = MainWindow(ctx)
        page = win.pages["任务"]
        page.refresh()  # 空目录不崩溃
        win.close()

    def test_app_context_paths_anchor_to_base(self):
        """配置路径锚定稳定目录，不随 cwd 漂移（保存不丢失的根因修复）"""
        import xiaoli_app.ui as ui_mod
        cwd = os.getcwd()
        try:
            os.chdir(tempfile.gettempdir())
            ctx = AppContext()
            self.assertTrue(os.path.isabs(ctx.cfg_path), "cfg_path 必须是绝对路径")
            self.assertTrue(os.path.isabs(ctx.cards_dir), "cards_dir 必须是绝对路径")
            # 源码模式下锚定项目根（xiaoli_desktop）
            base = os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(ui_mod.__file__))))
            self.assertEqual(ctx.cfg_path, os.path.join(base, "config.json"))
            self.assertEqual(ctx.cards_dir, os.path.join(base, "cards"))
        finally:
            os.chdir(cwd)

    def test_gui_entry_importable(self):
        # 入口模块可导入（不执行 main，避免启动引擎连微信）
        import xiaoli_gui  # noqa: F401
        self.assertTrue(callable(xiaoli_gui.main))


class TestUiFixes(unittest.TestCase):
    """UI 缺陷回归：单击编辑 / 测试连接信号桥 / 表格选中态 QSS"""

    def _page(self):
        from xiaoli_app.ui import AppContext
        from xiaoli_app.ui.pages import ModelsPage
        ctx = AppContext()
        ctx.cfg = {"providers": []}
        return ModelsPage(ctx)

    def test_table_single_click_edits(self):
        from PySide6.QtWidgets import QAbstractItemView
        mp = self._page()
        trig = mp.table.editTriggers()
        self.assertTrue(trig & QAbstractItemView.EditTrigger.SelectedClicked,
                        "单击应进入编辑（SelectedClicked）")

    def test_probe_signal_bridge_exists(self):
        mp = self._page()
        self.assertTrue(hasattr(mp, "_probe_done"),
                        "测试连接需经信号桥回主线程（跨线程弹窗会死锁）")

    def test_qss_table_selected_state(self):
        from xiaoli_app.ui import APP_QSS
        self.assertIn("QTableWidget::item:selected", APP_QSS,
                      "表格选中行应有变色反馈")
        self.assertIn("QTableWidget::item:hover", APP_QSS)

    def test_models_page_has_text_and_vision_sections(self):
        """模型页应有独立的文字模型/图片模型配置区（小白单独设置）"""
        from xiaoli_app.ui.pages import ModelsPage
        ctx = AppContext()
        ctx.cfg = {"providers": []}
        mp = ModelsPage(ctx)
        self.assertTrue(hasattr(mp, "cmb_text_provider"), "应有文字模型 provider 下拉")
        self.assertTrue(hasattr(mp, "cmb_text_model"), "应有文字模型下拉")
        self.assertTrue(hasattr(mp, "cmb_vision_provider"), "应有图片模型 provider 下拉")
        self.assertTrue(hasattr(mp, "cmb_vision_model"), "应有图片模型下拉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
