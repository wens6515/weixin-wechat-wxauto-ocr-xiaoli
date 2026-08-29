# -*- coding: utf-8 -*-
"""visual_backend：基于 PrintWindow 截图 + 本地 OCR 的微信视觉后端。

通道背景（见 docs\\微信通道验证结论.md，2026-08 实测）：
- 新版微信 4.1.12.51 的 UIA 控件树 / CDP / 窗口消息 / 本地数据通道全部关闭
- PrintWindow + PW_RENDERFULLCONTENT 截图是唯一已验证可行的读取通道
- 文本识别用 Windows 自带 OCR（winsdk，zh-Hans-CN，毫秒级，零多模态 API）
- 变化检测先行：无像素变化直接跳过，避免每轮 OCR 成本

效率设计（用户诉求"像 wxauto4 那样高效"）：
- 读取：截图（~5ms）→ 区域像素 diff（毫秒级）→ 有变化才 OCR（~50ms）
- 文本消息走本地 OCR；仅图片消息才由上层调多模态 API 描述
- 发送：定位输入框坐标 → 点击聚焦 → 键盘输入 → Enter

本实现是 wx_backend 注册表中的唯一后端（visual），auto 模式直接选中。
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import io
import json
import logging
import os
import re
import time
from typing import Any, Iterator

from PIL import Image

from . import BackendUnavailableError
from .models import MessageType, WeChatMessage

logger = logging.getLogger(__name__)

# ---------- Win32 ----------

u32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

_WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

# 64 位句柄安全（项目先例 e303f25：HWND 截断导致 Win32 调用失败）。
# 不设置 argtypes/restype 时 ctypes 默认 32 位 int，64 位句柄会溢出。
_HANDLE = ctypes.c_void_p
u32.GetWindowDC.restype = _HANDLE
u32.GetWindowDC.argtypes = [wt.HWND]
u32.ReleaseDC.restype = wt.INT
u32.ReleaseDC.argtypes = [wt.HWND, _HANDLE]
u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
gdi32.CreateCompatibleDC.restype = _HANDLE
gdi32.CreateCompatibleDC.argtypes = [_HANDLE]
gdi32.CreateCompatibleBitmap.restype = _HANDLE
gdi32.CreateCompatibleBitmap.argtypes = [_HANDLE, wt.INT, wt.INT]

# ---------- DPI 感知 ----------
# 不声明 DPI 感知时，GetWindowRect 返回被系统虚拟化的逻辑尺寸（如 743x920），
# 而真实窗口物理尺寸是 743×1.75=1300 x 920×1.75=1610——capture_window 按逻辑
# 尺寸建位图后 PrintWindow 只渲染出窗口上部约 62%（底部 352px 空白），表现为
# "图形框选/联调截图不是整个微信窗口"。声明 per-monitor aware 后坐标全部为
# 真实物理像素，截图完整、pyautogui 点击坐标也更准。
# 注：SetProcessDpiAwareness 必须在进程早期调用（任何窗口/DC 创建前），
# 失败时降级为 unaware（仅截图不完整，不影响其它逻辑）。此调用幂等。
try:
    _SHCORE = ctypes.windll.shcore
    _SHCORE.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        u32.SetProcessDPIAware()
    except Exception:
        pass
gdi32.SelectObject.restype = _HANDLE
gdi32.SelectObject.argtypes = [_HANDLE, _HANDLE]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.DeleteObject.argtypes = [_HANDLE]
gdi32.DeleteDC.restype = wt.BOOL
gdi32.DeleteDC.argtypes = [_HANDLE]
gdi32.PrintWindow = u32.PrintWindow  # 别名（PrintWindow 在 user32）
u32.PrintWindow.restype = wt.BOOL
u32.PrintWindow.argtypes = [wt.HWND, _HANDLE, wt.UINT]

# 窗口前置（pyautogui 点击/输入前必须，否则操作发到别的窗口）。
# 64 位句柄安全同 _HANDLE；keybd_event 的 dwExtraInfo 是 ULONG_PTR。
u32.SetForegroundWindow.restype = wt.BOOL
u32.SetForegroundWindow.argtypes = [wt.HWND]
u32.IsIconic.restype = wt.BOOL
u32.IsIconic.argtypes = [wt.HWND]
u32.ShowWindow.restype = wt.BOOL
u32.ShowWindow.argtypes = [wt.HWND, wt.INT]
u32.keybd_event.restype = None
u32.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_void_p]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


gdi32.GetDIBits.restype = wt.INT
gdi32.GetDIBits.argtypes = [
    _HANDLE, _HANDLE, wt.UINT, wt.UINT, ctypes.c_void_p,
    ctypes.POINTER(_BITMAPINFOHEADER), wt.UINT,
]


def find_wechat_window():
    """返回微信主窗口句柄；未找到返回 None。"""
    found = []

    @_WNDENUMPROC
    def _cb(hwnd, _lparam):
        title = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, title, 512)
        if "微信" in title.value and u32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    u32.EnumWindows(_cb, 0)
    return found[0] if found else None


def find_window_by_title(title: str):
    """按窗口标题精确匹配返回可见窗口句柄；未找到返回 None。

    微信 4.1.12 窗口类名是 Qt51514QWindowIcon（随 Qt 版本变化），uiautomation
    按 ClassName 搜索会失配、BoundingRectangle 对 Qt 窗口实测返回 (0,0,0x0)。
    图片预览窗口标题是「图片和视频」（微信 4.1.12 实测），主窗口标题「微信」。
    """
    if not title:
        return None
    found = []

    @_WNDENUMPROC
    def _cb(hwnd, _lparam):
        buf = ctypes.create_unicode_buffer(512)
        n = u32.GetWindowTextW(hwnd, buf, 512)
        if n and buf.value == title and u32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    u32.EnumWindows(_cb, 0)
    return found[0] if found else None


def window_rect(hwnd) -> tuple[int, int, int, int] | None:
    """返回窗口物理矩形 (left, top, width, height)；失败/零尺寸返回 None。

    与 capture_window 同一 DPI 感知上下文（进程级 SetProcessDpiAwareness），
    返回真实物理像素，与 pyautogui 屏幕坐标一致。
    """
    if not hwnd:
        return None
    rect = wt.RECT()
    if not u32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    return (rect.left, rect.top, w, h)


def ensure_window_visible(hwnd) -> bool:
    """窗口最小化时恢复（GetWindowRect 对最小化窗口返回任务栏占位
    (-32000, -32000, ...)，截图前必须先恢复）。返回窗口是否可用。"""
    if not hwnd:
        return False
    if u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.3)
    return True


def default_right_half_rect() -> tuple[int, int, int, int]:
    """默认窗口矩形：主屏「工作区」（SPI_GETWORKAREA，系统扣除任务栏后的
    可用区域）右半边——自动隐藏任务栏 → 工作区=全屏（窗口打满不留缝）；
    固定任务栏（底/左/右）→ 自动扣减；多显示器取主屏。物理像素（进程
    DPI 感知由 pyautogui 初始化保证）。工作区读取失败回退 SM_CXSCREEN/
    CYSCREEN 硬算。"""
    l, t, r, b = 0, 0, 0, 0
    ok = False
    try:
        wa = wt.RECT()
        # 0x0030 = SPI_GETWORKAREA（仅主屏；定位只摆主屏右半边，够用）
        ok = bool(u32.SystemParametersInfoW(0x0030, 0, ctypes.byref(wa), 0))
        if ok:
            l, t, r, b = wa.left, wa.top, wa.right, wa.bottom
    except Exception:
        ok = False
    if not ok or r <= l or b <= t:
        l, t = 0, 0
        r, b = u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)
    else:
        # 自动隐藏任务栏仍保留 ~2px「呼出条」（DPI 缩放下放大到数 px），
        # 工作区与整屏差值在这个量级时视为实际全屏直接打满（用户实测
        # 仍留一条细缝）；固定任务栏差值远超容差，正常扣减不受影响
        full_r, full_b = u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)
        if 0 < full_r - r <= 12:
            r = full_r
        if 0 < full_b - b <= 12:
            b = full_b
    x = l + (r - l) // 2
    return (x, t, r - x, max(400, b - t))


dwm = ctypes.windll.dwmapi


def visible_frame_margins(hwnd):
    """窗口不可见外沿（DWM 阴影/resize 边框，物理像素）：(左, 上, 右, 下)。

    GetWindowRect/SetWindowPos 的矩形含这些不可见外沿——把窗口矩形直接
    摆到屏幕右半边 (1280,0,1280,1600)，可见内容会两侧各缩进 ~10px（用户
    实测「右侧有缝隙」；手动拖到打满时系统保证的是可见内容贴边，窗口
    矩形反而伸出屏幕 (1270,0,1300,1610)）。DWMWA_EXTENDED_FRAME_BOUNDS(=9)
    是可见内容矩形，与 GetWindowRect 的差值即外沿。读取失败返回 None。"""
    if not hwnd:
        return None
    try:
        bounds = wt.RECT()
        if dwm.DwmGetWindowAttribute(
                hwnd, 9, ctypes.byref(bounds), ctypes.sizeof(bounds)) != 0:
            return None
        gr = wt.RECT()
        if not u32.GetWindowRect(hwnd, ctypes.byref(gr)):
            return None
        return (bounds.left - gr.left, bounds.top - gr.top,
                gr.right - bounds.right, gr.bottom - bounds.bottom)
    except Exception:
        return None


def position_window_visible(hwnd, x: int, y: int, w: int, h: int) -> bool:
    """按「可见内容」目标矩形定位：自动外扩不可见边框外沿，使可见内容
    精确落在 (x, y, w, h)——与用户手动拖窗口到打满的系统语义一致。
    外沿读取失败按零外沿处理（退化为 position_window 原行为）。"""
    m = visible_frame_margins(hwnd)
    if m is None:
        m = (0, 0, 0, 0)
    ml, mt, mr, mb = m
    return position_window(hwnd, x - ml, y - mt, w + ml + mr, h + mt + mb)


def position_window(hwnd, x: int, y: int, w: int, h: int) -> bool:
    """把窗口移动/缩放到指定矩形（物理像素）。不置顶不抢焦点（SWP_NOZORDER
    | SWP_NOACTIVATE）。失败返回 False，调用方降级为保持当前位置。"""
    if not hwnd:
        return False
    try:
        return bool(u32.SetWindowPos(
            hwnd, 0, int(x), int(y), int(w), int(h), 0x0004 | 0x0010))
    except Exception:
        return False


def capture_window(hwnd) -> Image.Image | None:
    """PrintWindow + PW_RENDERFULLCONTENT 截取窗口内容，返回 RGBA PIL Image。

    该方式可抓取 Chromium 渲染内容（含被遮挡/非前台窗口），
    比 BitBlt / pyautogui.screenshot 更稳。DPI 缩放由调用方处理。
    """
    rect = wt.RECT()
    if not u32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    hdc_win = u32.GetWindowDC(hwnd)
    if not hdc_win:
        return None
    try:
        hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
        if not hbmp:
            return None
        try:
            gdi32.SelectObject(hdc_mem, hbmp)
            ok = u32.PrintWindow(hwnd, hdc_mem, 2)  # PW_RENDERFULLCONTENT
            if not ok:
                logger.debug("PrintWindow 返回 0")
            bih = _BITMAPINFOHEADER()
            bih.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            bih.biWidth = w
            bih.biHeight = -h  # top-down
            bih.biPlanes = 1
            bih.biBitCount = 32
            bih.biCompression = 0
            buf = ctypes.create_string_buffer(w * h * 4)
            gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bih), 0)
            return Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1)
        finally:
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc_mem)
    finally:
        u32.ReleaseDC(hwnd, hdc_win)


# ---------- OCR（RapidOCR / PaddleOCR onnxruntime） ----------

_OCR_ENGINE = None


def _get_ocr_engine():
    """懒加载 RapidOCR 引擎（PaddleOCR onnxruntime 轻量版）。不可用返回 None。

    替换原 winsdk Windows OCR：实测 Windows OCR 对微信 UI 噪声致命且随内容
    漂移（王文生→干立牛、[图片]→隆片]、王美晨→艹王美晨），RapidOCR 同一张
    整窗截图全部读对（会话名置信度 1.00）。首次加载约 2s（模型初始化），
    单例缓存避免每轮重建。
    """
    global _OCR_ENGINE
    if _OCR_ENGINE is not None:
        return _OCR_ENGINE
    try:
        from rapidocr_onnxruntime import RapidOCR

        # intra_op_num_threads=2：限制 ONNX Runtime 推理线程数，避免 OCR
        # 全核打满（真机实测：小漓.exe 发现新消息时 CPU 100% 根因即此）。
        # 单次推理耗时 +5.6%（2954ms→3120ms，tools/ocr_benchmark.py 真机
        # 实测），文本匹配率 100%，CPU 占用从全核降到 2 核。
        _OCR_ENGINE = RapidOCR(intra_op_num_threads=2)
        return _OCR_ENGINE
    except Exception as e:
        logger.warning(f"RapidOCR 不可用: {e}")
        _OCR_ENGINE = False
        return None


def _norm_cjk(text: str) -> str:
    """清理 OCR 输出：中文单字间被 OCR 插入的空格去掉（'王 文 生'→'王文生'），
    保留英文/数字内部空格（'20:14' 不受影响）。"""
    if not text:
        return text
    # CJK 与 CJK 之间的空格、CJK 与相邻标点之间的空格删除
    out = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    out = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[，。！？：；、（）「」『』])", "", out)
    out = re.sub(r"(?<=[，。！？：；、（）「」『』])\s+(?=[\u4e00-\u9fff])", "", out)
    return out.strip()


def ocr_image(img: Image.Image, max_w: int = 0) -> list[dict]:
    """对图像做 RapidOCR 识别，返回 [{text, x, y, w, h}]（图像像素坐标）。

    大图先按 max_w 等比缩小（OCR 精度随缩放变化，默认不缩放）。
    返回空列表表示识别失败或无文本。
    """
    engine = _get_ocr_engine()
    if not engine:
        return []
    if max_w > 0 and img.width > max_w:
        ratio = max_w / img.width
        img = img.resize((max_w, int(img.height * ratio)), Image.LANCZOS)
    import numpy as np
    try:
        result, _elapse = engine(np.array(img.convert("RGB")))
    except Exception as e:
        logger.warning(f"OCR 识别失败: {e}")
        return []
    items = []
    if result:
        for box, text, _score in result:
            text = _norm_cjk((text or "").strip())
            if not text:
                continue
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            x, y = min(xs), min(ys)
            x2, y2 = max(xs), max(ys)
            items.append({
                "text": text,
                "x": int(x), "y": int(y),
                "w": int(x2 - x), "h": int(y2 - y),
            })
    return items




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

    逐行找连续段，跨行按「y 连续 + x 重叠」合并。过滤掉小噪声（气泡至少
    高 20px、宽 40px）。"""
    import numpy as np
    h, w = mask.shape
    boxes = []
    for y in range(h):
        xs = np.where(mask[y])[0]
        if len(xs) == 0:
            continue
        segs = []
        start = xs[0]
        prev = xs[0]
        for x in xs[1:]:
            if x - prev > x_gap:
                segs.append((start, prev))
                start = x
            prev = x
        segs.append((start, prev))
        for (l, r) in segs:
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
    # 媒体内容 ≥ 80px 宽高（真机实测对方头像 ~62px 被 60 阈值误检为媒体，
    # 提到 80 排除头像；图片/视频/表情/文件卡片均 > 80px）
    return _connected_boxes(content, min_h=80, min_w=80)


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


# ---------- 未读红圈角标检测 ----------

# 微信未读角标品牌红 ≈ #FA5151 (250,81,81)。容差放宽到 r>=200, g<=130, b<=130：
# 实测列表区红簇 avg_rgb=(248,80,80)/(249,81,81)，文字/头像红不满足 g/b 上限。
_BADGE_R_MIN = 200
_BADGE_G_MAX = 130
_BADGE_B_MAX = 130


def _detect_red_clusters(shot: Image.Image, grid: int = 20,
                         min_hits: int = 3,
                         region: tuple | None = None,
                         ) -> list[tuple[int, int, int, int]]:
    """检测会话列表区的红色角标簇，返回整窗图像坐标 [(l, t, r, b), ...]。

    实现：crop 列表区 → 用 PIL point() 生成三通道阈值掩码（C 级，~毫秒级）→
    收集红色像素 → 按 grid 网格分桶聚合（网格内命中 ≥ min_hits 视为一簇）。
    相比逐像素 Python 循环（~0.5s），掩码 + 网格分桶约 50ms，且探针实测
    与 BFS 连通域结果一致（列表区仅角标红为簇，头像/文字红零散不达标）。

    shot 为整窗截图；返回坐标以整窗图像像素为基准（列表区 crop 偏移已加回）。
    region 为列表区相对比例 (l, t, r, b)，缺省用模块默认常量（保持模块级函数可测）。
    """
    if shot is None:
        return []
    w, h = shot.size
    sr_region = _normalize_region(region) or _SESSION_REGION_RATIO
    sl = int(w * sr_region[0])
    st = int(h * sr_region[1])
    sr = int(w * sr_region[2])
    sb = int(h * sr_region[3])
    region = shot.convert("RGB").crop((sl, st, sr, sb))
    rw, rh = region.size
    if rw <= 0 or rh <= 0:
        return []
    try:
        from PIL import ImageChops
        r, g, b = region.split()
        rmask = r.point(lambda v: 255 if v >= _BADGE_R_MIN else 0)
        gmask = g.point(lambda v: 255 if v <= _BADGE_G_MAX else 0)
        bmask = b.point(lambda v: 255 if v <= _BADGE_B_MAX else 0)
        mask = ImageChops.multiply(ImageChops.multiply(rmask, gmask), bmask)
    except Exception:
        return []
    data = mask.tobytes()  # 每像素 1 字节（L 模式）
    # 收集红色像素坐标（20 万像素循环 ~30ms，仅列表区）
    reds = [(x, y) for y in range(rh) for x in range(rw)
            if data[y * rw + x]]
    if not reds:
        return []
    # 网格分桶聚合（角标 ~20x20px，网格 20px 内合并为一簇）
    buckets: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for x, y in reds:
        buckets.setdefault((x // grid, y // grid), []).append((x, y))
    clusters = []
    for (gx, gy), pts in buckets.items():
        if len(pts) < min_hits:
            continue
        minx = min(p[0] for p in pts)
        maxx = max(p[0] for p in pts)
        miny = min(p[1] for p in pts)
        maxy = max(p[1] for p in pts)
        # 相邻网格合并（角标跨网格边界时）
        clusters.append((sl + minx, st + miny, sl + maxx, st + maxy))
    # 合并相邻/重叠簇
    merged: list[tuple[int, int, int, int]] = []
    for c in sorted(clusters):
        if merged and _clusters_overlap(merged[-1], c):
            p = merged.pop()
            merged.append((min(p[0], c[0]), min(p[1], c[1]),
                           max(p[2], c[2]), max(p[3], c[3])))
        else:
            merged.append(c)
    return merged


def _clusters_overlap(a: tuple, b: tuple, gap: int = 15) -> bool:
    """两簇包围盒是否相邻（间隔 ≤ gap 视为同一角标）。"""
    return not (a[2] + gap < b[0] or b[2] + gap < a[0]
                or a[3] + gap < b[1] or b[3] + gap < a[1])


# 自学习选中高亮：微信列表选中条目有浅灰高亮背景（浅色主题 ≈ #F0F0F0，
# 未选中纯白 #FFFFFF，差值 15~25）。PrintWindow 像素稳定（噪声 <3），
# 容差取 12 夹在两者之间——既能容忍截图噪声，又不会把纯白误判成高亮。
_ROW_COLOR_TOL = 12
# 红圈锚定点击偏移：红圈在头像左上角，条目主体在头像右侧 ~45px
# （与 _anchor_badge 同一真机标定）。
_BADGE_CLICK_OFFSET_X = 45


def _color_close(a: tuple, b: tuple, tol: int = _ROW_COLOR_TOL) -> bool:
    """两个 RGB 颜色是否在每通道容差内相等（自学习选中高亮判定）。"""
    return all(abs(x - y) <= tol for x, y in zip(a, b))


# ---------- 后端实现 ----------

# 微信窗口布局（4.1.12.51 默认窗口，相对窗口客户区比例）
# 会话列表：左侧约 42% 宽；消息区：右侧约 58% 宽
# 注意：窗口位置/大小由用户用 tools/fix_window.py 固定，bot 不移动窗口、
# 不读窗口配置——坐标换算一律基于窗口当前实际 rect（见 _window_rect）。
#
# 默认值为真机框选标定（tools/pick_ocr_region.py，微信窗口 1300x1610 实测）：
# 消息区底部 0.8384 = 聊天记录下缘，刻意不含输入框（输入框"发送"按钮在
# 窗口 ~0.95 处——若默认区域含输入框，OCR 会把按钮文字当消息且判 self）。
# 打包 exe（PyInstaller）无 wx_ocr_region.json 时即用此默认；用户窗口尺寸
# 不同会导致错位，普通用户分发需引导框选或自适应检测。
_SESSION_REGION_RATIO = (0.09, 0.0878, 0.418, 0.9895)   # (l, t, r, b) 相对窗口
_MESSAGE_REGION_RATIO = (0.4165, 0.1288, 0.9913, 0.8337)
# 同一气泡内换行 vs 气泡间距的 y 差阈值（2x 坐标）。真机标定：气泡内换行
# 行距 ~40-50px，群聊名字-内容 106px、内容块间最小 186px——取 70 夹中间，
# 让换行合并、气泡分开。旧值 18 小于换行距，多行长消息被逐行拆散。
_BUBBLE_LINE_GAP = 70
# 右侧会话标题区（真机标定）：当前会话名权威来源 + 群聊判定（标题带括号人数）
_TITLE_REGION_RATIO = (0.4151, 0.0386, 0.8128, 0.082)

# 用户圈定配置：xiaoli_desktop\wx_ocr_region.json（tools/pick_ocr_region.py 生成）。
# 无配置/坏配置 → 回退模块默认常量（fail-closed），bot 不因配置问题中断。
_REGION_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "xiaoli_desktop", "wx_ocr_region.json",
)


def _region_dict_to_tuple(region) -> tuple | None:
    """配置 dict {l,t,r,b} → 有序元组；非 dict/缺键返回 None。"""
    if not isinstance(region, dict):
        return None
    try:
        return (region["l"], region["t"], region["r"], region["b"])
    except KeyError:
        return None


def _normalize_region(region: tuple) -> tuple | None:
    """校验并规范化区域元组：4 值都在 [0,1] 且 l<r、t<b，非法返回 None。"""
    if not isinstance(region, (tuple, list)) or len(region) != 4:
        return None
    try:
        vals = tuple(float(v) for v in region)
    except (TypeError, ValueError):
        return None
    l, t, r, b = vals
    if not all(0.0 <= v <= 1.0 for v in vals):
        return None
    if l >= r or t >= b:
        return None
    return vals


def _load_region_config() -> dict | None:
    """读用户圈定区域配置；无文件/坏值返回 None（调用方回退默认常量）。"""
    try:
        if not os.path.isfile(_REGION_CONFIG_PATH):
            return None
        with open(_REGION_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("wx_ocr_region.json 非对象，回退默认区域")
            return None
        session = _normalize_region(
            _region_dict_to_tuple(data.get("session_region"))
            if isinstance(data.get("session_region"), dict)
            else data.get("session_region"))
        message = _normalize_region(
            _region_dict_to_tuple(data.get("message_region"))
            if isinstance(data.get("message_region"), dict)
            else data.get("message_region"))
        title = _normalize_region(
            _region_dict_to_tuple(data.get("title_region"))
            if isinstance(data.get("title_region"), dict)
            else data.get("title_region"))
        if session is None or message is None:
            logger.warning(
                "wx_ocr_region.json 区域值非法（须 l<t? 实为 4 值 [0,1] 且 l<r、t<b），回退默认")
            return None
        logger.info(
            f"📐 已加载用户圈定区域：列表{session} 消息{message}"
            + (f" 标题{title}" if title else ""))
        return {"session": session, "message": message, "title": title}
    except Exception as e:
        logger.warning(f"wx_ocr_region.json 读取失败（{e}），回退默认区域")
        return None


_TITLE_GROUP_RE = re.compile(r"^(?P<name>.+?)\((?P<count>\d+)\)\s*$")


def parse_title(title: str) -> tuple[str, bool, int | None]:
    """解析会话标题 → (会话名, 是否群聊, 群人数)。

    群聊标题形如 '强盗"集团(5)'（括号内人数，真机标定）；私聊标题即会话名
    无括号。群聊判定依据标题而非会话名启发式——普通群名（如'哆菈A夢'）
    不含'群/集团'字，名称启发式会漏判。
    """
    if not title:
        return "", False, None
    m = _TITLE_GROUP_RE.match(title.strip())
    if m:
        return m.group("name"), True, int(m.group("count"))
    return title.strip(), False, None


class VisualBackend:
    """PrintWindow + 本地 OCR 的微信视觉后端。

    实现 wx_backend.WeChatBackend 协议。所有读操作：截图 → 区域 diff →
    有变化才 OCR。发送：坐标点击聚焦 + 键盘输入 + Enter。
    """

    name = "visual"

    def __init__(self, poll_region: bool = True, **kwargs: Any):
        self._hwnd = None
        self._last_shot: Image.Image | None = None
        self._poll_region = poll_region
        self._closed = False
        self._session_coords: dict[str, tuple[int, int]] = {}  # 会话名 → 屏幕中心点
        self._current_chat: str | None = None  # 当前选中的会话（微信 toggle 行为：已选中再点会取消）
        self._current_title: str | None = None  # 当前会话标题（read_title 权威名称）
        self._current_is_group: bool = False  # 当前会话是否群聊（标题含括号人数）
        # 选中行高亮色：真机采样固定值（深色主题 WeChat 4.x，RGB(13,168,105)，
        # 与消息气泡绿 (53,210,141) 是两个不同色值）。命中该色 = 已选中，
        # 调用方不点击防 toggle 取消选中——像素判定零 OCR，事件热路径第一道闸。
        # 微信切浅色主题时该值会变：点击成功后 _learn_selected_row_color 会
        # 重新采样覆盖；也可直接改这里的默认值。
        self._selected_row_color: tuple[int, int, int] = (13, 168, 105)
        self._badge_coords: dict[str, tuple[int, int]] = {}  # 会话名 → 红圈中心屏幕坐标（点击直用）
        # 用户圈定区域（tools/pick_ocr_region.py 配置）；无配置回退模块默认常量
        cfg = _load_region_config()
        self._session_region = cfg["session"] if cfg else _SESSION_REGION_RATIO
        self._message_region = cfg["message"] if cfg else _MESSAGE_REGION_RATIO
        self._title_region = (cfg.get("title") if cfg else None) or _TITLE_REGION_RATIO

    # ---- 协议：连接 ----

    def connect(self) -> bool:
        hwnd = find_wechat_window()
        if hwnd is None:
            raise BackendUnavailableError("未找到微信主窗口（请确认微信已登录并打开）")
        self._hwnd = hwnd
        # 窗口定位在 bot 初始化时一次性完成（WeChatBot._init_position_wechat，
        # 默认右半屏 + 告知用户勿动——用户定案，替代旧的「不移动窗口」约定）。
        # 后端自身运行期不移动窗口，坐标换算一律读窗口当前实际 rect，
        # 用户连接后手动调整仍以实时 rect 为准。
        self._ensure_not_iconic()
        shot = capture_window(hwnd)
        if shot is None:
            raise BackendUnavailableError("PrintWindow 截图失败")
        self._last_shot = shot
        logger.info(f"✅ 视觉后端已连接（窗口 0x{hwnd:x} {shot.size[0]}x{shot.size[1]}）")
        return True

    def _foreground(self) -> bool:
        """把微信窗口强制置前。截图（PrintWindow）与点击/键盘输入都依赖
        微信在前台——实测微信被其他窗口（如天枢界面）完全遮挡时，
        PrintWindow 返回黑图，OCR 读不到消息。因此所有截图/操作前先置前。

        已在前台时快速返回（不 Alt 空击、不 sleep），避免每轮轮询都打断用户。
        Windows 前台锁：非前台进程调用 SetForegroundWindow 会被静默拒绝，
        先 Alt 空击解除锁定；窗口最小化先 SW_RESTORE 恢复。"""
        if self._hwnd is None:
            return False
        try:
            if u32.GetForegroundWindow() == self._hwnd:
                return True  # 已在前台，无需操作
            if u32.IsIconic(self._hwnd):
                u32.ShowWindow(self._hwnd, 9)  # SW_RESTORE
            try:
                # VK_MENU(Alt) 空击解除前台锁
                u32.keybd_event(0x12, 0, 0, 0)
                u32.keybd_event(0x12, 0, 2, 0)  # KEYEVENTF_KEYUP
            except Exception:
                pass
            u32.SetForegroundWindow(self._hwnd)
            time.sleep(0.2)
            return True
        except Exception as e:
            logger.warning(f"[前置] 微信窗口置前失败: {e}")
            return False

    # ---- 协议：会话 ----

    def _ensure_not_iconic(self) -> bool:
        """最小化哨兵：微信最小化后 GetWindowRect 返回任务栏占位矩形，
        PrintWindow 只能产出 ~276x45 的垃圾小图，红圈检测恒 0——静默漏
        消息（真机实测：不报错、不返回 None，就是看不见）。发现即
        SW_RESTORE 恢复并告警。返回是否刚执行了恢复。"""
        if self._hwnd is None:
            return False
        try:
            if not u32.IsIconic(self._hwnd):
                return False
        except Exception:
            return False
        u32.ShowWindow(self._hwnd, 9)  # SW_RESTORE
        time.sleep(0.3)
        logger.warning("[哨兵] 微信窗口被最小化，已自动恢复——最小化期间无法监听消息，请勿最小化微信")
        return True

    def _refresh(self, force: bool = False,
                 foreground: bool = True) -> Image.Image | None:
        """截图并做区域变化检测；无变化且非 force 时返回 None。

        foreground=True（默认）：截图前先 _foreground 置前微信——微信被
        完全遮挡时 PrintWindow 返回黑图，OCR 读不到消息。
        foreground=False：后台静默截图（红圈轮询用）——每轮轮询都置前
        会反复打断用户；用户实测后台像素检测红圈可靠，不置前也能截图。
        点击/读取/发送前的截图仍置前（那是检测到新消息后的动作）。
        """
        if self._hwnd is None:
            raise BackendUnavailableError("后端未连接")
        if foreground:
            self._foreground()  # 被遮挡时 PrintWindow 返回黑图，先置前
        shot = capture_window(self._hwnd)
        if shot is None:
            return None
        # 尺寸骤缩哨兵：最小化/占位残帧（真机 276x45）远小于正常窗口面积，
        # 丢弃本帧并触发恢复（第二道防线，IsIconic 哨兵之外兜底）
        last = self._last_shot
        if last is not None:
            lw, lh = last.size
            if lw * lh > 0 and shot.size[0] * shot.size[1] < (lw * lh) * 0.15:
                logger.warning("[哨兵] 截图尺寸骤缩（疑似最小化占位帧），丢弃本帧")
                self._ensure_not_iconic()
                return None
        if not force and self._last_shot is not None:
            w, h = shot.size
            region = (
                int(w * self._session_region[0]), int(h * self._session_region[1]),
                int(w * self._session_region[2]), int(h * self._session_region[3]),
            )
            if not region_changed(self._last_shot, shot, region):
                return None
        self._last_shot = shot
        return shot

    def read_title(self, foreground: bool = False) -> str | None:
        """读右侧消息区顶部的当前会话标题（OCR）。

        标题是当前会话的权威名称：私聊即会话名，群聊形如
        '强盗"集团(5)'（括号内人数）。返回标题原文；区域为空/失败返回 None。
        调用方用 parse_title 解析会话名与群聊标记。

        foreground：默认 False 后台静默截图（只读轮询用）；微信被其他窗口
        遮挡时 PrintWindow 黑图导致 OCR 空——处理新消息的路径（get_messages）
        传 True 置前截图保证可靠。
        """
        shot = self._refresh(force=True, foreground=foreground)
        if shot is None:
            return None
        w, h = shot.size
        region = shot.crop((
            int(w * self._title_region[0]), int(h * self._title_region[1]),
            int(w * self._title_region[2]), int(h * self._title_region[3]),
        ))
        region = region.resize((region.width * 2, region.height * 2), Image.LANCZOS)
        items = ocr_image(region)
        # 标题可能被 OCR 拆成多段（真机：'"强盗"' + '集团(5)' 两个独立项），
        # 按 x 排序拼接，而非只取最长单行——否则群聊标题缺左半
        texts = []
        for it in sorted(items, key=lambda i: i["x"]):
            t = (it["text"] or "").strip()
            if len(t) < 2 or not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", t):
                continue  # 排除单字符按钮噪声（如 'X'）与纯符号
            texts.append(t)
        return "".join(texts) or None

    def iter_sessions(self) -> Iterator[str]:
        """整窗 OCR + 按 y 聚类识别会话项。

        背景（实测）：Windows OCR 对整窗识别最准（会话列表区域放大 2 倍会把
        '王文生' 误读成 '干立牛'）。整窗 OCR 中每个会话项可能拆成多行——
        主名（'王文生'）+ 标签行（'[ 视频 ]'）+ 消息预览（'群聊-王文生：…'）。
        按 y 聚类（同项行 y 差 < 36px，会话项间距 ~110px）合并，取聚类内
        第一个非标签文本为会话名，坐标为该行中心。

        会话列表列判定：x < 窗口宽*0.49（实测时间戳列 x≈432 在 58% 处，
        会话名 x≤365≈49%）。过滤 '搜索' 框与时间戳/日期行。
        """
        shot = self._refresh(force=True)
        if shot is None:
            return
        self._extract_session_names(shot)
        for name in self._session_coords:
            yield name

    def _extract_session_names(self, shot: Image.Image) -> dict[str, tuple[int, int]]:
        """整窗 OCR → 会话名 → 屏幕中心坐标（写 self._session_coords 并返回）。

        返回 {会话名: (屏幕x, 屏幕y)}；复用 iter_sessions 的聚类与清理逻辑，
        供 iter_sessions / iter_unread_sessions 共享。
        """
        w, h = shot.size
        items = ocr_image(shot)
        self._session_coords.clear()
        rect = wt.RECT()
        u32.GetWindowRect(self._hwnd, ctypes.byref(rect))
        win_l, win_t = rect.left, rect.top
        session_x_max = w * self._session_region[2]  # 列表区右边界（不含容差——+0.17 会把置顶会话的顶部标题 x≈0.42w 误纳为会话条目）

        # 1. 预筛选：会话列表列 + 过滤搜索框/时间戳
        cand = []
        for it in items:
            name = (it["text"] or "").strip()
            if not name or it["x"] >= session_x_max:
                continue
            if re.match(r"^[\d:：\s昨天前天上午下午晚上]+$", name):
                continue
            if "搜索" in name and len(name) <= 4:
                continue
            if len(name) < 2:
                continue
            cand.append(it)

        # 2. 按 y 聚类（标签行如 '[ 视频 ]' 与所属主名 y 差 ~38px，会话项间距 ~114px。
        #    阈值 45：并入标签/预览行，分隔不同会话项。）
        cand.sort(key=lambda it: it["y"])
        clusters: list[list[dict]] = []
        for it in cand:
            if clusters and it["y"] - clusters[-1][-1]["y"] < 45:
                clusters[-1].append(it)
            else:
                clusters.append([it])

        # 3. 每聚类取主名（标签/预览行除外 + OCR 残留清理，与条带路径共用）
        for cluster in clusters:
            picked = self._pick_main_name(cluster)
            if not picked:
                # 无主名（全是标签/预览）→ 跳过
                continue
            name, main_line = picked
            cx = win_l + main_line["x"] + main_line["w"] // 2
            cy = win_t + main_line["y"] + main_line["h"] // 2
            self._session_coords[name] = (cx, cy)
        return self._session_coords

    @staticmethod
    def _pick_main_name(lines: list[dict]) -> tuple[str, dict] | None:
        """从一组 OCR 行里挑会话主名，返回 (名字, 行对象)。

        跳过纯标签行/预览行/时间戳行，清理 OCR 残留（首尾非内容字符、
        引号内空格）。条带与整表两条提名路径共用同一套规则，保证名字
        口径一致；行对象供整表路径计算屏幕坐标。"""
        for it in sorted(lines, key=lambda i: i["y"]):
            t = (it.get("text") or "").strip()
            if not t or len(t) < 2:
                continue
            if re.match(r"^[\d:：\s昨天前天上午下午晚上]+$", t):
                continue
            if "搜索" in t and len(t) <= 4:
                continue
            if re.match(r"^[\s\[\]【】「」]*$", t):
                continue
            if "：" in t or ":" in t:
                continue
            if re.match(r"^[\s\[\]]{0,2}[\u4e00-\u9fff]{1,4}[\s\[\]]{0,2}$", t) \
                    and len(t) <= 6 and ("[" in t or "]" in t):
                continue
            name = re.sub(r"^[^\u4e00-\u9fffA-Za-z0-9]+", "", t)
            name = re.sub(r"\"\s+", "\"", name)
            name = re.sub(r"\s+\"", "\"", name)
            name = name.strip()
            if name:
                return name, it
        return None

    def _name_at_badge(self, bcx: int, bcy: int, shot: Image.Image) -> str | None:
        """事件热路径 OCR ①：红圈所在行的会话名（只裁该行条带）。

        整表 OCR ~1.2s → 单行条带 ~0.3s。条带 = 会话列表区横向全宽 ×
        红圈 y ±58px（行高 ~110px，±58 覆盖主名+预览两行）。名字取条带内
        最上方的非标签/非预览行（与整表聚类同规则）。"""
        w, h = shot.size
        rect = wt.RECT()
        u32.GetWindowRect(self._hwnd, ctypes.byref(rect))
        py = bcy - rect.top
        l = int(w * self._session_region[0])
        r = int(w * self._session_region[2])
        t0, t1 = max(0, py - 58), min(h, py + 58)
        if r <= l or t1 <= t0:
            return None
        strip = shot.crop((l, t0, r, t1))
        items = ocr_image(strip)
        if not items:
            return None
        picked = self._pick_main_name(items)
        return picked[0] if picked else None

    def iter_unread_sessions(self) -> Iterator[str]:
        """仅迭代有未读红圈角标的会话（可选能力，wxauto 等后端可不提供）。

        效率路径（事件热路径 OCR ①）：
        1. 最小化哨兵 + 截图 force（红圈像素检测毫秒级，无 diff 保护）
        2. 无红簇 → 直接结束（零 OCR 零点击）
        3. 有红簇 → 每个红圈只裁所在行条带 OCR 读会话名（~0.3s/个）
        4. 条带漏读的红圈 → 整表 OCR + 几何匹配旧路径兜底；仍失败 →
           红圈锚定 fallback（点击红圈右下条目，读顶部标题）
        """
        if self._ensure_not_iconic():
            return  # 最小化态截图是占位垃圾（真机 276x45），恢复后下一轮再扫
        shot = self._refresh(force=True, foreground=False)  # 红圈轮询：后台静默截图，不置前打断用户
        if shot is None:
            return
        badges = _detect_red_clusters(shot, region=self._session_region)
        if not badges:
            return
        rect = wt.RECT()
        u32.GetWindowRect(self._hwnd, ctypes.byref(rect))
        win_l, win_t = rect.left, rect.top
        seen: set[str] = set()
        self._badge_coords.clear()  # 每轮重建（红圈坐标通道：供 _switch_chat 点击直用）
        strip_failed = []
        for (bl, bt, br, bb) in badges:
            bcx = win_l + (bl + br) // 2
            bcy = win_t + (bt + bb) // 2
            name = self._name_at_badge(bcx, bcy, shot)
            if name:
                if name in seen:
                    logger.debug(f"[未读] {name!r} 已有红圈（重复角标，跳过）")
                    continue
                seen.add(name)
                self._badge_coords[name] = (bcx, bcy)
                # 条带锚定的点击坐标：红圈右下条目主体（与 _anchor_badge 同偏移）
                self._session_coords[name] = (bcx + _BADGE_CLICK_OFFSET_X, bcy)
                logger.debug(f"[未读] {name!r} 红圈屏幕 ({bcx},{bcy})")
                yield name
            else:
                strip_failed.append((bcx, bcy))
        if not strip_failed:
            return
        # 整表 OCR 兜底（旧路径：几何匹配红圈 ↔ 会话名）
        coords = self._extract_session_names(shot)
        for (bcx, bcy) in strip_failed:
            best = None
            best_d = 1e9
            for name, (sx, sy) in coords.items():
                if abs(bcy - sy) >= 60:
                    continue
                if bcx >= sx:  # 红簇必须在名字左侧（左上角角标）
                    continue
                d = abs(bcy - sy) + (sx - bcx)
                if d < best_d:
                    best_d = d
                    best = name
            if best is not None and best not in seen:
                seen.add(best)
                self._badge_coords[best] = (bcx, bcy)
                logger.debug(f"[未读] {best!r} 红圈屏幕 ({bcx},{bcy})（整表兜底）")
                yield best
            elif best is None:
                # 红圈锚定 fallback：会话名 OCR 漏读时，点击红圈右下条目，
                # 读顶部标题拿会话名（根治"王文生漏消息"——红圈可靠，别被 OCR 拖垮）
                anchored = self._anchor_badge(bcx, bcy)
                if anchored and anchored not in seen:
                    seen.add(anchored)
                    self._badge_coords[anchored] = (bcx, bcy)
                    yield anchored

    def _anchor_badge(self, bcx: int, bcy: int) -> str | None:
        """红圈锚定 fallback：点击红圈右下（联系人条目），读顶部标题拿会话名。

        红圈在头像左上角，头像约 40px，条目主体在头像右侧约 45px 处。
        点击后当前会话切换，顶部标题区（x 居中、y 偏上）即会话名。
        """
        try:
            import pyautogui
            self._foreground()  # 点击依赖前台，先置前微信窗口
            pyautogui.click(bcx + 45, bcy)
            time.sleep(0.6)
        except Exception as e:
            logger.warning(f"[锚定] 点击失败: {e}")
            return None
        shot = self._refresh(force=True)
        if shot is None:
            return None
        w, h = shot.size
        items = ocr_image(shot)
        for it in items:
            t = (it["text"] or "").strip()
            if 0.35 * w <= it["x"] <= 0.65 * w and it["y"] < 0.12 * h:
                if len(t) >= 2 and not re.match(r"^[\d:：\s]+$", t):
                    self._current_chat = t  # 锚定点击已切换会话，同步状态
                    logger.info(f"[锚定] 红圈 ({bcx},{bcy}) → 会话 {t!r}")
                    return t
        logger.warning(f"[锚定] 红圈 ({bcx},{bcy}) 点击后未读到顶部标题")
        return None

    def _switch_chat(self, chat: str, force: bool = False) -> bool:
        """点击会话列表中的目标会话切换聊天。返回是否已切换。

        微信会话列表是 toggle 行为：点已选中的会话会取消选中、右侧消息区
        变空。因此已选中目标会话时直接返回，不重复点击（用户实测：再点一次
        就取消选中了）。force=True 强制点击，绕过已选中判断——用于
        get_messages 读到 0 条时的 toggle 兜底重试。

        已选中判定优先级（force=False）：
        1. 选中高亮（自学习，_is_row_selected）——最强信号：微信 UI 直接
           用背景高亮标出选中条目，不受 OCR 全半角/emoji 差异影响。
        2. UI 标题区 OCR（read_title）——_current_chat 是进程内状态，与
           微信 UI 实际选中可能失同步（bot 重启清空/用户手动切换）。
        3. 内存 _current_chat——前两者失败时的兜底。

        坐标来源优先级：OCR 会话名坐标 → _badge_coords 红圈坐标直点
        （OCR 名漏读时点红圈右下条目主体，复用 _anchor_badge 标定）。
        点击成功后若标题非空（微信确认选中），采样该条目行背景色
        自学习缓存选中高亮（_learn_selected_row_color）。
        """
        if not force:
            # 1) 选中高亮检测（自学习；后台静默截图，不置前打断用户）
            try:
                item_y = None
                if chat in self._session_coords:
                    item_y = self._session_coords[chat][1]
                elif chat in self._badge_coords:
                    item_y = self._badge_coords[chat][1]
                if item_y is not None and self._is_row_selected(item_y):
                    self._current_chat = chat
                    return True
            except Exception:
                pass
            # 2) UI 检测：标题区显示目标会话 = 已选中（后台静默截图，不置前）
            try:
                title = self.read_title(foreground=False)
                if title and parse_title(title)[0] == chat:
                    self._current_chat = chat
                    return True
            except Exception:
                pass
            if self._current_chat == chat:
                return True  # 已选中（UI 检测失败时退回到内存状态）
        if chat not in self._session_coords:
            # 坐标未知：先刷新会话列表
            list(self.iter_sessions())
        coord = self._session_coords.get(chat)
        if coord is None:
            # 红圈坐标通道：OCR 名漏读/坐标缺失时，点红圈右下条目主体
            badge = self._badge_coords.get(chat)
            if badge is not None:
                coord = (badge[0] + _BADGE_CLICK_OFFSET_X, badge[1])
            else:
                logger.warning(f"[切换] 未找到会话 {chat!r} 的坐标")
                return False
        try:
            import pyautogui
            self._foreground()  # 点击依赖前台，先置前微信窗口
            logger.info(f"[切换] 点击会话 {chat!r} @({coord[0]},{coord[1]}) force={force} _current_chat={self._current_chat!r}")
            pyautogui.click(coord[0], coord[1])
            self._current_chat = chat
            time.sleep(0.5)  # 等待消息区刷新
            # 自学习选中高亮：点击成功且标题非空（微信确认已选中）→ 采样该
            # 条目行背景色。标题解析名与 chat 宽容匹配才采样——点击落空切到
            # 别处时（标题非空但非目标会话）不采样，避免把未选中行缓存成
            # 选中色导致后续误判。
            try:
                title = self.read_title(foreground=False)
                parsed = parse_title(title)[0] if title else ""
                if title and (parsed == chat or parsed.startswith(chat)
                              or chat.startswith(parsed)):
                    self._learn_selected_row_color(coord[1])
            except Exception:
                pass
            return True
        except Exception as e:
            logger.warning(f"[切换] 点击会话失败: {e}")
            return False

    def _is_row_selected(self, red_badge_y_or_item_y: int) -> bool:
        """判断条目行背景是否命中缓存选中高亮色（阈值容差）。

        输入为条目行 y（屏幕坐标，红圈 y 或 OCR 名中心 y 均可——两者都落在
        该条目行内）。命中 = 微信 UI 已选中该会话，调用方不点击避免 toggle
        取消选中。缓存缺失（_selected_row_color None）→ False，回退旧判定。
        后台静默截图（不置前）；状态检测必须 force 截图——region_changed
        相对上一帧 diff 不能用于当前状态检测（红圈已存在时两帧相同会漏读）。
        """
        if self._selected_row_color is None or self._hwnd is None:
            return False
        shot = self._refresh(force=True, foreground=False)
        if shot is None:
            return False
        w, h = shot.size
        rect = wt.RECT()
        u32.GetWindowRect(self._hwnd, ctypes.byref(rect))
        py = red_badge_y_or_item_y - rect.top
        px = int(w * self._session_region[0]) + 5  # 列表区左缘内侧空白带（避开头像/文字）
        if not (0 <= py < h and 0 <= px < w):
            return False
        try:
            rgb = shot.convert("RGB").getpixel((px, py))
        except Exception:
            return False
        return _color_close(rgb, self._selected_row_color)

    def _learn_selected_row_color(self, screen_y: int) -> None:
        """点击成功且标题非空后，采样该会话条目行背景色缓存为选中高亮色。

        进程内缓存即可（无需持久化）：微信主题变化后，下一次点击成功会
        重新采样覆盖旧值。采样点取列表区左缘内侧空白带（避开头像/文字）。
        """
        if self._hwnd is None:
            return
        shot = self._refresh(force=True, foreground=False)
        if shot is None:
            return
        w, h = shot.size
        rect = wt.RECT()
        u32.GetWindowRect(self._hwnd, ctypes.byref(rect))
        py = screen_y - rect.top
        px = int(w * self._session_region[0]) + 5
        if not (0 <= py < h and 0 <= px < w):
            return
        try:
            rgb = shot.convert("RGB").getpixel((px, py))
        except Exception:
            return
        self._selected_row_color = rgb
        logger.info(f"[高亮] 采样选中行背景色 {rgb}（y={screen_y}）")

    def get_messages(self, chat: str, limit: int | None = None,
                     assume_switched: bool = False) -> list[WeChatMessage]:
        """返回会话 chat 的消息（最近 limit 条）。消息区 OCR + 时间戳行切分。

        消息块判定：时间戳行（如 '昨天 18:45'、'20:14'）作为块分隔符；块内
        多行合并为一条消息。sender 按 x 坐标与消息区中线比较——右侧=自己
        （sender="self"，上层跳过），左侧=对方（私聊时 sender=会话名，
        即发送人；群聊发送者名读取待真机样本增强）。

        assume_switched：同一处理事件里 analyze_window 刚完成「切换 + 读
        标题」时置 True——跳过重切与标题重读（省一次点击、两次 OCR，是
        事件热路径提速的核心）。标题/群聊标记直接用缓存；消息区读空时
        仍会走 force 重切兜底（toggle 取消选中防线保留）。
        """
        if assume_switched and self._current_chat == chat:
            logger.info(f"[读取] {chat!r} 标题={self._current_title!r}（复用切换结果）")
        else:
            # 先切换到目标会话（visual 通道必须点击切换，无法像 wxauto4 ChatWith 直达）
            self._switch_chat(chat)
            # 读当前会话标题：会话名权威来源 + 群聊判定（标题带括号人数）。
            # 处理新消息的动作路径，置前截图保证微信不被遮挡时也能读到标题。
            title = self.read_title(foreground=True)
            if not title:
                # 标题区空（toggle 取消选中或截图失败）→ force 重切恢复后再读
                # 一次。名字比较退出切换判定——群名全半角/符号/emoji 的 OCR
                # 差异会让 startswith 误失败，白点 force 点击已选中会话导致
                # toggle 取消选中（真机日志：群聊名字后多带（数字）反复点击）。
                logger.warning(f"[读取] {chat!r} 标题={title!r}（空），force 重切")
                self._switch_chat(chat, force=True)
                title = self.read_title(foreground=True)
            logger.info(f"[读取] {chat!r} 标题={title!r}")
            if title:
                name, is_group, _ = parse_title(title)
                self._current_title = name or chat
                self._current_is_group = is_group
        region = None
        items = []
        bubble_boxes: list[tuple] = []  # 2x 坐标气泡框 [(t,b,l,r,is_self)]
        for attempt in range(2):
            shot = self._refresh(force=True)
            if shot is None:
                logger.warning(f"[读取] {chat!r} 截图失败（capture_window 返回 None，句柄失效/窗口关闭？）attempt={attempt}")
                return []
            w, h = shot.size
            region_1x = shot.crop((
                int(w * self._message_region[0]), int(h * self._message_region[1]),
                int(w * self._message_region[2]), int(h * self._message_region[3]),
            ))
            region = region_1x.resize((region_1x.width * 2, region_1x.height * 2), Image.LANCZOS)
            if assume_switched and self._current_chat == chat:
                # ---- 联合裁剪 OCR（用户定案：事件内 OCR 两次封顶）----
                # 标题带 + 消息区一次读：联合区 = 标题区 ∪ 消息区，下边框
                # 钉在标定消息区下沿（输入框永不入镜）。标题行 = 联合区内
                # 消息区上沿以上（2x 坐标）；消息行平移回消息区 2x 坐标系，
                # 下游气泡/头像/合并逻辑与旧路径完全共用。
                u_l = int(w * min(self._title_region[0], self._message_region[0]))
                u_t = int(h * min(self._title_region[1], self._message_region[1]))
                u_r = int(w * max(self._title_region[2], self._message_region[2]))
                u_b = int(h * max(self._title_region[3], self._message_region[3]))
                union = shot.crop((u_l, u_t, u_r, u_b))
                union2x = union.resize((union.width * 2, union.height * 2), Image.LANCZOS)
                all_items = ocr_image(union2x)
                # 标题带钉在标定标题区内（center-y < 标题区下沿）——标题区
                # 下沿与消息区上沿之间夹缝的内容（真机实测：群聊首行
                # 「何镇鸿:[图片]」会混进标题串）不得污染标题，全部归消息带
                t_bot_2x = (int(h * self._title_region[3]) - u_t) * 2
                title_items = [it for it in all_items
                               if (it["y"] + it["h"] // 2) < t_bot_2x]
                title = "".join(it["text"] for it in
                                sorted(title_items, key=lambda i: i["x"])).strip()
                # 空标题防线（自 analyze_window 迁入）：标题区读空多为
                # toggle 取消选中/黑图——force 重切，attempt 循环兜底重试
                if not title:
                    logger.warning(f"[读取] {chat!r} 标题区空（联合 OCR），force 重切")
                    self._switch_chat(chat, force=True)
                else:
                    logger.info(f"[读取] {chat!r} 标题={title!r}（联合 OCR）")
                    name, is_group, _ = parse_title(title)
                    self._current_title = name or chat
                    self._current_is_group = is_group
                # 消息行平移回消息区 2x 坐标系（几何检测沿用消息区子图）；
                # 标题区下沿与消息区上沿夹缝的内容一并归消息带
                dx2x = (int(w * self._message_region[0]) - u_l) * 2
                dy2x = (int(h * self._message_region[1]) - u_t) * 2
                items = [dict(it, x=it["x"] - dx2x, y=it["y"] - dy2x)
                         for it in all_items if (it["y"] + it["h"] // 2) >= t_bot_2x]
            else:
                # 单片 OCR（v2.1.3 废弃分片）：分片左右切边界会把整字切成两半
                # 误识（真机：「排」被切左半成「非」，产碎片「非序错乱」；探针
                # 稳定复现碎片「丙」「马」）。RapidOCR limit_side_len="min" 是
                # 短边不足才放大，宽图不压缩——分片解决的「压缩截断」不存在，
                # 单片读 40+ 字超长行一字不差。
                items = ocr_image(region)
            if items:
                # 连通域分气泡：自动探测主题气泡色/背景色，找气泡边界框，
                # 换算到 2x 与 OCR 坐标对齐。探测失败（纯色/mock 截图）时
                # bubble_boxes 留空 → 下行合并回退 y 阈值逻辑。
                colors = detect_bubble_colors(region_1x)
                if colors.get("self") or colors.get("other"):
                    boxes_1x = find_bubble_boxes(region_1x, colors)
                    bubble_boxes = [(t * 2, b * 2, l * 2, r * 2, is_self)
                                    for (t, b, l, r, is_self) in boxes_1x]
                break
            # 消息区空白：可能 toggle 取消选中了（微信再点一次恢复选中）
            if attempt == 0:
                logger.info(f"[读取] {chat!r} 消息区读到 0 条，可能 toggle 取消选中，再点一次")
                self._switch_chat(chat, force=True)
        if not items:
            return []
        # 按 y 排序 → 合并相邻行成消息块（时间戳行作为分隔）
        items.sort(key=lambda it: (it["y"], it["x"]))
        # 气泡归属标记：每个 OCR 行标它落在哪个气泡框（供分组 + sender 判定）
        for it in items:
            cy = it["y"] + it["h"] // 2
            cx = it["x"] + it["w"] // 2
            it["_bubble"] = None
            it["_bubble_self"] = None
            for (t, b, l, r, is_self) in bubble_boxes:
                if t <= cy <= b and l <= cx <= r:
                    it["_bubble"] = (t, b)
                    it["_bubble_self"] = is_self
                    break
        # 头像文字排除：自己头像（右侧几何窄带）+ 对方头像（气泡左边缘反推）。
        # 头像图片上的文字（如「蓝色大肥鱼」）不应成为消息内容——头像在
        # 气泡外侧：自己头像在右侧（x/w≥0.84 窄带），对方头像在左侧（气泡框
        # left 更左边那一列）。
        right_tops_1x = detect_avatar_tops(region_1x, colors.get("bg"), "right")
        other_tops_1x = detect_avatar_tops(region_1x, colors.get("bg"), "left")
        # media 框归属：文件卡片/图片是 media 框（无气泡），find_bubble_boxes
        # 覆盖不到（其文本行 _bubble=None）。补 media 框 + 头像几何判据——
        # 顶部对齐右侧头像的 media 归 bot、对齐左侧归对方，把 bot 文件卡片
        # 的文本行（文件名）也标 self。真机根因：bot 发 index.html，文件卡片
        # media 框横跨中线（l=334 < 373 < r=613），文件名 OCR 行 x 靠左被
        # _is_self 降级判对方，上层窗口过滤漏进 bot 消息误判为对方文件。
        media_1x = find_media_boxes(region_1x, colors)
        # 头像划块：左右头像 top 混合排序成边界序列，media 框顶部落入
        # 哪个区间 → 归属该头像（x 侧：右=自己/左=对方）。替代旧 tol=40
        # 对齐——区间归属无需魔法数容差。
        all_avatar_tops_1x = sorted(set(right_tops_1x) | set(other_tops_1x))
        media_self_boxes = []   # 2x 坐标，归 bot 的 media 框
        media_other_boxes = []  # 2x 坐标，归对方的 media 框
        for (mt, mb, ml, mr) in media_1x:
            hit = _bucket_avatar(mt, all_avatar_tops_1x)
            if hit is not None and hit in right_tops_1x:
                media_self_boxes.append((mt * 2, mb * 2, ml * 2, mr * 2))
            elif hit is not None:
                media_other_boxes.append((mt * 2, mb * 2, ml * 2, mr * 2))
        for it in items:
            cy = it["y"] + it["h"] // 2
            cx = it["x"] + it["w"] // 2
            it["_media_self"] = None
            it["_in_media"] = False
            for (t, b, l, r) in media_self_boxes:
                if t <= cy <= b and l <= cx <= r:
                    it["_media_self"] = True
                    break
            if it["_media_self"] is None:
                for (t, b, l, r) in media_other_boxes:
                    if t <= cy <= b and l <= cx <= r:
                        it["_media_self"] = False
                        # 仅对方媒体框剔除：对方图片块内文字（图片上的字）
                        # 是假消息（真机 '我不是'）；bot 文件卡片文件名
                        # （media_self_boxes）是真实 bot 输出，保留 sender='self'
                        # （既有验收 test_get_messages_bot_file_card_sender_self）。
                        it["_in_media"] = True
                        break
        avatar_h_1x = max(40, region_1x.height // 18)
        avatar_x_2x = int(region_1x.width * 0.84) * 2
        avatar_w_2x = region.width - avatar_x_2x
        avatar_h_2x = avatar_h_1x * 2
        self_avatar_boxes = [
            (avatar_x_2x, top * 2, avatar_w_2x, avatar_h_2x)  # (x, y, w, h)
            for top in right_tops_1x
        ]
        # 对方头像只在消息区左侧（对方消息/头像在左）：只取 left<中线 的
        # other 气泡框。对方气泡色接近背景时 find_bubble_boxes 会把右侧
        # 背景误连成 other 框（真机实测 left=1220 > 中线 747）——若不过滤，
        # other_avatar_x_max 被污染成右侧值，消息区所有行 cx<该值 被误判
        # 头像区丢弃，全部消息读空（get_messages 读 0 条的真机根因）。
        other_lefts = [l for (t, b, l, r, is_self) in bubble_boxes
                       if not is_self and l < region.width // 2]
        other_avatar_x_max = min(other_lefts) if other_lefts else None
        for it in items:
            cx = it["x"] + it["w"] // 2
            cy = it["y"] + it["h"] // 2
            it["_in_avatar"] = False
            for (ax, ay, aw, ah) in self_avatar_boxes:
                if ax <= cx <= ax + aw and ay <= cy <= ay + ah:
                    it["_in_avatar"] = True
                    break
            if not it["_in_avatar"] and other_avatar_x_max is not None \
                    and cx < other_avatar_x_max:
                it["_in_avatar"] = True
        midline_x = region.width // 2  # 消息区中线：右侧=自己发的
        # 头像锚定：右侧头像中心 y（几何窄带），优先于 x 坐标
        avatar_ys = [top * 2 + avatar_h_1x for top in right_tops_1x]  # 2x 头像中心 y

        def _is_self(first_x: int, first_y: int) -> bool:
            for ay in avatar_ys:
                if abs(first_y - ay) < 35:  # 消息 y 与头像 y 对齐 → self
                    return True
            return first_x > midline_x  # 降级：x 中线

        msgs: list[WeChatMessage] = []
        cur_lines: list[dict] = []
        cur_y: list[int] = []
        seq = 0
        # 群聊发送者名识别：气泡上方短文本行（如 '哆拉A萝'）紧贴内容上方。
        # pending_name = (文本, y)：候选发送者名；被后续内容行消费（y 差<150）
        # → 成为该块 sender；悬空（后面没紧跟内容）→ 它本身是一条独立短消息。
        pending_name: tuple[str, int] | None = None
        block_sender: str | None = None

        def _is_timestamp(text: str) -> bool:
            """时间戳/日期行：'昨天 18:45' / '20:14' / '下午 2:30' / '1月1日'。"""
            if not text:
                return True
            t = text.replace(" ", "").replace("：", ":")
            return bool(re.match(
                r"^(昨天|前天|今天)?(\d{1,2}[:.]\d{1,2}|上午|下午|晚上|"
                r"\d{1,2}月\d{1,2}日|星期[一二三四五六日天])",
                t))

        def _is_noise(text: str) -> bool:
            """OCR 噪音：空文本，或全部是标点/符号/异常字符（无 CJK 无字母无数字）。

            单字符（如「1」「好」「在」「嗯」）是合法消息，不得当噪声过滤——
            用户实测发「1」是真实测试消息，曾被 len<2 规则误杀。
            """
            if not text:
                return True
            # 全部是标点/符号/异常字符（无 CJK 无字母无数字）
            if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", text):
                return True
            return False

        def _flush():
            nonlocal seq, block_sender, pending_name
            if cur_lines:
                # 一条消息：多行文本合并
                text = " ".join(l["text"] for l in cur_lines).strip()
                text = re.sub(r"\s+", " ", text)
                if text and not _is_noise(text):
                    # sender 判定：块内首行 x 相对中线（右侧=自己）
                    first_x = cur_lines[0]["x"]
                    first_y = cur_lines[0]["y"]
                    # 输入框按钮噪声：微信输入框"发送"按钮固定在消息区右下角，
                    # OCR 会把它读成消息且 x 靠右判 self——顶掉真实最新消息导致
                    # 上层跳过整会话（真机日志：latest sender='self' content='发送'）。
                    is_send_button = (text == "发送" and first_x > 0.85 * region.width
                                      and first_y > 0.8 * region.height)
                    if not is_send_button:
                        # 群聊：气泡上方发送者名（pending_name 被内容行消费时
                        # 设置 block_sender）；私聊退回会话名。
                        # sender 判定：连通域气泡 is_self 优先（最可靠），
                        # 气泡探测失败时降级头像锚定 + x 中线。
                        bubble_self = cur_lines[0].get("_bubble_self")
                        media_self_flag = cur_lines[0].get("_media_self")
                        # 头像划块优先于气泡色：bot 文件卡片颜色接近 other
                        # 气泡色会被 find_bubble_boxes 误判 is_self=False，但
                        # 消息框顶（气泡框 top 优先、OCR 行 y 兜底）落入头像
                        # 边界序列区间 → 区间头像 x 侧定 sender（右=自己/
                        # 左=对方）。群聊对方消息 sender 用 block_sender
                        # （发送者名），不得被 x 中线/气泡色取代——名字是
                        # 用户硬性要求保留的信息。
                        first_y_1x = first_y // 2
                        if cur_lines[0].get("_bubble") is not None:
                            first_y_1x = cur_lines[0]["_bubble"][0] // 2
                        bucket_hit = _bucket_avatar(first_y_1x,
                                                    all_avatar_tops_1x)
                        if bucket_hit is not None \
                                and bucket_hit in right_tops_1x:
                            sender = "self"
                        elif bucket_hit is not None:
                            sender = block_sender or chat
                        elif bubble_self is not None:
                            sender = "self" if bubble_self else (block_sender or chat)
                        elif media_self_flag is not None:
                            sender = "self" if media_self_flag else (block_sender or chat)
                        else:
                            sender = "self" if _is_self(first_x, first_y) \
                                else (block_sender or chat)
                        seq += 1
                        msgs.append(WeChatMessage(
                            id=f"visual_{seq}",
                            chat=chat,
                            sender=sender,
                            content=text,
                            type=MessageType.TEXT,
                            y=first_y // 2,
                        ))
                cur_lines.clear()
                cur_y.clear()
            block_sender = None
            # 悬空的候选发送者名（后面没紧跟内容）→ 它本身是一条独立短消息
            if pending_name is not None:
                name, pname_y = pending_name
                pending_name = None
                seq += 1
                msgs.append(WeChatMessage(
                    id=f"visual_{seq}", chat=chat, sender=chat,
                    content=name, type=MessageType.TEXT,
                    y=pname_y // 2,
                ))

        for it in items:
            text = it["text"]
            # 时间戳行：作为分隔符（flush 前块 + 悬空候选名），本身不入块
            if _is_timestamp(text):
                _flush()
                pending_name = None
                continue
            if _is_noise(text):
                continue
            # 头像文字排除：落在头像区域（自己模板 / 对方气泡左列）的文字
            # 是头像图片上的字，不是消息内容，直接丢弃。
            if it.get("_in_avatar"):
                continue
            # media 框内文字剔除（与 _in_avatar 对称）：图片/文件消息的
            # 内容矩形（media 框）内 OCR 到的文字（图片上的字/文件名）不
            # 拆成假文字消息——图片块只产 1 条媒体消息（由 analyze_window
            # 媒体框承载，上层 has_media 分支处理）。紧贴框顶的候选发送者
            # 名（pending_name）是媒体消息的发送者，一并吞掉不产独立消息。
            if it.get("_in_media"):
                if pending_name is not None \
                        and abs(it["y"] - pending_name[1]) < 150:
                    pending_name = None
                continue
            # 气泡归并：连通域气泡框优先——同一气泡框的行合并、不同气泡框
            # 换块（气泡内换行 vs 跨气泡不再靠猜行距）。气泡框缺失（纯色/
            # mock 截图探测失败）时回退 y 阈值。
            if cur_lines:
                cb = cur_lines[0].get("_bubble")
                ib = it.get("_bubble")
                if cb is not None and ib is not None:
                    if ib != cb:
                        _flush()
                elif cb is not None or ib is not None:
                    _flush()  # 一个在气泡内、一个在气泡外（发送者名/时间戳）→ 换块
                elif cur_y and abs(it["y"] - cur_y[-1]) > _BUBBLE_LINE_GAP:
                    _flush()
            if not cur_lines:
                first_x0 = it["x"]
                first_y0 = it["y"]
                # 1. self（右侧自己发的）消息无群聊发送者名，直接成内容
                if _is_self(first_x0, first_y0):
                    if pending_name is not None:
                        _flush()  # 悬空候选（如私聊短消息）先成独立消息
                        pending_name = None
                    cur_lines.append(it)
                    cur_y.append(it["y"])
                    continue
                # 2. 候选发送者名已存在且本行紧贴（y 差 <150——真机名字-内容
                #    106px、内容块间最小 186px）→ 消费：本行是内容，候选是发送者
                if pending_name is not None and abs(first_y0 - pending_name[1]) < 150:
                    block_sender = pending_name[0]
                    pending_name = None
                    cur_lines.append(it)
                    cur_y.append(it["y"])
                    continue
                # 3. 悬空候选（存在但不紧贴）→ 它是独立短消息，先 flush
                if pending_name is not None:
                    _flush()
                    pending_name = None
                # 4. 对方短文本（≤8 字符）→ 候选群聊发送者名（不立即成消息）。
                #    但气泡框内的短文本是内容（如 8 字标题），不是发送者名——
                #    发送者名在气泡上方、气泡框外（_bubble 为 None）。
                if it.get("_bubble") is None and len(text.strip()) <= 8:
                    pending_name = (text.strip(), first_y0)
                    continue
                # 5. 普通内容行
                cur_lines.append(it)
                cur_y.append(it["y"])
            else:
                # 续行：同一气泡内的后续换行/左右分片框，直接并入当前块。
                # （旧实现无此分支——cur_lines 非空时续行被静默丢弃，多行长
                # 消息只剩第一行，任务 raw_message 截断。）
                cur_lines.append(it)
                cur_y.append(it["y"])
        _flush()

        if limit:
            msgs = msgs[-limit:]
        return msgs

    # ---- 协议：发送 ----

    def send_text(self, chat: str, text: str) -> bool:
        """定位输入框 → 点击聚焦 → 键盘输入 → Enter。"""
        try:
            import pyautogui
            rect = self._input_box_rect()
            if rect is None:
                logger.error("无法定位输入框")
                return False
            cx = rect[0] + rect[2] // 2
            cy = rect[1] + rect[3] // 2
            self._foreground()  # 点击/键盘输入依赖前台，先置前微信窗口
            pyautogui.click(cx, cy)
            time.sleep(0.3)
            # 中文发送必须走剪贴板粘贴：typewrite 逐键模拟对非 ASCII 字符
            # 无法映射键位，按键序列被中文输入法拦截成"（）"（真机实测）
            import pyperclip
            pyperclip.copy(text)
            time.sleep(0.1)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.2)
            pyautogui.press("enter")
            return True
        except Exception as e:
            logger.error(f"发送失败: {e}")
            return False

    def _input_box_rect(self):
        """定位输入框（打字区域），返回屏幕坐标 (l,t,w,h)，中心点供点击聚焦。

        优先读标定值（tools/calibrate_input_box.py 生成，用户点击确认的
        输入框中心相对窗口比例）；无标定时回退硬编码比例（消息区底部约 82% 高）。
        """
        if self._hwnd is None:
            return None
        rect = wt.RECT()
        u32.GetWindowRect(self._hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        cal = self._load_input_box_calibration()
        if cal:
            cx = rect.left + int(w * cal["fx"])
            cy = rect.top + int(h * cal["fy"])
            # 以标定点为中心的小矩形（点击聚焦用，宽高只需覆盖输入框即可）
            return (cx - int(w * 0.15), cy - int(h * 0.03), int(w * 0.30), int(h * 0.06))
        # 回退：硬编码比例
        l = rect.left + int(w * self._message_region[0]) + 20
        t = rect.top + int(h * 0.82)
        return (l, t, int(w * (self._message_region[2] - self._message_region[0])) - 40, int(h * 0.12))

    def _load_input_box_calibration(self):
        """读输入框标定值（相对窗口宽高比例）。无标定文件/非法值返回 None。"""
        try:
            cal_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "wx_input_box.json")
            if not os.path.isfile(cal_path):
                return None
            with open(cal_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            fx = data.get("fx")
            fy = data.get("fy")
            if isinstance(fx, (int, float)) and isinstance(fy, (int, float)):
                if 0 <= fx <= 1 and 0 <= fy <= 1:
                    return {"fx": float(fx), "fy": float(fy)}
        except (OSError, ValueError):
            pass
        return None

    def media_screen_boxes(self) -> list[tuple[int, int]]:
        """检测消息区媒体内容（图片/视频/表情）的屏幕中心点，供点击打开预览。

        截图消息区 → 探测气泡色 → find_media_boxes 检测「非背景非气泡」的
        大块媒体矩形 → 换算屏幕物理坐标（DPI 已 per-monitor aware，窗口
        rect 与截图同坐标系）。返回 [(cx, cy), ...] 屏幕坐标，检测不到返回空。
        """
        if self._hwnd is None:
            return []
        shot = self._refresh(force=True)
        if shot is None:
            return []
        w, h = shot.size
        region = shot.crop((
            int(w * self._message_region[0]), int(h * self._message_region[1]),
            int(w * self._message_region[2]), int(h * self._message_region[3]),
        ))
        colors = detect_bubble_colors(region)
        if not (colors.get("self") or colors.get("other")):
            return []
        boxes = find_media_boxes(region, colors)
        if not boxes:
            return []
        rect = wt.RECT()
        u32.GetWindowRect(self._hwnd, ctypes.byref(rect))
        win_l, win_t = rect.left, rect.top
        msg_l = win_l + int(w * self._message_region[0])
        msg_t = win_t + int(h * self._message_region[1])
        return [(msg_l + (l + r) // 2, msg_t + (t + b) // 2)
                for (t, b, l, r) in boxes]

    def analyze_window(self, chat: str, foreground: bool = True,
                       skip_bot: int = 0) -> dict:
        """切会话 + 截图 + 气泡/媒体分析，返回窗口内消息结构（不 OCR 文字）。

        供上层先判断「窗口内是否只有文字」还是「有图/文件」，再决定
        sleep 10s 防话没说完 / OCR 读文字。

        bot 消息判定：头像几何（右侧窄带非背景块=bot 头像，左侧=对方头像）。
        无需头像模板、无需绿气泡色、无需宽度阈值——头像大小/位置固定，
        对方长文字/文件即使右边缘靠右也不会被误判为 bot。

        skip_bot：跳过最近 N 条 bot 消息再定位 bot_bottom（占位回复剔除）。
        跳过的是 bot 头像序列里最新的 N 条，即 last_bot_top 取
        sorted(bot_tops)[-(1 + skip_bot)]；skip_bot=0 时恒等于 max(bot_tops)，
        与旧行为完全一致。越界时 skip 收敛到 len(bot_tops)-1 兜底不越界。
        other_text / other_media / has_other 随新的 last_bot_top 上移。

        返回 dict：
        {
            "bot_bottom": int | None,       # 我方最后回复之后第一条消息的上边框（1x）；无下一条=消息区高，None=无 bot 回复
            "other_text": [(t,b,l,r), ...],  # 窗口内对方文字气泡框
            "other_media": [(t,b,l,r), ...], # 窗口内对方媒体矩形（图/视频/表情/文件卡片）
            "has_text": bool,
            "has_media": bool,
            "has_other": bool,               # 有 bot 之后的对方新消息（纯头像几何判据，不依赖气泡/media）
            "width": int, "height": int,
        }
        """
        self._switch_chat(chat)
        # 纯像素分析（不再单独读标题——用户定案：事件内 OCR 两次封顶，
        # 即列表条带锚定 + 标题/消息联合 OCR）。权威 is_group 由
        # get_messages(assume_switched=True) 的联合 OCR 标题解析在读取
        # 消息时刷新 _current_is_group；本返回值的 is_group 仅为缓存快照
        # （可能来自上一轮事件），调用方必须在 _window_msgs 之后再取缓存。
        is_group = bool(getattr(self, "_current_is_group", False))
        empty = {"bot_bottom": None, "other_text": [], "other_media": [],
                 "has_text": False, "has_media": False, "has_other": False,
                 "is_group": is_group, "width": 0, "height": 0}
        for attempt in range(2):
            if attempt > 0:
                # 第一次分析结果为空：可能 toggle 取消选中（_switch_chat 标题
                # 检测在微信被遮挡时失败，误点击已选中会话），force 重切恢复选中
                self._switch_chat(chat, force=True)
            shot = self._refresh(force=True, foreground=foreground)
            if shot is None:
                continue
            w, h = shot.size
            region = shot.crop((
                int(w * self._message_region[0]), int(h * self._message_region[1]),
                int(w * self._message_region[2]), int(h * self._message_region[3]),
            ))
            rw, rh = region.size
            colors = detect_bubble_colors(region)
            bubbles = find_bubble_boxes(region, colors)   # [(t,b,l,r,is_self)]
            media = find_media_boxes(region, colors)       # [(t,b,l,r)]
            # bot 消息判定：头像几何。右侧窄带非背景块 = bot 头像，左侧 = 对方头像。
            # 头像大小/位置固定，无需模板、无需颜色、无需宽度阈值。真机实测：
            # bot 文件 r/w=0.83、对方长文字 r/w=0.80——宽度阈值切不开，头像一右一左分离。
            bot_tops = detect_avatar_tops(region, colors.get("bg"), "right")
            other_tops = detect_avatar_tops(region, colors.get("bg"), "left")
            # 头像划块：左右头像 top 混合排序成边界序列，消息框（气泡/
            # media 框）顶部 y 落入哪个区间 → 归属该区间头像（替代旧
            # tol=40 _aligned 对齐——区间归属无魔法数容差）。
            all_avatar_tops = sorted(set(bot_tops) | set(other_tops))
            # 消息定位改用头像几何不变量：消息上边框 = 对应头像上边框。
            # bot_bottom = 我方最后回复之后第一条消息的上边框（不再依赖
            # find_bubble_boxes 检测 bot 气泡算 bottom——气泡色漂移会导致
            # bot 气泡漏检、bot_bottom 偏小或为 None，漏掉对方新消息）。
            last_bot_top = max(bot_tops) if bot_tops else None
            # skip_bot：跳过最近 N 条 bot 消息（占位回复剔除）。last_bot_top
            # 取 sorted(bot_tops)[-(1+skip)]——skip=0 时恒等于 max(bot_tops)，
            # 与旧行为一致；skip 先做 min(skip_bot, len(bot_tops)-1) 兜底，
            # bot_tops 不足时不越界。other_new_tops 等随后续 last_bot_top 上移。
            if bot_tops:
                skip = min(skip_bot, len(bot_tops) - 1)
                last_bot_top = sorted(bot_tops)[-(1 + skip)]
            else:
                last_bot_top = None
            if last_bot_top is not None:
                after = [t for t in all_avatar_tops if t > last_bot_top]
                bot_bottom = after[0] if after else rh
            else:
                bot_bottom = None
            # 对方新消息 = bot 最后头像之后的左侧头像 top（纯几何，不依赖
            # 气泡/media 检测——气泡色漂移、黑图被当背景都不会漏判）
            other_new_tops = [t for t in other_tops
                              if last_bot_top is None or t > last_bot_top]
            other_new_set = set(other_new_tops)
            # 类型区分：气泡/media 框顶部落入对方新消息头像区间（划块归属）
            other_text = [(t, b, l, r) for (t, b, l, r, _is_self) in bubbles
                          if _bucket_avatar(t, all_avatar_tops) in other_new_set]
            other_media = [(t, b, l, r) for (t, b, l, r) in media
                           if _bucket_avatar(t, all_avatar_tops) in other_new_set]
            has_other = bool(other_new_tops)
            # [临时诊断日志] 抓"窗口空"现场，定位后删除
            logger.info(
                f"[analyze] chat={chat!r} attempt={attempt} region={rw}x{rh} "
                f"colors={colors} bubbles={bubbles} media={media} "
                f"bot_tops={bot_tops} other_tops={other_tops} "
                f"bot_bottom={bot_bottom} other_text={other_text} "
                f"other_media={other_media} has_text={bool(other_text)} "
                f"has_media={bool(other_media)} has_other={has_other}"
            )
            # 窗口完全空（无气泡/媒体/头像）→ toggle 取消选中，重切兜底
            if not bubbles and not media and not bot_tops and not other_tops:
                continue
            return {
                "bot_bottom": bot_bottom,
                "other_text": other_text,
                "other_media": other_media,
                "has_text": bool(other_text),
                "has_media": bool(other_media),
                "has_other": has_other,
                "is_group": is_group,
                "width": rw, "height": rh,
            }
        return empty

    def send_file(self, chat: str, file_path: str) -> bool:
        """发送文件：暂未实现（视觉定位文件按钮 + 文件对话框输入路径）。"""
        logger.warning("visual 后端 send_file 暂未实现")
        return False

    # ---- 协议：定位 ----

    def locate_message(self, message: WeChatMessage) -> Any:
        """视觉方案下消息定位 = 无（返回 None，上层退回截图处理）。"""
        return None

    # ---- 协议：关闭 ----

    def close(self) -> None:
        self._closed = True
        self._hwnd = None
        self._last_shot = None


def register():
    """注册 visual 后端（priority=1，auto 链第一优先——新版微信唯一通道，
    且无 wxauto 的窗口副作用）。"""
    from . import register_backend
    register_backend("visual", VisualBackend, priority=1)


# 模块导入即注册（wx_backend/__init__ 会显式调用，避免隐式副作用歧义，
# 这里保留函数便于测试与显式调用）
__all__ = [
    "VisualBackend", "register", "capture_window", "ocr_image",
    "region_changed", "find_wechat_window",
]
