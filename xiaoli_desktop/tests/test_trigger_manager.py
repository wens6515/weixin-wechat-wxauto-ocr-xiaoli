# -*- coding: utf-8 -*-
"""触发器管理器测试：状态徽章/活跃计数纯函数 + Dialog 离屏渲染冒烟
（表格行数、状态列、操作按钮按条目状态分布、详情文本）。"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from xiaoli_app.ui.pages import count_active_triggers, trigger_state_text


class TestTriggerStateText(unittest.TestCase):
    """状态徽章全分支（纯函数）。"""

    def test_time_states(self):
        now = time.time()
        self.assertEqual(trigger_state_text(
            {"kind": "time", "enabled": True, "fire_at": now + 60}, now),
            "待触发")
        self.assertEqual(trigger_state_text(
            {"kind": "time", "enabled": True, "fire_at": now - 10}, now),
            "触发中")
        self.assertEqual(trigger_state_text(
            {"kind": "time", "enabled": False, "last_fired": now,
             "fire_at": now - 60}, now),
            "已触发")
        self.assertEqual(trigger_state_text(
            {"kind": "time", "enabled": False, "missed": True}, now),
            "已错过")
        self.assertEqual(trigger_state_text(
            {"kind": "time", "enabled": False, "fire_at": now + 3600}, now),
            "已暂停")

    def test_condition_states(self):
        self.assertEqual(trigger_state_text(
            {"kind": "condition", "enabled": True, "done": None}), "监视中")
        self.assertEqual(trigger_state_text(
            {"kind": "condition", "enabled": False, "done": None}), "已暂停")
        self.assertEqual(trigger_state_text(
            {"kind": "condition", "enabled": False, "done": "met"}), "已达成")
        self.assertEqual(trigger_state_text(
            {"kind": "condition", "enabled": False, "done": "expired"}),
            "已到期")
        self.assertEqual(trigger_state_text(
            {"kind": "condition", "enabled": False, "done": "dead"}), "已失效")


class TestCountActiveTriggers(unittest.TestCase):
    """活跃计数：启用中的定时 + 进行中的监视；终态/暂停不计。"""

    def test_counts(self):
        now = time.time()
        items = [
            {"kind": "time", "enabled": True, "fire_at": now + 60},
            {"kind": "time", "enabled": False, "last_fired": now},
            {"kind": "condition", "enabled": True, "done": None},
            {"kind": "condition", "enabled": True, "done": None},
            {"kind": "condition", "enabled": False, "done": "met"},
            {"kind": "condition", "enabled": False, "done": "dead"},
        ]
        self.assertEqual(count_active_triggers(items), 3)
        self.assertEqual(count_active_triggers([]), 0)
        self.assertEqual(count_active_triggers(None), 0)


_SMOKE_CODE = r"""
import json, os, sys, tempfile, time
os.environ["QT_QPA_PLATFORM"] = "offscreen"
HERE = os.environ["HERE"]
sys.path.insert(0, HERE)
tmp = tempfile.mkdtemp(prefix="trig_smoke_")
import xiaoli_app.reminders_store as rs
path = os.path.join(tmp, "reminders.json")
rs.default_reminders_path = lambda: path
now = time.time()
items = [
    {"id": "a1", "kind": "time", "chat": "小明", "content": "查成绩",
     "fire_at": now + 3600, "repeat": "once", "enabled": True,
     "last_fired": None, "missed": False},
    {"id": "a2", "kind": "time", "chat": "小明", "content": "",
     "fire_at": now + 7200, "repeat": "daily", "enabled": False,
     "last_fired": None, "missed": False},                       # 手动暂停
    {"id": "a3", "kind": "time", "chat": "小红", "content": "起床",
     "fire_at": now - 86400, "repeat": "once", "enabled": False,
     "last_fired": now - 86400, "missed": False},                # 已触发（终态）
    {"id": "b1", "kind": "condition", "chat": "小刚", "content": "",
     "url": "https://wx.example.com/rain", "condition": "下雨了",
     "judge": "local", "match_type": "absent",
     "met_keywords": ["雨", "降雨"], "scope_start": "今天",
     "scope_end": "明天", "interval_seconds": 60,
     "expire_at": now + 86400, "next_check_at": now, "fail_count": 2,
     "enabled": True, "done": None, "evidence": None},           # 监视中
    {"id": "b2", "kind": "condition", "chat": "小刚", "content": "",
     "url": "https://wx.example.com/rain", "condition": "降温",
     "judge": "api", "match_type": "present", "met_keywords": [],
     "interval_seconds": 60, "expire_at": now + 86400,
     "next_check_at": now, "fail_count": 0, "enabled": False,
     "done": "met", "evidence": "页面显示：气温骤降 10 度"},        # 已达成（终态）
]
with open(path, "w", encoding="utf-8") as f:
    json.dump(items, f, ensure_ascii=False)

from PySide6.QtWidgets import QApplication, QPushButton
app = QApplication([])
from xiaoli_app.ui.pages import TriggerManagerDialog, count_active_triggers

dlg = TriggerManagerDialog()
assert dlg.table.rowCount() == 5, f"行数 {dlg.table.rowCount()}"
# 操作列固定宽：ResizeToContents 不计 cellWidget 按钮内容，按钮会被压成
# 窄条显示不全（真机截图确认）——回归钉住
assert dlg.table.columnWidth(5) >= 120, dlg.table.columnWidth(5)
# 行高 36：cellWidget 塞满行高时相邻行按钮粘连遮挡（真机截图确认）
assert dlg.table.rowHeight(0) >= 34, dlg.table.rowHeight(0)

def cell(row, col):
    return dlg.table.item(row, col).text()

def buttons_at(row):
    w = dlg.table.cellWidget(row, 5)
    return sorted(b.text() for b in w.findChildren(QPushButton))

states = {cell(r, 3): buttons_at(r) for r in range(5)}
# 活跃定时：暂停 + 删除
assert any(s == "待触发" and "暂停" in b and "删除" in b
           for s, b in states.items()), states
# 手动暂停：恢复 + 删除
assert any(s == "已暂停" and "恢复" in b and "删除" in b
           for s, b in states.items()), states
# 监视中：暂停 + 删除
assert any(s == "监视中" and "暂停" in b for s, b in states.items()), states
# 终态（已触发/已达成）：只有删除
terminal = [b for s, b in states.items()
            if s in ("已触发", "已达成", "已错过", "已到期", "已失效")]
assert len(terminal) == 2, states
assert all(b == ["删除"] for b in terminal), states

# 详情：选中监视中条目（按 ID 定位行）→ 详情文本含判定方式与失败计数
for r in range(5):
    if dlg.table.item(r, 6).text() == "b1":
        dlg.table.selectRow(r)
        break
detail = dlg.txt_detail.toPlainText()
assert "本地关键词" in detail and "雨、降雨" in detail, detail
assert "连续失败：2" in detail, detail
assert "今天～明天" in detail, detail
# 终态条目详情含 evidence
for r in range(5):
    if dlg.table.item(r, 6).text() == "b2":
        dlg.table.selectRow(r)
        break
assert "气温骤降" in dlg.txt_detail.toPlainText()

# 活跃计数（徽章口径）：a1 + b1
assert count_active_triggers(rs.RemindersStore().list()) == 2
print("SMOKE_OK", flush=True)
os._exit(0)
"""


class TestTriggerManagerDialogSmoke(unittest.TestCase):
    """子进程离屏渲染冒烟（与 test_ui_pages 同模式：os._exit 绕 Qt teardown）。"""

    def test_dialog_render(self):
        env = dict(os.environ, HERE=HERE)
        proc = subprocess.run(
            [sys.executable, "-c", _SMOKE_CODE], env=env,
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0,
                         f"stdout={proc.stdout}\nstderr={proc.stderr}")
        self.assertIn("SMOKE_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
