# -*- coding: utf-8 -*-
"""控制面板六个页面：状态 / 角色卡 / 模型 / 任务 / 日志 / 设置。"""
import json
import os
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QFormLayout,
    QListWidget, QListWidgetItem, QLineEdit, QPlainTextEdit, QTextEdit,
    QTableWidget, QTableWidgetItem, QComboBox, QDoubleSpinBox, QSpinBox,
    QFileDialog, QMessageBox, QGroupBox, QGridLayout, QCheckBox,
)

from xiaoli_app import card_store, config_store


def _key_display(key):
    """显示用 key：遮蔽中间。"""
    return config_store.mask_key(key)


# =====================================================================
# 状态页
# =====================================================================

class StatusPage(QWidget):
    def __init__(self, ctx, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        lay = QVBoxLayout(self)
        self.lbl_state = QLabel("引擎未启动")
        self.lbl_wx = QLabel("微信连接：-")
        self.lbl_card = QLabel("活跃角色卡：-")
        self.lbl_model = QLabel("聊天模型：-  视觉模型：-")
        self.btn_pause = QPushButton("暂停回复")
        self.btn_pause.clicked.connect(self.toggle_pause)
        lay.addWidget(self.lbl_state)
        lay.addWidget(self.lbl_wx)
        lay.addWidget(self.lbl_card)
        lay.addWidget(self.lbl_model)
        lay.addWidget(self.btn_pause)
        lay.addStretch(1)

    def refresh(self):
        eng = self.ctx.engine
        if eng is None:
            self.lbl_state.setText("引擎未启动")
            self.btn_pause.setEnabled(False)
            return
        bot = eng.bot
        if eng.is_alive() and bot is not None:
            if bot.paused:
                self.lbl_state.setText("引擎运行中（已暂停回复）")
                self.btn_pause.setText("恢复回复")
            else:
                self.lbl_state.setText("引擎运行中")
                self.btn_pause.setText("暂停回复")
            self.lbl_wx.setText(f"微信连接：{'已连接' if getattr(bot, 'wx', None) is not None else '连接中…'}")
        else:
            self.lbl_state.setText("引擎启动中…")
            self.btn_pause.setText("暂停回复")
        self.btn_pause.setEnabled(True)
        card = card_store.get_card(self.ctx.cards_dir, self.ctx.active_card_id())
        if card:
            self.lbl_card.setText(f"活跃角色卡：{card.get('name', '?')} {card.get('emoji', '')}")
            self.lbl_model.setText(
                f"聊天模型：{card.get('chat_model', '-')}  视觉模型：{card.get('vision_model', '-')}")
        else:
            self.lbl_card.setText("活跃角色卡：-")
            self.lbl_model.setText("聊天模型：-  视觉模型：-")

    def toggle_pause(self):
        eng = self.ctx.engine
        if eng is None or eng.bot is None:
            return
        if eng.bot.paused:
            eng.resume()
        else:
            eng.pause()


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
        self.ed_chat_model = QLineEdit()
        self.ed_vision_model = QLineEdit()
        self.ed_classify_model = QLineEdit()
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
        self.ed_chat_model.setText(card.get("chat_model", ""))
        self.ed_vision_model.setText(card.get("vision_model", ""))
        self.ed_classify_model.setText(card.get("classify_model", ""))
        self.sp_temp.setValue(float(card.get("temperature", 0.7)))
        self.sp_topp.setValue(float(card.get("top_p", 0.9)))
        self.sp_vtemp.setValue(float(card.get("vision_temp", 0.7)))
        self.sp_vmax.setValue(int(card.get("vision_max_tokens", 10000)))
        self.sp_history.setValue(int(card.get("max_history", 1000)))

    @staticmethod
    def _set_cb(cb, val):
        idx = cb.findData(val)
        cb.setCurrentIndex(idx if idx >= 0 else 0)

    def _collect(self):
        card = {
            "id": self._editing_id or "",
            "name": self.ed_name.text().strip(),
            "emoji": self.ed_emoji.text().strip(),
            "nickname": self.ed_nick.text().strip(),
            "system_prompt": self.ed_prompt.toPlainText().strip(),
            "chat_provider": self.cb_chat_provider.currentData() or "",
            "chat_model": self.ed_chat_model.text().strip(),
            "vision_provider": self.cb_vision_provider.currentData() or "",
            "vision_model": self.ed_vision_model.text().strip(),
            "classify_provider": self.cb_classify_provider.currentData() or "",
            "classify_model": self.ed_classify_model.text().strip(),
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
        self.ed_chat_model.clear()
        self.ed_vision_model.clear()
        self.ed_classify_model.clear()
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
            try:
                import requests
                models_url = url.replace("/chat/completions", "/models")
                resp = requests.get(models_url, headers={"Authorization": f"Bearer {key}"}, timeout=10)
                if resp.status_code == 200:
                    names = [m.get("id", "") for m in resp.json().get("data", [])]
                    QMessageBox.information(self, "连接成功",
                                            f"可用模型（{len(names)} 个）：\n" + "\n".join(names[:20]))
                else:
                    QMessageBox.warning(self, "连接失败", f"HTTP {resp.status_code}: {resp.text[:200]}")
            except Exception as e:
                QMessageBox.warning(self, "连接失败", str(e))

        threading.Thread(target=probe, daemon=True).start()


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
        g_img = QGroupBox("图片点击偏移（竖图点击偏位校准）")
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
