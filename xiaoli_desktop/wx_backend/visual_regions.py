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
# 注意：窗口位置由用户自己摆放，程序不移动窗口；窗口大小可在设置页固定
# （初始化套用）。坐标换算一律基于窗口当前实际 rect（见 _window_rect）。
#
# 默认值为真机框选标定（微信窗口 1300x1610 实测）：
# 消息区底部 0.8337 = 聊天记录下缘，刻意不含输入框（输入框"发送"按钮在
# 窗口 ~0.95 处——若默认区域含输入框，OCR 会把按钮文字当消息且判 self）。
# 无 wx_ocr_region.json（新装/未标定）即用此默认；用户窗口布局不同会导致
# 错位——分发引导用应用内画面标定（首启引导第 2 步 / 设置页）重标。
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


# ---------- 两框标定 ↔ 运行时三区域 ----------

def derive_regions(session_box, chat_box, split) -> dict | None:
    """两框标定结果 → 运行时三区域（比例元组）。

    session_box：会话列表框；chat_box：聊天区框（含标题带，右栏一整块）；
    split：标题分隔线位置 = 聊天区顶到分隔线的距离占聊天区高度的比例。
    派生：标题区 = 聊天区顶到分隔线的条带；消息区 = 分隔线以下到聊天区底。
    运行时读到的仍是三个独立比例区域（联合 OCR 外框 = 标题∪消息 =
    chat_box），热路径语义与三框时代完全一致。

    split 夹取 [0.02, 0.85]：太靠顶标题区高度趋零（t>=b 校验整份配置被拒
    回退默认），太靠底消息区只剩窄条。框非法返回 None。
    """
    s = _normalize_region(session_box)
    c = _normalize_region(chat_box)
    if s is None or c is None:
        return None
    try:
        k = min(0.85, max(0.02, float(split)))
    except (TypeError, ValueError):
        return None
    t_bot = c[1] + k * (c[3] - c[1])
    return {"session": s,
            "title": (c[0], c[1], c[2], t_bot),
            "message": (c[0], t_bot, c[2], c[3])}


def boxes_from_regions(session, message, title) -> dict | None:
    """运行时三区域 → 两框 + 分隔线（标定 UI 预填；默认标定值反推用同一函数）。"""
    s = _normalize_region(session)
    m = _normalize_region(message)
    t = _normalize_region(title)
    if not (s and m and t):
        return None
    chat = (min(t[0], m[0]), min(t[1], m[1]),
            max(t[2], m[2]), max(t[3], m[3]))
    ch = chat[3] - chat[1]
    split = (t[3] - chat[1]) / ch if ch > 1e-6 else 0.05
    return {"session_box": s, "chat_box": chat, "split": split}


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
