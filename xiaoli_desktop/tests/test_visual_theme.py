# -*- coding: utf-8 -*-
"""浅色/深色双主题像素判据测试（真机实测色板做的合成帧）。

背景：整套像素判据是深色主题下标定的，浅色主题真机一跑就整类失效——
`detect_bubble_colors` 的「对方气泡比背景亮」约束在浅色下反向，把气泡色探成
图片里的纯白；`find_media_boxes` 的排除窗随之错位到 243~267，真实气泡填充
(238,238,240) 成了「非背景内容」→ 每个对方文字气泡都变成 ≥40×40 的媒体框
→ 文字被当图片丢弃（真机事故：群聊里「0」这条回复整条没了、文件卡片失去
文件名、发送者名并进正文）。

本文件用真机实测色板合成帧固化两套主题的行为：
- 浅色：背景(250,250,250) / 对方气泡(238,238,240) / 自己气泡(157,242,159)
  / 图片消息的浅灰页底(243,243,243)（与气泡只差 5 色阶——这是浅色最难的一处）
- 深色：背景(30,30,31) / 对方气泡(47,47,48) / 自己气泡(53,210,141)
外加真机几何：单行气泡高 44~62、文件卡片 146x432、图片页 189x422、照片 240 高。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw

from wx_backend.visual_backend import (VisualBackend, analyze_blocks,
                                       find_bubble_boxes, find_media_boxes,
                                       find_panel_icon)
from wx_backend.visual_vision import (detect_bubble_colors, detect_avatar_tops,
                                      estimate_theme)

# ---- 真机实测色板 ----
LIGHT_BG = (250, 250, 250)
LIGHT_BUBBLE = (238, 238, 240)      # 对方文字气泡 / 文件卡片面板
LIGHT_SELF = (157, 242, 159)        # 自己（绿）气泡
LIGHT_PAGE = (243, 243, 243)        # 图片消息里的浅灰页底
LIGHT_WHITE = (255, 255, 255)       # 图片里的纯白（曾把气泡色带偏）
GLYPH = (25, 25, 26)                # 消息文字
ICON_GREEN = (6, 174, 86)           # Excel 图标（文件卡片类型图标）

DARK_BG = (30, 30, 31)
DARK_BUBBLE = (47, 47, 48)
DARK_SELF = (53, 210, 141)

W, H = 747, 1135


def _blank(bg):
    return Image.new("RGB", (W, H), bg)


def _bubble(img, box, fill, lines=1):
    """画一个气泡：圆角矩形填充 + 若干行文字（贴左内边距）。

    box 用 PIL 口径 (left, top, right, bottom)。"""
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(box, radius=8, fill=fill)
    l, t, r, b = box
    for i in range(lines):
        y = t + 14 + i * 24
        if y + 10 > b - 6:
            break
        d.rectangle([l + 18, y, max(l + 18, r - 24), y + 10], fill=GLYPH)
    return img


def _avatar(img, side, top):
    """在头像带画一个头像块（真机 ~62px 高，颜色远离背景/气泡色）。"""
    d = ImageDraw.Draw(img)
    x0 = 12 if side == "left" else int(W * 0.86)
    d.rounded_rectangle([x0, top, x0 + 60, top + 62], radius=6,
                        fill=(120, 90, 200) if side == "left" else (200, 120, 90))
    return img


def _light_text_frame():
    """浅色：机器人先回复（绿色，在上），对方随后发来文字（在下）——真机里
    「处理新消息」就是这种形态：对方新消息在 bot 最后一条之后。"""
    img = _blank(LIGHT_BG)
    _avatar(img, "right", 100)
    _bubble(img, (334, 105, 613, 165), LIGHT_SELF, lines=1)
    _avatar(img, "left", 420)
    _bubble(img, (110, 440, 330, 500), LIGHT_BUBBLE, lines=1)
    return img


def _light_image_frame():
    """浅色：浅灰页底截图（243）+ 纯白区（255）+ 彩色照片，全是大图。"""
    img = _blank(LIGHT_BG)
    d = ImageDraw.Draw(img)
    d.rectangle([119, 120, 308, 542], fill=LIGHT_PAGE)       # 浅灰截图页
    d.rectangle([130, 140, 300, 300], fill=LIGHT_WHITE)      # 页内白色卡片
    for i in range(6):
        d.rectangle([135, 330 + i * 28, 290, 338 + i * 28], fill=GLYPH)
    d.rectangle([119, 620, 549, 900], fill=(180, 60, 40))    # 彩色照片
    d.rectangle([300, 700, 500, 820], fill=(240, 220, 90))
    return img


def _light_mixed_frame():
    """浅色：对方先发文字（上）、再发浅灰页底截图（下）——分类测试用。"""
    img = _blank(LIGHT_BG)
    d = ImageDraw.Draw(img)
    _avatar(img, "right", 60)
    _bubble(img, (334, 65, 613, 125), LIGHT_SELF, lines=1)
    _avatar(img, "left", 420)
    _bubble(img, (110, 440, 330, 500), LIGHT_BUBBLE, lines=1)
    _avatar(img, "left", 620)
    d.rectangle([119, 640, 308, 1062], fill=LIGHT_PAGE)       # 浅灰截图页
    for i in range(6):
        d.rectangle([135, 700 + i * 28, 290, 708 + i * 28], fill=GLYPH)
    return img


def _light_bubble_with_image_frame():
    """浅色：带一张大图（含纯白）**同时**有一条对方文字气泡——气泡色不得被
    图片纯白带偏（真机事故的复现场景：群聊里图文混排）。"""
    img = _light_image_frame()
    _avatar(img, "left", 1000)
    _bubble(img, (110, 1020, 330, 1080), LIGHT_BUBBLE, lines=1)
    return img


def _light_file_card_frame():
    """浅色：文件卡片（与气泡同色面板 146 高 / 432 宽 + 右侧实心图标）。"""
    img = _blank(LIGHT_BG)
    _avatar(img, "left", 800)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([110, 808, 550, 954], radius=8, fill=LIGHT_BUBBLE)
    for i in range(2):                                        # 文件名两行
        d.rectangle([130, 838 + i * 30, 450, 852 + i * 30], fill=GLYPH)
    d.rectangle([130, 900, 220, 912], fill=(150, 150, 150))   # 文件大小行
    d.rectangle([470, 847, 525, 916], fill=ICON_GREEN)        # 类型图标
    # 图标里的白色字形（真机 Excel 是白 X、PDF 是白 W）——浅色下白 (255) 与
    # 背景 (250) 同色域，是「图标填充率会不会被挖掉」的真实压力点
    d.line([(482, 862), (513, 901)], fill=LIGHT_WHITE, width=9)
    d.line([(513, 862), (482, 901)], fill=LIGHT_WHITE, width=9)
    return img


def _light_frame_with_list_overlap():
    """浅色：消息区左缘与会话列表重叠 3px 的真机事故帧。

    应用内两框标定时消息区左缘可能压到会话列表，那条 3px 竖条是列表面板灰
    (238,238,240)——**与气泡同色**，会把「近背景窄带」从头粘到尾（真机实测
    整帧只剩一个 1209px 高的连通域），取色投票里唯一的候选块被形状闸丢掉，
    气泡色估不出来 → 文件卡片图标判据拿不到面板色 → 卡片全被判成文字消息。
    """
    img = _light_text_frame()
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 2, H - 1], fill=LIGHT_BUBBLE)          # 3px 同色竖条
    return img


def _dark_text_frame():
    """深色：对方深灰气泡 + 自己绿气泡（既有测试的同类真机结构）。"""
    img = _blank(DARK_BG)
    _avatar(img, "left", 440)
    _avatar(img, "right", 600)
    _bubble(img, (110, 460, 330, 520), DARK_BUBBLE, lines=1)
    _bubble(img, (334, 625, 613, 685), DARK_SELF, lines=1)
    return img


class TestThemeEstimate(unittest.TestCase):
    def test_estimate_theme_light_and_dark(self):
        self.assertEqual(estimate_theme(_light_text_frame()), "light")
        self.assertEqual(estimate_theme(_dark_text_frame()), "dark")

    def test_backend_detects_theme_on_connect_and_follows_frame(self):
        """启动识别主题：connect 判出浅色 → theme/选中行默认值都按浅色；帧换
        成深色时（用户中途切主题）自动跟随。"""
        b = VisualBackend()
        with mock.patch("wx_backend.visual_backend.find_wechat_window",
                        return_value=0x1234), \
             mock.patch("wx_backend.visual_backend.capture_window",
                        return_value=_light_text_frame()):
            b.connect()
        self.assertEqual(b.theme, "light")
        self.assertEqual(b._selected_row_color, (21, 172, 112))
        b._note_theme("dark")
        self.assertEqual(b.theme, "dark")
        self.assertEqual(b._selected_row_color, (13, 168, 105))
        b.close()


class TestLightBubbleColors(unittest.TestCase):
    def test_light_other_is_bubble_not_image_white(self):
        """浅色核心回归：图文混排的一帧里，对方气泡色必须是气泡填充
        (238,238,240)，不得被图片里的纯白 (255,255,255) 带偏。

        旧实现用「比背景亮」筛候选 → 真气泡（比背景暗）被排除，中位数落到
        图片纯白上，下游气泡框/媒体框全线错位。"""
        for frame in (_light_bubble_with_image_frame(), _light_mixed_frame()):
            colors = detect_bubble_colors(frame)
            self.assertEqual(colors["theme"], "light")
            self.assertEqual(colors["bg"], LIGHT_BG)
            self.assertIsNotNone(colors["other"])
            for i in range(3):
                self.assertLess(
                    abs(colors["other"][i] - 238), 4,
                    f"对方气泡色应为气泡填充，实际 {colors['other']}")

    def test_light_image_only_frame_has_no_bubble_color(self):
        """没有气泡的帧（纯图片）不得凭空造出气泡色（fail-closed）。"""
        colors = detect_bubble_colors(_light_image_frame())
        self.assertEqual(colors["theme"], "light")
        self.assertIsNone(colors["other"])

    def test_light_self_is_pastel_green(self):
        colors = detect_bubble_colors(_light_text_frame())
        for i, want in enumerate(LIGHT_SELF):
            self.assertLess(abs(colors["self"][i] - want), 6)

    def test_dark_colors_unchanged(self):
        """深色路径必须逐值不变（现网判据就是它标定的）。"""
        colors = detect_bubble_colors(_dark_text_frame())
        self.assertEqual(colors["theme"], "dark")
        for c in colors["other"]:
            self.assertLess(abs(c - 47), 4)
        self.assertGreater(colors["self"][1], 150)
        self.assertLess(colors["self"][0], 100)

    def test_explicit_theme_wins(self):
        """显式传主题时不看帧（后端启动判定的值优先）。"""
        colors = detect_bubble_colors(_light_text_frame(), theme="dark")
        self.assertEqual(colors["theme"], "dark")


class TestLightBubbleBoxes(unittest.TestCase):
    def test_light_panels_found_and_bounded(self):
        img = _light_text_frame()
        colors = detect_bubble_colors(img)
        boxes = find_bubble_boxes(img, colors)
        others = [b for b in boxes if not b[4]]
        self.assertTrue(others, "浅色下对方气泡应被检出")
        t, b, l, r, _ = others[0]
        self.assertLess(abs(t - 440), 6)
        self.assertLess(abs(b - 500), 6)
        for (_t, _b, _l, _r, is_self) in boxes:
            self.assertLess(_b - _t, 0.6 * H, "面板高度不得接近整屏")

    def test_light_image_page_is_not_panel(self):
        """浅灰页底截图（243）与气泡(238)只差 5 色阶：容差 3 + 形状闸必须
        把「图片页」挡在气泡之外，否则它的媒体框会被当成面板内容剔除。"""
        img = _light_image_frame()
        colors = detect_bubble_colors(img)
        others = [b for b in find_bubble_boxes(img, colors) if not b[4]]
        for (_t, _b, l, r) in others:
            self.assertLess(r - l, W * 0.75, "图片页不得被当成气泡框")


class TestLightMediaBoxes(unittest.TestCase):
    def test_light_text_bubble_is_not_media(self):
        """浅色回归：文字气泡不得进入媒体框（否则该条文字被当图片丢弃）。"""
        img = _light_text_frame()
        colors = detect_bubble_colors(img)
        self.assertEqual(find_media_boxes(img, colors), [],
                         "文字气泡不是图片")

    def test_light_image_page_detected_as_media(self):
        """反向回归：浅灰页底(243)+纯白(255)的截图必须仍判为媒体——浅色下
        不能为了排掉气泡而把「近白/浅灰」的内容一并排掉。"""
        img = _light_image_frame()
        colors = detect_bubble_colors(img)
        media = find_media_boxes(img, colors)
        self.assertTrue(media, "浅灰页底截图应被检测为媒体")
        tops = sorted(t for (t, _b, _l, _r) in media)
        self.assertLess(abs(tops[0] - 120), 12, "媒体框应覆盖整张截图的上沿")

    def test_light_file_card_is_file_block(self):
        """文件卡片：面板同色于气泡（238）、146x432，右侧实心图标——应判为
        file 块并找到图标，绝不能是 image（真机后果：文件名丢失 + 把图标
        碎片当图片打开用户文件）。"""
        img = _light_file_card_frame()
        colors = detect_bubble_colors(img)
        others = [b for b in find_bubble_boxes(img, colors) if not b[4]]
        card = [b for b in others if abs(b[0] - 808) < 8]
        self.assertTrue(card, f"文件卡片面板应被检出，实际 {others}")
        arr_img = img
        import numpy as np
        arr = np.asarray(arr_img.convert("RGB"), dtype=np.int16)
        icon = find_panel_icon(arr, card[0][:4], colors)
        self.assertIsNotNone(icon, "文件卡片面板内应找到类型图标")
        bt = detect_avatar_tops(img, colors["bg"], "right")
        ot = detect_avatar_tops(img, colors["bg"], "left")
        info = analyze_blocks(img, colors, bt, ot, skip_bot=0)
        self.assertEqual([b["kind"] for b in info["blocks"]], ["file"])
        self.assertTrue(info["has_media"])

    def test_light_color_estimate_survives_list_overlap(self):
        """消息区左缘压到会话列表（3px 同色竖条）时，气泡色仍要估得出来。

        真机事故：那条竖条把近背景窄带粘成一整块 → 取色投票唯一的候选被形状闸
        丢掉 → other=None → 文件卡片图标判据直接返回 None → 卡片被判成文字。"""
        colors = detect_bubble_colors(_light_frame_with_list_overlap())
        self.assertIsNotNone(colors["other"], "同色竖条不得让气泡色估不出来")
        for i in range(3):
            self.assertLess(abs(colors["other"][i] - 238), 4)
        others = [b for b in find_bubble_boxes(
            _light_frame_with_list_overlap(), colors) if not b[4]]
        self.assertTrue(others, "同色竖条不得吃掉气泡面板")

    def test_light_file_card_survives_list_overlap(self):
        """端到端：左缘同色竖条下，文件卡片仍须判成 file（任务桥入口）。"""
        img = _light_file_card_frame()
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 2, H - 1], fill=LIGHT_BUBBLE)
        colors = detect_bubble_colors(img)
        bt = detect_avatar_tops(img, colors["bg"], "right")
        ot = detect_avatar_tops(img, colors["bg"], "left")
        info = analyze_blocks(img, colors, bt, ot, skip_bot=0)
        self.assertEqual([b["kind"] for b in info["blocks"]], ["file"],
                         f"卡片应判 file，实际 {[b['kind'] for b in info['blocks']]}")

    def test_panel_icon_without_bubble_color(self):
        """面板色估不出来（other=None）时，图标判据要用面板自测的众数色兜底。

        真机事故：other=None 直接返回 None → 文件卡片全被判成文字消息。"""
        import numpy as np
        img = _light_file_card_frame()
        arr = np.asarray(img.convert("RGB"), dtype=np.int16)
        panel = (808, 954, 110, 550)
        for colors in ({"bg": LIGHT_BG, "other": None, "theme": "light"},
                       {"bg": LIGHT_BG, "other": None}):
            icon = find_panel_icon(arr, panel, colors)
            self.assertIsNotNone(icon, f"colors={colors} 时仍应找到图标")
            t, b, l, r = icon
            self.assertLess(abs(t - 847), 8)
            self.assertLess(abs(l - 470), 8)
        # 纯文字气泡不该因为兜底而误判出图标
        self.assertIsNone(find_panel_icon(arr, (0, 60, 110, 330),
                                          {"bg": LIGHT_BG, "other": None,
                                           "theme": "light"}))

    def test_light_multiline_bubble_still_text(self):
        """多行文字气泡（真机行距 ~29px，5 行 ≈ 200px）仍须判成文字：形状闸
        要放得下长消息气泡，同时把 ≥240px 的图片挡在外面（两边一起验）。"""
        img = _blank(LIGHT_BG)
        _avatar(img, "right", 60)
        _bubble(img, (334, 65, 613, 125), LIGHT_SELF, lines=1)
        _avatar(img, "left", 300)
        _bubble(img, (110, 320, 500, 500), LIGHT_BUBBLE, lines=6)   # 180px 高
        colors = detect_bubble_colors(img)
        self.assertEqual(find_media_boxes(img, colors), [],
                         "多行文字气泡不是图片")
        bt = detect_avatar_tops(img, colors["bg"], "right")
        ot = detect_avatar_tops(img, colors["bg"], "left")
        blocks = analyze_blocks(img, colors, bt, ot, skip_bot=0)["blocks"]
        self.assertEqual([b["kind"] for b in blocks], ["text"])

    def test_light_blocks_text_and_image(self):
        """浅色分类：文字气泡 → text，浅灰页底截图 → image。"""
        img = _light_mixed_frame()
        colors = detect_bubble_colors(img)
        bt = detect_avatar_tops(img, colors["bg"], "right")
        ot = detect_avatar_tops(img, colors["bg"], "left")
        blocks = analyze_blocks(img, colors, bt, ot, skip_bot=0)["blocks"]
        self.assertEqual([b["kind"] for b in blocks], ["text", "image"],
                         f"浅色分类错：{[(b['kind'], b['top']) for b in blocks]}")

    def test_dark_media_unchanged(self):
        img = _dark_text_frame()
        colors = detect_bubble_colors(img)
        self.assertEqual(find_media_boxes(img, colors), [])


class TestLightGroupSenderSplit(unittest.TestCase):
    """浅色群聊：发送者名（气泡外、气泡上方的一行短文本）必须从正文里拆出来。

    拆名判据依赖面板（head.y < panel[0]）——浅色下面板检不出来时，名字会并进
    正文（真机：「何镇鸿有人要带东西吗」整条当消息、sender 退化成群名）。
    """

    def _run(self, frame):
        b = VisualBackend()
        b._message_region = (0.0, 0.0, 1.0, 1.0)
        b._title_region = (0.0, 0.0, 0.0, 0.04)
        items = [
            {"text": "群名(5)", "x": 200, "y": 6, "w": 90, "h": 20},
            {"text": "何镇鸿", "x": 115, "y": 418, "w": 70, "h": 20},
            {"text": "有人要带东西吗", "x": 130, "y": 452, "w": 180, "h": 24},
        ]
        with mock.patch("wx_backend.visual_backend.find_wechat_window",
                        return_value=0x1234), \
             mock.patch("wx_backend.visual_backend.capture_window",
                        return_value=frame), \
             mock.patch("wx_backend.visual_backend.ocr_image",
                        return_value=items):
            b.connect()
            b._current_chat = "群名"
            msgs = b.get_messages("群名", assume_switched=True)
        b.close()
        return msgs

    def test_light_sender_split_from_content(self):
        msgs = self._run(_light_text_frame())
        self.assertEqual(len(msgs), 1, f"应读到 1 条，实际 {msgs}")
        self.assertEqual(msgs[0].sender, "何镇鸿", "浅色下发送者名应从正文拆出")
        self.assertEqual(msgs[0].content, "有人要带东西吗")


class TestLightUnreadAndSelectedRow(unittest.TestCase):
    """浅色下与主题无关的两条链路（红圈 / 选中行高亮）——真机实测值固化。

    红圈是微信品牌红（浅色真机截图实测角标 #FA5151 ≈ (250,81,81)，同心圆角标
    带白色条数文本），色域与主题无关；选中行高亮深浅两套都是绿（深色
    (13,168,105) / 浅色 (21,172,112)），同一 `_color_close` 容差判得中。"""

    BADGE_RED = (250, 81, 81)
    LIGHT_ROW_BG = (238, 238, 240)
    LIGHT_ROW_SELECTED = (21, 172, 112)

    def test_light_frame_badge_detected(self):
        from wx_backend.visual_badge import _detect_red_clusters
        img = Image.new("RGB", (W, H), LIGHT_BG)
        d = ImageDraw.Draw(img)
        sr = (0.09, 0.0878, 0.418, 0.9895)
        sl, st = int(W * sr[0]), int(H * sr[1])
        # 一条浅色会话行 + 行内头像 + 左上角红色未读角标（26x27，含白色条数）
        d.rectangle([sl, st + 100, int(W * sr[2]), st + 200], fill=self.LIGHT_ROW_BG)
        d.rectangle([sl + 10, st + 120, sl + 70, st + 180], fill=(120, 90, 200))
        d.ellipse([sl + 12, st + 112, sl + 38, st + 139], fill=self.BADGE_RED)
        d.rectangle([sl + 21, st + 118, sl + 25, st + 133], fill=(255, 255, 255))
        boxes = _detect_red_clusters(img, region=sr)
        self.assertTrue(boxes, "浅色会话列表里的红色未读角标应被检出")

    def test_light_red_avatar_not_badge(self):
        """浅色列表里的大块红色（红色头像）不得当红圈：面积上限挡掉。"""
        from wx_backend.visual_badge import _detect_red_clusters
        img = Image.new("RGB", (W, H), LIGHT_BG)
        d = ImageDraw.Draw(img)
        sr = (0.09, 0.0878, 0.418, 0.9895)
        sl, st = int(W * sr[0]), int(H * sr[1])
        d.rectangle([sl, st + 100, int(W * sr[2]), st + 320], fill=self.LIGHT_ROW_BG)
        d.rectangle([sl + 8, st + 120, sl + 78, st + 190], fill=self.BADGE_RED)
        self.assertEqual(_detect_red_clusters(img, region=sr), [],
                         "大块红色（头像）不应判成未读角标")

    def test_light_selected_row_is_recognized(self):
        """浅色选中行高亮（真机 (21,172,112)）命中缓存的浅色默认值。"""
        img = Image.new("RGB", (W, H), LIGHT_BG)
        d = ImageDraw.Draw(img)
        sr = (0.09, 0.0878, 0.418, 0.9895)
        sl, st = int(W * sr[0]), int(H * sr[1])
        d.rectangle([sl, st + 240, int(W * sr[2]), st + 340],
                    fill=self.LIGHT_ROW_SELECTED)
        b = VisualBackend()
        b._session_region = sr
        b._note_theme("light")
        with mock.patch.object(b, "_refresh", return_value=img), \
             mock.patch("wx_backend.visual_backend.find_wechat_window",
                        return_value=0x1234), \
             mock.patch("wx_backend.visual_backend.capture_window",
                        return_value=img), \
             mock.patch("wx_backend.visual_backend.u32.GetWindowRect"):
            b._hwnd = 0x1234
            self.assertTrue(b._is_row_selected(st + 290), "浅色选中行应判为已选中")
            self.assertFalse(b._is_row_selected(st + 150), "未选中行不应判为已选中")
        b.close()


if __name__ == "__main__":
    unittest.main()
