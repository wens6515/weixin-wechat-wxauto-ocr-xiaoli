# -*- coding: utf-8 -*-
"""小漓桌面版入口（GUI）：pythonw xiaoli_gui.py（无终端）

启动流程（启动即空闲，不做任何初始化）：
1. config_store 加载/迁移/投影（纯配置读写，不连微信）
2. 构造 EngineThread（不启动线程，等首页「初始化」按钮触发 initialize）
3. 托盘 + 主窗口；QTimer 拉取总线事件刷新界面
"""
import json
import logging
import os
import sys

logger = logging.getLogger("xiaoli")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton)

from xiaoli_app import config_store
from xiaoli_app.engine import EngineBus, EngineThread
from xiaoli_app.ui import AppContext, APP_QSS
from xiaoli_app.ui.main_window import MainWindow
from xiaoli_app.ui.tray import TrayIcon


def default_wechat_files_dir():
    """微信文件接收目录默认值：Documents\\WeChat Files 下含 FileStorage\\File 的目录。

    微信 4.x 路径形如 %USERPROFILE%\\Documents\\xwechat_files\\<wxid>_<hash>\\msg\\file；
    找不到时退回 Documents\\WeChat Files（用户可手动浏览修正）。
    """
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    candidates = []
    for base in (os.path.join(docs, "xwechat_files"), os.path.join(docs, "WeChat Files")):
        if os.path.isdir(base):
            try:
                for sub in os.listdir(base):
                    p = os.path.join(base, sub)
                    if os.path.isdir(p):
                        candidates.append(p)
            except OSError:
                pass
            candidates.append(base)
    if candidates:
        return candidates[0]
    return os.path.join(docs, "WeChat Files")


def needs_first_run(cfg_path, cfg):
    """首次启动判定：config 文件不存在，或任务工作目录/微信文件目录未配置。

    旧版本生成的 config.json 可能已存在但没有 tasks_dir/file_storage_path
    （或为空字符串）——此时任务桥目录与附件识别目录都是空的，必须引导用户
    选择，否则升级后任务功能静默不可用。
    cfg 未提供或字段缺失时回读 config 文件本身判断（文件是事实来源）。
    """
    if not os.path.isfile(cfg_path):
        return True
    if cfg is None or not str(cfg.get("tasks_dir") or "").strip() \
            or not str(cfg.get("file_storage_path") or "").strip():
        # cfg 缺失/字段为空 → 回读文件（可能调用方只传了路径）
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                return not str(disk.get("tasks_dir") or "").strip() or \
                    not str(disk.get("file_storage_path") or "").strip()
        except (OSError, ValueError):
            pass
    return False


class FirstRunDialog(QDialog):
    """首次启动引导：选择任务工作目录、微信文件目录与记忆存储位置。

    已有配置（如 memory_file）预填保留，只让用户补缺失/确认目录。
    """

    def __init__(self, parent=None, cfg=None):
        super().__init__(parent)
        cfg = cfg or {}
        self.setWindowTitle("欢迎使用小漓")
        self.setMinimumWidth(560)
        form = QFormLayout(self)
        tip = QLabel(
            "小漓需要三个文件夹存放工作文件：\n"
            "任务工作目录：小漓与天枢交换任务/成果文件的地方\n"
            "微信文件目录：微信收到的文件（图片/文档）下载位置，用于识别任务附件\n"
            "记忆存储位置：对话记忆文件（memory.json）\n"
            "建议保持默认位置，直接点击「开始使用」即可。")
        tip.setWordWrap(True)
        form.addRow(tip)
        self.ed_tasks = QLineEdit(str(cfg.get("tasks_dir") or "").strip()
                                  or config_store.default_tasks_dir())
        self.ed_files = QLineEdit(str(cfg.get("file_storage_path") or "").strip()
                                  or default_wechat_files_dir())
        self.ed_memory = QLineEdit(str(cfg.get("memory_file") or "").strip()
                                   or config_store.default_memory_file())
        self.ed_tasks.setReadOnly(True)
        self.ed_files.setReadOnly(True)
        self.ed_memory.setReadOnly(True)
        btn_tasks = QPushButton("浏览…")
        btn_tasks.clicked.connect(lambda: self._pick(self.ed_tasks, True))
        btn_files = QPushButton("浏览…")
        btn_files.clicked.connect(lambda: self._pick(self.ed_files, True))
        btn_mem = QPushButton("浏览…")
        btn_mem.clicked.connect(lambda: self._pick(self.ed_memory, False))
        row_t = QHBoxLayout()
        row_t.addWidget(self.ed_tasks, 1)
        row_t.addWidget(btn_tasks)
        row_f = QHBoxLayout()
        row_f.addWidget(self.ed_files, 1)
        row_f.addWidget(btn_files)
        row_m = QHBoxLayout()
        row_m.addWidget(self.ed_memory, 1)
        row_m.addWidget(btn_mem)
        form.addRow("任务工作目录", row_t)
        form.addRow("微信文件目录", row_f)
        form.addRow("记忆存储位置", row_m)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("开始使用")
        bb.button(QDialogButtonBox.Cancel).setText("取消")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _pick(self, edit, is_dir):
        if is_dir:
            p = QFileDialog.getExistingDirectory(self, "选择文件夹", edit.text())
            if p:
                edit.setText(p)
        else:
            p, _f = QFileDialog.getSaveFileName(
                self, "选择记忆文件位置", edit.text(), "JSON (*.json)")
            if p:
                edit.setText(p)

    def result_cfg(self):
        return {
            "tasks_dir": self.ed_tasks.text().strip() or config_store.default_tasks_dir(),
            "file_storage_path": self.ed_files.text().strip() or default_wechat_files_dir(),
            "memory_file": self.ed_memory.text().strip() or config_store.default_memory_file(),
        }


def build_bot_factory(ctx):
    """bot 工厂：子线程内创建 AgentBot（连微信可能阻塞）。"""
    from xiaoli_bot import AgentBot

    def factory():
        return AgentBot(ctx.cfg)

    return factory


def main():
    # 单实例锁：防止双击多开（多实例会并发抢微信窗口/任务冲突）
    from xiaoli_bot import acquire_single_instance
    if not acquire_single_instance("XiaoLiGui_SingleInstance"):
        QMessageBox.warning(None, "小漓", "小漓已在运行（在系统托盘图标处查看）。")
        return

    app = QApplication(sys.argv)
    app.setApplicationName("小漓")
    app.setQuitOnLastWindowClosed(False)  # 关窗不退出（隐藏到托盘）
    app.setStyleSheet(APP_QSS)

    ctx = AppContext()

    # 1. 配置：加载/迁移/投影（不连微信、不启动引擎）
    ctx.cfg = config_store.load_config_store(ctx.cfg_path, ctx.cards_dir)
    # 首次启动（config 文件不存在）或任务目录/微信文件目录未配置 → 引导选择，
    # 避免默认 D:\ 盘缺失崩溃、以及旧 config 升级后任务桥目录为空静默失效
    if needs_first_run(ctx.cfg_path, ctx.cfg):
        dlg = FirstRunDialog(cfg=ctx.cfg)
        dlg.exec()
        ctx.cfg.update(dlg.result_cfg())
        try:
            config_store.save_config(ctx.cfg, ctx.cfg_path)
        except OSError as e:
            QMessageBox.warning(None, "小漓", f"配置保存失败：{e}")
        # 选择完工作文件夹后立即初始化任务桥：生成 README.md 协议文档
        # （天枢 CLI 处理任务时读取；首次生成、已存在不覆盖，幂等）
        try:
            from xiaoli_app import setup as _setup
            _setup.ensure_bridge_readme(str(ctx.cfg.get("tasks_dir") or "").strip())
        except Exception as e:
            logger.warning(f"[首次引导] 生成任务桥 README 失败: {e}")
    if not ctx.cfg.get("providers"):
        QMessageBox.warning(
            None, "小漓",
            "尚未配置 API Provider。\n请到「模型」页添加（如 DeepSeek 官方 API），"
            "并在「角色卡」页确认模型引用。",
        )

    # 2. 引擎（仅构造；线程与 bot 由首页「初始化」按钮触发）
    ctx.bus = EngineBus()
    ctx.engine = EngineThread(build_bot_factory(ctx), bus=ctx.bus)

    # 3. UI：托盘 + 主窗口
    win = MainWindow(ctx)
    tray = TrayIcon(parent=win)
    ctx.tray = tray

    def show_panel():
        win.show()
        win.raise_()
        win.activateWindow()

    tray.show_requested.connect(show_panel)
    tray.toggle_pause_requested.connect(win.pages["首页"].toggle_pause)
    tray.quit_requested.connect(app.quit)
    app.aboutToQuit.connect(lambda: ctx.engine.stop(timeout=5))
    tray.show()

    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
