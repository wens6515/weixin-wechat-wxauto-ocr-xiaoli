# -*- coding: utf-8 -*-
"""README 展示图生成器 —— 仓库页面的品牌资产统一从这里出图。

子命令（在仓库根目录运行）：
    mascot <src.png> <out.png>    白底贴纸 → 透明底吉祥物（自动去掉漂浮问号/汗滴）
    shot   <src.png> <out.png>    原始聊天截图 → 深海渐变展示图（圆角 + 呼吸光 + 投影）
    banner <mascot.png> <out.png> 1280×640 横幅（仓库社交预览同款）
    all                            一次生成 README 用到的全部资产

    .venv/Scripts/python.exe tools/make_showcase.py all

设计约束：配色取自桌面端「深海小漓」主题（abyss），改主题色时同步下面的 ABYSS。
依赖 Pillow + numpy（cv2 可选：缺省时吉祥物抠图保留全部连通域，问号/汗滴不会被剔除）。
"""
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_BOLD = os.path.join(ROOT, "fonts", "NotoSansSC-Bold.otf")
FONT_REG = os.path.join(ROOT, "fonts", "NotoSansSC-Regular.otf")

# 「深海小漓」主题（xiaoli_desktop/xiaoli_app/ui/__init__.py → themes["abyss"]）
ABYSS = {
    "bg_top": (7, 20, 36),
    "bg_mid": (11, 37, 64),
    "bg_bot": (9, 28, 50),
    "p1": (79, 179, 255),
    "p2": (127, 212, 255),
    "text": (230, 244, 255),
    "muted": (122, 162, 196),
}


def _font(path, size):
    return ImageFont.truetype(path, size)


def _gradient(size, top, mid, bot):
    """竖向三段渐变（顶→中→底），比两段更接近主题背景的层次。"""
    w, h = size
    ys = np.linspace(0.0, 1.0, h)[:, None]
    t = np.where(ys < 0.5, ys * 2.0, (ys - 0.5) * 2.0)
    c0 = np.where(ys < 0.5, np.array(top), np.array(mid)).astype(np.float32)
    c1 = np.where(ys < 0.5, np.array(mid), np.array(bot)).astype(np.float32)
    col = c0 * (1 - t) + c1 * t
    arr = np.repeat(col[:, None, :], w, axis=1).astype(np.uint8)
    return Image.fromarray(arr, "RGB").convert("RGBA")


def _add_glow(img, center, radius, color, strength=0.55):
    """径向柔光（主题里的体光感），叠加到现有画布上。"""
    w, h = img.size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    dist = np.sqrt(((xx - center[0]) / radius) ** 2 + ((yy - center[1]) / radius) ** 2)
    falloff = np.clip(1.0 - dist, 0.0, 1.0) ** 2.2 * strength
    base = np.asarray(img).astype(np.float32)[:, :, :3]
    lit = base * (1 - falloff[:, :, None]) + np.array(color, np.float32) * falloff[:, :, None]
    out = np.dstack([np.clip(lit, 0, 255)]).astype(np.uint8)
    return Image.fromarray(out, "RGB").convert("RGBA")


def _add_beams(img, count=2):
    """丁达尔光束：自顶部斜下的柔光带（深海主题的标志性元素）。"""
    w, h = img.size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i in range(count):
        x0 = int(w * (0.10 + 0.52 * i))
        width = int(w * (0.16 + 0.05 * i))
        d.polygon([(x0, 0), (x0 + width, 0), (x0 - int(w * 0.42), h), (x0 - int(w * 0.42) - width, h)],
                  fill=(127, 212, 255, 26))
    layer = layer.filter(ImageFilter.GaussianBlur(w * 0.035))
    return Image.alpha_composite(img, layer)


def _add_bubbles(img, count=14, seed=7):
    """上浮气泡（深海主题 particle_style=bubble）：低透明度小圆点。"""
    w, h = img.size
    rng = np.random.default_rng(seed)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for _ in range(count):
        r = int(rng.integers(max(3, w // 220), max(6, w // 70)))
        x = int(rng.integers(0, w))
        y = int(rng.integers(0, h))
        d.ellipse([x - r, y - r, x + r, y + r], outline=(127, 212, 255, 58), width=max(1, r // 5))
    return Image.alpha_composite(img, layer)


def _rounded_mask(size, radius, scale=4):
    """超采样画圆角遮罩——PIL 的圆角在直角处没有抗锯齿，放大画再缩回来才不毛刺。"""
    w, h = size
    big = Image.new("L", (w * scale, h * scale), 0)
    ImageDraw.Draw(big).rounded_rectangle([0, 0, w * scale - 1, h * scale - 1],
                                          radius=radius * scale, fill=255)
    return big.resize((w, h), Image.LANCZOS)


def cutout_mascot(src, white_cut=234.0, feather=0.8):
    """白底贴纸 → 透明底：连通背景剔除 + 空洞回填 + 只留最大连通域。

    注意不要用「最暗通道当 alpha」那套白转透做法——这套画除了纯白还有大量中明度
    浅蓝，那套做法会把它们整片变半透明（放到深色背景上像是掉色）。这里用
    二值前景（略微内缩，顺带吃掉边上一圈白边）+ 轻羽化出抗锯齿。
    """
    im = Image.open(src).convert("RGB")
    rgb = np.asarray(im).astype(np.float32)
    h, w, _ = rgb.shape

    # 从四边泛洪：只有连到画布外的白才是背景，被轮廓围住的白色（围裙、发带）算实体
    bg = rgb.min(axis=2) >= white_cut
    outside = np.zeros((h, w), bool)
    stack = [(y, x) for x in range(w) for y in (0, h - 1) if bg[y, x]]
    stack += [(y, x) for y in range(h) for x in (0, w - 1) if bg[y, x] and not outside[y, x]]
    for y, x in stack:
        outside[y, x] = True
    while stack:
        y, x = stack.pop()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            yy, xx = y + dy, x + dx
            if 0 <= yy < h and 0 <= xx < w and bg[yy, xx] and not outside[yy, xx]:
                outside[yy, xx] = True
                stack.append((yy, xx))
    fg = ~outside

    try:  # 剔除与主体不相连的漂浮元素（贴纸里的问号、汗滴、手写文字）
        import cv2

        n, labels, stats, _ = cv2.connectedComponentsWithStats(fg.astype(np.uint8), 8)
        if n > 1:
            main = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            fg = labels == main
    except ImportError:
        print("  ! 未装 opencv，跳过浮标剔除")

    alpha = Image.fromarray((fg * 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(feather))
    out = np.dstack([rgb, np.asarray(alpha)]).astype(np.uint8)
    ys, xs = np.where(np.asarray(alpha) > 24)
    crop = Image.fromarray(out, "RGBA").crop((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1))
    print(f"  ✓ 抠图 {im.size} → {crop.size}（前景占比 {fg.mean():.1%}）")
    return crop


def fit_square(img, size, pad_ratio=0.06):
    """等比缩放 + 居中放进正方形画布（用于 README 顶部图标，尺寸固定不跳动）。"""
    inner = int(size * (1 - pad_ratio * 2))
    scale = min(inner / img.width, inner / img.height)
    resized = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(resized, ((size - resized.width) // 2, (size - resized.height) // 2), resized)
    return canvas


def polish_shot(src, pad_ratio=0.10, radius_ratio=0.038):
    """原始截图 → 展示图：深海渐变画布 + 呼吸光 + 光束 + 圆角卡片 + 投影。"""
    shot = Image.open(src).convert("RGB")
    w, h = shot.size
    pad = int(min(w, h) * pad_ratio)
    radius = max(12, int(min(w, h) * radius_ratio))
    canvas = _gradient((w + pad * 2, h + pad * 2), ABYSS["bg_top"], ABYSS["bg_mid"], ABYSS["bg_bot"])
    canvas = _add_glow(canvas, (canvas.width * 0.5, pad + h * 0.28), max(w, h) * 0.95, ABYSS["p1"], 0.38)
    canvas = _add_beams(canvas, 2)

    shadow = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(shadow).rounded_rectangle(
        [pad, pad + int(pad * 0.35), pad + w, pad + h + int(pad * 0.35)], radius, fill=170)
    shadow = shadow.filter(ImageFilter.GaussianBlur(pad * 0.55))
    shadow_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow_layer.putalpha(shadow)
    canvas = Image.alpha_composite(canvas, shadow_layer)

    card = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    card.paste(shot, (0, 0))
    card.putalpha(_rounded_mask((w, h), radius))
    canvas.paste(card, (pad, pad), card)

    ring = ImageDraw.Draw(canvas)
    ring.rounded_rectangle([pad, pad, pad + w - 1, pad + h - 1], radius, outline=ABYSS["p2"] + (70,), width=2)
    return canvas.convert("RGB")


def _fit_text(path, text, size, max_w, min_size=16):
    """把字号收紧到指定宽度内，避免长句被右侧立绘压掉尾部。"""
    while size > min_size:
        f = _font(path, size)
        if f.getlength(text) <= max_w:
            return f
        size -= 2
    return _font(path, min_size)


def make_banner(mascot, size=(1280, 640)):
    """社交预览横幅：深海底 + 吉祥物立绘 + 名称与一句话简介。"""
    w, h = size
    canvas = _gradient(size, ABYSS["bg_top"], ABYSS["bg_mid"], ABYSS["bg_bot"])
    canvas = _add_glow(canvas, (w * 0.78, h * 0.62), w * 0.42, ABYSS["p1"], 0.42)
    canvas = _add_beams(canvas, 2)
    canvas = _add_bubbles(canvas, 16)

    mh = int(h * 0.92)
    mw = int(mascot.width * mh / mascot.height)
    art = mascot.resize((mw, mh), Image.LANCZOS)
    art_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    art_layer.paste(art, (w - mw - int(w * 0.015), h - mh), art)
    canvas = Image.alpha_composite(canvas, art_layer)

    d = ImageDraw.Draw(canvas)
    x = int(w * 0.065)
    max_w = w - mw - x - int(w * 0.06)
    f_big, f_sub = _font(FONT_BOLD, 96), _font(FONT_BOLD, 46)
    y_big = int(h * 0.20)
    d.text((x, y_big), "小漓", font=f_big, fill=ABYSS["text"])
    d.text((x + f_big.getlength("小漓") + 26, y_big + 46), "蓝色大肥鱼", font=f_sub, fill=ABYSS["p2"])
    d.text((x, int(h * 0.46)), "微信 AI 机器人 · 视觉版",
           font=_fit_text(FONT_BOLD, "微信 AI 机器人 · 视觉版", 38, max_w), fill=ABYSS["text"])
    d.text((x, int(h * 0.575)), "免 UIA / CDP 的视觉 OCR 通道",
           font=_fit_text(FONT_REG, "免 UIA / CDP 的视觉 OCR 通道", 28, max_w), fill=ABYSS["muted"])
    d.text((x, int(h * 0.66)), "自动回复 · 语音发送 · 任务桥 · 长记忆 · 联网搜索",
           font=_fit_text(FONT_REG, "自动回复 · 语音发送 · 任务桥 · 长记忆 · 联网搜索", 28, max_w),
           fill=ABYSS["muted"])
    return canvas.convert("RGB")


def _save(img, out, **kw):
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    img.save(out, **kw)
    print(f"  ✓ {os.path.relpath(out, ROOT)}  {img.size}  {os.path.getsize(out) / 1024:.0f} KB")


def main(argv):
    assets = os.path.join(ROOT, "assets")
    shots = os.path.join(assets, "raw")
    cmd = argv[1] if len(argv) > 1 else "all"

    if cmd in ("all", "mascot"):
        src = argv[2] if len(argv) > 2 else os.path.join(ROOT, "小漓.png")
        out_dir = argv[3] if len(argv) > 3 else assets
        print("吉祥物抠图…")
        mascot = cutout_mascot(src)
        _save(fit_square(mascot, 384), os.path.join(out_dir, "mascot.png"), optimize=True)
        _save(mascot.resize((700, int(mascot.height * 700 / mascot.width)), Image.LANCZOS),
              os.path.join(out_dir, "mascot-full.png"), optimize=True)
        mascot.convert("RGB").save(os.path.join(ROOT, ".rivet", "scratch", "mascot_check_white.png"))
        Image.alpha_composite(Image.new("RGBA", mascot.size, (11, 37, 64, 255)), mascot).convert("RGB").save(
            os.path.join(ROOT, ".rivet", "scratch", "mascot_check_dark.png"))

    if cmd in ("all", "shot"):
        src = argv[2] if len(argv) > 2 else os.path.join(shots, "wechat-voice.png")
        out = argv[3] if len(argv) > 3 else os.path.join(assets, "showcase-voice.png")
        print("截图美化…")
        _save(polish_shot(src), out, optimize=True)

    if cmd in ("all", "banner"):
        src = argv[2] if len(argv) > 2 else os.path.join(assets, "mascot-full.png")
        out = argv[3] if len(argv) > 3 else os.path.join(assets, "social-preview.png")
        print("横幅合成…")
        _save(make_banner(Image.open(src).convert("RGBA")), out, optimize=True)


if __name__ == "__main__":
    main(sys.argv)
