# -*- coding: utf-8 -*-
"""定时消息（提醒）存储：reminders.json CRUD + 到期扫描（无 Qt 依赖）。

数据以文件为唯一事实源：设置页（UI 线程）增删改、调度线程扫描、
引擎主循环确认触发，全部经本类的类级路径锁串行化，跨实例互斥。

字段：id / chat（目标聊天）/ content（提醒内容）/ fire_at（epoch 秒）/
repeat（once | daily | weekly）/ enabled / last_fired / missed。

触发模型（用户定案）：只做系统时间（wall clock）单轨——相对表达
（「半小时后」）在创建那一刻折算成绝对时间点入库，之后同一张表同一条
触发路径；系统时间天然抗睡眠/挂起，倒计时会漂移。
"""
import json
import os
import threading
import time
import uuid

GRACE_SECONDS = 300  # 补发宽限：暂停/关机错过的提醒 5 分钟内照发，超过按错过处理


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
                if not r.get("enabled"):
                    continue
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
