# -*- coding: utf-8 -*-
"""接收链路：几何定位，**全程零 OCR**，名字交给切过去之后的联合 OCR。

用户定案的 OCR 预算（一次处理事件）：
    iter_unread_sessions → 红圈像素定位 + 点击 + 像素复验   0 次 OCR
    analyze_window(assume_switched=True)                    0 次 OCR
    get_messages(assume_switched=True) 联合区（标题带+消息区） 1 次 OCR ← 唯一一次

所以 `iter_unread_sessions` 不得读标题、不得读列表：它只产出**位置条目**
（点击点屏幕坐标），会话名由下游联合 OCR 刷新到 `_current_title` 后取用。

真机依据：红圈 y 与选中行可像素判定（probe_geo_judge）、点击落点 +45px 命中
（probe_geo_click）、read_title 单次 145~151ms（measure_ocr_budget）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

from wx_backend import visual_backend as vb_mod
from wx_backend.visual_backend import VisualBackend

WIN_W, WIN_H = 1300, 1610
SESSION_REGION = (0.09, 0.09, 0.42, 0.99)
ROWS = {163: "林小满", 294: "“摸鱼”集团"}


class _World:
    """自洽的微信世界：点击换选中（标题随后由联合 OCR 侧提供）。"""

    def __init__(self, badges, selected_y=None, click_moves=True):
        self.badges = badges
        self.selected_y = selected_y
        self.click_moves = click_moves
        self.clicks = []

    def is_row_selected(self, y):
        return self.selected_y is not None and abs(y - self.selected_y) < 40

    def click(self, x, y):
        self.clicks.append((x, y))
        if self.click_moves:
            for ry in ROWS:
                if abs(y - ry) < 40:
                    self.selected_y = ry


def make_backend(world, selected_color=(13, 168, 105)):
    b = VisualBackend.__new__(VisualBackend)
    b._hwnd = 12345
    b._session_region = SESSION_REGION
    b._selected_row_color = selected_color
    b._badge_coords = {}
    b._session_coords = {}
    b._current_chat = None
    b._last_shot = None
    b._ensure_not_iconic = lambda: False
    b._refresh = lambda force=False, foreground=False: Image.new(
        "RGB", (WIN_W, WIN_H), (47, 47, 48))
    b._is_row_selected = world.is_row_selected
    b._foreground = lambda: None
    # 不在此处设置实例级 read_title：否则会盖掉测试里 patch 的类方法，
    # "有没有读标题"就记录不到了。
    return b


class TestUnreadZeroOcr(unittest.TestCase):
    def _run(self, world, **kw):
        b = make_backend(world, **kw)
        title_calls = []
        with mock.patch.object(vb_mod, "_detect_red_clusters",
                               return_value=list(world.badges)), \
                mock.patch.object(vb_mod, "ocr_image") as ocr, \
                mock.patch.object(vb_mod, "u32"), \
                mock.patch("pyautogui.click",
                           side_effect=lambda x, y: world.click(x, y)), \
                mock.patch("time.sleep"), \
                mock.patch.object(VisualBackend, "read_title",
                                  side_effect=lambda foreground=False:
                                  title_calls.append(foreground) or None):
            entries = list(b.iter_unread_sessions())
        return b, entries, ocr, title_calls

    def test_zero_ocr_and_zero_title_reads(self):
        """★核心：接收链路一次 OCR 都不做，也不读标题（名字交给联合 OCR）。"""
        w = _World([(187, 150, 212, 176)], selected_y=294)
        _b, entries, ocr, titles = self._run(w)
        self.assertEqual(ocr.call_count, 0, "接收链路不得调用 ocr_image")
        self.assertEqual(titles, [], "接收链路不得读标题区（那是联合 OCR 的活）")
        self.assertEqual(entries, [(199, 163)],
                         "应产出红圈中心位置条目（名字由下游联合 OCR 给出）")

    def test_no_click_when_row_already_selected(self):
        """红圈所在行已是选中行 → 零点击，直接产出位置。"""
        w = _World([(187, 150, 212, 176)], selected_y=163)
        _b, entries, _o, _t = self._run(w)
        self.assertEqual(entries, [(199, 163)])
        self.assertEqual(w.clicks, [], "已在目标会话时不得点击")

    def test_clicks_and_verifies_by_pixel(self):
        """未选中 → 点击该行，并用像素复验选中转移（零 OCR）。"""
        w = _World([(187, 150, 212, 176)], selected_y=294)
        _b, entries, _o, _t = self._run(w)
        self.assertEqual(len(w.clicks), 1)
        self.assertEqual(w.clicks[0], (199 + vb_mod._BADGE_CLICK_OFFSET_X, 163))
        self.assertEqual(entries, [(199, 163)])

    def test_yields_nothing_when_selection_did_not_move(self):
        """点击后选中未转移（点击落空）→ 不产出（fail-closed）。"""
        w = _World([(187, 150, 212, 176)], selected_y=294, click_moves=False)
        _b, entries, _o, _t = self._run(w)
        self.assertEqual(entries, [], "选中未转移时不得产出条目")

    def test_same_row_duplicate_badges_yielded_once(self):
        """同一行的两个红圈簇 → 只产出一个位置条目（按行归并，不靠名字）。"""
        w = _World([(187, 150, 212, 176), (188, 152, 211, 178)], selected_y=163)
        _b, entries, _o, _t = self._run(w)
        self.assertEqual(len(entries), 1)

    def test_multiple_rows_each_yielded(self):
        """多未读行：各自产出位置（各自点击取材）。"""
        w = _World([(187, 150, 212, 176), (187, 281, 212, 307)], selected_y=None)
        _b, entries, _o, _t = self._run(w)
        self.assertEqual(len(entries), 2)
        self.assertEqual([y for _x, y in entries], [163, 294])

    def test_no_badges_no_work(self):
        """没有红圈：零 OCR、零点击、零产出。"""
        w = _World([], selected_y=163)
        _b, entries, ocr, titles = self._run(w)
        self.assertEqual(entries, [])
        self.assertEqual(ocr.call_count, 0)
        self.assertEqual(titles, [])
        self.assertEqual(w.clicks, [])


if __name__ == "__main__":
    unittest.main()
