# -*- coding: utf-8 -*-
"""系统托盘：图标（基础 logo + 运行状态角标）+ 菜单（打开面板 / 暂停·恢复 / 退出）。

基础图标：入口未传 icon_path 时用内置渐变 logo（render_logo）；有路径则加载缩放。
状态角标：右上角小圆点——运行=绿、暂停=黄、错误=红、空闲=灰，主窗口
drain bus 时调用 set_state 同步。
"""
import os

from PySide6.QtCore import QObject, QPointF, Qt, Signal
from PySide6.QtGui import (QAction, QBrush, QColor, QIcon, QPainter, QPen,
                           QPixmap)
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from . import render_logo

# 状态 → 角标颜色（语义与首页状态灯一致：绿运行/黄暂停/红错误/灰空闲）
_STATE_COLORS = {
    "idle": "#94A3B8",
    "running": "#10B981",
    "paused": "#F59E0B",
    "error": "#EF4444",
}
_BADGE_R = 9  # 角标圆点半径（64px 底图上）

_TRAY_ICON_SIZE = 64


def _icon_with_state(base: QPixmap, state: str) -> QIcon:
    """基础图标 + 右上角状态圆点（白描边，深色系统下也可辨）。"""
    pm = base.copy()
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor(_STATE_COLORS.get(state, _STATE_COLORS["idle"]))
    p.setPen(QPen(QColor("#FFFFFF"), 2))
    p.setBrush(QBrush(color))
    cx = pm.width() - _BADGE_R - 3
    cy = _BADGE_R + 3
    p.drawEllipse(QPointF(cx, cy), _BADGE_R, _BADGE_R)
    p.end()
    return QIcon(pm)


class TrayIcon(QObject):
    """托盘图标。信号桥接给主窗口/入口处理（保持本类无业务逻辑）。"""

    show_requested = Signal()              # 打开/唤起控制面板
    toggle_pause_requested = Signal()      # 暂停 ⇄ 恢复
    quit_requested = Signal()              # 退出程序

    def __init__(self, icon_path=None, parent=None):
        super().__init__(parent)
        base = QPixmap(_TRAY_ICON_SIZE, _TRAY_ICON_SIZE)
        if icon_path and os.path.isfile(icon_path):
            src = QPixmap(icon_path)
            if not src.isNull():
                base = src.scaled(_TRAY_ICON_SIZE, _TRAY_ICON_SIZE,
                                  Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
        else:
            base = render_logo(_TRAY_ICON_SIZE)
        self._base = base
        self._state = "idle"
        self.tray = QSystemTrayIcon(_icon_with_state(base, self._state), parent)
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

    def set_state(self, state):
        """更新托盘角标（idle/running/paused/error），状态不变则跳过。"""
        if state not in _STATE_COLORS or state == self._state:
            return
        self._state = state
        self.tray.setIcon(_icon_with_state(self._base, state))

    def set_paused_text(self, paused):
        self.act_pause.setText("恢复回复" if paused else "暂停回复")

    def show(self):
        self.tray.show()

    def isVisible(self):
        return self.tray.isVisible()

    def showMessage(self, *args, **kwargs):
        self.tray.showMessage(*args, **kwargs)
