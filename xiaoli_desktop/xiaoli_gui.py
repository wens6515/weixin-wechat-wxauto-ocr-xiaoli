# -*- coding: utf-8 -*-
"""小漓桌面版入口（GUI）：pythonw xiaoli_gui.py（无终端）

启动流程（启动即空闲，不做任何初始化）：
1. config_store 加载/迁移/投影（纯配置读写，不连微信）
2. 构造 EngineThread（不启动线程，等首页「初始化」按钮触发 initialize）
3. 托盘 + 主窗口；QTimer 拉取总线事件刷新界面
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton)

from xiaoli_app import config_store
from xiaoli_app.engine import EngineBus, EngineThread
from xiaoli_app.ui import AppContext, APP_QSS
from xiaoli_app.ui.main_window import MainWindow
from xiaoli_app.ui.tray import TrayIcon


class FirstRunDialog(QDialog):
    """首次启动引导：选择任务工作目录与记忆存储位置（默认 %USERPROFILE%\\小漓）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("欢迎使用小漓")
        self.setMinimumWidth(560)
        form = QFormLayout(self)
        tip = QLabel(
            "小漓需要两个文件夹存放工作文件：\n"
            "任务工作目录：小漓与天枢交换任务/成果文件的地方\n"
            "记忆存储位置：对话记忆文件（memory.json）\n"
            "建议保持默认位置，直接点击「开始使用」即可。")
        tip.setWordWrap(True)
        form.addRow(tip)
        self.ed_tasks = QLineEdit(config_store.default_tasks_dir())
        self.ed_memory = QLineEdit(config_store.default_memory_file())
        self.ed_tasks.setReadOnly(True)
        self.ed_memory.setReadOnly(True)
        btn_tasks = QPushButton("浏览…")
        btn_tasks.clicked.connect(lambda: self._pick(self.ed_tasks, True))
        btn_mem = QPushButton("浏览…")
        btn_mem.clicked.connect(lambda: self._pick(self.ed_memory, False))
        row_t = QHBoxLayout()
        row_t.addWidget(self.ed_tasks, 1)
        row_t.addWidget(btn_tasks)
        row_m = QHBoxLayout()
        row_m.addWidget(self.ed_memory, 1)
        row_m.addWidget(btn_mem)
        form.addRow("任务工作目录", row_t)
        form.addRow("记忆存储位置", row_m)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("开始使用")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _pick(self, edit, is_dir):
        if is_dir:
            p = QFileDialog.getExistingDirectory(self, "选择文件夹", edit.text())
            if p:
                edit.setText(p)
        else:
            p, _f = QFileDialog.getSaveFileName(
                self, "选择记忆文件位置", edit.text(), "JSON (*.json)")
            if p:
                edit.setText(p)

    def result_cfg(self):
        return {
            "tasks_dir": self.ed_tasks.text().strip() or config_store.default_tasks_dir(),
            "memory_file": self.ed_memory.text().strip() or config_store.default_memory_file(),
        }


def build_bot_factory(ctx):
    """bot 工厂：子线程内创建 AgentBot（连微信可能阻塞）。"""
    from xiaoli_bot import AgentBot

    def factory():
        return AgentBot(ctx.cfg)

    return factory


def main():
    # 单实例锁：防止双击多开（多实例会并发抢微信窗口/任务冲突）
    from xiaoli_bot import acquire_single_instance
    if not acquire_single_instance("XiaoLiGui_SingleInstance"):
        QMessageBox.warning(None, "小漓", "小漓已在运行（在系统托盘图标处查看）。")
        return

    app = QApplication(sys.argv)
    app.setApplicationName("小漓")
    app.setQuitOnLastWindowClosed(False)  # 关窗不退出（隐藏到托盘）
    app.setStyleSheet(APP_QSS)

    ctx = AppContext()

    # 1. 配置：加载/迁移/投影（不连微信、不启动引擎）
    ctx.cfg = config_store.load_config_store(ctx.cfg_path, ctx.cards_dir)
    # 首次启动（config 文件不存在）→ 引导选择工作目录与记忆位置，避免默认 D:\ 盘缺失崩溃
    if not os.path.isfile(ctx.cfg_path):
        dlg = FirstRunDialog()
        dlg.exec()
        ctx.cfg.update(dlg.result_cfg())
        try:
            config_store.save_config(ctx.cfg, ctx.cfg_path)
        except OSError as e:
            QMessageBox.warning(None, "小漓", f"配置保存失败：{e}")
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
