# -*- coding: utf-8 -*-
"""跨缩放标定探针 v2：对**当前缩放档位**下开着的微信窗口做被动测量。

不点击、不切换会话、不前置窗口——PrintWindow 后台截图 + 纯像素/OCR 测量。
v2 修正：分界用「列表↔聊天空谷」定位；聊天区背景用量化众数；输入面板用
白矩形行检测；引用竖线逐列扫描报精确偏移。

用法：python tools/scale_probe.py
输出：.scale_probe/report.json + full.png / msg_region.png
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PIL import Image

from wx_backend.visual_win32 import (capture_window, find_wechat_window,
                                     monitor_dpi_scale, monitor_work_area,
                                     window_rect)
from wx_backend.visual_backend import ocr_image
from wx_backend.visual_vision import (detect_bubble_colors, find_bubble_boxes,
                                      find_media_boxes, find_panel_icon)

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   ".scale_probe")

REF = {"boundary_px": 541.0, "msg_top": 207.4, "sep_from_bottom": 267.7,
       "title_bottom": 132.0, "avatar_h": 63.0, "row_pitch": 113.0,
       "quote_line_top": 15.5, "quote_x_off": 10.0}


def segments(counts, min_len, max_len, min_peak):
    out, in_seg, start = [], False, 0
    for y, c in enumerate(counts):
        if c >= 3 and not in_seg:
            in_seg, start = True, y
        elif c < 3 and in_seg:
            in_seg = False
            if min_len <= y - start <= max_len and int(counts[start:y].max()) >= min_peak:
                out.append((start, y))
    if in_seg:
        if min_len <= len(counts) - start <= max_len \
                and int(counts[start:].max()) >= min_peak:
            out.append((start, len(counts)))
    return out


def main():
    os.makedirs(OUT, exist_ok=True)
    hwnd = find_wechat_window()
    if not hwnd:
        print("未找到微信窗口")
        return 1
    scale = monitor_dpi_scale(hwnd)
    rect = window_rect(hwnd)
    work = monitor_work_area(hwnd)
    img = capture_window(hwnd).convert("RGB")
    w, h = img.size
    arr = np.asarray(img, dtype=np.int16)
    img.save(os.path.join(OUT, "full.png"))
    k = (scale or 175) / 175.0
    rep = {"scale_pct": scale, "k_vs_175": round(k, 4),
           "window_rect": list(rect), "work_area": list(work),
           "shot_size": [w, h],
           "pred": {"boundary_px": round(REF["boundary_px"] * k, 1),
                    "sep_from_bottom": round(REF["sep_from_bottom"] * k, 1),
                    "avatar_h": round(REF["avatar_h"] * k, 1),
                    "row_pitch": round(REF["row_pitch"] * k, 1),
                    "quote_x_off": round(REF["quote_x_off"] * k, 1),
                    "quote_line_top": round(REF["quote_line_top"] * k, 1)}}

    # ---- 分界：列表↔聊天之间的空谷（列内容占比 < 3% 且宽 8~60px） ----
    y0, y1 = int(h * 0.16), int(h * 0.78)
    probe_bg = tuple(int(v) for v in np.median(
        arr[int(h * 0.30):int(h * 0.55), int(w * 0.44):int(w * 0.52)]
        .reshape(-1, 3), axis=0))
    dist_all = np.abs(arr - np.array(probe_bg, dtype=np.int16)).sum(axis=2)
    col_frac = (dist_all[y0:y1, :] > 15).mean(axis=0)
    gaps, s = [], None
    for x in range(int(w * 0.25), int(w * 0.62)):
        if col_frac[x] < 0.03 and s is None:
            s = x
        elif col_frac[x] >= 0.03 and s is not None:
            if 8 <= x - s <= 60:
                gaps.append([s, x])
            s = None
    rep["gutter_candidates"] = gaps
    boundary = gaps[0][0] + 2 if gaps else int(REF["boundary_px"] * k)
    rep["boundary_x"] = boundary
    rep["boundary_pred_err"] = round(boundary - REF["boundary_px"] * k, 1)

    # ---- 消息区裁剪 + 背景（量化众数） ----
    top = int(REF["title_bottom"] * k) + 6
    crop = img.crop((boundary, top, w, h))
    crop.save(os.path.join(OUT, "msg_region.png"))
    rep["msg_crop_origin"] = [boundary, top]
    carr = np.asarray(crop, dtype=np.int16)
    rh, rw = carr.shape[:2]
    q = (carr.reshape(-1, 3) // 4 * 4).astype(np.int32)
    key = q[:, 0] * 65536 + q[:, 1] * 256 + q[:, 2]
    vals, counts = np.unique(key, return_counts=True)
    best = vals[int(counts.argmax())]
    bg = tuple(int(v) for v in np.median(
        carr.reshape(-1, 3)[key == best], axis=0))
    rep["chat_bg"] = bg
    rep["bg_bucket_share"] = round(float(counts.max()) / key.size, 3)
    colors = detect_bubble_colors(crop)
    rep["bubble_colors"] = {kk: (list(vv) if isinstance(vv, tuple) else vv)
                            for kk, vv in colors.items()}
    cdist = np.abs(carr - np.array(bg, dtype=np.int16)).sum(axis=2)

    # ---- 头像（两侧）+ 左缘精确位置 ----
    exp = REF["avatar_h"] * k
    for side, (fa, fb) in (("left", (0.02, 0.14)), ("right", (0.84, 0.98))):
        x0, x1 = int(rw * fa), int(rw * fb)
        cnt = (cdist[:, x0:x1] > 70).sum(axis=1)
        segs = segments(cnt, int(exp * 0.6), int(exp * 1.5), int(exp * 0.3))
        rep[f"avatar_{side}"] = [{"top": t, "h": b - t} for t, b in segs]
    band = (cdist[:, :int(rw * 0.20)] > 70)
    if rep["avatar_left"]:
        at, ab = rep["avatar_left"][0]["top"], rep["avatar_left"][0]["top"] \
            + rep["avatar_left"][0]["h"]
        cols = np.flatnonzero(band[at:ab].sum(axis=0) >= 10)
        if len(cols):
            rep["avatar_left_off_measured"] = int(cols.min())

    # ---- 气泡面板 / 文件图标 / 媒体框 ----
    panels = [(t, b, l, r) for (t, b, l, r, s_) in
              find_bubble_boxes(crop, colors) if not s_]
    rep["panels"] = [{"box": [int(t), int(b), int(l), int(r)],
                      "size": [int(r - l), int(b - t)],
                      "left_off": int(l),
                      "icon": ([int(v) for v in find_panel_icon(carr, p, colors)]
                               if find_panel_icon(carr, p, colors) is not None
                               else None)}
                     for (t, b, l, r) in panels[:14]]
    rep["media_boxes"] = [[int(v) for v in m]
                          for m in find_media_boxes(crop, colors)][:8]

    # ---- 引用竖线：每个面板下方逐列扫描灰线（宽窗口，报精确偏移） ----
    theme = colors.get("theme", "dark")
    base = np.array((225, 225, 225) if theme == "light" else (53, 53, 54),
                    dtype=np.int16)
    quotes = []
    for (t, b, l, r) in panels[:8]:
        best = None
        for x in range(max(0, l - 6), min(rw, l + int(46 * k))):
            hit = np.abs(carr[:, x] - base).sum(axis=1) <= 12
            lo_w, hi_w = int(round(5 * k)), int(round(31 * k))
            starts = np.flatnonzero(hit[min(b + lo_w, rh - 1):
                                        min(b + hi_w + 1, rh)])
            if starts.size == 0:
                continue
            a = int(starts[0]) + b + lo_w
            btm, gap = a, 0
            for y in range(a, rh):
                if hit[y]:
                    btm, gap = y, 0
                else:
                    gap += 1
                    if gap > 2:
                        break
            if btm - a + 1 >= 20 * k:
                if best is None or btm - a > best["len"]:
                    best = {"len": int(btm - a + 1), "x_off": int(x - l),
                            "top_off": int(a - b)}
        if best:
            quotes.append(best)
    rep["quote_probes"] = quotes

    # ---- 输入面板白矩形（浅色：面板白 255 vs 聊天底 250） ----
    near_white = (carr.min(axis=2) >= 252) if theme == "light" else None
    if near_white is not None:
        frac = near_white[:, int(rw * 0.05):int(rw * 0.92)].mean(axis=1)
        rows = np.flatnonzero(frac > 0.55)
        if len(rows):
            # 底部连续块
            blocks = []
            s = rows[0]
            for a, b2 in zip(rows, rows[1:]):
                if b2 - a > 12:
                    blocks.append((s, a))
                    s = b2
            blocks.append((s, rows[-1]))
            blk = max(blocks, key=lambda t: t[1] - t[0])
            rep["panel_white_rows"] = [int(blk[0] + top), int(blk[1] + top)]
            rep["panel_from_bottom"] = [int(h - blk[1] - top),
                                        int(h - blk[0] - top)]
            xs = np.flatnonzero(near_white[blk[0]:blk[1]].sum(axis=0)
                                > (blk[1] - blk[0]) * 0.5)
            if len(xs):
                rep["panel_x_extent"] = [int(xs.min() + boundary),
                                         int(xs.max() + boundary)]

    # ---- 标题 OCR ----
    tstrip = img.crop((boundary, int(30 * k), w, int(145 * k)))
    rep["title_ocr"] = [t["text"] for t in ocr_image(tstrip)][:8]

    # ---- 会话列表行距 ----
    larr = dist_all[int(h * 0.12):int(h * 0.85), int(w * 0.02):boundary - 12]
    rows = (larr > 40).sum(axis=1)
    bands, s = [], None
    for i, v in enumerate(rows):
        if v >= 3 and s is None:
            s = i
        elif v < 3 and s is not None:
            if i - s >= int(18 * k):
                bands.append((s + int(h * 0.12), i + int(h * 0.12)))
            s = None
    rep["list_bands_n"] = len(bands)
    if len(bands) >= 3:
        starts = [b[0] for b in bands]
        diffs = [b - a for a, b in zip(starts, starts[1:]) if 40 < b - a < 300]
        if diffs:
            rep["list_pitch_measured"] = int(np.median(diffs))

    # ---- 未读红圈 ----
    red = (np.abs(arr[:, :, 0] - 250) <= 5) & (np.abs(arr[:, :, 1] - 81) <= 5) \
        & (np.abs(arr[:, :, 2] - 81) <= 5)
    rep["red_pixels"] = int(red.sum())

    with open(os.path.join(OUT, "report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
