# -*- coding: utf-8 -*-
"""小漓状态层（默认关，用户开关）：给模型注入一条「此刻状态」背景，让语气
随时段/聊天热度/近期情绪自然起伏——不是台词，模型只在自然时机流露。

三块数据两来源（成本口径用户定案）：
- 时段精力、聊天热度：**本地计算，零 API 成本**（时钟 + memory_db 现成数据）；
- 情绪 + 精力损耗：**模型定量**——每次回复完整发出后，后台小调用（异步，
  不阻塞主循环、不碰微信窗口）读最近对话输出 {mood, energy}，落盘供下一
  轮注入；每聊天 5 分钟节流，失败静默保留旧状态。
存储：memory_file 同目录 persona_state.json（{chat_key: {mood, energy, ts}}）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from xiaoli_app.memory_store import memory_key

logger = logging.getLogger("xiaoli")

# 情绪新鲜度：超过该时长不再注入（旧情绪直接退场，不做数值衰减的戏）
FRESH_SECONDS = 6 * 3600
# 每聊天两次定量评估的最小间隔（秒）——每条回复都评估会让调用量翻倍
EVAL_THROTTLE_SECONDS = 300
# 精力本地衰减：距上次定量每 30 分钟 -4，下限 20——不让「昨天的心情/精力」
# 原样挂满 6 小时（衰减纯本地，零 API 成本）
ENERGY_DECAY_PER_30MIN = 4
ENERGY_FLOOR = 20

# mood 固定枚举（用户定案）：页面能按情绪上色、能做趋势统计；自由长句
# （「有点小失落但保持温柔」）不适合当状态值，也难以映射到具体语气。
MOOD_LABELS = (
    "开心", "平静", "聊得起劲", "被逗笑", "有点累",
    "有点委屈", "有点担心", "想撒娇", "有点敷衍", "心虚",
)

PERSONA_EVAL_PROMPT = (
    "你在追踪微信 AI 角色「小漓」此刻的情绪状态。下面是她与一位联系人的"
    "最近对话（含她刚发出的回复）。结合对话内容，定量她当前的情绪与精力"
    "（被夸了会开心、聊得投入会兴奋、对方冷淡会有点失落、深夜或长聊会累）。"
    "mood 只能从这些词里选一个：" + "、".join(MOOD_LABELS) + "。"
    "trigger 用一句话写这次情绪的触发点（如「对方刚夸了她」"
    "「聊到一半对方没回了」），没有就留空字符串。"
    "只输出一个 JSON 对象，不要输出任何其他文字：\n"
    '{"mood": "开心", "energy": 0到100的整数, "trigger": "一句话或空字符串"}'
)


def parse_state_json(raw):
    """解析定量输出 → (mood, energy, trigger)。任何异常返回 (None, None, "")。

    保留旧状态、不中断主流程；mood 不在枚举里也接受（旧数据/模型偶发
    用词不该让整次定量作废），页面按未知色显示。"""
    import re
    if not raw:
        return None, None, ""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw).strip(), flags=re.S)
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return None, None, ""
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None, None, ""
    if not isinstance(data, dict):
        return None, None, ""
    mood = str(data.get("mood") or "").strip() or None
    trigger = str(data.get("trigger") or "").strip()
    try:
        energy = int(data["energy"])
        if not 0 <= energy <= 100:
            energy = None
    except (KeyError, TypeError, ValueError):
        energy = None
    return mood, energy, trigger


class PersonaStateStore:
    """persona_state.json 读写（线程安全：后台评估线程写、引擎线程读）。"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def snapshot(self):
        """全部聊天状态快照 {chat_key: {mood, energy, ts}}（状态页/桥接用）。"""
        with self.lock:
            data = self._load()
        return {k: dict(v) for k, v in data.items()
                if isinstance(v, dict)}

    def get(self, chat):
        with self.lock:
            st = self._load().get(memory_key(chat)) if chat else None
        if not isinstance(st, dict):
            return None
        return st

    def set(self, chat, mood, energy, trigger=""):
        key = memory_key(chat)
        if not key:
            return
        with self.lock:
            data = self._load()
            old = data.get(key) if isinstance(data.get(key), dict) else {}
            hist = old.get("history")
            hist = list(hist) if isinstance(hist, list) else []
            ts = time.time()
            hist.append({"ts": ts, "mood": mood, "energy": energy})
            data[key] = {"mood": mood, "energy": energy,
                         "trigger": str(trigger or "").strip(),
                         "ts": ts, "history": hist[-30:]}
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)


def _store(bot):
    """按 bot.memory_file 现值取/建 store 实例（路径变化自动跟随）。"""
    base = os.path.dirname(os.path.abspath(bot.memory_file)) or "."
    path = os.path.join(base, "persona_state.json")
    inst = getattr(bot, "_persona_store_inst", None)
    if inst is None or inst.path != path:
        inst = PersonaStateStore(path)
        bot._persona_store_inst = inst
    return inst


def _period_label(hour):
    """本地时段 → 状态短句（零成本，永远可注入）。"""
    if 0 <= hour < 6:
        return "现在是深夜，你有点困"
    if 6 <= hour < 11:
        return "现在是早上，你精神不错"
    if 11 <= hour < 14:
        return "现在是中午"
    if 14 <= hour < 18:
        return "现在是下午"
    if 18 <= hour < 23:
        return "现在是晚上"
    return "夜深了"


# 时段短标签（状态页显示用；与 _period_label 同一边界口径）
PERIOD_LABELS = (
    (6, "深夜"), (11, "早上"), (14, "中午"), (18, "下午"), (23, "晚上"), (24, "深夜"),
)


def period_label(now=None):
    """当前时段短标签（深夜/早上/中午/下午/晚上）。"""
    t = time.localtime(now if now is not None else time.time())
    for edge, label in PERIOD_LABELS:
        if t.tm_hour < edge:
            return label
    return "深夜"


def effective_energy(st, now=None):
    """状态里的精力 → 当前有效精力（本地每 30 分钟 -4，下限 20）。

    纯本地衰减：定量调用 5 分钟才一次，用户看到的「精力」必须跟时间一起
    走，否则昨天 90 的精力会原样挂满 6 小时新鲜度窗口。"""
    try:
        base = int(st.get("energy"))
    except (TypeError, ValueError):
        return None
    base = max(0, min(100, base))
    try:
        ts = float(st.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    mins = max(0.0, ((time.time() if now is None else now) - ts) / 60.0)
    return max(ENERGY_FLOOR, base - int(mins // 30) * ENERGY_DECAY_PER_30MIN)


def _heat_tier(n):
    if n >= 60:
        return "聊得很热络"
    if n >= 20:
        return "聊得挺多"
    if n > 0:
        return "聊了几句"
    return "还没怎么聊"


def _behavior_hint(mood, energy):
    """状态 → 本轮可执行的行为约束（祈使句比形容词有效）。

    只调语气/主动性，明确不覆盖回复纪律（条数与长度纪律仍在 system 前缀里）。
    """
    if energy is not None and energy <= 30:
        return "这轮回得更短，少用颜文字；不主动开新话题，对方说完就收住。"
    if energy is not None and energy < 50:
        return "这轮别铺开：句子短一点，不主动追问。"
    if mood in ("有点委屈", "有点担心"):
        return "这轮语气软一点，先说自己这边的感受；不追问。"
    if mood == "想撒娇":
        return "这轮可以带一点撒娇的语气，但别叠太多颜文字。"
    if mood in ("聊得起劲", "被逗笑", "开心"):
        return "这轮可以活泼一点，但别超过两条。"
    return "这轮按平时语气，自然就好。"


def build_state_block(bot, chat_id):
    """组装注入正文：时段 + 真实当日热度 + 情绪/精力/诱因 + 行为约束。

    变化注入（A3）：状态签名（时段/热度档/心情/精力十位/诱因）与上次相同
    时只注入一行短标记——重复的长描述会被模型当模板文字忽略，变化本身才
    是信息。任何异常由调用方兜底（注入失败不影响回复）。"""
    today = time.strftime("%Y-%m-%d")
    n = 0
    try:
        fn = getattr(bot, "_today_msg_count", None)
        n = int(fn(chat_id) or 0) if fn is not None else 0
    except Exception:
        n = 0
    if not n:
        # 桩/旧 bot 没有计数器：回落到近期窗口计数（会封顶，但比重启归零好）
        try:
            for m in bot._get_history(chat_id):
                if str(m.get("time") or "").startswith(today):
                    n += 1
        except Exception:
            pass
    tier = _heat_tier(n)
    parts = [f"{period_label()} {time.strftime('%H:%M')}"]
    parts.append(f"今天聊了 {n} 条（{tier}）" if n else "今天还没怎么聊")
    st = None
    mood = None
    energy = None
    trigger = ""
    try:
        st = _store(bot).get(chat_id)
    except Exception:
        pass
    if isinstance(st, dict) \
            and time.time() - float(st.get("ts") or 0) < FRESH_SECONDS:
        mood = str(st.get("mood") or "").strip() or None
        energy = effective_energy(st)
        trigger = str(st.get("trigger") or "").strip()
        if mood:
            parts.append(f"心情：{mood}")
        if energy is not None:
            parts.append(f"精力 {energy}/100")
        if trigger:
            parts.append(f"起因：{trigger}")
    sig = (period_label(), tier, mood,
           None if energy is None else energy // 10, trigger)
    last = getattr(bot, "_persona_last_sig", None)
    if not isinstance(last, dict):
        last = bot._persona_last_sig = {}
    prev = last.get(chat_id)
    last[chat_id] = sig
    if prev == sig:
        # 状态没变：短标记足矣（避免同一段长描述每轮重复被忽略）
        return (f"[状态 {period_label()}·{mood or '—'}·"
                f"精力{energy if energy is not None else '—'}]")
    return ("[小漓此刻的状态｜只调语气，不改变事实与人设，"
            "也不覆盖回复纪律]\n"
            + "；".join(parts) + "。\n→ " + _behavior_hint(mood, energy))


def schedule_eval(bot, chat_id, reply_text):
    """回复完整发出后调用：节流过了就在后台线程做一次定量小调用。

    纯 API + 落盘，不碰微信窗口——可以安全地放后台线程（对齐 MemoryCompressor
    的线程纪律）。失败静默：定量是增强不是闸门。"""
    if not getattr(bot, "persona_state_enabled", False) or not chat_id:
        return
    now = time.time()
    last = getattr(bot, "_persona_eval_at", None)
    if last is None:
        bot._persona_eval_at = {}
        last = bot._persona_eval_at
    if now - last.get(chat_id, 0.0) < EVAL_THROTTLE_SECONDS:
        return
    last[chat_id] = now

    def worker():
        try:
            from wechat_bot import VISION_MODEL_DEFAULT, strip_model_prefix
            hist = []
            for m in bot._get_history(chat_id)[-8:]:
                role = "小漓" if m.get("role") == "assistant" else "用户"
                hist.append(f"[{role}] {str(m.get('content') or '')[:200]}")
            messages = [
                {"role": "system", "content": PERSONA_EVAL_PROMPT},
                {"role": "user", "content": "最近对话：\n" + "\n".join(hist)},
            ]
            with bot._model_lock:
                model = bot.chat_model or VISION_MODEL_DEFAULT
            payload = {"model": strip_model_prefix(model), "messages": messages,
                       "max_tokens": 100, "temperature": 0.3}
            data = bot._post_chat_completions(
                bot.api_url, {"Authorization": f"Bearer {bot.api_key}",
                              "Content-Type": "application/json"},
                payload, 60, label="persona",
                meta={"kind": "persona", "model": model, "messages": messages})
            raw = (data.get("choices") or [{}])[0].get("message", {}) \
                .get("content", "")
        except Exception as e:
            logger.debug(f"[状态层] 定量调用失败（保留旧状态）: {e}")
            return
        mood, energy, trigger = parse_state_json(raw)
        if mood is None and energy is None:
            return
        try:
            _store(bot).set(chat_id, mood, energy, trigger)
            logger.info(f"[状态层] {chat_id!r} mood={mood!r} energy={energy} "
                        f"trigger={trigger!r}")
        except Exception as e:
            logger.debug(f"[状态层] 状态落盘失败: {e}")

    threading.Thread(target=worker, daemon=True,
                     name="xiaoli-persona-eval").start()
