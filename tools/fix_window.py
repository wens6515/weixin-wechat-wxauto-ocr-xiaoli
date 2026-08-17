# -*- coding: utf-8 -*-
r"""微信窗口固定工具：手动调整并固定微信主窗口的位置与大小。

背景（见 wxauto_new_feasibility.md / docs 微信通道验证结论）：
新版微信 4.1.12 的视觉通道依赖 PrintWindow 截图 + OCR。窗口位置/大小
漂移会让 OCR 的区域坐标失效。本工具把微信窗口固定到指定位置尺寸，
并把配置写入 wx_window.json，visual_backend 启动时读取并应用同一配置。

用法：
  python tools/fix_window.py                  # 交互式：显示当前，输入 x y w h
  python tools/fix_window.py 50 50 900 920    # 参数式：x y width height
  python tools/fix_window.py --read           # 只显示当前窗口与已存配置
  python tools/fix_window.py --clear          # 清除已存配置（恢复自动）

输出：应用后打印窗口实际位置尺寸，并提示保存路径。
配置：xiaoli_desktop\wx_window.json
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys

_WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "xiaoli_desktop", "wx_window.json",
)


def find_wechat_window():
    """返回微信主窗口句柄（标题含'微信'且可见）；未找到返回 None。"""
    found = []

    @_WNDENUMPROC
    def _cb(hwnd, _lp):
        title = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(hwnd, title, 512)
        if "微信" in title.value and ctypes.windll.user32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    ctypes.windll.user32.EnumWindows(_cb, 0)
    return found[0] if found else None


def get_window_rect(hwnd):
    rect = wt.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return {
        "x": rect.left, "y": rect.top,
        "width": rect.right - rect.left, "height": rect.bottom - rect.top,
    }


def set_window_pos(hwnd, x, y, w, h):
    """固定窗口位置尺寸。SWP_NOZORDER | SWP_NOACTIVATE 不改变层级、不抢焦点。"""
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    ok = ctypes.windll.user32.SetWindowPos(
        hwnd, None, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE)
    return bool(ok)


def load_config():
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except Exception as e:
            print(f"[WARN] 读取配置失败: {e}")
    return None


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"[OK] 配置已保存: {CONFIG_PATH}")
    print(f"     {cfg}")


def main():
    ap = argparse.ArgumentParser(
        description="固定微信主窗口位置与大小（visual_backend 视觉通道依赖）")
    ap.add_argument("args", nargs="*", type=int,
                    help="x y width height（4 个整数）；缺省进入交互模式")
    ap.add_argument("--read", action="store_true", help="只显示当前窗口与已存配置")
    ap.add_argument("--clear", action="store_true", help="清除已存配置")
    ap.add_argument("--save-current", action="store_true",
                    help="把当前窗口实际位置/大小保存为固定配置（不移动窗口）")
    args = ap.parse_args()

    hwnd = find_wechat_window()
    if hwnd is None:
        print("[FAIL] 未找到微信主窗口（请先登录微信）")
        sys.exit(1)

    cur = get_window_rect(hwnd)
    print(f"微信窗口句柄: 0x{hwnd:x}")
    print(f"当前窗口: x={cur['x']} y={cur['y']} {cur['width']}x{cur['height']}")

    if args.read:
        cfg = load_config()
        print(f"已存配置: {cfg if cfg else '（无）'}")
        return
    if args.clear:
        if os.path.isfile(CONFIG_PATH):
            os.remove(CONFIG_PATH)
            print(f"[OK] 已清除配置: {CONFIG_PATH}")
        else:
            print("无配置可清除")
        return

    if args.save_current:
        cfg_out = {"x": cur["x"], "y": cur["y"],
                   "width": cur["width"], "height": cur["height"]}
        save_config(cfg_out)
        print(f"[OK] 已记住当前窗口为固定配置（不移动窗口）")
        return

    # 确定目标位置
    if len(args.args) == 4:
        x, y, w, h = args.args
    else:
        cfg = load_config()
        default = cfg if cfg else cur
        print("\n输入窗口位置与大小（直接回车用默认值）：")
        try:
            x = input(f"  左边缘 x [{default.get('x', 0)}]: ").strip()
            x = int(x) if x else int(default.get("x", 0))
            y = input(f"  上边缘 y [{default.get('y', 0)}]: ").strip()
            y = int(y) if y else int(default.get("y", 0))
            w = input(f"  宽度 width [{default.get('width', 900)}]: ").strip()
            w = int(w) if w else int(default.get("width", 900))
            h = input(f"  高度 height [{default.get('height', 920)}]: ").strip()
            h = int(h) if h else int(default.get("height", 920))
        except (EOFError, KeyboardInterrupt):
            print("\n已取消")
            sys.exit(0)
        except ValueError as e:
            print(f"[FAIL] 输入无效: {e}")
            sys.exit(1)

    if w <= 0 or h <= 0:
        print("[FAIL] 宽高必须为正数")
        sys.exit(1)

    ok = set_window_pos(hwnd, x, y, w, h)
    if not ok:
        print("[FAIL] SetWindowPos 失败")
        sys.exit(1)

    import time
    time.sleep(0.6)  # 等窗口重绘
    after = get_window_rect(hwnd)
    print(f"[OK] 已固定窗口: x={after['x']} y={after['y']} "
          f"{after['width']}x{after['height']}")

    cfg_out = {"x": after["x"], "y": after["y"],
               "width": after["width"], "height": after["height"]}
    save_config(cfg_out)


if __name__ == "__main__":
    main()
