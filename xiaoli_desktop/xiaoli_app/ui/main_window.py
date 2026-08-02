# -*- coding: utf-8 -*-
"""主窗口：六 Tab 面板 + 状态栏 + 定时刷新总线事件。关闭窗口隐藏到托盘。"""
from PySide6.QtWidgets import QMainWindow, QTabWidget, QLabel, QSystemTrayIcon
from PySide6.QtCore import QTimer

from .pages import HomePage, CardsPage, ModelsPage, TasksPage, LogPage, SettingsPage


class MainWindow(QMainWindow):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("小漓控制面板")
        self.resize(960, 660)

        # 六页
        self.tabs = QTabWidget()
        self.pages = {}
        for cls, name in ((HomePage, "首页"), (CardsPage, "角色卡"),
                          (ModelsPage, "模型"), (TasksPage, "任务"),
                          (LogPage, "日志"), (SettingsPage, "设置")):
            page = cls(ctx)
            self.pages[name] = page
            self.tabs.addTab(page, name)
        self.setCentralWidget(self.tabs)

        # 状态栏
        self.status_label = QLabel("就绪")
        self.statusBar().addWidget(self.status_label)

        # 定时刷新：总线事件 + 各页 tick
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start(1000)

    # ---------- 刷新 ----------

    def _on_tick(self):
        self._drain_bus()
        for p in self.pages.values():
            tick = getattr(p, "tick", None)
            if tick is not None:
                try:
                    tick()
                except Exception:
                    pass

    def _drain_bus(self):
        bus = self.ctx.bus
        if bus is None:
            return
        for kind, payload in bus.drain():
            if kind == "status":
                st = payload.get("state")
                if st == "paused":
                    self.status_label.setText("已暂停")
                elif st == "running":
                    self.status_label.setText("运行中")
                elif st == "started":
                    self.status_label.setText("引擎已启动")
                elif st == "stopped":
                    self.status_label.setText("引擎已停止")
            elif kind == "error":
                self.status_label.setText(f"错误: {payload.get('message', '')[:60]}")

    # ---------- 托盘联动 ----------

    def closeEvent(self, event):
        """关闭窗口 → 隐藏到托盘（托盘可见时），真正退出走托盘菜单。"""
        tray = getattr(self.ctx, "tray", None)
        if tray is not None and tray.isVisible():
            event.ignore()
            self.hide()
            tray.showMessage("小漓", "已最小化到托盘，双击图标重新打开", QSystemTrayIcon.Information, 2000)
        else:
            event.accept()
