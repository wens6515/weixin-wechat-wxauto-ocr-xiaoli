# -*- coding: utf-8 -*-
"""背景层：柔和渐变 + 漂浮粒子光点。

唯一背景绘制者（替代旧 QSS 顶部光晕——那会在浅色主题下形成一条突兀的
「色带长条」）。QPainter 运行时绘制，支持：
- 基础：大半径低透明径向渐变（过渡平滑，不形成色带）
- aurora 主题：斜向三段弥散渐变
- 粒子：主题 glow/p1 色系半透明圆点，QTimer 驱动缓慢漂移（动态效果）

粒子数量/透明度由主题深浅控制：深色主题粒子更亮更多，浅色更淡。
性能：30 个圆 30fps + 抗锯齿，QPainter 开销可忽略。
"""
from PySide6.QtCore import Qt, QPointF, QRectF, QTimer, QVariantAnimation
from PySide6.QtGui import (QColor, QPainter, QPixmap, QRadialGradient,
                           QLinearGradient, QBrush, QPainterPath, QPen)
from PySide6.QtWidgets import QWidget

from . import THEMES

# 粒子配置（深色主题取上限，浅色取下限）
_PARTICLE_COUNT = 26
_PARTICLE_R_MIN = 1.2
_PARTICLE_R_MAX = 3.2
_PARTICLE_SPEED = 0.35  # px/frame
_PARTICLE_ALPHA = {"深色": (0.10, 0.22), "浅色": (0.05, 0.12)}


def _hex_rgb(hex_color: str) -> QColor:
    h = hex_color.lstrip("#")
    return QColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


class ParticleBackdrop(QWidget):
    """粒子背景层。透明鼠标事件，垫在最底层。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._theme_key = "blue"
        self._particles = []  # [x, y, r, vx, vy, alpha, color]
        self._theme = None   # 当前主题 dict（缓存避免每次查）
        self._wallpaper = QPixmap()  # 壁纸（空 = 纯渐变背景）
        self._corner_radius = 16  # 圆角裁剪（配合无边框圆角窗口）
        self._style = "bokeh"   # 粒子风格（主题 particle_style 键）
        self._phase = 0.0       # 全局动画相位（星点闪烁/气泡摆动/光束摇摆）
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(33)  # ~30fps

    def set_corner_radius(self, radius: int):
        """设置圆角半径（圆角窗口时背景按圆角裁剪）。"""
        self._corner_radius = radius
        self.update()

    def set_wallpaper(self, path: str):
        """设置壁纸图（空路径清除）。壁纸上叠加半透明遮罩 + 粒子，保证可读。"""
        import os
        if path and os.path.isfile(path):
            self._wallpaper = QPixmap(path)
        else:
            self._wallpaper = QPixmap()
        self.update()

    # ---------- 主题 ----------

    def set_theme(self, key: str):
        """切换主题：粒子颜色渐变过渡（位置保留），背景重绘。

        首次设置（无旧主题）直接重建；后续切换用 300ms QVariantAnimation
        把每粒子的颜色从旧主题插值到新主题（glow/p1/p2 色系），避免
        主题切换时背景粒子「唰」地变色的生硬感。
        """
        old_theme = self._theme
        self._theme_key = key
        self._theme = THEMES.get(key, THEMES["blue"])
        self._style = self._theme.get("particle_style", "bokeh")
        if self._style == "none":
            # 无粒子主题（墨染/简素）：清空粒子，纯背景
            self._particles = []
            self.update()
            return
        if old_theme is not None and self._particles:
            glow = _hex_rgb(self._theme.get("glow", self._theme["p1"]))
            p1 = _hex_rgb(self._theme["p1"])
            p2 = _hex_rgb(self._theme["p2"])
            targets = [glow, p1, p2]
            import random
            for pt in self._particles:
                pt["from"] = QColor(pt["color"])
                pt["to"] = random.choice(targets)
            self._color_anim = QVariantAnimation(self)
            self._color_anim.setDuration(300)
            self._color_anim.setStartValue(0.0)
            self._color_anim.setEndValue(1.0)
            self._color_anim.valueChanged.connect(self._apply_color_t)
            self._color_anim.start()
        else:
            self._build_particles()
        self.update()

    def _apply_color_t(self, t: float):
        """粒子颜色插值帧：from → to 线性混合（t∈[0,1]）。"""
        for pt in self._particles:
            f = pt.get("from")
            if f is None:
                continue
            to = pt["to"]
            c = QColor(
                int(f.red() + (to.red() - f.red()) * t),
                int(f.green() + (to.green() - f.green()) * t),
                int(f.blue() + (to.blue() - f.blue()) * t))
            pt["color"] = c
            if t >= 1.0:
                pt.pop("from", None)
                pt.pop("to", None)
        self.update()

    def _theme_alpha_range(self):
        group = self._theme.get("group", "浅色")
        return _PARTICLE_ALPHA.get(group, _PARTICLE_ALPHA["浅色"])

    def _build_particles(self):
        import random
        if not self._theme:
            return
        style = getattr(self, "_style", "bokeh")
        if style == "none":
            self._particles = []
            return
        w, h = max(self.width(), 1), max(self.height(), 1)
        glow = _hex_rgb(self._theme.get("glow", self._theme["p1"]))
        p1 = _hex_rgb(self._theme["p1"])
        p2 = _hex_rgb(self._theme["p2"])
        colors = [glow, p1, p2]
        lo, hi = self._theme_alpha_range()
        self._particles = []
        if style == "star":
            # 星夜紫金：稀疏星点，静止 + 呼吸闪烁（绘制期相位驱动）
            for _ in range(_PARTICLE_COUNT):
                self._particles.append({
                    "x": random.uniform(0, w), "y": random.uniform(0, h),
                    "r": random.uniform(0.8, 2.2), "vx": 0.0, "vy": 0.0,
                    "alpha": random.uniform(lo, hi) * 1.6,
                    "color": random.choice(colors),
                    "tw": random.uniform(0.6, 1.8),   # 闪烁频率
                    "ph": random.uniform(0, 6.283),   # 初相位
                })
            return
        if style == "scanline":
            # 霓虹夜行：上升数据微光（竖直短线上浮，出顶重生）
            for _ in range(_PARTICLE_COUNT):
                self._particles.append({
                    "x": random.uniform(0, w), "y": random.uniform(0, h),
                    "r": random.uniform(0.8, 1.4),
                    "len": random.uniform(8, 26),
                    "vx": 0.0,
                    "vy": -_PARTICLE_SPEED * random.uniform(1.2, 2.6),
                    "alpha": random.uniform(lo, hi) * 1.4,
                    "color": random.choice(colors),
                })
            return
        if style == "bubble":
            # 薄荷/深海：上浮气泡（缓慢上升 + 水平轻摆）
            for _ in range(_PARTICLE_COUNT):
                self._particles.append({
                    "x": random.uniform(0, w), "y": random.uniform(0, h),
                    "r": random.uniform(1.6, 4.2),
                    "vx": 0.0,
                    "vy": -_PARTICLE_SPEED * random.uniform(0.4, 1.1),
                    "alpha": random.uniform(lo, hi),
                    "color": random.choice(colors),
                    "ph": random.uniform(0, 6.283),
                    "tw": random.uniform(0.4, 1.0),
                })
            return
        # 默认 bokeh：全向缓慢漂移（历史行为，旧主题保持不变）
        for _ in range(_PARTICLE_COUNT):
            x = random.uniform(0, w)
            y = random.uniform(0, h)
            r = random.uniform(_PARTICLE_R_MIN, _PARTICLE_R_MAX)
            v = _PARTICLE_SPEED * random.uniform(0.5, 1.5)
            self._particles.append({
                "x": x, "y": y, "r": r,
                "vx": v * random.uniform(-1, 1),
                "vy": v * random.uniform(-1, 1),
                "alpha": random.uniform(lo, hi),
                "color": random.choice(colors),
            })

    def _tick(self):
        if not self.isVisible():
            return
        self._phase += 0.05  # 全局相位推进（星点闪烁/气泡摆动/光束摇摆共用）
        style = getattr(self, "_style", "bokeh")
        if not self._particles:
            if self._theme and self._theme.get("light_beam"):
                self.update()  # 无粒子但有光束：仍需重绘摇摆
            return
        w, h = self.width(), self.height()
        import math, random
        for p in self._particles:
            if style == "star":
                continue  # 星点静止，仅绘制期闪烁
            if style == "bubble":
                p["y"] += p["vy"]
                p["x"] += math.sin(self._phase * p.get("tw", 0.6) + p.get("ph", 0.0)) * 0.25
                if p["y"] < -12:
                    p["y"] = h + 12
                    p["x"] = random.uniform(0, w)
                continue
            if style == "scanline":
                p["y"] += p["vy"]
                if p["y"] < -30:
                    p["y"] = h + 10
                    p["x"] = random.uniform(0, w)
                continue
            # 默认漂移
            p["x"] += p["vx"]
            p["y"] += p["vy"]
            # 边缘回绕（柔和进出）
            if p["x"] < -10:
                p["x"] = w + 10
            elif p["x"] > w + 10:
                p["x"] = -10
            if p["y"] < -10:
                p["y"] = h + 10
            elif p["y"] > h + 10:
                p["y"] = -10
        self.update()

    # ---------- 绘制 ----------

    def _draw_light_beams(self, p, w, h):
        """深海主题丁达尔光束：两条自顶部斜下的渐变光带，随相位轻摆。"""
        import math
        sway = math.sin(self._phase * 0.35) * 0.06
        for i, (cx, bw, alpha) in enumerate(((w * 0.28, w * 0.16, 0.10),
                                            (w * 0.62, w * 0.10, 0.07))):
            off = sway * w * (1 if i == 0 else -1)
            path = QPainterPath()
            path.moveTo(cx - bw * 0.5 + off, -4)
            path.lineTo(cx + bw * 0.5 + off, -4)
            path.lineTo(cx + bw * 1.8 + off * 1.6, h + 4)
            path.lineTo(cx - bw * 1.8 + off * 1.6, h + 4)
            path.closeSubpath()
            grad = QLinearGradient(QPointF(cx, 0), QPointF(cx, h))
            c = _hex_rgb(self._theme.get("glow", self._theme["p2"]))
            c.setAlphaF(alpha)
            grad.setColorAt(0.0, c)
            c2 = QColor(c)
            c2.setAlphaF(0.0)
            grad.setColorAt(0.85, c2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(grad))
            p.drawPath(path)

    def paintEvent(self, event):
        if not self._theme:
            self._theme = THEMES.get(self._theme_key, THEMES["blue"])
        t = self._theme
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        # 圆角裁剪（圆角窗口）：先按圆角路径裁剪，再绘制壁纸/渐变/粒子
        if self._corner_radius > 0:
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(0, 0, w, h), self._corner_radius, self._corner_radius)
            p.setClipPath(clip)

        # 1. 壁纸铺底（所有主题共用，包括 aurora——之前 aurora 分支漏画壁纸导致
        #    「背景被剔除」）
        has_wp = not self._wallpaper.isNull()
        if has_wp:
            p.drawPixmap(0, 0, self._wallpaper.scaled(
                w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation))
            veil = _hex_rgb(t["bg"])
            veil.setAlphaF(0.45)
            p.fillRect(QRectF(0, 0, w, h), QBrush(veil))
        else:
            # 无壁纸：兜底纯 bg
            p.fillRect(QRectF(0, 0, w, h), _hex_rgb(t["bg"]))

        # 2. 主题背景层
        if self._theme_key == "aurora":
            # 斜向三段弥散渐变（紫→淡紫→粉），半透明叠加在壁纸上
            grad = QLinearGradient(QPointF(0, 0), QPointF(w, h))
            p1 = _hex_rgb(t["p1"])
            glow = _hex_rgb(t.get("glow", t["p2"]))
            p2 = _hex_rgb(t["p2"])
            p1.setAlphaF(0.42)
            glow.setAlphaF(0.34)
            p2.setAlphaF(0.30)
            grad.setColorAt(0.0, p1)
            grad.setColorAt(0.45, glow)
            grad.setColorAt(0.75, p2)
            grad.setColorAt(1.0, _hex_rgb(t["bg"]))
            p.fillRect(QRectF(0, 0, w, h), QBrush(grad))
        else:
            # 大半径柔和径向光晕：中心偏上、半径=0.9 倍对角 → 过渡平滑不形成色带
            grad = QRadialGradient(QPointF(w * 0.5, h * 0.32),
                                   max(w, h) * 0.9)
            c = _hex_rgb(t["p1"])
            alpha = 0.16 if t.get("group", "浅色") == "深色" else 0.08
            c.setAlphaF(alpha)
            grad.setColorAt(0.0, c)
            bg = _hex_rgb(t["bg"])
            bg.setAlphaF(0.0)
            grad.setColorAt(0.6, bg)
            grad.setColorAt(1.0, _hex_rgb(t["bg"]))
            p.fillRect(QRectF(0, 0, w, h), QBrush(grad))

        # 深海小漓：顶部斜射丁达尔光束（随相位缓慢摇摆）
        if self._theme.get("light_beam"):
            self._draw_light_beams(p, w, h)

        # 粒子（壁纸模式下提亮，保证可见）
        style = getattr(self, "_style", "bokeh")
        alpha_boost = 1.6 if not self._wallpaper.isNull() else 1.0
        import math
        for pt in self._particles:
            c = QColor(pt["color"])
            a = pt["alpha"] * alpha_boost
            if style == "star":
                a *= 0.55 + 0.45 * math.sin(self._phase * pt.get("tw", 1.0) + pt.get("ph", 0.0))
            c.setAlphaF(min(0.5, max(0.0, a)))
            p.setBrush(QBrush(c))
            if style == "scanline":
                pen = QPen(c)
                pen.setWidthF(max(0.8, pt["r"]))
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.drawLine(QPointF(pt["x"], pt["y"]),
                           QPointF(pt["x"], pt["y"] + pt.get("len", 14)))
                p.setPen(Qt.PenStyle.NoPen)
            elif style == "bubble":
                # 气泡：半透明填充 + 浅色描边环
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QPointF(pt["x"], pt["y"]), pt["r"], pt["r"])
                ring = QColor(c)
                ring.setAlphaF(min(0.65, c.alphaF() + 0.18))
                pen = QPen(ring)
                pen.setWidthF(0.8)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(pt["x"], pt["y"]), pt["r"], pt["r"])
                p.setPen(Qt.PenStyle.NoPen)
            else:
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QPointF(pt["x"], pt["y"]), pt["r"], pt["r"])
        p.end()
