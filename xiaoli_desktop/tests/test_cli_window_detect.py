# -*- coding: utf-8 -*-
"""天枢 CLI 窗口定位的特征判定测试（xiaoli_app.setup 内部函数）。

真机背景：新版 CLI（tianshu-tui v3.20）不再有「npm prefix」标题——Windows
Terminal 默认标题 = 运行命令行（含 `tianshu-tui\dist\main.js`），跑工具时
自动标题变成子进程名（Windows PowerShell）。历史三处坏点（本次修复锚定）：
  1. 标题特征缺 `tianshu-tui` 形态（新版认不出）
  2. 桌面端排除按 "tianshu" 一刀切（新 CLI 标题含 tianshu-tui 被误杀）
  3. 进程证据：命令行为 UTF-8 解码失败 + 只找 rivet 字样（新 CLI 无此字样）
     + `.rivet` 数据目录路径误报
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app import setup as S


# 真机实测的新版 CLI 窗口标题（WT 默认 = 命令行形态）
NEW_CLI_TITLE = (
    r'C:\WINDOWS\system32\cmd.exe  - "node"  --expose-gc --max-old-space-size=4096 '
    r'"C:\Users\wens6515\AppData\Roaming\npm\\node_modules\tianshu-tui\dist\main.js"'
)
NEW_CLI_CMDLINE = (
    r'"node"  --expose-gc --max-old-space-size=4096 '
    r'"C:\Users\wens6515\AppData\Roaming\npm\\node_modules\tianshu-tui\dist\main.js" '
)
DESKTOP_MCP_CMDLINE = (
    r"D:\AI\Tianshu\node-runtime\win-x64\node.exe "
    r"D:\AI\Tianshu\TianshuData\.rivet\mcp\docx-mcp-server.mjs"
)
LEGACY_CMDLINE = r'C:\WINDOWS\system32\cmd.exe /k title npm prefix && rivet'


class TestTitleSignature(unittest.TestCase):
    def test_new_cli_commandline_title(self):
        """新版：WT 默认标题 = 命令行（含 tianshu-tui）→ 强特征命中。"""
        self.assertTrue(S._title_has_cli_signature(NEW_CLI_TITLE))

    def test_legacy_npm_prefix_title(self):
        self.assertTrue(S._title_has_cli_signature("npm prefix"))

    def test_legacy_rivet_title(self):
        self.assertTrue(S._title_has_cli_signature("rivet"))

    def test_weak_titles_are_not_signature(self):
        """终端默认标题（跑工具时自动改写）不算强特征——须进程证据。"""
        self.assertFalse(S._title_has_cli_signature("Windows PowerShell"))
        self.assertFalse(S._title_has_cli_signature("Command Prompt"))
        self.assertFalse(S._title_has_cli_signature("命令提示符"))

    def test_decoy_npm_window_not_signature(self):
        """裸 npm 子命令窗口（真机事故「npm root」）不是 CLI 标题。"""
        self.assertFalse(S._title_has_cli_signature("npm root"))
        self.assertFalse(S._title_has_cli_signature("npm"))

    def test_dot_rivet_path_not_signature(self):
        self.assertFalse(S._title_has_cli_signature(r"D:\AI\Tianshu\TianshuData\.rivet"))


class TestDesktopExclusion(unittest.TestCase):
    def test_desktop_chinese_display_name(self):
        self.assertTrue(S._is_desktop_window("天枢 · Tianshu"))

    def test_desktop_plain_english(self):
        self.assertTrue(S._is_desktop_window("Tianshu"))

    def test_new_cli_window_is_not_desktop(self):
        """回归锚定：新 CLI 标题含 tianshu 字样但必须不被当桌面端排除。

        历史缺陷：按 "tianshu" 一刀切 → 新 CLI 窗口被整体排除 + 进程证据
        同时失效 → CLI 窗口再也找不到（无限开新窗口）。
        """
        self.assertFalse(S._is_desktop_window(NEW_CLI_TITLE))

    def test_legacy_cli_title_is_not_desktop(self):
        self.assertFalse(S._is_desktop_window("npm prefix"))
        self.assertFalse(S._is_desktop_window("rivet"))

    def test_weak_terminal_title_is_not_desktop(self):
        self.assertFalse(S._is_desktop_window("Windows PowerShell"))


class TestProcessSignature(unittest.TestCase):
    def test_new_cli_process_line(self):
        self.assertTrue(S._line_is_cli_process(NEW_CLI_CMDLINE.lower()))

    def test_legacy_rivet_process_line(self):
        self.assertTrue(S._line_is_cli_process(LEGACY_CMDLINE.lower()))

    def test_desktop_mcp_dot_rivet_line_is_not_cli(self):
        """桌面端 MCP server 命令行含 `.rivet` 数据目录 → 历史误报源，须排除。"""
        self.assertFalse(S._line_is_cli_process(DESKTOP_MCP_CMDLINE.lower()))

    def test_rivet_runtime_serve_is_not_cli(self):
        line = (r'node C:\...\rivet-runtime\main.js serve').lower()
        self.assertFalse(S._line_is_cli_process(line))

    def test_unrelated_line_is_not_cli(self):
        self.assertFalse(S._line_is_cli_process(r'c:\windows\explorer.exe'.lower()))


class TestIsCliFeature(unittest.TestCase):
    def test_strong_title_without_process_check(self):
        """标题自带签名 → 无需进程证据（pid=None 也认）。"""
        self.assertTrue(S._is_cli_feature(NEW_CLI_TITLE))
        self.assertTrue(S._is_cli_feature("npm prefix"))

    def test_weak_title_with_process_evidence(self):
        """跑工具时标题被改写成终端默认名 → 系统里有 CLI 进程则认。"""
        self.assertTrue(S._is_cli_feature("Windows PowerShell", 1234,
                                          process_has_rivet_fn=lambda pid: True))

    def test_weak_title_without_process_evidence(self):
        """用户自己开的 PowerShell（系统里没有 CLI 进程）→ 不认，防误发。"""
        self.assertFalse(S._is_cli_feature("Windows PowerShell", 1234,
                                           process_has_rivet_fn=lambda pid: False))

    def test_weak_title_process_fn_exception_fails_closed(self):
        def _boom(pid):
            raise RuntimeError("boom")
        self.assertFalse(S._is_cli_feature("Windows PowerShell", 1234,
                                          process_has_rivet_fn=_boom))

    def test_unrelated_title(self):
        self.assertFalse(S._is_cli_feature("微信"))


class TestDecodeProcessBytes(unittest.TestCase):
    """进程表输出解码：中文 Windows 的 PowerShell 重定向输出是 GBK。

    历史缺陷：text=True 按 UTF-8 解码抛异常被吞 → 进程证据恒 False。
    """

    def test_gbk_bytes_decode(self):
        text = r"18776|1|D:\工作间\node_modules\tianshu-tui\dist\main.js"
        lines = S._decode_process_bytes(text.encode("gbk"))
        self.assertEqual(len(lines), 1)
        self.assertIn("tianshu-tui", lines[0])
        self.assertIn("工作间", lines[0])

    def test_utf8_bytes_decode(self):
        text = "1|0|ok\n2|1|fine"
        self.assertEqual(S._decode_process_bytes(text.encode("utf-8")),
                         ["1|0|ok", "2|1|fine"])

    def test_broken_bytes_never_raise(self):
        lines = S._decode_process_bytes(b"1|0|\xff\xfe\xca broken")
        self.assertEqual(len(lines), 1)  # replace 兜底，绝不抛

    def test_empty_input(self):
        self.assertEqual(S._decode_process_bytes(b""), [])
        self.assertEqual(S._decode_process_bytes(None), [])


class TestConsoleClassFilter(unittest.TestCase):
    """窗口类过滤：泛化关键字 "windowclass" 曾把 10 个隐藏辅助窗口当成终端
    候选（下列 JUNK 为真机实测名单）——候选列表被垃圾占满后，弱特征标题
    （终端默认名「Windows PowerShell」）要求的「候选唯一」永远不成立，
    每次定位都 fail-closed 去开新 CLI 窗口。现只认真终端类名子串。
    """

    JUNK = (
        "NvContainerWindowClass000047E8",
        "BluetoothNotificationAreaIconWindowClass",
        "Qt6111TrayIconMessageWindowClass",
        "Qt51514WxTrayIconMessageWindowClass",
        "CrossDeviceResumeWindowClass",
        "COMTASKSWINDOWCLASS",
    )
    REAL = (
        "ConsoleWindowClass",             # conhost
        "CASCADIA_HOSTING_WINDOW_CLASS",  # Windows Terminal
        "mintty",                         # Git Bash / MSYS2
        "VirtualConsoleClass",            # ConEmu
    )

    def test_junk_helper_windows_are_not_console(self):
        for cls in self.JUNK:
            self.assertFalse(S._is_console_class(cls), cls)

    def test_real_terminal_classes_are_console(self):
        for cls in self.REAL:
            self.assertTrue(S._is_console_class(cls), cls)

    def test_empty_class_never_matches(self):
        self.assertFalse(S._is_console_class(""))
        self.assertFalse(S._is_console_class(None))


class TestPickCliWindowLogging(unittest.TestCase):
    """fail-closed 日志：走 DEBUG（前端日志页只读 INFO+ 轨的 bot_run.log），
    且同一候选签名只记一次（定位轮询每秒一次，否则刷屏）。"""

    def setUp(self):
        S._last_dup_log_key = None

    def test_fail_closed_logs_once_at_debug(self):
        cands = [("A", 1), ("B", 2)]
        with self.assertLogs("xiaoli", level="DEBUG") as cm:
            self.assertIsNone(S._pick_cli_window(cands, lambda pid: True))
            self.assertIsNone(S._pick_cli_window(cands, lambda pid: True))
        hits = [r for r in cm.output if "fail-closed" in r]
        self.assertEqual(len(hits), 1, cm.output)
        self.assertTrue(hits[0].startswith("DEBUG"), hits[0])

    def test_match_resets_dedupe_and_logs_nothing(self):
        S._last_dup_log_key = ("A", "B")
        with self.assertNoLogs("xiaoli", level="DEBUG"):
            self.assertEqual(S._pick_cli_window([("npm prefix", 1)]), "npm prefix")
        self.assertIsNone(S._last_dup_log_key)

    def test_weak_single_candidate_matches(self):
        """去掉垃圾候选后，弱特征标题 + 进程证据 + 候选唯一 → 认（这才是
        正常路径：已开着的 CLI 窗口标题被终端改写为 Windows PowerShell）。"""
        with self.assertNoLogs("xiaoli", level="DEBUG"):
            self.assertEqual(
                S._pick_cli_window([("Windows PowerShell", 8204)],
                                   lambda pid: True),
                "Windows PowerShell")


if __name__ == "__main__":
    unittest.main()
