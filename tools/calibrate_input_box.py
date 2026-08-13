# -*- coding: utf-8 -*-
"""标定微信输入框（打字区域）坐标。

运行：.venv\\Scripts\\python.exe tools\\calibrate_input_box.py

步骤：
1. 打开微信主窗口（保持可见，不要最小化）
2. 把鼠标移到微信聊天输入框（打字区域）的正中间
3. 按 F8 记录当前位置（按 ESC 取消）

脚本会把输入框中心换算成「相对窗口宽高的比例」，存到
xiaoli_desktop\\wx_backend\\wx_input_box.json，供 visual_backend._input_box_rect
读取——发送文字/文件/图片时点击该位置聚焦输入框。

背景：_input_box_rect 原本用硬编码比例（y=窗口顶+0.82*高）定位输入框，
未真机标定。不同分辨率/窗口尺寸下可能点偏，导致发送的文字打进错误位置
或根本没聚焦。此脚本一次性标定后，发送操作点击真实输入框中心。
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import time

# 项目根 = tools/ 的上级
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_XIAOLI_DIR = os.path.join(_BASE, "xiaoli_desktop")
sys.path.insert(0, _XIAOLI_DIR)

from wx_backend.visual_backend import find_wechat_window  # noqa: E402

u32 = ctypes.windll.user32

VK_F8 = 0x77
VK_ESC = 0x1B


def _get_cursor_pos():
    pt = wt.POINT()
    u32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _get_window_rect(hwnd):
    rect = wt.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def main():
    hwnd = find_wechat_window()
    if hwnd is None:
        print("❌ 未找到微信主窗口，请先打开并登录微信")
        return 1

    l, t, r, b = _get_window_rect(hwnd)
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        print("❌ 微信窗口尺寸异常，请确认窗口可见后重试")
        return 1

    print(f"微信窗口：左上({l},{t}) 尺寸 {w}x{h}")
    print("请把鼠标移到微信聊天输入框（打字区域）的正中间，然后按 F8")
    print("按 ESC 取消")

    while True:
        if u32.GetAsyncKeyState(VK_ESC) & 0x8000:
            print("已取消")
            return 1
        if u32.GetAsyncKeyState(VK_F8) & 0x8000:
            cx, cy = _get_cursor_pos()
            if not (l <= cx <= r and t <= cy <= b):
                print("⚠ 鼠标不在微信窗口内，请移到输入框正中间再按 F8")
                time.sleep(0.5)
                continue
            fx = (cx - l) / w
            fy = (cy - t) / h
            out = os.path.join(_XIAOLI_DIR, "wx_backend", "wx_input_box.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump({"fx": round(fx, 4), "fy": round(fy, 4)},
                          f, ensure_ascii=False, indent=2)
            print(f"✅ 已记录输入框中心：屏幕({cx},{cy}) 相对比例({fx:.4f},{fy:.4f})")
            print(f"   已保存到 {out}")
            return 0
        time.sleep(0.05)


if __name__ == "__main__":
    sys.exit(main())
