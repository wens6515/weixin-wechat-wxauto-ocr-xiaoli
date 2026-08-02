# -*- coding: utf-8 -*-
"""PySide6 控制面板：托盘（tray.py）+ 主窗口（main_window.py）+ 页面（pages.py）。

AppContext：引擎/总线/配置的轻量容器，页面与入口共享。
"""

# 全局视觉 tokens（QSS）：主色蓝、成功绿、警告橙、错误红；圆角 10px；8px 间距栅格
APP_QSS = """
* { font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif; }
QMainWindow, QWidget { background: #F5F7FA; }
QTabWidget::pane { border: none; background: #F5F7FA; }
QTabBar::tab { background: transparent; padding: 8px 18px; margin-right: 4px;
               border-radius: 8px; color: #6B7280; font-size: 13px; }
QTabBar::tab:selected { background: #FFFFFF; color: #1F2937; font-weight: 600; }
QLabel#title { font-size: 26px; font-weight: 700; color: #1F2937; }
QLabel#subtitle { font-size: 13px; color: #9CA3AF; }
QLabel#stateLabel { font-size: 14px; color: #6B7280; }
QFrame#card { background: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 10px; }
QPushButton#btnMain { color: white; border: none; border-radius: 10px;
                      font-size: 16px; font-weight: 600; padding: 12px 36px; }
QPushButton#btnMain[tone="primary"] { background: #4A7CF7; }
QPushButton#btnMain[tone="primary"]:hover { background: #3B6BE0; }
QPushButton#btnMain[tone="primary"]:disabled { background: #A5B8F5; }
QPushButton#btnMain[tone="warn"] { background: #F59E0B; }
QPushButton#btnMain[tone="warn"]:hover { background: #D97706; }
QProgressBar { border: none; border-radius: 6px; background: #E5E7EB; height: 10px; }
QProgressBar::chunk { background: #4A7CF7; border-radius: 6px; }
QPushButton { background: #FFFFFF; border: 1px solid #D1D5DB; border-radius: 8px;
              padding: 6px 16px; color: #374151; font-size: 13px; }
QPushButton:hover { background: #F3F4F6; }
QPushButton:disabled { color: #9CA3AF; background: #F9FAFB; }
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #FFFFFF; border: 1px solid #D1D5DB; border-radius: 6px;
    padding: 4px 8px; }
QListWidget, QTableWidget { background: #FFFFFF; border: 1px solid #E5E7EB;
                            border-radius: 8px; }
QListWidget::item { padding: 6px 8px; }
QListWidget::item:selected { background: #EAF0FE; color: #1F2937; }
QHeaderView::section { background: #F9FAFB; border: none; border-bottom: 1px solid #E5E7EB;
                       padding: 6px; font-weight: 600; color: #374151; }
QStatusBar { background: #F5F7FA; color: #6B7280; }
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
