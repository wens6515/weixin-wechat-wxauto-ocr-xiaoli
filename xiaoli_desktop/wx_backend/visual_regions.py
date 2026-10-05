# -*- coding: utf-8 -*-
"""visual_backend 拆分 · OCR 三区域默认标定与会话标题解析。

区域默认值（真机标定常量）+ 区域元组校验 + parse_title（标题 → 会话名/
群聊标记）。徽章检测依赖 _normalize_region 与 _SESSION_REGION_RATIO，故
独立成模块避免与门面循环导入。用户圈定配置（wx_ocr_region.json）的路径
常量与加载函数留在门面——测试对 _REGION_CONFIG_PATH 的 patch 打在门面。
"""
from __future__ import annotations

import re

# 微信窗口布局（4.1.12.51 默认窗口，相对窗口客户区比例）
# 会话列表：左侧约 42% 宽；消息区：右侧约 58% 宽
# 注意：窗口位置/大小由用户用 tools/fix_window.py 固定，bot 不移动窗口、
# 不读窗口配置——坐标换算一律基于窗口当前实际 rect（见 _window_rect）。
#
# 默认值为真机框选标定（tools/pick_ocr_region.py，微信窗口 1300x1610 实测）：
# 消息区底部 0.8384 = 聊天记录下缘，刻意不含输入框（输入框"发送"按钮在
# 窗口 ~0.95 处——若默认区域含输入框，OCR 会把按钮文字当消息且判 self）。
# 打包 exe（PyInstaller）无 wx_ocr_region.json 时即用此默认；用户窗口尺寸
# 不同会导致错位，普通用户分发需引导框选或自适应检测。
_SESSION_REGION_RATIO = (0.09, 0.0878, 0.418, 0.9895)   # (l, t, r, b) 相对窗口
_MESSAGE_REGION_RATIO = (0.4165, 0.1288, 0.9913, 0.8337)
# 右侧会话标题区（真机标定）：当前会话名权威来源 + 群聊判定（标题带括号人数）
_TITLE_REGION_RATIO = (0.4151, 0.0386, 0.8128, 0.082)


def _normalize_region(region: tuple) -> tuple | None:
    """校验并规范化区域元组：4 值都在 [0,1] 且 l<r、t<b，非法返回 None。"""
    if not isinstance(region, (tuple, list)) or len(region) != 4:
        return None
    try:
        vals = tuple(float(v) for v in region)
    except (TypeError, ValueError):
        return None
    l, t, r, b = vals
    if not all(0.0 <= v <= 1.0 for v in vals):
        return None
    if l >= r or t >= b:
        return None
    return vals


_TITLE_GROUP_RE = re.compile(r"^(?P<name>.+?)\((?P<count>\d+)\)\s*$")


def parse_title(title: str) -> tuple[str, bool, int | None]:
    """解析会话标题 → (会话名, 是否群聊, 群人数)。

    群聊标题形如 '摸鱼"集团(5)'（括号内人数，真机标定）；私聊标题即会话名
    无括号。群聊判定依据标题而非会话名启发式——普通群名（如'哆菈A夢'）
    不含'群/集团'字，名称启发式会漏判。
    """
    if not title:
        return "", False, None
    m = _TITLE_GROUP_RE.match(title.strip())
    if m:
        return m.group("name"), True, int(m.group("count"))
    return title.strip(), False, None
