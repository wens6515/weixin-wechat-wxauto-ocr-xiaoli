# -*- coding: utf-8 -*-
"""触发器存储：reminders.json CRUD + 到期扫描（无 Qt 依赖）。

数据以文件为唯一事实源：设置页（UI 线程）增删改、调度线程扫描、
引擎主循环确认触发，全部经本类的类级路径锁串行化，跨实例互斥。

统一触发器（v2）：一张表两种 kind，火线一致——time 到点 / condition 达成
或到期，都回递 API 生成角色内回复，不再有固定文案直发。

- kind="time"（定时）：id / chat / content / fire_at（epoch 秒）/
  repeat（once | daily | weekly）/ enabled / last_fired / missed。
  只做系统时间（wall clock）单轨——相对表达（「半小时后」）在创建那一刻
  折算成绝对时间点入库；系统时间天然抗睡眠/挂起，倒计时会漂移。
- kind="condition"（状态监视）：content / url（固定轮询网页，搜索引擎只在
  创建时用）/ condition（自然语言条件原文）/ judge（local | api）/
  match_type（present | absent）/ met_keywords / scope_start / scope_end
  （当前时段切片标记，防未来预报误触发）/ interval_seconds（下限 10）/
  expire_at（固定截止时刻，非倒计时，暂停不顺延）/ next_check_at /
  fail_count / done（None=进行中，"met"|"expired"|"dead"=终态）/ evidence。
"""
import json
import os
import threading
import time
import uuid

GRACE_SECONDS = 300  # 补发宽限：暂停/关机错过的提醒 5 分钟内照发，超过按错过处理
WATCH_INTERVAL_MIN = 10       # 条件监视轮询间隔下限（秒）
WATCH_DEFAULT_INTERVAL = 60   # 条件监视默认轮询间隔（秒）
WATCH_DEFAULT_TTL = 7 * 86400  # 条件监视默认截止：创建时刻 + 7 天（不填 expire_at 时）


def default_reminders_path():
    from xiaoli_app.config_store import default_data_dir
    return os.path.join(default_data_dir(), "reminders.json")


def _empty():
    return []


class RemindersStore:
    """reminders.json 读写 + 到期/错过分类。所有实例按路径共享一把锁。"""

    _path_locks: dict = {}

    def __init__(self, path=None):
        self.path = path or default_reminders_path()

    def _lock(self):
        return RemindersStore._path_locks.setdefault(self.path, threading.Lock())

    # ---------- 基础读写 ----------

    def list(self):
        try:
            with self._lock():
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _save(self, items):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def add(self, chat, content, fire_at, repeat="once"):
        item = {
            "id": uuid.uuid4().hex[:12],
            "kind": "time",
            "chat": str(chat),
            "content": str(content),
            "fire_at": float(fire_at),
            "repeat": repeat if repeat in ("once", "daily", "weekly") else "once",
            "enabled": True,
            "last_fired": None,
            "missed": False,
        }
        with self._lock():
            items = self._load_unlocked()
            items.append(item)
            self._save(items)
        return item

    def add_condition(self, chat, content, url, condition, judge="local",
                      match_type="present", met_keywords=None,
                      scope_start=None, scope_end=None,
                      interval_seconds=WATCH_DEFAULT_INTERVAL, expire_at=None):
        """登记条件监视（参数合法性由调用方校验，这里只做规范化兜底）。"""
        item = {
            "id": uuid.uuid4().hex[:12],
            "kind": "condition",
            "chat": str(chat),
            "content": str(content),
            "url": str(url or "").strip(),
            "condition": str(condition or "").strip(),
            "judge": judge if judge in ("local", "api") else "local",
            "match_type": match_type if match_type in ("present", "absent") else "present",
            "met_keywords": [str(k).strip() for k in (met_keywords or [])
                             if str(k).strip()],
            "scope_start": (str(scope_start).strip() or None) if scope_start else None,
            "scope_end": (str(scope_end).strip() or None) if scope_end else None,
            "interval_seconds": max(WATCH_INTERVAL_MIN, int(interval_seconds
                                                           or WATCH_DEFAULT_INTERVAL)),
            "expire_at": float(expire_at) if expire_at else None,
            "next_check_at": time.time(),
            "fail_count": 0,
            "enabled": True,
            "done": None,
            "evidence": None,
        }
        with self._lock():
            items = self._load_unlocked()
            items.append(item)
            self._save(items)
        return item

    def list_conditions(self, active_only=True):
        """条件监视条目快照（拷贝）。active_only=True 只返回进行中的。"""
        with self._lock():
            items = self._load_unlocked()
        out = []
        for r in items:
            if r.get("kind") != "condition":
                continue
            if active_only and (not r.get("enabled") or r.get("done")):
                continue
            out.append(dict(r))
        return out

    def remove(self, rid):
        with self._lock():
            items = self._load_unlocked()
            remain = [r for r in items if r.get("id") != rid]
            if len(remain) == len(items):
                return False
            self._save(remain)
            return True

    def set_enabled(self, rid, enabled):
        return self._mutate(rid, lambda r: r.update(enabled=bool(enabled)))

    # ---------- 到期扫描（调度线程调用） ----------

    def due(self, now=None, grace=GRACE_SECONDS):
        """到期分类。返回 (待发列表, 已错过并就地处理数)。

        待发：enabled 且 fire_at <= now 且 now - fire_at <= grace。
        错过（就地处理，不再返回）：单次 → disabled + missed 标记；
        周期 → fire_at 滚动到下一个未来周期。"""
        now = time.time() if now is None else now
        pending, missed = [], 0
        with self._lock():
            items = self._load_unlocked()
            changed = False
            for r in items:
                if not r.get("enabled") or r.get("kind") == "condition":
                    continue  # 条件监视由 list_conditions/ConditionWatcher 单独处理
                fire_at = r.get("fire_at") or 0
                if fire_at > now:
                    continue
                if now - fire_at <= grace:
                    pending.append(dict(r))
                else:
                    if r.get("repeat") in ("daily", "weekly"):
                        step = 86400 if r["repeat"] == "daily" else 7 * 86400
                        while r["fire_at"] <= now - grace:
                            r["fire_at"] += step
                    else:
                        r["enabled"] = False
                        r["missed"] = True
                    r["last_fired"] = None
                    missed += 1
                    changed = True
            if changed:
                self._save(items)
        return pending, missed

    def mark_fired(self, rid, now=None):
        """确认触发：单次 → disabled（保留记录，UI 可见）；周期 → 滚动到
        下一个未来时刻。发送失败也调用（出队即确认，避免死循环重发）。"""
        now = time.time() if now is None else now
        def _fn(r):
            r["last_fired"] = now
            if r.get("repeat") in ("daily", "weekly"):
                step = 86400 if r["repeat"] == "daily" else 7 * 86400
                # max：提前触发（fire_at 仍在未来）时下一期从原时刻顺延
                r["fire_at"] = max(r.get("fire_at") or now, now) + step
            else:
                r["enabled"] = False
        return self._mutate(rid, _fn)

    # ---------- 条件监视（ConditionWatcher 线程调用） ----------

    def schedule_next(self, rid, when, reset_fail=False):
        """推进下一次轮询时刻（未达成时调用）。reset_fail：抓取/判定恢复正常
        时清零连续失败计数。"""
        def _fn(r):
            r["next_check_at"] = float(when)
            if reset_fail:
                r["fail_count"] = 0
        return self._mutate(rid, _fn)

    def record_fail(self, rid):
        """轮询/判定异常计一次连续失败，返回累计值（达到阈值判 dead）。"""
        count = {"n": 0}
        def _fn(r):
            r["fail_count"] = int(r.get("fail_count") or 0) + 1
            count["n"] = r["fail_count"]
        self._mutate(rid, _fn)
        return count["n"]

    def finish_condition(self, rid, outcome, evidence=None):
        """条件监视终态：met（达成）/ expired（到截止未达成）/ dead（页面
        连续失效）。enabled=False 保留记录，UI 可见。"""
        assert outcome in ("met", "expired", "dead")
        def _fn(r):
            r["enabled"] = False
            r["done"] = outcome
            r["evidence"] = str(evidence or "")[:500] or None
        return self._mutate(rid, _fn)

    def _mutate(self, rid, fn):
        with self._lock():
            items = self._load_unlocked()
            for r in items:
                if r.get("id") == rid:
                    fn(r)
                    self._save(items)
                    return True
            return False

    def _load_unlocked(self):
        """调用方必须已持锁。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []
