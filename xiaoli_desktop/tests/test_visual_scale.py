# -*- coding: utf-8 -*-
"""跨缩放口径表（visual_scale）与半屏窗口几何测试。

钉住的定案（150% 真机实测验证，tools/scale_probe.py；用户定案见下）：
- 微信布局按**逻辑像素**固定：分界列、头像、面板、行距的物理尺寸
  = 逻辑值 × 缩放（150% 实测：分界 464=541×6/7、头像 54=63×6/7）；
- 全部判据在 175% 档真机标定 → 任意档位下长度 ×k、面积 ×k²、颜色不变；
- **窗口物理尺寸恒为「工作区右半屏」，跨档位不变**（用户定案；175% 时代
  的 1300×1610 即 2560×1600 屏的右半）。判据锚点因此全部是「距窗口边缘
  的固定逻辑距离 ×k」，绝不按窗口比例——比例会在低档位把分界列算进聊天
  区、把滚动条留在消息区（150% 实测翻车）；
- 未实测档位缺省 175（k=1）：判据与标定值逐字节相同，既有基线零漂移。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wx_backend import visual_scale as vs
from wx_backend.visual_regions import (WINDOW_SIZE, regions_for_window,
                                       target_window_size)


class ScaleCase(unittest.TestCase):
    """口径表是全局状态：每个用例前后都回标定档，杜绝跨文件泄漏。"""

    def setUp(self):
        vs.set_active_scale(vs.CALIB_SCALE)

    def tearDown(self):
        vs.set_active_scale(vs.CALIB_SCALE)


class TestScalePrimitives(ScaleCase):
    def test_default_is_calibration(self):
        self.assertEqual(vs.active_scale(), 175)
        self.assertEqual(vs.k(), 1.0)
        self.assertEqual(vs.pxr(63), 63)
        self.assertEqual(vs.area(100), 100.0)

    def test_150_scaling(self):
        vs.set_active_scale(150)
        self.assertAlmostEqual(vs.k(), 150 / 175.0)
        self.assertEqual(vs.pxr(63), 54)          # 头像高（150% 实测 54）
        self.assertEqual(vs.pxr(541), 464)        # 会话/聊天分界（实测 464）
        self.assertAlmostEqual(vs.area(1000), 734.7, places=1)

    def test_set_active_scale_invalid_keeps_previous(self):
        for bad in (0, None, "abc", 10):
            self.assertEqual(vs.set_active_scale(bad), 175)
        self.assertEqual(vs.set_active_scale(150), 150)
        self.assertEqual(vs.set_active_scale(0), 150)
        vs.set_active_scale(vs.CALIB_SCALE)

    def test_pxr_floor(self):
        vs.set_active_scale(100)
        self.assertEqual(vs.pxr(1), 1)            # 极小值不缩成 0


class TestTargetWindowSize(unittest.TestCase):
    """窗口目标 = 工作区右半（物理，跨档位不变）；只钉尺寸不钉位置。"""

    def test_half_of_work_area(self):
        self.assertEqual(target_window_size((0, 0, 2560, 1528)), (1280, 1528))
        self.assertEqual(target_window_size((0, 0, 2600, 1610)), (1300, 1610))
        self.assertEqual(target_window_size((0, 0, 3840, 2160)), (1920, 2160))

    def test_invalid_area_falls_back_to_calibration(self):
        self.assertEqual(target_window_size(None), WINDOW_SIZE)
        self.assertEqual(target_window_size((0, 0, 0, 0)), WINDOW_SIZE)


class TestRegionsForWindow(ScaleCase):
    def test_calibrated_ratios_at_175(self):
        """175% 标定窗口下与书写常量逐值一致（锚点像素是常量的来源）。"""
        computed = regions_for_window(1300, 1610, 175)
        literal = ((0.09, 0.0878, 0.418, 0.9895),
                   (0.4165, 0.1288, 0.9913, 0.8337),
                   (0.4151, 0.0386, 0.8128, 0.082))
        for got, want in zip(computed, literal):
            for g, v in zip(got, want):
                self.assertAlmostEqual(g, v, delta=1e-4)

    def test_horizontal_edges_scale_from_window_edges(self):
        """横向锚点 = 距窗口左/右缘固定逻辑距离 ×k（半屏窗口宽度不随档位
        变，比例必然随档位变——150% 实测分界在 464 而非 0.4165×1280）。"""
        w, h = 1280, 1528
        vs.set_active_scale(150)
        (sl, st, sr, sb), (ml, mt, mr, mb), _ = regions_for_window(w, h, 150)
        self.assertAlmostEqual(sl * w, 117.0 * 150 / 175.0, delta=0.6)
        # 会话区右沿 = 分界列（贴左缘锚定）：150% 实测 464，绝不能按右缘锚
        self.assertAlmostEqual(sr * w, 543.4 * 150 / 175.0, delta=0.6)
        self.assertAlmostEqual(ml * w, 541.45 * 150 / 175.0, delta=0.6)
        self.assertAlmostEqual((1 - mr) * w, 11.31 * 150 / 175.0, delta=0.6)
        # 竖向锚点同样 ×k（150% 矮一截的面板/标题）
        self.assertAlmostEqual(mt * h, 207.4 * 150 / 175.0, delta=0.6)
        self.assertAlmostEqual((1 - mb) * h, 267.7 * 150 / 175.0, delta=0.6)

    def test_short_window_bottom_anchor_from_bottom(self):
        """矮窗口收口：底锚点距底固定（逻辑值），随档位换算。"""
        vs.set_active_scale(150)
        _, m, _ = regions_for_window(1280, 1300, 150)
        self.assertAlmostEqual(m[3] * 1300, 1300 - 267.7 * 150 / 175.0,
                               delta=0.6)

    def test_invalid_size_falls_back_to_ratios(self):
        r = regions_for_window(0, 0, 150)
        self.assertEqual(r[0][0], 0.09)

    def test_tiny_window_rects_stay_valid(self):
        for h in (600, 300, 200):
            for r in regions_for_window(1280, h, 150):
                self.assertLess(r[0], r[2], f"h={h} {r}")
                self.assertLess(r[1], r[3], f"h={h} {r}")


class TestScaledCriteria(ScaleCase):
    def test_avatar_height_gate_follows_tier(self):
        """头像高度闸随档位换算：26px 段在 150% 档是头像（63×0.857×0.45≈24），
        在 175% 档过不了下限（63×0.45≈28）。"""
        import numpy as np
        from PIL import Image
        from wx_backend.visual_vision import detect_avatar_tops

        def _shot(avatar_h):
            arr = np.full((400, 400, 3), 30, dtype=np.uint8)
            arr[100:100 + avatar_h, 20:60] = (200, 60, 60)
            return Image.fromarray(arr)

        vs.set_active_scale(150)
        self.assertEqual(detect_avatar_tops(_shot(26), (30, 30, 31), "left"),
                         [100])
        vs.set_active_scale(175)
        self.assertEqual(detect_avatar_tops(_shot(26), (30, 30, 31), "left"),
                         [])

    def test_avatar_core_erosion_at_100(self):
        """100% 回归（真机 diag_msg.png 段型复刻）：36px 头像里简笔画强像素
        核只剩 19 行（细线被抗锯齿侵蚀，125% 核占 73%、100% 剩 53%），0.7
        高度闸整列卡掉 → 读 0 条。下限放宽到 0.45×期望后 19 行段须过闸；
        贴区域上缘的半截头像（滚动裁断）仍须丢弃。"""
        import numpy as np
        from PIL import Image
        from wx_backend.visual_vision import detect_avatar_tops

        arr = np.full((400, 400, 3), 250, dtype=np.uint8)
        arr[100:119, 20:60] = (120, 100, 80)   # 19 行核，peak 40
        vs.set_active_scale(100)
        self.assertEqual(
            detect_avatar_tops(Image.fromarray(arr), (250, 250, 250), "left"),
            [100], "侵蚀后的 19 行核应是头像")

        arr2 = np.full((400, 400, 3), 250, dtype=np.uint8)
        arr2[0:19, 20:60] = (120, 100, 80)     # 同样 19 行但贴上缘 = 裁断
        self.assertEqual(
            detect_avatar_tops(Image.fromarray(arr2), (250, 250, 250), "left"),
            [], "贴区域上缘的半截头像不得放行")

    def test_avatar_gap_merge_at_125(self):
        """125% 回归（真机 diag_msg.png 段型复刻）：浅色简笔画面像下采样后，
        强像素行中间掉出稀疏行，把一整段劈成 3+2+26——主段 26 行卡在高度闸
        （0.7×45≈31）外，对方头像全灭 → 消息块无法锚定 → 读 0 条（用户实测
        125% 完全读不到新消息的根因）。段扫描须把 ≤2 行缺口并回同段。"""
        import numpy as np
        from PIL import Image
        from wx_backend.visual_vision import detect_avatar_tops

        arr = np.full((400, 400, 3), 250, dtype=np.uint8)
        # 行 100~102 强（6px）、103 稀疏、104~105 强（3px）、106 稀疏、
        # 107~132 强（40px）——与真机 3+2+26 同型
        for y0, y1, w in ((100, 103, 6), (104, 106, 3), (107, 133, 40)):
            arr[y0:y1, 20:20 + w] = (120, 100, 80)
        vs.set_active_scale(125)
        self.assertEqual(
            detect_avatar_tops(Image.fromarray(arr), (250, 250, 250), "left"),
            [100], "稀疏缺口应并回同段，整头像只出一个 top")

        # >2 行缺口仍是真间隙：两个不足下限的碎段都不得放行
        arr2 = np.full((400, 400, 3), 250, dtype=np.uint8)
        arr2[100:118, 20:60] = (120, 100, 80)
        arr2[121:139, 20:60] = (120, 100, 80)
        self.assertEqual(
            detect_avatar_tops(Image.fromarray(arr2), (250, 250, 250), "left"),
            [], "3 行缺口应照旧分段，碎段过不了高度闸")

    def test_badge_area_bounds_follow_tier(self):
        """红圈面积闸随档位 k²：900px 簇在 175% 档在界内（60~1000），
        150% 档上限 735 → 出局。"""
        import numpy as np
        from PIL import Image
        from wx_backend.visual_badge import _detect_red_clusters

        def _shot():
            arr = np.full((400, 400, 3), 250, dtype=np.uint8)
            arr[100:130, 100:130] = (250, 81, 81)   # 30x30 = 900px
            return Image.fromarray(arr)

        vs.set_active_scale(175)
        self.assertEqual(len(_detect_red_clusters(_shot())), 1)
        vs.set_active_scale(150)
        self.assertEqual(len(_detect_red_clusters(_shot())), 0)

    def test_capsule_threshold_scales(self):
        """胶囊绿阈值 150 是面积量：k² 换算后 150% 档约 110。"""
        vs.set_active_scale(150)
        self.assertAlmostEqual(vs.area(150), 110.2, places=1)

    def test_anchor_tolerance_floor_at_100(self):
        """100% 回归：面板顶与头像强像素核顶差 7~9 物理px（图形内边距+细线
        抗锯齿侵蚀，不随档位缩小），纯 ×k 容差在 100% 缩到 5 兜不住 → 回退
        区间桶 → 两张文件卡片互相错认领（文件名行被当发送者名吞掉）。
        容差须有 10 物理px 下限。"""
        from wx_backend.visual_vision import _anchor_avatar

        vs.set_active_scale(100)
        tops = [100, 200]
        self.assertEqual(_anchor_avatar(107, tops), 100,
                         "核顶下方 7px 的框应就近归属本头像")
        self.assertEqual(_anchor_avatar(193, tops), 200,
                         "下一头像核顶附近的框不得落进上一个头像的区间桶")


def _boundary_window(w, h, boundary_x):
    """合成窗口截图：左侧会话列表（241 底 + 深色文本行）、分界空谷、右侧
    聊天区（250 底 + 一个灰色气泡封住谷右口）——空谷扫描的目标形态。"""
    import numpy as np
    from PIL import Image

    arr = np.full((h, w, 3), 250, dtype=np.uint8)
    arr[:, :boundary_x] = (241, 241, 243)
    for y in range(int(h * 0.2), int(h * 0.7), 60):
        arr[y:y + 24, 20:max(30, boundary_x - 20)] = (60, 60, 62)
    y0 = int(h * 0.60)
    arr[y0:y0 + int(h * 0.10), boundary_x + 46:boundary_x + 346] = (238, 238, 240)
    return Image.fromarray(arr)


class TestBoundaryDetection(ScaleCase):
    def test_compressed_list_boundary_measured_at_200(self):
        """200% 回归：半屏窗口 1280 物理 = 640 逻辑 < 标定 731，微信压缩
        会话列——分界列实测 ~556 而非静态锚 543.4×1.143=621（真机 200%：
        静态锚偏进聊天区把头像裁出检测带 → 整列漏检）。空谷扫描须测出
        压缩后的分界。"""
        from wx_backend.visual_vision import detect_boundary_x

        vs.set_active_scale(200)
        img = _boundary_window(1280, 1504, 554)
        bx = detect_boundary_x(img)
        self.assertIsNotNone(bx, "压缩列表的空谷应被扫出")
        self.assertLessEqual(abs(bx - 556), 4, f"分界实测值偏移过大: {bx}")

    def test_uniform_image_returns_none(self):
        """纯色截图（异常帧/黑图）扫不出空谷 → None，调用方回退静态锚。"""
        import numpy as np
        from PIL import Image
        from wx_backend.visual_vision import detect_boundary_x

        img = Image.fromarray(np.full((800, 1280, 3), 250, dtype=np.uint8))
        self.assertIsNone(detect_boundary_x(img))

    def test_regions_boundary_override(self):
        """boundary_px 覆盖分界三沿：session 右沿与 message/title 左沿 =
        实测值/窗口宽；None 时维持静态锚 543.4×k。"""
        s, m, t = regions_for_window(1280, 1504, 200, boundary_px=556)
        self.assertAlmostEqual(s[2], 556 / 1280, places=3)
        self.assertAlmostEqual(m[0], 556 / 1280, places=3)
        self.assertAlmostEqual(t[0], 556 / 1280, places=3)
        s0, m0, _ = regions_for_window(1280, 1504, 200)
        self.assertAlmostEqual(s0[2], 543.4 * 200 / 175 / 1280, places=3)
        self.assertAlmostEqual(m0[0], 541.45 * 200 / 175 / 1280, places=3)

    def test_backend_measure_boundary_refits_regions(self):
        """_measure_boundary：截图尺寸变化才重扫；命中即覆盖 _boundary_x 并
        重算三区域（消息区左沿贴实测分界）；扫不出回退静态锚。"""
        from wx_backend.visual_backend import VisualBackend

        b = VisualBackend()
        self.assertIsNone(b._boundary_x)
        vs.set_active_scale(200)
        b._measure_boundary(_boundary_window(1280, 1504, 554))
        self.assertEqual(b._boundary_x, 556)
        self.assertAlmostEqual(b._message_region[0] * 1280, 556, delta=2,
                               msg="消息区左沿应贴实测分界列")
        # 同尺寸再测不重扫；换尺寸且扫不出 → 回退静态锚
        b._measure_boundary(_boundary_window(1280, 1504, 554))
        self.assertEqual(b._boundary_x, 556)
        b._measure_boundary(_boundary_window(1000, 800, 400))
        self.assertEqual(b._boundary_x, 402)
        b._measure_boundary(_boundary_window(900, 700, 0))  # 0 宽列表 = 纯色
        self.assertIsNone(b._boundary_x)
        self.assertAlmostEqual(b._message_region[0] * 900,
                               541.45 * 200 / 175, delta=2)


if __name__ == "__main__":
    unittest.main()
