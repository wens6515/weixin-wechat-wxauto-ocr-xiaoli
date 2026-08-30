# -*- coding: utf-8 -*-
"""快档轮询真机基准：PrintWindow 截图 + 红圈像素检测的单轮成本、CPU 占用、
以及降采样检测对真红圈的识别一致性（A/B）。

模拟 iter_unread_sessions 的热路径（后台静默截图，不置前、不点击、不 OCR），
按指定间隔连续轮询；每帧同屏跑三档检测：
  A 全分辨率  B 降采样50%（BILINEAR）  C 降采样33%（BILINEAR）
统计各档耗时段差与红圈识别一致性（B/C 相对 A 的漏检），并给出两种
生产方案的每轮工作量与 CPU 占用估算。另测一次会话名 OCR
（真实循环中「检测到红圈那几轮」的附加成本）。

用法：
    python tools/poll_benchmark.py [轮数] [间隔秒]

默认 120 轮 × 0.4s 间隔（约 50 秒）。只做被动截图与像素/OCR 运算，
不切换会话、不点击、不发送任何输入，不会消耗未读角标。
"""
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image

from wx_backend.visual_backend import (
    VisualBackend,
    capture_window,
    find_wechat_window,
    _detect_red_clusters,
    _SESSION_REGION_RATIO,
)


def region_of(w, h):
    return (int(w * _SESSION_REGION_RATIO[0]), int(h * _SESSION_REGION_RATIO[1]),
            int(w * _SESSION_REGION_RATIO[2]), int(h * _SESSION_REGION_RATIO[3]))


def ms(t):
    return t * 1000


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 0.4

    hwnd = find_wechat_window()
    if not hwnd:
        print("未找到微信窗口，请确认微信已登录并打开（不能最小化）")
        sys.exit(1)

    shot = capture_window(hwnd)
    if shot is None:
        print("截图失败（窗口可能被最小化）")
        sys.exit(1)
    print(f"微信窗口 0x{hwnd:x} 尺寸 {shot.size[0]}x{shot.size[1]}，"
          f"快档 {interval}s/轮 × {rounds} 轮（约 {rounds * interval:.0f} 秒）...")

    cap_ms, full_ms = [], []
    half_resize_ms, half_ms = [], []
    third_resize_ms, third_ms = [], []
    counts = []  # 每轮 (n_full, n_half, n_third)

    t0 = time.perf_counter()
    cpu0 = time.process_time()
    for i in range(rounds):
        loop_t = time.perf_counter()
        a = time.perf_counter()
        shot = capture_window(hwnd)
        cap_ms.append(ms(time.perf_counter() - a))
        if shot is not None:
            w, h = shot.size
            a = time.perf_counter()
            n_full = len(_detect_red_clusters(shot, region=region_of(w, h)))
            full_ms.append(ms(time.perf_counter() - a))

            a = time.perf_counter()
            half = shot.resize((w // 2, h // 2), Image.BILINEAR)
            half_resize_ms.append(ms(time.perf_counter() - a))
            a = time.perf_counter()
            n_half = len(_detect_red_clusters(half, region=region_of(w // 2, h // 2)))
            half_ms.append(ms(time.perf_counter() - a))

            a = time.perf_counter()
            third = shot.resize((w // 3, h // 3), Image.BILINEAR)
            third_resize_ms.append(ms(time.perf_counter() - a))
            a = time.perf_counter()
            n_third = len(_detect_red_clusters(third, region=region_of(w // 3, h // 3)))
            third_ms.append(ms(time.perf_counter() - a))

            counts.append((n_full, n_half, n_third))
            if (i + 1) % 10 == 0:
                last = counts[-10:]
                print(f"  第 {i + 1:3d} 轮 | 近10轮红圈 A全/B半/C三: "
                      f"{[x[0] for x in last]} / {[x[1] for x in last]} / {[x[2] for x in last]}")
        remain = interval - (time.perf_counter() - loop_t)
        if remain > 0:
            time.sleep(remain)
    wall = time.perf_counter() - t0
    cpu = time.process_time() - cpu0

    n = len(counts)
    cap_mean = statistics.mean(cap_ms)
    cost_a = cap_mean + statistics.mean(full_ms)
    cost_b = cap_mean + statistics.mean(half_resize_ms) + statistics.mean(half_ms)
    cost_c = cap_mean + statistics.mean(third_resize_ms) + statistics.mean(third_ms)
    print(f"\n== 单轮成本（{n} 轮有效，墙钟 {wall:.1f}s） ==")
    print(f"截图（PrintWindow）        均值 {cap_mean:.1f}ms  最大 {max(cap_ms):.1f}ms")
    print(f"A 全分辨率检测             均值 {statistics.mean(full_ms):.1f}ms  最大 {max(full_ms):.1f}ms")
    print(f"B 50% resize+检测  {statistics.mean(half_resize_ms):.1f} + {statistics.mean(half_ms):.1f} = "
          f"{statistics.mean(half_resize_ms) + statistics.mean(half_ms):.1f}ms")
    print(f"C 33% resize+检测  {statistics.mean(third_resize_ms):.1f} + {statistics.mean(third_ms):.1f} = "
          f"{statistics.mean(third_resize_ms) + statistics.mean(third_ms):.1f}ms")
    print(f"每轮工作量  A {cost_a:.1f}ms   B {cost_b:.1f}ms   C {cost_c:.1f}ms")
    print(f"CPU 估算（单核口径）  A {cost_a / (interval * 1000) * 100:.1f}%  "
          f"B {cost_b / (interval * 1000) * 100:.1f}%  C {cost_c / (interval * 1000) * 100:.1f}%；"
          f"本进程实测 {cpu / wall * 100:.1f}%（含 B/C 对比开销）")

    print(f"\n== 红圈识别一致性（以 A 全分辨率为基准，{n} 轮） ==")
    first_badge = next((i + 1 for i, c in enumerate(counts) if c[0] > 0), None)
    with_badge = [c for c in counts if c[0] > 0]
    print(f"A 检出红圈 {len(with_badge)}/{n} 轮"
          + (f"（首次出现于第 {first_badge} 轮）" if first_badge else "——测试期间无红圈"))
    if with_badge:
        miss_b = sum(1 for c in counts if c[0] > 0 and c[1] == 0)
        miss_c = sum(1 for c in counts if c[0] > 0 and c[2] == 0)
        agree_b = sum(1 for c in counts if c[0] == c[1])
        agree_c = sum(1 for c in counts if c[0] == c[2])
        extra_b = sum(1 for c in counts if c[0] == 0 and c[1] > 0)
        extra_c = sum(1 for c in counts if c[0] == 0 and c[2] > 0)
        print(f"B 50%: 数量逐轮一致 {agree_b}/{n}，漏检 {miss_b} 轮，多检 {extra_b} 轮")
        print(f"C 33%: 数量逐轮一致 {agree_c}/{n}，漏检 {miss_c} 轮，多检 {extra_c} 轮")

    # 附加：会话名 OCR 成本（真实循环中「检测到红圈那几轮」的下一步动作）
    try:
        backend = VisualBackend(poll_region=False)
        backend.connect()
        shot3 = capture_window(backend._hwnd)
        backend._extract_session_names(shot3)  # 预热 OCR 引擎
        ts = []
        for _ in range(3):
            a = time.perf_counter()
            backend._extract_session_names(shot3)
            ts.append(ms(time.perf_counter() - a))
        close = getattr(backend, "close", None)
        if callable(close):
            close()
        print(f"\n== 会话名 OCR（检测到红圈的轮次才会发生）== 热均值 {statistics.mean(ts):.0f}ms/次")
    except Exception as e:
        print(f"\nOCR 附加成本测量失败（不影响主结论）: {e}")


if __name__ == "__main__":
    main()
