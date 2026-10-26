# -*- coding: utf-8 -*-
"""visual_backend 拆分 · 缩放口径表：判据常量的跨缩放换算中心。

背景（150% 真机实测验证，见 tools/scale_probe.py 与立项文档）：微信的界面
布局按**逻辑像素**固定——头像、输入面板、列表/聊天分界列、行距的物理尺寸
= 逻辑值 × 显示缩放。全部像素判据在 175% 缩放的 1300×1610 窗口上真机标定
（逻辑窗口 743×920），因此把窗口**逻辑尺寸**钉在标定值（物理尺寸随档位
= 标定物理 × k）之后：

- 长度类判据（头像高、面板锚点、引用竖线偏移、图标尺寸闸……）× k = scale/175
- 面积类判据（红圈面积、胶囊绿像素数）× k²
- 比例判据（三区域、头像窄带、气泡高度过滤）与颜色判据（品牌红、气泡色、
  主题阈值）**不变**——逻辑布局与标定环境逐像素同构

运行档位由 VisualBackend 在 connect / 每轮几何复查时经 monitor_dpi_scale
实测并 set_active_scale 写入（多显示器跟随微信所在屏；跨屏拖动下轮复查
自动跟随）。未实测（测试桩 / 早期导入）默认 175 → k=1，全部判据与标定值
逐字节相同——既有测试与标定基线零漂移。
"""
from __future__ import annotations

# 标定档位（全部常量的真机标定环境）
CALIB_SCALE = 175

_ACTIVE_SCALE = CALIB_SCALE


def set_active_scale(pct) -> int:
    """写入当前生效档位（monitor_dpi_scale 的百分比读数）。

    非法值（0/None/异常）保持原档位不动——检测失效按上一档继续工作，
    不把判据打回未换算状态。返回生效档位。"""
    global _ACTIVE_SCALE
    try:
        pct = int(pct)
    except (TypeError, ValueError):
        return _ACTIVE_SCALE
    if pct >= 50:
        _ACTIVE_SCALE = pct
    return _ACTIVE_SCALE


def active_scale() -> int:
    """当前生效档位（百分比）。"""
    return _ACTIVE_SCALE


def k() -> float:
    """当前档位相对标定档位的线性缩放系数（175% → 1.0）。"""
    return _ACTIVE_SCALE / float(CALIB_SCALE)


def px(v: float) -> float:
    """175% 标定的长度值 → 当前档位物理像素（浮点，参与阈值比较用）。"""
    return v * k()


def pxr(v: float) -> int:
    """175% 标定的长度值 → 当前档位物理像素（四舍五入，且 ≥1——像素
    偏移/尺寸闸的整数口径）。"""
    return max(1, int(round(v * k())))


def area(v: float) -> float:
    """175% 标定的面积值（像素数）→ 当前档位物理像素数。"""
    kk = k()
    return v * kk * kk
