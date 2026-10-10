# -*- coding: utf-8 -*-
"""天枢配置引导（任务桥首次开启）：setup 公开入口 + webbridge 三个引导方法
+ 首启引导默认关任务桥。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app import setup


class TestSetupHelpers(unittest.TestCase):
    def test_detect_rivet_prefers_rivet_then_cmd(self):
        with mock.patch("shutil.which", side_effect=lambda n: {
                "rivet": r"C:\npm\rivet", "rivet.cmd": r"C:\npm\rivet.cmd"}.get(n)):
            self.assertEqual(setup.detect_rivet(), r"C:\npm\rivet")
        with mock.patch("shutil.which", side_effect=lambda n: {
                "rivet.cmd": r"C:\npm\rivet.cmd"}.get(n)):
            self.assertEqual(setup.detect_rivet(), r"C:\npm\rivet.cmd")

    def test_detect_rivet_absent(self):
        with mock.patch("shutil.which", return_value=None):
            self.assertIsNone(setup.detect_rivet())

    def test_find_cli_window_delegates(self):
        with mock.patch.object(setup, "_find_npm_prefix_window",
                               return_value="npm prefix") as f:
            self.assertEqual(setup.find_cli_window(), "npm prefix")
        f.assert_called_once_with()

    def test_guide_prompt_text_public_alias(self):
        self.assertEqual(setup.GUIDE_PROMPT_TEXT, setup._GUIDE_PROMPT_TEXT)


class _BridgeBase(unittest.TestCase):
    def setUp(self):
        from xiaoli_app.webbridge import BridgeApi

        class _Ctx:
            cfg = {}
            cfg_path = "unused.json"
            cards_dir = "unused"
            engine = None

        self.bridge = BridgeApi(_Ctx())
        p = mock.patch("xiaoli_app.webbridge.config_store.save_config")
        self.save = p.start()
        self.addCleanup(p.stop)


class TestGuideInfo(_BridgeBase):
    def test_reports_installed_window_and_guided(self):
        with mock.patch.object(setup, "detect_rivet", return_value=r"C:\npm\rivet"):
            with mock.patch.object(setup, "find_cli_window", return_value="npm prefix"):
                self.bridge.ctx.cfg["tianshu_guided"] = True
                r = self.bridge.tianshu_guide_info()
        self.assertTrue(r["ok"])
        self.assertTrue(r["installed"])
        self.assertEqual(r["window"], "npm prefix")
        self.assertTrue(r["guided"])
        self.assertIn("API key", r["prompt_text"])

    def test_reports_missing_cli(self):
        with mock.patch.object(setup, "detect_rivet", return_value=None):
            with mock.patch.object(setup, "find_cli_window", return_value=None):
                r = self.bridge.tianshu_guide_info()
        self.assertTrue(r["ok"])
        self.assertFalse(r["installed"])
        self.assertIn("tianshu-tui", r["detail"])
        self.assertFalse(r["guided"])


class TestGuideOpen(_BridgeBase):
    def test_open_ok(self):
        with mock.patch.object(setup, "launch_tianshu",
                               return_value=(True, "已启动")) as lt:
            r = self.bridge.tianshu_guide_open()
        self.assertTrue(r["ok"])
        self.assertEqual(r["detail"], "已启动")
        lt.assert_called_once_with(self.bridge.ctx.cfg)

    def test_open_failure_passthrough(self):
        with mock.patch.object(setup, "launch_tianshu",
                               return_value=(False, "未找到 rivet 命令")):
            r = self.bridge.tianshu_guide_open()
        self.assertFalse(r["ok"])
        self.assertIn("rivet", r["detail"])


class TestGuideFinish(_BridgeBase):
    """「我已配置完成」= 只发 /yes + 关窗（与引导文案承诺一致）。

    真机反馈：这里曾先发一遍首轮提示词（832 字任务桥协议），用户看到一大段
    文本被粘进刚配好的 CLI，以为程序发错了。首轮提示词归属「引擎初始化完成
    时后台自动发一次」+ 首页「重发一次」；而本流程紧接着关窗（进程树被杀），
    在这里发等于发进一个马上被销毁的会话。"""

    def test_finish_sends_yes_only_then_marks_guided(self):
        with mock.patch.object(setup, "find_cli_window", return_value="npm prefix"):
            with mock.patch.object(setup, "build_first_prompt") as bf:
                with mock.patch.object(setup, "send_prompt_to_tianshu") as sp:
                    with mock.patch.object(setup, "send_yes_and_close",
                                           return_value=True) as sy:
                        r = self.bridge.tianshu_guide_finish()
        self.assertTrue(r["ok"])
        self.assertTrue(self.bridge.ctx.cfg["tianshu_guided"])
        self.save.assert_called_once()
        sy.assert_called_once_with("npm prefix")     # 只发 /yes + 关窗
        sp.assert_not_called()                        # 不发首轮提示词
        bf.assert_not_called()

    def test_finish_without_window_errors(self):
        with mock.patch.object(setup, "find_cli_window", return_value=None):
            with mock.patch.object(setup, "send_yes_and_close") as sy:
                with mock.patch.object(setup, "send_prompt_to_tianshu") as sp:
                    r = self.bridge.tianshu_guide_finish()
        self.assertFalse(r["ok"])
        self.assertIn("未检测到天枢 CLI 窗口", r["error"])
        sy.assert_not_called()
        sp.assert_not_called()
        self.assertNotIn("tianshu_guided", self.bridge.ctx.cfg)

    def test_finish_send_failure_not_marked(self):
        with mock.patch.object(setup, "find_cli_window", return_value="npm prefix"):
            with mock.patch.object(setup, "send_yes_and_close",
                                   return_value=False):
                r = self.bridge.tianshu_guide_finish()
        self.assertFalse(r["ok"])
        self.assertNotIn("tianshu_guided", self.bridge.ctx.cfg)
        self.save.assert_not_called()


class TestFirstRunDefaultsTaskBridge(_BridgeBase):
    """新装默认关任务桥：天枢未配置前开着，模型会投递注定失败的任务。"""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(setup, "ensure_bridge_readme")
        p.start()
        self.addCleanup(p.stop)

    def test_first_run_writes_task_bridge_off(self):
        r = self.bridge.save_first_run("D:\\tasks", "D:\\files",
                                       "memory.json", "小漓")
        self.assertTrue(r["ok"])
        self.assertFalse(self.bridge.ctx.cfg["task_enabled"])

    def test_existing_explicit_value_kept(self):
        self.bridge.ctx.cfg["task_enabled"] = True
        self.bridge.save_first_run("D:\\tasks", "D:\\files",
                                   "memory.json", "小漓")
        self.assertTrue(self.bridge.ctx.cfg["task_enabled"])


if __name__ == "__main__":
    unittest.main()
