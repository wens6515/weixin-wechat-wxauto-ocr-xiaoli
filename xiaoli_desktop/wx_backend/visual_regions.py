# -*- coding: utf-8 -*-
"""visual_backend 拆分 · OCR 三区域默认标定与会话标题解析。

区域默认值（真机标定常量）+ parse_title（标题 → 会话名/群聊标记）。
徽章检测依赖 _normalize_region 与 _SESSION_REGION_RATIO，故独立成模块避免与
门面循环导入。

**用户自定义画面标定已撤回**：三区域恒为本模块的默认常量，运行期不再有
"用户画框 → 区域"的换算（wx_ocr_region.json 的读写、两框标定派生函数一并
移除）。理由：那套几何判据（头像窄带 = 框宽的比例、期望头像高 = 框高÷18、
气泡/媒体闸同样是框的比例）只在"框 == 聊天区"这一前提下成立，而微信布局
不是等比的（会话列表固定像素宽、输入框固定像素高）——用户手画的大框/小框
会让这些常量整体漂移，真机事故：自设区域后对方头像整列检不出（消息读不到）、
自己侧漏检（自家图片被当对方消息）。
"""
from __future__ import annotations

import re

# 微信窗口布局（真机标定，微信窗口 1300x1610 实测——即微信默认窗口尺寸下
# 的布局；**要求系统显示缩放 175%**：三区域与全部像素判据都按这台窗口标定，
# 缩放比例不同会让消息读不到/读错，见 README「系统要求」）。
# 会话列表：左侧约 42% 宽；消息区：右侧约 58% 宽。
# 消息区底部 0.8337 = 聊天记录下缘，刻意不含输入框（输入框"发送"按钮在
# 窗口 ~0.95 处——若区域含输入框，OCR 会把按钮文字当消息且判 self）。
_SESSION_REGION_RATIO = (0.09, 0.0878, 0.418, 0.9895)   # (l, t, r, b) 相对窗口
_MESSAGE_REGION_RATIO = (0.4165, 0.1288, 0.9913, 0.8337)
# 右侧会话标题区（真机标定）：当前会话名权威来源 + 群聊判定（标题带括号人数）
_TITLE_REGION_RATIO = (0.4151, 0.0386, 0.8128, 0.082)

# 三区域的**竖向锚点**（真机标定，1300x1610 窗口）：(l 宽比, t 距顶像素,
# r 宽比, b 距底像素——b 为负表示"距窗口底端向上量")。
# 为什么要分顶/底两类锚：窗口高度不再是恒定的 1610——任务栏可见的机器上
# 工作区（系统已扣任务栏）放不下 1610，程序按工作区收口后窗口更矮，而微信
# 布局里「聊天记录区上沿」贴窗口顶、「输入面板上沿」贴窗口底，两端各自固定。
# 只按窗口比例缩放的话，矮窗口下消息区底沿会切进输入框（读到的"消息"混进
# 输入框内容），会话列表底沿也会浮在窗口中间。
_REGION_EDGES = {
    "session": (0.09, 141.4, 0.418, -16.9),
    "message": (0.4165, 207.4, 0.9913, -267.7),
    "title": (0.4151, 62.1, 0.8128, 132.0),   # 标题带整条贴顶（在聊天头部内）
}


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def regions_for_window(w: int, h: int) -> tuple[tuple, tuple, tuple]:
    """窗口尺寸 (w, h) → 三区域**比例**（下游一律按"比例 × 截图尺寸"用）。

    横向按宽度比例（窗口宽度恒为标定宽度 1300）；竖向按 `_REGION_EDGES` 的
    固定像素：顶锚点不随高度变、底锚点距底固定。h 过小时把底沿压在顶沿之下
    8px，保证 l<r、t<b 的合法矩形。w/h 非法时回退模块常量。
    """
    if w <= 0 or h <= 0:
        return (_SESSION_REGION_RATIO, _MESSAGE_REGION_RATIO,
                _TITLE_REGION_RATIO)
    out = []
    for key in ("session", "message", "title"):
        l_r, t_px, r_r, b_px = _REGION_EDGES[key]
        t = t_px if t_px >= 0 else h + t_px
        b = b_px if b_px >= 0 else h + b_px
        t = min(max(t, 0.0), max(0.0, h - 8.0))
        b = min(max(b, t + 8.0), float(h))
        out.append((_clamp01(l_r), _clamp01(t / h),
                    _clamp01(r_r), _clamp01(b / h)))
    return tuple(out)  # type: ignore[return-value]

# 强制窗口几何（**窗口矩形**，物理像素）：尺寸强制为下列标定值——工作区
# （系统已扣任务栏）放不下时按工作区收口（任务栏可见的机器上可用高度只有
# ~1516 < 1610；不收口就会每轮请求 1610 被系统截短、每轮复查都判"尺寸不对"、
# 每秒重设一次并作废截图，读取全废）。位置由用户自由摆放，只在窗口（部分）
# 移出屏幕时拉回可见范围（见 visual_backend 的 enforce_window_geometry /
# pull_window_into_view）。
# 上面三组比例与全部像素判据（头像窄带 2%~14%、期望头像高、气泡/媒体闸）
# 都是在这台窗口上标定的——窗口宽高任一变，这些常量整体错位（真机事故：把
# 窗口拉大到 2178 宽后聊天区左边界从 0.415 挪到 0.246，消息区左沿切进聊天区、
# 整列头像检不出、消息读不到）；窗口被拖出屏幕则点击落到屏幕外，切会话/点图/
# 发送全失灵。用户自定义尺寸/区域两项设置已撤回，改为程序强制：
# `VisualBackend.enforce_window_geometry` 在 connect 与每轮消息监听时复查并
# 调回；受工作区限制而变矮时，竖向锚点由 `regions_for_window` 按顶/底分开算。
WINDOW_SIZE = (1300, 1610)


def _normalize_region(region: tuple) -> tuple | None:
    """校验并规范化区域元组：4 值都在 [0,1] 且 l<r、t<b，非法返回 None。

    仍被徽章检测消费（可选传入 region 参数）——非法值回退默认区域。
    """
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
