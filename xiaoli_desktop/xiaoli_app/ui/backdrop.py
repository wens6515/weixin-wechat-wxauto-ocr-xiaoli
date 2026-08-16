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
from PySide6.QtCore import Qt, QPointF, QRectF, QTimer
from PySide6.QtGui import (QColor, QPainter, QPixmap, QRadialGradient,
                           QLinearGradient, QBrush, QPainterPath)
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
        """切换主题：重建粒子（颜色/数量随主题），重绘背景。"""
        self._theme_key = key
        self._theme = THEMES.get(key, THEMES["blue"])
        self._build_particles()
        self.update()

    def _theme_alpha_range(self):
        group = self._theme.get("group", "浅色")
        return _PARTICLE_ALPHA.get(group, _PARTICLE_ALPHA["浅色"])

    def _build_particles(self):
        import random
        if not self._theme:
            return
        w, h = max(self.width(), 1), max(self.height(), 1)
        glow = _hex_rgb(self._theme.get("glow", self._theme["p1"]))
        p1 = _hex_rgb(self._theme["p1"])
        p2 = _hex_rgb(self._theme["p2"])
        colors = [glow, p1, p2]
        lo, hi = self._theme_alpha_range()
        self._particles = []
        for _ in range(_PARTICLE_COUNT):
            x = random.uniform(0, w)
            y = random.uniform(0, h)
            r = random.uniform(_PARTICLE_R_MIN, _PARTICLE_R_MAX)
            ang = random.uniform(0, 6.283)
            v = _PARTICLE_SPEED * random.uniform(0.5, 1.5)
            self._particles.append({
                "x": x, "y": y, "r": r,
                "vx": v * random.uniform(-1, 1),
                "vy": v * random.uniform(-1, 1),
                "alpha": random.uniform(lo, hi),
                "color": random.choice(colors),
            })

    def _tick(self):
        if not self.isVisible() or not self._particles:
            return
        w, h = self.width(), self.height()
        for p in self._particles:
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

        # 粒子（壁纸模式下提亮，保证可见）
        alpha_boost = 1.6 if not self._wallpaper.isNull() else 1.0
        for pt in self._particles:
            c = QColor(pt["color"])
            c.setAlphaF(min(0.5, pt["alpha"] * alpha_boost))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(c))
            p.drawEllipse(QPointF(pt["x"], pt["y"]), pt["r"], pt["r"])
        p.end()
