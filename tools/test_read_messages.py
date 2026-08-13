# -*- coding: utf-8 -*-
"""真机实测：连接 visual 后端，读取指定会话的消息内容。

运行：.venv\\Scripts\\python.exe tools\\test_read_messages.py [会话名]

显式用 visual 后端（不降级、不碰 wxauto4）。步骤：
1. PrintWindow 截图找微信窗口 → connect
2. _switch_chat 点击会话列表切到目标会话
3. 消息区截图 + RapidOCR 读文本
4. 打印每条消息的 sender/type/content

输出即证据：如果 OCR 读不到消息，这里会直接看到空列表/乱码，
而不是靠 mock 测试自嗨。
"""
import os
import sys
import time

# 项目根 = tools/ 的上级
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_XIAOLI_DIR = os.path.join(_BASE, "xiaoli_desktop")
sys.path.insert(0, _XIAOLI_DIR)

from wx_backend import create_backend  # noqa: E402
from wx_backend.models import MessageType  # noqa: E402


def main():
    chat = sys.argv[1] if len(sys.argv) > 1 else "王文生"
    print(f"[1/3] 连接 visual 后端（PrintWindow 截图）...")
    try:
        backend = create_backend("visual")
    except Exception as e:
        print(f"❌ 连接失败：{e}")
        print("   请确认微信已登录并打开（窗口可见），且标题含「微信」")
        return 1
    print(f"    ✅ 已连接：{backend.name}")

    print(f"[2/3] 切到会话「{chat}」并读取消息...")
    try:
        msgs = backend.get_messages(chat)
    except Exception as e:
        print(f"❌ 读取失败：{e}")
        return 1

    print(f"[3/3] 读到 {len(msgs)} 条消息：")
    if not msgs:
        print("    ⚠ 空列表——OCR 没读到任何消息区文本")
        print("      可能原因：会话切换点击失败 / 消息区坐标偏 / OCR 识别失败")
    for m in msgs:
        type_name = getattr(m.type, "name", str(m.type))
        print(f"    [{m.sender}] {type_name}: {m.content!r}")

    backend.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
