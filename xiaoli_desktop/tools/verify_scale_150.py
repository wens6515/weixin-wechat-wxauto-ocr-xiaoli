# -*- coding: utf-8 -*-
"""真机 150% 识别链路验证（被动）：走新代码的完整分析管线，不点击不切换。

对照 175% 标定链路预期：分界 464、头像 54、面板/引用/文件卡检出、
OCR 行成块。输出各环节中间量供人工核对。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from wx_backend import visual_scale as vs
from wx_backend.visual_win32 import (capture_window, find_wechat_window,
                                     monitor_dpi_scale, window_rect)
from wx_backend.visual_regions import regions_for_window, target_window_size
from wx_backend.visual_backend import (analyze_blocks, detect_bubble_colors,
                                       detect_avatar_tops, ocr_image,
                                       monitor_work_area)

vs.set_active_scale(monitor_dpi_scale(find_wechat_window()) or 175)
hwnd = find_wechat_window()
rect = window_rect(hwnd)
img = capture_window(hwnd).convert("RGB")
w, h = img.size
sr, mr, tr = regions_for_window(w, h, vs.active_scale())
print(f"scale={vs.active_scale()} rect={rect} shot={w}x{h}")
print(f"target_window_size(work)={target_window_size(monitor_work_area(hwnd))}")
print(f"regions ratios: msg=({mr[0]:.4f},{mr[1]:.4f},{mr[2]:.4f},{mr[3]:.4f})")
crop = img.crop((int(w * mr[0]), int(h * mr[1]),
                 int(w * mr[2]), int(h * mr[3])))
crop.save(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", ".scale_probe", "verify_region.png"))
colors = detect_bubble_colors(crop)
print(f"colors: bg={colors['bg']} other={colors['other']} "
      f"self={colors['self']} theme={colors['theme']}")
bot = detect_avatar_tops(crop, colors["bg"], "right")
other = detect_avatar_tops(crop, colors["bg"], "left")
print(f"avatar tops: right(bot)={bot} left(other)={other}")
info = analyze_blocks(crop, colors, bot, other)
for blk in info["blocks"]:
    print(f"  block top={blk['top']} bottom={blk['bottom']} kind={blk['kind']} "
          f"panel={blk['panel']} icon={blk['icon']} quote={blk['quote']} "
          f"quote_media={blk['quote_media']}")
lines = ocr_image(crop)
print(f"ocr lines: {len(lines)}")
for it in lines[:20]:
    print(f"  ({it['x']},{it['y']},{it['w']}x{it['h']}) {it['text']}")
print(f"has_text={info['has_text']} has_media={info['has_media']}")
