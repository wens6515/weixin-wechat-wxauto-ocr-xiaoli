# -*- coding: utf-8 -*-
r"""OCR 区域圈定工具：图形框选（PySide6）+ 手填坐标微调。

背景：visual_backend 的 OCR 读取区域（聊天列表区 / 消息区）默认用硬编码
窗口比例。本工具让用户自行圈定这两个区域，配置写入
xiaoli_desktop\wx_ocr_region.json，bot 启动时加载——窗口布局变了重新圈一次即可。

用法：
  python tools/pick_ocr_region.py                      # 图形框选（PySide6）
  python tools/pick_ocr_region.py --read               # 显示当前配置
  python tools/pick_ocr_region.py --clear              # 清除配置（回退默认）
  python tools/pick_ocr_region.py --session 0 0.08 0.32 1.0    # 手填列表区比例
  python tools/pick_ocr_region.py --message 0.32 0.08 1.0 1.0  # 手填消息区比例
  python tools/pick_ocr_region.py --session 0 0.08 0.32 1.0 --message 0.32 0.08 1.0 1.0

坐标一律用相对窗口比例（0~1）：l=左, t=上, r=右, b=下（l<r, t<b）。
配置：xiaoli_desktop\wx_ocr_region.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "xiaoli_desktop", "wx_ocr_region.json",
)

DEFAULT = {
    "session_region": [0.0, 0.08, 0.32, 1.0],
    "message_region": [0.32, 0.08, 1.0, 1.0],
}


def _valid_region(vals) -> bool:
    """4 值都在 [0,1] 且 l<r、t<b。"""
    if len(vals) != 4:
        return False
    try:
        l, t, r, b = (float(v) for v in vals)
    except (TypeError, ValueError):
        return False
    if not all(0.0 <= v <= 1.0 for v in (l, t, r, b)):
        return False
    return l < r and t < b


def load_config():
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            print(f"[WARN] 读取配置失败: {e}")
    return None


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"[OK] 配置已保存: {CONFIG_PATH}")
    print(f"     列表区 session_region: {cfg['session_region']}")
    print(f"     消息区 message_region: {cfg['message_region']}")
    print("     提示：重启小漓 bot 后生效（bot 运行中改配置需重启）")


# ---------- 图形框选（PySide6） ----------


def _gui_pick() -> int:
    """图形框选主流程。返回 0 成功 / 1 取消 / 2 环境不支持。"""
    try:
        from PySide6.QtWidgets import QApplication, QLabel, QMainWindow
        from PySide6.QtGui import QPixmap, QPainter, QPen, QColor
        from PySide6.QtCore import Qt, QRect, QTimer
    except ImportError as e:
        print(f"[FAIL] PySide6 不可用（{e}），请用手填坐标方式：")
        print("       python tools/pick_ocr_region.py --session 0 0.08 0.32 1.0 "
              "--message 0.32 0.08 1.0 1.0")
        return 2

    # 截取微信窗口
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "xiaoli_desktop"))
    from wx_backend.visual_backend import find_wechat_window, capture_window
    hwnd = find_wechat_window()
    if hwnd is None:
        print("[FAIL] 未找到微信主窗口（请先登录微信并打开）")
        return 1
    img = capture_window(hwnd)
    if img is None:
        print("[FAIL] 微信窗口截图失败")
        return 1
    iw, ih = img.size
    print(f"微信窗口截图: {iw}x{ih}")

    app = QApplication(sys.argv[:1])
    win = QMainWindow()
    win.setWindowTitle("圈定 OCR 区域（拖两次：先列表区，后消息区；右键取消）")
    win.setWindowFlags(Qt.WindowStaysOnTopHint)

    class Picker(QLabel):
        def __init__(self):
            super().__init__()
            self.boxes: list[QRect] = []
            self.dragging = False
            self.cur = QRect()
            self._start = None

        def mousePressEvent(self, ev):
            if ev.button() == Qt.RightButton:
                if self.boxes:
                    self.boxes.pop()  # 撤销上一个框
                    self.update()
                return
            if ev.button() == Qt.LeftButton:
                self._start = ev.position().toPoint()
                self.dragging = True

        def mouseMoveEvent(self, ev):
            if self.dragging and self._start:
                self.cur = QRect(self._start, ev.position().toPoint()).normalized()
                self.update()

        def mouseReleaseEvent(self, ev):
            if ev.button() == Qt.LeftButton and self.dragging:
                self.dragging = False
                r = QRect(self._start, ev.position().toPoint()).normalized()
                if r.width() > 10 and r.height() > 10:
                    self.boxes.append(r)
                    n = len(self.boxes)
                    if n == 1:
                        print("已圈：列表区（第一个框）。再拖第二个框圈消息区；右键撤销。")
                    else:
                        print("已圈：消息区（第二个框）。点关闭按钮保存，或继续调整（右键撤销最后一个）。")
                self.cur = QRect()
                self.update()

        def paintEvent(self, ev):
            super().paintEvent(ev)
            p = QPainter(self)
            colors = [QColor(0, 160, 255, 120), QColor(255, 120, 0, 120)]
            for i, r in enumerate(self.boxes):
                p.fillRect(r, colors[i % len(colors)])
                p.setPen(QPen(colors[i % len(colors)].darker(), 2))
                p.drawRect(r)
            if self.dragging and not self.cur.isNull():
                p.fillRect(self.cur, QColor(0, 255, 0, 60))
                p.setPen(QPen(QColor(0, 255, 0), 2))
                p.drawRect(self.cur)

    qpix = QPixmap.fromImage(
        img.convert("RGB").tobytes("raw", "RGB"), ) if False else None  # placeholder
    # PIL → QImage
    from PySide6.QtGui import QImage
    data = img.convert("RGB")
    qimage = QImage(data.tobytes("raw", "RGB"), iw, ih, iw * 3, QImage.Format_RGB888)
    qpix = QPixmap.fromImage(qimage)

    picker = Picker()
    # 等比缩放显示，完整适配屏幕可用区（Qt 逻辑像素；DPI 缩放由 Qt 处理）。
    # 关键：pixmap 物理尺寸 = 逻辑尺寸 × DPR——若不设 devicePixelRatio，
    # Qt 会把物理像素当逻辑尺寸渲染，导致高 DPI 下窗口远超屏幕、内容只显示一部分。
    dpr = app.devicePixelRatio()
    avail = app.primaryScreen().availableGeometry()
    max_w = avail.width() - 40
    max_h = avail.height() - 60
    # 截图物理 iw×ih → 逻辑尺寸 = 物理 / DPR
    log_w, log_h = iw / dpr, ih / dpr
    scale = min(1.0, max_w / log_w, max_h / log_h)
    disp_w, disp_h = int(log_w * scale), int(log_h * scale)
    # pixmap 物理像素缩放，并标记其物理分辨率（DPR），QLabel 按逻辑尺寸 1:1 显示
    qpix = qpix.scaled(int(disp_w * dpr), int(disp_h * dpr))
    qpix.setDevicePixelRatio(dpr)
    picker.setPixmap(qpix)
    picker.setFixedSize(disp_w, disp_h)
    win.setCentralWidget(picker)
    win.resize(disp_w, disp_h + 30)
    win.show()

    # 30s 无交互自动关闭（headless/误启动兜底）
    closed = {"flag": False}

    def _timeout():
        if len(picker.boxes) < 2:
            print("[WARN] 30 秒未完成圈定，已取消（未保存）")
            closed["flag"] = True
            win.close()

    QTimer.singleShot(30000, _timeout)
    app.exec()

    if closed["flag"] or len(picker.boxes) < 2:
        print("已取消（需要圈定 2 个区域）")
        return 1

    # 换算比例：显示坐标 → 原图比例
    def to_ratio(r: QRect):
        return [round(r.x() / disp_w, 4), round(r.y() / disp_h, 4),
                round((r.x() + r.width()) / disp_w, 4),
                round((r.y() + r.height()) / disp_h, 4)]

    session = to_ratio(picker.boxes[0])
    message = to_ratio(picker.boxes[1])
    if not (_valid_region(session) and _valid_region(message)):
        print(f"[FAIL] 圈定区域非法: session={session} message={message}")
        return 1
    save_config({
        "session_region": session,
        "message_region": message,
    })
    return 0


# ---------- CLI ----------


def main():
    ap = argparse.ArgumentParser(
        description="圈定微信 OCR 读取区域（列表区/消息区），写入 wx_ocr_region.json")
    ap.add_argument("--read", action="store_true", help="显示当前配置")
    ap.add_argument("--clear", action="store_true", help="清除配置（回退默认）")
    ap.add_argument("--session", nargs=4, type=float, metavar=("l", "t", "r", "b"),
                    help="手填列表区比例（l t r b，0~1）")
    ap.add_argument("--message", nargs=4, type=float, metavar=("l", "t", "r", "b"),
                    help="手填消息区比例（l t r b，0~1）")
    args = ap.parse_args()

    if args.read:
        cfg = load_config()
        if cfg:
            print(f"已存配置: session={cfg.get('session_region')} "
                  f"message={cfg.get('message_region')}")
        else:
            print(f"（无配置，使用默认）session={DEFAULT['session_region']} "
                  f"message={DEFAULT['message_region']}")
        return
    if args.clear:
        if os.path.isfile(CONFIG_PATH):
            os.remove(CONFIG_PATH)
            print(f"[OK] 已清除配置: {CONFIG_PATH}（回退默认区域）")
        else:
            print("无配置可清除")
        return

    # 手填模式：任一区域参数给出即按手填保存（可只填一个，另一个沿用现有/默认）
    if args.session is not None or args.message is not None:
        cfg = load_config() or dict(DEFAULT)
        new_session = list(cfg.get("session_region", DEFAULT["session_region"]))
        new_message = list(cfg.get("message_region", DEFAULT["message_region"]))
        if args.session is not None:
            if not _valid_region(args.session):
                print(f"[FAIL] session 非法（须 0~1 且 l<r、t<b）: {args.session}")
                sys.exit(1)
            new_session = list(args.session)
        if args.message is not None:
            if not _valid_region(args.message):
                print(f"[FAIL] message 非法（须 0~1 且 l<r、t<b）: {args.message}")
                sys.exit(1)
            new_message = list(args.message)
        save_config({"session_region": new_session, "message_region": new_message})
        return

    # 无参数 → 图形框选
    rc = _gui_pick()
    sys.exit(rc)


if __name__ == "__main__":
    main()
