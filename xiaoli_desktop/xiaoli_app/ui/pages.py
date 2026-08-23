# -*- coding: utf-8 -*-
"""控制面板六个页面：首页（状态机）/ 角色卡 / 模型 / 任务 / 日志 / 设置。"""
import json
import os
import threading
import time

from PySide6.QtCore import Qt, QTimer, Signal, QSize, QPoint, QRect
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QFormLayout,
    QListWidget, QListWidgetItem, QLineEdit, QPlainTextEdit, QTextEdit,
    QTableWidget, QTableWidgetItem, QComboBox, QDoubleSpinBox, QSpinBox,
    QFileDialog, QMessageBox, QGroupBox, QGridLayout, QCheckBox, QFrame,
    QProgressBar, QScrollArea, QSlider, QHeaderView, QLayout,
)

from xiaoli_app import card_store, config_store
from . import render_svg_icon

# bot.log 实际位置：wechat_bot.LOG_FILE = <xiaoli_desktop>/bot.log。
# pages.py 在 <xiaoli_desktop>/xiaoli_app/ui/，需 dirname 三次才到 xiaoli_desktop。
# 历史缺陷：LogPage 用 dirname 两次 → 指向不存在的 xiaoli_app/bot.log → 日志页永远空。
_BOT_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "bot.log")


# =====================================================================
# 通用：流式布局（空间不足自动换行，预设按钮行小窗口不截断）
# =====================================================================

class _FlowLayout(QLayout):
    """简易流式布局：空间不足自动换行。

    Qt Widgets 无内置 FlowLayout，按官方示例简化实现：
    高度依赖宽度（hasHeightForWidth），换行时 item 移入下一行。
    """

    def __init__(self, parent=None, margin=0, spacing=6):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return s + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _layout(self, rect, test_only):
        x, y = rect.x(), rect.y()
        line_h = 0
        m = self.contentsMargins()
        right = rect.right() - m.right()
        for it in self._items:
            w = it.sizeHint().width()
            if x + w > right and line_h > 0:
                x = rect.x()
                y += line_h + self._spacing
                line_h = 0
            if not test_only:
                it.setGeometry(QRect(QPoint(x, y), it.sizeHint()))
            x += w + self._spacing
            line_h = max(line_h, it.sizeHint().height())
        return y + line_h - rect.y() + m.top() + m.bottom()


# =====================================================================
# 通用：空状态提示（图标 + 文案，覆盖在表格/日志上，无数据时显示）
# =====================================================================

class _EmptyState(QWidget):
    """空状态插画层：SVG 图标 + 引导文案，垂直居中。"""

    def __init__(self, icon, text, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(10)
        self.lbl_icon = QLabel()
        self.lbl_icon.setPixmap(render_svg_icon(icon, size=52))
        self.lbl_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_text = QLabel(text)
        self.lbl_text.setObjectName("tip")
        self.lbl_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_text.setWordWrap(True)
        lay.addWidget(self.lbl_icon)
        lay.addWidget(self.lbl_text)


# =====================================================================
# 首页：状态机主按钮 + 环境检查 + 一键安装 + 首轮提示词
# =====================================================================

class HomePage(QWidget):
    """首页：初始化 → 启动 bot ⇄ 暂停运行 状态机；环境检查卡片；天枢一键安装；首轮提示词发送。

    线程模型：耗时操作（环境检测/下载安装/发送提示词）跑后台线程，结果写回普通属性
    （GIL 原子），主线程 tick() 读取后更新控件——避免跨线程触碰 Qt 控件。
    """

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._env_report = None      # worker 写入
        self._env_running = False
        self._install_running = False
        self._download_progress = 0
        self._install_state = None   # None | "ok" | "失败: ..."
        self._prompt_state = None
        self._prompt_done = False
        self._prompt_running = False
        self._prompt_attempted = False  # 流程发起过即置位：失败后 tick 不再自动重试

        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 20)
        lay.setSpacing(14)

        # 标题行：品牌 logo + 标题 + 副标题并排
        title_row = QHBoxLayout()
        title_row.setSpacing(12)
        from . import render_logo
        self.lbl_logo = QLabel()
        self.lbl_logo.setPixmap(render_logo(44))
        title_row.addWidget(self.lbl_logo)
        self.lbl_title = QLabel("小漓")
        self.lbl_title.setObjectName("title")
        # 标题淡主题色光晕（增强文字质感；颜色随主题主色，切主题时 apply_theme 更新）
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
        _sh = QGraphicsDropShadowEffect(self.lbl_title)
        _sh.setBlurRadius(18)
        _sh.setOffset(0, 2)
        _sh.setColor(QColor(91, 140, 255, 90))
        self._title_shadow = _sh
        self.lbl_title.setGraphicsEffect(_sh)
        title_row.addWidget(self.lbl_title)
        title_row.addStretch(1)
        self.lbl_subtitle = QLabel("你的微信 AI 助手 · 聊天 / 图片识别 / 任务桥")
        self.lbl_subtitle.setObjectName("subtitle")
        title_row.addWidget(self.lbl_subtitle, 0, Qt.AlignmentFlag.AlignBottom)
        lay.addLayout(title_row)

        # 状态 + 主按钮
        self.lbl_state = QLabel("尚未初始化")
        self.lbl_state.setObjectName("stateLabel")
        self.btn_main = QPushButton("初始化")
        self.btn_main.setObjectName("btnMain")
        self.btn_main.setProperty("tone", "primary")
        self.btn_main.setMinimumSize(220, 52)
        self.btn_main.clicked.connect(self._on_main_clicked)
        row = QHBoxLayout()
        row.addWidget(self.lbl_state)
        row.addStretch(1)
        row.addWidget(self.btn_main)
        lay.addLayout(row)

        # 环境检查仪表盘：三张状态卡片横排（微信/天枢/首轮提示词），
        # 每张卡片 = 状态圆点(绿/红/灰) + 大标题 + 检测详情。
        # 从「三行平铺标签」升级为「仪表盘卡片」，首页更有质感。
        self.env_frame = QFrame()
        self.env_frame.setObjectName("card")
        ev = QVBoxLayout(self.env_frame)
        ev.setContentsMargins(16, 14, 16, 14)
        ev.setSpacing(12)
        ev_header = QHBoxLayout()
        ev_header.addWidget(QLabel("环境检查"))
        ev_header.addStretch(1)
        ev.addLayout(ev_header)
        self.env_rows = {}
        env_cards = QHBoxLayout()
        env_cards.setSpacing(10)
        for key, title, sub in (("wechat", "微信 PC 版", "微信客户端连接状态"),
                                ("tianshu", "天枢 CLI", "任务桥命令行就绪"),
                                ("first_prompt", "首轮提示词", "初始化自动发送")):
            card = QFrame()
            card.setObjectName("card")
            card.setMinimumHeight(92)
            cv = QVBoxLayout(card)
            cv.setContentsMargins(14, 12, 14, 12)
            cv.setSpacing(6)
            row = QHBoxLayout()
            row.setSpacing(8)
            dot = QLabel("●")
            dot.setObjectName("envTag")
            dot.setStyleSheet("font-size: 14px;")  # 圆点大小
            t_lbl = QLabel(title)
            t_lbl.setStyleSheet("font-weight: 700; font-size: 15px;")
            row.addWidget(dot)
            row.addWidget(t_lbl)
            row.addStretch(1)
            cv.addLayout(row)
            det = QLabel("检测中…")
            det.setObjectName("tip")
            det.setWordWrap(True)
            cv.addWidget(det)
            env_cards.addWidget(card, 1)
            self.env_rows[key] = (dot, det)
        ev.addLayout(env_cards)
        self.btn_check = QPushButton("重新检查环境")
        self.btn_check.setProperty("compact", "true")  # 缩小：用户反馈与上方卡片靠太近
        self.btn_check.clicked.connect(self._check_env)
        self.btn_install = QPushButton("一键安装天枢")
        self.btn_install.setProperty("compact", "true")
        self.btn_install.setVisible(False)
        self.btn_install.clicked.connect(self._install_tianshu)
        ev.addLayout(self._row(self.btn_check, self.btn_install))
        lay.addWidget(self.env_frame)

        # 进度条（下载进度）
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        lay.addWidget(self.progress)

        # 首轮提示词状态
        self.lbl_prompt = QLabel("")
        self.lbl_prompt.setWordWrap(True)
        self.btn_retry = QPushButton("重试发送首轮提示词")
        self.btn_retry.setVisible(False)
        self.btn_retry.clicked.connect(lambda: self._run_prompt_flow(force=True))
        lay.addWidget(self.lbl_prompt)
        lay.addWidget(self.btn_retry)

        # 运行日志区（填充下方空白）：实时显示 bot.log 增量，
        # 如「发现新消息」「判断为文件消息」「[任务桥] 判定为任务」等运行日志
        log_frame = QFrame()
        log_frame.setObjectName("card")
        lv = QVBoxLayout(log_frame)
        lv.setContentsMargins(16, 12, 16, 12)
        lv.setSpacing(8)
        lh = QHBoxLayout()
        lh.addWidget(QLabel("运行日志"))
        lh.addStretch(1)
        self.btn_log_clear = QPushButton("清空显示")
        self.btn_log_clear.clicked.connect(self._clear_log)
        lh.addWidget(self.btn_log_clear)
        lv.addLayout(lh)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(800)  # 防止日志无限增长撑爆内存
        lv.addWidget(self.log_view)
        lay.addWidget(log_frame, 1)  # stretch=1 占据下方剩余空间

        # 日志轮询：与 LogPage 同源（bot.log），800ms 拉一次增量
        self._log_offset = 0
        self._log_timer = QTimer(self)
        self._log_timer.timeout.connect(self._poll_log)
        self._log_timer.start(800)

    @staticmethod
    def _row(*widgets):
        r = QHBoxLayout()
        for w in widgets:
            r.addWidget(w)
        r.addStretch(1)
        return r

    def apply_theme(self, theme_key):
        """切主题时更新标题光晕颜色（主窗口 apply_backdrop 遍历调用）。"""
        from . import THEMES
        from PySide6.QtGui import QColor
        t = THEMES.get(theme_key, THEMES["blue"])
        p1 = t.get("p1", "#5B8CFF")
        c = QColor(p1)
        c.setAlpha(90)
        shadow = getattr(self, "_title_shadow", None)
        if shadow is not None:
            shadow.setColor(c)

    # ---------- 主按钮状态机 ----------

    def _on_main_clicked(self):
        eng = self.ctx.engine
        if eng is None:
            return
        s = eng.state
        if s in ("idle", "error"):
            # 初始化前确认：自动化流程期间用户不得操作电脑（含切换窗口），
            # 否则按键/剪贴板指令会发到错误窗口（用户要求：点确定才开始初始化）
            ret = QMessageBox.question(
                self, "开始初始化",
                "即将初始化天枢 CLI。\n\n"
                "⚠ 在 Tianshu 完成程序预设指令之前，请不要操作电脑"
                "（包括切换窗口、移动鼠标、点击），以免干扰自动化流程。\n\n"
                "点击「是」开始初始化。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                return
            eng.initialize()
        elif s == "initialized":
            eng.start_bot()
        elif s == "running":
            eng.pause()
        elif s == "paused":
            eng.resume()

    def toggle_pause(self):
        """托盘菜单调用：运行 ⇄ 暂停。"""
        eng = self.ctx.engine
        if eng is None:
            return
        if eng.state == "running":
            eng.pause()
        elif eng.state == "paused":
            eng.resume()

    def _refresh_main_button(self):
        eng = self.ctx.engine
        state = eng.state if eng is not None else "idle"
        tone, text, enabled = "primary", "初始化", True
        if state == "initializing":
            text, enabled = "初始化中…", False
        elif state == "initialized":
            text = "启动 bot"
        elif state == "running":
            text, tone = "暂停运行", "warn"
        elif state == "paused":
            text = "继续运行"
        elif state == "stopped":
            text, enabled = "已停止", False
        elif state == "error":
            text = "重新初始化"
        self.btn_main.setText(text)
        self.btn_main.setEnabled(enabled)
        if self.btn_main.property("tone") != tone:
            self.btn_main.setProperty("tone", tone)
            self.btn_main.style().unpolish(self.btn_main)
            self.btn_main.style().polish(self.btn_main)
        labels = {
            "idle": "尚未初始化", "initializing": "正在初始化…", "initialized": "已就绪，点击「启动 bot」",
            "running": "运行中", "paused": "已暂停", "stopped": "引擎已停止", "error": "初始化失败",
        }
        self.lbl_state.setText(labels.get(state, state))
        if state == "error" and getattr(eng, "error", None):
            self.lbl_state.setText(f"初始化失败：{eng.error}")
        # 初始化完成即自动触发首轮提示词流程（不依赖环境检测报告；
        # 幂等由 _prompt_done 守卫，失败后由 _prompt_attempted 停止自动重试——
        # 否则每次 tick 都重新 resolve+launch CLI，窗口无限循环）
        if (state == "initialized" and not self._prompt_done
                and not self._prompt_running and not self._prompt_attempted):
            self._run_prompt_flow()

    # ---------- 环境检查 ----------

    def _check_env(self):
        if self._env_running:
            return
        self._env_running = True
        self._env_report = None
        for dot, det in self.env_rows.values():
            dot.setObjectName("envPending")
            dot.style().unpolish(dot)
            dot.style().polish(dot)
            det.setText("检测中…")
        threading.Thread(target=self._check_env_worker, daemon=True).start()

    def _check_env_worker(self):
        from xiaoli_app import setup
        try:
            report = setup.check_environment(self.ctx.cfg)
        except Exception as e:
            report = {k: {"ok": False, "detail": f"检测异常: {e}"}
                      for k in ("wechat", "tianshu", "first_prompt")}
        self._env_report = report
        self._env_running = False

    def _apply_env_report(self):
        report = self._env_report
        if report is None:
            return
        self._env_report = None
        for key in ("wechat", "tianshu", "first_prompt"):
            dot, det = self.env_rows[key]
            item = report.get(key, {"ok": False, "detail": "无数据"})
            ok = bool(item.get("ok"))
            # 圆点状态色：✓ 绿 / ✗ 红（objectName 切换后需 repolish 生效）
            dot.setObjectName("envOk" if ok else "envBad")
            dot.style().unpolish(dot)
            dot.style().polish(dot)
            det.setText(str(item.get("detail", "")))
        tianshu_ok = bool(report.get("tianshu", {}).get("ok"))
        self.btn_install.setVisible(not tianshu_ok)
        # 初始化完成后自动触发首轮提示词流程（失败后不自动重试——
        # 见 _refresh_main_button 同款注释：_prompt_attempted 防 tick 循环开 CLI）
        eng = self.ctx.engine
        if (eng is not None and eng.state == "initialized"
                and not self._prompt_done and not self._prompt_running
                and not self._prompt_attempted):
            self._run_prompt_flow()

    # ---------- 一键安装天枢 ----------

    def _install_tianshu(self):
        if self._install_running:
            return
        self._install_running = True
        self._install_state = None
        self._download_progress = 0
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.btn_install.setEnabled(False)
        threading.Thread(target=self._install_worker, daemon=True).start()

    def _install_worker(self):
        from xiaoli_app import setup, config_store
        dest = os.path.join(os.path.expanduser("~"), "Tianshu")
        try:
            inst = setup.install_tianshu(
                dest, progress_cb=lambda p: setattr(self, "_download_progress", p))
            self.ctx.cfg["tianshu_install_dir"] = inst
            try:
                config_store.save_config(self.ctx.cfg, self.ctx.cfg_path)
            except Exception:
                pass
            self._install_state = "ok"
        except Exception as e:
            self._install_state = f"失败：{e}"
        self._install_running = False
        self._download_progress = 100

    def _apply_install_state(self):
        if self._install_running:
            self.progress.setVisible(True)
            self.progress.setValue(self._download_progress)
            self.btn_install.setEnabled(False)
            return
        if self._install_state is not None:
            self.progress.setVisible(False)
            self.btn_install.setEnabled(True)
            st = self._install_state
            self._install_state = None
            QMessageBox.information(
                self, "天枢安装", "天枢已安装完成" if st == "ok" else st)
            if st == "ok":
                self._check_env()

    # ---------- 首轮提示词流程 ----------

    def _run_prompt_flow(self, force=False):
        if self._prompt_running:
            return
        if self._prompt_done and not force:
            return
        self._prompt_attempted = True  # 发起即记录：失败后 tick 不再自动重试（仅手动 force）
        self._prompt_running = True
        self.lbl_prompt.setText("正在发送首轮提示词…")
        threading.Thread(target=self._prompt_worker, daemon=True).start()

    def _prompt_worker(self):
        from xiaoli_app import setup
        cfg = self.ctx.cfg or {}
        fp = (cfg.get("first_prompt_path") or "").strip()
        text = setup.build_first_prompt(cfg)
        if not text:
            self._prompt_state = "首轮提示词为空，无法发送"
            self._prompt_running = False
            return
        # 自定义文件来源时顺带打开文件（内置模板无外部文件可开）
        if fp and os.path.isfile(fp):
            try:
                setup.open_first_prompt(fp)
            except Exception:
                pass
        # 定位 CLI 窗口（排除桌面端污染值）：手动配置 → CLI 特征 → 启动后新增窗口
        title, detail = setup.resolve_cli_window(cfg)
        if not title:
            self._prompt_state = detail or "未找到天枢 CLI 窗口"
            self._prompt_running = False
            return
        # 全自动模式由首启一次性引导的 /yes 保证（持久化，重启后仍生效，
        # 见 run_first_run_guide）——这里不再切 YOLO（旧机制：会话级
        # /permission yolo confirm，每次启动都要重发）。
        ok = setup.send_prompt_to_tianshu(text, title)
        self._prompt_state = "首轮提示词已发送给天枢 ✓" if ok else "发送失败，请确认天枢窗口已打开后重试"
        if ok:
            self._prompt_done = True
        self._prompt_running = False

    def _apply_prompt_state(self):
        if self._prompt_state is None:
            return
        txt = self._prompt_state
        self._prompt_state = None
        self.lbl_prompt.setText(txt)
        self.btn_retry.setVisible("✓" not in txt and "不存在" not in txt)

    # ---------- 主窗口 tick 驱动 ----------

    def tick(self):
        self._refresh_main_button()
        if self._env_report is not None and not self._env_running:
            self._apply_env_report()
        self._apply_install_state()
        self._apply_prompt_state()

    # ---------- 运行日志 ----------

    def _poll_log(self):
        """拉取 bot.log 增量到日志区（与 LogPage 同源，读正确路径）。"""
        try:
            size = os.path.getsize(_BOT_LOG_PATH)
            if size < self._log_offset:
                self._log_offset = 0  # 日志被轮转/清空（重启）
            with open(_BOT_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._log_offset)
                chunk = f.read()
            if chunk:
                self.log_view.appendPlainText(chunk.rstrip())
                self.log_view.verticalScrollBar().setValue(
                    self.log_view.verticalScrollBar().maximum())
                self._log_offset = size
        except OSError:
            pass

    def _clear_log(self):
        self.log_view.clear()


# =====================================================================
# 角色卡页
# =====================================================================

class CardsPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._editing_id = None
        lay = QHBoxLayout(self)

        # 左：卡片列表
        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._on_select)
        left.addWidget(QLabel("角色卡"))
        left.addWidget(self.list)
        btn_row = QHBoxLayout()
        self.btn_new = QPushButton("新建")
        self.btn_dup = QPushButton("复制")
        self.btn_del = QPushButton("删除")
        self.btn_export = QPushButton("导出")
        self.btn_import = QPushButton("导入")
        # 紧凑按钮：5 个按钮横排，左栏限宽下默认 padding(8px 20px) 会让
        # 每个按钮只分到 ~44px 而文字需 ~72px，文字被裁剪成碎点（用户反馈
        # 「五个空白框+小点」）。用 compact 变体缩小 padding 让文字完整。
        for b in (self.btn_new, self.btn_dup, self.btn_del, self.btn_export, self.btn_import):
            b.setProperty("compact", "true")
            b.clicked.connect({
                self.btn_new: self._new_card,
                self.btn_dup: self._duplicate_card,
                self.btn_del: self._delete_card,
                self.btn_export: self._export_card,
                self.btn_import: self._import_card,
            }[b])
            btn_row.addWidget(b)
        left.addLayout(btn_row)
        left_w = QWidget()
        left_w.setLayout(left)
        left_w.setMaximumWidth(320)
        lay.addWidget(left_w)

        # 右：编辑表单
        form = QFormLayout()
        self.ed_name = QLineEdit()
        self.ed_emoji = QLineEdit()
        self.ed_emoji.setMaximumWidth(60)
        self.ed_nick = QLineEdit()
        self.ed_prompt = QPlainTextEdit()
        self.ed_prompt.setPlaceholderText("角色人格设定（system prompt）")
        # 模型统一在模型页（ModelsPage）编辑，角色卡页不重复提供模型输入框与视觉参数
        self.sp_temp = QDoubleSpinBox()
        self.sp_temp.setRange(0, 2)
        self.sp_temp.setSingleStep(0.1)
        self.sp_topp = QDoubleSpinBox()
        self.sp_topp.setRange(0, 1)
        self.sp_topp.setSingleStep(0.05)
        self.sp_history = QSpinBox()
        self.sp_history.setRange(10, 10000)
        self.sp_history.setSingleStep(50)
        form.addRow("名称", self.ed_name)
        form.addRow("表情", self.ed_emoji)
        form.addRow("微信昵称", self.ed_nick)
        form.addRow("人格设定", self.ed_prompt)
        form.addRow("温度", self.sp_temp)
        form.addRow("top_p", self.sp_topp)
        form.addRow("历史条数", self.sp_history)
        # 人格设定撑大：占主导空间。⚠ 不能用固定 min-height 220——
        # 窗口缩小时硬占位会把下方行（温度/top_p）挤出重叠（用户实测「人格设定
        # 下边框和 top_p 重叠」）。改为：小最小高 + Expanding 策略，靠布局伸展
        # 吃剩余空间，窗口缩小时自适应压缩。
        self.ed_prompt.setMinimumHeight(100)
        from PySide6.QtWidgets import QSizePolicy
        self.ed_prompt.setSizePolicy(QSizePolicy.Policy.Expanding,
                                     QSizePolicy.Policy.Expanding)
        self.btn_save = QPushButton("保存")
        self.btn_activate = QPushButton("激活此卡")
        # 缩小：用户反馈两按钮下边界被吞
        self.btn_save.setProperty("compact", "true")
        self.btn_activate.setProperty("compact", "true")
        self.btn_save.clicked.connect(self._save_card)
        self.btn_activate.clicked.connect(self._activate_card)
        form.addRow(self.btn_save, self.btn_activate)
        right_w = QWidget()
        right_w.setLayout(form)
        lay.addWidget(right_w, 1)

    # ---------- 列表 ----------

    def refresh(self):
        cur = self.list.currentItem()
        cur_id = cur.data(Qt.UserRole) if cur else None
        self.list.blockSignals(True)
        self.list.clear()
        for card in card_store.list_cards(self.ctx.cards_dir):
            item = QListWidgetItem(f"{card.get('emoji', '')} {card.get('name', '?')}")
            item.setData(Qt.UserRole, card.get("id"))
            if card.get("id") == self.ctx.active_card_id():
                item.setText(f"{item.text()} ★")
            self.list.addItem(item)
            if card.get("id") == cur_id:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)

    # ---------- 事件 ----------

    def _on_select(self, cur, _prev):
        if cur is None:
            return
        card = card_store.get_card(self.ctx.cards_dir, cur.data(Qt.UserRole))
        if card is None:
            return
        self._editing_id = card.get("id")
        self.ed_name.setText(card.get("name", ""))
        self.ed_emoji.setText(card.get("emoji", ""))
        self.ed_nick.setText(card.get("nickname", ""))
        self.ed_prompt.setPlainText(card.get("system_prompt", ""))
        self.sp_temp.setValue(float(card.get("temperature", 0.7)))
        self.sp_topp.setValue(float(card.get("top_p", 0.9)))
        self.sp_history.setValue(int(card.get("max_history", 1000)))

    def _collect(self):
        # 模型统一在模型页（ModelsPage）编辑，角色卡页不提供模型输入框。
        # chat_provider/chat_model 沿用卡上既有值；新建卡继承当前活跃配置，
        # 避免新卡 chat_model 为空导致运行时 400。
        if self._editing_id:
            exist = card_store.get_card(self.ctx.cards_dir, self._editing_id) or {}
        else:
            exist = card_store.get_card(self.ctx.cards_dir, self.ctx.active_card_id()) or {}
        card = {
            "id": self._editing_id or "",
            "name": self.ed_name.text().strip(),
            "emoji": self.ed_emoji.text().strip(),
            "nickname": self.ed_nick.text().strip(),
            "system_prompt": self.ed_prompt.toPlainText().strip(),
            "chat_provider": exist.get("chat_provider", ""),
            "chat_model": exist.get("chat_model", ""),
            "temperature": self.sp_temp.value(),
            "top_p": self.sp_topp.value(),
            "max_history": self.sp_history.value(),
        }
        return card

    def _new_card(self):
        self._editing_id = None
        self.ed_name.clear()
        self.ed_emoji.clear()
        self.ed_nick.setText("小漓")
        self.ed_prompt.clear()
        self.sp_temp.setValue(0.7)
        self.sp_topp.setValue(0.9)
        self.sp_history.setValue(1000)

    def _save_card(self):
        card = self._collect()
        if not card["name"]:
            QMessageBox.warning(self, "提示", "请填写卡片名称")
            return
        if not card["system_prompt"]:
            QMessageBox.warning(self, "提示", "请填写人格设定")
            return
        if not card["id"]:
            base = "".join(ch for ch in card["name"] if ch.isalnum() or ch in "-_")
            card["id"] = base or f"card_{int(time.time())}"
            card["id"] = f"{card['id']}_{int(time.time()) % 100000}"
        try:
            saved = card_store.save_card(self.ctx.cards_dir, card)
        except ValueError as e:
            QMessageBox.warning(self, "校验失败", str(e))
            return
        self._editing_id = saved["id"]
        self.refresh()
        QMessageBox.information(self, "已保存", f"角色卡「{saved['name']}」已保存")

    def _duplicate_card(self):
        cur = self.list.currentItem()
        if cur is None:
            return
        cid = cur.data(Qt.UserRole)
        try:
            dup = card_store.duplicate_card(self.ctx.cards_dir, cid)
        except ValueError as e:
            QMessageBox.warning(self, "复制失败", str(e))
            return
        # 复制卡继承当前活跃配置：原卡模型配置为空时从活跃卡补齐，
        # 避免新卡 chat_model 为空导致运行时 400。
        if not dup.get("chat_model") or not dup.get("chat_provider"):
            act = card_store.get_card(self.ctx.cards_dir, self.ctx.active_card_id()) or {}
            dup["chat_provider"] = dup.get("chat_provider") or act.get("chat_provider", "")
            dup["chat_model"] = dup.get("chat_model") or act.get("chat_model", "")
            card_store.save_card(self.ctx.cards_dir, dup)
        self.refresh()
        QMessageBox.information(self, "已复制", f"已复制为「{dup['name']}」({dup['id']})")

    def _delete_card(self):
        cur = self.list.currentItem()
        if cur is None:
            return
        cid = cur.data(Qt.UserRole)
        if cid == self.ctx.active_card_id():
            QMessageBox.warning(self, "无法删除", "当前激活的卡不能删除，请先激活其他卡")
            return
        if QMessageBox.question(self, "确认", f"删除角色卡「{cid}」？") != QMessageBox.StandardButton.Yes:
            return
        card_store.delete_card(self.ctx.cards_dir, cid)
        self.refresh()

    def _export_card(self):
        cur = self.list.currentItem()
        if cur is None:
            return
        cid = cur.data(Qt.UserRole)
        path, _ = QFileDialog.getSaveFileName(self, "导出角色卡", f"{cid}.json", "JSON (*.json)")
        if not path:
            return
        try:
            data = card_store.export_card(self.ctx.cards_dir, cid)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))
            return
        QMessageBox.information(self, "已导出", f"已导出到 {path}\n（不含 API key）")

    def _import_card(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入角色卡", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            card = card_store.import_card(self.ctx.cards_dir, data)
        except ValueError as e:
            QMessageBox.warning(self, "导入失败", str(e))
            return
        self.refresh()
        QMessageBox.information(self, "已导入", f"角色卡「{card['name']}」已导入")

    def _activate_card(self):
        cur = self.list.currentItem()
        if cur is None:
            return
        cid = cur.data(Qt.UserRole)
        card = card_store.get_card(self.ctx.cards_dir, cid)
        if card is None:
            return
        # 保存到 config
        self.ctx.cfg["active_card_id"] = cid
        config_store.save_config(self.ctx.cfg, self.ctx.cfg_path)
        # 引擎热切换
        if self.ctx.engine is not None:
            ok = self.ctx.engine.apply_role(card, self.ctx.providers())
            if not ok:
                QMessageBox.information(self, "已保存", "角色卡已设为激活（引擎未就绪，重启后生效）")
        self.refresh()
        QMessageBox.information(self, "已激活", f"角色卡「{card['name']}」已激活")


# =====================================================================
# 模型页（Provider 管理）
# =====================================================================

class ModelsPage(QWidget):
    # 测试连接结果从后台线程回主线程弹窗（跨线程 GUI 会死锁）
    _probe_done = Signal(str, str)  # (kind, message) kind: "ok" / "fail"
    _models_fetched = Signal(int, str, list)  # (row, provider_id, model_ids) 检测到可用模型后写回

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, 5)
        # 隐藏行号列：深色 palette 下垂直表头漏深色成「竖黑条」，且行号无实用价值
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)  # 交替行色（配合 QSS alternate-background-color）
        self.table.setHorizontalHeaderLabels(["名称", "Base URL", "API Key", "模型", "ID"])
        # 列宽布局：模型列 stretch 拉长（内容长），Key/Base URL 加长，
        # ID 适度拉长（用户反馈 70px 太短，拉到 110 可读）；末列不 stretch
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setColumnWidth(0, 90)   # 名称
        self.table.setColumnWidth(1, 240)  # Base URL
        self.table.setColumnWidth(2, 240)  # API Key（加长）
        self.table.setColumnWidth(4, 110)  # ID（拉长，用户反馈原来太短）
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch)  # 模型列拉满剩余
        # 禁止单元格换行（长内容单行截断，避免行高不足内容溢出下边框）
        self.table.setWordWrap(False)
        # 双击/按键才进入编辑：SelectedClicked（单击即编辑）会让 key 单元格
        # 单击就弹编辑器——编辑器按内容宽度收缩、位置相对单元格偏移（用户实测
        # 「点击选中框很窄且双击后弹出的框位置偏移」）。改为单击只选中不编辑，
        # 双击/F2/直接打字才进编辑；key 框粘贴改用「先选中再 Ctrl+V」或双击。
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.EditKeyPressed
            | QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.AnyKeyPressed)
        self._probe_done.connect(self._show_probe_result)
        self._models_fetched.connect(self._apply_fetched_models)
        self.cb_show_key = QCheckBox("显示 API Key")
        self.cb_show_key.toggled.connect(lambda on: self._refresh_keys(on))
        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("添加 Provider")
        self.btn_del = QPushButton("删除选中")
        self.btn_save = QPushButton("保存并应用")
        self.btn_test = QPushButton("测试选中连接")
        self.btn_add.clicked.connect(self._add_row)
        self.btn_del.clicked.connect(self._del_row)
        self.btn_save.clicked.connect(self._save)
        self.btn_test.clicked.connect(self._test)
        for b in (self.btn_add, self.btn_del, self.btn_save, self.btn_test):
            btn_row.addWidget(b)
        # 快捷添加主流 Provider（一键填入 base_url + 常用模型，只填 key 即可）。
        # 流式布局：窗口缩小时自动换行，按钮文字不会被截断
        preset_row = _FlowLayout(spacing=6)
        preset_row.addWidget(QLabel("快捷添加："))
        for p in config_store.PRESET_PROVIDERS:
            b = QPushButton(p["name"])
            b.setToolTip(f"{p['base_url']}\n常用模型：{', '.join(p['models'])}")
            b.clicked.connect(lambda checked=False, pp=p: self._add_preset(pp))
            preset_row.addWidget(b)
        lay.addLayout(preset_row)
        # ---- 模型配置区：单模型（聊天），文字/图片不再分开选 ----
        g_model = QGroupBox("模型配置（聊天）")
        mform = QFormLayout(g_model)
        self.cmb_text_provider = QComboBox()
        self.cmb_text_model = QComboBox()
        self.cmb_text_model.setEditable(True)
        row_t = QHBoxLayout()
        row_t.addWidget(self.cmb_text_provider, 1)
        row_t.addWidget(self.cmb_text_model, 2)
        mform.addRow("文字模型（聊天）", row_t)
        self.btn_model_save = QPushButton("保存模型配置")
        self.btn_model_save.clicked.connect(self._save_model_config)
        mform.addRow(self.btn_model_save)
        lay.addWidget(g_model)
        self.cmb_text_provider.currentIndexChanged.connect(
            lambda *_: self._fill_model_options(self.cmb_text_provider, self.cmb_text_model))
        lay.addWidget(self.cb_show_key)
        lay.addWidget(self.table)
        lay.addLayout(btn_row)
        lay.addWidget(QLabel("提示：API Key 仅保存在本地 config.json（已 gitignore），不会进入日志与角色卡导出。"))

    def refresh(self):
        self.table.setRowCount(0)
        for p in self.ctx.providers():
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(p.get("name", "")))
            self.table.setItem(r, 1, QTableWidgetItem(p.get("base_url", "")))
            self.table.setItem(r, 2, QTableWidgetItem(p.get("api_key", "")))
            self.table.setItem(r, 3, QTableWidgetItem(", ".join(p.get("models", []))))
            self.table.setItem(r, 4, QTableWidgetItem(p.get("id", "")))
        self._refresh_keys(self.cb_show_key.isChecked())
        self._reload_model_config()

    def _reload_model_config(self):
        """模型配置区：provider 下拉 + 活跃卡当前选择回填。"""
        provs = self.ctx.providers()
        ids = [p.get("id", "") for p in provs]
        cb = self.cmb_text_provider
        cur = cb.currentData()
        cb.blockSignals(True)
        cb.clear()
        for pid in ids:
            cb.addItem(pid, pid)
        if cur in ids:
            cb.setCurrentIndex(ids.index(cur))
        cb.blockSignals(False)
        card = card_store.get_card(self.ctx.cards_dir, self.ctx.active_card_id()) or {}
        self._set_prov(self.cmb_text_provider, ids, card.get("chat_provider", ""))
        self._fill_model_options(self.cmb_text_provider, self.cmb_text_model)
        # 回填模型下拉：卡上 chat_model 有值优先；空则回填当前 provider 的
        # 第一个模型（避免下拉空白让用户误以为没配模型 → 保存空 → 重启 400）
        model = card.get("chat_model") or ""
        if not model:
            pid = self.cmb_text_provider.currentData() or ""
            for p in self.ctx.providers():
                if p.get("id") == pid and p.get("models"):
                    model = p["models"][0]
                    break
        self.cmb_text_model.setCurrentText(model)

    @staticmethod
    def _set_prov(cb, ids, val):
        idx = ids.index(val) if val in ids else 0
        cb.setCurrentIndex(idx)

    def _fill_model_options(self, cb_prov, cb_model):
        """模型下拉选项 = 所选 provider 的 models；未匹配时展示全部预设模型。"""
        pid = cb_prov.currentData() or ""
        models = []
        for p in self.ctx.providers():
            if p.get("id") == pid:
                models = p.get("models") or []
                break
        if not models:
            models = sorted({m for pp in config_store.PRESET_PROVIDERS
                             for m in pp.get("models", [])})
        cur = cb_model.currentText()
        cb_model.blockSignals(True)
        cb_model.clear()
        cb_model.addItems(models)
        if cur:
            cb_model.setCurrentText(cur)
        cb_model.blockSignals(False)

    def _save_model_config(self):
        cid = self.ctx.active_card_id()
        card = card_store.get_card(self.ctx.cards_dir, cid)
        if card is None:
            QMessageBox.warning(self, "提示", "未找到活跃角色卡")
            return
        cp = self.cmb_text_provider.currentData() or ""
        cm = self.cmb_text_model.currentText().strip()
        # 空选择防护：下拉为空（provider 无 models / 用户未选）时保留卡上
        # 旧值，不用空串覆盖——否则重启后模型丢失 → bot 启动 400。
        if cp:
            card["chat_provider"] = cp
        if cm:
            card["chat_model"] = cm
        # providers 表同步落盘：用户在表格添加的 Provider/模型必须在 config.json
        # 持久化，否则重启后 load_config_store 读回旧 providers，添加的模型丢失
        # （用户实测：保存模型配置 → 关掉程序再打开模型没了）
        provs = self._collect_providers()
        self.ctx.cfg["providers"] = provs
        config_store.save_config(self.ctx.cfg, self.ctx.cfg_path)
        try:
            card_store.save_card(self.ctx.cards_dir, card)
        except ValueError as e:
            QMessageBox.warning(self, "校验失败", str(e))
            return
        # 重新投影 + 引擎热应用
        self.ctx.cfg = config_store.load_config_store(self.ctx.cfg_path, self.ctx.cards_dir)
        if self.ctx.engine is not None:
            self.ctx.engine.apply_role(card, provs)
        self.refresh()
        QMessageBox.information(self, "已保存", "模型配置已保存并应用")

    def _refresh_keys(self, show):
        mode = QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 2)
            if item is not None:
                item.setFlags(item.flags() | Qt.ItemIsEditable)

    def _collect_providers(self):
        provs = []
        for r in range(self.table.rowCount()):
            name = self.table.item(r, 0).text().strip() if self.table.item(r, 0) else ""
            url = self.table.item(r, 1).text().strip() if self.table.item(r, 1) else ""
            key = self.table.item(r, 2).text().strip() if self.table.item(r, 2) else ""
            models = self.table.item(r, 3).text().strip() if self.table.item(r, 3) else ""
            pid = self.table.item(r, 4).text().strip() if self.table.item(r, 4) else ""
            if not pid:
                pid = name or f"p{r}"
            provs.append({
                "id": pid,
                "name": name or pid,
                "base_url": url,
                "api_key": key,
                "models": [m.strip() for m in models.split(",") if m.strip()],
            })
        return provs

    def _add_row(self):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 4, QTableWidgetItem(f"p{r + 1}"))

    def _add_preset(self, preset):
        """一键添加预设主流 Provider（按 id 去重，api_key 留空待填）。"""
        provs = self._collect_providers()
        if any(p.get("id") == preset["id"] for p in provs):
            QMessageBox.information(self, "已存在", f"{preset['name']} 已在列表中，直接填 API Key 即可")
            return
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(preset["name"]))
        self.table.setItem(r, 1, QTableWidgetItem(preset["base_url"]))
        self.table.setItem(r, 2, QTableWidgetItem(""))
        self.table.setItem(r, 3, QTableWidgetItem(", ".join(preset["models"])))
        self.table.setItem(r, 4, QTableWidgetItem(preset["id"]))

    def _del_row(self):
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)

    def _save(self):
        provs = self._collect_providers()
        self.ctx.cfg["providers"] = provs
        # 同步模型配置区选择到活跃卡：仅靠 providers 落盘不够——重启后模型
        # 下拉从「卡」回填（_reload_model_config），卡 chat_model 不写则重启
        # 后模型丢失 → bot 启动 model 空 → API 400（用户实测「重启后变
        # deepseek 空，启动 bot 报 400」）。此处把当前下拉选择一并写入卡。
        card = card_store.get_card(self.ctx.cards_dir, self.ctx.active_card_id())
        if card is not None:
            cp = self.cmb_text_provider.currentData() or ""
            cm = self.cmb_text_model.currentText().strip()
            if cp:
                card["chat_provider"] = cp
            if cm:
                card["chat_model"] = cm
            try:
                card_store.save_card(self.ctx.cards_dir, card)
            except ValueError as e:
                QMessageBox.warning(self, "校验失败", str(e))
                return
        config_store.save_config(self.ctx.cfg, self.ctx.cfg_path)
        # 重新投影 + 引擎热应用
        card = card_store.get_card(self.ctx.cards_dir, self.ctx.active_card_id())
        self.ctx.cfg = config_store.load_config_store(self.ctx.cfg_path, self.ctx.cards_dir)
        if card is not None and self.ctx.engine is not None:
            self.ctx.engine.apply_role(card, provs)
        self.refresh()
        QMessageBox.information(self, "已保存", "Provider 已保存并应用")

    def _test(self):
        r = self.table.currentRow()
        if r < 0:
            return
        url = self.table.item(r, 1).text().strip() if self.table.item(r, 1) else ""
        key = self.table.item(r, 2).text().strip() if self.table.item(r, 2) else ""
        pid = self.table.item(r, 4).text().strip() if self.table.item(r, 4) else ""
        if not url:
            QMessageBox.warning(self, "提示", "请先填写 Base URL")
            return
        import threading

        def probe():
            # 只做网络请求；结果经 _probe_done 信号回主线程弹窗（跨线程 GUI 会死锁）
            try:
                import requests
                from wechat_bot import models_endpoint
                models_url = models_endpoint(url)
                resp = requests.get(models_url, headers={"Authorization": f"Bearer {key}"}, timeout=(5, 10))
                if resp.status_code == 200:
                    names = [m.get("id", "") for m in resp.json().get("data", [])]
                    names = [n for n in names if n]
                    self._probe_done.emit(
                        "ok",
                        f"检测到 {len(names)} 个可用模型，已填入「模型」列，点「保存并应用」后生效：\n"
                        + "\n".join(names[:20]))
                    self._models_fetched.emit(r, pid, names)
                else:
                    self._probe_done.emit("fail", f"HTTP {resp.status_code}: {resp.text[:200]}")
            except Exception as e:
                self._probe_done.emit("fail", str(e))

        threading.Thread(target=probe, daemon=True).start()

    def _show_probe_result(self, kind, message):
        if kind == "ok":
            QMessageBox.information(self, "连接成功", message)
        else:
            QMessageBox.warning(self, "连接失败", message)

    def _apply_fetched_models(self, row, pid, names):
        """检测到可用模型后写回 provider 的「模型」列，并同步内存 providers（下拉立即可选）。

        模型 id 沿用「厂商:模型」前缀格式（与 PRESET_PROVIDERS 一致，引擎
        strip_model_prefix 剥离前缀透传）；真正持久化仍走「保存并应用」。
        """
        if row < 0 or row >= self.table.rowCount() or not names:
            return
        prefixed = [f"{pid}:{n}" if pid and ":" not in n else n for n in names]
        self.table.setItem(row, 3, QTableWidgetItem(", ".join(prefixed)))
        for p in self.ctx.cfg.get("providers", []):
            if p.get("id") == pid:
                p["models"] = prefixed
                break
        self._reload_model_config()


# =====================================================================
# 任务页
# =====================================================================

class TasksPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, 4)
        # 隐藏行号列（同 ModelsPage：防深色 palette 竖黑条）
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)  # 交替行色（配合 QSS alternate-background-color）
        self.table.setHorizontalHeaderLabels(["任务 ID", "状态", "描述", "更新时间"])
        # 末列拉伸填满视口右缘 + 禁止换行（长内容单行截断）
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.btn_refresh = QPushButton("刷新")
        self.btn_open = QPushButton("打开任务目录")
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_open.clicked.connect(self._open_dir)
        h = QHBoxLayout()
        h.addWidget(self.btn_refresh)
        h.addWidget(self.btn_open)
        h.addStretch(1)
        # 表格 + 空状态 overlay（无任务/目录未配置时显示引导插画）
        self.table_box = QWidget()
        _gl = QGridLayout(self.table_box)
        _gl.setContentsMargins(0, 0, 0, 0)
        _gl.addWidget(self.table, 0, 0)
        self.empty = _EmptyState("list-checks", "暂无任务", self.table_box)
        _gl.addWidget(self.empty, 0, 0)
        lay.addWidget(self.table_box, 1)
        lay.addLayout(h)

    def _set_empty(self, title, detail):
        """切到空态：隐藏表格，显示插画 + 文案。"""
        self.table.hide()
        self.empty.lbl_text.setText(f"{title}\n{detail}")
        self.empty.show()

    def refresh(self):
        tasks_dir = (self.ctx.cfg or {}).get("tasks_dir", "")
        self.table.setRowCount(0)
        if not tasks_dir or not os.path.isdir(tasks_dir):
            self._set_empty("任务目录未配置",
                            "请到「设置」页选择任务工作目录，小漓才能收发任务")
            return
        # 与 CLI task-status 共用 scan_task_status（历史缺陷：两处各扫一遍任务目录）
        from xiaoli_bot import scan_task_status
        entries, waiting, done, archived = scan_task_status(tasks_dir)
        if not entries and not archived:
            self._set_empty("暂无任务",
                            "任务桥目录为空，放入任务文件后这里会显示处理进度")
            return
        self.table.show()
        self.empty.hide()
        for name, state, desc, mtime in entries:
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(name))
            self.table.setItem(
                r, 1,
                QTableWidgetItem("✅ 已完成待回传" if state == "done" else "⏳ 天枢处理中"))
            self.table.setItem(r, 2, QTableWidgetItem(desc))
            self.table.setItem(r, 3, QTableWidgetItem(mtime))
        if archived:
            self.table.insertRow(self.table.rowCount())
            self.table.setItem(self.table.rowCount() - 1, 0, QTableWidgetItem("—"))
            self.table.setItem(self.table.rowCount() - 1, 1,
                               QTableWidgetItem(f"已归档 {archived} 个任务"))

    def _open_dir(self):
        tasks_dir = (self.ctx.cfg or {}).get("tasks_dir", "")
        if tasks_dir and os.path.isdir(tasks_dir):
            os.startfile(tasks_dir)


# =====================================================================
# 日志页
# =====================================================================

class LogPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._offset = 0
        lay = QVBoxLayout(self)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.btn_clear = QPushButton("清空显示")
        self.btn_clear.clicked.connect(self._clear_view)
        # 日志视图 + 空状态 overlay（无日志时显示引导插画）
        self.view_box = QWidget()
        _gl = QGridLayout(self.view_box)
        _gl.setContentsMargins(0, 0, 0, 0)
        _gl.addWidget(self.view, 0, 0)
        self.empty = _EmptyState("file-text", "暂无日志", self.view_box)
        _gl.addWidget(self.empty, 0, 0)
        lay.addWidget(self.view_box, 1)
        lay.addWidget(self.btn_clear)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(800)

    def _clear_view(self):
        self.view.clear()
        self.empty.show()

    def _poll(self):
        log_path = _BOT_LOG_PATH
        try:
            size = os.path.getsize(log_path)
            if size < self._offset:
                self._offset = 0  # 日志被清空（重启）
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._offset)
                chunk = f.read()
            if chunk:
                self.view.appendPlainText(chunk.rstrip())
                self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())
                self._offset = size
        except OSError:
            pass
        # 空态跟随日志内容显隐
        self.empty.setVisible(self.view.document().isEmpty())


# =====================================================================
# 设置页
# =====================================================================

class SettingsPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        # 设置项多：外层套 QScrollArea 可滚动，避免窗口放不下时被裁掉
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(12)

        # 界面主题 + 壁纸
        g_theme = QGroupBox("界面主题与壁纸")
        tv = QVBoxLayout(g_theme)
        tv.setSpacing(8)
        tip_theme = QLabel("点击缩略图切换主题（即时生效并保存）；右侧壁纸库「壁纸」文件夹自动收录")
        tip_theme.setObjectName("tip")
        tip_theme.setWordWrap(True)
        tv.addWidget(tip_theme)
        # 跟随系统深浅色：勾选后忽略手动主题选择（自动切 tokyonight/blue）
        self.cb_follow_system = QCheckBox("跟随系统深浅色（自动切换深/浅主题）")
        self.cb_follow_system.setChecked(bool((self.ctx.cfg or {}).get("follow_system")))
        self.cb_follow_system.toggled.connect(self._on_follow_system)
        tv.addWidget(self.cb_follow_system)
        # 两列布局：主题网格 | 壁纸网格 并排（原来上下排列滚动太深）
        duo = QHBoxLayout()
        duo.setSpacing(10)
        col_theme = QVBoxLayout()
        col_theme.setSpacing(6)
        lbl_t = QLabel("主题")
        lbl_t.setObjectName("tip")
        col_theme.addWidget(lbl_t)
        from . import THEMES, ThumbDelegate, render_theme_thumb
        self.theme_grid = QListWidget()
        self.theme_grid.setObjectName("themeGrid")
        self.theme_grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.theme_grid.setIconSize(QSize(120, 76))
        self.theme_grid.setGridSize(QSize(150, 110))
        self.theme_grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.theme_grid.setMovement(QListWidget.Movement.Static)
        self.theme_grid.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.theme_grid.setFlow(QListWidget.Flow.LeftToRight)
        self.theme_grid.setWrapping(True)
        self.theme_grid.setFixedHeight(236)
        # 自绘 delegate：hover 放大 + 选中态主题色描边 + 右上角对勾（每项用自身主题色）
        self.theme_grid.setItemDelegate(ThumbDelegate(self.theme_grid, per_item_color=True))
        self._theme_keys = []
        for key, t in THEMES.items():
            item = QListWidgetItem(QIcon(render_theme_thumb(key)), t["label"])
            item.setData(Qt.ItemDataRole.UserRole, key)
            item.setSizeHint(QSize(120, 92))
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
            self.theme_grid.addItem(item)
            self._theme_keys.append(key)
        self.theme_grid.currentItemChanged.connect(self._on_theme_picked)
        col_theme.addWidget(self.theme_grid)
        duo.addLayout(col_theme, 1)
        # 壁纸库：内置「壁纸」目录 + 无壁纸 + 用户自定义
        col_wp = QVBoxLayout()
        col_wp.setSpacing(6)
        lbl_w = QLabel("壁纸")
        lbl_w.setObjectName("tip")
        col_wp.addWidget(lbl_w)
        from . import list_wallpapers, wallpaper_short_name, render_wallpaper_thumb
        self.wp_grid = QListWidget()
        self.wp_grid.setObjectName("wpGrid")
        self.wp_grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.wp_grid.setIconSize(QSize(120, 76))
        self.wp_grid.setGridSize(QSize(150, 110))
        self.wp_grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.wp_grid.setMovement(QListWidget.Movement.Static)
        self.wp_grid.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.wp_grid.setFlow(QListWidget.Flow.LeftToRight)
        self.wp_grid.setWrapping(True)
        self.wp_grid.setFixedHeight(236)
        # 自绘 delegate：与主题网格同款交互；描边色用当前主题 p1（壁纸无自身配色）
        _cur_theme = (self.ctx.cfg or {}).get("theme", "blue")
        self.wp_delegate = ThumbDelegate(
            self.wp_grid, per_item_color=False,
            accent=THEMES.get(_cur_theme, THEMES["blue"])["p1"])
        self.wp_grid.setItemDelegate(self.wp_delegate)
        self._wp_items = []  # [(item, path)]
        # 无壁纸项（data=""）
        item = QListWidgetItem(QIcon(render_theme_thumb("blue")), "无壁纸")
        item.setData(Qt.ItemDataRole.UserRole, "")
        item.setSizeHint(QSize(120, 92))
        item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
        self.wp_grid.addItem(item)
        self._wp_items.append((item, ""))
        # 内置壁纸目录
        self._builtin_wp = [p for p, _f in list_wallpapers()]
        for path, fname in list_wallpapers():
            item = QListWidgetItem(QIcon(render_wallpaper_thumb(path)),
                                   wallpaper_short_name(fname))
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setSizeHint(QSize(120, 92))
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
            self.wp_grid.addItem(item)
            self._wp_items.append((item, path))
        self.wp_grid.currentItemChanged.connect(self._on_wp_picked)
        col_wp.addWidget(self.wp_grid)
        duo.addLayout(col_wp, 1)
        tv.addLayout(duo)
        self.ed_wallpaper = QLineEdit()
        self.ed_wallpaper.setReadOnly(True)
        row_wp = QHBoxLayout()
        row_wp.addWidget(self.ed_wallpaper, 1)
        btn_wp = QPushButton("导入壁纸…")
        btn_wp.clicked.connect(self._pick_wallpaper)
        btn_wp_clear = QPushButton("清除壁纸")
        btn_wp_clear.clicked.connect(self._clear_wallpaper)
        row_wp.addWidget(btn_wp)
        row_wp.addWidget(btn_wp_clear)
        tv.addLayout(row_wp)
        # 卡片透明度（毛玻璃强度）：滑块 50%~100%，实时生效并保存
        op_row = QHBoxLayout()
        op_row.setSpacing(8)
        op_row.addWidget(QLabel("卡片透明度"))
        self.sl_opacity = QSlider(Qt.Orientation.Horizontal)
        self.sl_opacity.setRange(0, 100)
        self.sl_opacity.setValue(int((self.ctx.cfg or {}).get("card_opacity", 0.5) * 100))
        self.lbl_op_val = QLabel()
        self.lbl_op_val.setObjectName("tip")
        self.sl_opacity.valueChanged.connect(self._on_opacity_changed)
        op_row.addWidget(self.sl_opacity, 1)
        op_row.addWidget(self.lbl_op_val)
        tv.addLayout(op_row)
        self._update_op_label(self.sl_opacity.value())
        # 面板/输入区透明度（日志区/表格/输入框等大白块，与卡片独立）
        pn_row = QHBoxLayout()
        pn_row.setSpacing(8)
        pn_row.addWidget(QLabel("面板透明度"))
        self.sl_panel = QSlider(Qt.Orientation.Horizontal)
        self.sl_panel.setRange(0, 100)
        self.sl_panel.setValue(int((self.ctx.cfg or {}).get("panel_opacity", 0.5) * 100))
        self.lbl_panel_val = QLabel()
        self.lbl_panel_val.setObjectName("tip")
        self.sl_panel.valueChanged.connect(self._on_panel_changed)
        pn_row.addWidget(self.sl_panel, 1)
        pn_row.addWidget(self.lbl_panel_val)
        tv.addLayout(pn_row)
        self._update_panel_label(self.sl_panel.value())
        # 全局字号：小/中/大三档
        fs_row = QHBoxLayout()
        fs_row.setSpacing(8)
        fs_row.addWidget(QLabel("字号大小"))
        self.cb_font = QComboBox()
        for label, key in (("小", "small"), ("中", "medium"), ("大", "large")):
            self.cb_font.addItem(label, key)
        _fi = self.cb_font.findData((self.ctx.cfg or {}).get("font_scale", "medium"))
        self.cb_font.setCurrentIndex(max(0, _fi))
        self.cb_font.currentIndexChanged.connect(self._on_font_changed)
        fs_row.addWidget(self.cb_font, 1)
        tv.addLayout(fs_row)
        lay.addWidget(g_theme)

        # 记忆
        g_mem = QGroupBox("对话记忆（memory.json）")
        gl = QVBoxLayout(g_mem)
        self.mem_summary = QLabel()
        self.btn_mem_view = QPushButton("查看记忆")
        self.btn_mem_clear = QPushButton("清空全部记忆")
        self.mem_view = QTextEdit()
        self.mem_view.setReadOnly(True)
        self.mem_view.setMinimumHeight(260)
        self.btn_mem_view.clicked.connect(self._view_memory)
        self.btn_mem_clear.clicked.connect(self._clear_memory)
        gl.addWidget(self.mem_summary)
        gl.addWidget(self.btn_mem_view)
        gl.addWidget(self.btn_mem_clear)
        gl.addWidget(self.mem_view)
        lay.addWidget(g_mem)

        # 微信文件目录（任务附件识别源）
        g_files = QGroupBox("微信文件目录（收到的文件下载位置）")
        ffl = QHBoxLayout(g_files)
        self.ed_files = QLineEdit()
        self.ed_files.setReadOnly(True)
        btn_files = QPushButton("浏览…")
        btn_files.clicked.connect(self._pick_files_dir)
        self.btn_files_save = QPushButton("保存")
        self.btn_files_save.clicked.connect(self._save_files_dir)
        ffl.addWidget(self.ed_files, 1)
        ffl.addWidget(btn_files)
        ffl.addWidget(self.btn_files_save)
        lay.addWidget(g_files)

        # 任务工作目录（小漓 ↔ 天枢交换任务/成果文件；首次启动引导选择的目录）
        g_tasks = QGroupBox("任务工作目录（小漓与天枢交换任务/成果文件）")
        tfl = QHBoxLayout(g_tasks)
        self.ed_tasks = QLineEdit()
        self.ed_tasks.setReadOnly(True)
        btn_tasks = QPushButton("浏览…")
        btn_tasks.clicked.connect(self._pick_tasks_dir)
        self.btn_tasks_save = QPushButton("保存")
        self.btn_tasks_save.clicked.connect(self._save_tasks_dir)
        self.btn_guide = QPushButton("重新引导天枢 CLI")
        self.btn_guide.clicked.connect(self._run_guide)
        tfl.addWidget(self.ed_tasks, 1)
        tfl.addWidget(btn_tasks)
        tfl.addWidget(self.btn_tasks_save)
        tfl.addWidget(self.btn_guide)
        lay.addWidget(g_tasks)

        # 成果排除登记
        g_stem = QGroupBox("成果排除登记（防止 bot 发出去的成果被误当用户文件）")
        stl = QVBoxLayout(g_stem)
        self.stem_view = QTextEdit()
        self.stem_view.setReadOnly(True)
        self.stem_view.setMinimumHeight(180)
        self.btn_stem_refresh = QPushButton("刷新")
        self.btn_stem_refresh.clicked.connect(self._refresh_stems)
        stl.addWidget(self.stem_view)
        stl.addWidget(self.btn_stem_refresh)
        lay.addWidget(g_stem)

        lay.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll)

    def refresh(self):
        cfg = self.ctx.cfg or {}
        mem_file = cfg.get("memory_file", "memory.json")
        if os.path.isfile(mem_file):
            try:
                with open(mem_file, "r", encoding="utf-8") as f:
                    db = json.load(f)
                total = sum(len(v) for v in db.values())
                self.mem_summary.setText(f"共 {len(db)} 个聊天，{total} 条消息（{os.path.getsize(mem_file) // 1024} KB）")
            except Exception:
                self.mem_summary.setText("记忆文件读取失败")
        else:
            self.mem_summary.setText("无记忆文件")
        self.ed_files.setText(cfg.get("file_storage_path", ""))
        self.ed_tasks.setText(cfg.get("tasks_dir", ""))
        theme = cfg.get("theme", "blue")
        self.cb_follow_system.setChecked(bool(cfg.get("follow_system")))
        # blockSignals：程序回填选中不触发 currentItemChanged→_on_theme_picked
        # （那会无条件写盘 + 全量 QSS 重建；回填仅是 UI 同步，非用户操作）
        self.theme_grid.blockSignals(True)
        try:
            self.theme_grid.setCurrentRow(self._theme_keys.index(theme))
        except ValueError:
            pass
        finally:
            self.theme_grid.blockSignals(False)
        self._select_wallpaper_item(cfg.get("wallpaper_path", ""))
        self.ed_wallpaper.setText(cfg.get("wallpaper_path", ""))
        self.sl_opacity.setValue(int(cfg.get("card_opacity", 0.5) * 100))
        self.sl_panel.setValue(int(cfg.get("panel_opacity", 0.5) * 100))
        _fi = self.cb_font.findData(cfg.get("font_scale", "medium"))
        self.cb_font.setCurrentIndex(max(0, _fi))
        self._refresh_stems()

    def _select_wallpaper_item(self, path):
        """按路径高亮壁纸网格项（当前壁纸；不在内置列表则不选中）。

        blockSignals：程序回填不触发 _on_wp_picked（避免非意图写盘+QSS 重建）。
        """
        self.wp_grid.blockSignals(True)
        try:
            for row, (item, wp_path) in enumerate(self._wp_items):
                if wp_path == path:
                    self.wp_grid.setCurrentRow(row)
                    return
            self.wp_grid.clearSelection()
        finally:
            self.wp_grid.blockSignals(False)

    def _on_wp_picked(self, current, previous):
        """点击壁纸缩略图：即时应用并保存（data 为 '' = 无壁纸）。"""
        if current is None:
            return
        path = current.data(Qt.ItemDataRole.UserRole) or ""
        self.ctx.cfg["wallpaper_path"] = path
        config_store.save_config(self.ctx.cfg, self.ctx.cfg_path)
        self.ed_wallpaper.setText(path)
        self._apply_theme_qss(self.ctx.cfg.get("theme", "blue"), path)

    def _pick_wallpaper(self):
        p, _f = QFileDialog.getOpenFileName(
            self, "导入壁纸", self.ed_wallpaper.text(),
            "图片 (*.png *.jpg *.jpeg *.bmp *.webp)")
        if not p:
            return
        self.ctx.cfg["wallpaper_path"] = p
        config_store.save_config(self.ctx.cfg, self.ctx.cfg_path)
        self.ed_wallpaper.setText(p)
        # 网格加一项并选中（不在内置列表时）
        if p not in self._builtin_wp:
            from . import render_wallpaper_thumb, wallpaper_short_name
            item = QListWidgetItem(QIcon(render_wallpaper_thumb(p)),
                                   "自定义: " + wallpaper_short_name(os.path.basename(p)))
            item.setData(Qt.ItemDataRole.UserRole, p)
            item.setSizeHint(QSize(120, 92))
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
            self.wp_grid.addItem(item)
            self._wp_items.append((item, p))
            self.wp_grid.setCurrentItem(item)
        else:
            self._select_wallpaper_item(p)
        self._apply_theme_qss(self.ctx.cfg.get("theme", "blue"), p)

    def _clear_wallpaper(self):
        """清除壁纸：恢复纯渐变背景。"""
        self.ctx.cfg["wallpaper_path"] = ""
        config_store.save_config(self.ctx.cfg, self.ctx.cfg_path)
        self.ed_wallpaper.clear()
        self._select_wallpaper_item("")
        self._apply_theme_qss(self.ctx.cfg.get("theme", "blue"), "")

    def _apply_theme_qss(self, theme, wp):
        """应用 QSS + 同步粒子背景层（主题/壁纸/透明度/字号变化后统一入口）。"""
        from . import THEMES, build_qss, resolve_theme
        # 跟随系统开关生效时，theme 参数自动解析为系统深浅对应主题
        theme = resolve_theme(theme, bool((self.ctx.cfg or {}).get("follow_system")))
        # 壁纸网格选中态描边/对勾色随主题切换（主题网格是每项自身主题色，无需更新）
        dl = getattr(self, "wp_delegate", None)
        if dl is not None:
            dl.set_accent(THEMES.get(theme, THEMES["blue"])["p1"])
        from PySide6.QtWidgets import QApplication
        cfg = self.ctx.cfg or {}
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(build_qss(theme, wp,
                                        card_opacity=cfg.get("card_opacity", 0.5),
                                        panel_opacity=cfg.get("panel_opacity", 0.5),
                                        font_scale=cfg.get("font_scale", "medium")))
        win = getattr(self.ctx, "win", None)
        if win is not None and hasattr(win, "apply_backdrop"):
            win.apply_backdrop(theme, wp)

    def _on_font_changed(self, idx):
        """字号档位切换：重建 QSS 并保存。"""
        scale = self.cb_font.currentData() or "medium"
        self.ctx.cfg["font_scale"] = scale
        config_store.save_config(self.ctx.cfg, self.ctx.cfg_path)
        self._apply_theme_qss(self.ctx.cfg.get("theme", "blue"),
                              self.ed_wallpaper.text().strip())

    def _update_op_label(self, val):
        self.lbl_op_val.setText(f"{val}%")

    def _on_opacity_changed(self, val):
        """卡片透明度滑块：实时重建 QSS（毛玻璃强度）并保存。"""
        opacity = val / 100.0
        self._update_op_label(val)
        self.ctx.cfg["card_opacity"] = opacity
        config_store.save_config(self.ctx.cfg, self.ctx.cfg_path)
        self._apply_theme_qss(self.ctx.cfg.get("theme", "blue"),
                              self.ed_wallpaper.text().strip())

    def _update_panel_label(self, val):
        self.lbl_panel_val.setText(f"{val}%")

    def _on_panel_changed(self, val):
        """面板透明度滑块：控制日志区/表格/输入框等大白块的透出程度。"""
        opacity = val / 100.0
        self._update_panel_label(val)
        self.ctx.cfg["panel_opacity"] = opacity
        config_store.save_config(self.ctx.cfg, self.ctx.cfg_path)
        self._apply_theme_qss(self.ctx.cfg.get("theme", "blue"),
                              self.ed_wallpaper.text().strip())

    def _on_theme_picked(self, current, previous):
        """点击主题缩略图：即时应用并保存（不弹窗）。"""
        if current is None:
            return
        key = current.data(Qt.ItemDataRole.UserRole)
        if not key:
            return
        self.ctx.cfg["theme"] = key
        config_store.save_config(self.ctx.cfg, self.ctx.cfg_path)
        self._apply_theme_qss(key, self.ed_wallpaper.text().strip())

    def _on_follow_system(self, on):
        """跟随系统深浅色开关：保存配置并立即按系统深浅应用主题。"""
        self.ctx.cfg["follow_system"] = bool(on)
        config_store.save_config(self.ctx.cfg, self.ctx.cfg_path)
        self._apply_theme_qss(self.ctx.cfg.get("theme", "blue"),
                              self.ed_wallpaper.text().strip())

    def _view_memory(self):
        cfg = self.ctx.cfg or {}
        mem_file = cfg.get("memory_file", "memory.json")
        if not os.path.isfile(mem_file):
            self.mem_view.setPlainText("无记忆文件")
            return
        try:
            with open(mem_file, "r", encoding="utf-8") as f:
                db = json.load(f)
            lines = []
            for chat, hist in sorted(db.items()):
                lines.append(f"===== {chat}（{len(hist)} 条）=====")
                for h in hist[-20:]:
                    ts = h.get("time", "")
                    role = "用户" if h.get("role") == "user" else "小漓"
                    lines.append(f"[{ts}] {role}: {h.get('content', '')[:80]}")
            self.mem_view.setPlainText("\n".join(lines[-500:]))
        except Exception as e:
            self.mem_view.setPlainText(f"读取失败: {e}")

    def _clear_memory(self):
        if QMessageBox.question(self, "确认", "清空全部对话记忆？此操作不可撤销") != QMessageBox.StandardButton.Yes:
            return
        cfg = self.ctx.cfg or {}
        mem_file = cfg.get("memory_file", "memory.json")
        if os.path.isfile(mem_file):
            with open(mem_file, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=2)
        self.refresh()
        QMessageBox.information(self, "已清空", "记忆已清空")

    def _pick_files_dir(self):
        p = QFileDialog.getExistingDirectory(
            self, "选择微信文件目录", self.ed_files.text() or "")
        if p:
            self.ed_files.setText(p)

    def _save_files_dir(self):
        self.ctx.cfg["file_storage_path"] = self.ed_files.text().strip()
        config_store.save_config(self.ctx.cfg, self.ctx.cfg_path)
        QMessageBox.information(self, "已保存", "微信文件目录已保存")

    def _pick_tasks_dir(self):
        p = QFileDialog.getExistingDirectory(
            self, "选择任务工作目录", self.ed_tasks.text() or "")
        if p:
            self.ed_tasks.setText(p)

    def _save_tasks_dir(self):
        new_tasks = self.ed_tasks.text().strip()
        old_tasks = str((self.ctx.cfg or {}).get("tasks_dir") or "").strip()
        old_workdir = str((self.ctx.cfg or {}).get("tianshu_workdir") or "").strip()
        self.ctx.cfg["tasks_dir"] = new_tasks
        # 任务目录实际变更 → 清除原目录文件 + 重置一次性引导标记（新目录重走首启引导）。
        # 用户明确要求：更改工作目录时删除原工作目录文件。防护：tasks_dir 若恰为
        # tianshu_workdir 本身（sync 派生关系异常场景）则不删 CLI 工作根，只提示。
        tasks_moved = bool(old_tasks) and os.path.normcase(os.path.abspath(old_tasks)) \
            != os.path.normcase(os.path.abspath(new_tasks))
        if tasks_moved:
            if os.path.normcase(os.path.abspath(old_tasks)) != os.path.normcase(os.path.abspath(old_workdir or "")):
                import shutil
                shutil.rmtree(old_tasks, ignore_errors=True)
            self.ctx.cfg["tianshu_guided"] = False  # 新目录需重新引导（/yes 全自动）
        # 天枢 CLI 以 tianshu_workdir 为 cwd（路径检查基于 cwd）——任务目录
        # 改到哪，工作目录就同步到它的父目录，用户选任意目录都能工作
        workdir_changed = config_store.sync_workdir_to_tasks(self.ctx.cfg)
        config_store.save_config(self.ctx.cfg, self.ctx.cfg_path)
        # 新任务目录自动初始化任务桥协议文档（幂等，已存在不覆盖）
        try:
            from xiaoli_app import setup as _setup
            _setup.ensure_bridge_readme(self.ctx.cfg.get("tasks_dir", ""))
        except Exception:
            pass
        # 预授权给天枢 CLI（agent.permissions）——常驻 CLI 也能读任意位置任务目录
        grant_ok, grant_changed = config_store.grant_tasks_dir_to_tianshu(new_tasks)
        msg = "任务工作目录已保存"
        new_workdir = str((self.ctx.cfg or {}).get("tianshu_workdir") or "").strip()
        if tasks_moved:
            msg += "\n原工作目录文件已清除，将在新目录重新引导天枢 CLI"
        if workdir_changed and old_workdir and old_workdir != new_workdir:
            msg += f"\n天枢 CLI 工作目录已同步为：{new_workdir}"
        if grant_changed:
            msg += "\n天枢 CLI 已授权读取新任务目录"
        if (workdir_changed or grant_changed) and not grant_ok:
            msg += "\n（未能自动授权天枢 CLI，任务可能需手动确认路径）"
        if workdir_changed or grant_changed:
            msg += "\n若天枢 CLI 窗口已打开，请重启它以生效"
        if tasks_moved:
            # 明确提醒：工作目录已更改，天枢 CLI 必须在新目录重新引导（/yes 全自动）
            # 才能正常处理任务——否则 CLI 仍以旧目录为 cwd，任务桥断链。
            ret = QMessageBox.question(
                self, "已保存",
                msg + "\n\n⚠ 工作目录已更改：天枢 CLI 需要在新的工作目录重新引导"
                      "（打开窗口 → 确认配置后自动发送 /yes 开启全自动），"
                      "否则任务处理会失效。\n\n是否现在重新引导？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ret == QMessageBox.StandardButton.Yes:
                self._run_guide()
            else:
                QMessageBox.information(
                    self, "提示",
                    "稍后可在设置页点击「重新引导天枢 CLI」完成引导。\n"
                    "在完成引导前，请勿向微信发送任务。")
        else:
            QMessageBox.information(self, "已保存", msg)

    def _run_guide(self):
        """重新跑首启一次性引导（/yes 全自动）——目录变更后或用户手动触发。"""
        try:
            from xiaoli_app import setup as _setup
            _setup.run_first_run_guide(self.ctx.cfg, parent=self,
                                       cfg_path=self.ctx.cfg_path)
        except Exception as e:
            QMessageBox.warning(self, "引导失败", f"天枢 CLI 引导失败：{e}")

    def _refresh_stems(self):
        cfg = self.ctx.cfg or {}
        stems_file = os.path.join(cfg.get("tasks_dir", ""), "sent_back_stems.json")
        if os.path.isfile(stems_file):
            try:
                with open(stems_file, "r", encoding="utf-8") as f:
                    stems = json.load(f)
                lines = [f"{k}（{time.strftime('%m-%d %H:%M', time.localtime(v))}）" for k, v in stems.items()]
                self.stem_view.setPlainText("\n".join(lines[-50:]) or "（空）")
            except Exception:
                self.stem_view.setPlainText("读取失败")
        else:
            self.stem_view.setPlainText("（无成果登记文件）")
