# -*- coding: utf-8 -*-
"""PySide6 控制面板：托盘（tray.py）+ 主窗口（main_window.py）+ 页面（pages.py）。

AppContext：引擎/总线/配置的轻量容器，页面与入口共享。
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
