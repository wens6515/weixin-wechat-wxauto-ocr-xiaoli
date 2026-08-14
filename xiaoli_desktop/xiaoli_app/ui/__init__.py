# -*- coding: utf-8 -*-
"""PySide6 控制面板：托盘（tray.py）+ 主窗口（main_window.py）+ 页面（pages.py）。

AppContext：引擎/总线/配置的轻量容器，页面与入口共享。
主题系统：THEMES 定义 5 套配色，build_qss 按主题 + 可选壁纸生成 QSS；
毛玻璃用半透明卡片（rgba）透过壁纸/背景实现（Qt QSS 无真高斯模糊）。
"""
import os
import sys
from string import Template

# 5 套主题配色。p1/p2 为渐变主色两端；bg 背景；card 卡片（半透明=毛玻璃）；
# text/muted 文字；border/hover/input_bg 边框/悬停/输入框底色。
THEMES = {
    "blue":    {"label": "蓝紫（默认）", "bg": "#F4F7FC", "p1": "#5B8CFF", "p2": "#8B5CF6",
                "card": "rgba(255,255,255,0.88)", "text": "#374151", "muted": "#94A3B8",
                "border": "#E8EDF5", "hover": "#EAF0FE", "input_bg": "#FFFFFF",
                "frame": "#FBFCFE", "header": "#F4F7FC"},
    "dark":    {"label": "暗夜深色", "bg": "#16161F", "p1": "#6366F1", "p2": "#8B5CF6",
                "card": "rgba(34,34,50,0.82)", "text": "#E2E8F0", "muted": "#7C8AA0",
                "border": "#2D2D3F", "hover": "#3B3B5C", "input_bg": "#22222E",
                "frame": "#1F1F2E", "header": "#1A1A28"},
    "emerald": {"label": "翡翠绿", "bg": "#F0FDF4", "p1": "#10B981", "p2": "#059669",
                "card": "rgba(255,255,255,0.88)", "text": "#374151", "muted": "#94A3B8",
                "border": "#D1FAE5", "hover": "#D1FAE5", "input_bg": "#FFFFFF",
                "frame": "#F0FDF4", "header": "#ECFDF5"},
    "sunset":  {"label": "落日橙", "bg": "#FFF7ED", "p1": "#F59E0B", "p2": "#EF4444",
                "card": "rgba(255,255,255,0.88)", "text": "#374151", "muted": "#94A3B8",
                "border": "#FED7AA", "hover": "#FFEDD5", "input_bg": "#FFFFFF",
                "frame": "#FFF7ED", "header": "#FFFBEB"},
    "rose":    {"label": "樱花粉", "bg": "#FDF2F8", "p1": "#EC4899", "p2": "#8B5CF6",
                "card": "rgba(255,255,255,0.88)", "text": "#374151", "muted": "#94A3B8",
                "border": "#FBCFE8", "hover": "#FCE7F3", "input_bg": "#FFFFFF",
                "frame": "#FDF2F8", "header": "#FDF2F8"},
}

_QSS_TEMPLATE = Template("""
* { font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif; }
QLabel, QListWidget, QTableWidget, QLineEdit, QPlainTextEdit, QTextEdit,
QComboBox, QSpinBox, QDoubleSpinBox, QProgressBar, QCheckBox { color: $text; }
QMainWindow, QWidget { $bg_rule }
QTabWidget::pane { border: none; background: transparent; }
QTabBar::tab { background: transparent; padding: 10px 22px; margin-right: 8px;
               border-radius: 10px; color: $muted; font-size: 13px; font-weight: 500; }
QTabBar::tab:hover { background: $hover; color: $text; }
QTabBar::tab:selected { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                       stop:0 $p1, stop:1 $p2);
                       color: #FFFFFF; font-weight: 600; }
QLabel#title { font-size: 26px; font-weight: 700; color: $p1; }
QLabel#subtitle { font-size: 13px; color: $muted; }
QLabel#stateLabel { font-size: 14px; color: $muted; }
QFrame#card { background: $card; border: 1px solid $border; border-radius: 14px; }
QPushButton#btnMain { color: white; border: none; border-radius: 12px;
                      font-size: 16px; font-weight: 600; padding: 12px 36px;
                      background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 $p1, stop:1 $p2); }
QPushButton#btnMain[tone="primary"]:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 $p1, stop:1 $p2); }
QPushButton#btnMain[tone="primary"]:pressed { background: $p2; }
QPushButton#btnMain[tone="primary"]:disabled { background: #B7C7F5; }
QPushButton#btnMain[tone="warn"] { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 #FBBF24, stop:1 #F59E0B); }
QPushButton#btnMain[tone="warn"]:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 #FCD34D, stop:1 #FBBF24); }
QPushButton#btnMain[tone="warn"]:pressed { background: #D97706; }
QProgressBar { border: none; border-radius: 7px; background: $border; height: 12px; }
QProgressBar::chunk { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 $p1, stop:1 $p2); border-radius: 7px; }
QPushButton { background: $input_bg; border: 1px solid $border; border-radius: 9px;
              padding: 8px 20px; min-height: 32px; color: $text; font-size: 13px; font-weight: 500; }
QPushButton:hover { background: $hover; border-color: $p1; color: $text; }
QPushButton:pressed { background: $hover; }
QPushButton:disabled { color: $muted; background: $frame; border-color: $border; }
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: $input_bg; border: 1px solid $border; border-radius: 8px;
    padding: 5px 10px; }
/* 下拉弹层（QComboBox 弹出的列表）：深色系统主题下 palette 文字是白色，
   必须显式设深灰文字 + 白底，否则浅色界面上下拉项白字看不清 */
QComboBox QAbstractItemView {
    color: $text; background: $input_bg; border: 1px solid $border;
    border-radius: 8px; selection-background-color: $hover; selection-color: $text; }
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus { border: 1px solid $p1; background: $input_bg; }
QListWidget, QTableWidget { background: $input_bg; border: 1px solid $border;
                            border-radius: 12px; }
QListWidget::item { padding: 7px 10px; border-radius: 8px; margin: 2px 4px; }
QListWidget::item:hover { background: $hover; }
QListWidget::item:selected { background: $hover; color: $text; }
/* 左侧导航栏：窄侧边 + 竖排导航项，选中态渐变背景 + 白字白图标 */
QListWidget#navList { background: $frame; border: none;
                      border-right: 1px solid $border; padding: 14px 10px; outline: 0; }
QListWidget#navList::item { padding: 12px 14px; border-radius: 10px;
                            margin: 3px 6px; color: $muted; font-size: 14px;
                            font-weight: 500; }
QListWidget#navList::item:hover { background: $hover; color: $text; }
QListWidget#navList::item:selected {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 $p1, stop:1 $p2);
    color: #FFFFFF; font-weight: 600; }
QTableWidget::item:selected { background: $hover; color: $text; }
QTableWidget::item:hover { background: $hover; }
QHeaderView::section { background: $header; border: none;
                       border-bottom: 2px solid $border; padding: 9px;
                       font-weight: 600; color: $text; }
QGroupBox { border: 1px solid $border; border-radius: 12px; margin-top: 12px;
            padding-top: 10px; background: $frame; font-weight: 600; color: $text; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px;
                   color: $p1; }
QStatusBar { background: transparent; color: $muted; }
""")


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    """#RRGGBB → rgba(r, g, b, alpha)，用于 QSS 渐变淡光晕。"""
    h = hex_color.lstrip("#")
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return f"rgba(91, 140, 255, {alpha})"
    return f"rgba({r}, {g}, {b}, {alpha})"


def build_qss(theme: str = "blue", wallpaper: str = "") -> str:
    """按主题名 + 可选壁纸路径生成 QSS。壁纸为空时用主题色径向光晕背景。"""
    t = THEMES.get(theme, THEMES["blue"])
    if wallpaper and os.path.isfile(wallpaper):
        wp = wallpaper.replace("\\", "/")
        bg_rule = f'background-image: url("{wp}"); background-position: center;'
    else:
        # 顶部主题色淡光晕 → 底部背景色，比纯色更有层次
        soft = _hex_to_rgba(t["p1"], 0.16)
        bg_rule = (f"background: qradialgradient(cx:0.5, cy:0, radius:1.3, "
                   f"fx:0.5, fy:0, stop:0 {soft}, stop:1 {t['bg']});")
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


class AppContext:
    """页面与引擎共享的应用上下文。"""

    def __init__(self):
        base = app_base_dir()
        self.cfg_path = os.path.join(base, "config.json")
        self.cards_dir = os.path.join(base, "cards")
        self.cfg = None          # 最新 config（含投影字段）
        self.engine = None       # EngineThread
        self.bus = None          # EngineBus

    def providers(self):
        return (self.cfg or {}).get("providers", [])

    def active_card_id(self):
        return (self.cfg or {}).get("active_card_id", "")

    def theme(self):
        return (self.cfg or {}).get("theme", "blue")

    def wallpaper(self):
        return (self.cfg or {}).get("wallpaper_path", "")
