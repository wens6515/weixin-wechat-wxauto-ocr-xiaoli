# -*- coding: utf-8 -*-
"""bot 后台守护线程（用户定案模型：只算时间 / 抓页面 / 调压缩模型，绝不
触碰微信窗口——发送与窗口操作全部发生在主循环节点内）。

- ReminderScheduler  定时触发器扫描：到期条目推入队列
- ConditionWatcher   状态监视轮询：抓固定网页 → 本地关键词/api 判定 → 事件入队
- MemoryCompressor   长记忆压缩：深层记忆攒批 → 提炼重要记忆 / 关键词索引

从 xiaoli_bot.py 拆出（纯移动，零逻辑改动）；xiaoli_bot 显式 re-import。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

from wechat_bot import VISION_MODEL_DEFAULT
from xiaoli_app.web_search import web_fetch

logger = logging.getLogger("xiaoli")

class ReminderScheduler(threading.Thread):
    """定时消息闹钟线程（用户定案模型）：只算时间、只投队列，绝不触碰
    微信窗口——发送必须发生在主循环节点内（与红圈扫描/成果回传共享微信
    窗口，闹钟线程发消息会撞车，_sending_lock 先例同理）。

    每 ≤scan_seconds 重扫一次 store：到期条目推入队列；(id, fire_at)
    内存去重防重发；扫描间隔同时兜住设置页的增删改（文件为事实源）。"""

    def __init__(self, store, out_queue, stop_event=None, scan_seconds=5.0):
        super().__init__(name="xiaoli-reminders", daemon=True)
        self.store = store
        self.out_queue = out_queue
        # 注意不可命名为 _stop——threading.Thread.join 内部调用 self._stop()
        self._stop_evt = stop_event if stop_event is not None else threading.Event()
        self._scan_seconds = scan_seconds
        self._claimed = set()

    def run(self):
        while not self._stop_evt.is_set():
            try:
                now = time.time()
                pending, _missed = self.store.due(now)
                for r in pending:
                    key = (r.get("id"), r.get("fire_at"))
                    if key in self._claimed:
                        continue
                    self._claimed.add(key)
                    self.out_queue.put(r)
                    logger.info(f"[定时] 到期入队: {r.get('chat')} <- {str(r.get('content'))[:40]}")
                if len(self._claimed) > 512:
                    self._claimed = {k for k in self._claimed
                                     if (k[1] or 0) >= now - 86400}
            except Exception as e:
                logger.error(f"[定时] 调度扫描失败: {e}")
            self._stop_evt.wait(self._scan_seconds)

# =====================================================================
# 条件监视（set_reminder kind=condition）：轮询固定 URL → 判定 → 事件入队
# =====================================================================

# 连续抓取/判定异常次数达到该值判 dead（页面改版/长期失效），提前走到期回递
WATCH_MAX_FAILS = 10
# absent 型本地判定的最小片段长度：切片/整页短于该值视为异常（防验证页/
# 空壳页被误判「关键词已消失」）
WATCH_MIN_SLICE_CHARS = 20
# api 判定喂给模型的页面文本上限
WATCH_EVIDENCE_CHARS = 3000

WATCH_JUDGE_PROMPT = (
    "你是状态监视判定器。判断给定网页文本中，用户关心的条件**当前**是否已经达成。"
    "注意：页面可能同时包含当前实况与未来时段（预报）的内容——只依据当前时段判断，"
    "未来时段的内容一律不算数。只输出一个 JSON 对象，不要输出任何其他文字：\n"
    '{"met": true 或 false, "reason": "一句话依据（引用页面里的原文短语）"}'
)


def parse_watch_json(raw):
    """解析 api 判定输出。任何异常返回 (None, "")——调用方按异常计次，不误判。"""
    if not raw:
        return None, ""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw).strip(), flags=re.S)
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return None, ""
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None, ""
    if not isinstance(data, dict) or "met" not in data:
        return None, ""
    met = data.get("met")
    if not isinstance(met, bool):
        return None, ""  # "yes"/"true" 等非布尔输出按解析失败计（宁可异常重试，不误判达成）
    return met, str(data.get("reason") or "").strip()


class ConditionWatcher(threading.Thread):
    """条件监视线程（用户定案模型）：轮询固定 URL + 判定 + 事件入队，绝不
    碰微信窗口（发送在主循环 _drain_conditions 节点）。

    - bot.paused 或 state_watch_enabled=False：只等待不轮询（不烧 API）；
      expire_at 是固定截止时刻，暂停不顺延——恢复后已过截止直接走到期回递
    - 抓取失败/切片标记缺失/判定异常 → fail_count 累计，达 WATCH_MAX_FAILS
      判 dead 提前终止（防页面改版后干烧）
    - 每轮串行处理（一个监视一轮），判定调用走 _post_chat_completions
      label="watch"（同一唯一 API 链路）"""

    def __init__(self, bot, stop_event=None, scan_seconds=5.0):
        super().__init__(name="xiaoli-condition-watch", daemon=True)
        self.bot = bot
        self._stop_evt = stop_event if stop_event is not None else threading.Event()
        self._scan_seconds = scan_seconds

    def run(self):
        while not self._stop_evt.is_set():
            try:
                # 轮询闸：全局开关开，或存在条件监视条目（全局关 + 有条目 =
                # 用户按聊天单独开的强制开覆盖，条目即事实源）
                if self.bot._state_watch_polling_active() \
                        and not self.bot.paused:
                    self._scan_once()
            except Exception as e:
                logger.error(f"[状态监视] 扫描异常: {e}")
            self._stop_evt.wait(self._scan_seconds)

    def _scan_once(self):
        bot = self.bot
        now = time.time()
        for r in bot.reminders.list_conditions():
            expire_at = r.get("expire_at") or 0
            if expire_at and now >= expire_at:
                # 固定截止（非倒计时）：暂停期间照常流逝，恢复后已过期即回递
                evidence = self._final_evidence(r)
                bot.reminders.finish_condition(r["id"], "expired", evidence)
                bot._condition_queue.put({
                    "type": "expired", "chat": r.get("chat"),
                    "condition": r.get("condition"),
                    "evidence": evidence,
                })
                logger.info(f"[状态监视] 到期未达成: {r.get('condition')!r}")
                continue
            if now < (r.get("next_check_at") or 0):
                continue
            self._check_once(r, now)

    def _check_once(self, r, now):
        bot = self.bot
        interval = int(r.get("interval_seconds") or 60)
        try:
            text = web_fetch(r.get("url") or "")
        except Exception as e:
            logger.debug(f"[状态监视] 抓取失败 {r.get('url')!r}: {e}")
            text = None
            seg = None
        else:
            seg = self._slice(text, r.get("scope_start"), r.get("scope_end"))
            if seg is None:
                # 切片标记在页面里找不到（改版/异常页）——按异常计次，
                # 绝不整页兜底（用户实验定案：整页匹配会被未来预报误触发）
                logger.warning(f"[状态监视] 切片标记未命中 "
                               f"{r.get('scope_start')!r}~{r.get('scope_end')!r}")
        met, evidence = (None, "")
        if text is not None and seg is not None:
            if r.get("judge") == "api":
                met, evidence = self._judge_api(r, text)
            else:
                met, evidence = self._judge_local(r, seg)
        if met is None:
            fails = bot.reminders.record_fail(r["id"])
            if fails >= WATCH_MAX_FAILS:
                bot.reminders.finish_condition(
                    r["id"], "dead", "监控的网页连续无法访问或内容结构变化")
                bot._condition_queue.put({
                    "type": "dead", "chat": r.get("chat"),
                    "condition": r.get("condition"),
                    "evidence": "监控的网页连续无法访问或内容结构变化",
                })
                logger.warning(f"[状态监视] 连续失效判 dead: {r.get('condition')!r}")
            else:
                bot.reminders.schedule_next(r["id"], now + interval)
            return
        if met:
            bot.reminders.finish_condition(r["id"], "met", evidence)
            bot._condition_queue.put({
                "type": "met", "chat": r.get("chat"),
                "condition": r.get("condition"),
                "evidence": evidence,
            })
            logger.info(f"[状态监视] 条件达成: {r.get('condition')!r} ({evidence[:50]})")
        else:
            bot.reminders.schedule_next(r["id"], now + interval, reset_fail=True)

    @staticmethod
    def _slice(text, start, end):
        """按创建时模型给的标记切出当前时段片段。任一标记给了但找不到 →
        None（异常）；两个都没给 → 整页（模型确认过页面只有单时段）。"""
        seg = text
        if start:
            i = seg.find(start)
            if i < 0:
                return None
            seg = seg[i:]
        if end:
            j = seg.find(end, len(start) if start else 0)
            if j < 0:
                return None
            seg = seg[:j]
        return seg

    def _judge_local(self, r, seg):
        """本地关键词判定（零 API）：present=出现任一关键词；absent=关键词
        全部消失（等雨停）。absent 有最小片段长度守卫——验证页/空壳页会
        被「关键词不出现」误判成达成，片段过短按异常处理（present 出现
        即命中，短页面不影响判定）。"""
        kws = [str(k) for k in (r.get("met_keywords") or []) if str(k)]
        if not kws:
            return None, ""
        low = seg.lower()
        hit = [k for k in kws if k.lower() in low]
        if r.get("match_type") == "absent":
            if len(seg) < WATCH_MIN_SLICE_CHARS:
                return None, ""
            if hit:
                return False, f"片段内仍有: {hit[0]}"
            excerpt = seg[:120].replace("\n", " ")
            return True, f"关键词 {kws} 已不在当前时段出现。片段：{excerpt}"
        if not hit:
            return False, ""
        i = low.find(hit[0].lower())
        excerpt = seg[max(0, i - 40):i + 80].replace("\n", " ")
        return True, f"命中关键词 {hit[0]}：…{excerpt}…"

    def _judge_api(self, r, text):
        """api 判定（每次轮询一次小调用，走唯一链路 label=watch）。
        调用失败/解析失败返回 (None, "")——按异常计次，不误判达成。"""
        bot = self.bot
        model = bot.chat_model or VISION_MODEL_DEFAULT
        headers = {"Authorization": f"Bearer {bot.api_key}",
                   "Content-Type": "application/json"}
        messages = [
            {"role": "system", "content": WATCH_JUDGE_PROMPT},
            {"role": "user", "content":
                f"用户关心的条件：{r.get('condition')}\n"
                f"（达成后要做的事：{r.get('content')}）\n\n网页文本：\n"
                + text[:WATCH_EVIDENCE_CHARS]},
        ]
        payload = {"model": model, "messages": messages,
                   "temperature": 0, "max_tokens": 200}
        try:
            data = bot._post_chat_completions(
                bot.api_url, headers, payload, 60, label="watch",
                meta={"kind": "watch", "model": model, "messages": messages})
            raw = (data.get("choices") or [{}])[0].get("message", {}) \
                .get("content", "")
        except Exception as e:
            logger.warning(f"[状态监视] api 判定调用失败（计异常）: {e}")
            return None, ""
        met, reason = parse_watch_json(raw)
        if met is None:
            return None, ""
        return met, (reason or ("模型判定达成" if met else ""))

    def _final_evidence(self, r):
        """到期回递前尽力抓一次页面，给出「到期时的状况」摘要。"""
        try:
            text = web_fetch(r.get("url") or "")
        except Exception:
            return "（到期时页面无法访问）"
        seg = self._slice(text, r.get("scope_start"), r.get("scope_end"))
        seg = seg if seg is not None else text
        return (f"页面片段：{seg[:200]}".replace("\n", " "))


# 长记忆压缩：提炼「必须记住的重要记忆 + 关键词记忆索引」（一次调用双产出）。
# 每攒满 memory_compress_batch 条深层消息触发一次；输入带上已有条目供模型
# 去重（不重复提炼、不重复建索引）。
MEMORY_COMPRESS_PROMPT = (
    "你是微信机器人小漓的记忆整理器。下面是与某位联系人的一段聊天记录"
    "（按时间顺序，[用户]/[小漓] 标记说话方）。请完成两件事：\n"
    "1. 提炼「必须记住的重要记忆」：身份信息、长期偏好、重要承诺或约定、"
    "重大事件、称呼与关系变化等长期有效的信息。只提炼确实重要且长期有效的，"
    "每条不超过 50 字；已给出的重要记忆里已有的不要再输出；没有就输出空数组。\n"
    "2. 为这段记录配置「关键词记忆索引」：挑出用户日后可能再次提起的话题，"
    "每个话题给 2-5 个具体检索关键词（人名/事件/物品名等，不要宽泛词如"
    "「聊天」「事情」），并写一段对应的记忆描述（保留关键细节，不超过 80 字）。\n"
    "只输出一个 JSON 对象，不要输出任何其他文字：\n"
    '{"important": [{"content": "重要记忆"}], "index": [{"kw": ["关键词1", "关键词2"], "mem": "记忆描述"}]}'
)


def parse_compress_json(raw):
    """解析压缩模型输出。任何异常/缺字段都兜底为空产出（不中断压缩循环）。"""
    if not raw:
        return [], []
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw).strip(), flags=re.S)
    m = re.search(r"\{.*\}", text, flags=re.S)
    data = None
    if m:
        try:
            data = json.loads(m.group(0))
        except Exception:
            data = None
    if not isinstance(data, dict):
        return [], []
    important = []
    for item in (data.get("important") or []):
        if isinstance(item, dict) and str(item.get("content") or "").strip():
            important.append({"content": str(item["content"]).strip()})
        elif isinstance(item, str) and item.strip():
            important.append({"content": item.strip()})
    index = []
    for item in (data.get("index") or []):
        if not isinstance(item, dict):
            continue
        kws = [str(k).strip() for k in (item.get("kw") or item.get("keywords") or [])
               if str(k).strip()]
        mem = str(item.get("mem") or item.get("memory") or "").strip()
        if kws and mem:
            index.append({"kw": kws, "mem": mem})
    return important, index


class MemoryCompressor(threading.Thread):
    """长记忆压缩线程：扫描各聊天的深层记忆增量，攒满 batch 条调压缩模型
    提炼「重要记忆 + 关键词索引」，结果经 bot.memory_commit_compression
    锁内写回。API 调用不持锁（只持锁快照段与写回结果）；每轮最多压缩一个
    聊天（串行 API，失败不重试——下轮扫描重新尝试同一段，索引边界不推进）。"""

    def __init__(self, bot, stop_event=None, scan_seconds=20.0):
        super().__init__(name="xiaoli-memory-compress", daemon=True)
        self.bot = bot
        self._stop_evt = stop_event if stop_event is not None else threading.Event()
        self._scan_seconds = scan_seconds

    def run(self):
        while not self._stop_evt.is_set():
            try:
                self._scan_once()
            except Exception as e:
                logger.error(f"[记忆压缩] 扫描异常: {e}")
            self._stop_evt.wait(self._scan_seconds)

    def _scan_once(self):
        bot = self.bot
        if not getattr(bot, "memory_compress_enabled", False):
            return
        if not getattr(bot, "memory_deep_enabled", False):
            return  # 深层记忆关着则没有溢出归档，无从压缩
        batch = int(bot.memory_compress_batch)
        with bot._memory_lock:
            chats = list(bot.memory_db.keys())
        for chat in chats:
            st = bot.memory_db.get(chat)
            if not isinstance(st, dict):
                continue
            total = int(bot._deep_count.get(chat, 0))
            if total - int(st.get("indexed") or 0) < batch:
                continue
            self._compress_chat(chat)
            return  # 每轮最多压缩一个聊天（串行 API 调用）

    def _compress_chat(self, chat):
        bot = self.bot
        st = bot.memory_db.get(chat) or {}
        indexed = int(st.get("indexed") or 0)
        msgs, consumed = bot._read_deep_range(
            chat, indexed, int(bot.memory_compress_batch))
        if not msgs:
            # 文件行数与计数不一致（外部改动）：把边界推进到计数处防死循环
            bot.memory_commit_compression(
                chat, int(bot._deep_count.get(chat, 0)), [], [])
            return
        existing_imp = [str(x.get("content") or "").strip()
                        for x in (st.get("important") or [])]
        existing_kw = [str(k)
                       for e in (st.get("index") or [])
                       for k in (e.get("kw") or [])]
        parts = []
        if existing_imp:
            parts.append("已有重要记忆（勿重复提炼）：\n"
                         + "\n".join(f"- {x}" for x in existing_imp if x))
        if existing_kw:
            parts.append("已有关键词（勿重复建索引）：\n"
                         + "、".join(dict.fromkeys(existing_kw)))
        parts.append("聊天记录：\n" + "\n".join(
            f"[{'用户' if m.get('role') == 'user' else '小漓'}]"
            f"[{m.get('time', '')}] {str(m.get('content') or '')[:300]}"
            for m in msgs))
        model = bot.memory_compress_model or bot.chat_model \
            or VISION_MODEL_DEFAULT
        headers = {"Authorization": f"Bearer {bot.api_key}",
                   "Content-Type": "application/json"}
        messages = [{"role": "system", "content": MEMORY_COMPRESS_PROMPT},
                    {"role": "user", "content": "\n\n".join(parts)}]
        payload = {"model": model, "messages": messages,
                   "temperature": 0.3, "max_tokens": 2000}
        try:
            data = bot._post_chat_completions(
                bot.api_url, headers, payload, 60, label="memory",
                meta={"kind": "memory", "model": model, "messages": messages})
            raw = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.warning(f"[记忆压缩] {chat} 调用失败（下轮重试）: {e}")
            return
        important, index = parse_compress_json(raw)
        bot.memory_commit_compression(chat, consumed, important, index)
        logger.info(f"[记忆压缩] {chat} 已压缩 {consumed - indexed} 条"
                    f"（重要 +{len(important)}，索引 +{len(index)}）")
