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
            "latency_sum": 0.0}


class UsageStore:
    """追加式用量记录 + 读取侧聚合。线程安全（引擎线程写、UI 线程读）。"""

    def __init__(self, path=None):
        self.path = path or default_usage_path()
        self._lock = threading.Lock()

    def record(self, kind=None, model=None, prompt_tokens=None,
               completion_tokens=None, ok=True, status=None, latency_ms=None):
        """终态记录一条。写失败静默（统计永不阻塞消息主流程）。"""
        rec = {
            "ts": time.time(),
            "kind": kind,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "ok": bool(ok),
            "status": status,
            "latency_ms": round(latency_ms) if latency_ms is not None else None,
        }
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return rec

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

        token 缺失（记录时无法估算）按 0 计，不影响调用数统计。
        """
        start = time.time() - days * 86400
        total = _empty_bucket()
        by_day = {}
        by_model = {}
        for r in self._load():
            ts = r.get("ts")
            if not isinstance(ts, (int, float)) or ts < start:
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
        today_key = self._day_key(time.time())
        return {
            "days": days,
            "total": total,
            "today": by_day.get(today_key, _empty_bucket()),
            "by_day": dict(sorted(by_day.items())),
            "by_model": dict(sorted(by_model.items(),
                                    key=lambda kv: -kv[1]["calls"])),
        }
