# -*- coding: utf-8 -*-
"""初始化失败情景测试：缩放检测原语 / 环境检查缩放项 / 失败诊断数据源。

钉住的定案：
- 显示缩放非 175% = 环境配置不满足（BackendConfigError）：连接立即失败、
  不进重试循环，错误消息带缩放值与修复路径——把「连上了但读不到消息」的
  隐性故障变成显式可操作的错误（用户定案）；
- 检测不出缩放（0）fail-open：检测手段失效不拦人；
- 初始化失败必弹诊断弹窗：原始错误 + 现场检测（缩放/微信窗口）+ 修复
  指引；缩放不符时指引恒定在场（engine_error_detail）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestDpiToScalePct(unittest.TestCase):
    """DPI → 缩放百分比换算（Windows 标准档位恰为整百分比）。"""

    def test_standard_steps(self):
        from wx_backend.visual_win32 import _dpi_to_scale_pct
        self.assertEqual(_dpi_to_scale_pct(96), 100)
        self.assertEqual(_dpi_to_scale_pct(120), 125)
        self.assertEqual(_dpi_to_scale_pct(144), 150)
        self.assertEqual(_dpi_to_scale_pct(168), 175)
        self.assertEqual(_dpi_to_scale_pct(192), 200)

    def test_bad_input(self):
        from wx_backend.visual_win32 import _dpi_to_scale_pct
        self.assertEqual(_dpi_to_scale_pct(0), 0)
        self.assertEqual(_dpi_to_scale_pct(None), 0)


class TestCheckEnvScaleItem(unittest.TestCase):
    """环境检查的显示缩放项：首页在初始化之前就把缩放要求亮出来。"""

    def _check(self, scale, hwnd=0x1234):
        from xiaoli_app import setup
        with mock.patch("wx_backend.visual_win32.find_wechat_window",
                        return_value=hwnd), \
             mock.patch("wx_backend.visual_win32.monitor_dpi_scale",
                        return_value=scale):
            return setup.check_environment({})

    def test_scale_175_ok(self):
        rep = self._check(175)
        self.assertTrue(rep["scale"]["ok"])
        self.assertIn("175%", rep["scale"]["detail"])

    def test_scale_wrong_is_bad_with_fix_path(self):
        rep = self._check(150)
        self.assertFalse(rep["scale"]["ok"])
        self.assertIn("150%", rep["scale"]["detail"])
        self.assertIn("175%", rep["scale"]["detail"])

    def test_scale_unknown_neutral(self):
        """检测不出（0）：中性展示（ok=None），不判坏也不判好。"""
        rep = self._check(0)
        self.assertIsNone(rep["scale"]["ok"])


class _Engine:
    def __init__(self, error):
        self.error = error


class _Ctx:
    def __init__(self, engine):
        self.engine = engine


class TestEngineErrorDetail(unittest.TestCase):
    """初始化失败诊断（前端失败弹窗的数据源）。"""

    def _detail(self, error, scale=175, wechat_found=True):
        from xiaoli_app.webbridge import BridgeApi
        bridge = BridgeApi(_Ctx(_Engine(error)))
        with mock.patch("wx_backend.visual_win32.find_wechat_window",
                        return_value=0x1234 if wechat_found else None), \
             mock.patch("wx_backend.visual_win32.monitor_dpi_scale",
                        return_value=scale):
            return bridge.engine_error_detail()

    def test_scale_wrong_hint_always_present(self):
        """缩放不符 → 指引恒定在场（哪怕错误本身与缩放无关）。"""
        r = self._detail("初始化失败: 未找到微信主窗口（请确认微信已登录并打开）",
                         scale=150, wechat_found=False)
        self.assertTrue(r["ok"])
        self.assertEqual(r["scale"], 150)
        self.assertFalse(r["wechat_found"])
        self.assertTrue(any("175%" in h for h in r["hints"]), r["hints"])
        self.assertTrue(any("微信" in h for h in r["hints"]), r["hints"])

    def test_scale_error_carries_fix_path(self):
        """错误本身是缩放问题且现场仍非 175% → 指引带设置路径。"""
        r = self._detail("初始化失败: 微信所在显示器的显示缩放为 150%…",
                         scale=150)
        self.assertTrue(any("缩放与布局" in h for h in r["hints"]), r["hints"])

    def test_scale_error_already_fixed_guides_retry(self):
        """错误是缩放问题、现场已读到 175%（用户改好了还没重试）→ 不再教
        改设置，直接引导重试。"""
        r = self._detail("初始化失败: 微信所在显示器的显示缩放为 150%…",
                         scale=175)
        self.assertTrue(any("重试" in h for h in r["hints"]), r["hints"])
        self.assertFalse(any("缩放与布局" in h for h in r["hints"]),
                         r["hints"])

    def test_scale_fine_no_scale_hint(self):
        """缩放正确 → 不硬塞缩放指引（弹窗只给与错误相关的动作）。"""
        r = self._detail("初始化失败: 未找到微信主窗口（请确认微信已登录并打开）")
        self.assertEqual(r["scale"], 175)
        self.assertFalse(any("缩放" in h for h in r["hints"]), r["hints"])

    def test_generic_fallback_hint(self):
        r = self._detail("初始化失败: PrintWindow 截图失败")
        self.assertTrue(r["hints"])
        self.assertTrue(any("重试" in h or "重启" in h for h in r["hints"]),
                        r["hints"])


if __name__ == "__main__":
    unittest.main()
