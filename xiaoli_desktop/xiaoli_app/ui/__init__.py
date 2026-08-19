# -*- coding: utf-8 -*-
"""PySide6 控制面板：托盘（tray.py）+ 主窗口（main_window.py）+ 页面（pages.py）。

AppContext：引擎/总线/配置的轻量容器，页面与入口共享。
主题系统：THEMES 定义 12 套主题（11 套精选风格 + blue 兼容默认），
build_qss 按主题 + 可选壁纸生成 QSS；毛玻璃用半透明卡片（rgba）透过
壁纸/背景实现（Qt QSS 无真高斯模糊）。组件语言统一现代规范：
大圆角卡片、渐变主按钮、focus 高亮环、圆角滚动条、tooltip/菜单样式。
"""
import os
import re
import sys
from string import Template

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QPainter, QPainterPath, QPalette, QPen,
                           QPixmap)
from PySide6.QtWidgets import (QApplication, QStyle, QStyleOptionViewItem,
                               QStyledItemDelegate)

# 基础色板：所有主题的兜底字段。主题 dict 覆盖需要的字段，缺失的用这里补，
# 保证未来新增主题缺字段也不会 KeyError（substitute 前合并）。
_DEFAULTS = {
    "bg": "#F4F7FC", "p1": "#5B8CFF", "p2": "#8B5CF6",
    "card": "rgba(255,255,255,0.88)", "text": "#374151", "muted": "#94A3B8",
    "border": "#E8EDF5", "hover": "#EAF0FE", "input_bg": "#FFFFFF",
    "frame": "#FBFCFE", "header": "#F4F7FC",
    "success": "#10B981", "danger": "#EF4444", "warning": "#F59E0B",
    "accent": "#5B8CFF", "focus": "#5B8CFF", "glow": "#8B5CF6",
    "scrollbar": "#CBD5E1", "card_alt": "#F8FAFC",
    "card_border": None,  # 卡片专用边框（None → 深色主题自动提亮 $border）
}

# 12 套主题。字段语义：
#   label 显示名 / group 设置页分组（浅色/深色/氛围）
#   p1/p2 渐变主色两端；bg 背景；card 卡片（半透明=毛玻璃）；
#   text/muted 文字/次要文字；border/hover/input_bg 边框/悬停/输入框底色；
#   frame/header 面板/表头底色；success/danger/warning 状态色（环境检测/进度）；
#   accent 次强调色；focus 输入框聚焦环色；glow 发光/光晕色（霓虹、弥散渐变）；
#   scrollbar 滚动条滑块色；card_alt 次级卡片底色。
THEMES = {
    # Tokyo Night 置顶（用户指定：默认主题，排序第一个展示）
    "tokyonight": {"label": "Tokyo Night", "group": "深色",
                   "bg": "#1A1B26", "p1": "#7AA2F7", "p2": "#BB9AF7",
                   "card": "rgba(26,27,38,0.85)", "text": "#C0CAF5", "muted": "#6B7390",
                   "border": "#2F354D", "hover": "#292E42", "input_bg": "#16161E",
                   "frame": "#1C1D29", "header": "#1A1B26",
                   "success": "#9ECE6A", "danger": "#F7768E", "warning": "#E0AF68",
                   "accent": "#7DCFFF", "focus": "#7AA2F7", "glow": "#BB9AF7",
                   "scrollbar": "#3B4261", "card_alt": "#24283B"},
    # ---- 浅色系 ----
    "blue": {"label": "蓝紫·默认（兼容保底）", "group": "浅色",
             "bg": "#F4F7FC", "p1": "#5B8CFF", "p2": "#8B5CF6",
             "card": "rgba(255,255,255,0.88)", "text": "#374151", "muted": "#94A3B8",
             "border": "#E8EDF5", "hover": "#EAF0FE", "input_bg": "#FFFFFF",
             "frame": "#FBFCFE", "header": "#F4F7FC",
             "success": "#10B981", "danger": "#EF4444", "warning": "#F59E0B",
             "accent": "#5B8CFF", "focus": "#5B8CFF", "glow": "#8B5CF6",
             "scrollbar": "#CBD5E1", "card_alt": "#F8FAFC"},
    "fluent": {"label": "Fluent 亚克力", "group": "浅色",
               "bg": "#F3F3F3", "p1": "#0067C0", "p2": "#2B7CD3",
               "card": "rgba(255,255,255,0.72)", "text": "#1B1B1B", "muted": "#757575",
               "border": "#E0E0E0", "hover": "#E5F1FB", "input_bg": "#FFFFFF",
               "frame": "#F7F7F7", "header": "#F3F3F3",
               "success": "#107C10", "danger": "#C42B1C", "warning": "#C73E1D",
               "accent": "#0067C0", "focus": "#0067C0", "glow": "#4CC2FF",
               "scrollbar": "#C7C7C7", "card_alt": "#FAFAFA"},
    "m3": {"label": "Material You", "group": "浅色",
           "bg": "#FEF7FF", "p1": "#6750A4", "p2": "#7D5260",
           "card": "rgba(255,255,255,0.90)", "text": "#1C1B1F", "muted": "#79747E",
           "border": "#E6E0E9", "hover": "#E8DEF8", "input_bg": "#F7F2FA",
           "frame": "#FDF8FD", "header": "#FEF7FF",
           "success": "#386A20", "danger": "#B3261E", "warning": "#7A5900",
           "accent": "#6750A4", "focus": "#6750A4", "glow": "#EADDFF",
           "scrollbar": "#CAC4D0", "card_alt": "#F7F2FA"},
    "linear": {"label": "极简 Linear", "group": "浅色",
               "bg": "#FFFFFF", "p1": "#5E6AD2", "p2": "#5E6AD2",
               "card": "#FFFFFF", "text": "#1F2328", "muted": "#6E6E6E",
               "border": "#E4E4E7", "hover": "#F4F4F5", "input_bg": "#FAFAFA",
               "frame": "#FCFCFC", "header": "#FFFFFF",
               "success": "#4DAC37", "danger": "#E5484D", "warning": "#F5A524",
               "accent": "#5E6AD2", "focus": "#5E6AD2", "glow": "#EFEFFF",
               "scrollbar": "#D4D4D8", "card_alt": "#FAFAFA"},
    "aurora": {"label": "弥散渐变 Aurora", "group": "浅色",
               "bg": "#F5F7FF", "p1": "#667EEA", "p2": "#F093FB",
               "card": "rgba(255,255,255,0.68)", "text": "#2D3350", "muted": "#8A90B0",
               "border": "#E3E7F5", "hover": "#ECEEFF", "input_bg": "#FFFFFF",
               "frame": "#F8FAFF", "header": "#F5F7FF",
               "success": "#34D399", "danger": "#F87171", "warning": "#FBBF24",
               "accent": "#A78BFA", "focus": "#667EEA", "glow": "#C084FC",
               "scrollbar": "#C7CDE8", "card_alt": "#FFFFFF"},
    "ink": {"label": "中国风水墨", "group": "浅色",
            "bg": "#F7F3EA", "p1": "#C0402F", "p2": "#7A5C3E",
            "card": "rgba(255,252,245,0.88)", "text": "#2C2A26", "muted": "#8B847A",
            "border": "#E2DAC8", "hover": "#EFE7D8", "input_bg": "#FDFAF3",
            "frame": "#FAF6EE", "header": "#F7F3EA",
            "success": "#5B7B5A", "danger": "#A63D2F", "warning": "#B8860B",
            "accent": "#8C3B2E", "focus": "#C0402F", "glow": "#C0402F",
            "scrollbar": "#CFC5B0", "card_alt": "#FDFAF3"},
    # ---- 深色系 ----
    "dracula": {"label": "Dracula 德古拉", "group": "深色",
                "bg": "#282A36", "p1": "#BD93F9", "p2": "#FF79C6",
                "card": "rgba(40,42,54,0.85)", "text": "#F8F8F2", "muted": "#8A8FA3",
                "border": "#44475A", "hover": "#3B3E4D", "input_bg": "#1F2129",
                "frame": "#232530", "header": "#282A36",
                "success": "#50FA7B", "danger": "#FF5555", "warning": "#F1FA8C",
                "accent": "#8BE9FD", "focus": "#BD93F9", "glow": "#FF79C6",
                "scrollbar": "#5A5E6E", "card_alt": "#2F3242"},
    "nord": {"label": "Nord 北极", "group": "深色",
             "bg": "#2E3440", "p1": "#88C0D0", "p2": "#81A1C1",
             "card": "rgba(46,52,64,0.85)", "text": "#ECEFF4", "muted": "#7B88A1",
             "border": "#434C5E", "hover": "#3B4252", "input_bg": "#272C36",
             "frame": "#2B303B", "header": "#2E3440",
             "success": "#A3BE8C", "danger": "#BF616A", "warning": "#EBCB8B",
             "accent": "#8FBCBB", "focus": "#88C0D0", "glow": "#88C0D0",
             "scrollbar": "#616E88", "card_alt": "#3B4252"},
    "onedark": {"label": "One Dark", "group": "深色",
                "bg": "#282C34", "p1": "#61AFEF", "p2": "#C678DD",
                "card": "rgba(40,44,52,0.85)", "text": "#ABB2BF", "muted": "#6F7683",
                "border": "#3E4452", "hover": "#31363F", "input_bg": "#23262D",
                "frame": "#262A31", "header": "#282C34",
                "success": "#98C379", "danger": "#E06C75", "warning": "#E5C07B",
                "accent": "#56B6C2", "focus": "#61AFEF", "glow": "#C678DD",
                "scrollbar": "#4B5263", "card_alt": "#2F343D"},
    "catppuccin": {"label": "Catppuccin", "group": "深色",
                   "bg": "#1E1E2E", "p1": "#89B4FA", "p2": "#CBA6F7",
                   "card": "rgba(30,30,46,0.85)", "text": "#CDD6F4", "muted": "#9399B2",
                   "border": "#45475A", "hover": "#313244", "input_bg": "#181825",
                   "frame": "#1B1B29", "header": "#1E1E2E",
                   "success": "#A6E3A1", "danger": "#F38BA8", "warning": "#F9E2AF",
                   "accent": "#94E2D5", "focus": "#89B4FA", "glow": "#CBA6F7",
                   "scrollbar": "#585B70", "card_alt": "#313244"},
    "cyberpunk": {"label": "赛博朋克", "group": "深色",
                  "bg": "#0D0221", "p1": "#00F0FF", "p2": "#FF00FF",
                  "card": "rgba(18,8,46,0.85)", "text": "#E6E1FF", "muted": "#8B7CC8",
                  "border": "#3D2E63", "hover": "#1E1240", "input_bg": "#150A2E",
                  "frame": "#12072B", "header": "#0D0221",
                  "success": "#00FF9D", "danger": "#FF3860", "warning": "#FFD700",
                  "accent": "#00F0FF", "focus": "#00F0FF", "glow": "#FF00FF",
                  "scrollbar": "#4A3A7A", "card_alt": "#1A0E38"},
}

# Aurora 弥散渐变由 ParticleBackdrop 运行时绘制（斜向三段色彩过渡），
# QSS 不再承担背景生成。

_QSS_TEMPLATE = Template("""
* { font-family: "Noto Sans SC", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
    font-size: $fs_base; }
QLabel, QListWidget, QTableWidget, QLineEdit, QPlainTextEdit, QTextEdit,
QComboBox, QSpinBox, QDoubleSpinBox, QProgressBar, QCheckBox { color: $text; }
/* 背景由 ParticleBackdrop 绘制（渐变+粒子）；QMainWindow 纯 bg 兜底。
   无边框圆角窗口下 QMainWindow 必须透明，否则圆角外漏出方块底色 */
QMainWindow { background: transparent; }
/* 顶层对话框：深色系统 palette 下会漏出黑底（欢迎窗/确认框）——显式覆盖 */
QDialog { background: $bg; }
QMessageBox { background: $bg; }
/* 提示框/确认框固定大字（不随字号档位）：用户要求弹窗始终清晰可读 */
QMessageBox, QDialog { font-size: 18px; }
QMessageBox QLabel { font-size: 16px; }
QDialogButtonBox QPushButton { font-size: 16px; min-height: 38px; padding: 8px 26px; }
/* 滚动区/页面容器：viewport 透出背景层，避免深色 palette 大黑边 */
QScrollArea, QStackedWidget { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QTabWidget::pane { border: none; background: transparent; }
QTabBar::tab { background: transparent; padding: 10px 22px; margin-right: 8px;
               border-radius: 10px; color: $muted; font-size: $fs_base; font-weight: 500; }
QTabBar::tab:hover { background: $hover; color: $text; }
QTabBar::tab:selected { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                       stop:0 $p1, stop:1 $p2);
                       color: #FFFFFF; font-weight: 600; }
QLabel#title { font-size: $fs_title; font-weight: 800; color: $p1; letter-spacing: 2px; }
QLabel#subtitle { font-size: $fs_sub; color: $muted; letter-spacing: 1px; }
QLabel#stateLabel { font-size: 14px; color: $muted; }
/* 环境检测标签（HomePage）：tag 状态色跟随主题，不再散落硬编码。
   envDot 为仪表盘圆点（●），envOk/envBad 同时服务标签与圆点 */
QLabel#envTag { font-weight: 600; color: $muted; }
QLabel#envOk { font-weight: 600; color: $success; }
QLabel#envBad { font-weight: 600; color: $danger; }
QLabel#envPending { font-weight: 600; color: $muted; }
/* 页内提示文字（ModelsPage tip_model 等）：text 80% 透明度，浅于正文深于 muted */
QLabel#tip { color: $tip; font-size: 13px; }
QFrame#card { background: $card; border: 1px solid $card_border; border-radius: 16px; }
QPushButton#btnMain { color: white; border: none; border-radius: 14px;
                      font-size: 16px; font-weight: 600; padding: 12px 36px;
                      background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 $p1, stop:1 $p2); }
QPushButton#btnMain[tone="primary"]:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 $p1_hi, stop:1 $p2_hi); }
QPushButton#btnMain[tone="primary"]:pressed { background: $p2; padding: 14px 32px 16px 32px; }
QPushButton#btnMain[tone="primary"]:disabled { background: $muted; }
QPushButton#btnMain[tone="warn"] { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 #FBBF24, stop:1 #F59E0B); }
QPushButton#btnMain[tone="warn"]:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 #FCD34D, stop:1 #FBBF24); }
QPushButton#btnMain[tone="warn"]:pressed { background: #D97706; }
QProgressBar { border: none; border-radius: 8px; background: $border; height: 12px; }
QProgressBar::chunk { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 $p1, stop:1 $p2); border-radius: 8px; }
QPushButton { background: $input_bg; border: 1px solid $border; border-radius: 10px;
              padding: 8px 20px; min-height: 32px; color: $text; font-size: $fs_base; font-weight: 500;
              letter-spacing: 1px; }
QPushButton:hover { background: $hover; border-color: $p1; color: $text; }
QPushButton:pressed { background: $hover; border-top: 2px solid rgba(0,0,0,0.12); }
QPushButton:disabled { color: $muted; background: $frame; border-color: $border; }
/* 紧凑按钮：窄栏横排（如角色卡页 5 按钮行）——小 padding 防文字挤压裁剪 */
QPushButton[compact="true"] { padding: 6px 8px; min-height: 30px; }
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: $input_bg; border: 1px solid $border; border-radius: 10px;
    padding: 5px 10px; min-height: 30px; }
/* 下拉弹层（QComboBox 弹出的列表）：深色系统主题下 palette 文字是白色，
   必须显式设深灰文字 + 白底，否则浅色界面上下拉项白字看不清 */
QComboBox QAbstractItemView {
    color: $text; background: $input_bg; border: 1px solid $border;
    border-radius: 10px; padding: 4px; selection-background-color: $hover; selection-color: $text; }
QComboBox QAbstractItemView::item { padding: 9px 12px; border-radius: 6px; }
QComboBox QAbstractItemView::item:selected { background: $hover; color: $text; }
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus { border: 1px solid $focus; background: $input_bg; }
QListWidget, QTableWidget { background: $input_bg; border: 1px solid $border;
                            border-radius: 12px; }
QTableWidget { alternate-background-color: $card_alt; gridline-color: transparent; }
QListWidget::item { padding: 7px 10px; border-radius: 8px; margin: 2px 4px; }
QListWidget::item:hover { background: $hover; }
QListWidget::item:selected { background: $hover; color: $text; }
/* 自绘标题栏（无边框圆角窗口）：品牌区 + 窗口控制按钮 */
QFrame#titleBar { background: transparent; }
QLabel#titleBarTitle { font-size: 15px; font-weight: 700; color: $text; }
QLabel#titleBarSub { font-size: 12px; color: $muted; }
QPushButton#winBtn { background: transparent; border: none; border-radius: 8px;
                     font-size: 15px; color: $muted; padding: 2px 8px;
                     min-height: 26px; }
QPushButton#winBtn:hover { background: $hover; color: $text; }
QPushButton#winBtn:pressed { background: $border; }
/* 外层圆角容器：背景由 backdrop 绘制，这里只保证圆角裁剪区透明 */
QFrame#windowShell { background: transparent; border: none; }
/* 左侧导航栏：窄侧边 + 竖排导航项，选中态渐变背景 + 白字白图标 */
QListWidget#navList { background: transparent; border: none;
                      border-right: 1px solid $border; padding: 14px 10px; outline: 0; }
QListWidget#navList::item { padding: 8px 10px; border-radius: 10px;
                            margin: 3px 6px; color: $muted; font-size: 42px;
                            font-weight: 600; letter-spacing: 2px; }
QListWidget#navList::item:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 $nav_hover0, stop:0.5 $nav_hover1, stop:1 $nav_hover2);
    color: $text; }
QListWidget#navList::item:selected {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 $p1, stop:1 $p2);
    color: #FFFFFF; font-weight: 600; }
/* 设置页主题缩略图网格：选中项主色描边 */
QListWidget#themeGrid, QListWidget#wpGrid { background: transparent; border: none; }
QListWidget#themeGrid::item, QListWidget#wpGrid::item { border: 2px solid transparent;
                              border-radius: 8px; color: $muted; font-size: 12px; }
QListWidget#themeGrid::item:hover, QListWidget#wpGrid::item:hover { background: $hover; }
QListWidget#themeGrid::item:selected, QListWidget#wpGrid::item:selected { background: $hover;
                              color: $text; }
QTableWidget::item { padding: 11px 8px; min-height: 26px; }
QTableWidget::item:selected { background: $sel_row; color: $text; }
QTableWidget::item:hover { background: $hover; }
QHeaderView::section { background: $header; border: none;
                       border-bottom: 2px solid $border; padding: 10px 8px;
                       font-weight: 600; color: $text; }
QHeaderView { background: $header; }
QGroupBox { border: 1px solid $card_border; border-radius: 12px; margin-top: 12px;
            padding-top: 10px; background: $card; font-weight: 600; color: $text; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px;
                   color: $p1; }
QStatusBar { background: transparent; color: $muted; }
/* 滚动条：细圆角滑块，替代 Windows 原生大滚动条（丑的重要来源） */
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar:vertical:hover { width: 12px; }
QScrollBar::handle:vertical { background: $scrollbar; border-radius: 4px;
                              min-height: 30px; }
QScrollBar::handle:vertical:hover { background: $muted; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar:horizontal:hover { height: 12px; }
QScrollBar::handle:horizontal { background: $scrollbar; border-radius: 4px;
                                min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: $muted; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
/* 悬浮提示 */
QToolTip { background: $card; color: $text; border: 1px solid $border;
           border-radius: 8px; padding: 5px 10px; font-size: 12px; }
/* 右键/下拉菜单 */
QMenu { background: $card; border: 1px solid $border; border-radius: 12px;
        padding: 6px; }
QMenu::item { padding: 7px 20px; border-radius: 7px; color: $text; }
QMenu::item:selected { background: $hover; }
QMenu::item:disabled { color: $muted; }
QMenu::separator { height: 1px; background: $border; margin: 5px 8px; }
/* 复选/单选指示器：胶囊圆角，选中渐变填充 */
QCheckBox::indicator, QRadioButton::indicator { width: 18px; height: 18px;
    border: 1px solid $border; border-radius: 5px; background: $input_bg; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover { border-color: $p1; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 $p1, stop:1 $p2);
    border: none; }
QRadioButton::indicator { border-radius: 9px; }
QRadioButton::indicator:checked { border-radius: 9px; }
""")

def _lighten(hex_color: str, amt: float) -> str:
    """向白色方向提亮 #RRGGBB，amt∈[0,1]，用于 hover 高亮主色。"""
    h = hex_color.lstrip("#")
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return hex_color
    r = min(255, int(r + (255 - r) * amt))
    g = min(255, int(g + (255 - g) * amt))
    b = min(255, int(b + (255 - b) * amt))
    return f"#{r:02X}{g:02X}{b:02X}"


def _parse_color(spec: str):
    """#RRGGBB 或 rgba(r,g,b,a) → QColor（缩略图绘制用）。"""
    from PySide6.QtGui import QColor
    s = spec.strip()
    if s.startswith("rgba"):
        m = re.match(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)", s)
        if m:
            r, g, b, a = (int(m.group(1)), int(m.group(2)),
                          int(m.group(3)), float(m.group(4)))
            return QColor(r, g, b, int(a * 255))
    h = s.lstrip("#")
    try:
        return QColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return QColor(91, 140, 255)


def render_logo(size=44):
    """渐变圆底品牌 logo（蓝紫渐变 + 白色「漓」字），固定品牌色。"""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPixmap
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    grad = QLinearGradient(QPointF(0, 0), QPointF(size, size))
    grad.setColorAt(0, QColor("#5B8CFF"))
    grad.setColorAt(1, QColor("#8B5CF6"))
    p.setBrush(QBrush(grad))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QPointF(size / 2, size / 2), size / 2, size / 2)
    f = QFont("Microsoft YaHei UI", int(size * 0.42))
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#FFFFFF"))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "漓")
    p.end()
    return pm


def render_svg_icon(name, color="#94A3B8", size=64):
    """渲染 Lucide SVG 图标为单色 pixmap（空状态插画等用）。

    与 main_window._load_nav_icon 同理：Lucide 用 currentColor 描边，
    QSvgRenderer 以 QPainter pen 色作为 currentColor。
    """
    from PySide6.QtCore import QByteArray
    from PySide6.QtSvg import QSvgRenderer
    from ._icons import SVG_ICONS
    svg = SVG_ICONS.get(name)
    if not svg:
        return QPixmap(size, size)
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor(color))
    QSvgRenderer(QByteArray(svg.encode("utf-8"))).render(p)
    p.end()
    return pm


def render_theme_thumb(key: str, width=120, height=76):
    """渲染主题缩略图（示意卡）：背景 + 导航条 + 选中项 + 卡片 + 主按钮。

    供设置页「点击缩略图选主题」使用；运行时手绘，不依赖外部资源。
    """
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QBrush, QLinearGradient, QPainter, QPixmap
    t = THEMES.get(key, THEMES["blue"])
    pm = QPixmap(width, height)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    # 背景
    p.fillRect(QRectF(0, 0, width, height), _parse_color(t["bg"]))
    # 左侧导航条
    p.fillRect(QRectF(0, 0, width * 0.22, height), _parse_color(t["frame"]))
    # 导航选中项（渐变块）
    grad = QLinearGradient(QPointF(0, 0), QPointF(width * 0.22, 0))
    grad.setColorAt(0, _parse_color(t["p1"]))
    grad.setColorAt(1, _parse_color(t["p2"]))
    p.setBrush(QBrush(grad))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(width * 0.03, height * 0.13, width * 0.16, height * 0.14), 2, 2)
    # 标题条（p1 色）
    p.setPen(_parse_color(t["p1"]))
    p.drawRect(int(width * 0.30), int(height * 0.07), int(width * 0.28), 3)
    # 内容卡片
    p.setBrush(_parse_color(t["card"]))
    p.setPen(_parse_color(t["border"]))
    p.drawRoundedRect(QRectF(width * 0.30, height * 0.20, width * 0.62, height * 0.30), 4, 4)
    p.setPen(_parse_color(t["muted"]))
    p.drawLine(int(width * 0.34), int(height * 0.28), int(width * 0.78), int(height * 0.28))
    p.drawLine(int(width * 0.34), int(height * 0.35), int(width * 0.66), int(height * 0.35))
    # 主按钮（p1→p2 渐变）
    bgrad = QLinearGradient(QPointF(width * 0.30, 0), QPointF(width * 0.92, 0))
    bgrad.setColorAt(0, _parse_color(t["p1"]))
    bgrad.setColorAt(1, _parse_color(t["p2"]))
    p.setBrush(QBrush(bgrad))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(width * 0.30, height * 0.58, width * 0.40, height * 0.15), 3, 3)
    p.end()
    return pm


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    """#RRGGBB → rgba(r, g, b, alpha)，用于 QSS 渐变淡光晕。"""
    h = hex_color.lstrip("#")
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return f"rgba(91, 140, 255, {alpha})"
    return f"rgba({r}, {g}, {b}, {alpha})"


# ---------- 内置壁纸库 ----------

_WALLPAPER_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
# 默认壁纸文件名关键词（匹配「壁纸」目录里的默认款）——
# 用户指定：启动默认用【哲风壁纸】二次元角色-动漫.png
_DEFAULT_WALLPAPER_KEY = "二次元角色-动漫"


def wallpapers_dir() -> str:
    """内置壁纸目录：项目根（app_base_dir 的父目录，即仓库根）下的「壁纸」文件夹。

    源码模式 app_base_dir = xiaoli_desktop → 父目录 = 仓库根 D:\\AI\\小漓；
    打包模式 app_base_dir = dist\\小漓，父目录与 exe 同级的「壁纸」也能被扫到。
    同时兼容 app_base_dir 自身下的 壁纸/wallpapers。
    """
    base = app_base_dir()
    for p in (base, os.path.dirname(base)):
        for cand in (os.path.join(p, "壁纸"), os.path.join(p, "wallpapers")):
            if os.path.isdir(cand):
                return cand
    return os.path.join(base, "壁纸")


def list_wallpapers():
    """扫描内置壁纸目录，返回 [(绝对路径, 文件名)] 按文件名排序。目录不存在返回 []。"""
    d = wallpapers_dir()
    if not os.path.isdir(d):
        return []
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    for f in names:
        if f.lower().endswith(_WALLPAPER_EXTS):
            p = os.path.join(d, f)
            if os.path.isfile(p):
                out.append((p, f))
    return out


def default_wallpaper_path() -> str:
    """默认壁纸：文件名含「二次元角色-动漫」的那张；找不到回退第一张；目录空返回空串。"""
    items = list_wallpapers()
    for p, f in items:
        if _DEFAULT_WALLPAPER_KEY in f:
            return p
    return items[0][0] if items else ""


def wallpaper_short_name(fname: str) -> str:
    """壁纸显示名：去「【哲风壁纸】」前缀 + 去扩展名。"""
    for pre in ("【哲风壁纸】", "哲风壁纸-"):
        if fname.startswith(pre):
            fname = fname[len(pre):]
    base = os.path.splitext(fname)[0]
    return base or fname


def render_wallpaper_thumb(path: str, width=120, height=76):
    """壁纸缩略图：QImageReader 按目标尺寸解码（避免全尺寸加载大图），
    失败回退主题色块。"""
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QImageReader, QPixmap
    try:
        r = QImageReader(path)
        r.setScaledSize(QSize(width, height))
        img = r.read()
        if not img.isNull():
            pm = QPixmap.fromImage(img)
            if not pm.isNull():
                return pm
    except Exception:
        pass
    return render_theme_thumb("blue", width, height)


class ThumbDelegate(QStyledItemDelegate):
    """主题/壁纸缩略图网格自绘 delegate：hover 放大 + 选中态主题色描边 + 对勾。

    原 QSS 选中态仅有 2px 当前主题描边（与缩略图自身配色无关，视觉弱）、
    无勾选标记、hover 无缩放。本 delegate：
      - hover/选中：缩略图轻微放大（≈1.05，基准 114×72 → 120×76，不溢出网格）
      - 选中：2px 圆角主题色描边 + 右上角主题色圆底白色对勾
      - 描边色：per_item_color=True（主题网格）取该项主题自身 p1；
        否则（壁纸网格）用当前主题 p1（accent 由 set_accent 随主题切换更新）
    """
    _BASE_W, _BASE_H = 114, 72
    _ZOOM_W, _ZOOM_H = 120, 76
    _RADIUS = 8

    def __init__(self, parent=None, per_item_color=False, accent="#5B8CFF"):
        super().__init__(parent)
        self.per_item_color = per_item_color
        self.accent = QColor(accent)

    def set_accent(self, color):
        """壁纸网格：主题切换后更新描边/对勾主色并重绘。"""
        self.accent = QColor(color)
        view = self.parent()
        if view is not None:
            view.viewport().update()

    def _accent_for(self, index):
        if self.per_item_color:
            key = index.data(Qt.ItemDataRole.UserRole)
            t = THEMES.get(key)
            if t:
                return _parse_color(t.get("p1", "#5B8CFF"))
        return self.accent

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if opt.widget is not None:
            style = opt.widget.style()
        else:
            style = QApplication.style()
        style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem,
                            opt, painter, opt.widget)
        r = opt.rect
        hover = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        # 布局：item 上部 76px 画缩略图，下部 16px 画文字（12px 字号行高 ~14px，
        # 必须给足 16px 区域——此前 12px 高度 + AlignTop 导致文字下半被裁）
        if not opt.icon.isNull():
            w = self._ZOOM_W if (hover or selected) else self._BASE_W
            h = self._ZOOM_H if (hover or selected) else self._BASE_H
            icon_rect = QRectF(r.center().x() - w / 2,
                               r.top() + (self._ZOOM_H - h) / 2, w, h)
            pm = opt.icon.pixmap(int(w), int(h))
            painter.drawPixmap(icon_rect.toRect(), pm)
            if selected:
                accent = self._accent_for(index)
                painter.setPen(QPen(accent, 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRoundedRect(icon_rect.adjusted(1, 1, -1, -1),
                                        self._RADIUS, self._RADIUS)
                self._draw_check(painter, icon_rect, accent)
        text = opt.text
        if text:
            painter.setFont(opt.font)
            painter.setPen(opt.palette.color(
                QPalette.ColorRole.Text if not selected
                else QPalette.ColorRole.HighlightedText))
            tr = QRectF(r.left(), r.top() + self._ZOOM_H,
                        r.width(), r.height() - self._ZOOM_H)
            painter.drawText(tr.toRect(),
                             Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                             text)
        painter.restore()

    def _draw_check(self, painter, icon_rect, accent):
        """右上角：主题色小圆底 + 白色对勾（✓）。"""
        d = 18.0
        cx = icon_rect.right() - 4
        cy = icon_rect.top() + 4
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        painter.drawEllipse(QPointF(cx - d / 2, cy - d / 2), d / 2, d / 2)
        painter.setPen(QPen(QColor("#FFFFFF"), 2.2, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath(QPointF(cx - d / 2 + 4.0, cy + 0.5))
        path.lineTo(QPointF(cx - d / 2 + 7.6, cy + 3.8))
        path.lineTo(QPointF(cx + d / 2 - 3.0, cy - 3.4))
        painter.drawPath(path)


# 字号三档：小/中/大（设置页 font_scale 选择；值必须带 px 单位——
# Qt QSS 不解析裸数字字号，缺单位会导致字号调节整体失效）
_FONT_SCALES = {
    "small":  {"fs_base": "13px", "fs_nav": "17px", "fs_title": "28px", "fs_sub": "13px"},
    "medium": {"fs_base": "15px", "fs_nav": "21px", "fs_title": "34px", "fs_sub": "15px"},
    "large":  {"fs_base": "17px", "fs_nav": "26px", "fs_title": "42px", "fs_sub": "17px"},
}


def _with_alpha(card_spec: str, opacity: float) -> str:
    """把卡片色 rgba/hex 统一为指定不透明度 rgba(r,g,b,opacity)。"""
    m = re.match(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*[\d.]+\)", card_spec.strip())
    if m:
        return f"rgba({m.group(1)}, {m.group(2)}, {m.group(3)}, {opacity})"
    h = card_spec.strip().lstrip("#")
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return f"rgba(255, 255, 255, {opacity})"
    return f"rgba({r}, {g}, {b}, {opacity})"


def system_theme_key():
    """当前系统深浅色对应的内置主题：深色 → tokyonight，浅色 → blue。"""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is not None and app.styleHints().colorScheme() == Qt.ColorScheme.Dark:
        return "tokyonight"
    return "blue"


def resolve_theme(theme, follow_system=False):
    """跟随系统开关生效时，忽略手动主题选择，返回系统深浅对应主题。"""
    if follow_system:
        return system_theme_key()
    return theme


def build_qss(theme: str = "blue", wallpaper: str = "", card_opacity=None,
              panel_opacity=None, font_scale: str = "medium") -> str:
    """按主题名 + 可选壁纸路径生成 QSS。

    背景（渐变/粒子/壁纸）由 ParticleBackdrop 运行时绘制，QSS 只负责控件；
    这里为 QMainWindow 提供纯 bg 兜底。动态派生 hover 提亮主色与导航悬停色。
    card_opacity：卡片不透明度 0~1.0（None = 用主题原始 card 值），
    panel_opacity：面板/输入区不透明度（日志区、表格、输入框等 input_bg
    底色），与卡片透明度独立可调——两个滑块分别控制「毛玻璃卡片」与
    「面板大色块」的透出程度，0 = 完全透明。
    font_scale：small/medium/large 字号档位。
    """
    t = dict(_DEFAULTS)
    t.update(THEMES.get(theme, THEMES["blue"]))
    if card_opacity is not None:
        t["card"] = _with_alpha(t.get("card", _DEFAULTS["card"]),
                                max(0.0, min(1.0, card_opacity)))
    if panel_opacity is not None:
        t["input_bg"] = _with_alpha(t.get("input_bg", _DEFAULTS["input_bg"]),
                                    max(0.0, min(1.0, panel_opacity)))
    # hover 主色：主题可自定义 p1_hi/p2_hi，缺省自动提亮 16%
    t["p1_hi"] = _lighten(t.get("p1_hi", t["p1"]), 0.16)
    t["p2_hi"] = _lighten(t.get("p2_hi", t["p2"]), 0.16)
    # 导航 hover：主色三档透明渐变（浅→中→浅），原单色 0.12 透明度深色下几乎不可见
    t["nav_hover0"] = _hex_to_rgba(t["p1"], 0.05)
    t["nav_hover1"] = _hex_to_rgba(t["p1"], 0.15)
    t["nav_hover2"] = _hex_to_rgba(t["p2"], 0.06)
    # 表格选中行：主色 18% 透明（比 hover 更浓，可辨识选中态）
    t["sel_row"] = _hex_to_rgba(t["p1"], 0.18)
    # 卡片边框：深色主题下 $border 与背景对比弱（卡片轮廓不清），
    # 自动向白色提亮 25% 强化轮廓；浅色主题保留原 border。
    if t.get("card_border"):
        pass  # 主题显式定义则用之
    elif t.get("group", "浅色") == "深色":
        t["card_border"] = _lighten(t["border"], 0.25)
    else:
        t["card_border"] = t["border"]
    # 提示文字（tip）：text 的 80% 透明度，比 muted 更清晰可读
    t["tip"] = _hex_to_rgba(t["text"], 0.8)
    # 字号档位
    t.update(_FONT_SCALES.get(font_scale, _FONT_SCALES["medium"]))
    bg_rule = f"background: {t['bg']};"
    return _QSS_TEMPLATE.substitute(**t, bg_rule=bg_rule)


# 向后兼容：默认主题 QSS（旧代码/测试直接引用 APP_QSS）
APP_QSS = build_qss()


def app_base_dir():
    """配置/卡片存储的稳定基目录。

    PyInstaller 打包（frozen）→ exe 所在目录（dist\\小漓，与现有 config.json 同处）；
    源码运行 → 项目根（xiaoli_desktop）。不随 cwd 漂移——cwd 依赖会让
    「保存的模型配置下次打开丢失」（双击 exe / 固定图标 / 快捷方式启动 cwd 各异）。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_app_font():
    """加载内置思源黑体（Noto Sans SC，fonts/ 目录）到应用字体表。

    用 QFontDatabase.addApplicationFont 从文件加载（不依赖系统字体注册，
    DirectWrite 扫描用户字体目录有滞后；也保证打包后随包可用）。
    字体缺失时静默回退系统字体（微软雅黑）。返回是否加载成功。
    """
    from PySide6.QtGui import QFontDatabase
    base = app_base_dir()
    for d in (os.path.join(base, "fonts"), os.path.join(os.path.dirname(base), "fonts")):
        if not os.path.isdir(d):
            continue
        added = False
        for fname in ("NotoSansSC-Regular.otf", "NotoSansSC-Bold.otf"):
            fp = os.path.join(d, fname)
            if os.path.isfile(fp):
                QFontDatabase.addApplicationFont(fp)
                added = True
        if added:
            return True
    return False


class AppContext:
    """页面与引擎共享的应用上下文。"""

    def __init__(self):
        base = app_base_dir()
        self.cfg_path = os.path.join(base, "config.json")
        self.cards_dir = os.path.join(base, "cards")
        self.cfg = None          # 最新 config（含投影字段）
        self.engine = None       # EngineThread
        self.bus = None          # EngineBus
        self.win = None          # MainWindow（设置页切主题时同步背景层）

    def providers(self):
        return (self.cfg or {}).get("providers", [])

    def active_card_id(self):
        return (self.cfg or {}).get("active_card_id", "")

    def theme(self):
        return (self.cfg or {}).get("theme", "blue")

    def wallpaper(self):
        return (self.cfg or {}).get("wallpaper_path", "")
