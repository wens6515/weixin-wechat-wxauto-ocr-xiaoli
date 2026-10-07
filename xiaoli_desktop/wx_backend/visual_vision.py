# -*- coding: utf-8 -*-
"""visual_backend 拆分 · 视觉原语层：气泡色探测 / 连通域 / 媒体框 / 头像检测 / 文件卡片图标。

纯像素判定（无 OCR、无 Win32 调用）。全部被门面 re-export；组合层
analyze_blocks 留在门面——它调用本模块原语的名字落在门面命名空间，
测试的 mock.patch("wx_backend.visual_backend.X") 语义不变。
"""
from __future__ import annotations

from PIL import Image

# 主题判定阈值：背景中位亮度高于此值 = 浅色主题（真机 深色 ~30 / 浅色 ~250）。
_LIGHT_THEME_LUM = 128
# 对方气泡「近背景窄带」：颜色距离（三通道绝对差之和）落在 lo~hi 内，再按
# 明暗方向过滤。**仅用于估算气泡色**（浅色主题的候选掩码）——那里要的是
# 「像气泡的色块」而不是精确色值，窄带口径比 per-channel 容差更耐渲染噪声。
_BUBBLE_BAND = (8, 90)
# 浅色主题面板（气泡 / 文件卡片）的形状闸——真机实测：单行文字气泡 44~62px 高、
# 行距 ~29（5 行 ≈ 200），文件卡片 146px 高；而图片消息里照片 240px、动画表情
# 270px、截图页 422px 高（图片按短边适配聊天区宽度，天然比文字面板高）。
# 高度上限定 200：放得下到 5 行的长消息气泡与文件卡片，又比最矮的图片（240）
# 低 40px；宽度上限放到 90% 幅面——文件卡片宽度跟着文件名长度走（真机
# 432/747=58%），窄窗口下占比会更大，卡太死会误杀卡片（卡片判不成 file 就丢
# 文件名，任务桥的入口就断了）；宽高比下限 0.9 兜住「窄而高」的竖图（真机
# 「0」这条最短气泡 70x62 → 1.13，留足余量）。
_LIGHT_BUBBLE_MAX_H = 200
_LIGHT_BUBBLE_MAX_W_RATIO = 0.9
_LIGHT_BUBBLE_MIN_ASPECT = 0.9
# 面板实心度下限：气泡/文件卡片是整片填充（实测 ≈1.0 / 0.75），文字抗锯齿
# 像素连成的稀疏条带只有 ~0.3，据此把条带挡在面板之外。
_LIGHT_PANEL_FILL_MIN = 0.5
# 自己气泡色容差：深色主题绿 (53,210,141) 与背景/图片色域相距远，容差 60 够用；
# 浅色主题绿 (157,242,159) 是粉彩绿，浅灰 (200,200,200) 也落进 60 的球内
# （实测三通道差 43/42/41），收紧到 45 才把它挡在外面。
_SELF_TOL = {"dark": 60, "light": 45}


def _lum(color) -> float:
    """感知亮度（主题判定与明暗方向用，不参与精确配色）。"""
    r, g, b = color
    return 0.299 * r + 0.587 * g + 0.114 * b


def _median_color(arr, mask):
    """掩码内像素的中位色；掩码为空返回 None。"""
    import numpy as np
    px = arr[mask]
    if len(px) == 0:
        return None
    return tuple(int(v) for v in np.median(px, axis=0))


def _near_bg_band(arr, bg, lo=8, hi=90, darker=True):
    """与背景「接近但不同」的窄带掩码：颜色距离 lo~hi 内，且比背景暗（或亮）。"""
    import numpy as np
    dist = np.abs(arr - np.array(bg, dtype=np.int16)).sum(axis=2)
    m = (dist > lo) & (dist < hi)
    bg_sum = int(bg[0]) + int(bg[1]) + int(bg[2])
    row_sum = arr.sum(axis=2)
    m &= (row_sum < bg_sum) if darker else (row_sum > bg_sum)
    return m


def _dominant_flat_color(arr, mask, max_h: int = _LIGHT_BUBBLE_MAX_H):
    """候选掩码里「像气泡的平坦色块」投票取色（浅色主题用）；取不到返回 None。

    浅色主题下图片的纯白/浅灰同样落在「近背景」色域里，直接取中位数会被大图
    内容带偏（真机：带截图的群聊，中位数落到图片纯白 (255,255,255)，气泡判定
    随之全线错位）。改为聚连通域、只给「高度像气泡（≤max_h）且不是整幅宽背景
    块」的域投票：气泡是大片单色矩形（票多），图片内容颜色杂、单色域小。
    """
    comps = []
    rw = mask.shape[1]
    for (t, b, l, r) in _connected_boxes(mask, min_h=8, min_w=30):
        h, w = b - t, r - l
        if h > max_h or w > rw * 0.9:
            continue
        sub = mask[t:b, l:r]
        n = int(sub.sum())
        if n < 40:
            continue
        col = _median_color(arr[t:b, l:r], sub)
        if col is None:
            continue
        comps.append((tuple(c // 4 * 4 for c in col), col, n))
    if not comps:
        return None
    votes: dict[tuple, int] = {}
    for key, _col, n in comps:
        votes[key] = votes.get(key, 0) + n
    best_key = max(votes, key=lambda k: votes[k])
    # 票高色域里取最大那块的原色（不量化，回到真实色值）
    return max((c for c in comps if c[0] == best_key), key=lambda c: c[2])[1]


def _dominant_color(arr, mask=None, step: int = 4):
    """掩码内出现最多的颜色（量化后取众数再回真实中位）；空掩码返回 None。

    两处用途：面板填充色自测量（`find_panel_icon`）、浅色气泡色投票
    （`_dominant_flat_color`）。UI 填充是整片单色，众数比中位数更抗文字/图标
    这些少数像素的干扰。
    """
    import numpy as np
    px = arr.reshape(-1, 3) if mask is None else arr[mask]
    if len(px) == 0:
        return None
    q = (px // step * step).astype(np.int32)
    key = q[:, 0] * 65536 + q[:, 1] * 256 + q[:, 2]
    vals, counts = np.unique(key, return_counts=True)
    best = vals[int(counts.argmax())]
    sel = key == best
    return tuple(int(v) for v in np.median(px[sel], axis=0))


def _light_band_mask(arr, bg, w, lo=8, hi=90):
    """浅色主题候选窄带掩码 + 几何清洗（气泡取色与面板判定共用同一份）。

    窄带口径（颜色距离 lo~hi 且比背景暗）而不是固定容差——浅色下气泡与背景
    每通道只差 ~12 色阶，容差小了不起作用、大了把整幅背景圈进来。自己（绿）
    气泡距离背景 ~192，天然落在窄带外。

    几何清洗必须做：真机事故——用户在应用内标定时把消息区左缘和会话列表重叠
    了 3px，那 3px 竖条是列表面板灰 (238,238,240)（与气泡**同色**），它把窄带
    连通域从上到下粘成一整块（真机实测：整帧只剩一个 1209px 高的连通域），
    取色投票里唯一的候选块被形状闸丢掉 → 气泡色估不出来 → 文件卡片图标判据
    拿不到面板色 → 卡片全被判成文字消息。左缘按头像带切（0.12w，气泡左缘
    实测 0.147w）、右缘按滚动条切（0.99w）。
    """
    m = _near_bg_band(arr, bg, lo, hi, darker=True)
    m[:, :int(w * 0.12)] = False
    m[:, int(w * 0.99):] = False
    return m


def _light_panel_mask(arr, bg):
    """浅色主题的面板候选掩码（窄带口径，几何清洗交给 _light_panel_boxes）。"""
    return _light_band_mask(arr, bg, arr.shape[1])


def _light_panel_boxes(arr, bg, w, h, mask=None):
    """浅色主题的面板框（气泡 / 文件卡片）：窄带连通域 ∩ 形状闸 ∩ 实心度闸。

    形状闸是「不依赖色值」的第二道判据：图片页/照片/动画表情即使落进窄带
    （浅灰页底与气泡只差 4 色阶），也会因为「太高 / 太宽 / 竖着」被挡在外面。
    实心度闸挡的是文字抗锯齿像素连成的稀疏条带（单行文字条带高 ~25px、填充率
    ~0.3；气泡/卡片是整片填充，实测填充率 ≈1.0/0.75）——条带若被当成面板，
    群聊的发送者名拆分会拿它当 panel 基准而拆不出来。
    """
    m = _light_band_mask(arr, bg, w) if mask is None else mask
    out = []
    for (t, b, l, r) in _connected_boxes(m):
        ph, pw = b - t, r - l
        if ph >= h * 0.6 or ph > _LIGHT_BUBBLE_MAX_H:
            continue
        if pw > w * _LIGHT_BUBBLE_MAX_W_RATIO:
            continue
        if pw < ph * _LIGHT_BUBBLE_MIN_ASPECT:
            continue
        if float(m[t:b, l:r].mean()) < _LIGHT_PANEL_FILL_MIN:
            continue
        out.append((t, b, l, r))
    return out


def estimate_theme(img: Image.Image) -> str:
    """识别微信界面主题："light" / "dark"。

    判据 = 画面中位亮度：面板底色占绝大多数，浅色主题中位 ~250、深色 ~30，
    两者相差极大，零标定即可判。**喂内容区截图（会话列表 / 消息区），不要喂
    整窗**——微信 4.x 的窗口顶栏两套主题下都是深色，会把整窗判成深色。
    """
    import numpy as np
    arr = np.asarray(img.convert("RGB"), dtype=np.int16)
    if arr.size == 0:
        return "dark"
    med = tuple(int(v) for v in np.median(arr.reshape(-1, 3), axis=0))
    return "light" if _lum(med) >= _LIGHT_THEME_LUM else "dark"


def detect_bubble_colors(img: Image.Image, theme: str | None = None) -> dict:
    """自动探测消息区背景色 / 对方气泡色 / 自己气泡色 / 主题（1x RGB 图）。

    微信两套主题的气泡色真机实测：
    - 深色主题：背景(30,30,31)、对方气泡(47,47,48)、自己气泡绿(53,210,141)
      —— 对方气泡比背景**亮**
    - 浅色主题：背景(250,250,250)、对方气泡(238,238,240)、自己气泡绿(157,242,159)
      —— 对方气泡比背景**暗**
    两组是镜像关系：深色时代写死的「对方气泡比背景亮」约束在浅色下反向，
    会把对方气泡色探成图片里的纯白、把文字气泡判成图片（真机事故）。

    探测失败项为 None。策略：
    - 背景：边缘 8px 像素中位数（边缘通常无气泡、无文字）
    - 主题：背景亮度（近白=浅色 / 近黑=深色），决定取色方向与容差；theme
      显式传入时以参数为准（后端启动时判定一次，运行时随帧跟随）
    - 自己气泡：绿色像素中位数（G 显著高于 R/B，两主题皆绿）
    - 对方气泡：近背景窄带内、按明暗方向过滤的非绿像素——深色取中位数
      （现网行为）；浅色改用平坦色块投票（图片白底会污染中位数）
    """
    import numpy as np
    arr = np.asarray(img.convert("RGB"), dtype=np.int16)
    h, w, _ = arr.shape
    if h < 16 or w < 16:
        return {"bg": None, "other": None, "self": None,
                "theme": theme if theme in ("light", "dark") else "dark"}
    border = np.concatenate([
        arr[:8].reshape(-1, 3), arr[-8:].reshape(-1, 3),
        arr[:, :8].reshape(-1, 3), arr[:, -8:].reshape(-1, 3),
    ])
    bg = tuple(int(v) for v in np.median(border, axis=0))
    if theme not in ("light", "dark"):
        theme = "light" if _lum(bg) >= _LIGHT_THEME_LUM else "dark"
    R, G, B = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    green = (G - R > 40) & (G - B > 40) & (G > 100)
    self_color = None
    if int(green.sum()) > 80:
        self_color = tuple(int(v) for v in np.median(arr[green], axis=0))
    # 对方气泡：与背景「接近但不同」的非绿像素。图片内容颜色多样、与背景差异
    # 大（总差常 >100），不进候选；落在窄带里的图片内容（深色主题的深色块、
    # 浅色主题的白底）才是噪声源，分别用「明暗方向」与「平坦色块投票」挡掉。
    # 深色补充：深色图片（群二维码 (15,15,17)）落在 10~90 内会把中位数从
    # (47,47,48) 拉低成 (39,39,41)，接近背景后 find_bubble_boxes tol=12
    # 把背景也当气泡吞了——「比背景亮」约束正是为它加的，深色路径保持不变。
    if theme == "light":
        # 浅色：候选掩码与面板判定共用同一份（含头像带/滚动条清洗，
        # 否则会话列表 3px 重叠边会把整个窄带粘成一块、取色失败）
        cand = _light_band_mask(arr, bg, w, *_BUBBLE_BAND) & ~green
    else:
        cand = _near_bg_band(arr, bg, *_BUBBLE_BAND, darker=False) & ~green
    other_color = None
    if int(cand.sum()) > 80:
        other_color = (_dominant_flat_color(arr, cand) if theme == "light"
                       else _median_color(arr, cand))
    return {"bg": bg, "other": other_color, "self": self_color, "theme": theme}


def _near_color(arr, color, tol):
    """返回与 color 接近（RGB 各分量差 ≤ tol）的布尔掩码。"""
    import numpy as np
    d = np.abs(arr - np.array(color, dtype=np.int16))
    return (d[:, :, 0] <= tol) & (d[:, :, 1] <= tol) & (d[:, :, 2] <= tol)


def _connected_boxes(mask, min_h=20, min_w=40, x_gap=8, y_gap=6):
    """把布尔掩码聚合成连通域边界框 [(top, bottom, left, right)]（像素坐标）。

    行内切段语义 = 相邻 True 像素列差 >x_gap 切段——一次 np.nonzero 全图
    向量化取全部 True 像素后直接切分（np.nonzero 按 C 序返回：行升序、
    行内列升序），替代旧的逐行 np.where + Python 切段循环；跨行合并保持
    流式贪心（y 隙 ≤y_gap 且 x 重叠，并入首个命中框）。真机消息区尺寸
    （747x1135）基准较旧逐行循环快 3.4~6.2 倍、输出逐场景一致
    （tools/bench_connected_boxes.py）。过滤掉小噪声（气泡至少高 20px、
    宽 40px）。"""
    import numpy as np
    rows, cols = np.nonzero(mask)
    if len(rows) == 0:
        return []
    brk = (rows[1:] != rows[:-1]) | (cols[1:] - cols[:-1] > x_gap)
    cut = np.flatnonzero(brk) + 1
    starts = np.concatenate(([0], cut))
    ends = np.concatenate((cut, [len(rows)]))  # exclusive
    boxes = []
    for y, l, r in zip(rows[starts].tolist(), cols[starts].tolist(),
                       (cols[ends - 1]).tolist()):
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

    对方气泡与背景的色差两套主题都很小（深色 ~17 色阶/分量、浅色 ~12），
    图片消息暗部/文字会把近似像素连成一片、聚合出接近全屏的异常框——过滤掉
    高度超消息区 60% 的框（正常对方气泡 < 60%），被过滤时调用方回退 y 阈值
    处理对方消息。

    对方通道按主题分叉（`colors["theme"]`，缺省深色）：
    - 深色：围绕探测到的气泡色 per-channel 容差 12（现网行为）
    - 浅色：结构判据（比背景暗的近背景窄带 ∩ 形状闸）——浅色下气泡填充
      (238,238,240) 与「浅灰图片页」(242,242,242) 只差 4 色阶，per-channel
      容差 12 时连背景 (250) 都在气泡色的邻域内（整幅背景变气泡）、收到 3
      才分得开却又依赖气泡色估得准；改用「窄带 + 形状」后判据与色值解耦，
      气泡、文件卡片进、图片页/照片/表情出。
    """
    import numpy as np
    arr = np.asarray(img.convert("RGB"), dtype=np.int16)
    theme = colors.get("theme", "dark")
    boxes = []
    if colors.get("self"):
        mask = _near_color(arr, colors["self"], tol=_SELF_TOL.get(theme, 60))
        for (t, b, l, r) in _connected_boxes(mask):
            boxes.append((t, b, l, r, True))
    if theme == "light" and colors.get("bg") is not None:
        for (t, b, l, r) in _light_panel_boxes(arr, colors["bg"],
                                               img.width, img.height):
            boxes.append((t, b, l, r, False))
    elif colors.get("other"):
        # 对方气泡色与背景差异小（深色主题仅 ~17 色阶），tol 要收紧
        mask = _near_color(arr, colors["other"], tol=12)
        # 排除右侧滚动条：滚动条（深色主题灰 ~(36,36,38)）与 other 色
        # (~47,47,48) 仅差 ~11 色阶，落在 tol=12 内被误判为 other，贯穿
        # 整图聚成接近全屏的异常框，把真实对方气泡（如群聊 @ 消息）一起
        # 吞掉后又被 60% 高度过滤丢弃（真机根因：群聊 @ 读不到消息）。
        # 滚动条固定在消息区最右侧（x/w ≥ 0.99），直接掩掉。
        mask[:, int(img.width * 0.99):] = False
        for (t, b, l, r) in _connected_boxes(mask):
            if (b - t) >= img.height * 0.6:
                continue
            boxes.append((t, b, l, r, False))
    return boxes


def find_media_boxes(img: Image.Image, colors: dict) -> list[tuple]:
    """检测图片/视频/表情的内容矩形（非背景、非气泡色的大块连通域）。

    媒体消息（图片/视频/动画表情）在消息区是一块非背景、非气泡色的大矩形
    （图片内容本身），无气泡包裹。排除 bg/气泡色/自己气泡色后剩余的大块
    连通域即媒体内容——用于图片消息精确定位（点击中心打开预览），替代
    整屏截图降级。返回 [(top, bottom, left, right)]（1x 坐标）。

    排除口径按主题分叉（`colors["theme"]`，缺省深色）：
    - 深色：bg±8 / 气泡色±12 / 自己色±60。bg 收紧到 8 是因为深色图片
      （群二维码 (15,15,17)）与深色背景 (30,30,31) 各分量只差 ~15，旧 tol=30
      把整张黑图当背景排除、media 框只覆盖图片中间一小段彩色区。
    - 浅色：bg±4 ＋ **排除面板像素**（`_light_panel_boxes` 用结构判据判出的
      气泡/文件卡片填充），外加一条只在「气泡色估值与面板实测填充一致」时
      启用的颜色窗口。浅色下不能单靠色值排除：背景 250 与白底内容 255 只差 5
      （容差大就吞掉白底图）、气泡填充 238 与图片页 242 只差 4（容差大就把
      图片页当气泡排掉、图片被判成文字），两头都错。按「这块像素属于一个已
      判定的面板」排除，等价于深色主题里按气泡色排除的语义，却不依赖色值
      估得准。面板内的文字/图标像素仍留在内容里（暗字、彩色图标），由 40px
      尺寸阈值与 `drop_panel_contents` 兜底——后者会把落在面板框内的碎片
      一并剔掉。

    尺寸阈值 40px + 头像带整体剔除：头像固定在消息区最左/最右 ~15% 带内
    （真机 ~62px 高），旧实现靠 min 80px 挡头像——代价是 ~40px 的小表情
    全部漏检。改为把左右头像带从内容掩码整体剔除（头像结构上不可能落到
    带外，气泡/媒体也不会伸进带内），阈值即可安全降到 40（小表情渲染
    高度 40~55px）。
    """
    import numpy as np
    arr = np.asarray(img.convert("RGB"), dtype=np.int16)
    h, w, _ = arr.shape
    theme = colors.get("theme", "dark")
    content = np.ones((h, w), dtype=bool)
    if theme == "light":
        if colors.get("bg") is not None:
            other = colors.get("other")
            content &= ~_near_color(arr, colors["bg"], 4)
            m = _light_panel_mask(arr, colors["bg"])
            panel_px = np.zeros((h, w), dtype=bool)
            for (t, b, l, r) in _light_panel_boxes(arr, colors["bg"], w, h,
                                                   mask=m):
                panel_px[t:b, l:r] |= m[t:b, l:r]
            content &= ~panel_px
            # 颜色窗口只在「估出的气泡色 == 结构判出的面板填充色」时才用：两者
            # 一致说明估值可信，能兜住形状闸漏掉的超长气泡；不一致（估值被图片
            # 内容带偏）就只用结构判据——宁可漏一个长气泡，也不能把浅灰图片页
            # （242，与气泡 238 只差 4）当气泡排掉、把图片判成文字。
            fill = _median_color(arr, panel_px)
            if fill is not None and other is not None and all(
                    abs(fill[i] - other[i]) <= 6 for i in range(3)):
                content &= ~_near_color(arr, other, 4)
        if colors.get("self") is not None:
            content &= ~_near_color(arr, colors["self"],
                                    _SELF_TOL.get(theme, 60))
    else:
        for key, tol in (("bg", 8), ("other", 12), ("self", 60)):
            c = colors.get(key)
            if c is not None:
                content &= ~_near_color(arr, c, tol)
    # 左右头像带剔除（detect_avatar_tops 的窄带几何：左 [0.02,0.14]、
    # 右 [0.84,0.98]，各留余量）——头像误检从「靠尺寸猜」变「结构上不可能」
    content[:, :int(w * 0.15)] = False
    content[:, int(w * 0.85):] = False
    return _connected_boxes(content, min_h=40, min_w=40)


def filter_media_boxes(boxes: list[tuple], min_top: int | None = None,
                       exclude_rows: list[tuple[int, int]] | None = None,
                       ) -> list[tuple]:
    """媒体框过滤（纯几何，模块级供单测复用）：
    - min_top：只保留 top ≥ 阈值的框（上层传 bot_bottom，排除 bot 历史
      媒体与其文件卡片图标碎片）
    - exclude_rows：y 区间 [(top, bottom), ...]，与任一区间垂直相交的框
      剔除——文件卡片的类型图标（W/PDF 彩色小方块）会从面板跳出成独立
      小媒体框（面板本身判气泡），与文件名行同块垂直相交；消息纵向堆叠
      保证跨块必不相交，真实图片不受影响
    """
    if min_top is not None:
        boxes = [(t, b, l, r) for (t, b, l, r) in boxes if t >= min_top]
    for (rt, rb) in (exclude_rows or []):
        boxes = [(t, b, l, r) for (t, b, l, r) in boxes if b < rt or t > rb]
    return list(boxes)


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


def _anchor_avatar(top: int, tops: list[int], tol: int = 8) -> int | None:
    """消息框归属头像（唯一判据 = 头像，用户定案）。

    消息框顶部与头像顶部对齐（真机实测差 0），但渲染取整可能差 1~2px；
    直接走区间归属会把「顶在头像上方 1px」的框错划给上一个头像。先取
    |top - 头像top| ≤ tol 里最近的头像，超出容差才回退区间归属；仍无归属
    （框顶在所有头像之上）返回 None——调用方按「无归属」丢弃，不做任何
    颜色/中线降级。
    """
    best = None
    for t in tops:
        d = abs(top - t)
        if d <= tol and (best is None or d < best[0]):
            best = (d, t)
    if best is not None:
        return best[1]
    return _bucket_avatar(top, tops)


def _contains(outer: tuple, inner: tuple, pad: int = 2) -> bool:
    """inner 框是否落在 outer 框内（外扩 pad 容差）。"""
    ot, ob, ol, orr = outer
    it, ib, il, ir = inner
    return it >= ot - pad and ib <= ob + pad and il >= ol - pad and ir <= orr + pad


def drop_panel_contents(boxes: list[tuple], panels: list[tuple],
                        pad: int = 2) -> list[tuple]:
    """剔除落在面板（气泡框）内部的内容框。

    文件卡片的类型图标（Excel 绿方块 / W·PDF 彩色小方块）会从面板跳出成
    独立小框——它是面板的一部分，不是图片：留在这里会被图片点击路径
    （media_screen_boxes → 点击 → Ctrl+C/裁剪）当成图片点开，真机会直接
    打开用户文件。面板内部一律不产出媒体框。
    """
    return [(t, b, l, r) for (t, b, l, r) in boxes
            if not any(_contains(p, (t, b, l, r), pad) for p in panels)]


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


# 文件卡片判据（用户定案）：被标记为多媒体的消息块，若整体颜色与文字气泡
# 一致、只多一个右侧小图标，即文件消息。图标是面板内部的「实心小色块」——
# 真机标定（实时截图）：Excel 图标 69x55、填充率 0.96；同一面板内的文字行
# 高 ≤31、填充率 ≤0.48（含贴到面板右缘的长行），高度 40 / 填充率 0.7 双阈
# 把两者彻底分开。图标颜色不参与判定（Excel 绿 ≈ 微信绿会被 self 通道吞掉，
# PDF/Word 的蓝红方块又会跳成独立媒体框——两种都靠本判据统一处理）。
_ICON_MIN_H = 40
_ICON_MIN_W = 30
_ICON_FILL_MIN = 0.7


def find_panel_icon(arr, panel: tuple, colors: dict) -> tuple | None:
    """在面板（气泡框 / 文件卡片）内部右侧找文件卡片的类型图标，返回 1x 框或 None。

    arr：消息区 1x RGB 数组（np.int16）；panel：(top, bottom, left, right)。
    只在面板右半找（左半是文件名/大小文字），要求实心（填充率高）、有图标
    量级的高度与宽度，且不贴顶（贴顶块是文件名首行）。

    面板填充色优先用 `colors["other"]`；**取不到时现测面板自身的众数色**——
    真机事故：应用内标定的消息区与会话列表重叠 3px，那条同色竖条把气泡色
    投票搞塌（见 _light_band_mask），`other=None` 时本函数直接返回 None，
    文件卡片全被判成文字消息、任务桥入口失效。面板填充是整片单色，自测
    比依赖全帧估值更稳。

    背景色窗按主题分：浅色主题下背景 (250) 与图片里的纯白 (255) 只差 5，
    用它做排除会把图标里的白色字（Excel 的 X、PDF 的 W）挖成空洞、填充率
    掉到阈值以下；浅色下 `other±12` 本来就覆盖背景，这层窗多余，去掉。
    """
    t, b, l, r = panel
    ph, pw = b - t, r - l
    if ph <= 0 or pw <= 0:
        return None
    sub = arr[t:b, l:r]
    c = colors.get("other")
    if c is None:
        c = _dominant_color(sub)
    if c is None:
        return None
    mask = ~_near_color(sub, c, 12)
    if colors.get("theme") != "light" and colors.get("bg") is not None:
        mask &= ~_near_color(sub, colors["bg"], 8)
    mask[:, :int(pw * 0.5)] = False
    best = None
    for (ct, cb, cl, cr) in _connected_boxes(mask, min_h=20, min_w=20):
        h, w = cb - ct, cr - cl
        if h < max(_ICON_MIN_H, int(ph * 0.2)) or w < _ICON_MIN_W:
            continue
        if h > int(ph * 0.85) or w > int(pw * 0.5):
            continue
        fill = float(mask[ct:cb, cl:cr].mean())
        if fill < _ICON_FILL_MIN:
            continue
        score = fill * h * w
        if best is None or score > best[0]:
            best = (score, (t + ct, t + cb, l + cl, l + cr))
    return best[1] if best else None
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
