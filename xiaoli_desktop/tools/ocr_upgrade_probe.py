# -*- coding: utf-8 -*-
"""OCR 引擎升级评估探针：同一批真机截图分别用 rapidocr_onnxruntime 1.4.4（现役）
与 rapidocr 3.x（PP-OCRv5）识别，输出逐行 diff + 置信度 + 耗时对比报告。

只读 PNG 截图文件，不连微信、不点击、不碰窗口。

用法（两个引擎装在同一个隔离 venv，包名不同可共存）：
  python -m venv .probe_ocr_venv
  .probe_ocr_venv/Scripts/pip install rapidocr_onnxruntime==1.4.4 rapidocr pillow numpy
  .probe_ocr_venv/Scripts/python tools/ocr_upgrade_probe.py capture   # 抓微信窗口截图
  .probe_ocr_venv/Scripts/python tools/ocr_upgrade_probe.py compare   # 双引擎对比出报告
"""
import difflib
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHOT_DIR = os.path.join(REPO_ROOT, ".probe_ocr")

# 消息区比例（与 wx_backend.visual_backend 默认标定一致；探针独立运行不 import 项目）
_MSG_REGION = (0.4165, 0.1288, 0.9913, 0.8337)


def cmd_capture(n=3, interval=1.5):
    """PrintWindow 抓当前微信窗口存 PNG（不改窗口状态、不置前、不点击）。"""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from wx_backend.visual_backend import capture_window, find_wechat_window
    hwnd = find_wechat_window()
    if not hwnd:
        raise SystemExit("未找到微信窗口")
    os.makedirs(SHOT_DIR, exist_ok=True)
    for i in range(n):
        shot = capture_window(hwnd)
        if shot is None:
            print(f"shot{i + 1}: 截图失败")
            continue
        path = os.path.join(SHOT_DIR, f"shot{i + 1}.png")
        shot.convert("RGB").save(path)
        print(f"shot{i + 1}: {shot.size[0]}x{shot.size[1]} -> {path}")
        time.sleep(interval)


def _to_bgr(pil_img):
    import numpy as np
    return np.asarray(pil_img.convert("RGB"))[:, :, ::-1].copy()


def _crop_message(pil_img):
    w, h = pil_img.size
    l, t, r, b = _MSG_REGION
    return pil_img.crop((int(w * l), int(h * t), int(w * r), int(h * b)))


def _engine_old():
    from rapidocr_onnxruntime import RapidOCR
    t0 = time.perf_counter()
    eng = RapidOCR(intra_op_num_threads=2)
    load_s = time.perf_counter() - t0

    def run(img):
        t0 = time.perf_counter()
        res, _elapse = eng(img)
        dt_ms = (time.perf_counter() - t0) * 1000
        items = []
        for box, text, score in (res or []):
            items.append(_norm_item(str(text), float(score), box))
        return items, dt_ms

    return "old(1.4.4)", load_s, run


def _engine_new():
    from rapidocr import RapidOCR
    t0 = time.perf_counter()
    eng = RapidOCR()
    load_s = time.perf_counter() - t0

    def run(img):
        t0 = time.perf_counter()
        out = eng(img)
        dt_ms = (time.perf_counter() - t0) * 1000
        if hasattr(out, "txts"):
            # rapidocr 3.x 返回 RapidOCROutput；boxes 是 numpy 数组（禁 `or` 真值判断）
            txts = out.txts if out.txts is not None else []
            scores = out.scores if out.scores is not None else []
            boxes = out.boxes if out.boxes is not None else []
            items = [_norm_item(str(t), float(s), b)
                     for t, s, b in zip(txts, scores, boxes)]
        else:
            res = out[0] if isinstance(out, tuple) else out
            items = [_norm_item(str(t), float(s), b) for b, t, s in (res or [])]
        return items, dt_ms

    return "new(3.x)", load_s, run


def _norm_item(text, score, box):
    """统一条目：box([[x,y],...4 点]) -> (text, score, x, y)。"""
    xs = [float(p[0]) for p in box]
    ys = [float(p[1]) for p in box]
    return (text, score, min(xs), min(ys))


def _sort_lines(items):
    """按 y 聚行（y 差 <12px 同行），行内按 x 排序拼接，输出 [(y, text)]。"""
    items = sorted(items, key=lambda it: (it[3], it[2]))
    lines = []
    for text, score, x, y in items:
        if lines and y - lines[-1][0] < 12:
            ly, ltext, lscore, ln = lines[-1]
            lines[-1] = (ly, f"{ltext}{text}", (lscore * ln + score) / (ln + 1), ln + 1)
        else:
            lines.append((y, text, score, 1))
    return [(round(y), t, round(s, 3)) for y, t, s, _n in lines]


def cmd_compare(repeats=3):
    engines = []
    for factory in (_engine_old, _engine_new):
        try:
            name, load_s, run = factory()
        except ImportError as e:
            print(f"[跳过] {factory.__name__}: {e}")
            continue
        engines.append((name, load_s, run))
    if len(engines) < 2:
        raise SystemExit("需要两个引擎都在当前 venv 可导入")

    shots = sorted(f for f in os.listdir(SHOT_DIR) if f.lower().endswith(".png"))
    if not shots:
        raise SystemExit(f"{SHOT_DIR} 下没有截图，先跑 capture")

    from PIL import Image
    report = []
    for shot_name in shots:
        img = Image.open(os.path.join(SHOT_DIR, shot_name))
        for region_name, region_img in (("message", _crop_message(img)),
                                        ("full", img)):
            report.append(f"== {shot_name} [{region_name}] ==")
            results = {}
            for name, load_s, run in engines:
                # 首跑预热（模型首次推理含初始化），取后续 repeats 次均值
                items, _ = run(_to_bgr(region_img))
                times = []
                for _ in range(repeats):
                    items, dt = run(_to_bgr(region_img))
                    times.append(dt)
                lines = _sort_lines(items)
                avg_conf = (sum(s for _y, _t, s in lines) / len(lines)) if lines else 0
                results[name] = lines
                report.append(
                    f"  {name}: {len(lines)} 行 | 平均置信度 {avg_conf:.3f} | "
                    f"平均 {sum(times) / len(times):.0f}ms（加载 {load_s:.1f}s）")
            names = [n for n, _l, _r in engines]
            if len(names) == 2:
                old_lines, new_lines = results[names[0]], results[names[1]]
                sm = difflib.SequenceMatcher(
                    a=[t for _y, t, _s in old_lines],
                    b=[t for _y, t, _s in new_lines], autojunk=False)
                n_equal = 0
                for tag, i1, i2, j1, j2 in sm.get_opcodes():
                    if tag == "equal":
                        n_equal += i2 - i1
                        continue
                    olds = old_lines[i1:i2]
                    news = new_lines[j1:j2]
                    if tag == "replace":
                        for k in range(max(len(olds), len(news))):
                            o = olds[k] if k < len(olds) else None
                            w = news[k] if k < len(news) else None
                            if o and w:
                                report.append(f"  [差异 y={o[0]}] {names[0]}: {o[1]!r}"
                                              f"  <->  {names[1]}: {w[1]!r}")
                            elif o:
                                report.append(f"  [仅{names[0]} y={o[0]}] {o[1]!r}")
                            elif w:
                                report.append(f"  [仅{names[1]} y={w[0]}] {w[1]!r}")
                    elif tag == "delete":
                        for o in olds:
                            report.append(f"  [仅{names[0]} y={o[0]}] {o[1]!r}")
                    elif tag == "insert":
                        for w in news:
                            report.append(f"  [仅{names[1]} y={w[0]}] {w[1]!r}")
                total = max(len(old_lines), len(new_lines), 1)
                report.append(
                    f"  一致行: {n_equal}/{total}"
                    f"（{names[0]} {len(old_lines)} 行 vs {names[1]} {len(new_lines)} 行）")
            report.append("")
    text = "\n".join(report)
    print(text)
    out_path = os.path.join(SHOT_DIR, "report.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"报告已保存: {out_path}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "compare"
    if cmd == "capture":
        cmd_capture()
    elif cmd == "compare":
        cmd_compare()
    else:
        print(__doc__)
