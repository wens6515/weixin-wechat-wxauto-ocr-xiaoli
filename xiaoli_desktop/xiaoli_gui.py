# -*- coding: utf-8 -*-
"""小漓桌面版入口（GUI）：pythonw xiaoli_gui.py（无终端）

启动流程（启动即空闲，不做任何初始化）：
1. config_store 加载/迁移/投影（纯配置读写，不连微信）
2. 构造 EngineThread（不启动线程，等首页「初始化」按钮触发 initialize）
3. 托盘 + 主窗口；QTimer 拉取总线事件刷新界面
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PySide6.QtWidgets import QApplication, QMessageBox

from xiaoli_app import config_store
from xiaoli_app.engine import EngineBus, EngineThread
from xiaoli_app.ui import AppContext, APP_QSS
from xiaoli_app.ui.main_window import MainWindow
from xiaoli_app.ui.tray import TrayIcon


def build_bot_factory(ctx):
    """bot 工厂：子线程内创建 AgentBot（连微信可能阻塞）。"""
    from xiaoli_bot import AgentBot

    def factory():
        return AgentBot(ctx.cfg)

    return factory


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("小漓")
    app.setQuitOnLastWindowClosed(False)  # 关窗不退出（隐藏到托盘）
    app.setStyleSheet(APP_QSS)

    ctx = AppContext()

    # 1. 配置：加载/迁移/投影（不连微信、不启动引擎）
    ctx.cfg = config_store.load_config_store(ctx.cfg_path, ctx.cards_dir)
    if not ctx.cfg.get("providers"):
        QMessageBox.warning(
            None, "小漓",
            "尚未配置 API Provider。\n请到「模型」页添加（如 DeepSeek 官方 API），"
            "并在「角色卡」页确认模型引用。",
        )

    # 2. 引擎（仅构造；线程与 bot 由首页「初始化」按钮触发）
    ctx.bus = EngineBus()
    ctx.engine = EngineThread(build_bot_factory(ctx), bus=ctx.bus)

    # 3. UI：托盘 + 主窗口
    win = MainWindow(ctx)
    tray = TrayIcon(parent=win)
    ctx.tray = tray

    def show_panel():
        win.show()
        win.raise_()
        win.activateWindow()

    tray.show_requested.connect(show_panel)
    tray.toggle_pause_requested.connect(win.pages["首页"].toggle_pause)
    tray.quit_requested.connect(app.quit)
    app.aboutToQuit.connect(lambda: ctx.engine.stop(timeout=5))
    tray.show()

    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
