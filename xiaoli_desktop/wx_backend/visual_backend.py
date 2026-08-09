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
gdi32.SelectObject.restype = _HANDLE
gdi32.SelectObject.argtypes = [_HANDLE, _HANDLE]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.DeleteObject.argtypes = [_HANDLE]
gdi32.DeleteDC.restype = wt.BOOL
gdi32.DeleteDC.argtypes = [_HANDLE]
gdi32.PrintWindow = u32.PrintWindow  # 别名（PrintWindow 在 user32）
u32.PrintWindow.restype = wt.BOOL
u32.PrintWindow.argtypes = [wt.HWND, _HANDLE, wt.UINT]


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


# ---------- Windows OCR（winsdk） ----------

_OCR_ENGINE = None


def _get_ocr_engine():
    """懒加载 Windows OCR 引擎（zh-Hans-CN）。不可用返回 None。"""
    global _OCR_ENGINE
    if _OCR_ENGINE is not None:
        return _OCR_ENGINE
    try:
        from winsdk.windows.globalization import Language
        from winsdk.windows.media.ocr import OcrEngine

        langs = [l.language_tag for l in OcrEngine.available_recognizer_languages]
        if not any("zh" in l for l in langs):
            logger.warning(f"Windows OCR 无中文语言包: {langs}")
            _OCR_ENGINE = False
            return None
        _OCR_ENGINE = OcrEngine.try_create_from_language(Language("zh-Hans-CN"))
        return _OCR_ENGINE
    except Exception as e:
        logger.warning(f"Windows OCR 不可用: {e}")
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
    """对图像做 Windows OCR，返回 [{text, x, y, w, h}]（图像像素坐标）。

    大图先按 max_w 等比缩小（OCR 精度随缩放变化，默认不缩放）。
    返回空列表表示识别失败或无文本。
    """
    engine = _get_ocr_engine()
    if not engine:
        return []

    async def _run():
        import winsdk.windows.graphics.imaging as imaging
        import winsdk.windows.storage.streams as streams

        rgb = img.convert("RGB")
        # 深色主题自适应：背景暗（平均亮度低）→ 反色后 OCR（Windows OCR
        # 假设浅底深字，深色主题下识别率暴跌——实测整窗 0 行，反色后 13 行）。
        # 用 PIL ImageStat 快速估计亮度，避免额外依赖。
        try:
            from PIL import ImageStat
            stat = ImageStat.Stat(rgb)
            mean_lum = sum(stat.mean) / 3
            if mean_lum < 100:  # 暗背景（深色主题）
                from PIL import ImageOps
                rgb = ImageOps.invert(rgb)
        except Exception:
            pass  # 亮度估计失败时按原样 OCR
        buf = io.BytesIO()
        rgb.save(buf, format="PNG")
        data = buf.getvalue()

        stream = streams.InMemoryRandomAccessStream()
        writer = streams.DataWriter(stream)
        writer.write_bytes(data)
        await writer.store_async()
        stream.seek(0)
        decoder = await imaging.BitmapDecoder.create_async(stream)
        sbitmap = await decoder.get_software_bitmap_async()
        return await engine.recognize_async(sbitmap)

    try:
        result = asyncio.run(_run())
    except Exception as e:
        logger.warning(f"OCR 识别失败: {e}")
        return []

    items = []
    for line in result.lines:
        text = _norm_cjk(line.text)
        if not text:
            continue
        # 行内词合并成整行包围盒
        ws = line.words
        if ws:
            x = min(w.bounding_rect.x for w in ws)
            y = min(w.bounding_rect.y for w in ws)
            x2 = max(w.bounding_rect.x + w.bounding_rect.width for w in ws)
            y2 = max(w.bounding_rect.y + w.bounding_rect.height for w in ws)
        else:
            r = line.bounding_rect
            x, y, x2, y2 = r.x, r.y, r.x + r.width, r.y + r.height
        items.append({
            "text": text,
            "x": int(x), "y": int(y),
            "w": int(x2 - x), "h": int(y2 - y),
        })
    return items


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


# ---------- 后端实现 ----------

# 微信窗口布局（4.1.12.51 默认窗口，相对窗口客户区比例）
# 会话列表：左侧约 30% 宽；消息区：右侧约 70% 宽
# 注意：窗口位置/大小由用户用 tools/fix_window.py 固定，bot 不移动窗口、
# 不读窗口配置——坐标换算一律基于窗口当前实际 rect（见 _window_rect）。
_SESSION_REGION_RATIO = (0.0, 0.08, 0.32, 1.0)   # (l, t, r, b) 相对窗口
_MESSAGE_REGION_RATIO = (0.32, 0.08, 1.0, 1.0)

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
        if session is None or message is None:
            logger.warning(
                "wx_ocr_region.json 区域值非法（须 l<t? 实为 4 值 [0,1] 且 l<r、t<b），回退默认")
            return None
        logger.info(
            f"📐 已加载用户圈定区域：列表{session} 消息{message}")
        return {"session": session, "message": message}
    except Exception as e:
        logger.warning(f"wx_ocr_region.json 读取失败（{e}），回退默认区域")
        return None


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
        # 用户圈定区域（tools/pick_ocr_region.py 配置）；无配置回退模块默认常量
        cfg = _load_region_config()
        self._session_region = cfg["session"] if cfg else _SESSION_REGION_RATIO
        self._message_region = cfg["message"] if cfg else _MESSAGE_REGION_RATIO

    # ---- 协议：连接 ----

    def connect(self) -> bool:
        hwnd = find_wechat_window()
        if hwnd is None:
            raise BackendUnavailableError("未找到微信主窗口（请确认微信已登录并打开）")
        self._hwnd = hwnd
        # 注意：不移动窗口——窗口位置/大小是用户手动控制的资产（可用
        # tools/fix_window.py 固定）。坐标换算一律读窗口当前实际 rect，
        # 与 wx_window.json 配置解耦，避免 bot 连接时覆盖用户调整。
        shot = capture_window(hwnd)
        if shot is None:
            raise BackendUnavailableError("PrintWindow 截图失败")
        self._last_shot = shot
        logger.info(f"✅ 视觉后端已连接（窗口 0x{hwnd:x} {shot.size[0]}x{shot.size[1]}）")
        return True

    # ---- 协议：会话 ----

    def _refresh(self, force: bool = False) -> Image.Image | None:
        """截图并做区域变化检测；无变化且非 force 时返回 None。"""
        if self._hwnd is None:
            raise BackendUnavailableError("后端未连接")
        shot = capture_window(self._hwnd)
        if shot is None:
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
        session_x_max = w * (self._session_region[2] + 0.17)  # 列表区右边界+0.17容差（无配置时 0.32+0.17=0.49，保留原语义）

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

        # 3. 每聚类取主名：优先选不含方括号标签、不含消息预览前缀的行；
        #    清理 OCR 残留前缀字符（如 '艹王美晨' 的 '艹'）
        for cluster in clusters:
            cluster.sort(key=lambda it: it["y"])
            main_line = None
            for it in cluster:
                t = it["text"]
                if re.match(r"^[\s\[\]【】「」]*$", t):
                    continue  # 纯括号/空格标签
                # 消息预览行（'群聊 - 王文生：…' 或含 '：' 的预览）不作为主名
                if "：" in t or ":" in t:
                    continue
                # 纯标签行（'[ 视频 ]' 短文本）
                if re.match(r"^[\s\[\]]{0,2}[\u4e00-\u9fff]{1,4}[\s\[\]]{0,2}$", t) \
                        and len(t) <= 6 and ("[" in t or "]" in t):
                    continue
                main_line = it
                break
            if main_line is None:
                # 无主名（全是标签/预览）→ 跳过
                continue
            name = main_line["text"]
            # 清理 OCR 残留：
            # - 去首尾非内容字符（'艹王美晨' → '王美晨'；'艹' 是未读角标误读）
            # - 引号内空格（'" 强盗 " 集团' → '"强盗"集团'）
            name = re.sub(r"^[^\u4e00-\u9fffA-Za-z0-9]+", "", name)
            name = re.sub(r"\"\s+", "\"", name)
            name = re.sub(r"\s+\"", "\"", name)
            name = name.strip()
            if not name:
                continue
            cx = win_l + main_line["x"] + main_line["w"] // 2
            cy = win_t + main_line["y"] + main_line["h"] // 2
            self._session_coords[name] = (cx, cy)
        return self._session_coords

    def iter_unread_sessions(self) -> Iterator[str]:
        """仅迭代有未读红圈角标的会话（可选能力，wxauto 等后端可不提供）。

        效率路径（重构核心）：
        1. 截图（force=True——红圈检测走像素级 ~毫秒级，无需 diff 保护；
           不能用 force=False：region_changed 检测的是相对上一帧的变化，
           红圈已存在时两帧相同会误判"无变化"而漏读）
        2. 列表区红色角标簇检测（crop + PIL point 掩码，~毫秒级，无 OCR）
        3. 无红簇 → 直接结束（无未读时每轮只花截图+像素检测，零 OCR 零点击）
        4. 有红簇 → 整窗 OCR 提取会话名（crop OCR 实测读不出会话名，必须整窗）
           → 红簇与名字坐标匹配（用户描述：红圈在联系人名字左上角）
        5. yield 有未读的会话名（后续 get_messages 会点击切换该会话）

        匹配规则（实测校准）：红簇中心在某个会话名中心的左上邻近
        （|dy| < 60px 且红簇 x < 名字 x，避免跨会话误配）；未匹配的红簇记
        warning 日志（如 OCR 未读出名字的折叠条目），不阻断。
        """
        shot = self._refresh(force=True)
        if shot is None:
            return
        badges = _detect_red_clusters(shot, region=self._session_region)
        if not badges:
            return
        # 有红簇才整窗 OCR 提取会话名（复用 iter_sessions 逻辑）
        self._extract_session_names(shot)
        if not self._session_coords:
            return
        rect = wt.RECT()
        u32.GetWindowRect(self._hwnd, ctypes.byref(rect))
        win_l, win_t = rect.left, rect.top
        # 红簇中心 → 屏幕坐标；与会话名中心匹配
        seen: set[str] = set()
        for (bl, bt, br, bb) in badges:
            bcx = win_l + (bl + br) // 2
            bcy = win_t + (bt + bb) // 2
            best = None
            best_d = 1e9
            for name, (sx, sy) in self._session_coords.items():
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
                logger.debug(f"[未读] {best!r} 红圈屏幕 ({bcx},{bcy})")
                yield best
            elif best is not None:
                logger.debug(f"[未读] {best!r} 已有红圈（重复角标，跳过）")
            else:
                logger.warning(
                    f"[未读] 红圈屏幕 ({bcx},{bcy}) 未匹配到会话名（可能是 OCR 未读出的条目）")

    def _switch_chat(self, chat: str) -> bool:
        """点击会话列表中的目标会话切换聊天。返回是否已切换。"""
        if chat not in self._session_coords:
            # 坐标未知：先刷新会话列表
            list(self.iter_sessions())
        coord = self._session_coords.get(chat)
        if coord is None:
            logger.warning(f"[切换] 未找到会话 {chat!r} 的坐标")
            return False
        try:
            import pyautogui
            pyautogui.click(coord[0], coord[1])
            time.sleep(0.5)  # 等待消息区刷新
            return True
        except Exception as e:
            logger.warning(f"[切换] 点击会话失败: {e}")
            return False

    def get_messages(self, chat: str, limit: int | None = None) -> list[WeChatMessage]:
        """返回会话 chat 的消息（最近 limit 条）。消息区 OCR + 时间戳行切分。

        消息块判定：时间戳行（如 '昨天 18:45'、'20:14'）作为块分隔符；块内
        多行合并为一条消息。sender 按 x 坐标与消息区中线比较——右侧=自己
        （sender="self"，上层跳过），左侧=对方（sender="未知"）。
        """
        # 先切换到目标会话（visual 通道必须点击切换，无法像 wxauto4 ChatWith 直达）
        self._switch_chat(chat)
        shot = self._refresh(force=True)
        if shot is None:
            return []
        w, h = shot.size
        region = shot.crop((
            int(w * self._message_region[0]), int(h * self._message_region[1]),
            int(w * self._message_region[2]), int(h * self._message_region[3]),
        ))
        region = region.resize((region.width * 2, region.height * 2), Image.LANCZOS)
        items = ocr_image(region)
        if not items:
            return []
        # 按 y 排序 → 合并相邻行成消息块（时间戳行作为分隔）
        items.sort(key=lambda it: (it["y"], it["x"]))
        midline_x = region.width // 2  # 消息区中线：右侧=自己发的
        msgs: list[WeChatMessage] = []
        cur_lines: list[dict] = []
        cur_y: list[int] = []
        seq = 0

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
            """OCR 噪音：过短（<2 字符）或不可打印字符占比高。"""
            if not text:
                return True
            if len(text) < 2:
                return True
            # 全部是标点/符号/异常字符（无 CJK 无字母无数字）
            if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", text):
                return True
            return False

        def _flush():
            nonlocal seq
            if not cur_lines:
                return
            # 一条消息：多行文本合并
            text = " ".join(l["text"] for l in cur_lines).strip()
            text = re.sub(r"\s+", " ", text)
            if text and not _is_noise(text):
                # sender 判定：块内首行 x 相对中线（右侧=自己）
                first_x = cur_lines[0]["x"]
                sender = "self" if first_x > midline_x else "未知"
                seq += 1
                msgs.append(WeChatMessage(
                    id=f"visual_{seq}",
                    chat=chat,
                    sender=sender,
                    content=text,
                    type=MessageType.TEXT,
                ))
            cur_lines.clear()
            cur_y.clear()

        for it in items:
            # 时间戳行：作为分隔符（flush 前块），本身不入块
            if _is_timestamp(it["text"]):
                _flush()
                continue
            if _is_noise(it["text"]):
                continue
            if cur_y and abs(it["y"] - sum(cur_y) / len(cur_y)) > 18:
                _flush()
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
            pyautogui.click(cx, cy)
            time.sleep(0.3)
            pyautogui.typewrite(text, interval=0.01)
            time.sleep(0.2)
            pyautogui.press("enter")
            logger.info(f"🤖 → [{chat}]: {text[:50]}")
            return True
        except Exception as e:
            logger.error(f"发送失败: {e}")
            return False

    def _input_box_rect(self):
        """输入框位于消息区底部（约 85% 高度处，全宽）。返回屏幕坐标 (l,t,w,h)。"""
        if self._hwnd is None:
            return None
        rect = wt.RECT()
        u32.GetWindowRect(self._hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        l = rect.left + int(w * self._message_region[0]) + 20
        t = rect.top + int(h * 0.82)
        return (l, t, int(w * (self._message_region[2] - self._message_region[0])) - 40, int(h * 0.12))

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
