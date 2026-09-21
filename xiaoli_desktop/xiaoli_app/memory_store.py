# -*- coding: utf-8 -*-
"""对话记忆存储层：memory 键归一化 + memory.json v2 迁移 + 深层 jsonl 文件级操作。

从 wechat_bot 抽出的纯函数部分（无 bot 依赖，UI/CLI/绑定投影共用）。
wechat_bot 保留同名 re-export 保持既有导入路径（tests / 记忆管理页直接
from wechat_bot import 这些名字）。
"""
import json
import logging
import os
import re
import threading
import time

logger = logging.getLogger("xiaoli")

# memory 键归一化：OCR 对引号半/全角极不稳（'摸鱼”集团' / '摸鱼"集团' /
# '" 摸鱼 " 集团' 是同一会话），原样做键会让同一会话的记忆分裂到多个条目。
# 规则：所有引号变体（单双/半全角/弯直）与空格类字符一律剥掉再存取。
# 只影响 memory 键——显示名（回复目标/日志/群聊判定）不受影响。剥引号同时
# 覆盖「OCR 整个丢掉前引号」的漏字场景。per-chat 角色卡绑定键
# （config_store._project_chat_bindings）沿用同一口径。
_QUOTE_CHARS = (
    "\u201c\u201d\u2018\u2019\u201e\u201f"  # “ ” ‘ ’ „ ‟
    "\u00ab\u00bb\u2039\u203a"              # « » ‹ ›
    "\u300c\u300d\u300e\u300f"              # 「 」 『 』
    "\uff02\u02bc\u0060\u00b4\"'"           # ＂ ʼ ` ´ " '
)


def memory_key(chat_id):
    """memory 键归一化：剥掉所有引号变体与空格类字符。"""
    s = str(chat_id or "").translate(str.maketrans("", "", _QUOTE_CHARS))
    return re.sub(r"\s+", "", s)


def migrate_memory_data(data):
    """memory.json 结构迁移（v2）：{chat: {"recent": [...], "important":
    [...], "index": [...], "indexed": N}}。

    旧结构 {chat: [msgs]} 原位升级——旧消息整体作为 recent（超出 recent
    上限的部分由启动加载流程一次性归档进深层文件）；坏数据项返回空。
    """
    if not isinstance(data, dict):
        return {}
    out = {}
    for chat, v in data.items():
        if isinstance(v, dict) and isinstance(v.get("recent"), list):
            out[chat] = {
                "recent": v["recent"],
                "important": [x for x in (v.get("important") or [])
                              if isinstance(x, dict)],
                "index": [x for x in (v.get("index") or [])
                          if isinstance(x, dict)],
                "indexed": int(v.get("indexed") or 0),
            }
        elif isinstance(v, list):
            out[chat] = {"recent": v, "important": [], "index": [], "indexed": 0}
    return out


# ---------- 深层文件级操作（bot 未运行时 UI 直读/直删走这里，与 bot 方法共用） ----------

def deep_count_lines(path):
    """深层 jsonl 有效行数（坏行不计）。文件缺失返回 0。"""
    n = 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("{"):
                    n += 1
    except OSError:
        return 0
    return n


def deep_read_page(path, offset=0, limit=200, query=None):
    """深层 jsonl 分页/过滤读取。

    query 非空：全量按内容子串（不区分大小写）过滤，返回 (命中列表截
    200 条, 命中总数)；否则返回 (第 offset 页的 ≤limit 条, None)。
    """
    msgs = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    msgs.append(item)
    except OSError:
        return [], None
    if query:
        q = str(query).lower()
        hits = [m for m in msgs if q in str(m.get("content") or "").lower()]
        return hits[:200], len(hits)
    return msgs[offset:offset + limit], None


def deep_delete_line(path, line_no):
    """删除深层 jsonl 第 line_no（1 基，按文件序=时间正序）行。

    tmp + replace 原子重写。返回 (被删消息, 新有效行数)；行号越界/
    文件缺失/重写失败返回 (None, 原行数)。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None, 0
    kept, removed, n = [], None, 0
    for line in lines:
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            kept.append(line)  # 坏行原样保留
            continue
        n += 1
        if n == line_no:
            try:
                removed = json.loads(stripped)
            except ValueError:
                pass
            continue
        kept.append(line)
    if removed is None:
        return None, n
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(kept)
        os.replace(tmp, path)
    except OSError as e:
        logger.error(f"[记忆] 深层文件重写失败: {e}")
        return None, n
    return removed, n - 1


class MemoryStore:
    """对话记忆存储（从 WeChatBot 抽出）：v2 结构（recent/important/index/
    indexed）+ 深层 jsonl 永久存档 + 压缩产出写回 + 节流落盘。

    全部读写经 RLock 串行化（与压缩线程/UI 线程互斥）。容量策略经
    cap_fn(chat_id) 注入——per-chat 角色卡可覆盖 max_history，本类不感知
    角色卡。深层记忆开关（deep_enabled）由调用方按调用传入——开关是 bot
    的可热改属性，不在本类存快照。
    """

    def __init__(self, memory_file, cap_fn=None):
        self.memory_file = memory_file
        self._cap_fn = cap_fn or (lambda chat_id: 30)
        self.memory_db = {}
        self.deep_count = {}   # chat -> 深层文件行数（追加时维护，启动时清点）
        self.lock = threading.RLock()
        base = os.path.dirname(os.path.abspath(memory_file)) or "."
        self._deep_dir = os.path.join(base, "memory_deep")
        self._dirty = False
        self._last_save = 0.0

    def set_memory_file(self, value):
        """memory_file 变更（GUI 首启引导/测试夹具）：重算深层目录。"""
        self.memory_file = value
        base = os.path.dirname(os.path.abspath(value)) or "."
        self._deep_dir = os.path.join(base, "memory_deep")

    # ---------- 近期记忆（注入上下文的 recent 窗口） ----------

    def _resolve_key(self, chat_id):
        """把任意 OCR 变体解析到已存在的原文键；无已存在的则返回原文。

        键存的是**会话名原文**（不归一化——用户在记忆页看到的就是微信里
        的名字本身，「“摸鱼”集团」不再被剥成「摸鱼集团」）。

        但**查找**必须容忍 OCR 差异：同一会话可能被读成「“摸鱼”集团」
        「摸鱼”集团」「"摸鱼"集团」「" 摸鱼 " 集团」等。这里按 memory 键
        口径做等价类匹配，命中即复用首次写入的那个原文键——既不因 OCR
        抖动把记忆分裂成多份，也不改写要显示的名字。

        用 list() 快照遍历：调用方未必持锁（_lookup 是只读路径），
        直接遍历 dict 会在并发写入时炸 RuntimeError。
        """
        if chat_id in self.memory_db:
            return chat_id
        target = memory_key(chat_id)
        if target:
            for k in list(self.memory_db):
                if memory_key(k) == target:
                    return k
        return chat_id

    def _state(self, chat_id):
        """取（或创建）聊天的 v2 记忆状态。调用方必须已持锁。

        键 = 会话名原文；查找经 _resolve_key 做 OCR 变体等价匹配，
        于是「显示用原文」与「OCR 抖动不分裂」两者兼得。
        """
        key = self._resolve_key(chat_id)
        st = self.memory_db.get(key)
        if st is None:
            st = {"recent": [], "important": [], "index": [], "indexed": 0}
            self.memory_db[key] = st
        return st

    def _lookup(self, chat_id):
        """按原文取状态（只读，不创建）；经 _resolve_key 容忍 OCR 变体。

        供 important_block / match_related 这类「有就注入、没有就跳过」的
        只读路径使用——不能用 _state（它会创建空条目）。
        """
        return self.memory_db.get(self._resolve_key(chat_id)) or {}

    def recent(self, chat_id):
        """近期记忆。深层历史不在此列——由 recall 按需检索。"""
        with self.lock:
            return self._state(chat_id)["recent"]

    def add(self, chat_id, role, content, deep_enabled=True):
        with self.lock:
            st = self._state(chat_id)
            st["recent"].append({
                "role": role,
                "content": content,
                "time": time.strftime("%Y-%m-%d %H:%M:%S")
            })
            cap = max(5, int(self._cap_fn(chat_id)))
            while len(st["recent"]) > cap:
                # 溢出归档：深层记忆启用时写入 memory_deep/ 永久保存，
                # 未启用则与旧行为一致（超出窗口即丢弃）
                if deep_enabled:
                    self._append_deep_unlocked(chat_id, st["recent"].pop(0))
                else:
                    st["recent"].pop(0)
            self.schedule_save()

    def load(self, deep_enabled=True):
        if os.path.exists(self.memory_file):
            try:
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    self.memory_db = migrate_memory_data(json.load(f))
            except Exception as e:
                logger.error(f"加载记忆失败: {e}，将使用空白记忆")
                self.memory_db = {}
        # 启动一次性溢出搬运：v1 迁移来的长历史超出 recent 上限的部分
        # 全部归档进深层文件（永不删除）；同时建立深层行数计数
        for chat, st in self.memory_db.items():
            cap = max(5, int(self._cap_fn(chat)))
            if len(st["recent"]) > cap:
                if deep_enabled:
                    for msg in st["recent"][:-cap]:
                        self._append_deep_unlocked(chat, msg)
                st["recent"] = st["recent"][-cap:]
            self.deep_count[chat] = self.count_deep(chat)

    # ---------- 深层记忆（memory_deep/<chat>.jsonl，append-only 永不删除） ----------

    def deep_path(self, chat_id):
        """深层记忆文件路径：聊天名 percent-encode（文件名安全且可逆）。

        文件名用的键先经 _resolve_key 解析——这是本条链路上唯一没有等价
        匹配的地方：变体名（OCR 把「“摸鱼”集团」读成「摸鱼”集团」）若原样
        建文件，同一会话的深层历史会分裂成两份 jsonl，记忆页的「深层」计数
        与 recall_memory 都只看到其中一份（症状：记忆看起来丢了）。解析后
        落到首次写入的那个键上，与 memory.json 的键同源。

        兼容升级前的命名（键曾归一化）：那时文件名是归一化名的 encode，
        与新名不同。新名文件不存在、旧名文件存在时返回旧路径，让历史深层
        记忆继续可读可追加（否则「记忆丢了」）。
        """
        from urllib.parse import quote
        key = self._resolve_key(chat_id)
        p = os.path.join(self._deep_dir, quote(key, safe="") + ".jsonl")
        if not os.path.exists(p):
            legacy = memory_key(key)
            if legacy and legacy != key:
                lp = os.path.join(self._deep_dir, quote(legacy, safe="") + ".jsonl")
                if os.path.exists(lp):
                    return lp
        return p

    def _append_deep_unlocked(self, chat_id, msg):
        """一条消息溢出 recent 时归档进深层文件。调用方负责开关判定与持锁。

        路径与计数都用解析后的键：变体名必须追加到同一份文件、同一份计数
        上，否则 deep_count 会分裂成两个键（与 memory.json 的单键不一致，
        记忆页数字对不上）。
        """
        try:
            os.makedirs(self._deep_dir, exist_ok=True)
            key = self._resolve_key(chat_id)
            with open(self.deep_path(key), "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.deep_count[key] = self.deep_count.get(key, 0) + 1
        except Exception as e:
            logger.error(f"[记忆] 深层写入失败: {e}")

    def append_deep(self, chat_id, msg, deep_enabled=True):
        """一条消息归档进深层文件（deep_enabled 关闭时跳过）。"""
        if not deep_enabled:
            return
        with self.lock:
            self._append_deep_unlocked(chat_id, msg)

    def count_deep(self, chat_id):
        """清点深层文件行数（启动时建立计数基线；坏行跳过不计数）。"""
        return deep_count_lines(self.deep_path(chat_id))

    def iter_deep(self, chat_id):
        """按序读取深层文件全部消息（坏行跳过）。"""
        try:
            with open(self.deep_path(chat_id), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(item, dict):
                        yield item
        except OSError:
            return

    def read_deep_range(self, chat_id, start, count):
        """读取深层文件 [start, start+count) 区间的消息（压缩线程取待压缩段）。
        返回 (消息列表, 实际区间终点)：文件行数少于预期（文件被外部改动）
        时按实际返回。"""
        out = []
        for i, msg in enumerate(self.iter_deep(chat_id)):
            if i < start:
                continue
            out.append(msg)
            if len(out) >= count:
                break
        return out, start + len(out)

    def clear_deep(self, chat_id=None):
        """删除深层记忆文件（清空记忆时联动；chat_id=None 清全部）。"""
        try:
            if chat_id:
                paths = [self.deep_path(chat_id)]
            else:
                paths = [os.path.join(self._deep_dir, n)
                         for n in os.listdir(self._deep_dir)
                         if n.endswith(".jsonl")] if os.path.isdir(self._deep_dir) else []
            for p in paths:
                if os.path.isfile(p):
                    os.remove(p)
        except OSError as e:
            logger.error(f"[记忆] 深层清理失败: {e}")
        if chat_id:
            self.deep_count.pop(chat_id, None)
        else:
            self.deep_count.clear()

    # ---------- recall_memory 工具：深层 + 近期记忆关键词检索 ----------

    def recall(self, chat_id, query):
        """在本聊天的全部记忆（深层存档 + 近期窗口）中按关键词检索。
        返回给模型的可读文本（带时间戳的命中消息列表）。"""
        terms = [t for t in re.split(r"[\s，。！？、：；\"'（）()\[\]【】]+",
                                     str(query or "").strip()) if t]
        if not terms:
            return "检索词为空，请给出人名/事件/物品等关键词"
        terms_l = [t.lower() for t in terms]

        def _hit(msg):
            c = str(msg.get("content") or "").lower()
            return any(t in c for t in terms_l)

        with self.lock:
            hits = [m for m in self.iter_deep(chat_id) if _hit(m)]
            hits += [m for m in self._state(chat_id)["recent"] if _hit(m)]
        if not hits:
            return "没有找到相关记忆"
        tail = hits[-20:]  # 最多给模型 20 条（时间最晚的优先）
        lines = []
        for m in tail:
            role = "用户" if m.get("role") == "user" else "小漓"
            lines.append(f"[{m.get('time', '')}] {role}: "
                         f"{str(m.get('content') or '')[:150]}")
        head = (f"共命中 {len(hits)} 条，显示最近的 {len(tail)} 条：\n"
                if len(hits) > len(tail) else "")
        return head + "\n".join(lines)

    # ---------- 压缩产出：重要记忆（常驻注入）+ 关键词索引（命中注入） ----------

    def important_block(self, chat_id):
        """重要记忆 system 块（无内容返回 None）。per-chat 独立 system——
        不得并进人设消息（人设是跨聊天共享的缓存前缀）。"""
        st = self._lookup(chat_id)
        items = [str(x.get("content") or "").strip()
                 for x in (st.get("important") or []) if isinstance(x, dict)]
        items = [x for x in items if x]
        if not items:
            return None
        return ("以下是你在与该联系人长期相处中沉淀的重要记忆，回复时遵循：\n"
                + "\n".join(f"{i}. {c}" for i, c in enumerate(items, 1)))

    def match_related(self, chat_id, user_text):
        """关键词索引匹配：用户消息命中索引关键词时返回相关记忆注入块。"""
        text = str(user_text or "")
        if not text.strip():
            return None
        st = self._lookup(chat_id)
        hits = []
        for e in (st.get("index") or []):
            if not isinstance(e, dict):
                continue
            kws = [str(k) for k in (e.get("kw") or []) if str(k).strip()]
            mem = str(e.get("mem") or "").strip()
            if mem and any(k in text for k in kws):
                hits.append(mem)
                if len(hits) >= 3:
                    break
        if not hits:
            return None
        return ("[相关记忆]（历史对话中与本次消息相关的内容，供参考）\n"
                + "\n".join(f"- {h}" for h in hits))

    def commit_compression(self, chat_id, consumed, important_new, index_new,
                           important_max=20):
        """压缩线程写回：推进 indexed 边界 + 合并重要记忆/索引条目（锁内）。

        consumed：本次已压缩到的深层行数（绝对值）；important_new：
        [{"content": ...}]；index_new：[{"kw": [...], "mem": ...}]。
        条目超上限时裁掉最旧的（重要记忆上限 important_max，索引上限
        300——索引只增会让注入匹配越来越慢且陈旧）。"""
        with self.lock:
            st = self._state(chat_id)
            st["indexed"] = max(int(st.get("indexed") or 0), int(consumed))
            for item in important_new:
                if isinstance(item, dict) and str(item.get("content") or "").strip():
                    item = dict(item, time=time.strftime("%Y-%m-%d %H:%M:%S"))
                    st["important"].append(item)
            st["important"] = st["important"][-max(3, int(important_max)):]
            for item in index_new:
                if isinstance(item, dict) \
                        and [k for k in (item.get("kw") or []) if str(k).strip()] \
                        and str(item.get("mem") or "").strip():
                    st["index"].append(item)
            st["index"] = st["index"][-300:]
            self.schedule_save()

    # ---------- 记忆管理页的数据通道（运行中经 engine.bot 调用；全部持锁） ----------

    def overview(self):
        """全部聊天的计数快照（UI 线程读，锁内构建）：{chat: {recent, deep,
        important, index}}。deep 取 deep_count（深层文件有效行数）。"""
        with self.lock:
            out = {}
            for chat, st in self.memory_db.items():
                out[chat] = {
                    "recent": len(st.get("recent") or []),
                    "deep": int(self.deep_count.get(chat, 0)),
                    "important": len(st.get("important") or []),
                    "index": len(st.get("index") or []),
                }
            return out

    def detail(self, chat_id, deep_offset=0, deep_limit=200, deep_query=None):
        """单聊天详情快照（锁内拷贝，UI 展示用）。

        deep_query 非空：深层全量过滤，deep 截 200 条 + deep_matched 总数；
        否则深层返回第 deep_offset 页（deep_limit 条），deep_matched=None。"""
        with self.lock:
            key = self._resolve_key(chat_id)
            st = self.memory_db.get(key) or {}
            deep_total = int(self.deep_count.get(key, 0))
            deep_matched = None
            if deep_query:
                q = str(deep_query).lower()
                hits = [dict(m) for m in self.iter_deep(chat_id)
                        if q in str(m.get("content") or "").lower()]
                deep_matched = len(hits)
                deep = hits[:200]
            else:
                deep = []
                for i, m in enumerate(self.iter_deep(chat_id)):
                    if i >= deep_offset and len(deep) < deep_limit:
                        deep.append(dict(m))
            return {
                "recent": [dict(m) for m in (st.get("recent") or [])],
                "important": [dict(m) for m in (st.get("important") or [])],
                "index": [dict(m) for m in (st.get("index") or [])],
                "deep_total": deep_total,
                "deep": deep,
                "deep_matched": deep_matched,
            }

    def delete_important(self, chat_id, idx):
        """删除第 idx（1 基）条重要记忆。返回是否删除。"""
        with self.lock:
            st = self._state(chat_id)
            if 1 <= idx <= len(st["important"]):
                st["important"].pop(idx - 1)
                self.schedule_save()
                return True
            return False

    def delete_index_entry(self, chat_id, idx):
        """删除第 idx（1 基）条关键词索引。返回是否删除。"""
        with self.lock:
            st = self._state(chat_id)
            if 1 <= idx <= len(st["index"]):
                st["index"].pop(idx - 1)
                self.schedule_save()
                return True
            return False

    def delete_deep_message(self, chat_id, line_no):
        """删除深层存档第 line_no（1 基，时间正序）条消息。

        深层文件原子重写；行号落在压缩边界 indexed 之前时 indexed 同步
        -1（边界按行数推进，少一行必须回退，否则下轮压缩错位跳过一条）。
        返回是否删除。"""
        with self.lock:
            key = self._resolve_key(chat_id)
            st = self._state(key)
            removed, new_count = deep_delete_line(self.deep_path(key),
                                                  line_no)
            if removed is None:
                return False
            self.deep_count[key] = new_count
            if line_no - 1 < int(st.get("indexed") or 0):
                st["indexed"] = int(st["indexed"]) - 1
            self.schedule_save()
            return True

    def delete_messages(self, chat_id, indices):
        with self.lock:
            st = self.memory_db.get(self._resolve_key(chat_id))
            if st is None:
                logger.warning(f"❌ 聊天 {chat_id} 不存在于记忆中")
                return False
            hist = st["recent"]
            total = len(hist)
            to_delete = []
            for idx in indices:
                if 1 <= idx <= total:
                    to_delete.append(idx - 1)
                else:
                    logger.warning(f"序号 {idx} 超出范围（1-{total}），已忽略")
            if not to_delete:
                return False
            to_delete = sorted(set(to_delete), reverse=True)
            deleted_msgs = []
            for i in to_delete:
                deleted_msgs.append(hist.pop(i))
            self.save()
        logger.info(f"已从聊天 {chat_id} 中删除 {len(deleted_msgs)} 条消息")
        for msg in deleted_msgs:
            role = "用户" if msg["role"] == "user" else "小漓"
            ts = msg.get("time", "未知时间")
            content = msg["content"]
            logger.info(f"  删除: [{ts}] {role}: {content[:50]}...")
        return True

    def clear_history(self, chat_id=None):
        with self.lock:
            if chat_id:
                key = self._resolve_key(chat_id)   # 变体名也能清到同一条
                self.memory_db.pop(key, None)
                self.clear_deep(key)
                logger.info(f"已清空聊天 {chat_id} 的历史（含深层记忆）")
            else:
                self.memory_db.clear()
                self.clear_deep()
                logger.info("已清空全部对话历史（含深层记忆）")
            self.save()

    # ---------- 节流落盘 ----------

    def save(self):
        try:
            with open(self.memory_file, "w", encoding="utf-8") as f:
                json.dump(self.memory_db, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存记忆失败: {e}")

    def schedule_save(self):
        """节流写盘：距上次写盘 ≥1s 立即写，否则只标记脏。
        兜底时钟在 flush_if_due（引擎每 0.5s 轮询时检查到期）——稀疏对话
        下脏数据最多 ~1.5s 落盘。"""
        self._dirty = True
        now = time.time()
        if now - self._last_save >= 1.0:
            self.flush()

    def flush_if_due(self):
        """节流兜底：脏数据超过 1s 未落盘就强制写（引擎轮询每 0.5s 调用）。"""
        if self._dirty and time.time() - self._last_save >= 1.0:
            self.flush()

    def flush(self):
        """有脏数据则写盘。程序退出/引擎停止前调用，保证最近消息不丢。"""
        if self._dirty:
            self._dirty = False
            self._last_save = time.time()
            self.save()
