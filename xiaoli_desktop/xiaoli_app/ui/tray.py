# -*- coding: utf-8 -*-
"""系统托盘：图标 + 菜单（打开面板 / 暂停·恢复 / 退出）。"""
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


class TrayIcon(QObject):
    """托盘图标。信号桥接给主窗口/入口处理（保持本类无业务逻辑）。"""

    show_requested = Signal()              # 打开/唤起控制面板
    toggle_pause_requested = Signal()      # 暂停 ⇄ 恢复
    quit_requested = Signal()              # 退出程序

    def __init__(self, icon_path=None, parent=None):
        super().__init__(parent)
        self._icon = QIcon(icon_path) if icon_path else QIcon()
        self.tray = QSystemTrayIcon(self._icon, parent)
        self.tray.setToolTip("小漓控制面板")

        menu = QMenu()
        act_open = menu.addAction("打开控制面板")
        self.act_pause = menu.addAction("暂停回复")
        menu.addSeparator()
        act_quit = menu.addAction("退出小漓")
        self.tray.setContextMenu(menu)

        act_open.triggered.connect(self.show_requested.emit)
        self.act_pause.triggered.connect(self.toggle_pause_requested.emit)
        act_quit.triggered.connect(self.quit_requested.emit)
        self.tray.activated.connect(self._on_activated)

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_requested.emit()

    def set_paused_text(self, paused):
        self.act_pause.setText("恢复回复" if paused else "暂停回复")

    def show(self):
        self.tray.show()

    def isVisible(self):
        return self.tray.isVisible()

    def showMessage(self, *args, **kwargs):
        self.tray.showMessage(*args, **kwargs)
