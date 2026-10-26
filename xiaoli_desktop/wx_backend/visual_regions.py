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

from .visual_scale import active_scale, k as _k

# 微信窗口布局（真机标定：175% 缩放、窗口 1300x1610 = 2560x1600 屏的右半——
# 用户定案：窗口物理尺寸恒为「工作区右半屏」，跨缩放档位不变）。
# 会话列表：左起 117px（175% 物理值，下同）；消息区：117~756.6 之外的右半。
# 消息区底部 0.8337 = 聊天记录下缘，刻意不含输入框（输入框"发送"按钮在
# 窗口 ~0.95 处——若区域含输入框，OCR 会把按钮文字当消息且判 self）。
# 三区域四沿全部是**距窗口边缘的固定逻辑距离**（微信布局按逻辑像素固定，
# 150% 真机实测验证：分界列/头像/面板物理位置 = 标定值 × k，与窗口多宽无关）
# ——因此不按窗口比例存，按「175% 标定物理像素」存，运行时 × k
# （scale/175，见 visual_scale）。比例判据（返回值）按当前窗口换算。
_SESSION_REGION_RATIO = (0.09, 0.0878, 0.418, 0.9895)   # 标定比例（回退/参考）
_MESSAGE_REGION_RATIO = (0.4165, 0.1288, 0.9913, 0.8337)
_TITLE_REGION_RATIO = (0.4151, 0.0386, 0.8128, 0.082)

# 三区域锚点（175% 标定物理像素）：(l, t, r, b)。锚定语义与符号绑定：
# l/t 恒为「距左/距顶」；r/b **负值 = 距右/距底**、正值 = 距左/距顶。
# 负值例：消息区右沿 -11.3（滚动条贴窗口右缘）、标题区右沿 -243.4（窗口
# 控制按钮贴右缘）；正值例：会话区右沿 543.4 = 会话|聊天**分界列**，它贴的
# 是窗口左缘（会话列宽固定逻辑）而非右缘——按右缘锚会在低档位把分界算进
# 列表区、让聊天区文字漏进列表 OCR（150% 真机实测翻车）。为什么分四沿
# 绝对锚而不是窗口比例：窗口物理尺寸恒为半屏（宽度不随档位变），而微信
# 列宽/面板高按逻辑像素固定（物理 = 逻辑 × 档位）——按比例存会在低档位
# 把分界列算进聊天区、把滚动条留在消息区内（150% 真机实测翻车）。
_REGION_EDGES = {
    "session": (117.0, 141.4, 543.4, -16.9),
    "message": (541.45, 207.4, -11.31, -267.7),
    "title": (539.63, 62.1, -243.36, 132.0),  # 标题带整条贴顶（在聊天头部内）
}


def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def regions_for_window(w: int, h: int, scale: int | None = None,
                       boundary_px: float | None = None
                       ) -> tuple[tuple, tuple, tuple]:
    """窗口尺寸 (w, h) → 三区域**相对当前窗口的比例**（下游按"比例 × 截图
    尺寸"用，返回值口径与标定时代一致，下游零改动）。

    四沿 = `_REGION_EDGES` 的标定物理像素 × k（k = 档位/175），锚定语义
    见该常量注释（l/t 距左/顶；r/b 负值距右/底、正值距左/顶——分界列贴左）。窗口物理宽度恒为半屏（不随档位变），低档位
    下这些绝对锚自动落在正确位置（150% 实测：分界 541×0.857=464 ✓）；返回
    前换算成当前窗口的比例只是下游接口口径。非法尺寸回退标定比例。scale
    缺省读当前生效档位（visual_scale）。

    boundary_px：实测分界列（`visual_vision.detect_boundary_x` 的空谷扫描）。
    窗口逻辑宽 < 标定窗口（半屏 731 逻辑）时微信压缩会话列，静态锚 543.4×k
    失准（200% 真机实测）——传入实测值即覆盖 session 右沿与 message/title
    左沿（三者都锚在分界列上）；None 回退静态锚。
    """
    if w <= 0 or h <= 0:
        return (_SESSION_REGION_RATIO, _MESSAGE_REGION_RATIO,
                _TITLE_REGION_RATIO)
    kk = _k() if scale is None else scale / 175.0
    boundary = boundary_px if (boundary_px is not None and 0 < boundary_px < w) \
        else None
    out = []
    for key in ("session", "message", "title"):
        l_px, t_px, r_px, b_px = _REGION_EDGES[key]
        l = l_px * kk
        r = (w + r_px * kk) if r_px < 0 else r_px * kk
        if boundary is not None:
            # 分界列三沿整体换到实测值（仍走同一套钳制）
            if key == "session":
                r = boundary
            else:
                l = boundary
        t = t_px * kk if t_px >= 0 else h + t_px * kk
        b = b_px * kk if b_px >= 0 else h + b_px * kk
        t = min(max(t, 0.0), max(0.0, h - 8.0 * kk))
        b = min(max(b, t + 8.0 * kk), float(h))
        l = min(max(l, 0.0), max(0.0, r - 8.0 * kk))
        r = min(max(r, l + 8.0 * kk), float(w))
        out.append((_clamp01(l / w), _clamp01(t / h),
                    _clamp01(r / w), _clamp01(b / h)))
    return tuple(out)  # type: ignore[return-value]


def target_window_size(work_area: tuple | None) -> tuple[int, int]:
    """强制窗口目标尺寸（**物理**）= 工作区右半：宽 = 工作区宽 ÷ 2、
    高 = 工作区高。用户定案：窗口物理尺寸恒为半屏（跨缩放档位不变，与
    175% 时代的标定占屏一致）；只钉尺寸，位置由用户自由摆放。任务栏可见
    与否不影响——工作区已扣除。area 非法时回退标定尺寸（175% 半屏的
    实测值 1300×1610，由调用方按工作区再收口）。"""
    try:
        l, t, r, b = (float(v) for v in work_area)
        w, h = r - l, b - t
    except (TypeError, ValueError):
        return WINDOW_SIZE
    if w <= 0 or h <= 0:
        return WINDOW_SIZE
    return (max(400, int(w) // 2), max(400, int(h)))

# 标定窗口物理尺寸（175% 档，2560x1600 屏右半；也是 target_window_size 的
# 回退值）。位置由用户自由摆放，只在窗口（部分）移出屏幕时拉回可见范围
# （见 visual_backend 的 enforce_window_geometry / pull_window_into_view）。
# 上面三组锚点与全部像素判据（头像高 63、期望头像带、气泡/媒体闸）都是
# 在这台窗口（175% 缩放）上标定的——元素物理尺寸随档位 ×k 换算（口径表），
# 窗口物理尺寸恒为半屏不随档位变；窗口被拖出屏幕则点击落到屏幕外，切会话/
# 点图/发送全失灵。用户自定义尺寸/区域两项设置已撤回，改为程序强制：
# `VisualBackend.enforce_window_geometry` 在 connect 与每轮消息监听时复查并
# 调回。
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
