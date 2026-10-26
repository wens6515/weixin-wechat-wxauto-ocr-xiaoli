# -*- coding: utf-8 -*-
"""visual_backend 拆分 · 未读红圈角标检测（固定品牌红 + 连通域面积，纯像素）。

被门面 re-export；region 归一化与列表区默认值取自 visual_regions
（独立成模块避免与门面循环导入）。
"""
from __future__ import annotations

import logging

from PIL import Image

from .visual_regions import _normalize_region, _SESSION_REGION_RATIO
from .visual_scale import area as _area, pxr as _pxr

logger = logging.getLogger(__name__)

# ---------- 未读红圈角标检测 ----------

# 未读角标判据（用户定案）：**固定品牌红**，不再用红色系范围。
# 微信未读角标品牌红 #FA5151 = (250,81,81)，特征 G≈B。真机实测
# （.rivet/scratch/red_probe*.py，窗口 1300x1610）：真角标 518px / 26x27，
# 簇均值 (248,80,80)/(249,81,81)，主体像素逐通道紧贴该值（像素 G/B 分布
# 73~122 的长尾是数字描边/抗锯齿像素）。
# 逐像素 ±5 容差而非零容差：零容差只保得住核心像素，换台机器 GPU 取整/
# 渲染路径差 1 就可能整簇掉出面积闸——失败模式是静默收不到所有消息，
# 比误报严重得多；±5 是「固定色」与「鲁棒」的平衡点。
# 范围判据的排除实例（真机截图逐像素实测）：预览行红心 emoji 簇 299px、
# 中位 (244,83,104)——B 通道比品牌红高 23、G≠B（偏粉），±5 内 0 像素
# 命中；旧范围判据（R≥200、G/B∈[60,130]）对它整体放行（真机误报：
# 红心被当未读红圈反复处理）。橙红 emoji（237,112,37，G≫B）同样出局。
_BADGE_RED = (250, 81, 81)
_BADGE_TOL = 5
# 面积上下限（8 邻接连通域像素数）：微信 PC 角标尺寸固定（约 26x27 ≈ 518px，
# 不随未读条数变宽）。下限排除零星噪声像素，上限排除红色头像/大块红色图形。
_BADGE_AREA_MIN = 60
_BADGE_AREA_MAX = 1000


def _detect_red_clusters(shot: Image.Image, grid: int = 20,
                         min_hits: int = 3,
                         region: tuple | None = None,
                         ) -> list[tuple[int, int, int, int]]:
    """检测会话列表区的未读红圈角标，返回整窗图像坐标 [(l, t, r, b), ...]。

    判定 = 固定品牌红 (250,81,81) 每通道 ±5 + 连通域面积区间。
    实现：crop 列表区 → numpy 三通道掩码 → np.where 取命中点 → 只在命中点
    上跑 8 邻接连通域（真机实测整轮 ~2ms，旧实现的逐像素 Python 扫描整幅
    图要 ~28ms）。

    grid / min_hits：旧网格分桶参数的兼容保留位，判定已由连通域面积取代，
    调用方可照旧传（不再影响结果）。

    shot 为整窗截图；返回坐标以整窗图像像素为基准（列表区 crop 偏移已加回）。
    region 为列表区相对比例 (l, t, r, b)，缺省用模块默认常量（保持模块级函数可测）。
    """
    if shot is None:
        return []
    w, h = shot.size
    sr_region = _normalize_region(region) or _SESSION_REGION_RATIO
    sl = int(w * sr_region[0])
    st = int(h * sr_region[1])
    sr = int(w * sr_region[2])
    sb = int(h * sr_region[3])
    crop = shot.convert("RGB").crop((sl, st, sr, sb))
    rw, rh = crop.size
    if rw <= 0 or rh <= 0:
        return []
    try:
        import numpy as np
        arr = np.asarray(crop)
        r = arr[:, :, 0].astype(np.int16)
        g = arr[:, :, 1].astype(np.int16)
        b = arr[:, :, 2].astype(np.int16)
        mask = ((np.abs(r - _BADGE_RED[0]) <= _BADGE_TOL)
                & (np.abs(g - _BADGE_RED[1]) <= _BADGE_TOL)
                & (np.abs(b - _BADGE_RED[2]) <= _BADGE_TOL))
        ys, xs = np.where(mask)
    except Exception:
        return []
    if len(ys) == 0:
        return []
    seen = np.zeros((rh, rw), dtype=bool)
    out: list[tuple[int, int, int, int]] = []
    for sy, sx in zip(ys.tolist(), xs.tolist()):
        if seen[sy, sx]:
            continue
        # 8 邻接连通域（迭代式，避免深图递归爆栈）
        stack = [(sy, sx)]
        seen[sy, sx] = True
        n = 0
        minx = maxx = sx
        miny = maxy = sy
        while stack:
            cy, cx = stack.pop()
            n += 1
            if cx < minx:
                minx = cx
            elif cx > maxx:
                maxx = cx
            if cy < miny:
                miny = cy
            elif cy > maxy:
                maxy = cy
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = cy + dy, cx + dx
                    if (0 <= ny < rh and 0 <= nx < rw
                            and mask[ny, nx] and not seen[ny, nx]):
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        if _area(_BADGE_AREA_MIN) <= n <= _area(_BADGE_AREA_MAX):
            out.append((sl + minx, st + miny, sl + maxx, st + maxy))
    return sorted(out)


# 红圈锚定的最大容许距离（列表区局部像素）。真机实测：名字块 Δ≈15、
# 角标数字 Δ≈35、时间戳/预览行 Δ≥50（见 .rivet/scratch/verify_anchor2d.py）。
# 超阈值说明红圈位置异常（如误报落在预览行）→ 返回 None，宁可不处理也不猜。
_BADGE_ANCHOR_MAX_DIST = 80


def _pick_block_near_badge(badge_xy: tuple[int, int],
                           blocks: list[dict],
                           max_dist: int | None = None,
                           ) -> dict | None:
    """红圈锚定：取距红圈 (x, y) 二维距离最近的文本块（会话名块）；超阈值 → None。

    为什么二维而非只看 y（真机实测，窗口 1300x1610，列表区局部坐标）：

        真角标 (82,22) → '林小满'(97,22) Δ=15 ；'20:33'(349,24) Δ=269
                         仅比 y：名字 Δy=0 但时间戳 Δy=2 —— 2px 之差会翻车
        红圈在头像位（x≈82），名字紧邻其右（x≈97），时间戳在最右列（x≈349），
        带上 x 后两者差一个数量级。

    badge_xy 与 blocks 必须同一坐标系（调用方传列表区局部坐标）。
    max_dist（175% 标定 80px）按档位换算。
    """
    if not blocks:
        return None
    max_dist = _pxr(_BADGE_ANCHOR_MAX_DIST) if max_dist is None else max_dist
    best = None
    best_d = None
    for it in blocks:
        d = abs(it["x"] - badge_xy[0]) + abs(it["y"] - badge_xy[1])
        if best_d is None or d < best_d:
            best_d = d
            best = it
    if best is None or best_d is None or best_d > max_dist:
        return None
    return best
# 自学习选中高亮：微信列表选中条目有浅灰高亮背景（浅色主题 ≈ #F0F0F0，
# 未选中纯白 #FFFFFF，差值 15~25）。PrintWindow 像素稳定（噪声 <3），
# 容差取 12 夹在两者之间——既能容忍截图噪声，又不会把纯白误判成高亮。
_ROW_COLOR_TOL = 12
# 红圈锚定点击偏移：红圈在头像左上角，条目主体在头像右侧 ~45px
# （与 _anchor_badge 同一真机标定）。
_BADGE_CLICK_OFFSET_X = 45
# 同一行红圈簇的归并容差（像素）：行高约 113px，取 40 足以区分相邻行、又能
# 合并同一行内被切开的重复簇（不需要会话名就能去重）。
_BADGE_ROW_TOL = 40


def _color_close(a: tuple, b: tuple, tol: int = _ROW_COLOR_TOL) -> bool:
    """两个 RGB 颜色是否在每通道容差内相等（自学习选中高亮判定）。"""
    return all(abs(x - y) <= tol for x, y in zip(a, b))
