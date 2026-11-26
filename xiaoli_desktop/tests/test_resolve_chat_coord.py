# -*- coding: utf-8 -*-
"""按会话名定位列表条目：只认「标题行」，预览行永远不是候选。

真机缺陷（用户实测）：主动发起/追尾要切到私聊「联系人甲」，列表区 OCR 里
「某群聊」那一行的预览是「联系人甲：yes, is me」——旧实现把整块 OCR
的每一行都拿去子串匹配，预览行先命中（群行在私聊行上方），点击落进群里；
随后标题复验不过，主动发起每次都被放弃，窗口还被拽到群里。日志坐标可证：
点击 y=442 而私聊标题行 y≈509，x 随预览文本变宽从 1526 漂到 1584。

因此定位的候选集必须是「每个会话行的标题行」：
- 预览行（含「发送者：内容」或正文）、[图片]/[视频] 标签、时间戳一律出局；
- 匹配分 exact > truncated > extended 三档，档内不唯一即 fail-closed。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

from wx_backend.visual_backend import VisualBackend


def _row(name, y, h=18, x=30, w=60):
    return {"text": name, "x": x, "y": y, "w": w, "h": h}


class _ResolveBase(unittest.TestCase):
    """resolve_chat_coord 的桩：截图固定 1300×1610（标定尺寸，k=1）。"""

    def _coord(self, blocks, target):
        b = VisualBackend()
        b._hwnd = 0x1234
        shot = Image.new("RGB", (1300, 1610), (30, 30, 30))
        with mock.patch.object(VisualBackend, "_refresh", return_value=shot), \
                mock.patch("wx_backend.visual_backend.ocr_image",
                           return_value=blocks):
            return b.resolve_chat_coord(target)


class TestPreviewLineIsNotCandidate(_ResolveBase):
    def test_group_preview_with_sender_name_is_ignored(self):
        """★ 真机缺陷回归：群预览「联系人甲：yes, is me」不得冒充会话名。"""
        blocks = [
            _row("联系人乙", 10),
            _row("[图片]", 45, h=16),
            _row("联系人丙", 120),
            _row("阿姨吃饱啦，圆圆的肚子才可…", 152, h=16, w=200),
            _row("“某群” 集团", 230, h=20, w=100),
            _row("联系人甲：yes, is me", 264, h=16, w=180),
            _row("联系人甲", 340),
            _row("唔......好像也是，我确实一直…", 374, h=16, w=200),
        ]
        coord = self._coord(blocks, "联系人甲")
        self.assertIsNotNone(coord)
        # 命中私聊「联系人甲」标题行（列表区起点 x=117 / y=141 折算）
        self.assertEqual(coord, (117 + 30 + 30, 141 + 340 + 9))

    def test_only_preview_line_matching_target_returns_none(self):
        """列表里只有群标题 + 以目标名开头的预览行 → 找不到（不点）。"""
        blocks = [
            _row("“某群” 集团", 230, h=20, w=100),
            _row("联系人甲：yes, is me", 264, h=16, w=180),
        ]
        self.assertIsNone(self._coord(blocks, "联系人甲"))

    def test_private_preview_body_is_ignored(self):
        """私聊预览正文（无冒号）同样不是候选。"""
        blocks = [
            _row("联系人甲", 340),
            _row("联系人甲你好呀，最近怎么样", 374, h=16, w=200),
        ]
        self.assertEqual(self._coord(blocks, "联系人甲"),
                         (117 + 30 + 30, 141 + 340 + 9))


class TestMatchTiers(_ResolveBase):
    def test_exact_beats_extended(self):
        """同名联系人与「XX的工作群」并存 → 精确相等优先，绝不点群。"""
        blocks = [
            _row("联系人甲的工作群", 100, w=120),
            _row("联系人甲", 220),
        ]
        self.assertEqual(self._coord(blocks, "联系人甲"),
                         (117 + 30 + 30, 141 + 220 + 9))

    def test_duplicate_exact_fails_closed(self):
        """两个完全同名的条目 → 无法确定是哪个，fail-closed 不点。"""
        blocks = [
            _row("联系人甲", 100),
            _row("联系人甲", 220),
        ]
        self.assertIsNone(self._coord(blocks, "联系人甲"))

    def test_truncated_candidate_matches(self):
        """列表 OCR 漏字（「闲聊集」vs「闲聊集团」）仍能定位。"""
        blocks = [_row("闲聊集", 100, w=60)]
        self.assertEqual(self._coord(blocks, "闲聊集团"),
                         (117 + 30 + 30, 141 + 100 + 9))

    def test_truncated_requires_enough_length(self):
        """只剩一两个字不算截断命中——否则「王」会匹配到一片。"""
        blocks = [_row("王", 100, w=20)]
        self.assertIsNone(self._coord(blocks, "联系人甲"))

    def test_extended_unique_matches(self):
        """目标名来自更残缺的一次 OCR（「闲聊」vs「“闲聊”集团」）→ 唯一可点。"""
        blocks = [_row("“闲聊”集团", 100, w=120)]
        self.assertEqual(self._coord(blocks, "闲聊"),
                         (117 + 30 + 60, 141 + 100 + 9))

    def test_ambiguous_extended_fails_closed(self):
        """多个以目标名开头的会话 → 不赌，fail-closed。"""
        blocks = [
            _row("闲聊集团", 100, w=90),
            _row("闲聊小分队", 220, w=110),
        ]
        self.assertIsNone(self._coord(blocks, "闲聊"))

    def test_not_found_logs_candidates(self):
        blocks = [_row("联系人乙", 100)]
        self.assertIsNone(self._coord(blocks, "联系人甲"))


class TestClusteringSingleSource(unittest.TestCase):
    def test_extract_session_names_and_resolve_share_rows(self):
        """整表提名与按名定位共用聚类口径：预览行都不进候选。"""
        blocks = [
            _row("“某群” 集团", 230, h=20, w=100),
            _row("联系人甲：yes, is me", 264, h=16, w=180),
            _row("联系人甲", 340),
        ]
        b = VisualBackend()
        rows = b._list_title_rows(blocks)
        names = [r[0] for r in rows]
        self.assertIn("“某群” 集团", names)
        self.assertIn("联系人甲", names)
        self.assertNotIn("联系人甲：yes, is me", names)


if __name__ == "__main__":
    unittest.main()
