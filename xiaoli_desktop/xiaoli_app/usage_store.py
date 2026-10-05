# -*- coding: utf-8 -*-
"""用量统计存储：SQLite 单文件库（usage.db）+ 聚合查询（无 Qt 依赖，后端共用）。

每次 API 调用终态（成功/最终失败）记录一行：ts（epoch 秒）/kind
（chat|vision|reply|...）/model/tokens/ok/status/latency_ms 等。此前是
usage.jsonl 逐行 append + 读取侧全量 parse——记录量随使用年限线性膨胀，
用量页每次刷新三处复用全量文件的成本一起涨。现改 SQLite（标准库零依赖）：
写仍是单条 INSERT（低频），聚合走 SQL（WHERE 时间窗 + GROUP BY），老库
长期使用不再变慢。

迁移：构造时发现同目录旧 usage.jsonl → 事务内一次性导入 → 改名
usage.jsonl.imported 留档（不删除，用户可回溯/手动清理）。导入与改名都
幂等：库中已有数据只归档不再导入；导入半途失败不归档，下次完整重试。

埋点位置：wechat_bot.WeChatBot._post_chat_completions 的终态分支（一个
逻辑调用含内部重试只记一条；latency 口径 = 成功时只计成功那次尝试的耗时
（退避等待不计），失败时保留全程耗时——那反映的是用户真实等待）。
"""
import json
import logging
import os
import sqlite3
import threading
import time

logger = logging.getLogger("xiaoli")


def default_usage_path():
    """用量库默认位置：默认数据目录下 usage.db（与 memory.json 同目录）。"""
    from xiaoli_app.config_store import default_data_dir
    return os.path.join(default_data_dir(), "usage.db")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    kind TEXT,
    model TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    ok INTEGER,
    status INTEGER,
    latency_ms REAL,
    cache_hit INTEGER,
    cache_miss INTEGER,
    reasoning INTEGER,
    total_tokens INTEGER,
    src TEXT
)
"""

_INSERT = ("INSERT INTO usage (ts, kind, model, prompt_tokens, "
           "completion_tokens, ok, status, latency_ms, cache_hit, "
           "cache_miss, reasoning, total_tokens, src) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")

_COLS = ("ts, kind, model, prompt_tokens, completion_tokens, ok, status, "
         "latency_ms, cache_hit, cache_miss, reasoning, total_tokens, src")


def _empty_bucket():
    return {"calls": 0, "ok": 0, "fail": 0, "prompt": 0, "completion": 0,
            "latency_sum": 0.0, "cache_hit": 0, "cache_miss": 0,
            "reasoning": 0, "total": 0, "est_calls": 0}


def hit_ratio(bucket):
    """缓存命中率：hit/(hit+miss)。无缓存数据（旧记录/未启用缓存的模型
    两项全 0）返回 None——UI 显示「—」，不显示伪造的 0%。"""
    denom = bucket["cache_hit"] + bucket["cache_miss"]
    if denom <= 0:
        return None
    return bucket["cache_hit"] / denom


class UsageStore:
    """追加式用量记录 + SQL 聚合。线程安全（引擎线程写、UI 线程读；
    sqlite3 连接不跨线程复用，每次操作短连接 + 锁串行化）。"""

    def __init__(self, path=None):
        self.path = path or default_usage_path()
        # 旧 JSONL 存档路径：同目录 usage.jsonl（构造时一次性迁移，见
        # _migrate_legacy）
        self._legacy_path = os.path.join(
            os.path.dirname(self.path) or ".", "usage.jsonl")
        self._lock = threading.Lock()
        self._migrate_legacy()

    # ---------- 连接 ----------

    def _conn(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute(_SCHEMA)
        return conn

    @staticmethod
    def _row(rec):
        """记录 dict → INSERT 参数元组（字段名与旧 JSONL 行完全一致）。"""
        return (rec.get("ts"), rec.get("kind"), rec.get("model"),
                rec.get("prompt_tokens"), rec.get("completion_tokens"),
                1 if rec.get("ok") else 0, rec.get("status"),
                rec.get("latency_ms"), rec.get("cache_hit"),
                rec.get("cache_miss"), rec.get("reasoning"),
                rec.get("total_tokens"), rec.get("src"))

    # ---------- 写入 ----------

    def record(self, kind=None, model=None, prompt_tokens=None,
               completion_tokens=None, ok=True, status=None, latency_ms=None,
               cache_hit=0, cache_miss=0, reasoning=0, total_tokens=None,
               src=None, ts=None):
        """终态记录一条。写失败静默（统计永不阻塞消息主流程）。

        cache_hit/cache_miss：缓存命中/未命中 tokens（DeepSeek 顶层字段或
        OpenAI prompt_tokens_details 归一而来，见 wechat_bot._finish_usage）；
        reasoning：推理 tokens；total_tokens：响应 total；src：数据来源
        （"api"=响应实测 / "est"=本地估算）——旧记录无这些字段按缺省值读。
        ts：epoch 秒，缺省 now（测试/迁移锚定历史时间用）。"""
        rec = {
            "ts": float(ts) if ts is not None else time.time(),
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
            with self._lock:
                conn = self._conn()
                try:
                    conn.execute(_INSERT, self._row(rec))
                    conn.commit()
                finally:
                    conn.close()
        except (sqlite3.Error, OSError):
            pass
        return rec

    def clear(self):
        """清空全部用量记录（DELETE 全表，库文件与表结构保留）。

        返回 True = 已清空；False = 库不可用（Windows 下正被并发写入
        sqlite 超时/目录不可写），调用方据此提示稍后再试。"""
        with self._lock:
            try:
                conn = self._conn()
                try:
                    conn.execute("DELETE FROM usage")
                    conn.commit()
                finally:
                    conn.close()
                return True
            except (sqlite3.Error, OSError):
                return False

    # ---------- 读取 ----------

    def load_records(self, days=None):
        """原始记录列表（dict，字段名与旧 JSONL 行同构），按时间升序。

        days=None 全量；否则近 days 天。供用量页一次加载多处复用
        （summary / 估算消费 / 折线图）。"""
        sql = f"SELECT {_COLS} FROM usage"
        params = ()
        if days is not None:
            sql += " WHERE ts >= ?"
            params = (time.time() - days * 86400,)
        sql += " ORDER BY ts"
        try:
            with self._lock:
                conn = self._conn()
                try:
                    rows = conn.execute(sql, params).fetchall()
                finally:
                    conn.close()
        except (sqlite3.Error, OSError):
            return []
        out = []
        for r in rows:
            out.append({
                "ts": r[0], "kind": r[1], "model": r[2],
                "prompt_tokens": r[3], "completion_tokens": r[4],
                "ok": bool(r[5]), "status": r[6], "latency_ms": r[7],
                "cache_hit": r[8] or 0, "cache_miss": r[9] or 0,
                "reasoning": r[10] or 0, "total_tokens": r[11], "src": r[12],
            })
        return out

    # 旧 JSONL 时代的私有读取名：UI/调用方沿用，语义 = load_records()
    _load = load_records

    @staticmethod
    def _day_key(ts):
        return time.strftime("%Y-%m-%d", time.localtime(ts))

    def summary(self, days=7, records=None):
        """近 days 天聚合：total / today / by_day / by_model 四个视角。

        聚合走 SQL（时间窗 + 按「天×模型」GROUP BY，本地时区）；kind="reply"
        是端到端回复耗时记录（非 API 调用），不进调用/token 各桶，单独聚
        合进 reply_by_model（{model: {count, latency_sum}}）。token 缺失
        （记录时无法估算）按 0 计；缓存字段全 0 时 hit_ratio() 返回 None
        而非 0%。records：旧签名兼容参数（JSONL 时代的外部预读复用），
        现已忽略——SQL 直接范围查询。"""
        del records  # 兼容旧签名；实际数据经 SQL 范围查询
        start = time.time() - days * 86400
        total = _empty_bucket()
        by_day = {}
        by_model = {}
        reply_by_model = {}
        try:
            with self._lock:
                conn = self._conn()
                try:
                    rows = conn.execute(
                        "SELECT COALESCE(model, '未知') AS m, "
                        "date(ts, 'unixepoch', 'localtime') AS d, "
                        "COUNT(*) AS calls, "
                        "SUM(CASE WHEN ok THEN 1 ELSE 0 END), "
                        "SUM(CASE WHEN ok THEN 0 ELSE 1 END), "
                        "COALESCE(SUM(prompt_tokens), 0), "
                        "COALESCE(SUM(completion_tokens), 0), "
                        "COALESCE(SUM(latency_ms), 0), "
                        "COALESCE(SUM(cache_hit), 0), "
                        "COALESCE(SUM(cache_miss), 0), "
                        "COALESCE(SUM(reasoning), 0), "
                        "COALESCE(SUM(total_tokens), 0), "
                        "COALESCE(SUM(CASE WHEN src = 'est' THEN 1 ELSE 0 END), 0) "
                        "FROM usage WHERE kind IS NOT 'reply' AND ts >= ? "
                        "GROUP BY m, d",
                        (start,)).fetchall()
                    replies = conn.execute(
                        "SELECT COALESCE(model, '未知') AS m, COUNT(*), "
                        "COALESCE(SUM(latency_ms), 0) FROM usage "
                        "WHERE kind = 'reply' AND ts >= ? GROUP BY m",
                        (start,)).fetchall()
                finally:
                    conn.close()
        except (sqlite3.Error, OSError):
            rows, replies = [], []
        def _acc(bucket, calls, ok_n, fail_n, p, c, lat, ch, cm, rt, tt, est):
            bucket["calls"] += calls
            bucket["ok"] += ok_n
            bucket["fail"] += fail_n
            bucket["prompt"] += p
            bucket["completion"] += c
            bucket["latency_sum"] += lat
            bucket["cache_hit"] += ch
            bucket["cache_miss"] += cm
            bucket["reasoning"] += rt
            bucket["total"] += tt
            bucket["est_calls"] += est

        for (_m, d, calls, ok_n, fail_n, p, c, lat, ch, cm, rt, tt, est) in rows:
            _acc(total, calls, ok_n, fail_n, p, c, lat, ch, cm, rt, tt, est)
            _acc(by_day.setdefault(d, _empty_bucket()),
                 calls, ok_n, fail_n, p, c, lat, ch, cm, rt, tt, est)
            _acc(by_model.setdefault(_m, _empty_bucket()),
                 calls, ok_n, fail_n, p, c, lat, ch, cm, rt, tt, est)
        for (m, cnt, lat) in replies:
            b = reply_by_model.setdefault(m, {"count": 0, "latency_sum": 0.0})
            b["count"] += cnt
            b["latency_sum"] += lat
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

    # ---------- 旧 JSONL 一次性迁移 ----------

    def _migrate_legacy(self):
        """同目录旧 usage.jsonl → 事务导入 → 改名 .imported 留档。

        幂等：库已有数据（上次 COMMIT 成功但归档改名失败）只归档不再导入，
        绝不重复计费；导入半途失败不归档，下次完整重试。坏行跳过（与旧
        读取侧容错一致）。任何失败静默——统计存储迁移不阻塞启动。"""
        legacy = self._legacy_path
        if not os.path.isfile(legacy):
            return
        try:
            with self._lock:
                conn = self._conn()
                try:
                    has_rows = conn.execute(
                        "SELECT EXISTS(SELECT 1 FROM usage)").fetchone()[0]
                    if not has_rows:
                        rows = []
                        with open(legacy, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    rec = json.loads(line)
                                except ValueError:
                                    continue
                                if isinstance(rec, dict):
                                    rows.append(self._row(rec))
                        conn.executemany(_INSERT, rows)
                        conn.commit()
                    try:
                        os.replace(legacy, legacy + ".imported")
                    except OSError:
                        pass  # 归档失败下次再试（导入已幂等跳过）
                finally:
                    conn.close()
        except (sqlite3.Error, OSError) as e:
            logger.warning(
                f"[用量] 旧 usage.jsonl 迁移跳过（下次重试）: {e}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
