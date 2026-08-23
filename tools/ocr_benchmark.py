# -*- coding: utf-8 -*-
r"""OCR 性能基准探针：baseline vs intra_op_num_threads=2 vs OpenVINO(可选)。

背景：小漓.exe 发现新消息时 CPU 冲到 100%，根因是 RapidOCR(onnxruntime)
全核推理。本探针在同一张真机截图上对比三种引擎配置，量化耗时与识别
一致性，用于验证「限制 ONNX Runtime 线程数」是否满足性能 + 精度双目标。

配置：
  baseline  RapidOCR()                                  # 现状(全核)
  A1        RapidOCR(intra_op_num_threads=2)            # 限 2 核
  A2        rapidocr_openvino.RapidOCR()                # OpenVINO 后端(需安装，缺则跳过)

用法：
  python tools/ocr_benchmark.py                          # 自动抓微信主窗口截图
  python tools/ocr_benchmark.py --img shot.png           # 用指定截图
  python tools/ocr_benchmark.py --runs 5 --threads 2     # 自定义跑批次数 / 线程数
  python tools/ocr_benchmark.py --save shot.png          # 抓图并存盘(供后续 --img 复用)

测量协议：
  - 每配置预热 1 次(排除模型初始化 ~2s)，再跑 N 次取中位数
  - 两种负载(与生产路径等价)：
      a) 整窗 ocr_image(shot)
      b) 消息区 2x 分片 ocr_image_sharded(region_2x)   (visual_backend.get_messages 生产路径)
  - 记录 median(ms)、items 数；A1/A2 的文本与 baseline 逐项比对

精度判定(以 baseline 为基准)：
  - 文本匹配率 >= 0.98(_norm_cjk 清洗后，按 y 容差 20px 匹配)
  - 漏检率 <= 2%(baseline 有的项 A1/A2 没有)
  - 关键锚点零丢失：会话名/标题等短文本不能丢(匹配率之外的人工复核点)

输出：stdout 表格 + ocr_benchmark_result.json(含耗时明细与完整 items，供回归 diff)。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

# GBK 控制台兜底：不因打印非 ASCII 崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 探针从仓库根或 xiaoli_desktop 均可跑：确保 wx_backend 可导入
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "xiaoli_desktop"))

from wx_backend import visual_backend  # noqa: E402
from wx_backend.visual_backend import (  # noqa: E402
    _MESSAGE_REGION_RATIO,
    _norm_cjk,
    ocr_image,
    ocr_image_sharded,
)

OUT_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr_benchmark_result.json")


def find_wechat_hwnd():
    """复用 fix_window 的找窗逻辑：标题含'微信'且可见。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if "微信" in title and "WeChat" not in title:
            found.append((hwnd, title))
        return True

    user32.EnumWindows(_enum, 0)

    def _area(item):
        hwnd, _ = item
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        return (r.right - r.left) * (r.bottom - r.top)

    if not found:
        return None
    found.sort(key=_area, reverse=True)
    return found[0][0]


def grab_shot(save_path=None):
    """抓微信主窗口截图(PrintWindow + PW_RENDERFULLCONTENT，与生产一致)。"""
    hwnd = find_wechat_hwnd()
    if hwnd is None:
        print("[FAIL]  未找到微信主窗口，请用 --img 指定截图")
        sys.exit(2)
    print(f"[OK]  微信窗口 hwnd={hwnd}")
    shot = visual_backend.capture_window(hwnd)
    if shot is None:
        print("[FAIL]  capture_window 返回 None(窗口句柄失效？)")
        sys.exit(2)
    if save_path:
        shot.convert("RGB").save(save_path)
        print(f"[OK]  截图已存 {save_path} ({shot.size[0]}x{shot.size[1]})")
    return shot


def make_engines(threads: int):
    """构造三个引擎；A2 缺依赖时返回 None 占位并注明。"""
    from rapidocr_onnxruntime import RapidOCR

    engines = {
        "baseline": RapidOCR(),
        f"A1_threads{threads}": RapidOCR(intra_op_num_threads=threads),
        "A2_openvino": None,
    }
    try:
        from rapidocr_openvino import RapidOCR as VinoOCR

        engines["A2_openvino"] = VinoOCR()
    except Exception as e:  # noqa: BLE001
        print(f"[INFO]  OpenVINO 后端不可用(跳过 A2)：{type(e).__name__}: {e}")
    return engines


def run_once(engine, img):
    """对一张图跑一次 OCR，返回 (耗时秒, items)。"""
    t0 = time.perf_counter()
    items = ocr_image(img)
    dt = time.perf_counter() - t0
    return dt, items


def norm_key(it: dict, y_tol: int = 20):
    """规范化键：清洗文本 + y 分桶(用于跨配置文本比对)。"""
    return (round(it["y"] / y_tol), _norm_cjk((it.get("text") or "").strip()))


def compare_texts(baseline_items, other_items, label: str):
    """以 baseline 为基准：匹配率、漏检率、多余项。"""
    base_map = {norm_key(it): it["text"] for it in baseline_items}
    other_map = {norm_key(it): it["text"] for it in other_items}
    base_keys = set(base_map)
    other_keys = set(other_map)
    matched = base_keys & other_keys
    miss = base_keys - other_keys
    extra = other_keys - base_keys
    match_rate = len(matched) / len(base_keys) if base_keys else 1.0
    miss_rate = len(miss) / len(base_keys) if base_keys else 0.0
    return {
        "label": label,
        "match_rate": round(match_rate, 4),
        "miss_rate": round(miss_rate, 4),
        "n_base": len(base_keys),
        "n_matched": len(matched),
        "n_miss": len(miss),
        "n_extra": len(extra),
        "missed_texts": [base_map[k] for k in sorted(miss)][:10],
    }


def bench(engine, img, runs: int):
    """预热 1 次 + runs 次计时，返回 (median_ms, last_items)。"""
    run_once(engine, img)  # 预热(排除模型初始化)
    timings = []
    last_items = []
    for _ in range(runs):
        dt, items = run_once(engine, img)
        timings.append(dt * 1000)
        last_items = items
    return statistics.median(timings), last_items


def main():
    ap = argparse.ArgumentParser(description="OCR 性能基准探针")
    ap.add_argument("--img", help="指定截图 PNG(缺省自动抓微信窗口)")
    ap.add_argument("--save", help="抓图并存盘为 PNG")
    ap.add_argument("--runs", type=int, default=5, help="每配置跑批次数(默认 5)")
    ap.add_argument("--threads", type=int, default=2, help="A1 线程数(默认 2)")
    args = ap.parse_args()

    if args.img:
        from PIL import Image

        shot = Image.open(args.img).convert("RGB")
        print(f"[OK]  载入截图 {args.img} ({shot.size[0]}x{shot.size[1]})")
    else:
        shot = grab_shot(save_path=args.save)

    # 消息区 2x 分片(生产 get_messages 路径等价)
    w, h = shot.size
    region_1x = shot.crop((
        int(w * _MESSAGE_REGION_RATIO[0]), int(h * _MESSAGE_REGION_RATIO[1]),
        int(w * _MESSAGE_REGION_RATIO[2]), int(h * _MESSAGE_REGION_RATIO[3]),
    ))
    region_2x = region_1x.resize((region_1x.width * 2, region_1x.height * 2), 1)  # Image.LANCZOS

    engines = make_engines(args.threads)
    result = {"img": args.img or "live-wechat", "runs": args.runs,
              "threads": args.threads, "configs": {}}

    print(f"\n{'配置':<16}{'整窗 ms':>10}{'消息区2x ms':>14}{'整窗 items':>12}  (各 {args.runs} 次中位数)")
    for name, eng in engines.items():
        if eng is None:
            print(f"{name:<16}{'-':>10}{'-':>14}{'-':>12}  (OpenVINO 未安装，跳过)")
            result["configs"][name] = {"skipped": True}
            continue
        whole_med, whole_items = bench(eng, shot, args.runs)
        region_med, region_items = bench(eng, region_2x, args.runs)
        print(f"{name:<16}{whole_med:>10.1f}{region_med:>14.1f}{len(whole_items):>12}")
        result["configs"][name] = {
            "whole_median_ms": round(whole_med, 1),
            "region2x_median_ms": round(region_med, 1),
            "whole_items": len(whole_items),
            "region2x_items": len(region_items),
            "region2x_texts": [_norm_cjk(it["text"]) for it in region_items[:40]],
        }

    # 精度比对：以 baseline 为准(比对用整窗，避免分片坐标偏移干扰)
    base = result["configs"].get("baseline")
    if base:
        print("\n-- 文本一致性(vs baseline，y 容差 20px)--")
        _, base_items = bench(engines["baseline"], shot, 1)
        for name in result["configs"]:
            if name == "baseline" or result["configs"][name].get("skipped"):
                continue
            _, whole_items = bench(engines[name], shot, 1)
            cmp = compare_texts(base_items, whole_items, name)
            print(f"  {name}: 匹配率 {cmp['match_rate']:.2%}  漏检 {cmp['miss_rate']:.2%}"
                  f"  (base {cmp['n_base']} / 命中 {cmp['n_matched']} / 漏 {cmp['n_miss']} / 多 {cmp['n_extra']})")
            if cmp["missed_texts"]:
                print(f"    漏检样例: {cmp['missed_texts'][:5]}")
            result["configs"][name]["text_match_rate"] = cmp["match_rate"]
            result["configs"][name]["text_miss_rate"] = cmp["miss_rate"]
            result["configs"][name]["missed_texts"] = cmp["missed_texts"]

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n[OK]  结果已落盘 {OUT_JSON}")


if __name__ == "__main__":
    main()
