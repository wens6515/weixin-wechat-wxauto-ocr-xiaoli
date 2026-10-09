# -*- coding: utf-8 -*-
"""visual_backend：基于 PrintWindow 截图 + 本地 OCR 的微信视觉后端。

通道背景（见 docs\\微信通道验证结论.md，2026-08 实测）：
- 新版微信 4.1.12.51 的 UIA 控件树 / CDP / 窗口消息 / 本地数据通道全部关闭
- PrintWindow + PW_RENDERFULLCONTENT 截图是唯一已验证可行的读取通道
- 文本识别用本地 RapidOCR（PP-OCRv5 mobile 模型，onnxruntime 后端，零多模态 API）
- 变化检测先行：无像素变化直接跳过，避免每轮 OCR 成本

效率设计（用户诉求"像 wxauto4 那样高效"）：
- 读取：截图（~5ms）→ 区域像素 diff（毫秒级）→ 有变化才 OCR（~50ms）
- 文本消息走本地 OCR；仅图片消息才由上层调多模态 API 描述
- 发送：定位输入框坐标 → 点击聚焦 → 键盘输入 → Enter

本实现是 wx_backend 注册表中的唯一后端（visual），auto 模式直接选中。

拆分布局（纯移动，零逻辑改动）：
- visual_win32.py   Win32 通道层（DPI 感知 / 窗口查找定位 / PrintWindow 截图）
- visual_vision.py  视觉原语层（气泡色 / 连通域 / 媒体框 / 头像 / 文件卡片图标）
- visual_badge.py   未读红圈角标检测
- visual_regions.py OCR 三区域标定 + parse_title
- voice_channel.py  VB-CABLE 语音发送通道
- 本文件（门面）    OCR 引擎 + analyze_blocks + VisualBackend。被 mock.patch
                    的名字（ocr_image / capture_window / detect_* / find_* 等）
                    的消费方都在本模块，patch 打在门面命名空间语义不变；
                    各子模块公开名在此显式 re-export，既有导入路径不变。
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import sys
import time
from typing import Any, Iterator

from PIL import Image

from . import BackendUnavailableError
from .models import MessageType, WeChatMessage
from .visual_win32 import (
    capture_window, default_right_half_rect, dwm, ensure_window_visible,
    find_wechat_window, find_window_by_title, position_window,
    position_window_visible, resize_window_visible, u32,
    visible_frame_margins, visible_window_size, window_rect, wt,
)
from .visual_vision import (
    _anchor_avatar, _bucket_avatar, _connected_boxes, _contains,
    _near_color, drop_panel_contents, detect_avatar_tops,
    detect_bubble_colors, estimate_theme, find_bubble_boxes, find_media_boxes,
    find_panel_icon, filter_media_boxes, region_changed,
)
from .visual_badge import (
    _BADGE_ANCHOR_MAX_DIST, _BADGE_CLICK_OFFSET_X, _BADGE_ROW_TOL,
    _color_close, _detect_red_clusters, _pick_block_near_badge,
)
from .visual_regions import (
    _MESSAGE_REGION_RATIO, _SESSION_REGION_RATIO,
    _TITLE_REGION_RATIO, WINDOW_SIZE, parse_title,
)
from .voice_channel import (
    _VoiceChannel, _capsule_green_count, _get_voice_channel,
    _pick_cable_endpoints, alt_down, alt_up, _send_pill_visible,
)

logger = logging.getLogger(__name__)

# 界面主题：两套主题的像素判据分叉点（visual_vision 里按 colors["theme"] 选）。
_THEME_LABELS = {"light": "浅色", "dark": "深色"}
# 选中行高亮色（会话条目背景）两套主题的真机实测值：深色 (13,168,105)、
# 浅色 (21,172,112)（后者与深色默认值各通道差 ≤8，容差 12 内本就判得中，
# 这里按主题给初值是为了首帧就准，不依赖容差兜）。
_SELECTED_ROW_COLOR = {"dark": (13, 168, 105), "light": (21, 172, 112)}

# 强制窗口尺寸的三个闸（详见 VisualBackend.enforce_window_size）：
# 容差管「多大算不一致」，复查间隔管「多久查一次」，静置管「刚调完等多久
# 再查」——调完立刻复查会读到微信重绘前的过渡尺寸，白调一次。
_SIZE_TOL = 4              # 像素：渲染取整/贴边吸附不触发调整
_SIZE_CHECK_INTERVAL = 1.0  # 秒：复查节流（0.5s 轮询下约每秒一次）
_SIZE_SETTLE = 1.5          # 秒：调整后静置期

# 三区域一律用模块内置常量（visual_regions 的真机标定值）。
# 用户自定义画面标定（wx_ocr_region.json）已撤回：那套几何判据（头像窄带 =
# 框宽 2%~14%、期望头像高 = 框高÷18、媒体/气泡闸全是框的比例）只在"框 ==
# 聊天区"这一前提下成立，而微信布局不是等比的（会话列表固定像素宽、输入框
# 固定像素高），用户手画的大/小框会让这些常量整体漂移——真机事故：自设区域
# 后对方头像整列检不出（消息读不到）、自己侧漏检（自家图片被当对方消息）。
# 撤回后窗口尺寸/布局的适配责任回到程序侧：微信窗口按默认大小使用即可。



# ---------- OCR（RapidOCR：PP-OCRv5 mobile 模型，onnxruntime 后端） ----------

_OCR_ENGINE = None


def _get_ocr_engine():
    """懒加载 RapidOCR 引擎（rapidocr 3.x + PP-OCRv5 mobile，onnxruntime 后端）。
    不可用返回 None。

    引擎选型（真机 + 合成双验证，见 docs/OCR引擎升级评估.md）：
    - 模型锁 PP-OCRv5 mobile：热路径耗时与旧 PP-OCRv4 持平（消息区联合 OCR
      627ms vs 601ms），小字/生僻字/符号精度更高（下划线文件名、弯引号、
      群成员生僻字读对，头像幻影行消失）；默认的 PP-OCRv6 small 慢
      52%~3 倍，不用
    - rapidocr 3.x 构造参数是枚举（传字符串会被参数校验拒绝）；模型文件由
      打包预置进 rapidocr/models/（源码运行缺模型时按 registry 自动下载）
    - 替换更早的 winsdk Windows OCR：对微信 UI 噪声致命且随内容漂移
      （人名被整字误读、[图片]→隆片]），RapidOCR 同一张整窗截图全部读对
    """
    global _OCR_ENGINE
    if _OCR_ENGINE is not None:
        return _OCR_ENGINE
    try:
        from rapidocr import RapidOCR
        from rapidocr.utils.typings import ModelType, OCRVersion

        # intra_op_num_threads=2：限制 ONNX Runtime 推理线程数，避免 OCR
        # 全核打满（真机实测：小漓.exe 发现新消息时 CPU 100% 根因即此）。
        # rapidocr 3.x 默认 -1 吃满核，必须显式限 2；单次推理耗时 +5.6%
        # 换 CPU 从全核降到 2 核（tools/ocr_benchmark.py 真机实测）。
        #
        # Det.limit_side_len=224：v5 det 对默认「短边放大到 736」的预处理
        # 敏感——标题区裁剪（517x70）被放大 10.5 倍后整块检不出文本（真机
        # 复现，v4/v6 无此问题）。224 把放大倍率上限压到 ~3 倍：标题区
        # conf 1.0 恢复读取，条带主名行合并更完整；消息区/整窗（min 边
        # ≥736）本就不放大，参数对热路径零影响（分区实测见评估文档）。
        _OCR_ENGINE = RapidOCR(params={
            "Det.ocr_version": OCRVersion.PPOCRV5,
            "Det.model_type": ModelType.MOBILE,
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.model_type": ModelType.MOBILE,
            "Det.limit_side_len": 224,
            "Det.limit_type": "min",
            "EngineConfig.onnxruntime.intra_op_num_threads": 2,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        })
        return _OCR_ENGINE
    except Exception as e:
        logger.warning(f"RapidOCR 不可用: {e}")
        _OCR_ENGINE = False
        return None


def _norm_cjk(text: str) -> str:
    """清理 OCR 输出：中文单字间被 OCR 插入的空格去掉（'林 小 满'→'林小满'），
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
        out = engine(np.array(img.convert("RGB")))
        # rapidocr 3.x 返回 RapidOCROutput（.txts/.scores/.boxes）；boxes 是
        # numpy 数组，禁真值判断（`or` 会 raise），逐字段 None 检查。
        txts = out.txts if out.txts is not None else []
        scores = out.scores if out.scores is not None else []
        boxes = out.boxes if out.boxes is not None else []
        items = []
        for box, text, _score in zip(boxes, txts, scores):
            text = _norm_cjk(str(text).strip())
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
    except Exception as e:
        logger.warning(f"OCR 识别失败: {e}")
        return []

# ---------- 消息块分析（组合层：归属只用头像，用户定案） ----------

def analyze_blocks(img: Image.Image, colors: dict, bot_tops: list[int],
                   other_tops: list[int], skip_bot: int = 0) -> dict:
    """头像锚定的消息块分析（纯像素、无 OCR）：一条消息 = 一个头像。

    用户定案的流程（视觉层唯一入口，analyze_window 与 get_messages 共用）：
    1. 先确认 bot 最后一条消息 = 右侧头像 y 最大者（skip_bot 再往前数 N 条，
       占位回复语义保留）；2. 分析区上沿 = 该消息之后的下一条**对方**头像
       上边界，只有该上沿以下才是本轮对方新内容；3. 逐对方头像锚定一条
       消息，块 = 归属该头像的面板/媒体框并集，类型 text / image / file。
    归属只用头像（_anchor_avatar），气泡颜色只用于「找面板/媒体框」这类
    结构判定，不再参与 self/对方 判定；头像检测不到 = 该侧无新消息，
    不做任何颜色/中线降级。

    返回 dict：
      bot_last_top   int|None   bot 最后一条消息的头像 top
      region_top     int|None   分析区上沿（= 对方新消息第一条的上边框）
      other_new_tops [int,...]  本轮对方新消息的头像 top（升序）
      all_tops       [int,...]  全部头像 top（升序）
      blocks         [dict,...] 每个对方头像一条：{avatar_top, top, bottom,
                                left, right, kind, panel, media, icon}
      has_text       bool       含文字消息块
      has_media      bool       含多媒体块（图片或文件卡片）
    """
    import numpy as np
    arr = np.asarray(img.convert("RGB"), dtype=np.int16)
    rh, rw = img.height, img.width
    all_tops = sorted(set(bot_tops) | set(other_tops))
    last_bot_top = None
    if bot_tops:
        skip = min(skip_bot, len(bot_tops) - 1)
        last_bot_top = sorted(bot_tops)[-(1 + skip)]
    other_new_tops = sorted(t for t in other_tops
                            if last_bot_top is None or t > last_bot_top)
    region_top = other_new_tops[0] if other_new_tops else None
    bubbles = find_bubble_boxes(img, colors)
    panels_all = [(t, b, l, r) for (t, b, l, r, is_self) in bubbles if not is_self]
    media_raw = find_media_boxes(img, colors)
    # 面板 ↔ 媒体框互含剔除（两个方向都要）：
    # - 媒体框在面板内 = 文件卡片的类型图标，不是图片（点它会打开用户文件）
    # - 面板在媒体框内 = 图片内容里与气泡色相近的色块（真机 fixture：图片块
    #   838~1117 内被判出「气泡框」869~928），不是消息
    panels = [p for p in panels_all
              if not any(_contains(m, p) for m in media_raw)]
    media = drop_panel_contents(media_raw, panels_all)
    icons = {}
    for p in panels:
        ic = find_panel_icon(arr, p, colors)
        if ic is not None:
            icons[p] = ic
    blocks = []
    for T in other_new_tops:
        anchored = [p for p in panels if _anchor_avatar(p[0], all_tops) == T]
        anchored_media = [m for m in media if _anchor_avatar(m[0], all_tops) == T]
        nxt = [t for t in all_tops if t > T]
        cap = (min(nxt) - 1) if nxt else (rh - 1)
        boxes = anchored + anchored_media
        panel = anchored[0] if anchored else None
        if boxes:
            bottom = min(max(b[1] for b in boxes), cap)
            left = min(b[2] for b in boxes)
            right = max(b[3] for b in boxes)
        else:
            # 头像在、框没检到（气泡色漂移等）：块范围退到下一个头像为止，
            # 类型按文字处理，保证该头像新消息的文字仍被读走；不做归属降级。
            bottom, left, right = cap, 0, rw
        if anchored_media:
            kind = "image"
        elif panel is not None and icons.get(panel) is not None:
            kind = "file"
        else:
            kind = "text"
        blocks.append({
            "avatar_top": T, "top": min(T, boxes[0][0]) if boxes else T,
            "bottom": bottom, "left": left, "right": right, "kind": kind,
            "panel": panel, "media": (anchored_media[0] if anchored_media else None),
            "icon": (icons.get(panel) if panel is not None else None),
        })
    return {
        "bot_last_top": last_bot_top,
        "region_top": region_top,
        "other_new_tops": other_new_tops,
        "all_tops": all_tops,
        "blocks": blocks,
        "has_text": any(b["kind"] == "text" for b in blocks),
        "has_media": any(b["kind"] in ("image", "file") for b in blocks),
    }

# 会话名匹配键：引号变体（全半角/弯直）与空白类字符一律剥掉。
# 与 xiaoli_app.memory_store.memory_key 同口径——但那在上层包，这里内联一份
# 避免底层（wx_backend）反向依赖上层（分层纪律）。
_KEY_QUOTE_CHARS = (
    "\u201c\u201d\u2018\u2019\u201e\u201f"
    "\u00ab\u00bb\u2039\u203a"
    "\u300c\u300d\u300e\u300f"
    "\uff02\u02bc\u0060\u00b4\"'"
)


def _norm_chat_key(name: str) -> str:
    """会话名归一化：剥引号变体与空白，供「按名找会话」匹配用。

    OCR 对引号半/全角极不稳（真机：「“摸鱼”集团」会读出「摸鱼”集团」
    「"摸鱼"集团」「“ 摸鱼 ” 集团」等变体），原样比较会找不到目标会话。
    """
    s = str(name or "").translate(str.maketrans("", "", _KEY_QUOTE_CHARS))
    return re.sub(r"\s+", "", s)


def _same_chat_name(a: str, b: str) -> bool:
    """两个会话名是否同一会话（容忍 OCR 变体与漏字）。

    归一化后相等，或一方是另一方的前缀。真机 OCR 会把「“摸鱼”集团」读成
    「摸鱼"集团」「摸鱼”」等残缺形态；严格相等会把"同一会话"判成"不同会话"，
    于是对已选中的条目白点一下 → toggle 取消选中 → 消息区变空。
    """
    ka, kb = _norm_chat_key(a), _norm_chat_key(b)
    if not ka or not kb:
        return False
    return ka == kb or ka.startswith(kb) or kb.startswith(ka)

# ---------- 后端实现 ----------

class VisualBackend:
    """PrintWindow + 本地 OCR 的微信视觉后端。

    实现 wx_backend.WeChatBackend 协议。所有读操作：截图 → 区域 diff →
    有变化才 OCR。发送：坐标点击聚焦 + 键盘输入 + Enter。

    双主题：微信浅色/深色两套界面的像素判据不同（气泡与背景的明暗关系相反、
    色差量级也不同），初始化时识别一次主题并落 `_theme`，运行时每次分析由
    `detect_bubble_colors` 回带本帧主题、变化即跟随（用户在设置里切主题不用
    重启程序）。判据分叉点全在 visual_vision 的 `colors["theme"]` 上。
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
        # 界面主题：None = 尚未识别（connect 时按会话列表区∪消息区判定）。
        # 未知期间一律按深色判据（历史行为，且深色判据在浅色下只是退化不会崩）。
        self._theme: str | None = None
        # 选中行高亮色：命中该色 = 微信已选中该会话，调用方不点击防 toggle
        # 取消选中——像素判定零 OCR，事件热路径第一道闸。默认值按主题取真机
        # 实测色（深色 (13,168,105) / 浅色 (21,172,112)）；点击成功后
        # _learn_selected_row_color 会重采样覆盖，主题切换时回到该主题初值。
        self._selected_row_color: tuple[int, int, int] = _SELECTED_ROW_COLOR["dark"]
        # 强制窗口尺寸的复查节流时间戳（见 enforce_window_size）
        self._size_check_at: float = 0.0
        # 三区域恒为模块内置常量（用户自定义标定已撤回，见文件头注释）
        self._apply_regions()

    def _apply_regions(self) -> None:
        """把内置默认区域落到三个运行时区域属性。"""
        self._session_region = _SESSION_REGION_RATIO
        self._message_region = _MESSAGE_REGION_RATIO
        self._title_region = _TITLE_REGION_RATIO

    # ---- 强制窗口尺寸（用户自定义尺寸/区域已撤回，改为程序强制）----

    def enforce_window_size(self, force: bool = False, now: float | None = None) -> bool:
        """把微信窗口强制回标定尺寸 `WINDOW_SIZE`（只改大小，不动位置）。

        为什么强制：三区域比例与全部像素判据（头像窄带 = 区域宽 2%~14%、期望
        头像高 = 区域高÷18、气泡/媒体闸同样是区域比例）都按那台 1300x1610 的
        窗口标定——窗口尺寸一变这些常量整体错位（真机事故：窗口拉大后聊天区
        左边界从 0.415 挪到 0.246，消息区左沿切进聊天区 → 整列头像检不出、
        消息读不到）。用户侧已无尺寸/区域设置，改为程序侧保证：

        - `connect()` 里 `force=True` 调一次（初始化即对齐）；
        - 消息监听每轮（`iter_unread_sessions`）与每次截图分析入口
          （`analyze_window` / `get_messages`）复查一次：发现被改动就调回。

        节流：复查间隔 `_SIZE_CHECK_INTERVAL`，调整后静置 `_SIZE_SETTLE`
        再复查（等微信重绘，避免和用户拖动打架打成一串调整）。容差
        `_SIZE_TOL` 像素（渲染取整/贴边吸附不折腾）。最大化窗口先 SW_RESTORE
        再改大小（SetWindowPos 对最大化窗口行为不可靠）。返回是否发生了调整。
        """
        # getattr 兜底：测试常用 VisualBackend.__new__ 直构桩（不走 __init__），
        # 本方法挂在监听/分析热路径上，不能因为桩缺属性就抛异常
        if getattr(self, "_hwnd", None) is None or getattr(self, "_closed", False):
            return False
        t = time.monotonic() if now is None else now
        if not force and t < getattr(self, "_size_check_at", 0.0):
            return False
        self._size_check_at = t + _SIZE_CHECK_INTERVAL
        rect = window_rect(self._hwnd)
        if not rect:
            return False
        want_w, want_h = WINDOW_SIZE
        if abs(rect[2] - want_w) <= _SIZE_TOL and abs(rect[3] - want_h) <= _SIZE_TOL:
            return False
        try:
            if u32.IsZoomed(self._hwnd):
                u32.ShowWindow(self._hwnd, 9)  # SW_RESTORE
                time.sleep(0.3)
        except Exception:
            pass
        ok = position_window(self._hwnd, rect[0], rect[1], want_w, want_h)
        if not ok:
            logger.warning("[窗口] 强制窗口尺寸失败（句柄异常？），下一轮重试")
            return False
        logger.info(f"[窗口] 微信窗口已强制为标定尺寸 {want_w}x{want_h}"
                    f"（原 {rect[2]}x{rect[3]}，位置未改动）")
        # 尺寸一变，上一帧截图（及其坐标系）作废：丢弃，免得拿旧尺寸的帧算坐标
        self._last_shot = None
        self._size_check_at = t + _SIZE_SETTLE
        return True

    # ---- 界面主题（浅色 / 深色）----

    @property
    def theme(self) -> str | None:
        """当前生效的界面主题："light" / "dark"；尚未识别时为 None。"""
        return self._theme

    def detect_theme(self, shot: Image.Image | None = None) -> str | None:
        """识别微信界面主题（"light"/"dark"），截图失败返回 None。

        取会话列表区 ∪ 消息区的像素中位亮度判定。**不喂整窗**：微信 4.x 的
        窗口顶栏两套主题下都是深色（跟系统主题走），整窗中位会被它带偏；也不
        只看消息区：窗口刚打开时消息区可能空着。两区判定不一致时以会话列表区
        为准（列表任何时候都有内容，消息区可能被一张大图占满）。
        """
        if shot is None:
            shot = self._refresh(force=True, foreground=False)
        if shot is None:
            return None
        w, h = shot.size
        themes = []
        for (rl, rt, rr, rb) in (self._session_region, self._message_region):
            crop = shot.crop((int(w * rl), int(h * rt), int(w * rr), int(h * rb)))
            themes.append(estimate_theme(crop))
        return themes[0] if themes[0] == themes[1] else themes[0]

    def _note_theme(self, theme: str | None) -> None:
        """记录本帧主题；变化时套用主题相关默认值并记日志（主题未知忽略）。"""
        if theme not in ("light", "dark") or theme == self._theme:
            return
        prev = self._theme
        self._theme = theme
        self._selected_row_color = _SELECTED_ROW_COLOR[theme]
        if prev is not None:
            logger.info(f"🎨 微信界面主题切换：{_THEME_LABELS[prev]} → "
                        f"{_THEME_LABELS[theme]}（像素判据已跟随）")

    # ---- 协议：连接 ----

    def connect(self) -> bool:
        hwnd = find_wechat_window()
        if hwnd is None:
            raise BackendUnavailableError("未找到微信主窗口（请确认微信已登录并打开）")
        self._hwnd = hwnd
        # 窗口尺寸：程序既不移动位置，也不再交给用户设置——统一强制为标定
        # 尺寸（WINDOW_SIZE，三区域与像素判据都按它标定）。这里先对齐一次，
        # 之后每轮消息监听与每次分析入口复查（enforce_window_size）。
        self._ensure_not_iconic()
        self.enforce_window_size(force=True)
        shot = capture_window(hwnd)
        if shot is None:
            raise BackendUnavailableError("PrintWindow 截图失败")
        self._last_shot = shot
        theme = self.detect_theme(shot)
        self._note_theme(theme)
        if theme:
            logger.info(f"🎨 识别到微信界面：{_THEME_LABELS[theme]}模式"
                        f"（气泡/媒体判据按该主题工作）")
        else:
            logger.warning("🎨 未识别到微信界面主题（截图区域为空），暂按深色判据工作")
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
        '摸鱼"集团(5)'（括号内人数）。返回标题原文；区域为空/失败返回 None。
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
        # 1x 原生 OCR（去 2x）：新引擎小字 1x 精度与 2x 持平（真机 conf 1.0），
        # 省一次 LANCZOS 放大和一半 OCR 耗时（820ms → ~110ms）
        items = ocr_image(region)
        # 标题可能被 OCR 拆成多段（真机：'"摸鱼"' + '集团(5)' 两个独立项），
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
        真机出现过整字误读，如人名被读成完全无关的另外三字）。整窗 OCR 中每个会话项可能拆成多行——
        主名（'林小满'）+ 标签行（'[ 视频 ]'）+ 消息预览（'群聊-林小满：…'）。
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
            # 只清引号内侧的 OCR 空格，**不剥首尾符号**——名字开头的引号/
            # emoji 属于名字本身（历史缺陷：剥首把 OCR 读对的「“摸鱼”集团」
            # 削成「摸鱼”集团」，再经键归一化成「摸鱼集团」，用户在记忆页
            # 看到的是残缺名；真机日志 303 次「摸鱼”」即此因）。
            name = t.strip()
            name = re.sub(r"\"\s+", "\"", name)
            name = re.sub(r"\s+\"", "\"", name)
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

    def iter_unread_sessions(self) -> Iterator[tuple[int, int]]:
        """仅迭代有未读红圈角标的会话（可选能力，wxauto 等后端可不提供）。

        **全程零 OCR**（用户定案：会话名在切过去之后，跟消息内容一起读）：
        1. 最小化哨兵 + 后台静默截图 → 红圈像素检测（毫秒级）
        2. 逐个红圈（按屏幕 y 自上而下）：
           a. `_is_row_selected(红圈 y)` 命中 → 该红圈属于当前已选中会话 →
              **不点击**（微信列表是 toggle：点已选中条目会取消选中、消息区变空）；
           b. 未命中 → `_click_badge_row()` 按红圈几何点击切过去（y + 45px
              横向偏移，真机标定），再用**像素复验**该行已选中；复验不过 =
              点击落空 → **不产出**（fail-closed：宁可不处理，也不认错会话）。
        3. 产出**位置条目** `(红圈中心 x, 红圈中心 y)`（屏幕坐标）。会话名不在
           这里取——`_handle_unread_session` 切过去后由 `get_messages` 的**联合
           OCR**（标题带 + 消息区一次读）给出权威名并刷新 `_current_title`。

        为什么不在迭代期读名字：列表名只是锚点、下游本来就用标题区覆盖它，
        而在迭代期读标题会白白多一次 OCR——一次处理事件本该只有联合那一次。
        真机依据：.rivet/scratch/probe_geo_judge.py（要不要切）、probe_geo_click.py
        （点击落点）、measure_ocr_budget.py（OCR 预算对比）。
        """
        if self._ensure_not_iconic():
            return  # 最小化态截图是占位垃圾（真机 276x45），恢复后下一轮再扫
        # 强制窗口尺寸：监听每轮复查一次（用户拖动/最大化/吸附都会在此被拉回，
        # 因为三区域与像素判据都按 WINDOW_SIZE 标定）
        self.enforce_window_size()
        shot = self._refresh(force=True, foreground=False)  # 红圈轮询：后台静默截图，不置前打断用户
        if shot is None:
            return
        badges = _detect_red_clusters(shot, region=self._session_region)
        if not badges:
            return
        rect = wt.RECT()
        u32.GetWindowRect(self._hwnd, ctypes.byref(rect))
        win_l, win_t = rect.left, rect.top
        seen_rows: list[int] = []
        # 自上而下处理：与列表视觉顺序一致，也让「每轮只处理一个」的推进稳定
        for (bl, bt, br, bb) in sorted(badges, key=lambda b: (b[1], b[0])):
            bcx = win_l + (bl + br) // 2
            bcy = win_t + (bt + bb) // 2
            if any(abs(bcy - y) < _BADGE_ROW_TOL for y in seen_rows):
                continue  # 同一行的重复红圈簇：按行归并（不需要名字就能去重）
            seen_rows.append(bcy)
            if not self._is_row_selected(bcy):
                if not self._click_badge_row(bcx, bcy):
                    continue  # 点击后选中未转移 → 放弃该条目
            logger.debug(f"[未读] 红圈屏幕 ({bcx},{bcy}) → 条目（名字稍后由标题区给出）")
            yield (bcx, bcy)

    def _click_badge_row(self, bcx: int, bcy: int) -> bool:
        """点击红圈所在条目行，并**用像素复验选中已转移**（全程零 OCR）。

        红圈在头像左上角、条目主体在其右侧约 45px（`_BADGE_CLICK_OFFSET_X`
        真机标定）。复验依据与 `_is_row_selected` 同一套——微信用背景高亮标出
        选中条目。复验不过 = 点击落空 → 返回 False，调用方放弃该条目。
        """
        try:
            import pyautogui
            self._foreground()  # 点击依赖前台，先置前微信窗口
            pyautogui.click(bcx + _BADGE_CLICK_OFFSET_X, bcy)
            time.sleep(0.6)     # 等列表选中态与消息区刷新
        except Exception as e:
            logger.warning(f"[未读] 红圈 ({bcx},{bcy}) 点击失败: {e}")
            return False
        if self._selected_row_color is None:
            logger.debug("[未读] 无可比对的选中高亮色，信任本次点击")
            return True
        if not self._is_row_selected(bcy):
            logger.warning(
                f"[未读] 红圈 ({bcx},{bcy}) 点击后选中未转移——放弃该条目，防认错会话")
            return False
        return True

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

    def resolve_chat_coord(self, chat: str) -> tuple[int, int] | None:
        """按会话名现读列表区 OCR，返回该会话的屏幕点击坐标；找不到返回 None。

        供「触发式发送」路径使用（定时/条件到点后要切到指定会话）：目标
        会话此刻通常**没有红圈**（消息早已读过），既无法靠红圈锚定，也可能
        不在 _session_coords 缓存里（只有走过的路径才填）。这里现截一帧、
        读列表区 OCR，按会话名归一化匹配（含双向子串兜底，容忍 OCR 残缺），
        命中取该块中心。

        为何不用整窗 OCR（iter_sessions）：整窗 ~1.2s，列表区 crop ~0.46s，
        且列表区本就不含搜索框/消息区，语义更干净（真机实测见
        .rivet/scratch/bench_ocr.py）。
        """
        if self._hwnd is None:
            return None
        shot = self._refresh(force=True, foreground=False)
        if shot is None:
            return None
        w, h = shot.size
        sr = self._session_region
        sl, st = int(w * sr[0]), int(h * sr[1])
        srr, sbb = int(w * sr[2]), int(h * sr[3])
        blocks = ocr_image(shot.crop((sl, st, srr, sbb)))
        if not blocks:
            return None
        target = _norm_chat_key(chat)
        if not target:
            return None
        hit = None
        for it in blocks:
            key = _norm_chat_key(it.get("text") or "")
            if not key:
                continue
            # 双向子串：OCR 漏字（「摸鱼”」vs「“摸鱼”集团」）也算命中
            if key == target or key.startswith(target) or target.startswith(key):
                hit = it
                break
        if hit is None:
            logger.debug(f"[定位] 列表区未找到会话 {chat!r}")
            return None
        rect = wt.RECT()
        u32.GetWindowRect(self._hwnd, ctypes.byref(rect))
        return (rect.left + sl + hit["x"] + hit["w"] // 2,
                rect.top + st + hit["y"] + hit["h"] // 2)

    def _switch_chat(self, chat: str, force: bool = False) -> bool:
        """点击会话列表中的目标会话切换聊天。返回是否已切换。

        微信会话列表是 toggle 行为：点已选中的会话会取消选中、右侧消息区
        变空。因此已选中目标会话时直接返回，不重复点击（用户实测：再点一次
        就取消选中了）。force=True 强制点击，绕过已选中判断——用于
        get_messages 读到 0 条时的 toggle 兜底重试。

        已选中判定优先级（force=False）：
        1. UI 标题区 OCR（read_title）——**权威信号**：标题显示哪个会话，
           微信就停在哪个会话。标题是目标即已选中（不点击）；标题是**别的**
           会话即"不在目标"，此时不再看低优先级信号，直接走切换。
        2. 标题读不到时（被遮挡/OCR 空），才退回：选中高亮像素
           （_is_row_selected）→ 内存 _current_chat。
           低优先级信号**不得覆盖标题的否定证据**：_current_chat 只在点击
           成功时更新，用户手动切换微信不会通知本进程，拿它当依据会把
           "窗口停在别的会话"误判成"已在目标会话"（真机缺陷：小漓在林小满
           回复完、用户手动切到别的群，林小满再来消息时不切换、读到的是别的
           会话的消息）。

        坐标来源优先级：_session_coords 缓存（整窗 OCR 兼容通道填充，热路径
        不再主动重建）→ resolve_chat_coord 现读列表区 OCR（触发式发送主
        路径）。点击成功后若标题非空（微信确认选中），采样该条目行背景色
        自学习缓存选中高亮（_learn_selected_row_color）。
        """
        if not force:
            # 1) 标题区 OCR = 权威信号：它读到什么，微信就停在什么会话上。
            #    （后台静默截图，不置前打断用户）
            title = None
            try:
                title = self.read_title(foreground=False)
            except Exception:
                pass
            if title:
                if _same_chat_name(parse_title(title)[0], chat):
                    self._current_chat = chat
                    return True
                # 标题明确显示的是**别的**会话 → 当前不在目标会话，继续往下
                # 走切换。**不得**在这里退回 _current_chat：它只在点击成功时
                # 更新，用户手动切换微信不会通知本进程（真机缺陷：小漓在
                # 林小满回复完，用户手动切到「“摸鱼”集团」，林小满再来新消息
                # 时被判为"已在目标会话"→ 不点击 → 读到的是摸鱼集团的消息；
                # 真机探针实测标题='“摸鱼”集团(5)' 而 _current_chat 仍='林小满'）。
            else:
                # 2) 标题读不到（窗口被遮挡 / OCR 空）→ 才退回像素高亮与内存
                #    状态，用来避免"重复点已选中条目 → toggle 取消选中"。
                try:
                    item_y = None
                    if chat in self._session_coords:
                        item_y = self._session_coords[chat][1]
                    if item_y is not None and self._is_row_selected(item_y):
                        self._current_chat = chat
                        return True
                except Exception:
                    pass
                if self._current_chat == chat:
                    return True  # 已选中（标题与像素都读不到时的最后兜底）
        coord = self._session_coords.get(chat)
        if coord is None:
            # 现读列表区 OCR 定位（触发式发送主路径）：目标会话没有红圈
            # （消息早已读过），整窗缓存通常没有它。列表区 crop (~0.46s) 比
            # 整窗 iter_sessions (~1.2s) 快 2.6 倍，语义也更干净（不含搜索
            # 框/消息区）——旧实现先跑一次整窗重建再落到这里，慢路径白花
            # 1.2s，已移除（iter_sessions 保留作后端协议兼容面）。
            coord = self.resolve_chat_coord(chat)
        if coord is None:
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
                if title and _same_chat_name(parsed, chat):
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

    def get_messages(self, chat: str | None, limit: int | None = None,
                     assume_switched: bool = False,
                     skip_bot: int = 0) -> list[WeChatMessage]:
        """返回会话 chat **本轮对方新消息**（最近 limit 条）。消息区 OCR + 头像锚定合并。

        用户定案的分块规则（与 analyze_window 共用 analyze_blocks）：
        - 归属只用头像：一条消息 = 一个头像，消息文字范围 = [该消息头像上
          边界, 该消息块下边界]；块外文字（时间/日期分隔行、bot 自己的消息、
          更早的历史消息）一律不读；
        - 头像区域内的 OCR 文字按几何剔除（头像窄带 x ∩ 头像竖直区间）——
          头像图片上的字（真机幻影行「用户已无生命体征」）不是消息内容；
        - 块类型 image：块内文字全部丢弃（图片上的字不是消息）；块类型
          file：文字保留（文件名是文件流程唯一凭据），消息 type=FILE 供上层
          直接判文件，不再依赖 OCR 扩展名正则。
        sender：私聊 = 会话名；群聊 = 面板（气泡）外、气泡上方的短文本行
        （发送者名）；取不到就退回会话名。skip_bot 与 analyze_window 同源
        （占位回复剔除），必须传同一个值，否则分析区上沿会错位。

        assume_switched：同一处理事件里 analyze_window 刚完成「切换 + 读
        标题」时置 True——跳过重切与标题重读（省一次点击、两次 OCR，是
        事件热路径提速的核心）。标题/群聊标记直接用缓存；消息区读空时
        仍会走 force 重切兜底（toggle 取消选中防线保留）。
        """
        # 强制窗口尺寸：分析前复查（处理事件途中被改尺寸 → 本轮就拉回，
        # 免得按旧尺寸算出的坐标写进记忆/发送）
        self.enforce_window_size()
        if assume_switched and (chat is None or self._current_chat == chat):
            # 诊断行降 DEBUG：每个含文件/媒体的事件跑两遍读取管线（10s 防抖），
            # INFO 级会刷爆前端日志（bot_run.log）；排障看 bot.log 全量
            logger.debug(f"[读取] {chat!r} 标题={self._current_title!r}（复用切换结果）")
        else:
            if chat is None:
                logger.warning("[读取] 位置模式下未走联合路径（assume_switched=False），"
                               "无法确定会话 → 放弃本次读取")
                return []
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
            logger.debug(f"[读取] {chat!r} 标题={title!r}")
            if title:
                name, is_group, _ = parse_title(title)
                self._current_title = name or chat
                self._current_is_group = is_group
        region = None
        items = []
        info: dict = {}
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
            # 1x 原生 OCR（去 2x）：新引擎小字 1x 精度与 2x 持平（真机逐行
            # 对比无内容丢失，省略号/头像噪声反而更好），消息区联合 OCR
            # 1579ms → 631ms。坐标口径全线 1x，几何阈值线性减半行为不变。
            region = region_1x
            if assume_switched and (chat is None or self._current_chat == chat):
                # ---- 联合裁剪 OCR（用户定案：事件内 OCR 一次）----
                # 标题带 + 消息区一次读：联合区 = 标题区 ∪ 消息区，下边框
                # 钉在标定消息区下沿（输入框永不入镜）。标题行 = 联合区内
                # 消息区上沿以上（1x 坐标）；消息行平移回消息区 1x 坐标系，
                # 下游气泡/头像/合并逻辑与旧路径完全共用。
                u_l = int(w * min(self._title_region[0], self._message_region[0]))
                u_t = int(h * min(self._title_region[1], self._message_region[1]))
                u_r = int(w * max(self._title_region[2], self._message_region[2]))
                u_b = int(h * max(self._title_region[3], self._message_region[3]))
                union = shot.crop((u_l, u_t, u_r, u_b))
                all_items = ocr_image(union)
                # 标题带钉在标定标题区内（center-y < 标题区下沿）——标题区
                # 下沿与消息区上沿之间夹缝的内容（真机实测：群聊首行
                # 「何镇鸿:[图片]」会混进标题串）不得污染标题，全部归消息带
                t_bot = int(h * self._title_region[3]) - u_t
                title_items = [it for it in all_items
                               if (it["y"] + it["h"] // 2) < t_bot]
                title = "".join(it["text"] for it in
                                sorted(title_items, key=lambda i: i["x"])).strip()
                # 空标题防线（自 analyze_window 迁入）：标题区读空多为
                # toggle 取消选中/黑图——force 重切，attempt 循环兜底重试
                if not title:
                    if chat is None:
                        # 位置模式：没有会话名可切，且标题区空说明窗口状态不可信
                        # （多为 toggle 取消选中）→ 放弃本次读取，让上层下一轮
                        # 重新按红圈定位。**不得**回落到列表/整窗 OCR 找会话。
                        logger.warning("[读取] 位置模式下标题区空（疑似取消选中），"
                                       "放弃本次读取，等下一轮红圈定位")
                        return []
                    logger.warning(f"[读取] {chat!r} 标题区空（联合 OCR），force 重切")
                    self._switch_chat(chat, force=True)
                else:
                    logger.debug(f"[读取] {chat!r} 标题={title!r}（联合 OCR）")
                    name, is_group, _ = parse_title(title)
                    self._current_title = name or chat
                    self._current_is_group = is_group
                    if chat is None:
                        # 位置模式（红圈几何链路）：调用方只知道条目位置，会话名
                        # 正是由这次联合 OCR 给出的——回填 chat，供私聊 sender
                        # 与每条消息的 chat 字段使用。不回填的话 sender=None 会被
                        # 上层的「对方消息」过滤整条丢掉（实测：消息读到但被丢）。
                        chat = self._current_title
                # 消息行平移回消息区 1x 坐标系（几何检测沿用消息区子图）；
                # 标题区下沿与消息区上沿夹缝的内容一并归消息带
                dx = int(w * self._message_region[0]) - u_l
                dy = int(h * self._message_region[1]) - u_t
                items = [dict(it, x=it["x"] - dx, y=it["y"] - dy)
                         for it in all_items if (it["y"] + it["h"] // 2) >= t_bot]
            else:
                # 单片 OCR（v2.1.3 废弃分片）：分片左右切边界会把整字切成两半
                # 误识（真机：「排」被切左半成「非」，产碎片「非序错乱」；探针
                # 稳定复现碎片「丙」「马」）。RapidOCR limit_side_len="min" 是
                # 短边不足才放大，宽图不压缩——分片解决的「压缩截断」不存在，
                # 单片读 40+ 字超长行一字不差。
                items = ocr_image(region)
            if items:
                # 头像检测 + 头像锚定的消息块分析（与 analyze_window 同一核心、
                # 同一张截图——OCR 坐标与块几何必须同源）。气泡颜色只用于结构
                # （找面板/媒体框/文件卡片图标），归属一律走头像。
                colors = detect_bubble_colors(region_1x)
                self._note_theme(colors.get("theme"))
                bot_tops = detect_avatar_tops(region_1x, colors.get("bg"), "right")
                other_tops = detect_avatar_tops(region_1x, colors.get("bg"), "left")
                info = analyze_blocks(region_1x, colors, bot_tops, other_tops,
                                      skip_bot=skip_bot)
                break
            # 消息区空白：可能 toggle 取消选中了（微信再点一次恢复选中）
            if attempt == 0:
                logger.info(f"[读取] {chat!r} 消息区读到 0 条，可能 toggle 取消选中，再点一次")
                self._switch_chat(chat, force=True)
        if not items:
            return []
        # 归属只用头像（用户定案）：analyze_blocks 给出本轮对方新消息的逐头像
        # 消息块；没有块 = 无对方新消息（头像检测不到也走这里，不做降级）。
        blocks = info.get("blocks") or []
        if not blocks:
            return []
        region_w, region_h = region_1x.width, region_1x.height
        avatar_h = max(40, region_h // 18)
        # 头像窄带（与 detect_avatar_tops 的 x 窗口同源）：头像图片上的文字
        # 不是消息内容，必须按几何剔除——真机幻影行「用户已无生命体征」悬在
        # 头像上、且正好落在该条消息的文字范围内，不剔除会混进文件名 OCR。
        left_band = (int(region_w * 0.02), int(region_w * 0.14))
        right_band = (int(region_w * 0.84), int(region_w * 0.98))

        def _in_avatar(cy: int, cx: int) -> bool:
            for (x0, x1), tops in ((left_band, other_tops),
                                   (right_band, bot_tops)):
                if not (x0 <= cx <= x1):
                    continue
                for t in tops:
                    if t <= cy <= t + avatar_h:
                        return True
            return False

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

        # 逐行归位：剔噪 → 剔头像区文字 → 剔输入框"发送"按钮 → 按消息块收行。
        # 不属于任何消息块的文字（时间/日期分隔行、bot 自己的消息、更早的
        # 历史）一律丢弃——块边界由头像锚定，不猜、不降级。
        by_block: dict[int, list[dict]] = {}
        for it in items:
            text = it["text"]
            if _is_noise(text):
                continue
            cy = it["y"] + it["h"] // 2
            cx = it["x"] + it["w"] // 2
            if _in_avatar(cy, cx):
                continue
            # 输入框按钮噪声：微信输入框"发送"按钮固定在消息区右下角，OCR 会
            # 把它读成消息（真机日志：latest content='发送'）；同时防 x 靠右
            # 的按钮被并进消息尾部。
            if text == "发送" and it["x"] > 0.85 * region_w \
                    and it["y"] > 0.8 * region_h:
                continue
            hit = None
            for bi, blk in enumerate(blocks):
                if blk["top"] <= cy <= blk["bottom"]:
                    hit = bi
                    break
            if hit is None:
                continue
            by_block.setdefault(hit, []).append(it)

        msgs: list[WeChatMessage] = []
        seq = 0
        for bi, blk in enumerate(blocks):
            lines = by_block.get(bi)
            if not lines:
                continue
            lines.sort(key=lambda i: (i["y"], i["x"]))
            if blk["kind"] == "image":
                # 图片消息：块内 OCR 文字全部丢弃——图片上的字不是消息内容
                # （真机 '我不是'）。图片本体由媒体捕获路径处理。
                continue
            panel = blk["panel"]
            sender = chat
            content_lines = lines
            # 群聊发送者名：面板（气泡）外、气泡上方的短文本行（真机
            # '哆拉A萝'）；私聊没有这行，sender 退回会话名。
            if panel is not None and len(lines) > 1:
                head = lines[0]
                if (head["y"] + head["h"] // 2) < panel[0] \
                        and len(head["text"].strip()) <= 8:
                    sender = head["text"].strip()
                    content_lines = lines[1:]
            text = " ".join(l["text"] for l in content_lines).strip()
            text = re.sub(r"\s+", " ", text)
            if not text or _is_noise(text):
                continue
            seq += 1
            mtype = MessageType.FILE if blk["kind"] == "file" else MessageType.TEXT
            msgs.append(WeChatMessage(
                id=f"visual_{seq}",
                chat=chat,
                sender=sender,
                content=text,
                type=mtype,
                y=blk["top"],
            ))
            # 归位诊断（DEBUG 轨，只进 bot.log）：块类型/头像锚点/块范围/
            # 面板与图标全现场，排障 grep "[归位]" 即得。
            logger.debug(
                f"[归位] {text[:24]!r} -> {sender!r} kind={blk['kind']}"
                f" avatar_top={blk['avatar_top']}"
                f" block=({blk['top']},{blk['bottom']})"
                f" panel={panel} icon={blk['icon']}"
                f" right_tops={bot_tops} left_tops={other_tops}")

        if limit:
            msgs = msgs[-limit:]
        return msgs

    # ---- 协议：发送 ----

    def send_text(self, chat: str, text: str, hold_after_paste: float = 0.0) -> bool:
        """定位输入框 → 点击聚焦 → 粘贴 → （可选停留）→ Enter。

        hold_after_paste：粘贴完成后、回车发送前的停留秒数——多条分段发送
        时，该停留让对方端看到「对方正在输入…」（活人感，用户定案）；
        0 = 粘贴后立即回车（默认，与旧版行为一致）。
        """
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
            if hold_after_paste and hold_after_paste > 0:
                time.sleep(float(hold_after_paste))
            pyautogui.press("enter")
            return True
        except Exception as e:
            logger.error(f"发送失败: {e}")
            return False

    # 语音发送时序常量（真机标定，蓝本 voice_send_dafeiyu.py）
    _VOICE_PRE_ALT = 1.2   # 按下右 Alt 后等录音启动（含胶囊检测前缓冲）
    _VOICE_TAIL = 0.6      # 音频写完后的尾巴（防尾字被截）
    _VOICE_AFTER = 2.5     # 松开后等微信完成发送

    def send_voice(self, chat: str, wav_path: str) -> bool:
        """发送语音消息：切目标会话（标题复验）→ 聚焦输入框 → SendInput
        物理右 Alt 触发微信录音 → 胶囊像素检测确认 → 经持久渲染流把音频
        写进虚拟声卡 → 尾音 → 松开（微信自动发送）。

        全程 fail-closed：端点缺失 / 环回质检 / 设备切换 / 会话复验 / 胶囊
        检测任一不过 → 返回 False（**该条确认未发出**，上层回退文本），
        绝不发静音、绝不发错人；默认录音设备 finally 三 role 恢复。
        返回 True = 已松开右 Alt，微信应已自动发出。
        """
        ch = _get_voice_channel()
        if ch is None:
            return False
        if not ch.ensure_stream():
            logger.warning(
                "[语音] VB-CABLE 端点不可用（请确认已安装 VB-CABLE 虚拟声卡）")
            return False
        try:
            data48, dur = ch.load_resampled(wav_path)
        except Exception as e:
            logger.error(f"[语音] 音频读取失败: {e}")
            return False
        if data48 is None or dur <= 0:
            logger.error("[语音] 音频内容为空，中止")
            return False
        if not ch.loopback_gate(data48):
            return False
        orig = ch.switch_default_capture()
        if orig is None:
            return False
        try:
            # 会话定位 + 标题复验（复用既有切换与引号变体等价口径，防发错人）
            self._foreground()
            title = self.read_title(foreground=True)
            cur = parse_title(title)[0] if title else ""
            if not _same_chat_name(cur, chat):
                if not self._switch_chat(chat):
                    logger.warning(f"[语音] 切换会话 {chat!r} 失败，中止")
                    return False
                title = self.read_title(foreground=True)
                cur = parse_title(title)[0] if title else ""
            if not _same_chat_name(cur, chat):
                logger.warning(f"[语音] 标题复验失败（{cur!r} ≠ {chat!r}），中止")
                return False
            self._foreground()
            time.sleep(0.3)
            rect = self._input_box_rect()
            if not rect:
                logger.warning("[语音] 无法定位输入框，中止")
                return False
            import pyautogui
            pyautogui.click(rect[0] + rect[2] // 2, rect[1] + rect[3] // 2)
            time.sleep(0.5)
            if not alt_down(right=True):
                logger.warning("[语音] SendInput 右 Alt 按下失败，中止")
                return False
            time.sleep(self._VOICE_PRE_ALT)
            if not _send_pill_visible(self._hwnd):
                alt_up(right=True)
                logger.warning("[语音] 松开发送胶囊未出现（录音未触发），中止")
                return False
            ch.write(data48)
            time.sleep(self._VOICE_TAIL)
            alt_up(right=True)
            time.sleep(self._VOICE_AFTER)
            logger.info(f"🎤 [语音] 已发送给 {chat!r}（{dur:.1f}s）")
            return True
        finally:
            ch.restore_default_capture(orig)

    def mark_session_read(self) -> bool:
        """点击一次输入框让当前会话标记已读（清红圈），不切换会话。

        微信只在会话获得窗口交互时才把新消息标记已读——当前会话已选中时
        _switch_chat 不点击（防 toggle 取消选中），「群聊无 @ 跳过回复」的
        流程全程零点击，红圈原样留在列表里，8s 退避后无限循环重处理。
        其余流程发送回复时点输入框顺带清圈，唯独该流程没有——点一次输入框
        补上（聚焦交互即标记已读，且不会像点会话行那样 toggle 取消选中）。"""
        try:
            import pyautogui
            rect = self._input_box_rect()
            if rect is None:
                logger.warning("[已读] 无法定位输入框，红圈可能滞留")
                return False
            cx = rect[0] + rect[2] // 2
            cy = rect[1] + rect[3] // 2
            self._foreground()  # 点击依赖前台，先置前微信窗口
            pyautogui.click(cx, cy)
            time.sleep(0.3)
            logger.info("[已读] 已点击输入框标记当前会话已读")
            return True
        except Exception as e:
            logger.warning(f"[已读] 点击输入框失败: {e}")
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

    def media_screen_boxes(self, min_top: int | None = None,
                           exclude_rows: list[tuple[int, int]] | None = None,
                           ) -> list[tuple[int, int, int, int]]:
        """检测消息区媒体内容（图片/视频/表情）的屏幕矩形，供点击与裁剪。

        截图消息区 → 探测气泡色 → find_media_boxes 检测「非背景非气泡」的
        大块媒体矩形 → 换算屏幕物理坐标（DPI 已 per-monitor aware，窗口
        rect 与截图同坐标系）。返回 [(l, t, r, b), ...] 屏幕坐标，按上→下
        （时间正序）排列；检测不到返回空。

        min_top：消息区 1x 坐标下沿阈值——只保留 top ≥ min_top 的框。上层
        传 analyze_window 的 bot_bottom（bot 最后回复之后第一条消息的上边
        框），即可只取对方本轮新媒体，排除 bot 自己的文件卡片与历史媒体。

        exclude_rows：消息区 1x 坐标 y 区间 [(top, bottom), ...]，上层传文件
        名 OCR 行区间——文件卡片的类型图标（W/PDF 彩色小方块）会从面板
        跳出成独立小媒体框（面板本身判气泡），与文件名行同块垂直相交，
        相交即排除；消息纵向堆叠保证跨块必不相交，真实图片不受影响。
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
        self._note_theme(colors.get("theme"))
        if not (colors.get("self") or colors.get("other")):
            return []
        # 面板（气泡框）内部的框一律不算媒体：文件卡片的类型图标会跳出成
        # 独立小框，点它会打开用户文件（真机风险）——先剔除再过滤。
        panels = [(t, b, l, r) for (t, b, l, r, is_self)
                  in find_bubble_boxes(region, colors) if not is_self]
        boxes = filter_media_boxes(
            drop_panel_contents(find_media_boxes(region, colors), panels),
            min_top=min_top, exclude_rows=exclude_rows)
        if not boxes:
            return []
        rect = wt.RECT()
        u32.GetWindowRect(self._hwnd, ctypes.byref(rect))
        win_l, win_t = rect.left, rect.top
        msg_l = win_l + int(w * self._message_region[0])
        msg_t = win_t + int(h * self._message_region[1])
        return [(msg_l + l, msg_t + t, msg_l + r, msg_t + b)
                for (t, b, l, r) in boxes]

    def analyze_window(self, chat: str, foreground: bool = True,
                       skip_bot: int = 0,
                       assume_switched: bool = False) -> dict:
        """切会话 + 截图 + 头像锚定的消息块分析，返回窗口内消息结构（不 OCR）。

        供上层先判断「窗口内是否只有文字」还是「有图片/文件卡片」，再决定
        sleep 10s 防话没说完 / OCR 读文字。

        assume_switched：调用方在**同一次处理事件**里刚切过（红圈几何链路：
        `iter_unread_sessions` 已点击该行并用像素复验选中）→ 跳过首切，省
        一次点击与一次标题 OCR。只省首切——分析为空时的 force 重切兜底
        （toggle 取消选中防线）保留不动。

        归属判据（用户定案）：只用头像。右侧窄带非背景块 = bot 头像，左侧 =
        对方头像；一条消息 = 一个头像；分析区上沿 = bot 最后一条消息之后
        的下一条对方头像上边框，只有该上沿以下才判类型与分块。气泡颜色只
        用于结构（找面板/媒体框），不再参与 self/对方判定；头像检测不到即
        视作该侧无新消息，不做任何颜色/中线降级。

        skip_bot：跳过最近 N 条 bot 消息（占位回复剔除），分析区上沿随之
        上移——占位之前对方发来的消息仍纳入本轮。越界时收敛到
        len(bot_tops)-1 兜底不越界。

        返回 dict：
        {
            "bot_bottom": int | None,       # 分析区上沿 = 对方新消息第一条的上边框（1x）
            "other_first_top": int | None,  # 同上（文件候选阈值沿用的键）
            "other_text": [(t,b,l,r), ...],  # 对方文字消息的面板框
            "other_media": [(t,b,l,r), ...], # 对方图片/视频/表情的媒体矩形
            "other_files": [(t,b,l,r), ...], # 对方文件卡片的面板框（属多媒体，供 10s 防抖）
            "other_blocks": [dict, ...],     # 逐头像锚定的消息块（kind: text/image/file）
            "has_text": bool,
            "has_media": bool,              # 有图片或文件卡片
            "has_other": bool,              # 有 bot 之后的对方新消息（纯头像几何判据）
            "width": int, "height": int,
        }
        """
        # 强制窗口尺寸：分析前复查（三区域/头像判据都按 WINDOW_SIZE 标定）
        self.enforce_window_size()
        if not assume_switched:
            self._switch_chat(chat)
        # 纯像素分析（本函数不读标题）。事件内 OCR 预算：会话身份与群聊标记由
        # get_messages(assume_switched=True) 的**联合 OCR**（标题带 + 消息区
        # 一次读）给出——权威 is_group 在那里解析并刷新 _current_is_group；
        # 本返回值的 is_group 仅为缓存快照（可能来自上一轮事件），调用方必须
        # 在 _window_msgs 之后再取缓存。
        is_group = bool(getattr(self, "_current_is_group", False))
        empty = {"bot_bottom": None, "other_first_top": None,
                 "other_text": [], "other_media": [], "other_files": [],
                 "other_blocks": [], "skip_bot": skip_bot,
                 "has_text": False, "has_media": False, "has_other": False,
                 "is_group": is_group, "width": 0, "height": 0}
        for attempt in range(2):
            if attempt > 0:
                # 第一次分析结果为空：可能 toggle 取消选中（_switch_chat 标题
                # 检测在微信被遮挡时失败，误点击已选中会话），force 重切恢复选中。
                # 位置模式（assume_switched=True，调用方只知道未读条目位置、
                # 还不知道会话名）下没有名字可切——由上层用红圈位置重新点击。
                if chat:
                    self._switch_chat(chat, force=True)
                else:
                    logger.warning(
                        "[分析] 窗口疑似取消选中，但位置模式下无会话名可切，交由上层重试")
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
            self._note_theme(colors.get("theme"))
            # 头像检测：右侧窄带 = bot 头像，左侧 = 对方头像（全流程唯一归属判据）。
            # 真机实测：bot 文件 r/w=0.83、对方长文字 r/w=0.80——宽度阈值切不开，
            # 头像一右一左天然分离，无需模板/颜色/宽度判据。
            bot_tops = detect_avatar_tops(region, colors.get("bg"), "right")
            other_tops = detect_avatar_tops(region, colors.get("bg"), "left")
            # 窗口完全空（无头像、无气泡色）→ 疑似 toggle 取消选中，重切兜底
            if not bot_tops and not other_tops \
                    and not (colors.get("self") or colors.get("other")):
                continue
            info = analyze_blocks(region, colors, bot_tops, other_tops,
                                  skip_bot=skip_bot)
            blocks = info["blocks"]
            return {
                "bot_bottom": info["region_top"],
                "other_first_top": info["region_top"],
                # 框未检到的块（气泡色漂移）在列表里不出现——块本身仍在
                # other_blocks 里（文字照读），列表只收真实框。
                "other_text": [b["panel"] for b in blocks
                               if b["kind"] == "text" and b["panel"]],
                "other_media": [b["media"] for b in blocks
                                if b["kind"] == "image" and b["media"]],
                "other_files": [b["panel"] for b in blocks
                                if b["kind"] == "file" and b["panel"]],
                "other_blocks": blocks,
                # skip_bot 原样回传：调用方随后调 get_messages 时必须传同一个
                # 值，否则两次分析的「分析区上沿」会错位（占位回复剔除失效）。
                "skip_bot": skip_bot,
                "has_text": info["has_text"],
                "has_media": info["has_media"],
                "has_other": bool(info["other_new_tops"]),
                "is_group": is_group,
                "width": rw, "height": rh,
            }
        return empty

    def send_file(self, chat: str, file_path: str) -> bool:
        """发送文件：暂未实现（视觉定位文件按钮 + 文件对话框输入路径）。"""
        logger.warning("visual 后端 send_file 暂未实现")
        return False

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
