# -*- coding: utf-8 -*-
"""控制面板六个页面：首页（状态机）/ 角色卡 / 模型 / 任务 / 日志 / 设置。"""
import json
import os
import threading
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QFormLayout,
    QListWidget, QListWidgetItem, QLineEdit, QPlainTextEdit, QTextEdit,
    QTableWidget, QTableWidgetItem, QComboBox, QDoubleSpinBox, QSpinBox,
    QFileDialog, QMessageBox, QGroupBox, QGridLayout, QCheckBox, QFrame,
    QProgressBar,
)

from xiaoli_app import card_store, config_store


def _key_display(key):
    """显示用 key：遮蔽中间。"""
    return config_store.mask_key(key)


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

        # 标题
        self.lbl_title = QLabel("小漓")
        self.lbl_title.setObjectName("title")
        self.lbl_subtitle = QLabel("你的微信 AI 助手 · 聊天 / 图片识别 / 任务桥")
        self.lbl_subtitle.setObjectName("subtitle")
        lay.addWidget(self.lbl_title)
        lay.addWidget(self.lbl_subtitle)

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

        # 环境检查卡片
        self.env_frame = QFrame()
        self.env_frame.setObjectName("card")
        ev = QVBoxLayout(self.env_frame)
        ev.setContentsMargins(16, 12, 16, 12)
        ev.setSpacing(8)
        ev.addWidget(QLabel("环境检查"))
        self.env_rows = {}
        for key, title in (("wechat", "微信 PC 版"), ("tianshu", "天枢 CLI"),
                           ("first_prompt", "首轮提示词")):
            r = QHBoxLayout()
            tag = QLabel("—")
            tag.setStyleSheet("font-weight:600;")
            det = QLabel("检测中…")
            det.setWordWrap(True)
            r.addWidget(QLabel(title))
            r.addStretch(1)
            r.addWidget(tag)
            r.addWidget(det)
            ev.addLayout(r)
            self.env_rows[key] = (tag, det)
        self.btn_check = QPushButton("重新检查环境")
        self.btn_check.clicked.connect(self._check_env)
        self.btn_install = QPushButton("一键安装天枢")
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
        lay.addStretch(1)

    @staticmethod
    def _row(*widgets):
        r = QHBoxLayout()
        for w in widgets:
            r.addWidget(w)
        r.addStretch(1)
        return r

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
        for tag, det in self.env_rows.values():
            tag.setText("…")
            tag.setStyleSheet("font-weight:600; color:#9CA3AF;")
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
            tag, det = self.env_rows[key]
            item = report.get(key, {"ok": False, "detail": "无数据"})
            ok = bool(item.get("ok"))
            tag.setText("✓" if ok else "✗")
            tag.setStyleSheet("font-weight:600; color:#10B981;" if ok else "font-weight:600; color:#EF4444;")
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
        self.btn_new.clicked.connect(self._new_card)
        self.btn_dup.clicked.connect(self._duplicate_card)
        self.btn_del.clicked.connect(self._delete_card)
        self.btn_export.clicked.connect(self._export_card)
        self.btn_import.clicked.connect(self._import_card)
        for b in (self.btn_new, self.btn_dup, self.btn_del, self.btn_export, self.btn_import):
            btn_row.addWidget(b)
        left.addLayout(btn_row)
        left_w = QWidget()
        left_w.setLayout(left)
        left_w.setMaximumWidth(260)
        lay.addWidget(left_w)

        # 右：编辑表单
        form = QFormLayout()
        self.ed_name = QLineEdit()
        self.ed_emoji = QLineEdit()
        self.ed_emoji.setMaximumWidth(60)
        self.ed_nick = QLineEdit()
        self.ed_prompt = QPlainTextEdit()
        self.ed_prompt.setPlaceholderText("角色人格设定（system prompt）")
        self.cb_chat_provider = QComboBox()
        self.cb_vision_provider = QComboBox()
        self.cb_classify_provider = QComboBox()
        # 模型输入：可编辑下拉（选项来自所选 provider 的 models；未选时展示全部预设模型）
        self.ed_chat_model = QComboBox()
        self.ed_chat_model.setEditable(True)
        self.ed_vision_model = QComboBox()
        self.ed_vision_model.setEditable(True)
        self.ed_classify_model = QComboBox()
        self.ed_classify_model.setEditable(True)
        self.cb_chat_provider.currentIndexChanged.connect(self._refresh_model_options)
        self.cb_vision_provider.currentIndexChanged.connect(self._refresh_model_options)
        self.cb_classify_provider.currentIndexChanged.connect(self._refresh_model_options)
        self.sp_temp = QDoubleSpinBox()
        self.sp_temp.setRange(0, 2)
        self.sp_temp.setSingleStep(0.1)
        self.sp_topp = QDoubleSpinBox()
        self.sp_topp.setRange(0, 1)
        self.sp_topp.setSingleStep(0.05)
        self.sp_vtemp = QDoubleSpinBox()
        self.sp_vtemp.setRange(0, 2)
        self.sp_vtemp.setSingleStep(0.1)
        self.sp_vmax = QSpinBox()
        self.sp_vmax.setRange(1, 100000)
        self.sp_vmax.setSingleStep(500)
        self.sp_history = QSpinBox()
        self.sp_history.setRange(10, 10000)
        self.sp_history.setSingleStep(50)
        form.addRow("名称", self.ed_name)
        form.addRow("表情", self.ed_emoji)
        form.addRow("微信昵称", self.ed_nick)
        form.addRow("人格设定", self.ed_prompt)
        form.addRow("聊天提供商", self.cb_chat_provider)
        form.addRow("聊天模型", self.ed_chat_model)
        form.addRow("视觉提供商", self.cb_vision_provider)
        form.addRow("视觉模型", self.ed_vision_model)
        form.addRow("分类提供商", self.cb_classify_provider)
        form.addRow("分类模型", self.ed_classify_model)
        form.addRow("温度", self.sp_temp)
        form.addRow("top_p", self.sp_topp)
        form.addRow("视觉温度", self.sp_vtemp)
        form.addRow("视觉 max_tokens", self.sp_vmax)
        form.addRow("历史条数", self.sp_history)
        self.btn_save = QPushButton("保存")
        self.btn_activate = QPushButton("激活此卡")
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

    def _reload_providers(self):
        provs = self.ctx.providers()
        ids = [p.get("id", "") for p in provs]
        for cb in (self.cb_chat_provider, self.cb_vision_provider, self.cb_classify_provider):
            cur = cb.currentData()
            cb.blockSignals(True)
            cb.clear()
            for pid in ids:
                cb.addItem(pid, pid)
            if cur in ids:
                cb.setCurrentIndex(ids.index(cur))
            cb.blockSignals(False)

    # ---------- 事件 ----------

    def _on_select(self, cur, _prev):
        if cur is None:
            return
        card = card_store.get_card(self.ctx.cards_dir, cur.data(Qt.UserRole))
        if card is None:
            return
        self._editing_id = card.get("id")
        self._reload_providers()
        self.ed_name.setText(card.get("name", ""))
        self.ed_emoji.setText(card.get("emoji", ""))
        self.ed_nick.setText(card.get("nickname", ""))
        self.ed_prompt.setPlainText(card.get("system_prompt", ""))
        self._set_cb(self.cb_chat_provider, card.get("chat_provider", ""))
        self._set_cb(self.cb_vision_provider, card.get("vision_provider", ""))
        self._set_cb(self.cb_classify_provider, card.get("classify_provider", ""))
        self._refresh_model_options()
        self.ed_chat_model.setCurrentText(card.get("chat_model", ""))
        self.ed_vision_model.setCurrentText(card.get("vision_model", ""))
        self.ed_classify_model.setCurrentText(card.get("classify_model", ""))
        self.sp_temp.setValue(float(card.get("temperature", 0.7)))
        self.sp_topp.setValue(float(card.get("top_p", 0.9)))
        self.sp_vtemp.setValue(float(card.get("vision_temp", 0.7)))
        self.sp_vmax.setValue(int(card.get("vision_max_tokens", 10000)))
        self.sp_history.setValue(int(card.get("max_history", 1000)))

    @staticmethod
    def _set_cb(cb, val):
        idx = cb.findData(val)
        cb.setCurrentIndex(idx if idx >= 0 else 0)

    def _refresh_model_options(self, *_):
        """模型下拉选项 = 对应 provider 的 models；未选 provider 时展示全部预设模型。"""
        pairs = ((self.ed_chat_model, self.cb_chat_provider),
                 (self.ed_vision_model, self.cb_vision_provider),
                 (self.ed_classify_model, self.cb_classify_provider))
        for cb, cb_prov in pairs:
            pid = cb_prov.currentData() or ""
            models = []
            for p in self.ctx.providers():
                if p.get("id") == pid:
                    models = p.get("models") or []
                    break
            if not models:
                models = sorted({m for pp in config_store.PRESET_PROVIDERS
                                 for m in pp.get("models", [])})
            cur_text = cb.currentText()
            cb.blockSignals(True)
            cb.clear()
            cb.addItems(models)
            if cur_text:
                cb.setCurrentText(cur_text)
            cb.blockSignals(False)

    def _collect(self):
        card = {
            "id": self._editing_id or "",
            "name": self.ed_name.text().strip(),
            "emoji": self.ed_emoji.text().strip(),
            "nickname": self.ed_nick.text().strip(),
            "system_prompt": self.ed_prompt.toPlainText().strip(),
            "chat_provider": self.cb_chat_provider.currentData() or "",
            "chat_model": self.ed_chat_model.currentText().strip(),
            "vision_provider": self.cb_vision_provider.currentData() or "",
            "vision_model": self.ed_vision_model.currentText().strip(),
            "classify_provider": self.cb_classify_provider.currentData() or "",
            "classify_model": self.ed_classify_model.currentText().strip(),
            "temperature": self.sp_temp.value(),
            "top_p": self.sp_topp.value(),
            "vision_temp": self.sp_vtemp.value(),
            "vision_max_tokens": self.sp_vmax.value(),
            "max_history": self.sp_history.value(),
        }
        return card

    def _new_card(self):
        self._editing_id = None
        self._reload_providers()
        self.ed_name.clear()
        self.ed_emoji.clear()
        self.ed_nick.setText("小漓")
        self.ed_prompt.clear()
        self.ed_chat_model.clearEditText()
        self.ed_vision_model.clearEditText()
        self.ed_classify_model.clearEditText()
        self._refresh_model_options()
        self.sp_temp.setValue(0.7)
        self.sp_topp.setValue(0.9)
        self.sp_vtemp.setValue(0.7)
        self.sp_vmax.setValue(10000)
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

    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["名称", "Base URL", "API Key", "模型", "ID"])
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.setColumnWidth(0, 100)
        self.table.setColumnWidth(1, 300)
        self.table.setColumnWidth(2, 200)
        self.table.setColumnWidth(3, 220)
        self.table.setColumnWidth(4, 90)
        # 单击即进入编辑（key 框可直接 Ctrl+V 粘贴，不用先敲字符）
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.SelectedClicked
            | QTableWidget.EditTrigger.EditKeyPressed
            | QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.AnyKeyPressed)
        self._probe_done.connect(self._show_probe_result)
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
        # 快捷添加主流 Provider（一键填入 base_url + 常用模型，只填 key 即可）
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("快捷添加："))
        for p in config_store.PRESET_PROVIDERS:
            b = QPushButton(p["name"])
            b.setToolTip(f"{p['base_url']}\n常用模型：{', '.join(p['models'])}")
            b.clicked.connect(lambda checked=False, pp=p: self._add_preset(pp))
            preset_row.addWidget(b)
        preset_row.addStretch(1)
        lay.addLayout(preset_row)
        # ---- 模型配置区：文字/图片模型独立选择（小白友好）----
        g_model = QGroupBox("模型配置（文字 / 图片分开选）")
        mform = QFormLayout(g_model)
        self.cmb_text_provider = QComboBox()
        self.cmb_text_model = QComboBox()
        self.cmb_text_model.setEditable(True)
        self.cmb_vision_provider = QComboBox()
        self.cmb_vision_model = QComboBox()
        self.cmb_vision_model.setEditable(True)
        row_t = QHBoxLayout()
        row_t.addWidget(self.cmb_text_provider, 1)
        row_t.addWidget(self.cmb_text_model, 2)
        row_v = QHBoxLayout()
        row_v.addWidget(self.cmb_vision_provider, 1)
        row_v.addWidget(self.cmb_vision_model, 2)
        mform.addRow("文字模型（聊天）", row_t)
        mform.addRow("图片模型（识图）", row_v)
        tip_model = QLabel("提示：DeepSeek 不支持图片识别，图片模型请选智谱/通义等支持视觉的模型。")
        tip_model.setWordWrap(True)
        tip_model.setStyleSheet("color:#9CA3AF; font-size:12px;")
        mform.addRow(tip_model)
        self.btn_model_save = QPushButton("保存模型配置")
        self.btn_model_save.clicked.connect(self._save_model_config)
        mform.addRow(self.btn_model_save)
        lay.addWidget(g_model)
        self.cmb_text_provider.currentIndexChanged.connect(
            lambda *_: self._fill_model_options(self.cmb_text_provider, self.cmb_text_model))
        self.cmb_vision_provider.currentIndexChanged.connect(
            lambda *_: self._fill_model_options(self.cmb_vision_provider, self.cmb_vision_model))
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
        for cb in (self.cmb_text_provider, self.cmb_vision_provider):
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
        self.cmb_text_model.setCurrentText(card.get("chat_model", ""))
        self._set_prov(self.cmb_vision_provider, ids, card.get("vision_provider", ""))
        self._fill_model_options(self.cmb_vision_provider, self.cmb_vision_model)
        self.cmb_vision_model.setCurrentText(card.get("vision_model", ""))

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
        card["chat_provider"] = self.cmb_text_provider.currentData() or ""
        card["chat_model"] = self.cmb_text_model.currentText().strip()
        card["vision_provider"] = self.cmb_vision_provider.currentData() or ""
        card["vision_model"] = self.cmb_vision_model.currentText().strip()
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
                    self._probe_done.emit("ok",
                                          f"可用模型（{len(names)} 个）：\n" + "\n".join(names[:20]))
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


# =====================================================================
# 任务页
# =====================================================================

class TasksPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["任务 ID", "状态", "描述", "更新时间"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.btn_refresh = QPushButton("刷新")
        self.btn_open = QPushButton("打开任务目录")
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_open.clicked.connect(self._open_dir)
        h = QHBoxLayout()
        h.addWidget(self.btn_refresh)
        h.addWidget(self.btn_open)
        h.addStretch(1)
        lay.addWidget(self.table)
        lay.addLayout(h)

    def refresh(self):
        tasks_dir = (self.ctx.cfg or {}).get("tasks_dir", "")
        self.table.setRowCount(0)
        if not tasks_dir or not os.path.isdir(tasks_dir):
            self.table.setRowCount(1)
            self.table.setItem(0, 0, QTableWidgetItem("任务目录不存在"))
            return
        names = sorted(os.listdir(tasks_dir), reverse=True)
        for name in names:
            task_dir = os.path.join(tasks_dir, name)
            if not os.path.isdir(task_dir) or name == "sent":
                continue
            tj = os.path.join(task_dir, "task.json")
            if not os.path.isfile(tj):
                continue
            try:
                with open(tj, "r", encoding="utf-8") as f:
                    info = json.load(f)
            except Exception:
                continue
            desc = str(info.get("task", ""))[:50]
            has_result = os.path.isfile(os.path.join(task_dir, "result.json"))
            state = "✅ 已完成待回传" if has_result else "⏳ 天枢处理中"
            mtime = time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(tj)))
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QTableWidgetItem(name))
            self.table.setItem(r, 1, QTableWidgetItem(state))
            self.table.setItem(r, 2, QTableWidgetItem(desc))
            self.table.setItem(r, 3, QTableWidgetItem(mtime))
        sent_dir = os.path.join(tasks_dir, "sent")
        if os.path.isdir(sent_dir):
            n = len(os.listdir(sent_dir))
            self.table.insertRow(self.table.rowCount())
            self.table.setItem(self.table.rowCount() - 1, 0, QTableWidgetItem("—"))
            self.table.setItem(self.table.rowCount() - 1, 1, QTableWidgetItem(f"已归档 {n} 个任务"))

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
        self.btn_clear.clicked.connect(self.view.clear)
        lay.addWidget(self.view)
        lay.addWidget(self.btn_clear)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(800)

    def _poll(self):
        log_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.log")
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


# =====================================================================
# 设置页
# =====================================================================

class SettingsPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        lay = QVBoxLayout(self)

        # 记忆
        g_mem = QGroupBox("对话记忆（memory.json）")
        gl = QVBoxLayout(g_mem)
        self.mem_summary = QLabel()
        self.btn_mem_view = QPushButton("查看记忆")
        self.btn_mem_clear = QPushButton("清空全部记忆")
        self.mem_view = QTextEdit()
        self.mem_view.setReadOnly(True)
        self.mem_view.setMaximumHeight(200)
        self.btn_mem_view.clicked.connect(self._view_memory)
        self.btn_mem_clear.clicked.connect(self._clear_memory)
        gl.addWidget(self.mem_summary)
        gl.addWidget(self.btn_mem_view)
        gl.addWidget(self.btn_mem_clear)
        gl.addWidget(self.mem_view)
        lay.addWidget(g_mem)

        # 图片偏移
        g_img = QGroupBox("图片点击偏移（已按常见版本校准，点击图片位置偏了再调）")
        fl = QFormLayout(g_img)
        self.sp_off_x = QSpinBox()
        self.sp_off_x.setRange(-200, 200)
        self.sp_off_y = QSpinBox()
        self.sp_off_y.setRange(-200, 200)
        fl.addRow("偏移 X", self.sp_off_x)
        fl.addRow("偏移 Y", self.sp_off_y)
        self.btn_img_save = QPushButton("保存偏移")
        self.btn_img_save.clicked.connect(self._save_image_offset)
        fl.addRow(self.btn_img_save)
        lay.addWidget(g_img)

        # 发送方式
        g_send = QGroupBox("成果文件发送方式")
        sl = QHBoxLayout(g_send)
        self.cb_send = QComboBox()
        self.cb_send.addItem("剪贴板粘贴（主用）", "clipboard")
        self.cb_send.addItem("wxauto SendFiles（兜底）", "wxauto")
        self.btn_send_save = QPushButton("保存")
        self.btn_send_save.clicked.connect(self._save_send_method)
        sl.addWidget(self.cb_send)
        sl.addWidget(self.btn_send_save)
        sl.addStretch(1)
        lay.addWidget(g_send)

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
        self.stem_view.setMaximumHeight(120)
        self.btn_stem_refresh = QPushButton("刷新")
        self.btn_stem_refresh.clicked.connect(self._refresh_stems)
        stl.addWidget(self.stem_view)
        stl.addWidget(self.btn_stem_refresh)
        lay.addWidget(g_stem)

        lay.addStretch(1)

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
        off = cfg.get("image_click_offset", [0, 0])
        self.sp_off_x.setValue(int(off[0]))
        self.sp_off_y.setValue(int(off[1]))
        idx = self.cb_send.findData(cfg.get("file_send_method", "clipboard"))
        self.cb_send.setCurrentIndex(max(0, idx))
        self.ed_files.setText(cfg.get("file_storage_path", ""))
        self.ed_tasks.setText(cfg.get("tasks_dir", ""))
        self._refresh_stems()

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

    def _save_image_offset(self):
        self.ctx.cfg["image_click_offset"] = [self.sp_off_x.value(), self.sp_off_y.value()]
        config_store.save_config(self.ctx.cfg, self.ctx.cfg_path)
        QMessageBox.information(self, "已保存", "图片偏移已保存")

    def _save_send_method(self):
        self.ctx.cfg["file_send_method"] = self.cb_send.currentData()
        config_store.save_config(self.ctx.cfg, self.ctx.cfg_path)
        QMessageBox.information(self, "已保存", "发送方式已保存")

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
