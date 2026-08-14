# -*- coding: utf-8 -*-
"""PySide6 控制面板：托盘（tray.py）+ 主窗口（main_window.py）+ 页面（pages.py）。

AppContext：引擎/总线/配置的轻量容器，页面与入口共享。
"""
import os
import sys

# 全局视觉 tokens（QSS）：渐变主色蓝紫、圆角 10px、选中/悬停反馈、8px 间距栅格
APP_QSS = """
* { font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif; }
/* 深色系统主题下 palette.WindowText 为白色——必须显式给文字控件设色，否则白字隐形 */
QLabel, QListWidget, QTableWidget, QLineEdit, QPlainTextEdit, QTextEdit,
QComboBox, QSpinBox, QDoubleSpinBox, QProgressBar, QCheckBox { color: #374151; }
QMainWindow, QWidget { background: #F4F7FC; }
QTabWidget::pane { border: none; background: #F4F7FC; }
QTabBar::tab { background: transparent; padding: 10px 22px; margin-right: 8px;
               border-radius: 10px; color: #64748B; font-size: 13px; font-weight: 500; }
QTabBar::tab:hover { background: #E6EDFB; color: #1E293B; }
QTabBar::tab:selected { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                       stop:0 #5B8CFF, stop:1 #8B5CF6);
                       color: #FFFFFF; font-weight: 600; }
QLabel#title { font-size: 26px; font-weight: 700;
               color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
               stop:0 #4A7CF7, stop:1 #7C5BFF); }
QLabel#subtitle { font-size: 13px; color: #9CA3AF; }
QLabel#stateLabel { font-size: 14px; color: #6B7280; }
QFrame#card { background: #FFFFFF; border: 1px solid #E8EDF5; border-radius: 14px; }
QPushButton#btnMain { color: white; border: none; border-radius: 12px;
                      font-size: 16px; font-weight: 600; padding: 12px 36px;
                      background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 #5B8CFF, stop:1 #8B5CF6); }
QPushButton#btnMain[tone="primary"]:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 #6B9AFF, stop:1 #9B6BFF); }
QPushButton#btnMain[tone="primary"]:pressed { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 #4A7CF7, stop:1 #7C4BE0); }
QPushButton#btnMain[tone="primary"]:disabled { background: #B7C7F5; }
QPushButton#btnMain[tone="warn"] { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 #FBBF24, stop:1 #F59E0B); }
QPushButton#btnMain[tone="warn"]:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 #FCD34D, stop:1 #FBBF24); }
QPushButton#btnMain[tone="warn"]:pressed { background: #D97706; }
QProgressBar { border: none; border-radius: 7px; background: #E8EDF5; height: 12px; }
QProgressBar::chunk { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 #5B8CFF, stop:1 #8B5CF6); border-radius: 7px; }
QPushButton { background: #FFFFFF; border: 1px solid #D6DEEA; border-radius: 9px;
              padding: 7px 18px; color: #374151; font-size: 13px; font-weight: 500; }
QPushButton:hover { background: #F1F5FC; border-color: #A5B8F5; color: #2B4EC2; }
QPushButton:pressed { background: #E8EEFB; }
QPushButton:disabled { color: #A8B3C2; background: #F8FAFD; border-color: #E5EAF2; }
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #FFFFFF; border: 1px solid #D6DEEA; border-radius: 8px;
    padding: 5px 10px; }
/* 下拉弹层（QComboBox 弹出的列表）：深色系统主题下 palette 文字是白色，
   必须显式设深灰文字 + 白底，否则浅色界面上下拉项白字看不清 */
QComboBox QAbstractItemView {
    color: #374151; background: #FFFFFF; border: 1px solid #D6DEEA;
    border-radius: 8px; selection-background-color: #EAF0FE; selection-color: #1E293B; }
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus { border: 1px solid #4A7CF7; background: #FBFDFF; }
QListWidget, QTableWidget { background: #FFFFFF; border: 1px solid #E8EDF5;
                            border-radius: 12px; }
QListWidget::item { padding: 7px 10px; border-radius: 8px; margin: 2px 4px; }
QListWidget::item:hover { background: #F3F6FE; }
QListWidget::item:selected { background: #EAF0FE; color: #1E293B; }
QTableWidget::item:selected { background: #EAF0FE; color: #1E293B; }
QTableWidget::item:hover { background: #F5F8FF; }
QHeaderView::section { background: #F4F7FC; border: none;
                       border-bottom: 2px solid #E5EAF2; padding: 9px;
                       font-weight: 600; color: #475569; }
QGroupBox { border: 1px solid #E8EDF5; border-radius: 12px; margin-top: 12px;
            padding-top: 10px; background: #FBFCFE; font-weight: 600; color: #374151; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px;
                   color: #4A7CF7; }
QStatusBar { background: #F4F7FC; color: #64748B; }
"""


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
