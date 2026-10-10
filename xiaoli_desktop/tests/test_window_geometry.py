# -*- coding: utf-8 -*-
"""强制窗口几何：尺寸钉死在标定值（放不下则按工作区收口）；位置只做「出屏救援」。

钉住的定案：

- 尺寸强制：三区域比例与全部像素判据（头像窄带 = 区域宽 2%~14%、气泡/媒体闸
  同样是区域比例）都按 `WINDOW_SIZE` 那台窗口标定（**175% 显示缩放**，见
  README 系统要求）——窗口宽度一变这些常量整体错位（真机事故：窗口拉大到
  2178 宽后聊天区左边界从 0.415 挪到 0.246，消息区左沿切进聊天区 → 整列头像
  检不出、消息读不到）。`connect()` 里 force 调一次，消息监听每轮 + 每次分析
  入口复查；
- 尺寸**按工作区收口**：任务栏没隐藏的机器工作区只有 ~1516 高，1610 放不下，
  不收口就会每轮请求 1610 → 被系统截短 → 每轮复查都判"尺寸不对" → 每秒重设
  一次并作废截图（真机事故：任务栏一显示读取全废）。收口后窗口矮一截，
  由「竖向锚点分顶/底两类」接住：消息区上沿贴窗口顶、下沿贴输入面板上沿；
- 位置**不钉死**：窗口在屏幕内的位置完全由用户决定，程序只在窗口（部分）
  移出所在显示器工作区时把它拉回完整可见（出屏后 pyautogui 点击落到屏幕外，
  切会话/点图/发送全失灵）。历史缺陷：曾按「启动瞬间锚定 + 之后钉死」实现，
  启动时窗口恰好在旁边就把那个位置当成用户想要的位置，用户往右拖被拽回
  13 次（真机日志）——本文件用「可见范围内移动 → 不动手」的用例钉住这条。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wx_backend import visual_backend as vb
from wx_backend import visual_regions as vr
from wx_backend import visual_win32 as vw
from wx_backend.visual_backend import VisualBackend
from wx_backend.visual_regions import WINDOW_SIZE

W, H = WINDOW_SIZE
# 测试用工作区（1920x2200，够放 1300x1610 的窗口且留出 y 余量——真机小屏
# 会被 clamp 贴顶，单独用例覆盖）
AREA = (0, 0, 1920, 2200)


def _backend(hwnd=0x9):
    b = VisualBackend()
    b._hwnd = hwnd
    return b


class TestEnforceWindowGeometry(unittest.TestCase):
    def setUp(self):
        # 默认工作区给足高度（够放 1300x1610）→ 这些用例按"标定尺寸"语义；
        # 任务栏可见/小屏的"按工作区收口"语义由下方专用用例覆盖
        p = mock.patch.object(vb, "monitor_work_area", return_value=AREA)
        p.start()
        self.addCleanup(p.stop)

    def test_no_window_is_noop(self):
        b = VisualBackend()
        with mock.patch.object(vb, "pull_window_into_view") as pull:
            self.assertFalse(b.enforce_window_geometry(force=True))
        pull.assert_not_called()

    def test_already_correct_does_not_touch_window(self):
        b = _backend()
        with mock.patch.object(vb, "window_rect", return_value=(100, 200, W, H)), \
                mock.patch.object(vb, "pull_window_into_view",
                                  return_value=(100, 200)) as pull, \
                mock.patch.object(vb, "position_window") as pos:
            self.assertFalse(b.enforce_window_geometry(force=True))
        pos.assert_not_called()
        pull.assert_called_once_with(0x9)

    def test_moved_but_visible_not_touched(self):
        """窗口被拖到屏幕内别处（尺寸没变）→ 程序不动它（位置不钉死）。"""
        b = _backend()
        b._last_shot = object()
        with mock.patch.object(vb, "window_rect",
                               return_value=(800, 500, W, H)), \
                mock.patch.object(vb, "pull_window_into_view",
                                  return_value=(800, 500)), \
                mock.patch.object(vb, "position_window") as pos:
            self.assertFalse(b.enforce_window_geometry(force=True))
        pos.assert_not_called()
        self.assertIsNotNone(b._last_shot)     # 没动过窗口，旧帧仍有效

    def test_within_tolerance_does_not_touch_window(self):
        """渲染取整/贴边吸附（±4px 内）不触发尺寸调整。"""
        b = _backend()
        with mock.patch.object(vb, "window_rect",
                               return_value=(100, 200, W + 4, H - 4)), \
                mock.patch.object(vb, "pull_window_into_view",
                                  return_value=(100, 200)), \
                mock.patch.object(vb, "position_window") as pos:
            self.assertFalse(b.enforce_window_geometry(force=True))
        pos.assert_not_called()

    def test_wrong_size_resized_keeping_position(self):
        """尺寸不对 → 只改尺寸，左上角保持当前值（不钉位置）。"""
        b = _backend()
        b._last_shot = object()          # 旧帧应被作废
        with mock.patch.object(vb, "window_rect",
                               side_effect=[(327, 284, 2119, 1288),
                                            (327, 284, W, H)]), \
                mock.patch.object(vb, "pull_window_into_view",
                                  return_value=(327, 284)), \
                mock.patch.object(vb, "position_window",
                                  return_value=True) as pos, \
                mock.patch.object(vb.u32, "IsZoomed", return_value=False):
            changed = b.enforce_window_geometry(force=True)
        self.assertTrue(changed)
        pos.assert_called_once_with(0x9, 327, 284, W, H)   # 位置不变、尺寸归位
        self.assertIsNone(b._last_shot)

    def test_offscreen_pulled_back(self):
        """窗口被拖出屏幕 → 拉回可见范围（救援），并作废旧帧。"""
        b = _backend()
        b._last_shot = object()
        with mock.patch.object(vb, "window_rect",
                               return_value=(2000, 100, W, H)), \
                mock.patch.object(vb, "pull_window_into_view",
                                  return_value=(620, 100)) as pull, \
                mock.patch.object(vb, "position_window") as pos:
            changed = b.enforce_window_geometry(force=True)
        self.assertTrue(changed)
        pull.assert_called_once_with(0x9)
        self.assertIsNone(b._last_shot)

    def test_size_and_offscreen_single_pass(self):
        """尺寸错 + 出屏：先对齐尺寸（保持左上角），再救援位置。"""
        b = _backend()
        with mock.patch.object(vb, "window_rect",
                               side_effect=[(2100, -50, 900, 700),
                                            (2100, -50, W, H)]), \
                mock.patch.object(vb, "pull_window_into_view",
                                  return_value=(620, 0)), \
                mock.patch.object(vb, "position_window",
                                  return_value=True) as pos, \
                mock.patch.object(vb.u32, "IsZoomed", return_value=False):
            self.assertTrue(b.enforce_window_geometry(force=True))
        pos.assert_called_once_with(0x9, 2100, -50, W, H)

    def test_maximized_restored_before_resize(self):
        b = _backend()
        calls = []
        with mock.patch.object(vb, "window_rect",
                               side_effect=[(0, 0, 2560, 1400),
                                            (0, 0, W, H)]), \
                mock.patch.object(vb, "pull_window_into_view",
                                  return_value=(0, 0)), \
                mock.patch.object(vb, "position_window",
                                  return_value=True), \
                mock.patch.object(vb.u32, "IsZoomed", return_value=True), \
                mock.patch.object(vb.u32, "ShowWindow",
                                  side_effect=lambda *a: calls.append(a)), \
                mock.patch.object(vb.time, "sleep"):
            self.assertTrue(b.enforce_window_geometry(force=True))
        self.assertEqual(calls, [(0x9, 9)])   # SW_RESTORE

    def test_resize_failure_reports_false(self):
        b = _backend()
        with mock.patch.object(vb, "window_rect",
                               return_value=(1, 2, 900, 700)), \
                mock.patch.object(vb, "pull_window_into_view") as pull, \
                mock.patch.object(vb, "position_window", return_value=False), \
                mock.patch.object(vb.u32, "IsZoomed", return_value=False):
            self.assertFalse(b.enforce_window_geometry(force=True))
        pull.assert_not_called()          # 尺寸没对上就退出，本轮不救援

    def test_throttled_between_checks(self):
        """复查节流：interval 内不重复测量（0.5s 轮询下不必每轮读 rect）。"""
        b = _backend()
        with mock.patch.object(vb, "window_rect",
                               return_value=(100, 200, W, H)) as rect, \
                mock.patch.object(vb, "pull_window_into_view",
                                  return_value=(100, 200)):
            b.enforce_window_geometry(now=1000.0)          # 第一次测量
            b.enforce_window_geometry(now=1000.5)          # 节流内 → 不测量
        self.assertEqual(rect.call_count, 1)

    # ---- 任务栏可见 / 小屏：按工作区收口（真机事故回归）----

    def test_taskbar_visible_shrinks_target_to_work_area(self):
        """任务栏没隐藏（工作区 1516 高）→ 目标收口到 1516，不再请求 1610。

        旧行为：每轮请求 1610 → 系统截短到 ~1516 → 每轮复查都判"尺寸不对"
        → 每秒重设一次并作废截图 → 任务栏一显示，读取全废。"""
        b = _backend()
        with mock.patch.object(vb, "window_rect",
                               side_effect=[(1270, 0, W, H),
                                            (1270, 0, W, 1516)]), \
                mock.patch.object(vb, "monitor_work_area",
                                  return_value=(0, 0, 2560, 1516)), \
                mock.patch.object(vb, "pull_window_into_view",
                                  return_value=(1270, 0)), \
                mock.patch.object(vb, "position_window",
                                  return_value=True) as pos, \
                mock.patch.object(vb.u32, "IsZoomed", return_value=False):
            self.assertTrue(b.enforce_window_geometry(force=True))
        pos.assert_called_once_with(0x9, 1270, 0, W, 1516)

    def test_taskbar_visible_stays_stable_after_shrink(self):
        """收口后再复查不再判"尺寸不对"（不长驻"每秒重设一次"的循环）。"""
        b = _backend()
        with mock.patch.object(vb, "window_rect",
                               return_value=(1270, 0, W, 1516)), \
                mock.patch.object(vb, "monitor_work_area",
                                  return_value=(0, 0, 2560, 1516)), \
                mock.patch.object(vb, "pull_window_into_view",
                                  return_value=(1270, 0)), \
                mock.patch.object(vb, "position_window") as pos:
            self.assertFalse(b.enforce_window_geometry(force=True))
            self.assertFalse(b.enforce_window_geometry(force=True))
        pos.assert_not_called()

    def test_narrow_work_area_clamps_width(self):
        """小屏（工作区 1200 宽）→ 宽度同样收口，不请求放不下的 1300。"""
        b = _backend()
        with mock.patch.object(vb, "window_rect",
                               side_effect=[(0, 0, 900, 700),
                                            (0, 0, 1200, H)]), \
                mock.patch.object(vb, "monitor_work_area",
                                  return_value=(0, 0, 1200, 2200)), \
                mock.patch.object(vb, "pull_window_into_view",
                                  return_value=(0, 0)), \
                mock.patch.object(vb, "position_window",
                                  return_value=True) as pos, \
                mock.patch.object(vb.u32, "IsZoomed", return_value=False):
            self.assertTrue(b.enforce_window_geometry(force=True))
        pos.assert_called_once_with(0x9, 0, 0, 1200, H)

    def test_regions_follow_actual_window_height(self):
        """收口后区域按实际尺寸重算：消息区底沿跟着输入面板上沿上移。"""
        b = _backend()
        with mock.patch.object(vb, "window_rect",
                               return_value=(0, 0, W, 1516)), \
                mock.patch.object(vb, "monitor_work_area",
                                  return_value=(0, 0, 2560, 1516)), \
                mock.patch.object(vb, "pull_window_into_view",
                                  return_value=(0, 0)):
            b.enforce_window_geometry(force=True)
        msg = b._message_region
        self.assertLess(msg[3] * 1516, 1516 - 267)     # 输入面板上沿 = h-267
        self.assertAlmostEqual(msg[1] * 1516, 207.4, delta=0.5)   # 上沿钉住

    def test_checks_again_after_interval(self):
        b = _backend()
        with mock.patch.object(vb, "window_rect",
                               return_value=(100, 200, W, H)) as rect, \
                mock.patch.object(vb, "pull_window_into_view",
                                  return_value=(100, 200)):
            b.enforce_window_geometry(now=1000.0)
            b.enforce_window_geometry(now=1000.0 + vb._SIZE_CHECK_INTERVAL + 0.01)
        self.assertEqual(rect.call_count, 2)

    def test_settle_after_adjust(self):
        """调整成功后进入静置期：立刻复查会读到重绘前的过渡尺寸，白调一次。"""
        b = _backend()
        with mock.patch.object(vb, "window_rect",
                               side_effect=[(0, 0, 900, 700), (0, 0, W, H)]) as rect, \
                mock.patch.object(vb, "pull_window_into_view",
                                  return_value=(0, 0)), \
                mock.patch.object(vb, "position_window", return_value=True), \
                mock.patch.object(vb.u32, "IsZoomed", return_value=False):
            self.assertTrue(b.enforce_window_geometry(now=2000.0))
            self.assertFalse(b.enforce_window_geometry(now=2000.5))  # 静置期内
        self.assertEqual(rect.call_count, 2)   # 调整前 1 次 + 对齐后重读 1 次


class TestPullWindowIntoView(unittest.TestCase):
    """救援原语：可见内容越出工作区才拉回，否则原样返回。"""

    def test_fully_visible_untouched(self):
        with mock.patch.object(vw, "window_rect", return_value=(1260, 0, W, H)), \
                mock.patch.object(vw, "monitor_work_area",
                                  return_value=(0, 0, 2560, 1600)), \
                mock.patch.object(vw, "visible_frame_margins",
                                  return_value=(10, 0, 10, 10)), \
                mock.patch.object(vw, "position_window_visible") as pv:
            self.assertEqual(vw.pull_window_into_view(0x9), (1260, 0))
        pv.assert_not_called()

    def test_right_edge_overhang_untouched_after_margin_correction(self):
        """右贴边：窗口矩形超出屏幕（可见内容正好贴边）→ 不动（真机摆位）。"""
        with mock.patch.object(vw, "window_rect", return_value=(1270, 0, W, H)), \
                mock.patch.object(vw, "monitor_work_area",
                                  return_value=(0, 0, 2560, 1600)), \
                mock.patch.object(vw, "visible_frame_margins",
                                  return_value=(10, 0, 10, 10)), \
                mock.patch.object(vw, "position_window_visible") as pv:
            self.assertEqual(vw.pull_window_into_view(0x9), (1270, 0))
        pv.assert_not_called()

    def test_partially_offscreen_pulled_in(self):
        """半出屏（右侧看不见一截）→ 按可见内容拉回工作区内。"""
        calls = []
        with mock.patch.object(vw, "window_rect",
                               side_effect=[(1500, 0, W, H),
                                            (1260, 0, W, H)]), \
                mock.patch.object(vw, "monitor_work_area",
                                  return_value=(0, 0, 2560, 1600)), \
                mock.patch.object(vw, "visible_frame_margins",
                                  return_value=(10, 0, 10, 10)), \
                mock.patch.object(vw, "position_window_visible",
                                  side_effect=lambda *a: calls.append(a)):
            self.assertEqual(vw.pull_window_into_view(0x9), (1260, 0))
        # 可见内容 (1510,0,1280,1600) → clamp 到 (1280,0)
        self.assertEqual(calls, [(0x9, 1280, 0, 1280, 1600)])

    def test_rect_read_failure_returns_none(self):
        with mock.patch.object(vw, "window_rect", return_value=None):
            self.assertIsNone(vw.pull_window_into_view(0x9))

    def test_work_area_failure_returns_current(self):
        with mock.patch.object(vw, "window_rect", return_value=(100, 200, W, H)), \
                mock.patch.object(vw, "monitor_work_area", return_value=None), \
                mock.patch.object(vw, "position_window_visible") as pv:
            self.assertEqual(vw.pull_window_into_view(0x9), (100, 200))
        pv.assert_not_called()


class TestClampPosVisible(unittest.TestCase):
    """纯函数：可见内容拉回工作区（越界任意一边都拉回）。"""

    def test_inside_unchanged(self):
        self.assertEqual(vw.clamp_pos_visible(100, 200, W, H,
                                              (0, 0, 1920, 2200)), (100, 200))

    def test_pulled_from_left_top(self):
        self.assertEqual(vw.clamp_pos_visible(-300, -80, W, H,
                                              (0, 0, 1920, 2200)), (0, 0))

    def test_pulled_from_right_bottom(self):
        self.assertEqual(vw.clamp_pos_visible(1900, 1600, W, H,
                                              (0, 0, 1920, 2200)), (620, 590))

    def test_window_taller_than_area_pins_top(self):
        """窗口比工作区还高（小屏 + 强制尺寸）：贴顶，底部伸出不可避免。"""
        self.assertEqual(vw.clamp_pos_visible(100, 300, W, H,
                                              (0, 0, 1920, 1040)), (100, 0))

    def test_secondary_monitor_area_respected(self):
        self.assertEqual(vw.clamp_pos_visible(1300, 0, W, H,
                                              (1280, 0, 2800, 1600)),
                         (1300, 0))
        self.assertEqual(vw.clamp_pos_visible(3000, 0, W, H,
                                              (1280, 0, 2800, 1600)),
                         (1500, 0))


class TestEnforceHooks(unittest.TestCase):
    """三处钩子：connect（force）/ 监听每轮 / 分析入口。"""

    def test_connect_forces_geometry(self):
        b = VisualBackend()
        with mock.patch.object(vb, "find_wechat_window", return_value=0x9), \
                mock.patch.object(b, "enforce_window_geometry") as enf, \
                mock.patch.object(b, "_ensure_not_iconic"), \
                mock.patch.object(vb, "capture_window",
                                  return_value=__import__("PIL.Image",
                                                          fromlist=["Image"]).
                                  new("RGB", (W, H), (250, 250, 250))), \
                mock.patch.object(b, "detect_theme", return_value="light"):
            b.connect()
        enf.assert_called_once_with(force=True)

    def test_poll_entry_checks_geometry(self):
        """消息监听入口：即便这一轮没扫到红圈，也要复查几何。"""
        b = _backend()
        with mock.patch.object(b, "enforce_window_geometry") as enf, \
                mock.patch.object(b, "_ensure_not_iconic", return_value=False), \
                mock.patch.object(b, "_refresh", return_value=None):
            list(b.iter_unread_sessions())
        enf.assert_called_once()

    def test_read_entry_checks_geometry(self):
        """get_messages 入口：位置模式下不给会话名也要先复查几何。"""
        b = _backend()
        with mock.patch.object(b, "enforce_window_geometry") as enf:
            self.assertEqual(b.get_messages(None), [])
        enf.assert_called_once()

    def test_analyze_entry_checks_geometry(self):
        b = _backend()
        with mock.patch.object(b, "enforce_window_geometry") as enf, \
                mock.patch.object(b, "_refresh", return_value=None):
            win = b.analyze_window(None, assume_switched=True)
        self.assertFalse(win["has_other"])
        enf.assert_called_once()


class TestRegionsForWindow(unittest.TestCase):
    """竖向锚点分「距顶固定」与「距底固定」——窗口矮一截时读的还是聊天记录。"""

    def test_calibrated_size_matches_module_constants(self):
        """标定尺寸下与原常量逐值一致（常量是四位小数的书写形式，运行时按
        锚点像素现算，差值 < 0.05px）。"""
        computed = vr.regions_for_window(W, H)
        literal = (vr._SESSION_REGION_RATIO, vr._MESSAGE_REGION_RATIO,
                   vr._TITLE_REGION_RATIO)
        for got, want in zip(computed, literal):
            for g, v in zip(got, want):
                self.assertAlmostEqual(g, v, delta=1e-4,
                                       msg=f"{got} vs {want}")

    def test_short_window_keeps_top_anchors(self):
        """窗口矮一截：会话区/消息区**上沿**与标题带都不动（贴窗口顶）。"""
        s1, m1, t1 = vr.regions_for_window(W, H)
        s2, m2, t2 = vr.regions_for_window(W, 1516)
        for a, b, h2 in ((s1[1], s2[1], 1516), (m1[1], m2[1], 1516),
                         (t1[1], t2[1], 1516), (t1[3], t2[3], 1516)):
            self.assertAlmostEqual(a * H, b * h2, delta=0.5)

    def test_short_window_message_region_avoids_input_box(self):
        """矮窗口下消息区底沿仍停在输入面板上沿之上（真机事故回归）。"""
        h = 1516
        _, m, _ = vr.regions_for_window(W, h)
        self.assertAlmostEqual(m[3] * h, h - 267.7, delta=0.5)
        self.assertLess(m[3] * h, h - 267)          # 面板上沿 = h-267
        self.assertGreater(m[3] * h, h - 320)       # 也别留出过大空隙

    def test_short_window_session_region_follows_bottom(self):
        h = 1516
        s, _, _ = vr.regions_for_window(W, h)
        self.assertAlmostEqual(s[3] * h, h - 16.9, delta=0.5)

    def test_tiny_window_rects_stay_valid(self):
        """极端矮窗口（比底锚偏移还矮）也必须给出 l<r、t<b 的合法矩形。"""
        for h in (600, 300, 200):
            for r in vr.regions_for_window(W, h):
                self.assertLess(r[0], r[2], f"h={h} {r}")
                self.assertLess(r[1], r[3], f"h={h} {r}")
                self.assertTrue(all(0.0 <= v <= 1.0 for v in r), f"h={h} {r}")

    def test_invalid_size_falls_back_to_constants(self):
        self.assertEqual(vr.regions_for_window(0, 0),
                         (vr._SESSION_REGION_RATIO, vr._MESSAGE_REGION_RATIO,
                          vr._TITLE_REGION_RATIO))


class TestInputBoxRect(unittest.TestCase):
    """输入框聚焦点：距窗口底固定（不再按窗口高比例，矮窗口下不漏到消息区）。"""

    def _rect(self, h, cal=None):
        b = _backend()
        with mock.patch.object(b, "_load_input_box_calibration",
                               return_value=cal), \
                mock.patch.object(vb, "window_rect",
                                  return_value=(100, 0, W, h)):
            b._apply_regions((W, h))
            return b._input_box_rect()

    def test_center_keeps_constant_distance_from_bottom(self):
        for h in (H, 1516, 1200):
            rect = self._rect(h)
            cy = rect[1] + rect[3] // 2
            self.assertEqual(cy, h - 149)
            self.assertGreater(cy, h - 267)        # 在输入面板内
            self.assertLess(cy, h - 39)

    def test_calibration_fx_used_for_x(self):
        rect = self._rect(H, cal={"fx": 0.8, "fy": 0.9})
        self.assertEqual(rect[0] + rect[2] // 2, 100 + int(W * 0.8))

    def test_default_x_is_message_region_center(self):
        rect = self._rect(H)
        mr = vr._MESSAGE_REGION_RATIO
        self.assertEqual(rect[0] + rect[2] // 2,
                         100 + int(W * (mr[0] + mr[2]) / 2))

    def test_no_window_returns_none(self):
        self.assertIsNone(VisualBackend()._input_box_rect())


if __name__ == "__main__":
    unittest.main()
