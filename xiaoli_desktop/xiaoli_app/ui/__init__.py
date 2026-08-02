# -*- coding: utf-8 -*-
"""PySide6 控制面板：托盘（tray.py）+ 主窗口（main_window.py）+ 页面（pages.py）。

AppContext：引擎/总线/配置的轻量容器，页面与入口共享。
"""

# 全局视觉 tokens（QSS）：渐变主色蓝紫、圆角 10px、选中/悬停反馈、8px 间距栅格
APP_QSS = """
* { font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif; }
/* 深色系统主题下 palette.WindowText 为白色——必须显式给文字控件设色，否则白字隐形 */
QLabel, QListWidget, QTableWidget, QLineEdit, QPlainTextEdit, QTextEdit,
QComboBox, QSpinBox, QDoubleSpinBox, QProgressBar { color: #374151; }
QMainWindow, QWidget { background: #EEF2F9; }
QTabWidget::pane { border: none; background: #EEF2F9; }
QTabBar::tab { background: transparent; padding: 9px 20px; margin-right: 6px;
               border-radius: 9px; color: #6B7280; font-size: 13px; font-weight: 500; }
QTabBar::tab:hover { background: #E3EAF7; color: #1F2937; }
QTabBar::tab:selected { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                       stop:0 #5B8CFF, stop:1 #4A7CF7);
                       color: #FFFFFF; font-weight: 600; }
QLabel#title { font-size: 26px; font-weight: 700;
               color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
               stop:0 #4A7CF7, stop:1 #7C5BFF); }
QLabel#subtitle { font-size: 13px; color: #9CA3AF; }
QLabel#stateLabel { font-size: 14px; color: #6B7280; }
QFrame#card { background: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 12px; }
QPushButton#btnMain { color: white; border: none; border-radius: 10px;
                      font-size: 16px; font-weight: 600; padding: 12px 36px;
                      background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 #5B8CFF, stop:1 #4A7CF7); }
QPushButton#btnMain[tone="primary"]:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 #6B9AFF, stop:1 #5B8CFF); }
QPushButton#btnMain[tone="primary"]:pressed { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 #4A7CF7, stop:1 #3B6BE0); }
QPushButton#btnMain[tone="primary"]:disabled { background: #A5B8F5; }
QPushButton#btnMain[tone="warn"] { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 #FBBF24, stop:1 #F59E0B); }
QPushButton#btnMain[tone="warn"]:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 #FCD34D, stop:1 #FBBF24); }
QPushButton#btnMain[tone="warn"]:pressed { background: #D97706; }
QProgressBar { border: none; border-radius: 6px; background: #E5E7EB; height: 10px; }
QProgressBar::chunk { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                      stop:0 #5B8CFF, stop:1 #4A7CF7); border-radius: 6px; }
QPushButton { background: #FFFFFF; border: 1px solid #D1D5DB; border-radius: 8px;
              padding: 6px 16px; color: #374151; font-size: 13px; }
QPushButton:hover { background: #F3F4F6; border-color: #A5B8F5; color: #2B4EC2; }
QPushButton:pressed { background: #E8EEFB; }
QPushButton:disabled { color: #9CA3AF; background: #F9FAFB; }
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #FFFFFF; border: 1px solid #D1D5DB; border-radius: 6px;
    padding: 4px 8px; }
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus { border: 1px solid #4A7CF7; background: #FBFDFF; }
QListWidget, QTableWidget { background: #FFFFFF; border: 1px solid #E5E7EB;
                            border-radius: 10px; }
QListWidget::item { padding: 6px 8px; border-radius: 6px; margin: 1px 3px; }
QListWidget::item:hover { background: #F3F6FE; }
QListWidget::item:selected { background: #EAF0FE; color: #1F2937; }
QTableWidget::item:selected { background: #EAF0FE; color: #1F2937; }
QTableWidget::item:hover { background: #F5F8FF; }
QHeaderView::section { background: #F4F7FC; border: none;
                       border-bottom: 2px solid #E5E7EB; padding: 7px;
                       font-weight: 600; color: #4B5563; }
QGroupBox { border: 1px solid #E5E7EB; border-radius: 10px; margin-top: 10px;
            padding-top: 8px; background: #FBFCFE; font-weight: 600; color: #374151; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px;
                   color: #4A7CF7; }
QStatusBar { background: #EEF2F9; color: #6B7280; }
"""


class AppContext:
    """页面与引擎共享的应用上下文。"""

    def __init__(self):
        self.cfg_path = "config.json"
        self.cards_dir = "cards"
        self.cfg = None          # 最新 config（含投影字段）
        self.engine = None       # EngineThread
        self.bus = None          # EngineBus

    def providers(self):
        return (self.cfg or {}).get("providers", [])

    def active_card_id(self):
        return (self.cfg or {}).get("active_card_id", "")
