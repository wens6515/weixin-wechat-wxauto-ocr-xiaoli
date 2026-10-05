# -*- coding: utf-8 -*-
"""visual_backend 拆分 · 视觉原语层：气泡色探测 / 连通域 / 媒体框 / 头像检测 / 文件卡片图标。

纯像素判定（无 OCR、无 Win32 调用）。全部被门面 re-export；组合层
analyze_blocks 留在门面——它调用本模块原语的名字落在门面命名空间，
测试的 mock.patch("wx_backend.visual_backend.X") 语义不变。
"""
from __future__ import annotations

from PIL import Image

def detect_bubble_colors(img: Image.Image) -> dict:
    """自动探测消息区背景色 / 对方气泡色 / 自己气泡色（1x RGB 图）。

    微信两套主题的气泡色实测（真机探针）：
    - 深色主题：背景(30,30,31)、对方气泡(47,47,48)、自己气泡绿(53,210,141)
    - 浅色主题：背景白灰、对方气泡白、自己气泡绿(149,236,105)
    探测失败项为 None。策略：
    - 背景：边缘 8px 像素中位数（边缘通常无气泡、无文字）
    - 自己气泡：绿色像素中位数（G 显著高于 R/B，微信品牌绿两主题皆绿）
    - 对方气泡：非背景、非绿色像素的中位数（深色=深灰、浅色=白）
    """
    import numpy as np
    arr = np.asarray(img.convert("RGB"), dtype=np.int16)
    h, w, _ = arr.shape
    if h < 16 or w < 16:
        return {"bg": None, "other": None, "self": None}
    border = np.concatenate([
        arr[:8].reshape(-1, 3), arr[-8:].reshape(-1, 3),
        arr[:, :8].reshape(-1, 3), arr[:, -8:].reshape(-1, 3),
    ])
    bg = tuple(int(v) for v in np.median(border, axis=0))
    R, G, B = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    green = (G - R > 40) & (G - B > 40) & (G > 100)
    self_color = None
    if int(green.sum()) > 80:
        self_color = tuple(int(v) for v in np.median(arr[green], axis=0))
    bgdist = np.abs(arr - np.array(bg, dtype=np.int16)).sum(axis=2)
    # 对方气泡：与背景「接近但不同」的非绿色像素。深色主题深灰比背景亮
    # ~17 色阶/分量（总差 ~51），浅色主题白气泡比背景亮（总差 ~45）；
    # 图片内容颜色多样、与背景差异大（总差常 >100），不纳入 other——
    # 否则大块图片会污染 other 中位数，导致气泡色探测错。
    # 真机补充：深色图片（群二维码 (15,15,17)）比背景暗，但 bgdist 落在
    # 10~90 内被 other_mask 圈进，把 other 中位数从 (47,47,48) 拉低成
    # (39,39,41)，接近背景后 find_bubble_boxes tol=12 把背景也当气泡吞了。
    # 加「比背景亮」约束：对方气泡永远比背景亮，深色图片被排除。
    other_mask = (bgdist > 10) & (bgdist < 90) & (~green)
    brighter = arr.sum(axis=2) > np.array(bg, dtype=np.int16).sum()
    other_mask &= brighter
    other_color = None
    if int(other_mask.sum()) > 80:
        other_color = tuple(int(v) for v in np.median(arr[other_mask], axis=0))
    return {"bg": bg, "other": other_color, "self": self_color}


def _near_color(arr, color, tol):
    """返回与 color 接近（RGB 各分量差 ≤ tol）的布尔掩码。"""
    import numpy as np
    d = np.abs(arr - np.array(color, dtype=np.int16))
    return (d[:, :, 0] <= tol) & (d[:, :, 1] <= tol) & (d[:, :, 2] <= tol)


def _connected_boxes(mask, min_h=20, min_w=40, x_gap=8, y_gap=6):
    """把布尔掩码聚合成连通域边界框 [(top, bottom, left, right)]（像素坐标）。

    行内切段语义 = 相邻 True 像素列差 >x_gap 切段——一次 np.nonzero 全图
    向量化取全部 True 像素后直接切分（np.nonzero 按 C 序返回：行升序、
    行内列升序），替代旧的逐行 np.where + Python 切段循环；跨行合并保持
    流式贪心（y 隙 ≤y_gap 且 x 重叠，并入首个命中框）。真机消息区尺寸
    （747x1135）基准较旧逐行循环快 3.4~6.2 倍、输出逐场景一致
    （tools/bench_connected_boxes.py）。过滤掉小噪声（气泡至少高 20px、
    宽 40px）。"""
    import numpy as np
    rows, cols = np.nonzero(mask)
    if len(rows) == 0:
        return []
    brk = (rows[1:] != rows[:-1]) | (cols[1:] - cols[:-1] > x_gap)
    cut = np.flatnonzero(brk) + 1
    starts = np.concatenate(([0], cut))
    ends = np.concatenate((cut, [len(rows)]))  # exclusive
    boxes = []
    for y, l, r in zip(rows[starts].tolist(), cols[starts].tolist(),
                       (cols[ends - 1]).tolist()):
        merged = False
        for b in boxes:
            if b[1] >= y - y_gap and not (r < b[2] or l > b[3]):
                b[1] = y
                b[2] = min(b[2], l)
                b[3] = max(b[3], r)
                merged = True
                break
        if not merged:
            boxes.append([y, y, l, r])
    return [(t, b, l, r) for (t, b, l, r) in boxes
            if (b - t) >= min_h and (r - l) >= min_w]


def find_bubble_boxes(img: Image.Image, colors: dict) -> list[tuple]:
    """用颜色连通域找气泡边界框 [(top, bottom, left, right, is_self)]（1x 坐标）。

    is_self：气泡是绿色（自己发的）为 True，否则 False。

    对方气泡色（深色主题下深灰）与背景差异仅 ~17 色阶，图片消息暗部/文字
    会把深灰像素连成一片、聚合出接近全屏的异常框——过滤掉高度超消息区
    60% 的框（正常对方气泡 < 60%），被过滤时调用方回退 y 阈值处理对方消息。
    """
    import numpy as np
    arr = np.asarray(img.convert("RGB"), dtype=np.int16)
    boxes = []
    if colors.get("self"):
        mask = _near_color(arr, colors["self"], tol=60)
        for (t, b, l, r) in _connected_boxes(mask):
            boxes.append((t, b, l, r, True))
    if colors.get("other"):
        # 对方气泡色与背景差异小（深色主题仅 ~17 色阶），tol 要收紧
        mask = _near_color(arr, colors["other"], tol=12)
        # 排除右侧滚动条：滚动条（深色主题灰 ~(36,36,38)）与 other 色
        # (~47,47,48) 仅差 ~11 色阶，落在 tol=12 内被误判为 other，贯穿
        # 整图聚成接近全屏的异常框，把真实对方气泡（如群聊 @ 消息）一起
        # 吞掉后又被 60% 高度过滤丢弃（真机根因：群聊 @ 读不到消息）。
        # 滚动条固定在消息区最右侧（x/w ≥ 0.99），直接掩掉。
        mask[:, int(img.width * 0.99):] = False
        for (t, b, l, r) in _connected_boxes(mask):
            if (b - t) < img.height * 0.6:
                boxes.append((t, b, l, r, False))
    return boxes


def find_media_boxes(img: Image.Image, colors: dict) -> list[tuple]:
    """检测图片/视频/表情的内容矩形（非背景、非气泡色的大块连通域）。

    媒体消息（图片/视频/动画表情）在消息区是一块非背景、非气泡色的大矩形
    （图片内容本身），无气泡包裹。排除 bg/other/self 三种色后剩余的大块
    连通域即媒体内容——用于图片消息精确定位（点击中心打开预览），替代
    整屏截图降级。返回 [(top, bottom, left, right)]（1x 坐标）。

    尺寸阈值 40px + 头像带整体剔除：头像固定在消息区最左/最右 ~15% 带内
    （真机 ~62px 高），旧实现靠 min 80px 挡头像——代价是 ~40px 的小表情
    全部漏检。改为把左右头像带从内容掩码整体剔除（头像结构上不可能落到
    带外，气泡/媒体也不会伸进带内），阈值即可安全降到 40（小表情渲染
    高度 40~55px）。
    """
    import numpy as np
    arr = np.asarray(img.convert("RGB"), dtype=np.int16)
    h, w, _ = arr.shape
    content = np.ones((h, w), dtype=bool)
    # bg 阈值收紧到 8：深色图片（如群二维码 (15,15,17)）与深色背景
    # (30,30,31) 各分量只差 ~15，旧 tol=30 把整张黑图当背景排除，media
    # 只检出图片中间一小段彩色区，顶部对不齐头像。收紧后深色内容不再被
    # 当背景，media 框覆盖整张图、顶部对齐头像。
    for key, tol in (("bg", 8), ("other", 12), ("self", 60)):
        c = colors.get(key)
        if c is not None:
            content &= ~_near_color(arr, c, tol)
    # 左右头像带剔除（detect_avatar_tops 的窄带几何：左 [0.02,0.14]、
    # 右 [0.84,0.98]，各留余量）——头像误检从「靠尺寸猜」变「结构上不可能」
    content[:, :int(w * 0.15)] = False
    content[:, int(w * 0.85):] = False
    return _connected_boxes(content, min_h=40, min_w=40)


def filter_media_boxes(boxes: list[tuple], min_top: int | None = None,
                       exclude_rows: list[tuple[int, int]] | None = None,
                       ) -> list[tuple]:
    """媒体框过滤（纯几何，模块级供单测复用）：
    - min_top：只保留 top ≥ 阈值的框（上层传 bot_bottom，排除 bot 历史
      媒体与其文件卡片图标碎片）
    - exclude_rows：y 区间 [(top, bottom), ...]，与任一区间垂直相交的框
      剔除——文件卡片的类型图标（W/PDF 彩色小方块）会从面板跳出成独立
      小媒体框（面板本身判气泡），与文件名行同块垂直相交；消息纵向堆叠
      保证跨块必不相交，真实图片不受影响
    """
    if min_top is not None:
        boxes = [(t, b, l, r) for (t, b, l, r) in boxes if t >= min_top]
    for (rt, rb) in (exclude_rows or []):
        boxes = [(t, b, l, r) for (t, b, l, r) in boxes if b < rt or t > rb]
    return list(boxes)


def _bucket_avatar(top: int, tops: list[int]) -> int | None:
    """头像划块归属：top 落入排序头像边界序列的哪个区间。

    窗口内左右头像 top 混合排序成边界序列：区间 [tops[i], tops[i+1])
    归属 tops[i]（最后一个区间向 +∞ 延伸）。消息框（气泡/media）顶部 y
    落入哪个区间 → 归属该区间下边界头像，再按该头像 x 侧定 self/对方。
    替代旧 tol=40 对齐判据——区间归属无魔法数容差。top 低于最上方头像
    （消息顶在最高头像之上，无归属）或 tops 为空时返回 None。
    """
    hit = None
    for t in sorted(tops):
        if top >= t:
            hit = t
        else:
            break
    return hit


def _anchor_avatar(top: int, tops: list[int], tol: int = 8) -> int | None:
    """消息框归属头像（唯一判据 = 头像，用户定案）。

    消息框顶部与头像顶部对齐（真机实测差 0），但渲染取整可能差 1~2px；
    直接走区间归属会把「顶在头像上方 1px」的框错划给上一个头像。先取
    |top - 头像top| ≤ tol 里最近的头像，超出容差才回退区间归属；仍无归属
    （框顶在所有头像之上）返回 None——调用方按「无归属」丢弃，不做任何
    颜色/中线降级。
    """
    best = None
    for t in tops:
        d = abs(top - t)
        if d <= tol and (best is None or d < best[0]):
            best = (d, t)
    if best is not None:
        return best[1]
    return _bucket_avatar(top, tops)


def _contains(outer: tuple, inner: tuple, pad: int = 2) -> bool:
    """inner 框是否落在 outer 框内（外扩 pad 容差）。"""
    ot, ob, ol, orr = outer
    it, ib, il, ir = inner
    return it >= ot - pad and ib <= ob + pad and il >= ol - pad and ir <= orr + pad


def drop_panel_contents(boxes: list[tuple], panels: list[tuple],
                        pad: int = 2) -> list[tuple]:
    """剔除落在面板（气泡框）内部的内容框。

    文件卡片的类型图标（Excel 绿方块 / W·PDF 彩色小方块）会从面板跳出成
    独立小框——它是面板的一部分，不是图片：留在这里会被图片点击路径
    （media_screen_boxes → 点击 → Ctrl+C/裁剪）当成图片点开，真机会直接
    打开用户文件。面板内部一律不产出媒体框。
    """
    return [(t, b, l, r) for (t, b, l, r) in boxes
            if not any(_contains(p, (t, b, l, r), pad) for p in panels)]


def detect_avatar_tops(img: Image.Image, bg, side: str) -> list[int]:
    """检测消息区左侧/右侧头像的顶部 y 列表（几何判据，无需头像模板）。

    头像固定在消息区两侧：对方头像在最左（x/w∈[0.02,0.14]）、自己头像在最右
    （x/w∈[0.84,0.98]）。头像颜色显著区别于背景（真机实测距离 >100，bg 距离 0、
    对方气泡色距离 ~51），高度固定 ≈ 消息区高/18（真机 1135/18=63px）。

    在窄带内找「非背景像素连续行段」，高度落在头像高附近、峰值足够的段即头像，
    返回其顶部 y（头像顶部与消息气泡/媒体顶部对齐，真机实测差 0）。高度不足
    （消息被上下滚动截断、显示不全）或峰值不足（滚动条等噪声）都丢弃。

    side: "left"（对方头像）| "right"（自己头像）；bg 为 None 时返回空。
    """
    import numpy as np
    if bg is None:
        return []
    arr = np.asarray(img.convert("RGB"), dtype=np.int16)
    rh, rw = arr.shape[:2]
    if rw <= 0 or rh <= 0:
        return []
    if side == "right":
        x0, x1 = int(rw * 0.84), int(rw * 0.98)
    else:
        x0, x1 = int(rw * 0.02), int(rw * 0.14)
    if x1 - x0 <= 0:
        return []
    dist = np.abs(arr - np.array(bg, dtype=np.int16)).sum(axis=2)
    counts = (dist[:, x0:x1] > 70).sum(axis=1)  # 每行非背景像素数
    expected_h = max(40, rh // 18)
    min_h = int(expected_h * 0.7)
    max_h = int(expected_h * 1.4)
    min_peak = int(expected_h * 0.4)
    tops = []
    in_seg = False
    seg_start = 0
    for y in range(rh):
        c = int(counts[y])
        if c >= 3 and not in_seg:
            in_seg = True
            seg_start = y
        elif c < 3 and in_seg:
            in_seg = False
            seg_h = y - seg_start
            if min_h <= seg_h <= max_h and int(counts[seg_start:y].max()) >= min_peak:
                tops.append(seg_start)
    if in_seg:
        seg_h = rh - seg_start
        if min_h <= seg_h <= max_h and int(counts[seg_start:rh].max()) >= min_peak:
            tops.append(seg_start)
    return tops


# 文件卡片判据（用户定案）：被标记为多媒体的消息块，若整体颜色与文字气泡
# 一致、只多一个右侧小图标，即文件消息。图标是面板内部的「实心小色块」——
# 真机标定（实时截图）：Excel 图标 69x55、填充率 0.96；同一面板内的文字行
# 高 ≤31、填充率 ≤0.48（含贴到面板右缘的长行），高度 40 / 填充率 0.7 双阈
# 把两者彻底分开。图标颜色不参与判定（Excel 绿 ≈ 微信绿会被 self 通道吞掉，
# PDF/Word 的蓝红方块又会跳成独立媒体框——两种都靠本判据统一处理）。
_ICON_MIN_H = 40
_ICON_MIN_W = 30
_ICON_FILL_MIN = 0.7


def find_panel_icon(arr, panel: tuple, colors: dict) -> tuple | None:
    """在面板（气泡框）内部右侧找文件卡片的类型图标，返回 1x 框或 None。

    arr：消息区 1x RGB 数组（np.int16）；panel：(top, bottom, left, right)。
    只在面板右半找（左半是文件名/大小文字），要求实心（填充率高）、有图标
    量级的高度与宽度，且不贴顶（贴顶块是文件名首行）。
    """
    t, b, l, r = panel
    ph, pw = b - t, r - l
    if ph <= 0 or pw <= 0:
        return None
    c = colors.get("other")
    if c is None:
        return None
    sub = arr[t:b, l:r]
    mask = ~_near_color(sub, c, 12)
    if colors.get("bg") is not None:
        mask &= ~_near_color(sub, colors["bg"], 8)
    mask[:, :int(pw * 0.5)] = False
    best = None
    for (ct, cb, cl, cr) in _connected_boxes(mask, min_h=20, min_w=20):
        h, w = cb - ct, cr - cl
        if h < max(_ICON_MIN_H, int(ph * 0.2)) or w < _ICON_MIN_W:
            continue
        if h > int(ph * 0.85) or w > int(pw * 0.5):
            continue
        fill = float(mask[ct:cb, cl:cr].mean())
        if fill < _ICON_FILL_MIN:
            continue
        score = fill * h * w
        if best is None or score > best[0]:
            best = (score, (t + ct, t + cb, l + cl, l + cr))
    return best[1] if best else None
def region_changed(a: Image.Image, b: Image.Image, region=None,
                   threshold: int = 30) -> bool:
    """像素级差异检测（毫秒级）：region=(l,t,r,b) 局部区域或整图。

    返回 True 表示该区域存在显著变化（像素差绝对值超 threshold 的像素占比 >0.1%）。
    """
    if a is None or b is None:
        return True
    if a.size != b.size:
        return True
    if region:
        rl, rt, rr, rb = region
        a = a.crop((rl, rt, rr, rb))
        b = b.crop((rl, rt, rr, rb))
    diff = Image.new("L", a.size)
    # 用 abs 差值的均值快速估计（避免整图逐像素——PIL 没有内置 abs diff，
    # 用 ImageChops.difference + 直方图）
    try:
        from PIL import ImageChops
        d = ImageChops.difference(a.convert("L"), b.convert("L"))
        hist = d.histogram()
        # 差异像素（亮度差 > threshold）数量
        changed = sum(hist[threshold + 1:])
        total = a.size[0] * a.size[1]
        return changed > total * 0.001
    except Exception:
        return True
