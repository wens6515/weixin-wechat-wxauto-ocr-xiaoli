# -*- coding: utf-8 -*-
"""任务状态扫描公共函数测试（CLI task-status 与 GUI 任务页共用）。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_bot import dispatch_task, scan_task_status


class TestScanTaskStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="task_status_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_dir(self):
        entries, waiting, done, archived = scan_task_status(self.tmp)
        self.assertEqual((entries, waiting, done, archived), ([], 0, 0, 0))

    def test_missing_dir(self):
        entries, waiting, done, archived = scan_task_status(
            os.path.join(self.tmp, "nope"))
        self.assertEqual((entries, waiting, done, archived), ([], 0, 0, 0))

    def test_waiting_done_archived(self):
        tid1 = dispatch_task(self.tmp, {"chat_name": "x", "task": "做PPT"}, None)
        tid2 = dispatch_task(self.tmp, {"chat_name": "x", "task": "做网站"}, None)
        with open(os.path.join(self.tmp, tid2, "result.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"status": "success"}, f)
        entries, waiting, done, archived = scan_task_status(self.tmp)
        self.assertEqual((waiting, done, archived), (1, 1, 0))
        states = {n: s for n, s, _d, _m in entries}
        self.assertEqual(states[tid1], "waiting")
        self.assertEqual(states[tid2], "done")
        # 归档后：归档任务以 state="archived" 进入 entries（任务页列出 +
        # 删除按钮的数据源），不再计入 waiting
        shutil.move(os.path.join(self.tmp, tid1),
                    os.path.join(self.tmp, "sent", tid1))
        entries, waiting, done, archived = scan_task_status(self.tmp)
        self.assertEqual((waiting, done, archived), (0, 1, 1))
        by_name = {n: (s, d) for n, s, d, _m in entries}
        self.assertEqual(by_name[tid1][0], "archived")
        self.assertEqual(by_name[tid1][1], "做PPT",
                         "归档任务带 task.json 描述")
        # sent 目录本身不是条目
        self.assertNotIn("sent", [n for n, _s, _d, _m in entries])

    def test_archived_dir_without_task_json_listed(self):
        """归档目录缺 task.json（异常残留）→ 仍列出（ID 可见可删），描述为空。"""
        sent = os.path.join(self.tmp, "sent", "20260101000000dead")
        os.makedirs(sent)
        entries, _w, _d, archived = scan_task_status(self.tmp)
        self.assertEqual(archived, 1)
        self.assertEqual([(n, s, d) for n, s, d, _m in entries],
                         [("20260101000000dead", "archived", "")])

    def test_non_task_dir_skipped(self):
        os.makedirs(os.path.join(self.tmp, "attachments"))
        entries, waiting, done, archived = scan_task_status(self.tmp)
        self.assertEqual((entries, waiting, done), ([], 0, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
