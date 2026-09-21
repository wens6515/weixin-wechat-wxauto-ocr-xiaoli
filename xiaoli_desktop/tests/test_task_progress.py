# -*- coding: utf-8 -*-
"""任务进度回传语义：progress.json 每被**新写一次**就回传一次。

旧实现有三重闸门（投递后前 120s 静默 / 阶段文本必须变化 / 每 600s 最多一条），
天枢写了进度但小漓可能整条吞掉。现要求：只要天枢新写了 progress.json，
就回传一份进度（不再有时间限制与"文本必须变化"的限制）。

"新写"的判定 = 文件 mtime 变化 **或** stage 文本变化——二者任一即视为
新写入（保守：宁可多发一次，也不漏发天枢刚写的进度）。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_bot import AgentBot

CHAT = "林小满"
STAGE = "正在跑单元测试"


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def make_bot(tmp):
    bot = AgentBot.__new__(AgentBot)   # 不跑 __init__（会连微信）
    bot.tasks_dir = tmp
    bot._progress_state = {}
    bot.sent = []
    bot._send_trigger_reply = lambda text, chat: bot.sent.append((chat, text))
    return bot


class TestProgressForwarding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="progress_")
        self.bot = make_bot(self.tmp)
        self.tdir = os.path.join(self.tmp, "task_1")
        os.makedirs(self.tdir, exist_ok=True)
        write_json(os.path.join(self.tdir, "task.json"), {"chat_name": CHAT})
        self.progress = os.path.join(self.tdir, "progress.json")

    def progress_write(self, stage, mtime=None, extra=None):
        """写 progress.json，并（可选）把 mtime 钉到指定值。"""
        write_json(self.progress, {"stage": stage})
        if mtime is not None:
            os.utime(self.progress, (mtime, mtime))
        elif extra is not None:
            os.utime(self.progress, (extra, extra))

    def test_first_sight_sends_immediately(self):
        """刚投递就写进度 → 立刻回传（不再有 120s 静默期）。"""
        self.progress_write(STAGE, mtime=1000.0)
        self.bot._poll_task_progress()
        self.assertEqual(self.bot.sent, [(CHAT, STAGE)],
                         "首次发现的进度应立即回传，无静默期")

    def test_rewrite_same_stage_sends_again(self):
        """天枢用同一段文本重写 progress.json（mtime 更新）→ 再回传一次。"""
        self.progress_write(STAGE, mtime=1000.0)
        self.bot._poll_task_progress()
        self.progress_write(STAGE, mtime=1001.0)      # 同文本，新写入
        self.bot._poll_task_progress()
        self.assertEqual(len(self.bot.sent), 2,
                         "同文本重写也是一次新写入，应再回传")

    def test_two_quick_writes_both_reported(self):
        """两次写入间隔远小于旧的最小间隔（600s）→ 两次都回传。"""
        self.progress_write("阶段一", mtime=1000.0)
        self.bot._poll_task_progress()
        self.progress_write("阶段二", mtime=1002.0)   # 仅隔 2 秒
        self.bot._poll_task_progress()
        self.assertEqual([t for _c, t in self.bot.sent], ["阶段一", "阶段二"])

    def test_unchanged_file_not_resent(self):
        """文件没被重新写过（轮询重复读到同一份）→ 不重复回传。"""
        self.progress_write(STAGE, mtime=1000.0)
        self.bot._poll_task_progress()
        self.bot._poll_task_progress()
        self.bot._poll_task_progress()
        self.assertEqual(len(self.bot.sent), 1, "未变更的文件不得反复回传")

    def test_text_change_with_same_mtime_still_sent(self):
        """mtime 精度不足但文本变了 → 仍视为新写入（保守多发不漏发）。"""
        self.progress_write("阶段一", mtime=1000.0)
        self.bot._poll_task_progress()
        self.progress_write("阶段二", mtime=1000.0)   # mtime 相同
        self.bot._poll_task_progress()
        self.assertEqual([t for _c, t in self.bot.sent], ["阶段一", "阶段二"])

    def test_result_json_stops_progress(self):
        """result.json 出现后进度停发（成果回传归 _poll_outbox）。"""
        self.progress_write(STAGE, mtime=1000.0)
        write_json(os.path.join(self.tdir, "result.json"), {"status": "success"})
        self.bot._poll_task_progress()
        self.assertEqual(self.bot.sent, [])

    def test_empty_stage_not_sent(self):
        """stage 为空（占位/写坏了）→ 不发，也不占状态。"""
        self.progress_write("   ", mtime=1000.0)
        self.bot._poll_task_progress()
        self.assertEqual(self.bot.sent, [])

    def test_finished_task_state_cleaned(self):
        """任务目录消失 → 该任务的进度状态被清理（不泄漏）。"""
        self.progress_write(STAGE, mtime=1000.0)
        self.bot._poll_task_progress()
        self.assertIn("task_1", self.bot._progress_state)
        for root, _dirs, files in os.walk(self.tdir, topdown=False):
            for fn in files:
                os.remove(os.path.join(root, fn))
        os.rmdir(self.tdir)
        self.bot._poll_task_progress()
        self.assertEqual(self.bot._progress_state, {})

    def test_missing_progress_file_skipped(self):
        """没有 progress.json 的任务不发（也不建状态）。"""
        self.bot._poll_task_progress()
        self.assertEqual(self.bot.sent, [])
        self.assertEqual(self.bot._progress_state, {})


if __name__ == "__main__":
    unittest.main()
