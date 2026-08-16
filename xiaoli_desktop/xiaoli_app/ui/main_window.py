# -*- coding: utf-8 -*-
"""主窗口：左侧导航 + 右侧内容区 + 状态栏 + 定时刷新总线事件。关闭窗口隐藏到托盘。"""
import os

from PySide6.QtCore import QTimer, Qt, QSize, QByteArray, QRectF, QPoint, QPointF
from PySide6.QtGui import (QIcon, QPixmap, QPainter, QColor, QFont,
                           QPainterPath, QLinearGradient, QBrush)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QMainWindow, QListWidget, QListWidgetItem, QLabel,
                               QSystemTrayIcon, QMessageBox, QHBoxLayout, QWidget,
                               QStackedWidget, QVBoxLayout, QStyledItemDelegate,
                               QStyle, QStyleOptionViewItem, QApplication,
                               QFrame, QPushButton, QGraphicsDropShadowEffect,
                               QSizeGrip)

from .backdrop import ParticleBackdrop
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


# 导航项字体：必须显式 setFont——Qt 不把 QSS 的 ::item font-size 应用到
# item 渲染（item 继承 view 字体），QSS 写 px 实际无效（实测 small/large
# 档位下导航文字高度随 base 字号 13px→17px 变化）。setPixelSize 固定物理像素。
# 用户反馈链：42px 截断「角色卡→角···」→ 改垂直排布+38px → 用户再反馈
# 「文字下方被吞一点点，缩小 15-20%」→ 38→31（约 -18%），图标 43→40。
_NAV_FONT = QFont("Noto Sans SC", 15)  # 31px ≈ 15.5pt(96dpi)
_NAV_FONT.setPixelSize(31)
_NAV_FONT.setWeight(QFont.Weight.DemiBold)
# 导航图标渲染尺寸（43→40，随文字缩小保持比例）
_NAV_ICON_SIZE = 40


def _load_nav_icon(name, normal_color="#94A3B8", selected_color="#FFFFFF",
                   size=24) -> QIcon:
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


class NavItemDelegate(QStyledItemDelegate):
    """导航项垂直排布绘制：图标在上、文字在下、选中态左侧高亮条。

    QListWidgetItem 默认图标在左文字在右——42px 字号 3 字 + 48px 图标在
    200px 导航横排必然截断（用户实测「角色卡→角···」）。改用 delegate
    自绘：保留 QSS 选中渐变背景（super().paint 绘制底）+ 图标/文字垂直
    排布，宽度只受图标与单字宽度约束，任何导航名都不会截断。
    """

    def __init__(self, parent=None, icon_size=40):
        super().__init__(parent)
        self.icon_size = icon_size
        self.font = _NAV_FONT
        self.accent = QColor("#5B8CFF")

    def paint(self, painter, option, index):
        # 先画默认底（含 QSS 渐变选中背景 / hover 背景）
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        painter.save()
        if opt.widget is not None:
            style = opt.widget.style()
        else:
            style = QApplication.style()
        style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem,
                            opt, painter, opt.widget)
        # 垂直布局：图标居中上部，文字居中下部
        r = opt.rect
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        icon = opt.icon
        if not icon.isNull():
            isz = self.icon_size
            icon_rect = QRectF(r.center().x() - isz / 2,
                               r.top() + 8, isz, isz)
            mode = QIcon.Mode.Selected if selected else QIcon.Mode.Normal
            icon.paint(painter, icon_rect.toRect(), Qt.AlignmentFlag.AlignCenter,
                       mode, QIcon.State.Off)
        # 文字：选中白 / 未选中 muted
        text = opt.text
        if text:
            f = self.font
            if selected:
                color = QColor("#FFFFFF")
            else:
                color = QColor("#94A3B8")
            painter.setFont(f)
            painter.setPen(color)
            text_rect = QRectF(r.left(), r.top() + self.icon_size + 8,
                               r.width(), r.height() - self.icon_size - 12)
            painter.drawText(text_rect.toRect(),
                             Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                             text)
        # 选中态左侧高亮条（现代侧边栏风格）
        if selected:
            bar = QRectF(r.left() + 4, r.top() + 6, 3, r.height() - 12)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self.accent)
            painter.drawRoundedRect(bar, 1.5, 1.5)
        painter.restore()

    def sizeHint(self, option, index):
        base = super().sizeHint(option, index)
        return QSize(base.width(), max(base.height(), self.icon_size + 34))


class MainWindow(QMainWindow):
    # 圆角窗口常量
    _WINDOW_RADIUS = 16
    _SHELL_MARGIN = 22  # 四周留边放阴影
    _TITLEBAR_H = 42

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("小漓控制面板")
        self.resize(1020, 680)
        self.setMinimumSize(940, 620)  # 防 QStackedWidget sizeHint 强制撑大
        # 无边框 + 透明背景 → 自绘圆角窗口（Windows 非无边框无法真圆角）。
        # 保留 WA_TranslucentBackground 让 shell 圆角外的背景透出桌面。
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # 外层圆角容器：承载阴影效果 + 圆角裁剪；背景由 backdrop 绘制
        self._shell = QFrame(self)
        self._shell.setObjectName("windowShell")
        self.setCentralWidget(self._shell)
        _shadow = QGraphicsDropShadowEffect(self._shell)
        _shadow.setBlurRadius(28)
        _shadow.setOffset(0, 8)
        _shadow.setColor(QColor(0, 0, 0, 90))
        self._shell.setGraphicsEffect(_shadow)
        _shell_lay = QVBoxLayout(self._shell)
        _shell_lay.setContentsMargins(0, 0, 0, 0)
        _shell_lay.setSpacing(0)

        # 自绘标题栏（无系统边框 → 拖拽区 + 最小化/关闭按钮）
        self._build_titlebar(_shell_lay)

        # 主体：backdrop 垫底 + content 在上
        body = QWidget(self._shell)
        _shell_lay.addWidget(body, 1)
        self.backdrop = ParticleBackdrop(body)
        self.backdrop.setGeometry(0, 0, body.width(), body.height())
        self.backdrop.lower()

        def _sync_backdrop(e):
            self.backdrop.setGeometry(0, 0, e.size().width(), e.size().height())
            self._layout_nav()
        body.resizeEvent = _sync_backdrop

        content = QWidget(body)
        _outer = QVBoxLayout(body)
        _outer.setContentsMargins(0, 0, 0, 0)
        _outer.setSpacing(0)
        _outer.addWidget(content)
        lay = QHBoxLayout(content)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # 左侧导航
        self.nav = QListWidget()
        self.nav.setObjectName("navList")
        self.nav.setFixedWidth(200)

        # 右侧内容区
        self.stack = QStackedWidget()
        self.pages = {}
        # 导航字号/图标固定（不随字号档位变化）：用户要求导航始终大且居中。
        # 尺寸已按用户反馈 -10%（图标 48→43、字号 42→38），垂直排布防截断。
        self.nav.setItemDelegate(NavItemDelegate(self.nav, icon_size=_NAV_ICON_SIZE))
        # delegate 自绘图标，不再依赖 QListWidget 的 iconSize 钳制
        for cls, name, icon_name in _NAV_ITEMS:
            page = cls(ctx)
            self.pages[name] = page
            self.stack.addWidget(page)
            item = QListWidgetItem(_load_nav_icon(icon_name, size=_NAV_ICON_SIZE), name)
            item.setFont(_NAV_FONT)  # 显式固定导航字号（QSS ::item font-size 不生效）
            item.setSizeHint(QSize(0, 46))
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
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

        # 背景层应用当前主题/壁纸（粒子系统随主题重建）
        self.apply_backdrop()
        self._layout_nav()

    def _layout_nav(self):
        """左侧导航项从上到下排满：6 项均分导航高度（最小 88px 容纳垂直排布）。"""
        if getattr(self, "_nav_layouting", False):
            return
        self._nav_layouting = True
        try:
            n = self.nav.count()
            if n <= 0:
                return
            # 用 nav.height()（布局后稳定）而非 viewport 高度——viewport 随
            # item 高度变化导致收敛漂移；最小 88px 容纳 40px 图标 + 31px 文字
            per = max(88, (self.nav.height() - 28) // n)
            for i in range(n):
                self.nav.item(i).setSizeHint(QSize(0, per))
        finally:
            self._nav_layouting = False

    def showEvent(self, event):
        """首次显示后布局稳定，重算导航排满（构造时 nav 高度未定）。"""
        super().showEvent(event)
        self._layout_nav()

    # ---------- 圆角窗口 ----------

    def _build_titlebar(self, shell_lay):
        """自绘标题栏：左侧品牌区（可拖拽）+ 右侧最小化/关闭按钮。"""
        tb = QFrame()
        tb.setObjectName("titleBar")
        tb.setFixedHeight(self._TITLEBAR_H)
        h = QHBoxLayout(tb)
        h.setContentsMargins(14, 0, 10, 0)
        h.setSpacing(8)
        # 品牌区：logo + 标题（双击最大化，拖动移动窗口）
        from . import render_logo
        logo = QLabel()
        logo.setPixmap(render_logo(26))
        title = QLabel("小漓")
        title.setObjectName("titleBarTitle")
        sub = QLabel("控制面板")
        sub.setObjectName("titleBarSub")
        h.addWidget(logo)
        h.addWidget(title)
        h.addWidget(sub)
        h.addStretch(1)
        # 控制按钮
        btn_min = QPushButton("—")
        btn_min.setObjectName("winBtn")
        btn_min.setToolTip("最小化")
        btn_min.clicked.connect(self.showMinimized)
        self.btn_max = QPushButton("□")
        self.btn_max.setObjectName("winBtn")
        self.btn_max.setToolTip("最大化")
        self.btn_max.clicked.connect(self._toggle_max)
        btn_close = QPushButton("✕")
        btn_close.setObjectName("winBtn")
        btn_close.setToolTip("关闭")
        btn_close.clicked.connect(self.close)  # 走 closeEvent 弹窗逻辑
        h.addWidget(btn_min)
        h.addWidget(self.btn_max)
        h.addWidget(btn_close)
        shell_lay.addWidget(tb)
        # 窗口最大化/还原时切换按钮图标
        def _sync_max_btn(*_a):
            if self.isMaximized():
                self.btn_max.setText("❐")  # 还原
                self.btn_max.setToolTip("还原")
            else:
                self.btn_max.setText("□")  # 最大化
                self.btn_max.setToolTip("最大化")
        self.btn_max.clicked.connect(_sync_max_btn)
        self._sync_max_btn = _sync_max_btn
        # 拖拽移动：标题栏整条响应
        for w in (tb, logo, title, sub):
            w.mousePressEvent = lambda e: self._tb_press(e)
            w.mouseMoveEvent = lambda e: self._tb_move(e)
            w.mouseDoubleClickEvent = lambda e: self._tb_dblclick(e)
        self._drag_offset = None

    def _tb_press(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (e.globalPosition().toPoint()
                                 - self.frameGeometry().topLeft())

    def _tb_move(self, e):
        if self._drag_offset is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            self.move(e.globalPosition().toPoint() - self._drag_offset)

    def _tb_dblclick(self, e):
        self._toggle_max()

    def _toggle_max(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        sync = getattr(self, "_sync_max_btn", None)
        if sync is not None:
            sync()

    def changeEvent(self, event):
        """最大化/还原/最小化时同步标题栏按钮图标。"""
        super().changeEvent(event)
        if event.type() == event.Type.WindowStateChange:
            sync = getattr(self, "_sync_max_btn", None)
            if sync is not None:
                sync()

    def _sync_shell_geometry(self):
        """shell 由 QMainWindow 布局管理（充满窗口），阴影空间依赖窗口透明边缘。
        （保留方法避免误用；圆角由 backdrop 裁剪实现）"""

    def resizeEvent(self, event):
        super().resizeEvent(event)

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

    def apply_backdrop(self, theme=None, wallpaper=None):
        """主题/壁纸切换时同步背景层（设置页/入口调用）。"""
        theme = theme if theme is not None else self.ctx.theme()
        wallpaper = wallpaper if wallpaper is not None else self.ctx.wallpaper()
        self.backdrop.set_theme(theme)
        self.backdrop.set_wallpaper(wallpaper)
        # 页面级主题钩子（首页标题光晕等随主题主色刷新）
        for p in self.pages.values():
            hook = getattr(p, "apply_theme", None)
            if hook is not None:
                try:
                    hook(theme)
                except Exception:
                    pass

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
