# -*- coding: utf-8 -*-
"""图片消息点击位置校准脚本（小漓 bot 专用）

背景：bot 自动点击图片消息时用的是消息控件 BoundingRectangle 的中心点，
但竖图/某些尺寸的图片实际可点击区域与中心有偏差，导致点不到打不开预览。
本脚本让你手动点击"真正能打开预览的位置"，算出与控件中心的偏移量，
把结果发回给天枢写入 config.json 的 image_click_offset。

用法：
    python calibrate_click.py [会话名]
    （不带参数会列出所有会话让你选；直接输入会话名更快）

流程（对最近的若干张图片消息逐条执行）：
    1. 脚本把消息滚动到可见，打印控件中心坐标 (cx, cy)
    2. 你把鼠标移到该图片消息上"真正能点开预览"的位置（横图竖图都测）
    3. 按回车，脚本记录你点击的位置 (ux, uy)，计算偏移 [ux-cx, uy-cy]
    4. 全部测完后，把打印的「偏移量汇总」发回给天枢

注意：
    - 运行前确保微信窗口可见、不被其他窗口遮挡
    - 鼠标移动到目标位置后不要再动，直接按回车
    - 建议横图、竖图各测 2-3 张，取稳定值
"""
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pyautogui
from wxauto4 import WeChat
from wxauto4.msgs.mtype import ImageMessage

RESULTS = []  # 每张图的 (会话名, 控件中心, 实际点击, 偏移)


def pick_session(wx, target):
    """返回会话名：优先 target；否则列出全部让用户选"""
    sessions = wx.GetSession()
    names = []
    for s in sessions:
        name = s.name if hasattr(s, "name") else (s if isinstance(s, str) else str(s))
        if name:
            names.append(name)

    if target:
        for n in names:
            if target in n:
                return n
        print(f"未找到包含「{target}」的会话，可用会话如下：")
        for i, n in enumerate(names):
            print(f"  {i + 1}. {n}")
        return None

    print("可用会话：")
    for i, n in enumerate(names):
        print(f"  {i + 1}. {n}")
    choice = input("输入序号或会话名（回车跳过 = 不校准）: ").strip()
    if not choice:
        return None
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(names):
            return names[idx]
        print("序号超出范围")
        return None
    return choice


def calibrate_one(wx, chat_name, msg, idx):
    """对单张图片消息做一次校准，返回偏移 [dx, dy] 或 None"""
    try:
        msg.roll_into_view()
        time.sleep(0.6)  # 等滚动稳定
    except Exception as e:
        print(f"  [{idx}] 滚动失败: {e}")
        return None

    control = getattr(msg, "control", None)
    if not control or not hasattr(control, "BoundingRectangle"):
        print(f"  [{idx}] 无法获取控件，跳过")
        return None

    rect = control.BoundingRectangle
    cx = rect.left + rect.width() // 2
    cy = rect.top + rect.height() // 2
    print(f"  [{idx}] 控件中心=({cx}, {cy})  尺寸={rect.width()}x{rect.height()}")
    print(f"        → 请把鼠标移到这张图片上「真正能点开预览」的位置，然后按回车...")
    input()
    ux, uy = pyautogui.position()
    dx, dy = ux - cx, uy - cy
    print(f"        → 你点击=({ux}, {uy})   偏移=[{dx}, {dy}]")
    RESULTS.append((chat_name, (cx, cy), (ux, uy), (dx, dy)))
    return (dx, dy)


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None
    print("正在连接微信（保持微信登录状态）...")
    wx = WeChat(resize=False, ads=False)
    print("微信连接成功")

    chat_name = pick_session(wx, target)
    if not chat_name:
        print("未选择会话，退出")
        return

    print(f"打开会话：{chat_name}")
    wx.ChatWith(chat_name)
    time.sleep(0.5)
    msgs = wx.GetAllMessage()
    if not msgs:
        print("该会话没有消息")
        return

    images = [m for m in msgs if isinstance(m, ImageMessage)]
    if not images:
        print(f"该会话最近 {len(msgs)} 条消息中没有图片消息（ImageMessage）")
        return

    print(f"共找到 {len(images)} 张图片消息，取最近最多 5 张校准：")
    for i, msg in enumerate(images[-5:], 1):
        calibrate_one(wx, chat_name, msg, i)
        print()

    print("=" * 50)
    print("偏移量汇总（请发回给天枢）：")
    for chat, c, u, off in RESULTS:
        print(f"  会话[{chat}] 中心{c} 实际{u} 偏移{list(off)}")
    if RESULTS:
        avgs = [sum(r[3][0] for r in RESULTS) // len(RESULTS),
                sum(r[3][1] for r in RESULTS) // len(RESULTS)]
        print(f"平均偏移: [{avgs[0]}, {avgs[1]}]")
        print("说明：横图竖图偏移差异大时，把每条单独发给我，不要只发平均。")
    else:
        print("（没有成功记录任何数据）")


if __name__ == "__main__":
    main()
