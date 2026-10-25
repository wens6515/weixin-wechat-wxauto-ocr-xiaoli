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

PERSONA_EVAL_PROMPT = (
    "你在追踪微信 AI 角色「小漓」此刻的情绪状态。下面是她与一位联系人的"
    "最近对话（含她刚发出的回复）。结合对话内容，定量她当前的情绪与精力"
    "（被夸了会开心、聊得投入会兴奋、对方冷淡会有点失落、深夜或长聊会累）。"
    "只输出一个 JSON 对象，不要输出任何其他文字：\n"
    '{"mood": "一个词或短语，如 开心/有点委屈/平静/聊得起劲/有点累", '
    '"energy": 0到100的整数（精力值）}'
)


def parse_state_json(raw):
    """解析定量输出。任何异常返回 (None, None)——保留旧状态，不中断主流程。"""
    import re
    if not raw:
        return None, None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw).strip(), flags=re.S)
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        return None, None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    mood = str(data.get("mood") or "").strip() or None
    try:
        energy = int(data["energy"])
        if not 0 <= energy <= 100:
            energy = None
    except (KeyError, TypeError, ValueError):
        energy = None
    return mood, energy


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

    def set(self, chat, mood, energy):
        key = memory_key(chat)
        if not key:
            return
        with self.lock:
            data = self._load()
            data[key] = {"mood": mood, "energy": energy,
                         "ts": time.time()}
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


def build_state_block(bot, chat_id):
    """组装注入正文（时段 + 热度 + 新鲜的模型定量）。返回 str（调用方注入）。

    只读 bot 数据不加锁重活：热度走 _get_history（内部有锁），定量走 store
    锁。任何异常由调用方兜底（注入失败不影响回复）。"""
    today = time.strftime("%Y-%m-%d")
    n = 0
    try:
        for m in bot._get_history(chat_id):
            if str(m.get("time") or "").startswith(today):
                n += 1
    except Exception:
        pass
    parts = [_period_label(time.localtime().tm_hour)]
    if n >= 20:
        parts.append(f"今天你们已经聊了 {n} 条，聊得很热络")
    elif n > 0:
        parts.append(f"今天聊了 {n} 条")
    else:
        parts.append("今天还没怎么聊")
    st = None
    try:
        st = _store(bot).get(chat_id)
    except Exception:
        pass
    if isinstance(st, dict) and time.time() - float(st.get("ts") or 0) < FRESH_SECONDS:
        mood = str(st.get("mood") or "").strip()
        try:
            energy = int(st.get("energy"))
        except (TypeError, ValueError):
            energy = None
        if mood:
            parts.append(f"你现在的心情：{mood}")
        if energy is not None and energy < 40:
            parts.append("精力有点跟不上了")
    return ("[小漓此刻的状态·背景感受，不要主动说出这些状态，"
            "只在语气自然的时候让它流露出来]\n" + "；".join(parts) + "。")


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
        mood, energy = parse_state_json(raw)
        if mood is None and energy is None:
            return
        try:
            _store(bot).set(chat_id, mood, energy)
            logger.info(f"[状态层] {chat_id!r} mood={mood!r} energy={energy}")
        except Exception as e:
            logger.debug(f"[状态层] 状态落盘失败: {e}")

    threading.Thread(target=worker, daemon=True,
                     name="xiaoli-persona-eval").start()
