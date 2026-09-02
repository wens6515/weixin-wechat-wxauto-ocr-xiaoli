# -*- coding: utf-8 -*-
"""本地价目估算：内置版本化价目表 × 用量记录 = 估算消费。

设计边界（用户定案）：
- 只收录各平台**当前主推模型**的现行价（各官方定价页核实）：用户调用了
  表外模型 → 无价目 → 只计 token 不给估算（UI 显示「—」），绝不用旧价/
  均价编数字
- 有余额适配器的平台（DeepSeek 等）以平台真实余额为准，本表只给「按量
  折算」参考值，UI 必须标「估算」；价目可能过时（平台调价不会通知本表），
  config.json 的 price_overrides 可按模型覆盖，无需改代码
- 分时段（peak_spec）：DeepSeek 官方高峰=周一至周五 9:00-12:00、
  14:00-18:00（空闲=一半价）；硅基流动高峰=每日 0:00-2:00、8:00-24:00
  （空闲=2:00-8:00）。按记录 ts 的本地时钟落档
- cache_hit 缺省 = input 全价（无公开缓存价的模型不做折扣假设，估算偏高
  而不是凭空打折）；推理 tokens 是输出细分，不重复计价
"""
import time

PRICE_TABLE_VERSION = 2

# 单位：元 / 百万 token。来源：各平台官方定价页现行价（GLM-5.3-flash 为
# 限时活动口径，活动结束实付可能变化）；覆盖字段 price_overrides：
# {"模型名": {"input": ..., "cache_hit": ..., "output": ..., "off": {...}}}
_DS_PEAK = {"scope": "weekday", "ranges": [[9, 12], [14, 18]]}
_SF_PEAK = {"scope": "daily", "ranges": [[0, 2], [8, 24]]}
PRICES = {
    # DeepSeek 官方（api-docs.deepseek.com/zh-cn/quick_start/pricing）：
    # 空闲时段价格为高峰的一半
    "deepseek-v4-flash": {
        "input": 3.0, "cache_hit": 0.10, "output": 9.0,
        "off": {"input": 1.5, "cache_hit": 0.05, "output": 4.5},
        "peak": _DS_PEAK,
    },
    "deepseek-v4-pro": {
        "input": 9.0, "cache_hit": 0.30, "output": 27.0,
        "off": {"input": 4.5, "cache_hit": 0.15, "output": 13.5},
        "peak": _DS_PEAK,
    },
    "deepseek-v4-flash-vision-exp": {
        "input": 3.0, "cache_hit": 0.10, "output": 9.0,
        "off": {"input": 1.5, "cache_hit": 0.05, "output": 4.5},
        "peak": _DS_PEAK,
    },
    # 智谱 BigModel（glm-5.3 与前代同价；flash 为限时活动价口径）
    "glm-5.3": {"input": 8.0, "cache_hit": 2.0, "output": 28.0},
    "glm-5.3-flash": {"input": 0.8, "cache_hit": 0.16, "output": 2.8},
    # 月之暗面 Kimi 开放平台（K3：缓存命中 2 / 输入 20 / 输出 100）
    "kimi-k3": {"input": 20.0, "cache_hit": 2.0, "output": 100.0},
    # 通义百炼（Qwen3.8 系列，中国站价格）
    "qwen3.8-max": {"input": 12.0, "cache_hit": 1.5, "output": 36.0},
    "qwen3.8-flash": {"input": 0.8, "cache_hit": 0.1, "output": 2.7},
    # 火山方舟（豆包当前在售：旗舰 Evolving / 最低刊例 2.0-mini；
    # 无公开缓存价 → 命中按全价保守计）
    "doubao-seed-evolving": {"input": 6.0, "output": 30.0},
    "doubao-seed-2.0-mini": {"input": 0.2, "output": 2.0},
    # 硅基流动（只收 v4 系列；高峰=每日 0-2 点、8-24 点，空闲=2-8 点）
    "deepseek-ai/deepseek-v4-flash-0731": {
        "input": 3.0, "cache_hit": 0.30, "output": 9.0,
        "off": {"input": 1.5, "cache_hit": 0.15, "output": 4.5},
        "peak": _SF_PEAK,
    },
    "deepseek-ai/deepseek-v4-pro": {
        "input": 12.0, "cache_hit": 1.0, "output": 24.0,
    },
}

SOURCE_NOTE = "各平台官方现行价（内置，可能过时，可在设置里按模型覆盖）"


def _norm_model(model):
    """模型名归一化：剥「厂商:」前缀（用量记录里是 strip 后的纯名，兜底
    处理 UI 传入带前缀的形式）、去空白、忽略大小写。"""
    s = str(model or "").strip()
    if ":" in s:
        s = s.split(":", 1)[-1]
    return s.lower()


def _in_ranges(hour, ranges):
    return any(int(a) <= hour < int(b) for a, b in (ranges or []))


def _is_peak(entry, ts):
    """按记录 ts（本地时钟）判断是否高峰档。无 peak_spec = 全时段同价。"""
    spec = entry.get("peak")
    if not spec or not entry.get("off"):
        return True
    t = time.localtime(ts if ts is not None else time.time())
    if spec.get("scope") == "weekday" and t.tm_wday >= 5:
        return False  # 周末全天空闲
    return _in_ranges(t.tm_hour, spec.get("ranges"))


def price_for(model, overrides=None):
    """查模型价目条目（含 off/peak 分档结构）；无价目返回 None。
    overrides：{模型名: {input/cache_hit/output/off{...}}}——命中字段逐项
    覆盖内置值（off 子档同样逐项覆盖）。"""
    key = _norm_model(model)
    if not key:
        return None
    base = None
    for name, p in PRICES.items():
        if _norm_model(name) == key:
            base = json_copy(p)
            break
    if base is None:
        for name, p in (overrides or {}).items():
            if _norm_model(name) == key and isinstance(p, dict):
                base = {}
                break
    if base is None:
        return None
    for name, p in (overrides or {}).items():
        if _norm_model(name) == key and isinstance(p, dict):
            for f in ("input", "cache_hit", "output"):
                try:
                    v = float(p[f])
                    if v >= 0:
                        base[f] = v
                except (KeyError, TypeError, ValueError):
                    continue
            off = p.get("off")
            if isinstance(off, dict) and isinstance(base.get("off"), dict):
                for f in ("input", "cache_hit", "output"):
                    try:
                        v = float(off[f])
                        if v >= 0:
                            base["off"][f] = v
                    except (KeyError, TypeError, ValueError):
                        continue
    if "input" not in base or "output" not in base:
        return None
    return base


def json_copy(p):
    import json
    return json.loads(json.dumps(p))


def estimate_cost(model, prompt_tokens, completion_tokens, cache_hit_tokens=0,
                  overrides=None, ts=None):
    """单次调用费用估算（元）。无价目返回 None。ts：调用发生时刻（本地
    时间落高峰/空闲档，None=按当前时刻）。"""
    entry = price_for(model, overrides)
    if entry is None:
        return None
    if _is_peak(entry, ts) or not entry.get("off"):
        p = entry
    else:
        p = entry["off"]
    prompt = max(0, int(prompt_tokens or 0))
    completion = max(0, int(completion_tokens or 0))
    hit = min(max(0, int(cache_hit_tokens or 0)), prompt)
    miss = prompt - hit
    # 无公开缓存价的模型（cache_hit 键缺失）命中按全价保守计
    hit_price = p.get("cache_hit")
    if hit_price is None:
        hit_price = p["input"]
    return (miss / 1e6 * p["input"]
            + hit / 1e6 * hit_price
            + completion / 1e6 * p["output"])


def estimate_records(records, days=30, overrides=None):
    """用量记录列表 → 估算消费汇总 {total, today, by_day, priced, unpriced}。

    records：usage.jsonl 行（UsageStore._load 的输出，含 ts 供分档）。
    total 为 None 表示窗口内全部调用都没有价目（unpriced=条数）；部分有
    价目时只累加有价目的部分，unpriced 计条数供 UI 提示「N 条无价目未计入」。"""
    start = time.time() - days * 86400
    total = 0.0
    priced = 0
    unpriced = 0
    by_day = {}
    for r in records or []:
        ts = r.get("ts")
        if not isinstance(ts, (int, float)) or ts < start:
            continue
        cost = estimate_cost(r.get("model"), r.get("prompt_tokens") or 0,
                             r.get("completion_tokens") or 0,
                             r.get("cache_hit") or 0, overrides, ts=ts)
        if cost is None:
            unpriced += 1
            continue
        priced += 1
        total += cost
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        by_day[day] = by_day.get(day, 0.0) + cost
    today = time.strftime("%Y-%m-%d")
    return {
        "total": total if priced else None,
        "today": by_day.get(today, 0.0) if priced else None,
        "by_day": dict(sorted(by_day.items())),
        "priced": priced,
        "unpriced": unpriced,
    }
