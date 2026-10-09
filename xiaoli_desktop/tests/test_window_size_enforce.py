# -*- coding: utf-8 -*-
"""强制窗口尺寸：程序把微信窗口钉在标定尺寸上（用户自定义尺寸已撤回）。

钉住的定案：

- 三区域比例与全部像素判据（头像窄带 = 区域宽 2%~14%、期望头像高 = 区域高÷18、
  气泡/媒体闸同样是区域比例）都按 `WINDOW_SIZE` 那台窗口标定——窗口尺寸一变
  这些常量整体错位（真机事故：窗口拉大后聊天区左边界从 0.415 挪到 0.246，
  消息区左沿切进聊天区 → 整列头像检不出、消息读不到）。所以程序强制：
  `connect()` 里 force 调一次，消息监听每轮 + 每次分析入口复查；
- 只改大小不动位置（保持左上角）；最大化先 SW_RESTORE 再改；容差内不折腾；
  调整后进入静置期（等微信重绘，避免与用户拖动打成一串）；
- 调整成功即作废上一帧截图（尺寸变了，旧帧的坐标系不能用）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wx_backend import visual_backend as vb
from wx_backend.visual_backend import VisualBackend
from wx_backend.visual_regions import WINDOW_SIZE

W, H = WINDOW_SIZE


def _backend(hwnd=0x9):
    b = VisualBackend()
    b._hwnd = hwnd
    return b


class TestEnforceWindowSize(unittest.TestCase):
    def test_no_window_is_noop(self):
        b = VisualBackend()
        with mock.patch.object(vb, "position_window") as pos:
            self.assertFalse(b.enforce_window_size(force=True))
        pos.assert_not_called()

    def test_already_correct_does_not_touch_window(self):
        b = _backend()
        with mock.patch.object(vb, "window_rect", return_value=(100, 200, W, H)), \
                mock.patch.object(vb, "position_window") as pos:
            self.assertFalse(b.enforce_window_size(force=True))
        pos.assert_not_called()

    def test_within_tolerance_does_not_touch_window(self):
        """渲染取整/贴边吸附（±4px 内）不触发调整。"""
        b = _backend()
        with mock.patch.object(vb, "window_rect",
                               return_value=(100, 200, W + 4, H - 4)), \
                mock.patch.object(vb, "position_window") as pos:
            self.assertFalse(b.enforce_window_size(force=True))
        pos.assert_not_called()

    def test_wrong_size_resized_keeping_topleft(self):
        b = _backend()
        b._last_shot = object()          # 旧帧应被作废
        with mock.patch.object(vb, "window_rect",
                               return_value=(327, 284, 2119, 1288)), \
                mock.patch.object(vb, "position_window",
                                  return_value=True) as pos, \
                mock.patch.object(vb.u32, "IsZoomed", return_value=False):
            changed = b.enforce_window_size(force=True)
        self.assertTrue(changed)
        pos.assert_called_once_with(0x9, 327, 284, W, H)   # 位置不变、尺寸归位
        self.assertIsNone(b._last_shot)

    def test_maximized_restored_before_resize(self):
        b = _backend()
        calls = []
        with mock.patch.object(vb, "window_rect",
                               return_value=(0, 0, 2560, 1400)), \
                mock.patch.object(vb, "position_window",
                                  return_value=True), \
                mock.patch.object(vb.u32, "IsZoomed", return_value=True), \
                mock.patch.object(vb.u32, "ShowWindow",
                                  side_effect=lambda *a: calls.append(a)), \
                mock.patch.object(vb.time, "sleep"):
            self.assertTrue(b.enforce_window_size(force=True))
        self.assertEqual(calls, [(0x9, 9)])   # SW_RESTORE

    def test_resize_failure_reports_false(self):
        b = _backend()
        with mock.patch.object(vb, "window_rect",
                               return_value=(1, 2, 900, 700)), \
                mock.patch.object(vb, "position_window", return_value=False), \
                mock.patch.object(vb.u32, "IsZoomed", return_value=False):
            self.assertFalse(b.enforce_window_size(force=True))

    def test_throttled_between_checks(self):
        """复查节流：interval 内不重复测量（0.5s 轮询下不必每轮读 rect）。"""
        b = _backend()
        with mock.patch.object(vb, "window_rect",
                               return_value=(100, 200, W, H)) as rect:
            b.enforce_window_size(now=1000.0)          # 第一次测量
            b.enforce_window_size(now=1000.5)          # 节流内 → 不测量
        self.assertEqual(rect.call_count, 1)

    def test_checks_again_after_interval(self):
        b = _backend()
        with mock.patch.object(vb, "window_rect",
                               return_value=(100, 200, W, H)) as rect:
            b.enforce_window_size(now=1000.0)
            b.enforce_window_size(now=1000.0 + vb._SIZE_CHECK_INTERVAL + 0.01)
        self.assertEqual(rect.call_count, 2)

    def test_settle_after_resize(self):
        """调整成功后进入静置期：立刻复查会读到重绘前的过渡尺寸，白调一次。"""
        b = _backend()
        with mock.patch.object(vb, "window_rect",
                               return_value=(0, 0, 900, 700)) as rect, \
                mock.patch.object(vb, "position_window", return_value=True), \
                mock.patch.object(vb.u32, "IsZoomed", return_value=False):
            self.assertTrue(b.enforce_window_size(now=2000.0))
            self.assertFalse(b.enforce_window_size(now=2000.5))  # 静置期内
        self.assertEqual(rect.call_count, 1)


class TestEnforceHooks(unittest.TestCase):
    """三处钩子：connect（force）/ 监听每轮 / 分析入口。"""

    def test_connect_forces_size(self):
        b = VisualBackend()
        with mock.patch.object(vb, "find_wechat_window", return_value=0x9), \
                mock.patch.object(b, "enforce_window_size") as enf, \
                mock.patch.object(b, "_ensure_not_iconic"), \
                mock.patch.object(vb, "capture_window",
                                  return_value=__import__("PIL.Image",
                                                          fromlist=["Image"]).
                                  new("RGB", (W, H), (250, 250, 250))), \
                mock.patch.object(b, "detect_theme", return_value="light"):
            b.connect()
        enf.assert_called_once_with(force=True)

    def test_poll_entry_checks_size(self):
        """消息监听入口：即便这一轮没扫到红圈，也要复查尺寸。"""
        b = _backend()
        with mock.patch.object(b, "enforce_window_size") as enf, \
                mock.patch.object(b, "_ensure_not_iconic", return_value=False), \
                mock.patch.object(b, "_refresh", return_value=None):
            list(b.iter_unread_sessions())
        enf.assert_called_once()

    def test_read_entry_checks_size(self):
        """get_messages 入口：位置模式下不给会话名也要先复查尺寸。"""
        b = _backend()
        with mock.patch.object(b, "enforce_window_size") as enf:
            self.assertEqual(b.get_messages(None), [])
        enf.assert_called_once()

    def test_analyze_entry_checks_size(self):
        b = _backend()
        with mock.patch.object(b, "enforce_window_size") as enf, \
                mock.patch.object(b, "_refresh", return_value=None):
            win = b.analyze_window(None, assume_switched=True)
        self.assertFalse(win["has_other"])
        enf.assert_called_once()


if __name__ == "__main__":
    unittest.main()
