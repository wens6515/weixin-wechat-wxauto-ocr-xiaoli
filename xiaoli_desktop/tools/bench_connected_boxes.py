# -*- coding: utf-8 -*-
"""_connected_boxes 三方案基准：现行 Python 逐行循环 / numpy 段级向量化 /
scipy 膨胀+label（C 实现），在模拟真实负载的掩码上对比耗时与结果一致性。

负载形态说明：find_bubble_boxes 的掩码是「近气泡色像素」，气泡为实心色块
被文字笔画打洞（洞是笔画状小块），因此每行被切成多个段——这是真实热路径
的负载，不是理想化的几个大矩形。三个场景：
  bubbles  7 个大块（对方/自己气泡，250-480 x 70-160）+ 笔画洞（典型消息区）
  dense    20 个小块（表情/文件卡片密集场景）
  large    2 个超大块（长图/大段引用）

scipy 版语义说明：binary_dilation(y±6, x±8) + label 与原实现「行内 x_gap
切段 + 跨行重叠合并 + y_gap 行隙」近似但不严格等价（跨行合并从「x 直接
重叠」放宽为「x 距离 ≤8」），输出一致性单独统计供产品化决策参考。

用法：python tools/bench_connected_boxes.py
"""
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wx_backend.visual_backend import _connected_boxes  # 现行实现（真代码）  # noqa: E402

X_GAP, Y_GAP, MIN_H, MIN_W = 8, 6, 20, 40


def _connected_boxes_numpy(mask, min_h=MIN_H, min_w=MIN_W,
                           x_gap=X_GAP, y_gap=Y_GAP):
    """numpy 段级版（与现行实现精确等价）：
    行内切段语义原版是「相邻 True 像素列差 >x_gap 切段」——直接全图向量化
    复刻：一次 np.nonzero（C 序 = 行升序、行内列升序）取全部 True 像素，
    切分点 = 相邻像素换行 或 列差 >x_gap，向量化切出全部行段；段间合并
    与现行实现逐字一致（首个命中 box 合并、y 隙 ≤y_gap、x 直接重叠）。
    无闭运算、无近似、零依赖。"""
    rows, cols = np.nonzero(mask)
    if len(rows) == 0:
        return []
    # 切分点：与前一 True 像素不在同一行，或列差 >x_gap
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


def _connected_boxes_scipy(mask, min_h=MIN_H, min_w=MIN_W,
                           x_gap=X_GAP, y_gap=Y_GAP):
    """scipy 版（C 实现）：y±6 / x±8 膨胀桥接间隙 → label 连通标记 →
    find_objects 取 bbox → 缩回膨胀量 → 尺寸过滤。"""
    from scipy import ndimage
    if not mask.any():
        return []
    struct = np.ones((2 * y_gap + 1, 2 * x_gap + 1), dtype=bool)
    dil = ndimage.binary_dilation(mask, structure=struct)
    lab, _n = ndimage.label(dil)
    out = []
    for sl in ndimage.find_objects(lab):
        t = sl[0].start + y_gap
        b = sl[0].stop - 1 - y_gap
        l = sl[1].start + x_gap
        r = sl[1].stop - 1 - x_gap
        if (b - t) >= min_h and (r - l) >= min_w:
            out.append((t, b, l, r))
    return out


def make_mask(seed, blocks, hole_ratio=0.22, w=747, h=1135):
    """模拟消息区掩码：实心色块 + 笔画状小洞（洞把每行切成多段，贴近
    find_bubble_boxes 在真机截图上的真实负载）。"""
    rng = np.random.default_rng(seed)
    mask = np.zeros((h, w), dtype=bool)
    for bw, bh in blocks:
        x0 = int(rng.integers(0, w - bw))
        y0 = int(rng.integers(0, h - bh))
        mask[y0:y0 + bh, x0:x0 + bw] = True
        n_holes = int(bw * bh * hole_ratio / 90)
        for _ in range(n_holes):
            hw = int(rng.integers(2, 9))
            hh = int(rng.integers(4, 26))
            hx = int(rng.integers(x0, x0 + bw - hw))
            hy = int(rng.integers(y0, y0 + bh - hh))
            mask[hy:hy + hh, hx:hx + hw] = False
    return mask


SCENES = {
    "bubbles（7 个大气泡块+笔画洞）": make_mask(1, [(420, 130), (360, 90), (480, 160),
                                                    (300, 75), (250, 70), (390, 120), (340, 100)]),
    "dense（20 个小块密集）": make_mask(2, [(90, 55), (70, 45), (110, 65), (80, 50),
                                            (60, 42), (120, 70), (85, 48), (100, 60),
                                            (75, 44), (95, 58), (65, 40), (115, 68),
                                            (88, 52), (78, 46), (105, 62), (92, 54),
                                            (68, 41), (125, 72), (82, 49), (98, 57)]),
    "large（2 个超大块）": make_mask(3, [(520, 320), (500, 280)]),
}

# scipy 版仅在环境装有 scipy 时纳入对比（基准结论：binary_dilation 对大
# 结构元素是卷积级开销，慢 8~35 倍且语义宽松，已出局——产品零依赖）
VERSIONS = [
    ("现行（numpy 段级）", _connected_boxes),
    ("numpy 段级（独立副本）", _connected_boxes_numpy),
]
try:
    import scipy  # noqa: F401
    VERSIONS.append(("scipy 膨胀+label", _connected_boxes_scipy))
except ImportError:
    pass


def main():
    rounds = 10
    print(f"掩码尺寸 747x1135（真机消息区 1x），每方案 {rounds} 轮取平均\n")
    for name, mask in SCENES.items():
        n_seg = int(mask.sum())
        print(f"== {name} ==  掩码像素 {n_seg}")
        ref_boxes = None
        for vname, fn in VERSIONS:
            # 预热一次（scipy 首次调用含编译/导入开销）
            boxes = fn(mask)
            t0 = time.perf_counter()
            for _ in range(rounds):
                boxes = fn(mask)
            dt = (time.perf_counter() - t0) / rounds * 1000
            if ref_boxes is None:
                ref_boxes = sorted(boxes)
                same = "（基准）"
            else:
                same = "结果一致" if sorted(boxes) == ref_boxes \
                    else f"结果差异（{len(boxes)} vs {len(ref_boxes)} 框）"
            print(f"  {vname:<18} {dt:8.2f} ms/次   {len(boxes):3d} 框   {same}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
