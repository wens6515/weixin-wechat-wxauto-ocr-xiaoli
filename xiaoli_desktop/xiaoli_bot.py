# -*- coding: utf-8 -*-
"""小漓合并版：普通微信聊天 + 天枢任务桥

收到消息 → LLM 判断是否任务：
  - 闲聊/提问 → 原聊天路线（call_chat_ai 回复）
  - 任务请求（如"根据文档做一个网站"）→ 投递 D:\\工作间\\wxauto\\<任务id>\\ + 唤起天枢 CLI 窗口输入"开始处理"
天枢完成任务后写 result.json + 成果文件 → bot 轮询回传（文本 SendMsg + 文件 SendFiles）→ 归档 sent\\

运行：python xiaoli_bot.py --run     自检：python xiaoli_bot.py --test
"""
import json, os, sys, time, re, traceback, base64
import queue
import random
import threading
import requests as req
import pyautogui
from wechat_bot import (WeChatBot, Controller, logger,
                        is_group_chat, _extract_file_name_token,
                        VISION_MODEL_DEFAULT, strip_reply_prefix)
from wx_backend.models import MessageType
from wx_backend.visual_backend import parse_title
from wechat_bot import _memory_key
from xiaoli_app.reminders_store import (RemindersStore, GRACE_SECONDS,
                                        WATCH_INTERVAL_MIN, WATCH_DEFAULT_TTL)
from xiaoli_app.web_search import web_fetch, resolve_redirect

if getattr(sys.stdout, "encoding", "utf-8") != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# =====================================================================
# 任务桥配置
# =====================================================================

TASK_DEFAULTS = {
    # 新装默认关（与 config_store.AI_DEFAULTS 同口径）：天枢 CLI 未配置前
    # 开着，模型会投递注定失败的任务；显式设过该键的老配置不受影响
    "task_enabled": False,
    "tasks_dir": r"D:\工作间\wxauto",
    "tianshu_window_title": "",            # 空 = 启动时交互选择
    "tianshu_trigger_command": "开始处理",
    "tianshu_poll_interval": 5,
    "file_send_method": "clipboard",   # 成果文件发送方式（UI 保存；业务恒走剪贴板，wxauto 后端已移除）
    "listen_hold_seconds": 2,   # 任务完成后延迟恢复消息监听的秒数（缓冲文件发送，防止切窗打断）
}

# 任务进度转发（progress.json 协议）：天枢每**新写一次** progress.json 就
# 回传一次进度——不再有静默期与最小间隔闸门（用户定案）。轮询重复读到同一
# 份文件不会重复发；「新写入」的判定见 AgentBot._poll_task_progress。


def load_merged_config(path="config.json"):
    """CLI 配置入口：委托 config_store（迁移/投影/写回——与 GUI 同一事实源），
    再补齐 task_* 默认。历史缺陷：CLI 曾维护一套独立默认值表（已删）、
    GUI 走 config_store.load_config_store，两套默认值并存（tasks_dir 默认
    都不一致），字段补全逻辑漂移。统一后 CLI 也支持角色卡/多 provider。
    """
    from xiaoli_app import config_store as _cs
    base = os.path.dirname(os.path.abspath(path)) or "."
    cfg = _cs.load_config_store(path, os.path.join(base, "cards"))
    changed = False
    for k, v in TASK_DEFAULTS.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    if changed:
        try:
            _cs.save_config(cfg, path)
        except OSError as e:
            logger.error(f"[配置] 写回失败: {e}")
        logger.info("[配置] 已补齐 task_* 配置项")
    return cfg


VISION_ROUTE_PROMPT = (
    "有人给你发了条消息，照你平时的样子回他。\n"
    "只有当这件事确实需要动手做出来时才调用 dispatch_task 把活交出去"
    "（例如：根据文档做一个网站、做一个 PPT、写一段代码、分析一份数据、"
    "整理文件、生成文档）；普通的闲聊、打招呼、问问题、要资料都直接回他，"
    "不要调用任何工具。"
)


# 任务桥按聊天关闭且**其他工具也不可用**（语音/搜索/闹钟/回忆全无）时的
# 纯聊天 prompt 变体：文字里完全不提任何工具名，与「工具不声明」配套——
# 提示词提一个没声明的工具会诱导模型幻觉调用。
VISION_CHAT_PROMPT = (
    "有人给你发了条消息，照你平时的样子回他。\n"
    "普通的闲聊、打招呼、问问题、要资料都直接回他，不要调用任何工具。"
)


# 中性路由变体：任务桥关闭但其他工具（语音/搜索/闹钟/回忆）可用时使用。
# 不能说「不要调用任何工具」（会压制已声明的工具——实测事故：任务桥关 +
# 文字设闹钟，模型无工具可调只能闲聊）；也不提 dispatch_task（工具未声明，
# 提了会诱导幻觉调用）。
VISION_NEUTRAL_PROMPT = (
    "有人给你发了条消息，照你平时的样子回他。"
)


# 主动追尾的评估指令（系统把「要不要补」作为一个必答问题递给模型——
# 二选一输出，不依赖模型自发想起调工具，规避 no_reply 式的低调用率）。
FOLLOWUP_EVAL_PROMPT = (
    "你刚回完对方的话（最近对话见历史）。现在有一个可选动作：**自然地补"
    "一句**——像真人那样偶尔回完话想起什么，追加一条短消息（想到了什么、"
    "顺口一问、分享个相关的东西）。想补就只输出那一条消息的文本（口语化、"
    "二十字左右）；不想补就只输出 [不补] 三个字符。要克制：十次里补两三次"
    "就够了，只有真的自然才补。不要调用任何工具。"
)

# 引用标注解析（get_messages 装配的「[引用 …] 」前缀）
_QUOTE_MARKER_RE = re.compile(r"^\[引用 (.+?)\]\s*")


# =====================================================================
# 任务桥与后台线程已拆至 xiaoli_app（纯移动）；此处显式 re-import，
# 既有导入路径（tests / xiaoli_app.setup / xiaoli_web）不变。
# =====================================================================
from xiaoli_app.task_bridge import (
    CLASSIFY_PROMPT, acquire_single_instance, activate_window_by_title,
    classify_task_with_llm,
    clipboard_set_text, dispatch_task, find_window_by_title, generate_task_id,
    has_active_tasks, list_windows, poll_outbox, read_clipboard_files,
    release_single_instance, resolve_result_file, scan_task_status,
    send_trigger_to_window, set_clipboard_files, should_resume_listen,
    _parse_classify_json,
)
from xiaoli_app.bot_daemons import (
    ConditionWatcher, MEMORY_COMPRESS_PROMPT, MemoryCompressor,
    ReminderScheduler, WATCH_EVIDENCE_CHARS, WATCH_JUDGE_PROMPT,
    WATCH_MAX_FAILS, WATCH_MIN_SLICE_CHARS, parse_compress_json,
    parse_watch_json,
)

# =====================================================================
# AgentBot：普通聊天 + 天枢任务桥
# =====================================================================

class AgentBot(WeChatBot):
    """小漓合并版：继承原 WeChatBot（聊天/图片/文件识别），叠加天枢任务桥"""

    def __init__(self, cfg, stop_event=None, max_connect_retries=None):
        """stop_event/max_connect_retries：透传给 WeChatBot（GUI 引擎停止
        可中断微信连接重试；重试超限抛异常 → 引擎 error 状态）。CLI 模式
        不传：保留无限重试（用户开着 bot 等微信启动）。"""
        super().__init__(cfg, stop_event=stop_event,
                         max_connect_retries=max_connect_retries)
        self.task_enabled = cfg.get("task_enabled", False)
        self.tasks_dir = cfg.get("tasks_dir", r"D:\工作间\wxauto")
        self.tianshu_window_title = cfg.get("tianshu_window_title", "")
        self.tianshu_trigger_command = cfg.get("tianshu_trigger_command", "开始处理")
        self.tianshu_workdir = cfg.get("tianshu_workdir", "")  # CLI（rivet）工作目录，resolve_cli_window 第 3 级启动时使用
        self.tianshu_poll_interval = cfg.get("tianshu_poll_interval", 5)
        self._last_poll_time = 0
        self._progress_state = {}  # 任务 id -> {mtime, stage}（进度回传去重状态）
        self._sending_lock = False  # 成果回传期间置 True，暂停消息轮询防发错联系人
        self._listen_hold_seconds = cfg.get("listen_hold_seconds", 10)
        self._task_was_active = False  # 是否曾因任务暂停监听（用于任务完成后的缓冲期）
        self._task_end_time = None     # 最后一次任务完成（归档）的时刻
        try:
            os.makedirs(self.tasks_dir, exist_ok=True)
        except OSError as e:
            logger.warning(f"[AgentBot] tasks_dir 创建失败 {self.tasks_dir}: {e}，回退用户目录")
            from xiaoli_app import config_store as _cs
            self.tasks_dir = _cs.default_tasks_dir()
            os.makedirs(self.tasks_dir, exist_ok=True)
        # 首轮提示词引导天枢读 tasks_dir\README.md：首次生成协议文档（已存在不覆盖）
        try:
            from xiaoli_app import setup as _setup
            _setup.ensure_bridge_readme(self.tasks_dir)
        except Exception as e:
            logger.warning(f"[AgentBot] 生成任务桥 README 失败: {e}")
        # 全自动模式由首启一次性引导的 /yes 保证（持久化，重启后仍生效，
        # 见 run_first_run_guide）——不再配置 config 级 YOLO（旧机制：
        # rivet config set-approval 只影响下次启动，且 /yes 已覆盖此需求）。
        # 是否暂停消息监听由 has_active_tasks 每次实时判断，不保存粘滞状态
        self._pending_files = {}  # chat_name -> {sender} 群聊文件等待用户指令
        # 占位回复计数（_pending_placeholders）由基类 WeChatBot 提供（基类
        # __init__ 初始化；placeholder 发送 +1 / 实质回复归零，按 chat_name 隔离）
        # 失败退避：处理未产出回复（红圈滞留）的会话 8s 内不重复处理——
        # 防滞留红圈在 0.5s 快档下每轮都打一遍整条 OCR 管线，把单核打满
        self._chat_fail_at = {}
        self._fail_backoff = 8.0
        # 主动追尾运行态（内存即可：冷却/日限跨重启重置可接受，追尾晚一轮
        # 无害）。pending 由主循环消费（发送切会话，窗口互斥纪律同触发器）。
        self._followup_pending = []   # [{chat, due_at, scheduled_at}]
        self._followup_last = {}      # chat -> 上次追尾发出时刻
        self._followup_day = {"date": None, "count": 0}
        # 每日预算：当日告警标记 + 结果缓存（60s 一查——别在 0.5s 轮询里
        # 每轮跑一次 SQL 聚合）
        self._budget_day = {"date": None, "flagged": False}
        self._budget_cache = {"at": 0.0, "out": False}
        # 定时消息：存储 + 闹钟线程 + 到期队列（发送在主循环节点消费）
        self.reminders = RemindersStore()
        self._reminder_queue = queue.Queue()
        self._reminder_sched = ReminderScheduler(
            self.reminders, self._reminder_queue, stop_event=stop_event)
        self._reminder_sched.start()
        # 条件监视（kind=condition）：轮询线程只抓页/判定，事件入队；
        # 发送在主循环 _drain_conditions 节点（窗口互斥纪律同定时消息）
        self._condition_queue = queue.Queue()
        self._condition_watcher = ConditionWatcher(self, stop_event=stop_event)
        self._condition_watcher.start()
        # 长记忆压缩线程：深层记忆攒满 batch 条后提炼重要记忆/关键词索引
        # （memory_compress_enabled 关闭时线程空转等待，设置热改后自动生效）
        self._memory_compressor = MemoryCompressor(self, stop_event=stop_event)
        self._memory_compressor.start()
        logger.info(f"[AgentBot] tasks_dir={self.tasks_dir}, task_enabled={self.task_enabled}")

    # ---------- 任务桥 ----------

    def set_tianshu_window_title(self, title):
        """记住天枢窗口标题（写回 config.json）"""
        self.tianshu_window_title = title
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg["tianshu_window_title"] = title
            with open("config.json", "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=4)
            logger.info(f"[天枢] 窗口标题已保存到 config: {title}")
        except Exception as e:
            logger.error(f"[天枢] 保存窗口标题失败: {e}")

    def _select_window(self):
        """交互式选择天枢窗口（启动时/命令调用）。返回所选标题或 None"""
        wins = list_windows()
        if not wins:
            logger.error("未找到任何窗口")
            return None
        for i, (name, _h) in enumerate(wins, 1):
            print(f"  {i}. {name}")
        choice = input("请输入序号选择天枢窗口，输入 cancel 取消: ").strip()
        if choice.lower() == "cancel":
            return None
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(wins):
                title = wins[idx][0]
                self.set_tianshu_window_title(title)
                return title
            logger.error("序号超出范围")
        except ValueError:
            logger.error("请输入有效序号")
        return None

    def apply_role(self, card, providers=None):
        """热切换角色卡：人格/昵称/模型/参数/端点（带锁）。

        card 由 card_store 规范化；providers 为 config.json 的 providers 路由表。
        端点投影：聊天/视觉按各自 provider 解析 base_url + key（config_store.project_config）。
        """
        from xiaoli_app.config_store import project_config
        from wechat_bot import strip_model_prefix
        proj = project_config({"providers": providers or []}, card)
        with self._model_lock:
            self.system_prompt = card.get("system_prompt", self.system_prompt)
            self.nickname = card.get("nickname") or self.nickname
            # 单模型化：卡只派生 chat_model（视觉/分类统一走 chat_model，
            # vision_model/classify_model/vision_temp/vision_max_tokens 已从卡删除）
            self.chat_model = strip_model_prefix(card.get("chat_model") or self.chat_model)
            self.chat_temperature = float(card.get("temperature", self.chat_temperature))
            self.chat_top_p = float(card.get("top_p", self.chat_top_p))
            self.max_history = int(card.get("max_history", self.max_history))
            self.api_url = proj.get("ai_api_url", self.api_url)
            self.api_key = proj.get("ai_api_key", self.api_key)
            self.vision_api_url = proj.get("vision_api_url", self.vision_api_url)
            self.vision_api_key = proj.get("vision_api_key", self.vision_api_key)
        logger.info(
            f"[角色卡] 已切换: {card.get('name')} "
            f"(chat={self.chat_model}, temp={self.chat_temperature})"
        )

    def _classify_task(self, text):
        # 任务有效开关由调用方按聊天判断（feature_enabled），此处只负责
        # LLM 分类本身——全局 task_enabled 闸放在这里会拦掉「全局关 +
        # 单聊强制开」的降级路径
        return classify_task_with_llm(self.api_url, self.api_key, self.chat_model, text)

    def _vision_route(self, chat_name, sender, text, img_paths=None, msg_id=None,
                      raw_message=None, attachments=None, is_group=None,
                      multi_sender=False):
        """vision-exp 单调用分流：任务判断 + 回复一次完成（替代两段式）。

        img_paths：图片临时文件路径列表（可空）——全部按发送顺序作为
        image_url 块放进同一条 user 消息，与文字一次发给 API。

        契约（基类 call_vision_api 由并行维度实现）：
          content 块 = [{"type": "text", "text": ...},
                        {"type": "image_url", ...}, ...]
          call_vision_api(content) 返回 dict 或 None：
            {"kind": "tool_call", "name": "dispatch_task",
             "arguments": '{"task": "..."}'} → 投递天枢（task 从 arguments JSON 解析）
            {"kind": "text", "content": "..."} → 直接回复（实质回复 → 占位归零）
            None / 异常                          → 返回 None，调用方降级

        返回 True = 已处理；None = 调用失败，交调用方降级。
        """
        # 方案二：人设由 call_vision_api 的 system 消息承载，这里不再重复注入
        # （避免同段人设同时出现在 system 与 user prompt 造成冗余/冲突）
        # sender 分流：对齐 call_chat_ai（wechat_bot.py）三分支——群聊带发送者
        # 名、多发送者只包群名前缀不重包 sender、私聊带发送者；否则模型不知
        # 道谁发的消息（历史缺陷：sender 与群聊名完全没进 prompt）。
        if is_group is None:
            is_group = bool(
                getattr(getattr(self, "wx", None), "_current_is_group", None)
                or is_group_chat(chat_name))
        if is_group:
            if multi_sender:
                decorated = f"群聊：{chat_name} {text}"
            elif sender:
                decorated = f"群聊：{chat_name} {sender}：{text}"
            else:
                decorated = f"群聊：{chat_name}：{text}"
        else:
            decorated = f"私聊 - {sender}：{text}" if sender else f"私聊：{text}"
        # 提示词与工具注入同一套条件（_vision_tools 是 call_vision_api 的
        # 声明单一事实源）：任务桥开 → 提 dispatch_task 的路由变体；任务桥
        # 关但语音/搜索/闹钟等工具可用 → 中性变体（「不要调用任何工具」会
        # 压制已声明工具）；一个工具都没有 → 纯聊天变体防幻觉。
        if self.feature_enabled(chat_name, "task"):
            route_prompt = VISION_ROUTE_PROMPT
        elif self._vision_tools(chat_name):
            route_prompt = VISION_NEUTRAL_PROMPT
        else:
            route_prompt = VISION_CHAT_PROMPT
        prompt = f"{route_prompt}\n\n用户消息：\n{decorated}"
        content = [{"type": "text", "text": prompt}]
        # 关键词记忆索引：用原始用户消息（非装饰串）匹配，命中的相关记忆
        # 由 call_vision_api 注入到历史之后
        related = self._match_related_memory(chat_name, text)
        for p in (img_paths or []):
            try:
                with open(p, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode()
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                })
            except OSError as e:
                logger.error(f"[vision] 图片读取失败，忽略该图: {e}")
        try:
            resp = self.call_vision_api(content, chat_id=chat_name,
                                        related_memory=related)
        except Exception as e:
            logger.error(f"[vision] 调用异常: {e}")
            return None
        return self._apply_vision_result(
            chat_name, sender, resp, img_paths=img_paths, msg_id=msg_id,
            raw_message=raw_message, attachments=attachments, user_text=decorated)

    def _apply_vision_result(self, chat_name, sender, result, img_paths=None,
                             msg_id=None, raw_message=None, attachments=None,
                             user_text=None):
        """vision 单调用结果分流核心（_vision_route 与 _route_vision_result 共用）。

        result: call_vision_api 的 dict 返回（{'kind':'tool_call'|'text',...}）
        或 None；user_text 为用户消息原文（tool_call JSON 解析失败降级用、
        text 分支记历史用；纯图路径为 None 时降级用 arguments 原文）。
        img_paths 为图片临时文件路径列表（attachments 缺省时全部作为任务附件）。
        返回 True = 已处理；None = 调用失败/未识别，交调用方降级。
        """
        if not result:
            return None
        kind = result.get("kind")
        if kind == "tool_call" and result.get("name") == "no_reply":
            # 智能静默（no_reply_enabled 开启时声明）：模型决定不回复
            return self._handle_no_reply(chat_name, result, user_text)
        if kind == "tool_call" and result.get("name") == "send_voice":
            # 语音发送工具（该聊天语音有效模式 auto/always 时注入）
            return self._handle_send_voice(chat_name, result, user_text)
        if kind == "tool_call":
            # 契约：name 必须为 dispatch_task；task 从 arguments JSON 解析
            # （json.loads 后取 task 字段；解析失败降级用原文）
            if result.get("name") != "dispatch_task":
                logger.warning(f"[vision] 未知工具调用 name={result.get('name')!r}，降级")
                return None
            if not self.feature_enabled(chat_name, "task"):
                # fail-closed：该聊天任务桥已关（全局关 + 强制开未覆盖/强制
                # 关）——模型幻觉调用未声明工具时拒绝投递，降级普通聊天
                logger.warning(
                    f"[任务桥] {chat_name!r} 任务桥已按聊天关闭，忽略投递工具调用")
                return None
            task_desc = user_text or ""
            raw_args = result.get("arguments") or "{}"
            try:
                args = json.loads(raw_args)
                task_desc = str(args.get("task") or user_text or "").strip()
            except (ValueError, TypeError):
                task_desc = (user_text or raw_args).strip()
            logger.info(f"[任务桥] vision 判定为任务: {task_desc[:60]}")
            self._add_history(chat_name, "assistant", "[任务已投递天枢处理]")
            atts = attachments if attachments is not None \
                else (list(img_paths) if img_paths else None)
            self._dispatch_and_notify(
                chat_name, sender, task_desc,
                attachment_paths=atts,
                extra={"msg_id": msg_id, "raw_message": raw_message or task_desc},
            )
            return True
        if kind == "text":
            reply = str(result.get("content") or "").strip()
            sticker_sent = bool(result.get("sticker_sent"))
            stickers = [str(s).strip() for s in (result.get("stickers") or [])
                        if str(s).strip()]
            if not reply and not sticker_sent:
                logger.warning("[vision] text 响应无回复文本，降级")
                return None
            logger.info(f"[vision] 判定非任务，直接回复: "
                        f"{reply[:60] if reply else '[纯表情包]'}")
            sticker_mark = ("[发送了表情包: " + "、".join(stickers) + "]"
                            if stickers else "[发送了表情包]")
            if user_text:
                self._add_history(chat_name, "user", user_text)
            if stickers:
                # 表情包动作写入历史（真机事故：模型连发两次同一张图——
                # 历史里没有它发过表情包的痕迹，模型不知道自己发过）。
                # 记在回复文本之前，时序与实际一致（工具循环内先发图后说话）
                self._add_history(chat_name, "assistant", sticker_mark)
            if reply:
                self._add_history(chat_name, "assistant", reply)
                self._deliver_reply(chat_name, reply)
            else:
                # 纯表情包回复：图已在工具循环内发出，这里只记记忆 + 占位
                # 归零（实质回复语义，与 _send_text 对齐）
                self._add_history(chat_name, "assistant", sticker_mark)
                self._pending_placeholders.pop(chat_name, None)
            return True
        logger.warning(f"[vision] 未知响应 kind={kind!r}，降级")
        return None

    def _route_vision_result(self, chat_name, sender, result, img_paths=None):
        """纯图路径 hook 覆写：复用 _apply_vision_result 分流核心。

        纯图路径（_process_pure_image：捕获全部新媒体 → vision 单调用 →
        本 hook）的结果分流：tool_call → 投递天枢（attachment_paths=全部
        图片，任务桥逐张复制进任务目录 attachments\）；text → 直接回复
        （实质回复 → 占位归零）；None → 返回 None，调用方降级。
        无用户文字，JSON 解析失败降级用 arguments 原文。
        """
        return self._apply_vision_result(
            chat_name, sender, result, img_paths=img_paths)

    def _dispatch_and_notify(self, chat_name, sender, task_desc, attachment_paths=None, extra=None):
        """投递任务 → 微信告知"处理中" → 唤起天枢窗口。返回是否投递成功"""
        task_info = {
            "msg_id": (extra or {}).get("msg_id"),
            "sender": sender,
            "chat_name": chat_name,
            "is_group": bool(is_group_chat(chat_name)),
            "raw_message": (extra or {}).get("raw_message", ""),
            "task": task_desc,
        }
        for k, v in (extra or {}).items():
            if k not in task_info:
                task_info[k] = v
        task_id = dispatch_task(self.tasks_dir, task_info, attachment_paths)
        # 投递后幂等预授权天枢 CLI 读取任务目录（配置化授权 agent.permissions，
        # 常驻 CLI 重启后生效；dispatch 已确保 tasks_dir 存在，fail-closed 不跳过）
        try:
            from xiaoli_app import config_store as _cs
            _cs.grant_tasks_dir_to_tianshu(str(self.tasks_dir or "").strip())
        except Exception:
            pass
        self._send_text("收到任务啦，正在处理中，稍等一下哦～", chat_name, placeholder=True)
        # 唤起天枢：统一走 resolve_cli_window——tianshu_window_title 可能被
        # 旧版本污染为桌面端「天枢 · Tianshu」，直接 send 会激活桌面端窗口。
        # resolve 三级定位（手动配置→CLI 特征→启动后新增窗口），含桌面端排除。
        try:
            from xiaoli_app import setup as _setup
            # tianshu_workdir 必须传入——第 3 级自动启动 CLI 时 launch_tianshu 用它
            # 决定 cwd，缺失会落入 ~ 而非用户配置的工作目录。
            title, detail = _setup.resolve_cli_window({
                "tianshu_window_title": self.tianshu_window_title,
                "tianshu_workdir": getattr(self, "tianshu_workdir", ""),
                "tasks_dir": getattr(self, "tasks_dir", ""),  # launch cwd 优先用户选的工作间
            })
            if not title:
                logger.warning(f"[天枢] 未定位到 CLI 窗口（{detail}），任务已投递但未唤起")
                return True
            # 全自动模式由首启一次性引导的 /yes 保证（持久化，重启后仍生效，
            # 见 run_first_run_guide）——这里不再会话内切 YOLO（旧机制：
            # /permission yolo confirm 每次投递都要重发）。直接发触发指令。
            ok = send_trigger_to_window(title, self.tianshu_trigger_command, hold=2.0)
            if not ok:
                logger.error(f"[天枢] 唤起窗口失败: {title}，任务 {task_id} 保留在 {os.path.join(self.tasks_dir, task_id)}")
                self._send_text("天枢窗口没找到，不过任务已经记下了，处理完我会把结果发给你～", chat_name, placeholder=True)
        except Exception as e:
            logger.error(f"[天枢] 唤起异常: {e}，任务已投递")
        return True

    def _switch_to_chat(self, chat):
        """切到目标会话（发送前必须）。visual 后端（当前唯一可用通道）有
        _switch_chat（点击会话列表）。wxauto4 在微信 4.1.12 结构性失效，
        不作为可用后端——无切换能力时 fail-closed 返回 False，不假装能切。"""
        switcher = getattr(self.wx, "_switch_chat", None)
        if switcher is not None:
            return bool(switcher(chat))
        logger.warning(f"[切换] 后端无 _switch_chat 能力，无法切到会话 {chat!r}")
        return False

    def _send_file_clipboard(self, fpath, chat):
        """剪贴板 CF_HDROP + Ctrl+V 发送文件（SendFiles UI 自动化失效时的替代方案）。

        置前微信走 visual 后端 _foreground（Win32 SetForegroundWindow，不依赖
        UIA 窗口类名，微信 4.x 下可靠）；无 _foreground 能力时兜底不阻塞发送。
        """
        fg = getattr(self.wx, "_foreground", None)
        if fg is not None:
            fg()
        if chat:
            try:
                self._switch_to_chat(chat)
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f"[回传] 切换聊天窗口失败 {chat}: {e}")
        # 切会话后再确保微信前台（_switch_chat 内部操作可能改变前台）
        if fg is not None:
            fg()
        if not set_clipboard_files([fpath]):
            logger.error(f"[回传] 剪贴板设置文件失败: {fpath}")
            return False
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.6)  # 等微信把文件加入待发送区
        pyautogui.press("enter")
        logger.info(f"[回传] 剪贴板方式已发送文件: {os.path.basename(fpath)}")
        return True

    def _send_sticker_file(self, fpath):
        """表情包图片粘贴发送（剪贴板 CF_HDROP + Ctrl+V + Enter，与文件回传
        同一机制——微信对图片文件粘贴按图片消息发出，GIF 按动图）。

        处理消息期间窗口已停在目标会话（与 _send_text 同一假设），不切会话；
        粘贴后留 0.6s 等微信把图放进待发送区再回车。返回是否执行成功
        （fail-closed：剪贴板/输入框定位任一失败都 False，上层放弃不回退）。"""
        fg = getattr(self.wx, "_foreground", None)
        if fg is not None:
            fg()
        if not set_clipboard_files([fpath]):
            logger.error(f"[表情包] 剪贴板设置文件失败: {fpath}")
            return False
        rect_fn = getattr(self.wx, "_input_box_rect", None)
        box = rect_fn() if rect_fn is not None else None
        if not box:
            logger.warning("[表情包] 无法定位输入框，放弃发送")
            return False
        time.sleep(0.2)
        pyautogui.click(box[0] + box[2] // 2, box[1] + box[3] // 2)
        time.sleep(0.3)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.6)
        pyautogui.press("enter")
        logger.info(f"[表情包] 已粘贴发送: {os.path.basename(fpath)}")
        return True

    def _poll_outbox(self):
        """封装 poll_outbox：从 task.json 取 chat_name 回传"""
        if not self.task_enabled:
            return []

        def deliver(task_dir, task_info, result):
            chat = task_info.get("chat_name", "")
            status = result.get("status", "success")
            reply_text = str(result.get("reply_text", "")).strip()
            if status == "failed":
                reply_text = reply_text or "任务处理失败了，不好意思呀～"
            self._sending_lock = True
            try:
                # 发送前显式切到目标聊天，防止轮询把窗口切走导致发错联系人
                if chat:
                    try:
                        self._switch_to_chat(chat)
                        time.sleep(0.3)
                    except Exception as e:
                        logger.error(f"[回传] 切换聊天窗口失败 {chat}: {e}")
                if reply_text:
                    self._send_text(reply_text, chat)
                for fname in (result.get("files") or []):
                    # 并发/重试场景：任务可能已被归档进 sent，文件需从 sent 下兜底定位
                    fpath = resolve_result_file(task_dir, os.path.join(self.tasks_dir, "sent"), fname)
                    if os.path.isfile(fpath):
                        sent = False
                        # 剪贴板 CF_HDROP 唯一发送方式（wxauto SendFiles 在新版微信
                        # 实测不可用；visual 后端未实现协议 send_file，无兜底分支）
                        try:
                            sent = self._send_file_clipboard(fpath, chat)
                        except Exception as e:
                            logger.warning(f"[回传] 剪贴板发送异常: {e}")
                        if not sent:
                            logger.error(f"[回传] 文件发送失败，保留在任务目录: {fname}")
                # 无论发送成败，任务结果都写入对话记忆（记忆记录的是任务产出，不是发送状态）
                self._remember_task_result(chat, result)
            finally:
                self._sending_lock = False

        try:
            return poll_outbox(self.tasks_dir, deliver)
        except Exception as e:
            logger.error(f"[回传] 轮询异常: {e}")
            return []

    def _remember_task_result(self, chat, result):
        """任务回传后把结果写入该聊天的对话记忆，让小漓后续能回忆任务成果。
        与投递时写入的 '[任务] ...' / '[任务已投递天枢处理]' 构成完整对话流"""
        if not chat:
            return
        status = result.get("status", "success")
        reply_text = str(result.get("reply_text", "")).strip()
        if status == "failed":
            reply_text = reply_text or "任务处理失败了，不好意思呀～"
        summary = f"[任务结果] {reply_text}" if reply_text else "[任务结果] 任务完成"
        files = result.get("files") or []
        if files:
            summary += "，成果文件: " + "、".join(str(f) for f in files)
        self._add_history(chat, "assistant", summary[:500])

    def _tick_poll_outbox(self):
        """按间隔轮询 outbox（不受消息 cooldown / 任务暂停限制）"""
        now = time.time()
        if now - self._last_poll_time >= self.tianshu_poll_interval:
            self._last_poll_time = now
            self._poll_outbox()
            self._poll_task_progress()

    def _poll_task_progress(self):
        """任务进度转发（progress.json 协议）：天枢每新写一次 progress.json，
        就回传一次进度到微信。

        「新写入」判定 = 文件 mtime 变化 **或** stage 文本变化（二者任一）：
        轮询重复读到同一份文件不重复发；天枢用同一段文本覆写也算一次新写入
        （mtime 变了），照发。不再有静默期 / 最小间隔闸门——写多勤就报多勤，
        天枢侧靠「只在关键节点写」自律（见 tasks_dir 的 README 与首轮提示词）。

        result.json 出现后由 _poll_outbox 负责回传与归档，此处只处理未完成
        任务；已结束任务的转发状态随之清理。"""
        if not os.path.isdir(self.tasks_dir):
            return
        active = set()
        for name in sorted(os.listdir(self.tasks_dir)):
            task_dir = os.path.join(self.tasks_dir, name)
            if not os.path.isdir(task_dir) or name == "sent":
                continue
            if not os.path.isfile(os.path.join(task_dir, "task.json")):
                continue
            if os.path.isfile(os.path.join(task_dir, "result.json")):
                continue  # 已完成待回传：发送与归档由 _poll_outbox 负责
            active.add(name)
            pfile = os.path.join(task_dir, "progress.json")
            try:
                mtime = os.path.getmtime(pfile)
                with open(pfile, "r", encoding="utf-8") as f:
                    stage = str(json.load(f).get("stage") or "").strip()
            except (OSError, ValueError):
                continue
            if not stage:
                continue
            st = self._progress_state.setdefault(
                name, {"mtime": None, "stage": None})
            if st["mtime"] == mtime and st["stage"] == stage:
                continue  # 文件没被重新写过 → 不重复回传
            st["mtime"] = mtime
            st["stage"] = stage
            chat = ""
            try:
                with open(os.path.join(task_dir, "task.json"),
                          "r", encoding="utf-8") as f:
                    chat = str(json.load(f).get("chat_name") or "").strip()
            except (OSError, ValueError):
                pass
            if not chat:
                continue
            logger.info(f"[任务进度] {name} -> {chat}: {stage[:60]}")
            # 触发回复通道直发（旁路 _send_text 的占位归零语义，同定时/条件触发器）
            self._send_trigger_reply(stage, chat)
        for tid in list(self._progress_state.keys()):
            if tid not in active:
                self._progress_state.pop(tid, None)

    def _process_file_with_instruction(self, chat_name, sender, filepath, filename, user_instruction, extra_attachments=None, multi_sender=False):
        """根据用户指令处理文件：vision-exp 单调用判断任务 → 天枢投递 或 原文件识别。

        任务分支只投文件本体（天枢 CLI 自行读附件），不提取文件文字、不写
        file_text；只有非任务分支（把文件内容喂给 AI 生成回复）才提取文字。
        vision 调用失败（None）时降级回退原两段式（_classify_task + 文件识别）。
        """
        is_group = is_group_chat(chat_name)
        instruction = user_instruction
        if is_group:
            at_tag = f"@{self.nickname}"
            instruction = instruction.replace(at_tag, "").strip()
        if not instruction:
            instruction = user_instruction.strip()

        # 任务判断输入 = 文件名 + 处理要求
        # （LLM 需知道「处理的对象」才能判断是否动手类任务；仅指令如
        # '把这个做成网页' 缺少对象，单独看会被误判闲聊）：
        #   '[文件]部门简介+纳新宣传(6).docx 把这个做成一个赛博朋克风格的网页'
        classify_input = f"[文件]{filename} {instruction}"
        if self.feature_enabled(chat_name, "task"):
            resp = self._vision_route(
                chat_name, sender, classify_input,
                msg_id=None, raw_message=filename,
                attachments=[filepath] + (extra_attachments or []),
                is_group=is_group, multi_sender=multi_sender,
            )
            if resp is not None:
                return True
            # vision 降级（None）：回退原两段式任务判断
            cls = self._classify_task(classify_input)
            if cls["is_task"]:
                logger.info(f"[任务桥] 文件+指令判定为任务: {cls['task'][:60]}")
                self._add_history(chat_name, "assistant", "[任务已投递天枢处理]")
                atts = [filepath] + (extra_attachments or [])
                self._dispatch_and_notify(
                    chat_name, sender, cls["task"],
                    attachment_paths=atts,
                    extra={"msg_id": None, "raw_message": filename},
                )
                return True

        # 非任务 → 此时才提取文件文字，连同用户指令一起喂给 AI
        logger.info(f"[文件] 非任务，走文件识别流程，附带用户指令: {instruction[:60]}")
        file_content = self._extract_file_text(filepath)
        if file_content is None:
            self._send_text(f"收到文件「{filename}」，但这个格式我看不懂呢～", chat_name)
            return True
        self._add_history(chat_name, "assistant", f"[文件内容: {filename}] {file_content}")
        refine_prompt = (
            f"用户发来一个文件（{filename}），内容如下：\n\n"
            f"{file_content}\n\n"
            f"用户对文件处理的要求是：{instruction}\n\n"
            f"请根据文件内容和用户的要求，以{self.nickname}的身份回复用户。"
        )
        final_reply = self.call_chat_ai(chat_name, refine_prompt, sender_name=sender, is_group=is_group, multi_sender=multi_sender)
        self._deliver_reply(chat_name, final_reply)
        return True

    @staticmethod
    def _reminder_trigger_text(chat, item, fire_hm):
        """定时触发的回递文案（按创建来源分路）+ 本次触发元数据 + 主动调整提示。

        元数据（[id]/重复/内容）与调整提示是触发器主动性的关键（真机事故：
        用户说「每天这个时候都在回家的路上」，模型毫无反应——lazy 档下它
        既看不到触发器清单、fire 文案里也没有任何触发器信息，不知道有东西
        可改）。界面手动创建的触发器（source=ui）内容由用户填写——模型无从
        知道用户写了什么，必须把内容一并递过去；模型在对话里创建的
        （source=model，内容恒为空）保持「创建时的对话就在历史里」这条既有
        语义。旧数据没有 source 字段，按 content 是否为空回退判定。
        """
        content = str(item.get("content") or "").strip()
        source = str(item.get("source") or ("ui" if content else "model"))
        rid = str(item.get("id") or "")
        repeat = str(item.get("repeat") or "once")
        hint = ("\n\n（这条触发器归你管理：如果你从对话中发现它的安排该调整"
                "——时间总在不合适的时刻、内容过时、或者该取消了——可以用 "
                "manage_reminder 修改或取消，改完自然地跟用户提一句。）")
        if source == "ui" and content:
            return ("[定时触发] 时间到了，该去做这件事了。\n"
                    f"聊天对象：{chat}\n"
                    f"本次触发：[{rid}]（{repeat}）「{content}」\n"
                    f"你之前和用户约好的事：{content}\n"
                    "请按人设自然地主动把这件事说出来——直接说事，"
                    "不要提「提醒」「定时」「触发器」这类字眼。" + hint)
        return (f"[定时触发] 与用户约定的指定时间（{fire_hm}）到了。"
                f"本次触发：[{rid}]（{repeat}）。"
                "这条触发器创建时的对话就在历史里，"
                "请按人设自然地主动回复。" + hint)

    def _trigger_generate_reply(self, chat, trigger):
        """触发器回递生成（定时/条件统一火线）：优先走 vision 链路——工具
        全程可用（manage_reminder 现场调整触发器 / web_search / no_reply），
        生成后由调用方 _deliver_reply(trigger=True) 发送；vision 失败/未收敛
        降级旧 call_chat_ai 链路。历史两条链路对齐：user=触发事件、
        assistant=最终回复（call_chat_ai 自带写入；vision 链路在此手写——
        它不自动写历史）。

        返回 ("reply", 文本) / ("silent", None) / ("fallback", None)。"""
        resp = None
        try:
            resp = self.call_vision_api(
                [{"type": "text", "text": trigger}], chat_id=chat)
        except Exception as e:
            logger.error(f"[触发回递] vision 链路异常: {e}")
        if resp and resp.get("kind") == "text":
            reply = strip_reply_prefix(str(resp.get("content") or "").strip())
            if reply:
                self._add_history(chat, "user", trigger)
                self._add_history(chat, "assistant", reply)
                return "reply", reply
            return "fallback", None
        if resp and resp.get("kind") == "tool_call" \
                and resp.get("name") == "no_reply":
            # 模型对触发事件本身选择沉默（罕见）：记录在案，不发送不降级
            self._add_history(chat, "user", trigger)
            self._add_history(chat, "assistant", "[触发未回复]")
            logger.info(f"[静默] {chat!r} 模型对触发事件选择沉默")
            return "silent", None
        if resp and resp.get("kind") == "tool_call":
            logger.warning(f"[触发回递] 触发事件返回工具调用 "
                           f"{resp.get('name')!r}（触发场景不走该出口），降级聊天链路")
        return "fallback", None

    def _drain_reminders(self):
        """消费到期定时触发器（kind=time）队列（主循环节点）。

        火线统一（用户定案）：到点把触发事件回递给 API 生成角色内回复后
        发送。回递优先走 vision 链路（工具可用——模型可在触发现场用
        manage_reminder 调整触发器，见 `_reminder_trigger_text` 的元数据与
        提示），失败降级 call_chat_ai。发送失败也确认出队（mark_fired），
        避免死循环重发刷屏。回递文案按创建来源分路：界面创建的带用户填的
        内容，模型创建的靠历史。"""
        while True:
            try:
                r = self._reminder_queue.get_nowait()
            except queue.Empty:
                break
            rid = r.get("id")
            try:
                now = time.time()
                if now - (r.get("fire_at") or now) > GRACE_SECONDS:
                    logger.warning(f"[定时] 触发器 {rid} 超宽限（暂停/滞留），按错过处理")
                    continue
                chat = r.get("chat") or ""
                if not chat:
                    continue
                fire_hm = time.strftime("%m-%d %H:%M",
                                        time.localtime(r.get("fire_at") or now))
                trigger = self._reminder_trigger_text(chat, r, fire_hm)
                mode, reply = self._trigger_generate_reply(chat, trigger)
                if mode == "silent":
                    continue
                if mode != "reply":
                    # 降级：旧 call_chat_ai 链路（人设/历史/缓存布局全沿用）
                    reply = self.call_chat_ai(chat, trigger)
                self._deliver_reply(chat, reply, trigger=True)
                logger.info(f"[定时] 已触发 -> {chat}: {reply[:40]}")
            except Exception as e:
                logger.error(f"[定时] 触发失败 {rid}: {e}")
            finally:
                self.reminders.mark_fired(rid)

    def _drain_conditions(self):
        """消费条件监视事件队列（主循环节点）：达成/到期/失效统一回递 API
        生成角色内回复后发送（与定时触发同一火线，用户定案）。

        触发消息只带条件与页面状况——达成后的措辞由 API 自行生成（content
        提醒事项字段已随「回递即回复」语义废除，用户定案）。
        call_chat_ai 自身会把触发消息与回复写入对话历史（不在此重复写）。
        发送失败不再重试——终态已在 store 落盘（done 标记），重发会造成
        重复打扰。"""
        while True:
            try:
                ev = self._condition_queue.get_nowait()
            except queue.Empty:
                break
            chat = str(ev.get("chat") or "").strip()
            if not chat:
                continue
            condition = str(ev.get("condition") or "")
            evidence = str(ev.get("evidence") or "")
            kind = ev.get("type")
            if kind == "met":
                trigger = (f"[条件达成] 你之前答应帮用户盯着的条件出现了——\n"
                           f"条件：{condition}\n页面状况：{evidence}\n"
                           f"请按人设把这个消息告诉用户。")
            elif kind == "expired":
                trigger = (f"[监视到期] 你之前答应帮用户盯着「{condition}」，"
                           f"到截止时间了仍未达成。最后看到的页面状况：{evidence}\n"
                           f"请按人设告诉用户没等到，别再让他等了。")
            else:  # dead
                trigger = (f"[监视失效] 你之前答应帮用户盯着「{condition}」，但监控"
                           f"的网页一直无法访问（可能改版了），监视被迫停止。\n"
                           f"请按人设向用户说明情况并致歉。")
            try:
                mode, reply = self._trigger_generate_reply(chat, trigger)
                if mode != "silent":
                    if mode != "reply":
                        reply = self.call_chat_ai(chat, trigger)
                    self._deliver_reply(chat, reply, trigger=True)
                logger.info(f"[状态监视] 已回递 -> {chat} ({kind})")
            except Exception as e:
                logger.error(f"[状态监视] 回递失败 {chat}: {e}")

    @staticmethod
    def _chat_name_matches(active, target):
        """当前会话名与目标名是否同一会话（容忍 OCR 差异）。

        口径与 memory 键一致（剥引号变体与空白），并做双向子串兜底——
        OCR 会漏字（真机「“摸鱼”集团」被读成「摸鱼”」），严格相等会
        把切换判成失败。
        """
        a = _memory_key(active)
        b = _memory_key(target)
        if not a or not b:
            return False
        return a == b or a.startswith(b) or b.startswith(a)

    def _ensure_chat_active(self, chat):
        """确保微信窗口停在 chat 会话；返回是否已确认（可以发送）。

        触发式发送（定时/条件到点）必须走这里：那一刻窗口停在「最后处理过
        的会话」上——真机事故：拿快递提醒的意图发给「林小满」，实际落进了
        「摸鱼集团」。判定顺序：

        1. 已在目标会话 → 直接确认（**不点击**：重复点已选中条目会 toggle
           取消选中，消息区反而变空）
        2. 交给后端切换（`_switch_to_chat` → `_switch_chat`：内部先查选中
           高亮/标题，仍缺坐标时现读列表区 OCR 定位）
        3. 切完**读标题复验**；标题不是目标即返回 False（调用方 fail-closed）
        """
        # 1) 已在目标会话：读标题确认，不点击
        try:
            title = self.wx.read_title(foreground=False)
        except Exception:
            title = None
        if title and self._chat_name_matches(parse_title(title)[0], chat):
            return True
        # 2) 切换
        try:
            self._switch_to_chat(chat)
        except Exception as e:
            logger.warning(f"[触发回复] 切换 {chat!r} 异常: {e}")
        # 3) 复验：切完标题必须是目标，否则视为没切成功
        try:
            title = self.wx.read_title(foreground=True)
        except Exception as e:
            logger.warning(f"[触发回复] 读标题失败，无法验证落点: {e}")
            return False
        if not title:
            return False
        return self._chat_name_matches(parse_title(title)[0], chat)

    def _send_trigger_reply(self, text, chat):
        """触发器回复直发（旁路 _send_text 的占位归零——定时/条件触发消息
        不得破坏 skip_bot/N[chat] 语义：占位挂起时若被归零，占位会被当成
        实质回复，用户其后的消息将被漏读）。拆分与段间间隔统一走基类的
        `_send_parts`（与普通回复同一套规则：换行切段、段尾单句号剥离、
        段间固定间隔），避免两处各写一遍造成漂移。

        **发送前必须先确认窗口在目标会话**，确认不了就不发：触发是异步的
        （闹钟/状态监视线程入队 → 主循环消费），此刻 visual 后端的
        `send_text` 只会往「当前窗口」输入框打字，窗口停在哪就发给谁。
        宁可不发并记错误日志，也不把给 A 的消息发进 B。
        """
        if not self._ensure_chat_active(chat):
            logger.error(
                f"[触发回复] 未能确认窗口在会话 {chat!r}，放弃本次发送"
                f"（防发错人）：{str(text or '')[:40]!r}")
            return
        try:
            self._send_parts(chat, text)
        except Exception as e:
            logger.error(f"[触发回复] 发送失败: {e}")

    def _handle_send_voice(self, chat_name, result, user_text):
        """send_voice 工具分支（voice_mode=auto/always）：解析 {text, emotion}
        → 语音发送（任一环节失败内部已回退文本，回复不丢）。

        auto 下工具由模型按语境触发「发语音」；always 下工具是模型选情绪
        的通道（普通回复不走工具、自动转语音走通用音色）。语音内容以
        **纯文本**写记忆（用户定案：记忆存的是说了什么，不是用什么形式
        说的，不加 [语音] 之类标记）。text 缺失返回 None 交上层降级普通聊天。"""
        try:
            args = json.loads(result.get("arguments") or "{}")
        except (ValueError, TypeError):
            return None
        if not isinstance(args, dict):
            return None
        text = str(args.get("text") or "").strip()
        if not text:
            logger.warning("[语音] send_voice 缺 text，降级")
            return None
        if self.voice_state(chat_name) == "off":
            # fail-closed：该聊天语音已关（强制关覆盖 / 全局关闭且未放开）
            # ——模型幻觉调用未声明工具时拒绝发语音，降级普通聊天
            logger.warning(f"[语音] {chat_name!r} 语音已按聊天关闭，降级普通聊天")
            return None
        emotion = str(args.get("emotion") or "").strip() or None
        if user_text:
            self._add_history(chat_name, "user", user_text)
        self._add_history(chat_name, "assistant", text)
        self._send_voice_reply(chat_name, text, emotion=emotion)
        self._after_reply_sent(chat_name, text)
        return True

    def _handle_no_reply(self, chat_name, result, user_text):
        """no_reply 工具分支：模型决定对这条消息保持沉默（不发送任何回复）。

        三件必须做的事（漏一件都会出问题）：
        1. 清红圈：静默 = 全程零点击，微信不会标记已读 → 红圈滞留、8s 退避
           后无限循环重处理同一条消息（与群聊无 @ 跳过路径同一处理：点一次
           输入框标记已读）；
        2. 记忆标注：user 消息 + assistant「[已读未回复]」写入历史——下一轮
           模型看得到这条消息出现过且被看过，上下文不断裂；
        3. 占位归零：静默是「实质处理完成」语义，占位计数不清会把 skip_bot
           撑大、分析区上沿上移，漏读对方后续消息。
        """
        if user_text:
            self._add_history(chat_name, "user", user_text)
        self._add_history(chat_name, "assistant", "[已读未回复]")
        self._pending_placeholders.pop(chat_name, None)
        mark_read = getattr(self.wx, "mark_session_read", None)
        if mark_read is not None:
            try:
                mark_read()
            except Exception as e:
                logger.warning(f"[静默] 标记已读失败: {e}")
        logger.info(f"[静默] {chat_name!r} 模型决定不回复"
                    f"（{str(user_text or '')[:40]!r}）")
        return True

    def _deliver_reply(self, chat, text, trigger=False):
        """模型生成的对话回复统一出口。该聊天语音有效模式为 always
        （voice_state：per-chat 覆盖优先）时先试语音（任一环节失败内部已
        回退文本）；其余模式/语音未启用直接文本发送。

        程序固定文案（占位「正在处理中」「文件已收到」、任务进度、错误
        兜底）不走这里，保持文字——always 语义只覆盖模型说出口的话。
        trigger=True 用触发器文本语义（旁路占位归零），语音回退同样旁路。
        回复完整发出后经 _after_reply_sent 收尾（状态层定量 + 追尾登记）。
        """
        if self.voice_state(chat) == "always" and \
                self._send_voice_reply(
                    chat, text,
                    text_fallback=(lambda t: self._send_trigger_reply(t, chat))
                    if trigger else None):
            self._after_reply_sent(chat, text)
            return
        if trigger:
            self._send_trigger_reply(text, chat)
        else:
            self._send_text(text, chat)
        self._after_reply_sent(chat, text)

    def _record_reply_latency(self, t0):
        """记录一次端到端回复耗时（识别到红圈 → 产出回复），供用量页
        「平均回复耗时」列。记录为 kind="reply"——聚合时不计入 API 调用
        数（summary 里单独走 reply 桶），含媒体防抖等待等全部处理耗时。"""
        store = getattr(self, "usage_store", None)
        if store is None:
            return
        try:
            store.record(kind="reply", model=self.chat_model, ok=True,
                         latency_ms=(time.time() - t0) * 1000.0)
        except Exception as e:
            logger.debug(f"[用量] 回复耗时记录失败: {e}")

    def _exec_set_reminder(self, chat_id, raw_args):
        """set_reminder 工具执行（call_vision_api 工具循环内调用）。

        创建成功/失败一律返回回执文本——模型拿回执后用自己的话自然确认、
        致歉或再问一次（固定确认文案已废，用户定案）。user 消息与最终自然
        回复由 text 分支统一写历史，这里不写（避免重复记录）。"""
        try:
            args = json.loads(raw_args or "{}")
        except (ValueError, TypeError):
            return "set_reminder 参数解析失败，请检查参数格式"
        if not isinstance(args, dict):
            return "set_reminder 参数解析失败，请检查参数格式"
        kind = str(args.get("kind") or "").strip().lower()
        if not kind:
            # 旧格式兼容：给了 time 即定时，给了 url 即状态监视
            kind = "condition" if str(args.get("url") or "").strip() else "time"
        if kind == "condition":
            return self._create_condition_watch(chat_id, args)
        # 无 content 概念（用户定案）：到点/达成只回递事件，回复由 API 结合
        # 创建时的历史对话自行生成，模型不预写任何提醒文案
        return self._create_time_reminder(chat_id, args)

    def _create_time_reminder(self, chat_name, args):
        """创建定时触发器，返回回执文本（成功带 id/时间/重复，失败带原因）。"""
        if getattr(self, "reminders", None) is None:
            return "定时功能不可用（触发器存储未就绪）"
        raw_time = str(args.get("time") or "").strip()
        repeat = str(args.get("repeat") or "once").strip()
        if not raw_time:
            return "缺少触发时间 time（格式 YYYY-MM-DD HH:MM）"
        try:
            fire_at = time.mktime(time.strptime(raw_time, "%Y-%m-%d %H:%M"))
        except (ValueError, TypeError):
            return f"时间格式不对（{raw_time}），应为 YYYY-MM-DD HH:MM"
        if fire_at <= time.time():
            return (f"触发时间 {raw_time} 已经过去了；要现在就做的事不用建"
                    "触发器，请和用户确认清楚时间再试")
        # source="model"：模型创建的触发器内容为空、靠创建时的对话历史兜底
        # （界面「触发器管理」创建的走 webbridge.add_trigger，内容由用户填写，
        # 到点回递必须把内容递过去）
        item = self.reminders.add(chat_name, "", fire_at, repeat, source="model")
        logger.info(f"[定时] 已创建触发器 -> {chat_name} @ {raw_time}（{repeat}）")
        return (f"已创建定时触发：[{item['id']}] {raw_time}（{repeat}）——"
                "到点系统会把触发事件回递给你，届时结合当时的对话自然回复，"
                "不需要预先写提醒文案。请用你自己的话自然地跟用户确认一下")

    def _create_condition_watch(self, chat_name, args):
        """创建状态监视，返回回执文本。

        条件监视不再依赖 content（提醒事项）——达成后是回递 API 让其根据
        条件与页面状况自行回复（用户定案），content 仅作备注存档可缺省。
        开关未开启 → 回执说明原因，模型自然转告用户（不再由程序发固定提示，
        也不降级普通聊天——降级会让模型凭空答应）。"""
        if getattr(self, "reminders", None) is None:
            return "状态监视功能不可用（触发器存储未就绪）"
        if not self.feature_enabled(chat_name, "state_watch"):
            logger.info(f"[状态监视] {chat_name!r} 未开启（全局开关与按聊天"
                        "覆盖均未放开），已回执模型转告用户")
            return ("状态监视功能在这个聊天没有开启（小漓设置页「状态监视」"
                    "全局开关或本聊天的例外里放开）——请自然地告诉用户先去"
                    "开启再让你试一次，不要凭空答应")
        url = str(args.get("url") or "").strip()
        condition = str(args.get("condition") or "").strip()
        if not url.startswith(("http://", "https://")) or not condition:
            return "参数不完整：condition 监视需要合法的 url 与 condition"
        # 解析跳转链到真实目标页（模型常直接选搜索结果里的搜狗 /link——
        # 跳转链接会过期且展示无意义；失败原样保留）
        resolved = resolve_redirect(url)
        if resolved != url:
            logger.info(f"[状态监视] 轮询目标已解析: {url[:60]} -> {resolved[:60]}")
            url = resolved
        judge = str(args.get("judge") or "local").strip().lower()
        match_type = str(args.get("match_type") or "present").strip().lower()
        raw_kws = args.get("met_keywords")
        if isinstance(raw_kws, str):
            raw_kws = re.split(r"[,，、\s]+", raw_kws)
        kws = [str(k).strip() for k in (raw_kws or []) if str(k).strip()]
        if judge == "local" and not kws:
            return ("local 判定需要 met_keywords（按页面实际措辞给全同义形式）；"
                    "条件无法用关键词判断时改用 judge=api")
        try:
            interval = int(float(args.get("interval_seconds") or 60))
        except (TypeError, ValueError):
            interval = 60
        raw_expire = str(args.get("expire_at") or "").strip()
        if raw_expire:
            try:
                expire_at = time.mktime(time.strptime(raw_expire, "%Y-%m-%d %H:%M"))
                if expire_at <= time.time():
                    return "截止时间已经过去了，请和用户确认截止时间再试"
            except (ValueError, TypeError):
                expire_at = time.time() + WATCH_DEFAULT_TTL
        else:
            expire_at = time.time() + WATCH_DEFAULT_TTL
        content = str(args.get("content") or "").strip()  # 备注，可缺省
        item = self.reminders.add_condition(
            chat_name, content, url, condition, judge=judge, match_type=match_type,
            met_keywords=kws, scope_start=args.get("scope_start"),
            scope_end=args.get("scope_end"), interval_seconds=interval,
            expire_at=expire_at)
        logger.info(f"[状态监视] 已创建 -> {chat_name}: {condition[:40]} "
                    f"@ {item['url'][:60]} every {item['interval_seconds']}s")
        exp_s = time.strftime("%m-%d %H:%M", time.localtime(expire_at))
        return (f"已创建状态监视：[{item['id']}]「{condition[:40]}」→ "
                f"{url[:60]}，每 {item['interval_seconds']}s 查一次，"
                f"截止 {exp_s}。达成或到截止系统会把事件回递给你，届时结合"
                "上下文自然回复。请用你自己的话自然地跟用户确认一下")

    def _handle_text(self, chat_name, sender, content, msg_id=None,
                     multi_sender=False, img_paths=None):
        """文本消息统一处理：vision-exp 单调用判断+回复 → 天枢投递 或 普通聊天。

        附件只由文件消息路径投递（_process_file_with_instruction，用户确实
        发了文件时才带）。纯文字任务不带任何附件——历史缺陷：文本路径无条件
        找接收目录"最新"文件，用户没发文件时把无关旧文件投给 agent 造成误判。
        img_paths：引用联动预捕获的图片临时件（引用图片随文字进同一 vision
        调用），可空。恒走 vision 单调用（工具按各自开关在 _vision_tools 逐项
        注入）；调用失败（None）时降级回退普通聊天。
        """
        is_group = getattr(self.wx, "_current_is_group", None)
        if is_group is None:
            is_group = is_group_chat(chat_name)
        if is_group:
            at_tag = f"@{self.nickname}"
            content = content.replace(at_tag, "").strip()
            if not content:
                content = "你好呀～"  # 与基类 process_new_messages 群聊空内容文案一致
        question = content.strip()
        logger.info(f"[MSG] [{chat_name}] {sender}: {question[:80]}")
        # 恒走 vision 单调用：任务桥只是 dispatch_task 一项的开关，不再把守
        # 整条链路——否则任务桥关闭时语音/搜索/闹钟工具对文字消息全部陪葬
        #（实测事故：任务桥关 +「明天9:30提醒我」，模型无工具可调只能闲聊）
        resp = self._vision_route(
            chat_name, sender, question,
            msg_id=msg_id, raw_message=content,
            is_group=is_group, multi_sender=multi_sender, img_paths=img_paths)
        if resp is not None:
            return True
        # vision 降级（None）：API 失败回退普通聊天
        reply = self.call_chat_ai(chat_name, question, sender_name=sender, is_group=is_group, multi_sender=multi_sender)
        self._deliver_reply(chat_name, reply)
        return True

    # ---------- 消息处理（改造） ----------

    def process_new_messages(self):
        self._flush_memory_if_due()
        if self.paused:
            return
        if self._sending_lock:
            # 成果回传期间彻底暂停：不轮询任务目录、不遍历会话，
            # 防止 ChatWith 切走窗口打断文件发送/发错联系人
            return
        # 定时消息消费节点：必须在主循环内发送（窗口互斥）；暂停态走到
        # 不了这里（上面 return），错过的提醒由宽限/滚动逻辑兜底
        if getattr(self, "_reminder_queue", None) is not None:
            self._drain_reminders()
        if getattr(self, "_condition_queue", None) is not None:
            self._drain_conditions()
        # 追尾评估到期消费（主循环节点——发送切会话，窗口互斥纪律同触发器）
        if getattr(self, "_followup_pending", None):
            self._drain_followups()
        # 任务成果轮询不受 cooldown / 任务暂停限制，必须最先执行
        self._tick_poll_outbox()
        resume, self._task_was_active, self._task_end_time = should_resume_listen(
            has_active_tasks(self.tasks_dir),
            self._task_was_active,
            self._task_end_time,
            time.time(),
            self._listen_hold_seconds,
        )
        if not resume:
            # 天枢任务进行中或刚完成（缓冲期内）：暂停消息监听，
            # 缓冲期满后自动恢复，避免 ChatWith 切走窗口打断文件发送
            return
        if time.time() - self.last_reply_time < self.cooldown:
            return
        try:
            # A: 红圈检测
            if hasattr(self.wx, "iter_unread_sessions"):
                # 红圈链路由后端在迭代时点击该行并做了选中复验（几何驱动）
                # → 后续 analyze_window 不必再切一次、再读一次标题
                sessions = list(self.wx.iter_unread_sessions())
                switched = True
            else:
                sessions = list(self.wx.iter_sessions())
                switched = False
            if not sessions:
                return
            for entry in sessions:
                if not entry:
                    continue
                # 退避键：红圈链路给位置条目（坐标，8s 窗口内稳定），
                # 降级路径给会话名。
                key = f"{entry[0]},{entry[1]}" if switched else entry
                # 失败退避：上次处理未产出回复（红圈滞留）的条目暂跳过
                if time.time() - self._chat_fail_at.get(key, 0.0) < self._fail_backoff:
                    logger.debug(f"[退避] {key} 上次处理失败未满 {self._fail_backoff:.0f}s，跳过")
                    continue
                # 每日预算闸：超限当天不发起模型调用，但点输入框清红圈——
                # 不清圈会被 8s 退避无限重处理（与静默路径同一纪律）
                if self._budget_exhausted():
                    mark_read = getattr(self.wx, "mark_session_read", None)
                    if mark_read is not None:
                        try:
                            mark_read()
                        except Exception as e:
                            logger.warning(f"[预算] 标记已读失败: {e}")
                    logger.info(f"[预算] 今日估算消费已达上限，跳过 {key} 的处理"
                                "（红圈已清，明天自动恢复）")
                    continue
                logger.info(f"🔔 发现新消息（未读条目 {key}）"
                            f"{'，会话名由标题区给出' if switched else ''}")
                t0 = time.time()  # 端到端回复耗时起点（识别到红圈）
                try:
                    handled = self._handle_unread_session(entry, switched=switched)
                except Exception as e:
                    logger.error(f"处理会话 {key} 异常: {e}\n{traceback.format_exc()}")
                    handled = False
                if handled:
                    self._record_reply_latency(t0)
                    self._chat_fail_at.pop(key, None)  # 处理成功，解除退避
                    self.last_reply_time = time.time()
                    return  # 每轮只处理一个会话，回复后回到红点监听
                self._chat_fail_at[key] = time.time()
        except Exception as e:
            logger.error(f"Message processing error: {e}\n{traceback.format_exc()}")

    # ---------- 每日预算 / 媒体防抖 / 主动追尾 ----------

    def _budget_exhausted(self):
        """今日估算消费是否已达预算上限（结果缓存 60s）。

        估算口径 = 内置价目（pricing，无价目的调用不计入）——这是「按量
        折算参考」不是账单，设置页/用量页文案均已标注。开关关闭恒 False；
        查询异常按未超限处理（统计故障不该把人挡在门外）。"""
        if not getattr(self, "daily_cost_limit_enabled", False):
            return False
        now = time.time()
        if now - self._budget_cache.get("at", 0.0) < 60.0:
            return self._budget_cache.get("out", False)
        out = False
        try:
            from xiaoli_app import pricing
            from xiaoli_app.usage_store import UsageStore
            limit = float(getattr(self, "daily_cost_limit", 0) or 0)
            if limit > 0:
                est = pricing.estimate_records(
                    UsageStore().load_records(days=2), days=2)
                today_cost = float(est.get("today") or 0.0)
                out = today_cost >= limit
                today = time.strftime("%Y-%m-%d")
                if self._budget_day.get("date") != today:
                    self._budget_day = {"date": today, "flagged": False}
                if out and not self._budget_day.get("flagged"):
                    self._budget_day["flagged"] = True
                    logger.warning(
                        f"[预算] 今日估算消费 ¥{today_cost:.2f} 已达上限 "
                        f"¥{limit:.2f}——今天不再调用模型（红圈仍会清掉，"
                        "明天自动恢复）")
        except Exception as e:
            logger.debug(f"[预算] 查询失败（按未超限处理）: {e}")
        self._budget_cache = {"at": now, "out": out}
        return out

    def _media_debounce(self, chat_name, switched):
        """媒体消息防抖：等对方跟着媒体发的文字一起到，防拆散任务描述。

        双模式（media_wait_mode 用户选）：
        - fixed：固定 media_wait_seconds 秒（历史行为）——对方打长任务描述
          也不怕拆，代价是没跟文字时白等；
        - smart：纯像素静默期检测——每 2s 重数一次对方消息块（analyze_window
          纯像素路径，零 OCR 零 API），出现新块（对方还在发）就重置静默
          计时，连续 media_wait_quiet_seconds 秒无新块（说完了）即继续；
          硬上限 12s 兜底（永远不超过固定档的等待上限量级）。"""
        mode = str(getattr(self, "media_wait_mode", "fixed") or "fixed")
        if mode != "smart":
            time.sleep(max(2.0, float(getattr(self, "media_wait_seconds", 10))))
            return
        quiet_need = max(2.0, float(getattr(self, "media_wait_quiet_seconds", 5)))
        cap = 12.0
        waited = 0.0
        quiet_for = 0.0
        last_n = None
        while waited < cap:
            time.sleep(2.0)
            waited += 2.0
            try:
                w = self.wx.analyze_window(
                    chat_name,
                    skip_bot=self._pending_placeholders.get(chat_name, 0),
                    assume_switched=switched)
                n = len(w.get("other_blocks") or [])
            except Exception:
                n = None
            if last_n is not None and n is not None and n > last_n:
                quiet_for = 0.0  # 对方又发了：重新等静默
            else:
                quiet_for += 2.0
                if quiet_for >= quiet_need:
                    return
            if n is not None:
                last_n = n

    @staticmethod
    def _chat_msg_time(m):
        try:
            return time.mktime(time.strptime(str(m.get("time") or ""),
                                             "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return 0.0

    def _quote_attachment_plan(self, win, window_msgs):
        """从 quote 块提取引用内容联动计划：返回 (图片 1x 框列表, 引用文件名)。

        判型读的是 get_messages 装配好的引用标注（可靠来源）：
        - 「[引用 X发来的图片]」且块带媒体框（缩略图）→ 引用图片，返回其
          1x 框供点击开查看器捕获原图（用户定案：与普通图片同样处理）；
        - 其余引用行能拆出文件名 token（「·」按 OCR 对「.」的高频误读先
          归一）→ 引用文件名，交普通文件流程按显示名定位；
        - 都不是 → 纯文字引用，无联动（标注已随文字进模型）。"""
        boxes, quoted_file = [], None
        for blk in (win.get("other_blocks") or []):
            if blk.get("kind") != "quote":
                continue
            msg = next((m for m in window_msgs if m.y == blk.get("top")), None)
            if msg is None:
                continue
            mobj = _QUOTE_MARKER_RE.match(msg.content or "")
            if not mobj:
                continue
            inner = mobj.group(1)
            if inner.endswith("发来的图片"):
                # 缩略图框：专用检测（quote_media）优先，通用媒体框兜底——
                # 窄缩略图（真机 ~30px 宽）过不了通用 40px 闸
                box = blk.get("quote_media") or blk.get("media")
                if box:
                    boxes.append(box)
                continue
            body = re.split(r"[:：]", inner, 1)[-1].strip()
            token = _extract_file_name_token(body.replace("·", "."))
            if token:
                quoted_file = token
        return boxes, quoted_file

    def _box_screen_rect_safe(self, box):
        """box_screen_rect 的容错包装（测试桩后端可能没有该方法）。"""
        fn = getattr(self.wx, "box_screen_rect", None)
        if fn is None:
            return None
        try:
            return fn(box)
        except Exception as e:
            logger.debug(f"[引用] 缩略图屏幕坐标换算失败: {e}")
            return None

    def _chat_recent_count(self, chat, seconds):
        """该聊天近 seconds 秒内的消息条数（追尾热度判据，读 recent 窗口）。"""
        now = time.time()
        try:
            hist = self._get_history(chat)
        except Exception:
            return 0
        return sum(1 for m in hist if now - self._chat_msg_time(m) <= seconds)

    def _chat_last_user_at(self, chat):
        """该聊天最近一条 user 消息的时刻（追尾取消护栏用）。"""
        try:
            for m in reversed(self._get_history(chat)):
                if m.get("role") == "user":
                    return self._chat_msg_time(m)
        except Exception:
            pass
        return 0.0

    def _followup_schedule(self, chat, is_group=None):
        """回复发出后登记一次追尾评估（发不发由触发时刻的评估调用决定）。

        登记门槛（触发时刻重查）：开关开、（可选）仅私聊、近 10 分钟 ≥6 条
        （聊得热才追尾，冷场追尾像骚扰）、每聊天冷却、当日上限。延迟在
        [followup_delay_min, followup_delay_max] 内随机取——固定间隔是机器味。
        同聊天只保留最新一个待评估（连发多条回复时不叠）。"""
        if not getattr(self, "followup_enabled", False) or not chat:
            return
        if getattr(self, "followup_private_only", True):
            if is_group is None:
                is_group = bool(
                    getattr(getattr(self, "wx", None), "_current_is_group", None)
                    or is_group_chat(chat))
            if is_group:
                return
        now = time.time()
        if self._chat_recent_count(chat, 600) < 6:
            return
        if now - self._followup_last.get(chat, 0.0) < \
                getattr(self, "followup_cooldown_minutes", 120) * 60:
            return
        today = time.strftime("%Y-%m-%d")
        if self._followup_day.get("date") != today:
            self._followup_day = {"date": today, "count": 0}
        if self._followup_day["count"] >= int(getattr(self, "followup_daily_max", 10)):
            return
        lo = float(getattr(self, "followup_delay_min", 45))
        hi = max(lo, float(getattr(self, "followup_delay_max", 90)))
        self._followup_pending = [p for p in self._followup_pending
                                  if p["chat"] != chat]
        self._followup_pending.append(
            {"chat": chat, "due_at": now + random.uniform(lo, hi),
             "scheduled_at": now})

    def _drain_followups(self):
        """消费到期的追尾评估（主循环节点，窗口互斥纪律同定时/条件触发器）。

        固定护栏（不可配置）：等待期间对方又发了消息 → 取消——人家正在说话，
        插一句「对了」很怪。评估小调用无图带历史；[不补]/失败/工具调用一律
        放弃本轮。发送走 _send_trigger_reply（切会话+标题复验防发错人）。"""
        if self.paused or self._sending_lock or self._budget_exhausted():
            # 到期项留在队列里：暂停/回传占用/预算耗尽期间不消费也不丢弃
            # （暂停几小时后恢复，过期项会被「对方已说话」护栏或冷却拦下）
            return
        now = time.time()
        due = [p for p in self._followup_pending if p["due_at"] <= now]
        if not due:
            return
        self._followup_pending = [p for p in self._followup_pending
                                  if p["due_at"] > now]
        for p in due:
            chat = p["chat"]
            if self._chat_last_user_at(chat) > p["scheduled_at"]:
                logger.debug(f"[追尾] {chat!r} 等待期间对方又发消息，取消")
                continue
            if now - self._followup_last.get(chat, 0.0) < \
                    getattr(self, "followup_cooldown_minutes", 120) * 60:
                continue
            today = time.strftime("%Y-%m-%d")
            if self._followup_day.get("date") != today:
                self._followup_day = {"date": today, "count": 0}
            if self._followup_day["count"] >= int(getattr(self, "followup_daily_max", 10)):
                return
            text = self._followup_generate(chat)
            if not text:
                continue
            self._followup_last[chat] = time.time()
            self._followup_day["count"] += 1
            # 纯文本写历史（不加标记——带标记的 assistant 消息会被模型当
            # 「段首格式」复读进后续回复），下一轮模型看得到自己补过这句
            self._add_history(chat, "assistant", text)
            self._send_trigger_reply(text, chat)
            logger.info(f"[追尾] -> {chat}: {text[:40]}")

    def _followup_generate(self, chat):
        """触发时刻的追尾评估小调用。返回追尾文本或 None（放弃本轮）。"""
        try:
            resp = self.call_vision_api(
                [{"type": "text", "text": FOLLOWUP_EVAL_PROMPT}], chat_id=chat)
        except Exception as e:
            logger.debug(f"[追尾] 评估调用失败: {e}")
            return None
        if not resp or resp.get("kind") != "text":
            return None
        t = strip_reply_prefix(str(resp.get("content") or "").strip())
        if not t or "[不补]" in t:
            return None
        return t

    def _after_reply_sent(self, chat, text):
        """模型回复完整发出后的收尾钩子：状态层定量评估（异步小调用）+
        主动追尾登记（到点由主循环评估）。占位/固定文案不经这里。"""
        try:
            from xiaoli_app.persona_state import schedule_eval
            schedule_eval(self, chat, text)
        except Exception:
            pass
        try:
            self._followup_schedule(chat)
        except Exception as e:
            logger.debug(f"[追尾] 登记失败: {e}")

    def _handle_unread_session(self, entry, switched=False):
        """处理一个未读会话：窗口边界（bot 最后回复之后的对方消息）+ 分类分发。

        entry：红圈几何链路给的是**位置条目** `(x, y)`（switched=True——此刻
        窗口已被点过去，会话名还不知道）；降级路径（iter_sessions）给的是
        会话名（switched=False）。

        会话名统一在 `_window_msgs` 之后取——那一步的 `get_messages` 会做
        联合 OCR（标题带 + 消息区一次读）并刷新 `_current_title`，名字从那
        里来。位置模式拿不到名字就放弃本轮（fail-closed，不认错会话）。

        返回 True = 已处理（回复/投递）；False = 无待处理（回到红点监听）。
        """
        at_tag = f"@{self.nickname}"
        entry_is_pos = switched and isinstance(entry, tuple)
        chat_name = None if entry_is_pos else entry

        def _window_msgs(win):
            # assume_switched：analyze_window 刚完成切换+读标题（同一处理
            # 事件），跳过重切与标题重读——事件热路径省一次点击两次 OCR。
            # skip_bot 必须与刚才 analyze_window 用同一个值（win 里带回来）：
            # 它决定「分析区上沿」= 哪条对方头像以下算本轮新消息，值不一致
            # 两次分析的边界就会错位（占位回复剔除失效）。
            msgs = self.wx.get_messages(chat_name, assume_switched=True,
                                        skip_bot=win.get("skip_bot", 0))
            bot_bottom = win.get("bot_bottom")
            # 阈值 = 分析区上沿（bot 最后回复之后的下一条对方头像上边界，
            # analyze_window 的 bot_bottom）。bot_bottom 为 None = 无 bot
            # 消息，窗口内全部视为新，维持原语义。
            return [
                m for m in msgs
                if m.sender not in (None, "self", self.nickname)
                and (bot_bottom is None
                     or (m.y is not None and m.y >= bot_bottom))
            ]

        # D/R: 截图 + 气泡/媒体分析（无 OCR）。skip_bot：跳过最近 N 条 bot
        # 占位回复（_pending_placeholders，占位发送时 +1、实质回复归零），
        # 占位"正在处理中"不顶掉用户的新消息。
        win = self.wx.analyze_window(
            chat_name, skip_bot=self._pending_placeholders.get(chat_name, 0),
            assume_switched=switched)
        if not (win.get("has_other") or win.get("has_text") or win.get("has_media")):
            logger.info(f"[跳过] {chat_name or entry} 窗口空（bot 已回复或无对方消息）")
            return False
        # 读窗口内对方新消息（get_messages 内部 read_title 会刷新
        # _current_is_group 为本次会话权威值——群聊判定必须在这之后读取，
        # 否则私聊被上一轮群聊残留误判为群聊（实测日志「私聊林小满被判
        # 群聊消息未 @小漓」；analyze_window 本身无 read_title 不刷新）。
        window_msgs = _window_msgs(win)
        # F/H: 有多媒体（图片或文件卡片，analyze_blocks 的 kind=file 计入
        # has_media；quote 不算——引用内容物不点击不裁剪无需防抖）→ 防抖
        # 等待（双模式用户选）后再分析
        if win.get("has_media"):
            self._media_debounce(chat_name, switched)
            win = self.wx.analyze_window(
                chat_name, skip_bot=self._pending_placeholders.get(chat_name, 0),
                assume_switched=switched)
            if not (win.get("has_other") or win.get("has_text") or win.get("has_media")):
                return False
            window_msgs = _window_msgs(win)
        # 会话身份以标题区读取为准（用户定案）：标题解析出的权威名统一用于
        # 记忆/回复/日志。位置模式（红圈几何链路）下 chat_name 此刻还是 None
        # ——名字只能从这里拿（`_window_msgs` 的联合 OCR 刚刷新了
        # `_current_title`）；拿不到就放弃本轮：宁可漏一条，也不能把别的
        # 会话的消息当目标处理。
        canonical = (getattr(self.wx, "_current_title", "") or "").strip()
        if canonical:
            if chat_name and canonical != chat_name:
                logger.info(f"[身份] 会话名以标题为准: {chat_name!r} -> {canonical!r}")
            chat_name = canonical
            logger.info(f"[身份] 本轮会话: {chat_name!r}（标题区）")
        elif not chat_name:
            logger.warning(
                f"[处理] 条目 {entry} 位置模式下标题区为空，无法确定会话身份 → 放弃本轮")
            return False
        # 位置模式：首次分析时还不知道会话名，skip_bot 只能按 0 走；拿到权威名后
        # 若该会话确实挂着占位回复，再分析一次（纯像素、零 OCR）。占位计数决定
        # bot_bottom 起点，跳错会漏读/误读对方新消息。
        skip = self._pending_placeholders.get(chat_name, 0)
        if skip and entry_is_pos:
            logger.debug(f"[处理] {chat_name!r} 有 {skip} 条占位回复，按名重算 skip_bot")
            win = self.wx.analyze_window(chat_name, skip_bot=skip,
                                         assume_switched=True)
            window_msgs = _window_msgs(win)
        # 群聊判定（本次会话权威值）：联合 OCR 的标题解析已在 _window_msgs
        # 内刷新 _current_is_group（标题不再在 analyze 阶段单独读——事件内
        # OCR 两次封顶）；缺失时回退名称启发式。
        is_group = getattr(self.wx, "_current_is_group", None)
        if is_group is None:
            is_group = is_group_chat(chat_name)
        # 群聊 @ 过滤：只有 @小漓 的消息才处理
        if is_group:
            window_msgs = [m for m in window_msgs if at_tag in m.content]
            if not window_msgs:
                logger.info(f"[跳过] {chat_name} 群聊消息未 {at_tag}")
                # 无 @ 跳过回复 = 全程零点击，而微信只在会话获得交互时标记
                # 已读——红圈原样滞留 → 8s 退避后无限循环重处理（其余流程
                # 发回复时点输入框顺带清圈）。点一次输入框标记已读防循环
                mark_read = getattr(self.wx, "mark_session_read", None)
                if mark_read is not None:
                    try:
                        mark_read()
                    except Exception as e:
                        logger.warning(f"[已读] 标记已读失败: {e}")
                return False
        sender = window_msgs[-1].sender if window_msgs else chat_name
        # 文件识别：视觉层判定的文件卡片（type=FILE）唯一判据——归属/类型
        # 全由色块与头像给出。OCR 扩展名旁路已删：含扩展名的普通文字（如
        # 「报告.docx 发我一下」）会被劫持进文件流程回「下载失败」；图标
        # 判据漏检的最坏退化只是「AI 对着文件名聊天」，重发即可恢复。
        file_text = next(
            (m.content.strip() for m in window_msgs if m.type == MessageType.FILE),
            None)
        # 文字部分（排除文件消息）。多条合并时**逐行发送者名只在「群聊且
        # 发送者确实不止一人」时加**——前缀本是为区分群聊多发送者设计的；
        # 私聊 sender 恒等于会话名，逐行加名再被外层「私聊 - sender：」包
        # 一次 = 双层嵌套（真机事故：记忆里存下「私聊 - 王文生：王文生：
        # 话说 ⏎ 王文生：…」）；群聊同一人连发多条同理，外层按单条语义包
        # 过一次名即可。
        text_candidates = [
            m for m in window_msgs
            if m.content.strip() and m.type != MessageType.FILE
        ]
        senders = {(m.sender or chat_name) for m in text_candidates}
        multi_sender = bool(is_group) and len(text_candidates) > 1 \
            and len(senders) > 1
        if multi_sender:
            text_parts = [
                f"{m.sender or chat_name}：{m.content.strip()}"
                for m in text_candidates
            ]
        else:
            text_parts = [m.content.strip() for m in text_candidates]
        text_content = "\n".join(text_parts)
        has_media = bool(win.get("has_media"))
        # sender 关联：该发送者之前发过文件、现在发来文字指令
        if sender in self._pending_files and text_content:
            pending = self._pending_files.pop(sender)
            logger.info(f"[文件] {sender} 发来处理指令，关联到待处理文件 {pending['filename']}")
            return self._process_file_with_instruction(
                pending["chat_name"], sender, pending["file_path"],
                pending["filename"], text_content, multi_sender=multi_sender)
        # ============ 分类分发 ============
        # 引用内容联动（用户定案）：引用图片 → 点击引用区缩略图开查看器捕获
        # 原图，随文字一起进 vision；引用文件 → 按显示名定位文件走普通文件
        # 流程；纯文字引用维持标注语义。
        quote_boxes, quoted_file = self._quote_attachment_plan(win, window_msgs)
        quote_atts = []
        if quote_boxes:
            rects = [r for r in (self._box_screen_rect_safe(b)
                                 for b in quote_boxes) if r]
            if rects:
                logger.info(f"📎 引用图片：点击缩略图捕获原图（{len(rects)} 张）")
                quote_atts = self._capture_media_rects(rects)
        try:
            if quoted_file:
                # 引用文件优先于一切分支：找到文件 → 与「文件+指令」同一流程
                # （vision 判任务投递 / 非任务读内容回复）；找不到（如引用的
                # 是小漓自己发的文件，接收目录没有）→ 落回文字，引用标注里
                # 已带文件名，模型能自然回应
                file_path = self._find_file_by_display_name(quoted_file)
                if not file_path:
                    time.sleep(3)  # 微信下载落盘有延迟，等一拍按同名再找一次
                    file_path = self._find_file_by_display_name(quoted_file)
                if file_path:
                    logger.info(f"📎 判断为引用文件消息：{chat_name}（{quoted_file}）")
                    return self._process_file_with_instruction(
                        chat_name, sender, file_path, os.path.basename(file_path),
                        text_content, multi_sender=multi_sender)
                logger.info(f"[引用] 接收目录未找到引用的文件 {quoted_file!r}，按文字处理")
            if file_text:
                # 文件（可能同时有图片）：对方本轮新图一并投递，不再被文件分支吞掉。
                # 文件卡片本身不会进媒体框（面板内部的图标已在像素层剔除），
                # 所以这里不需要再按行剔除图标碎片；min_top=分析区上沿只放行
                # 本轮对方的真图片。
                extra_attachments = []
                if has_media:
                    extra_attachments = self._capture_media_images(
                        chat_name, min_top=win.get("bot_bottom"))
                logger.info(f"📁 判断为文件消息：{chat_name}（{file_text[:40]}）")
                try:
                    return self._handle_file_message(
                        chat_name, sender, file_text, text_content,
                        extra_attachments=extra_attachments,
                        multi_sender=multi_sender)
                finally:
                    # 临时图片由本分支捕获：dispatch 同步复制进任务目录后原件即
                    # 无用，非任务/降级分支同样不留残留（与 _process_pure_image 对齐）
                    for p in extra_attachments:
                        try:
                            os.unlink(p)
                        except OSError:
                            pass
            if has_media and text_content:
                # 图片 + 文字 → 全部新图 + 文字一次 vision 调用，任务投递（全图
                # 截图），非任务组装；引用图片的捕获件一并随调（quote_atts 由
                # 本层 finally 统一清理，此处只消费不删）
                logger.info(f"🖼💬 判断为图片+文字消息：{chat_name}")
                return self._handle_image_with_text(
                    chat_name, sender, text_content, multi_sender=multi_sender,
                    min_top=win.get("bot_bottom"), extra_imgs=quote_atts or None)
            if has_media and not text_content:
                # 无文字 + 有媒体（图片/视频/表情统一当图片）→ 逐张 Ctrl+C/裁剪
                logger.info(f"🖼 判断为图片消息：{chat_name}")
                if self._process_pure_image(chat_name,
                                            min_top=win.get("bot_bottom")):
                    return True
                # 失败必须回一句：发送点输入框顺带清红圈，防滞留循环
                self._send_text("图片识别失败了，可能是什么地方出了问题呀～", chat_name)
                return True
            if text_content:
                # 纯文字（含引用文字）→ 任务判断 + 聊天；引用图片捕获件随调
                logger.info(f"💬 判断为文字消息：{chat_name} {text_content[:40]!r}")
                return self._handle_text(
                    chat_name, sender, text_content, None,
                    multi_sender=multi_sender, img_paths=quote_atts or None)
            if win.get("has_other") and not has_media:
                # 纯空块轮：对方只发了微信自带小表情（❤ 实测 33x28px——过不了
                # 媒体检测的 40px 尺寸闸，OCR 也无文字）。旧逻辑整条丢弃且不发
                # 任何东西清红圈 → 红圈滞留、8s 退避循环重处理（真机事故）。
                # 按块裁剪当前帧走表情识别，正常角色化回复；识别失败也要回一句
                # 清圈（与纯图路径的失败语义一致）。
                blocks = [b for b in (win.get("other_blocks") or [])]
                logger.info(f"💜 判断为小表情消息：{chat_name}（{len(blocks)} 个空块）")
                if self._process_emoji_blocks(chat_name, blocks):
                    return True
                self._send_text("这条小鱼没看清楚呀，再发一次好不好", chat_name)
                return True
            return False
        finally:
            # 引用图片捕获件：被 vision/dispatch 同步消费后原件即无用，统一清理
            for p in quote_atts:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _handle_file_message(self, chat_name, sender, file_text, text_content, extra_attachments=None, multi_sender=False):
        """文件消息处理：按显示名定位（重名取 (N) 最大）→ 处理或回复收到并询问。

        文字提取延迟到真正需要时（非任务分支喂 AI）才做，任务分支只投
        文件本体——避免对任务文件做多余的 _extract_file_text。
        """
        file_dir = self.file_storage_path
        if not file_dir or not os.path.isdir(file_dir):
            self._send_text("文件下载失败，请重试～", chat_name)
            return True
        # 拆出干净文件名（OCR 可能读到 "文件名.docx 20.1K W" 含大小+图标字符，
        # 20.1K 的小数点会坑 splitext——先 token 拆出真实文件名）
        clean_name = _extract_file_name_token(file_text) or file_text
        file_path = self._find_file_by_display_name(clean_name)
        if not file_path:
            time.sleep(3)  # 微信下载落盘有延迟，等一拍按同名再找一次
            file_path = self._find_file_by_display_name(clean_name)
        if not file_path:
            self._send_text("文件下载失败，请重试～", chat_name)
            return True
        filename = os.path.basename(file_path)
        if text_content:
            # 文件 + 伴随文字 → 按指令处理（任务判断 or 文件识别附带指令）
            return self._process_file_with_instruction(
                chat_name, sender, file_path, filename, text_content,
                extra_attachments=extra_attachments, multi_sender=multi_sender)
        # 无伴随文字 → 回复收到 + 记录 sender 关联（不停摆，等该 sender 后续指令）
        self._pending_files[sender] = {
            "chat_name": chat_name,
            "file_path": file_path,
            "filename": filename,
        }
        self._send_text("文件已收到～请告诉我需要怎么处理呢？", chat_name)
        return True

    def _handle_image_with_text(self, chat_name, sender, text_content,
                                multi_sender=False, min_top=None,
                                extra_imgs=None):
        """图片 + 文字：vision-exp 单调用（全部新图 + 文字一次判断 + 回复）。

        min_top：消息区 1x 下沿阈值（analyze_window 的 bot_bottom），只捕获
        对方本轮新图。extra_imgs：引用联动预捕获的图片临时件（引用缩略图
        点击复制/裁剪所得），与本轮新图一并进同一次 vision 调用，finally
        统一清理（外层对同一批的二次清理是 OSError 空操作）。任务 → 全部
        图 + 文字投递（tool_call 不发送回复文本）；非任务 → 直接回复（不再
        两段式：GLM-4V 描述转述 + 主模型二次回复）。vision 调用失败（None）
        → 降级回退纯文字处理（保持现状）。
        """
        # 恒走 vision 单调用（旧闸下任务桥关闭时图+文直接掉进纯文字处理，
        # 图片内容完全丢失）；工具由 _vision_tools 按开关逐项注入
        img_paths = self._capture_media_images(chat_name, min_top=min_top)
        if extra_imgs:
            img_paths = list(extra_imgs) + list(img_paths)
        try:
            resp = self._vision_route(
                chat_name, sender, text_content, img_paths=img_paths,
                msg_id=None, raw_message=text_content,
                multi_sender=multi_sender)
            if resp is not None:
                return True
        finally:
            # dispatch 同步复制附件进任务目录，路由返回后临时文件即无用；
            # 文本/触发器/降级分支同样不留残留（与 _process_pure_image 对齐）
            for p in (img_paths or []):
                try:
                    os.unlink(p)
                except OSError:
                    pass
        # 降级：vision 失败 → 回退纯文字处理
        return self._handle_text(
            chat_name, sender, text_content, None, multi_sender=multi_sender)

class TianshuController(Controller):
    HELP = {
        **Controller.HELP,
        "tianshu-window": "重新选择天枢 CLI 窗口",
        "task-status": "查看任务流转状态（等待/完成/归档）",
    }

    def _register_commands(self):
        """继承基类全部命令，扩展天枢任务桥命令（历史缺陷：整段复制
        Controller._listen 的 if/elif 链，新增命令全靠复制粘贴）。"""
        super()._register_commands()
        self._commands.update({
            "tianshu-window": self._cmd_tianshu_window,
            "task-status": self._cmd_task_status,
        })

    def _cmd_tianshu_window(self, cmd):
        was_paused = self.bot.paused
        self.bot.paused = True
        logger.info("⏸️  已暂停，正在列出窗口...")
        title = self.bot._select_window()
        if title:
            logger.info(f"🔄 天枢窗口已切换为：{title}")
        else:
            logger.info("已取消")
        self.bot.paused = was_paused
        if not self.bot.paused:
            logger.info("▶️  已自动恢复回复")

    def _cmd_task_status(self, cmd):
        self._show_task_status()


    def _show_task_status(self):
        """任务状态展示（与 GUI 任务页共用 scan_task_status）。"""
        entries, waiting, done, archived = scan_task_status(self.bot.tasks_dir)
        if not os.path.isdir(self.bot.tasks_dir):
            print("任务目录不存在")
            return
        tags = {"done": "✅ 天枢已完成", "waiting": "⏳ 天枢处理中",
                "archived": "📦 已归档"}
        for name, state, desc, _mtime in entries:
            print(f"  - {name} [{tags.get(state, state)}] {desc}")
        if archived:
            print(f"已归档: {archived} 个任务")
        print(f"统计: {waiting} 等待中 / {done} 待回传")


# =====================================================================
# 入口
# =====================================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="小漓合并版（聊天 + 天枢任务桥）")
    parser.add_argument("--test", action="store_true", help="运行自检（不连微信、不唤起窗口）")
    parser.add_argument("--run", action="store_true", help="启动微信机器人（合并版）")
    args = parser.parse_args()

    if args.test:
        # 懒加载：selftest 反向 import xiaoli_bot，模块级互导会成环
        from xiaoli_app.selftest import run_self_test
        sys.exit(run_self_test())
    elif args.run:
        if not acquire_single_instance():
            logger.error("检测到已有小漓实例在运行（双开会导致任务并发处理冲突、文件回传失败），本实例退出")
            sys.exit(1)
        cfg = load_merged_config("config.json")
        # 联网搜索代理注入（仅作用于 web_search/web_fetch，不影响模型 API）
        from xiaoli_app.web_search import set_proxy
        set_proxy(cfg.get("web_proxy", ""))
        bot = AgentBot(cfg)
        # 首次启动：选择天枢 CLI 窗口
        if cfg.get("task_enabled") and not cfg.get("tianshu_window_title"):
            logger.info("首次启动：请选择天枢 CLI 窗口")
            bot._select_window()
        ctrl = TianshuController(bot)
        ctrl.start()
        # quit 命令 → stop_event → bot.run 优雅退出（节流窗口内记忆 flush 落盘）
        bot.run(stop_event=ctrl.stop_event)
    else:
        print("用法: python xiaoli_bot.py --test | --run")
