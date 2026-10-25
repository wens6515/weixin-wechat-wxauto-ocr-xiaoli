# -*- coding: utf-8 -*-
"""引用消息判据：竖线检测原语 + analyze_blocks 的 kind 判定 + 引用行前缀。

真机标定（1300x1610，深/浅双主题，见 visual_vision 的常量注释）：
引用块 = 气泡面板下方约 +15px 起、面板左缘 +10 处一条 2~4px 竖灰线
（深 (53,53,54) / 浅 (225,225,225)），长度与引用内容物等高。修复前引用
消息被当图片：正文整行丢弃、缩略图被点击裁剪送视觉（真机事故链）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PIL import Image

from wx_backend import visual_backend
from wx_backend.visual_vision import detect_quote_line

W, H = 747, 300


def _img(theme="dark", line_top=None, line_len=50, line_color=None):
    """合成消息区：背景打底 + 面板左缘 +10 处一条竖线（line_top=None 不画）。"""
    bg = (30, 30, 31) if theme == "dark" else (250, 250, 250)
    color = line_color or ((53, 53, 54) if theme == "dark" else (225, 225, 225))
    img = Image.new("RGB", (W, H), bg)
    if line_top is not None:
        arr = np.array(img)
        arr[line_top:line_top + line_len, 119:122] = color
        img = Image.fromarray(arr)
    return img


PANEL = (100, 160, 110, 380)   # (top, bottom, left, right)


class TestDetectQuoteLine(unittest.TestCase):
    DARK = {"theme": "dark"}
    LIGHT = {"theme": "light"}

    def test_dark_line_found(self):
        arr = np.asarray(_img("dark", line_top=175, line_len=50))
        got = detect_quote_line(arr, self.DARK, PANEL)
        self.assertIsNotNone(got)
        self.assertEqual(got[0], 175)
        self.assertEqual(got[1], 224)

    def test_light_line_found(self):
        arr = np.asarray(_img("light", line_top=175, line_len=50))
        got = detect_quote_line(arr, self.LIGHT, PANEL)
        self.assertIsNotNone(got)
        self.assertEqual(got[0], 175)

    def test_no_line_returns_none(self):
        arr = np.asarray(_img("dark", line_top=None))
        self.assertIsNone(detect_quote_line(arr, self.DARK, PANEL))

    def test_short_line_rejected(self):
        arr = np.asarray(_img("dark", line_top=175, line_len=10))
        self.assertIsNone(detect_quote_line(arr, self.DARK, PANEL))

    def test_line_top_outside_window_rejected(self):
        # 竖线顶离面板底太远（窗口 +5~+31 之外）→ 不是引用竖线
        arr = np.asarray(_img("dark", line_top=160 + 60, line_len=50))
        self.assertIsNone(detect_quote_line(arr, self.DARK, PANEL))

    def test_bubble_below_not_mistaken_for_line(self):
        """下一条消息的气泡（47,47,48）只比竖线色暗 6/通道——容差必须把
        它排除，否则连续两条短文字消息会误判引用（检测窗口内气泡顶）。"""
        img = _img("dark", line_top=None)
        arr = np.array(img)
        arr[160 + 25:160 + 25 + 62, 110:400] = (47, 47, 48)   # 下一气泡顶在 +25
        got = detect_quote_line(arr.astype(np.int16), self.DARK, PANEL)
        self.assertIsNone(got)


class TestQuoteMarker(unittest.TestCase):
    def test_text_quote(self):
        m = visual_backend._quote_marker([{"text": "王文生：吴小鹏.pptx"}])
        self.assertEqual(m, "[引用 王文生：吴小鹏.pptx] ")

    def test_image_quote_name_only(self):
        m = visual_backend._quote_marker([{"text": "王文生:"}])
        self.assertEqual(m, "[引用 王文生发来的图片] ")

    def test_image_quote_fullwidth_colon(self):
        m = visual_backend._quote_marker([{"text": "王文生："}])
        self.assertEqual(m, "[引用 王文生发来的图片] ")

    def test_empty(self):
        self.assertEqual(visual_backend._quote_marker([]), "")


class TestAnalyzeBlocksQuote(unittest.TestCase):
    """analyze_blocks 的 kind 判定（mock 框检测原语，只测组合逻辑）：

    面板+图标=file；有面板（含面板外游离媒体框）=quote/text（竖线定夺，
    游离框绝不再判 image）；无面板有媒体框=image。"""

    def _analyze(self, img, panels, media, icons=None):
        with mock.patch.object(visual_backend, "find_bubble_boxes",
                               return_value=[p + (False,) for p in panels]), \
             mock.patch.object(visual_backend, "find_media_boxes",
                               return_value=list(media)), \
             mock.patch.object(visual_backend, "find_panel_icon",
                               side_effect=(lambda arr, panel, colors:
                                            (icons or {}).get(panel))):
            colors = {"theme": "dark", "bg": (30, 30, 31),
                      "other": (47, 47, 48), "self": None}
            return visual_backend.analyze_blocks(
                img, colors, [], [100], skip_bot=0)

    def test_panel_with_line_is_quote(self):
        img = _img("dark", line_top=175, line_len=50)
        info = self._analyze(img, panels=[PANEL], media=[(180, 220, 140, 200)])
        (b,) = info["blocks"]
        self.assertEqual(b["kind"], "quote")
        self.assertEqual(b["quote"], (175, 224))
        self.assertEqual(b["bottom"], 226)   # 块底延伸到线底（媒体框更浅时）
        self.assertFalse(info["has_media"])  # 引用不触发媒体防抖

    def test_panel_without_line_is_text_even_with_stray_media(self):
        """竖线漏检时游离媒体框也不再回退 image——文字优先（fail-safe）。"""
        img = _img("dark", line_top=None)
        info = self._analyze(img, panels=[PANEL], media=[(180, 230, 140, 200)])
        (b,) = info["blocks"]
        self.assertEqual(b["kind"], "text")

    def test_panel_with_icon_is_file(self):
        img = _img("dark", line_top=None)
        info = self._analyze(img, panels=[PANEL], media=[],
                             icons={PANEL: (150, 190, 300, 330)})
        (b,) = info["blocks"]
        self.assertEqual(b["kind"], "file")
        self.assertTrue(info["has_media"])

    def test_media_without_panel_is_image(self):
        img = _img("dark", line_top=None)
        info = self._analyze(img, panels=[], media=[(100, 200, 140, 300)])
        (b,) = info["blocks"]
        self.assertEqual(b["kind"], "image")
        self.assertTrue(info["has_media"])


if __name__ == "__main__":
    unittest.main()


class TestQuoteAttachmentPlan(unittest.TestCase):
    """引用联动计划：图片引用出框（点击捕获）、文件引用出文件名、
    纯文字引用不联动（OCR 把「.」读成「·」要先归一再提 token）。"""

    def _bot(self):
        from xiaoli_bot import AgentBot
        return AgentBot.__new__(AgentBot)   # 不跑 __init__

    def test_image_quote_yields_box(self):
        bot = self._bot()
        win = {"other_blocks": [{"kind": "quote", "top": 100,
                                 "media": (140, 200, 130, 190)}]}
        msgs = [mock.Mock(y=100,
                          content="[引用 王文生发来的图片] 这是什么")]
        boxes, quoted = bot._quote_attachment_plan(win, msgs)
        self.assertEqual(boxes, [(140, 200, 130, 190)])
        self.assertIsNone(quoted)

    def test_image_quote_without_media_no_box(self):
        bot = self._bot()
        win = {"other_blocks": [{"kind": "quote", "top": 100, "media": None}]}
        msgs = [mock.Mock(y=100, content="[引用 王文生发来的图片] 看看")]
        boxes, _ = bot._quote_attachment_plan(win, msgs)
        self.assertEqual(boxes, [])

    def test_file_quote_yields_token(self):
        bot = self._bot()
        win = {"other_blocks": [{"kind": "quote", "top": 100, "media": None}]}
        msgs = [mock.Mock(y=100,
                          content="[引用 王文生：吴小鹏.pptx] 你能看到这个文件嘛")]
        boxes, quoted = bot._quote_attachment_plan(win, msgs)
        self.assertEqual(boxes, [])
        self.assertEqual(quoted, "吴小鹏.pptx")

    def test_file_quote_middle_dot_normalized(self):
        """OCR 高频误读「.」→「·」：引用文件名先归一再提 token。"""
        bot = self._bot()
        win = {"other_blocks": [{"kind": "quote", "top": 100, "media": None}]}
        msgs = [mock.Mock(y=100,
                          content="[引用 王文生：吴小鹏·pptx] 你能看到嘛")]
        _, quoted = bot._quote_attachment_plan(win, msgs)
        self.assertEqual(quoted, "吴小鹏.pptx")

    def test_text_quote_no_attachment(self):
        bot = self._bot()
        win = {"other_blocks": [{"kind": "quote", "top": 100, "media": None}]}
        msgs = [mock.Mock(y=100,
                          content="[引用 小漓: 明天去哪] 那到底去哪")]
        boxes, quoted = bot._quote_attachment_plan(win, msgs)
        self.assertEqual(boxes, [])
        self.assertIsNone(quoted)

    def test_non_quote_blocks_ignored(self):
        bot = self._bot()
        win = {"other_blocks": [{"kind": "text", "top": 100, "media": None}]}
        msgs = [mock.Mock(y=100, content="普通消息")]
        boxes, quoted = bot._quote_attachment_plan(win, msgs)
        self.assertEqual((boxes, quoted), ([], None))


class TestQuoteContentBox(unittest.TestCase):
    """引用区内容物专用检测：窄缩略图（通用 40px 闸漏掉的 ~30px 竖图）、
    文字字形排除（min 24 + 填充率）、浅色低阈值档。"""

    def _find(self, theme, draw, panel=(100, 160, 110, 380),
              quote=(175, 240), bg=None):
        img = Image.new("RGB", (747, 300), bg or ((30, 30, 31) if theme == "dark"
                                                  else (250, 250, 250)))
        draw(img)
        colors = {"theme": theme, "bg": bg or ((30, 30, 31) if theme == "dark"
                                               else (250, 250, 250))}
        arr = np.asarray(img).astype(np.int16)
        return detect_quote_line and __import__(
            "wx_backend.visual_vision", fromlist=["find_quote_content_box"]
        ).find_quote_content_box(arr, colors, panel, quote)

    def test_narrow_thumbnail_found_dark(self):
        """~30px 宽竖版缩略图（通用媒体判据会漏）：专用检测必须找到。"""
        def draw(img):
            arr = np.array(img)
            arr[180:235, 220:250] = (200, 120, 140)   # 30×55 粉色缩略图
            img.paste(Image.fromarray(arr.astype("uint8")))
        got = self._find("dark", draw)
        self.assertIsNotNone(got)
        t, b, l, r = got
        self.assertGreaterEqual(r - l, 24)
        self.assertGreaterEqual(b - t, 40)

    def test_text_glyphs_not_detected(self):
        """引用行的灰色文字（~20px 字形，稀疏笔画）不得被判为内容物。"""
        def draw(img):
            arr = np.array(img)
            arr[185:205, 136:210] = (113, 113, 117)   # 「王文生」样式的字块
            img.paste(Image.fromarray(arr.astype("uint8")))
        got = self._find("dark", draw)
        self.assertIsNone(got)

    def test_light_theme_pale_content_found(self):
        """浅色：白底缩略图上的深色 QR 模块（与背景差大）要能成块。"""
        def draw(img):
            arr = np.array(img)
            arr[182:240, 224:282] = (250, 250, 250)   # 白底（与浅背景差 15，隐形）
            arr[190:234, 232:274] = (60, 60, 65)      # QR 深色模块核心 42×42
            img.paste(Image.fromarray(arr.astype("uint8")))
        got = self._find("light", draw)
        self.assertIsNotNone(got)


class TestQuoteMarkerContent(unittest.TestCase):
    """引用标注判型（has_content=True）：含扩展名 → 文件整行标注；
    内嵌文字无扩展名 → 只留名字的图片标注（缩略图内嵌文字不进标注）。"""

    def test_file_with_content_keeps_full_line(self):
        m = visual_backend._quote_marker(
            [{"text": "王文生：吴小鹏·pptx"}], has_content=True)
        self.assertEqual(m, "[引用 王文生：吴小鹏·pptx] ")

    def test_image_embedded_text_dropped(self):
        """真机事故回归：缩略图上印的「好耶」被 OCR 读走 → 旧逻辑判成
        文字引用；新逻辑有内容物且无扩展名 → 图片标注 + 丢内嵌文字。"""
        m = visual_backend._quote_marker(
            [{"text": "王文生:"}, {"text": "好耶"}], has_content=True)
        self.assertEqual(m, "[引用 王文生发来的图片] ")

    def test_no_content_keeps_old_logic(self):
        m = visual_backend._quote_marker([{"text": "王文生:"}])
        self.assertEqual(m, "[引用 王文生发来的图片] ")
        m2 = visual_backend._quote_marker(
            [{"text": "小漓: 唔……那小鱼就画不出来了"}])
        self.assertEqual(m2, "[引用 小漓: 唔……那小鱼就画不出来了] ")


class TestQuotePlanPrefersDedicatedBox(unittest.TestCase):
    def test_quote_media_wins_over_media(self):
        """窄缩略图场景：quote_media（专用检测框）优先于通用 media 框。"""
        from xiaoli_bot import AgentBot
        bot = AgentBot.__new__(AgentBot)
        win = {"other_blocks": [{"kind": "quote", "top": 100,
                                 "media": (140, 200, 130, 190),
                                 "quote_media": (150, 205, 140, 170)}]}
        msgs = [mock.Mock(y=100,
                          content="[引用 王文生发来的图片] 看这个")]
        boxes, _ = bot._quote_attachment_plan(win, msgs)
        self.assertEqual(boxes, [(150, 205, 140, 170)])
