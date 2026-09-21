# -*- coding: utf-8 -*-
"""会话切换的「已选中」判定：标题是权威信号。

真机缺陷（用户实测复现）：小漓在「王文生」回复完消息后，用户**手动**切到
「“强盗”集团」。王文生再发新消息时，bot 认为"已经在王文生会话里"→ 不点击
→ 后续读到的是「“强盗”集团」的消息（窗口实际停在那儿）。

真机探针（.rivet/scratch/probe_switch_state.py）实测：
    标题 read_title() = '“强盗”集团(5)'    ← 权威：微信停在强盗集团
    '王文生' 行像素 (47,47,48)  → _is_row_selected = False   ← 像素判定正确
    '“强盗”集团' 行像素 (13,168,105) → True                  ← 只有当前会话高亮

即像素级判定没错，是 `_switch_chat(force=False)` 的**第三级兜底**把标题读到的
否定证据盖掉了：`_current_chat` 只在点击成功时更新，用户手动切换微信不会通知
进程，于是它永远停在旧会话名上，导致"已在目标会话"的误判。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wx_backend.visual_backend import VisualBackend

TARGET = "王文生"
OTHER_TITLE = "“强盗”集团(5)"


def make_backend(titles, highlight=False, current="王文生"):
    """VisualBackend 桩：不连微信，只装配 _switch_chat 判定所需的字段。"""
    b = VisualBackend.__new__(VisualBackend)
    b._hwnd = 1
    b._current_chat = current
    b._session_coords = {TARGET: (300, 179)}
    b._badge_coords = {}
    b._selected_row_color = (13, 168, 105)
    seq = list(titles)

    def _read_title(foreground=False):
        return seq.pop(0) if seq else TARGET

    b.read_title = _read_title
    b._is_row_selected = lambda y: highlight
    b._learn_selected_row_color = lambda y: None
    b._foreground = lambda: None
    return b


class TestSwitchChatToggleGuard(unittest.TestCase):
    def _click_count(self, backend):
        with mock.patch("pyautogui.click") as click, \
                mock.patch("time.sleep"):
            ok = backend._switch_chat(TARGET, force=False)
        return ok, click.call_count

    def test_stale_memory_must_not_skip_click_when_title_is_other(self):
        """★ 真机缺陷回归：标题显示别的会话时，即便内存状态还停在目标名，
        也必须点击切换（内存状态会因用户手动切换而过期）。"""
        b = make_backend([OTHER_TITLE, TARGET], highlight=False,
                         current=TARGET)   # 过期的 _current_chat
        ok, clicks = self._click_count(b)
        self.assertTrue(ok)
        self.assertEqual(clicks, 1,
                         "标题已明确显示别的会话，必须点击切换（不得用过期内存状态跳过）")

    def test_title_matches_skips_click(self):
        """标题就是目标会话 → 不点击（防 toggle 取消选中，消息区变空）。"""
        b = make_backend([TARGET], highlight=True, current=TARGET)
        ok, clicks = self._click_count(b)
        self.assertTrue(ok)
        self.assertEqual(clicks, 0, "已在目标会话时不得点击")

    def test_title_variant_matches_skips_click(self):
        """OCR 变体是同一会话 → 不点击。

        真机形态差异：列表区那条读成「强盗"集团」（前引号被吃掉），标题区读
        成「“强盗”集团(5)」。严格相等会判成"不在目标会话"→ 白点一下反而
        toggle 取消选中、消息区变空。"""
        variant = '强盗"集团'
        b = make_backend([OTHER_TITLE], highlight=False, current="")
        b._session_coords = {variant: (300, 294)}
        with mock.patch("pyautogui.click") as click, mock.patch("time.sleep"):
            ok = b._switch_chat(variant, force=False)
        self.assertTrue(ok)
        self.assertEqual(click.call_count, 0,
                         "归一化后同一会话（仅引号变体）不得点击")

    def test_no_title_falls_back_to_highlight(self):
        """标题读不到（窗口被遮挡）→ 退回像素高亮判定，仍然防 toggle。"""
        b = make_backend([None, None], highlight=True)
        ok, clicks = self._click_count(b)
        self.assertTrue(ok)
        self.assertEqual(clicks, 0, "高亮命中时应判为已选中，不点击")

    def test_no_title_no_highlight_uses_memory(self):
        """标题读不到 + 像素不命中 → 才允许退回内存状态兜底。"""
        b = make_backend([None, None], highlight=False, current=TARGET)
        ok, clicks = self._click_count(b)
        self.assertTrue(ok)
        self.assertEqual(clicks, 0, "标题读不到时保留内存兜底")

    def test_no_title_other_memory_clicks(self):
        """标题读不到 + 像素不命中 + 内存状态不是目标 → 必须点击。"""
        b = make_backend([None, None], highlight=False, current="杨冬梅")
        ok, clicks = self._click_count(b)
        self.assertTrue(ok)
        self.assertEqual(clicks, 1, "内存状态也非目标时应点击切换")


if __name__ == "__main__":
    unittest.main()
