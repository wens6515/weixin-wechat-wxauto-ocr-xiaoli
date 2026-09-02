# -*- coding: utf-8 -*-
"""用量统计存储：LLM 调用逐条 JSONL 落盘 + 聚合查询（无 Qt 依赖，后端共用）。

每次 API 调用终态（成功/最终失败）记录一行：ts（epoch 秒）/kind
（chat|vision）/model/prompt_tokens/completion_tokens/ok/status/latency_ms。
文件 usage.jsonl 存默认数据目录（%USERPROFILE%\\小漓），行级 append 代价
极小；聚合在读取侧完成（本地文件量级小，全量读足够）。

埋点位置：wechat_bot.WeChatBot._post_chat_completions 的终态分支（一个
逻辑调用含内部重试只记一条，latency 为各次尝试之和）。
"""
import json
import os
import threading
import time


def default_usage_path():
    """用量文件默认位置：默认数据目录下 usage.jsonl（与 memory.json 同目录）。"""
    from xiaoli_app.config_store import default_data_dir
    return os.path.join(default_data_dir(), "usage.jsonl")


def _empty_bucket():
    return {"calls": 0, "ok": 0, "fail": 0, "prompt": 0, "completion": 0,
            "latency_sum": 0.0, "cache_hit": 0, "cache_miss": 0,
            "reasoning": 0, "total": 0}


def hit_ratio(bucket):
    """缓存命中率：hit/(hit+miss)。无缓存数据（旧记录/未启用缓存的模型
    两项全 0）返回 None——UI 显示「—」，不显示伪造的 0%。"""
    denom = bucket["cache_hit"] + bucket["cache_miss"]
    if denom <= 0:
        return None
    return bucket["cache_hit"] / denom


class UsageStore:
    """追加式用量记录 + 读取侧聚合。线程安全（引擎线程写、UI 线程读）。"""

    def __init__(self, path=None):
        self.path = path or default_usage_path()
        self._lock = threading.Lock()

    def record(self, kind=None, model=None, prompt_tokens=None,
               completion_tokens=None, ok=True, status=None, latency_ms=None,
               cache_hit=0, cache_miss=0, reasoning=0, total_tokens=None,
               src=None):
        """终态记录一条。写失败静默（统计永不阻塞消息主流程）。

        cache_hit/cache_miss：缓存命中/未命中 tokens（DeepSeek 顶层字段或
        OpenAI prompt_tokens_details 归一而来，见 wechat_bot._finish_usage）；
        reasoning：推理 tokens；total_tokens：响应 total；src：数据来源
        （"api"=响应实测 / "est"=本地估算）——旧记录无这些字段按缺省值读。"""
        rec = {
            "ts": time.time(),
            "kind": kind,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "ok": bool(ok),
            "status": status,
            "latency_ms": round(latency_ms) if latency_ms is not None else None,
            "cache_hit": int(cache_hit or 0),
            "cache_miss": int(cache_miss or 0),
            "reasoning": int(reasoning or 0),
            "total_tokens": total_tokens,
            "src": src,
        }
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return rec

    def clear(self):
        """清空全部用量记录：删除 usage.jsonl（下次 record 自动重建文件）。

        返回是否删除了文件；文件不存在（本就为空）与删除失败（Windows 下
        正被并发写入会 PermissionError）都返回 False，调用方据此提示。"""
        with self._lock:
            try:
                os.remove(self.path)
                return True
            except OSError:
                return False

    def _load(self):
        """全量读入（跳过坏行；文件不存在返回空表）。"""
        out = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict):
                        out.append(rec)
        except OSError:
            pass
        return out

    @staticmethod
    def _day_key(ts):
        return time.strftime("%Y-%m-%d", time.localtime(ts))

    def summary(self, days=7):
        """近 days 天聚合：total / today / by_day / by_model 四个视角。

        token 缺失（记录时无法估算）按 0 计，不影响调用数统计。缓存字段
        （cache_hit/cache_miss/reasoning/total）来自响应 usage 透传，旧记录
        按 0 读——命中率用 hit_ratio() 判「无数据」而非 0%。kind="reply"
        是端到端回复耗时记录（非 API 调用），不进调用/token 各桶，单独聚
        合进 reply_by_model（{model: {count, latency_sum}}）。"""
        start = time.time() - days * 86400
        total = _empty_bucket()
        by_day = {}
        by_model = {}
        reply_by_model = {}
        for r in self._load():
            ts = r.get("ts")
            if not isinstance(ts, (int, float)) or ts < start:
                continue
            if r.get("kind") == "reply":
                b = reply_by_model.setdefault(str(r.get("model") or "未知"),
                                              {"count": 0, "latency_sum": 0.0})
                b["count"] += 1
                b["latency_sum"] += (r.get("latency_ms") or 0)
                continue
            ok = bool(r.get("ok"))
            p = r.get("prompt_tokens") or 0
            c = r.get("completion_tokens") or 0
            d = by_day.setdefault(self._day_key(ts), _empty_bucket())
            m = by_model.setdefault(str(r.get("model") or "未知"), _empty_bucket())
            for bucket in (total, d, m):
                bucket["calls"] += 1
                bucket["ok" if ok else "fail"] += 1
                bucket["prompt"] += p
                bucket["completion"] += c
                bucket["latency_sum"] += (r.get("latency_ms") or 0)
                bucket["cache_hit"] += (r.get("cache_hit") or 0)
                bucket["cache_miss"] += (r.get("cache_miss") or 0)
                bucket["reasoning"] += (r.get("reasoning") or 0)
                bucket["total"] += (r.get("total_tokens") or 0)
        today_key = self._day_key(time.time())
        return {
            "days": days,
            "total": total,
            "today": by_day.get(today_key, _empty_bucket()),
            "by_day": dict(sorted(by_day.items())),
            "by_model": dict(sorted(by_model.items(),
                                    key=lambda kv: -kv[1]["calls"])),
            "reply_by_model": reply_by_model,
        }
