# -*- coding: utf-8 -*-
"""OCR 预算：已知窗口已切好时，analyze_window 不得再切一次。

背景：一次处理事件本应只有**一次** OCR —— get_messages 的**联合 OCR（标题带
+ 消息区一次读）**。两处冗余去掉后预算落到 1 次（真机实测见
.rivet/scratch/verify_one_ocr.py）：
    ① iter_unread_sessions 取材不再读标题（改名 _click_badge_row，只点击 +
       像素复验，零 OCR）
    ② analyze_window 不再切（本文件测的这条）

② 的依据已经存在：红圈几何链路刚点击该行并用像素复验过"该行已选中"
（_click_badge_row / _is_row_selected），没有理由再切一次。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wx_backend.visual_backend import VisualBackend


def make_backend(shot=None):
    b = VisualBackend.__new__(VisualBackend)
    b._hwnd = 1
    b._message_region = (0.42, 0.13, 0.99, 0.83)
    b._current_is_group = False
    b._current_chat = None
    b._switch_calls = []
    b._switch_chat = lambda chat, force=False: (
        b._switch_calls.append((chat, force)) or True)
    b._refresh = lambda force=False, foreground=True: shot
    b._ensure_not_iconic = lambda: False
    return b


class TestAnalyzeWindowAssumeSwitched(unittest.TestCase):
    def test_assume_switched_skips_switch(self):
        """assume_switched=True（红圈链路刚切过）→ 不得再有非 force 的切换。"""
        b = make_backend(shot=None)
        b.analyze_window("王文生", assume_switched=True)
        plain = [c for c in b._switch_calls if c[1] is False]
        self.assertEqual(plain, [],
                         "已切好的窗口不应再切一次（省一次点击 + 一次标题 OCR）")

    def test_default_still_switches(self):
        """默认行为不变：没有 assume_switched 时首个动作仍是切到目标会话。"""
        b = make_backend(shot=None)
        b.analyze_window("王文生")
        self.assertEqual(b._switch_calls[0], ("王文生", False),
                         "默认行为：首个动作仍是切会话")

    def test_assume_switched_retry_still_has_toggle_net(self):
        """assume_switched 只省首切；分析为空时的 force 重切兜底必须保留
        （toggle 取消选中的防线不能因为提速被拆掉）。"""
        b = make_backend(shot=None)          # 两次都分析失败 → 触发 attempt=1
        b.analyze_window("王文生", assume_switched=True)
        forces = [c for c in b._switch_calls if c[1] is True]
        self.assertEqual(len(forces), 1,
                         "第二次 attempt 必须仍带 force 重切（toggle 兜底）")


if __name__ == "__main__":
    unittest.main()
