# -*- coding: utf-8 -*-
"""visual_backend 拆分 · Win32 通道层：DPI 感知 / 窗口查找定位 / PrintWindow 截图。

被 wx_backend.visual_backend（门面）re-export——测试与工具的导入路径、
mock.patch("wx_backend.visual_backend.capture_window") 等打点全部不变
（门面是这些名字的消费方，patch 打在门面命名空间才生效）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import time

from PIL import Image

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
u32.IsZoomed.restype = wt.BOOL
u32.IsZoomed.argtypes = [wt.HWND]
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


# 微信主窗口判据（**进程名优先**，与标题无关）。历史缺陷：按「标题含微信」
# 子串匹配——用户在浏览器里开着本仓库页面时，浏览器标签标题（仓库描述里含
# "微信 PC 4.x"）被当成微信窗口：仪表盘上把整条标题当详情显示，运行期还会
# 照它截图。现行判据两级：
#   ① 进程名 = Weixin.exe（微信 4.x）/ WeChat.exe（3.x）——精确相等，不用
#      子串（WeChatAppEx.exe 是小程序宿主，同样有窗口，必须排除在外）；
#   ② 标题严格等于「微信」/「WeChat」（进程名读不到时的兜底；只做相等，
#      不做包含——标题里带"微信"二字的浏览器/文档窗口一律不认）。
_PROCESS_NAMES = ("weixin.exe", "wechat.exe")
_TITLE_EXACT = ("微信", "wechat")
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
u32.GetWindowThreadProcessId.restype = wt.DWORD
# 64 位句柄安全同上：不声明 argtypes 时 HWND 会被截成 32 位
u32.GetWindowTextW.argtypes = [wt.HWND, ctypes.c_wchar_p, wt.INT]
_k32 = ctypes.windll.kernel32
_k32.OpenProcess.restype = _HANDLE
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.QueryFullProcessImageNameW.argtypes = [
    _HANDLE, wt.DWORD, ctypes.c_wchar_p, ctypes.POINTER(wt.DWORD)]
_k32.CloseHandle.argtypes = [_HANDLE]


def window_title(hwnd) -> str:
    """窗口标题（空标题返回空串）。"""
    if not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(512)
    u32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def process_basename(pid: int) -> str:
    """进程可执行文件名（小写，如 "weixin.exe"）；读不到返回空串。

    微信可能以更高权限运行：QueryFullProcessImageNameW 用
    PROCESS_QUERY_LIMITED_INFORMATION 打开（Vista+ 对提权进程也放行），
    失败一律返回空串由标题兜底，不抛异常。
    """
    if not pid:
        return ""
    handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        if _k32.QueryFullProcessImageNameW(handle, 0, buf,
                                           ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1].lower()
        return ""
    except Exception:
        return ""
    finally:
        _k32.CloseHandle(handle)


def window_pid(hwnd) -> int:
    """窗口所属进程 PID；失败返回 0。"""
    if not hwnd:
        return 0
    pid = wt.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def is_wechat_window(hwnd) -> bool:
    """该窗口是否微信主窗口（进程名判据，标题无关）。"""
    return process_basename(window_pid(hwnd)) in _PROCESS_NAMES


def _window_area(hwnd) -> int:
    rect = window_rect(hwnd)
    return rect[2] * rect[3] if rect else 0


def pick_wechat_window(candidates) -> int | None:
    """从候选窗口里挑微信主窗口（纯函数，便于单测）。

    candidates: [(hwnd, title, proc_name, area), ...]（proc_name 小写，
    读不到时为空串；area 为窗口像素面积）。
    判据优先级同 find_wechat_window：先只信进程名，进程名一个都没有才退回
    "标题严格等于微信"。同组内先取标题严格命中的（主窗口），再按面积取最大
    者——`Weixin.exe` 同时持有主窗口与若干辅助窗，辅助窗更小。
    """
    def _exact(c):
        return c[1].strip().lower() in _TITLE_EXACT
    for pool in (
        [c for c in candidates if (c[2] or "") in _PROCESS_NAMES],
        [c for c in candidates if _exact(c)],
    ):
        if not pool:
            continue
        exact = [c for c in pool if _exact(c)]
        return max(exact or pool, key=lambda c: c[3])[0]
    return None


def enumerate_visible_windows() -> list:
    """可见顶层窗口 → [(hwnd, title, proc_name, area), ...]。"""
    out: list = []

    @_WNDENUMPROC
    def _cb(hwnd, _lparam):
        if not u32.IsWindowVisible(hwnd):
            return True
        title = window_title(hwnd).strip()
        if not title:
            return True
        out.append((hwnd, title, process_basename(window_pid(hwnd)),
                    _window_area(hwnd)))
        return True

    u32.EnumWindows(_cb, 0)
    return out


def find_wechat_window():
    """返回微信主窗口句柄；未找到返回 None（判据见 _PROCESS_NAMES 注释）。"""
    return pick_wechat_window(enumerate_visible_windows())


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


def resize_window_visible(hwnd, w: int, h: int) -> bool:
    """只改窗口大小、**保持左上角不动**（可见内容语义：按目标可见尺寸外扩
    不可见边框外沿）。与 position_window_visible 的唯一区别是 x/y 取窗口
    当前值——位置由用户自己摆放，程序一概不移动。"""
    r = window_rect(hwnd)
    if not r:
        return False
    m = visible_frame_margins(hwnd)
    if m is None:
        m = (0, 0, 0, 0)
    ml, mt, mr, mb = m
    return position_window(hwnd, r[0], r[1],
                           int(w) + ml + mr, int(h) + mt + mb)


def visible_window_size(hwnd) -> tuple[int, int] | None:
    """窗口可见内容尺寸 (w, h)：窗口矩形扣掉 DWM 不可见外沿。"""
    r = window_rect(hwnd)
    if not r:
        return None
    m = visible_frame_margins(hwnd) or (0, 0, 0, 0)
    ml, mt, mr, mb = m
    return (max(1, r[2] - ml - mr), max(1, r[3] - mt - mb))


# ---------- 窗口位置锚定（可见内容拉回显示器工作区） ----------

_MONITORINFO = None


def _monitor_info_type():
    """MONITORINFO 结构体（懒构造，import 期不碰 ctypes 结构定义顺序）。"""
    global _MONITORINFO
    if _MONITORINFO is None:
        class _MI(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD),
                        ("rcMonitor", wt.RECT),
                        ("rcWork", wt.RECT),
                        ("dwFlags", wt.DWORD)]
        _MONITORINFO = _MI
    return _MONITORINFO


def _work_area_from_handle(hmon) -> tuple[int, int, int, int] | None:
    """HMONITOR → 工作区 (l, t, r, b)；失败 None。"""
    if not hmon:
        return None
    try:
        mi = _monitor_info_type()()
        mi.cbSize = ctypes.sizeof(mi)
        if not u32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return None
        rc = mi.rcWork
        return (rc.left, rc.top, rc.right, rc.bottom)
    except Exception:
        return None


def monitor_work_area(hwnd) -> tuple[int, int, int, int] | None:
    """窗口所在显示器（MONITOR_DEFAULTTONEAREST）的工作区 (l, t, r, b)。

    工作区 = 扣除任务栏后的可用区域，物理像素（与 window_rect 同一 DPI
    感知上下文）。失败返回 None。"""
    if not hwnd:
        return None
    try:
        u32.MonitorFromWindow.restype = _HANDLE
        u32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
        u32.GetMonitorInfoW.restype = wt.BOOL
        u32.GetMonitorInfoW.argtypes = [_HANDLE, ctypes.POINTER(_monitor_info_type())]
        return _work_area_from_handle(u32.MonitorFromWindow(hwnd, 2))
    except Exception:
        return None


def monitor_work_area_at(x: int, y: int) -> tuple[int, int, int, int] | None:
    """坐标 (x, y) 所在显示器（最近者）的工作区；失败 None。

    位置强制的运行期复查用它按「家坐标」找显示器：窗口被拖到副屏时
    仍按家所在屏的工作区判定，而不是跟着窗口跑到副屏去（否则家坐标
    会被副屏工作区裁剪、窗口被拽到副屏边缘）。屏被拔掉后最近者返回
    剩余显示器，家坐标自然被拉回可见范围。"""
    try:
        u32.MonitorFromPoint.restype = _HANDLE
        u32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
        u32.GetMonitorInfoW.restype = wt.BOOL
        u32.GetMonitorInfoW.argtypes = [_HANDLE, ctypes.POINTER(_monitor_info_type())]
        pt = wt.POINT(int(x), int(y))
        return _work_area_from_handle(u32.MonitorFromPoint(pt, 2))
    except Exception:
        return None


def clamp_pos_visible(x: int, y: int, w: int, h: int,
                      area: tuple[int, int, int, int]) -> tuple[int, int]:
    """把可见内容矩形 (x, y, w, h) 拉回工作区 area=(l, t, r, b) 内（纯函数）。

    返回拉回后的可见内容左上角。越界任意一边都拉回：可见内容超出工作区
    的部分点不到（pyautogui 点屏幕外坐标落空），半出屏和全出屏一样坏。
    窗口可见内容比工作区还大时贴工作区左/上沿（另一侧伸出不可避免，
    强制尺寸 1300x1610 在小屏上会这样）。"""
    l, t, r, b = area
    max_x = max(l, r - w)
    max_y = max(t, b - h)
    return (min(max(int(x), l), max_x), min(max(int(y), t), max_y))


def pull_window_into_view(hwnd) -> tuple[int, int] | None:
    """窗口（部分）越出所在显示器工作区时拉回完整可见；已在区内则不动。

    位置强制的**唯一**动作（不做「钉死」）：窗口在屏幕内的位置完全由用户
    决定，程序只在它出屏时出手——点击是屏幕绝对坐标，窗口（部分）移出屏幕
    后 pyautogui 点到屏幕外（切会话/点图片/发送全失灵）。返回最终窗口矩形
    左上角 (x, y)；读取失败返回 None。

    可见内容判定用 DWM 外沿（窗口矩形含 ~10px 不可见边框，直接按窗口矩形
    判定会把贴边摆放误判成越界——真机：右贴边窗口矩形伸出屏幕约 10px）。
    窗口比工作区还大时贴工作区左/上沿（另一侧伸出不可避免，强制尺寸
    1300x1610 在小屏上会这样）。"""
    rect = window_rect(hwnd)
    if not rect:
        return None
    area = monitor_work_area(hwnd)
    if not area:
        return (rect[0], rect[1])
    m = visible_frame_margins(hwnd)
    if m is None:
        m = (0, 0, 0, 0)
    ml, mt, mr, mb = m
    vx, vy = rect[0] + ml, rect[1] + mt
    vw = max(1, rect[2] - ml - mr)
    vh = max(1, rect[3] - mt - mb)
    cx, cy = clamp_pos_visible(vx, vy, vw, vh, area)
    if (cx, cy) == (vx, vy):
        return (rect[0], rect[1])
    position_window_visible(hwnd, cx, cy, vw, vh)
    moved = window_rect(hwnd)
    return (moved[0], moved[1]) if moved else (rect[0], rect[1])


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
