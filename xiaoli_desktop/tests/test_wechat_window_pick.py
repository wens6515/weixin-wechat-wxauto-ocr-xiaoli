# -*- coding: utf-8 -*-
"""微信主窗口识别：**进程名优先、标题严格相等兜底**。

钉住的定案（真机事故：仪表盘把浏览器标签标题当"检测到微信窗口"上屏——用户
开着本仓库的 GitHub 页面，标签标题里含"微信 PC 4.x"；运行期 find_wechat_window
用的是同款宽松判据，还可能照浏览器窗口截图）：

- 进程名 Weixin.exe（微信 4.x）/ WeChat.exe（3.x）**精确相等** → 认。不用子串：
  WeChatAppEx.exe（小程序宿主，同样持有窗口）必须排除在外；
- 标题**严格等于**「微信」/「WeChat」→ 进程名读不到时兜底认；
- 标题只是"含"微信二字（浏览器标签、文档窗口）→ 一律不认；
- 同组多候选：先取标题严格命中的（主窗口），再按面积取最大者
  （Weixin.exe 同时持有若干辅助窗，辅助窗更小）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wx_backend.visual_win32 import pick_wechat_window


def _w(hwnd, title, proc, area):
    """候选窗口元组：(hwnd, title, proc_name 小写, area)。"""
    return (hwnd, title, proc, area)


class TestPickWechatWindow(unittest.TestCase):
    def test_process_name_wins_over_browser_title(self):
        """浏览器标签里含"微信"二字（进程是浏览器）不得压过真微信窗口。"""
        cands = [
            _w(0x1, "wens6515/weixin-wechat-wxauto-ocr-xiaoli: 微信 PC 4.x"
                   "（适配最新版 4.1.15.13）AI 机器人 - 豆包浏览器", "doubao.exe",
               1_600_000),
            _w(0x2, "微信", "weixin.exe", 2_700_000),
        ]
        self.assertEqual(pick_wechat_window(cands), 0x2)

    def test_browser_title_alone_is_rejected(self):
        """只有浏览器标签命中时 → 判未找到（旧实现会把它当微信窗口）。"""
        cands = [_w(0x1, "微信 PC 4.x 机器人 - 豆包浏览器", "chrome.exe", 1_600_000)]
        self.assertIsNone(pick_wechat_window(cands))

    def test_appex_helper_excluded(self):
        """WeChatAppEx.exe 是小程序宿主，不在判据内（精确词形）。"""
        cands = [_w(0x1, "小程序", "wechatappex.exe", 900_000)]
        self.assertIsNone(pick_wechat_window(cands))

    def test_main_window_picked_by_exact_title(self):
        """同进程多窗口：标题严格等于「微信」的是主窗口。"""
        cands = [
            _w(0x1, "微信", "weixin.exe", 2_700_000),
            _w(0x2, "图片和视频", "weixin.exe", 500_000),
        ]
        self.assertEqual(pick_wechat_window(cands), 0x1)

    def test_area_breaks_tie_within_process(self):
        """同进程、标题都不严格命中时按面积取最大（辅助窗更小）。"""
        cands = [
            _w(0x1, "微信聊天记录", "weixin.exe", 100_000),
            _w(0x2, "另一个窗口", "weixin.exe", 900_000),
        ]
        self.assertEqual(pick_wechat_window(cands), 0x2)

    def test_title_fallback_when_process_unknown(self):
        """进程名读不到（提权/被占用）→ 标题严格相等的兜底仍生效。"""
        cands = [_w(0x1, "微信", "", 2_700_000)]
        self.assertEqual(pick_wechat_window(cands), 0x1)

    def test_title_fallback_does_not_match_substring(self):
        """兜底也只做相等：'微信文件' 这类窗口不算微信主窗口。"""
        cands = [_w(0x1, "微信文件", "", 2_700_000)]
        self.assertIsNone(pick_wechat_window(cands))

    def test_legacy_process_name(self):
        """微信 3.x 进程名 WeChat.exe 同样认。"""
        cands = [_w(0x1, "微信", "wechat.exe", 2_000_000)]
        self.assertEqual(pick_wechat_window(cands), 0x1)

    def test_prefers_wechat_over_unrelated_process_by_area(self):
        """进程名组非空时不再看其它进程（哪怕面积更大）。"""
        cands = [
            _w(0x1, "微信", "weixin.exe", 2_700_000),
            _w(0x2, "某个很大的窗口", "other.exe", 9_000_000),
        ]
        self.assertEqual(pick_wechat_window(cands), 0x1)

    def test_empty_candidates(self):
        self.assertIsNone(pick_wechat_window([]))


if __name__ == "__main__":
    unittest.main()
