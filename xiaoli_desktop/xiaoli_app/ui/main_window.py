# -*- coding: utf-8 -*-
"""主窗口：左侧导航 + 右侧内容区 + 状态栏 + 定时刷新总线事件。关闭窗口隐藏到托盘。"""
import os

from PySide6.QtCore import QTimer, Qt, QSize, QByteArray
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QMainWindow, QListWidget, QListWidgetItem, QLabel,
                               QSystemTrayIcon, QMessageBox, QHBoxLayout, QWidget,
                               QStackedWidget)

from .pages import HomePage, CardsPage, ModelsPage, TasksPage, LogPage, SettingsPage
from ._icons import SVG_ICONS

# 导航项：(页面类, 显示名, Lucide 图标名)。图标内嵌于 _icons.py（ISC 协议，
# 下载自 unpkg；原始 SVG 见 assets/icons/*.svg）。
_NAV_ITEMS = (
    (HomePage, "首页", "home"),
    (CardsPage, "角色卡", "user-round"),
    (ModelsPage, "模型", "cpu"),
    (TasksPage, "任务", "list-checks"),
    (LogPage, "日志", "file-text"),
    (SettingsPage, "设置", "settings"),
)


def _load_nav_icon(name, normal_color="#94A3B8", selected_color="#FFFFFF",
                   size=20) -> QIcon:
    """运行时渲染 Lucide SVG 图标并着色，返回 normal/selected 双态 QIcon。

    Lucide 图标 stroke=currentColor——QSvgRenderer 渲染时以 QPainter 的 pen
    颜色作为 currentColor，据此生成未选中（muted）与选中（白）两套 pixmap。
    QListWidget 项选中时 Qt 自动切到 Selected 态 pixmap，图标随选中态变白。
    """
    svg = SVG_ICONS.get(name)
    if not svg:
        return QIcon()
    svg_bytes = QByteArray(svg.encode("utf-8"))
    icon = QIcon()
    for mode, color in ((QIcon.Mode.Normal, normal_color),
                        (QIcon.Mode.Active, selected_color),
                        (QIcon.Mode.Selected, selected_color)):
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor(color))
        QSvgRenderer(svg_bytes).render(painter)
        painter.end()
        icon.addPixmap(pm, mode)
    return icon


class MainWindow(QMainWindow):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("小漓控制面板")
        self.resize(1020, 680)

        central = QWidget()
        self.setCentralWidget(central)
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # 左侧导航
        self.nav = QListWidget()
        self.nav.setObjectName("navList")
        self.nav.setFixedWidth(200)

        # 右侧内容区
        self.stack = QStackedWidget()
        self.pages = {}
        for cls, name, icon_name in _NAV_ITEMS:
            page = cls(ctx)
            self.pages[name] = page
            self.stack.addWidget(page)
            item = QListWidgetItem(_load_nav_icon(icon_name), name)
            item.setSizeHint(QSize(0, 46))
            self.nav.addItem(item)
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)

        lay.addWidget(self.nav)
        lay.addWidget(self.stack, 1)

        # 首次填充：各页 refresh() 从 ctx.cfg 回填数据（模型表/设置项等）。
        # 不调的话 ModelsPage 表格永远空、SettingsPage 编辑框永远空——
        # 用户添加的模型保存到了 config.json 但重启后界面不显示。
        for page in self.pages.values():
            refresh = getattr(page, "refresh", None)
            if refresh is not None:
                try:
                    refresh()
                except Exception:
                    pass
        self.nav.setCurrentRow(0)

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
        """关闭窗口：弹确认框——隐藏到托盘（继续运行）或完全退出。"""
        tray = getattr(self.ctx, "tray", None)
        if tray is None or not tray.isVisible():
            event.accept()  # 无托盘（测试/异常态）：直接关闭
            return
        box = QMessageBox(self)
        box.setWindowTitle("小漓")
        box.setText("关闭窗口后要做什么？")
        box.setInformativeText("隐藏到托盘：小漓继续在后台运行并回复消息\n完全退出：停止小漓（可从托盘重新打开）")
        b_hide = box.addButton("隐藏到托盘", QMessageBox.ButtonRole.AcceptRole)
        b_quit = box.addButton("完全退出", QMessageBox.ButtonRole.DestructiveRole)
        b_cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is b_quit:
            event.accept()
            from PySide6.QtWidgets import QApplication
            QApplication.quit()  # aboutToQuit → engine.stop
        elif clicked is b_hide:
            event.ignore()
            self.hide()
            tray.showMessage("小漓", "已最小化到托盘，双击图标重新打开", QSystemTrayIcon.Information, 2000)
        else:  # 取消
            event.ignore()
