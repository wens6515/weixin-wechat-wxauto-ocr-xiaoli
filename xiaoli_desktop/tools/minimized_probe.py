# -*- coding: utf-8 -*-
"""最小化状态监听探测：验证微信窗口最小化后 PrintWindow 截图与红圈检测是否失效。

流程：记录当前状态 → 可见状态基准帧（亮度/红圈数）→ SW_MINIMIZE 最小化 →
间隔采样 5 帧 → SW_RESTORE 恢复 → 复测。全程不点击、不切换会话、
不发送输入，不会消耗未读角标；结束时恢复原窗口状态。

用法：
    python tools/minimized_probe.py
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import ImageStat

from wx_backend.visual_backend import (
    capture_window,
    find_wechat_window,
    window_rect,
    _detect_red_clusters,
    _SESSION_REGION_RATIO,
)

u32 = ctypes.windll.user32
SW_MINIMIZE, SW_RESTORE = 6, 9


def region_of(w, h):
    return (int(w * _SESSION_REGION_RATIO[0]), int(h * _SESSION_REGION_RATIO[1]),
            int(w * _SESSION_REGION_RATIO[2]), int(h * _SESSION_REGION_RATIO[3]))


def sample(hwnd, tag):
    shot = capture_window(hwnd)
    if shot is None:
        print(f"{tag}: capture_window 返回 None")
        return None
    w, h = shot.size
    n_badge = len(_detect_red_clusters(shot, region=region_of(w, h)))
    bright = ImageStat.Stat(shot.convert("L")).mean[0]
    rect = window_rect(hwnd)
    print(f"{tag}: 窗口rect={rect} 尺寸 {w}x{h} 平均亮度 {bright:.1f} 红圈 {n_badge}")
    return {"size": (w, h), "bright": bright, "badges": n_badge}


def main():
    hwnd = find_wechat_window()
    if not hwnd:
        print("未找到微信窗口")
        sys.exit(1)

    was_iconic = bool(u32.IsIconic(hwnd))
    print(f"窗口 0x{hwnd:x}  当前最小化: {was_iconic}")

    base = sample(hwnd, "[可见基准]")

    u32.ShowWindow(hwnd, SW_MINIMIZE)
    time.sleep(1.5)
    print(f"已最小化  IsIconic: {bool(u32.IsIconic(hwnd))}")
    minis = []
    for i in range(5):
        minis.append(sample(hwnd, f"[最小化 {i + 1}]"))
        time.sleep(1.0)

    u32.ShowWindow(hwnd, SW_RESTORE)
    time.sleep(1.5)
    print(f"已恢复  IsIconic: {bool(u32.IsIconic(hwnd))}")
    after = sample(hwnd, "[恢复后]")

    print("\n== 结论 ==")
    if not was_iconic and base:
        mini_brights = [m["bright"] for m in minis if m]
        mini_sizes = {m["size"] for m in minis if m}
        if mini_brights and max(mini_brights) < 8:
            verdict = "最小化 → 黑帧（内容不可见，红圈检测必然失效）"
        elif mini_sizes and all(s[0] < base["size"][0] // 2 for s in mini_sizes):
            verdict = "最小化 → 占位小尺寸残帧（非真实内容，检测不可信）"
        elif base["badges"] > 0 and all(m and m["badges"] == base["badges"] for m in minis):
            verdict = "最小化 → 截图仍含完整内容且红圈可检（后台监听可行）"
        else:
            verdict = "最小化 → 截图内容异常/冻结帧，需人工核对亮度与红圈数对比"
        print(verdict)
        print(f"亮度对比：可见基准 {base['bright']:.1f}；最小化采样 {['%.1f' % b for b in mini_brights]}；"
              f"恢复后 {after['bright'] if after else '-'}")
        print(f"红圈对比：可见基准 {base['badges']}；最小化 {[m['badges'] if m else '-' for m in minis]}；"
              f"恢复后 {after['badges'] if after else '-'}")


if __name__ == "__main__":
    main()
