# -*- coding: utf-8 -*-
import time
import json
import os
import ctypes
import logging
import random
import re
import threading
import traceback
import requests
import base64
import pyautogui
# 无人值守自动化（用户定案）：小漓共享物理鼠标且处理消息时要前置微信点击，
# 用户鼠标恰停在/甩到屏幕四角时 fail-safe 会拒绝全部输入动作——回复静默
# 丢失且失败会话反复重处理白烧 API。「人工夺回控制权」由暂停/恢复（托盘/
# CLI）承担，不依赖此保护，故关闭。
pyautogui.FAILSAFE = False
import tempfile
from wx_backend import create_backend
from wx_backend.visual_backend import (
    ensure_window_visible,
    find_window_by_title,
    window_rect,
    default_right_half_rect,
    position_window_visible,
)
from xiaoli_app.config_store import AI_DEFAULTS, REPLY_STYLE_RULES
from xiaoli_app.file_text import (_FILE_TOKEN_LOOSE_RE, _FILE_TOKEN_RE,
                                  _extract_file_name_token,
                                  extract_file_display_name,
                                  extract_file_text,
                                  extract_office_com_text)
from xiaoli_app.media_capture import MediaCaptureMixin
from xiaoli_app.usage_store import UsageStore
from xiaoli_app.tts import (GptSovitsClient, TtsError, strip_unspeakable,
                            synthesize_with_emotion, wav_duration_seconds)
from xiaoli_app.web_search import (web_search, web_fetch, WebSearchError,
                                   format_search_results)


def models_endpoint(chat_url):
    """由聊天端点推导模型列表端点：把末尾的 /chat/completions 换成 /models。
    用 rsplit 只替换最后一处（str.replace 会替换所有出现处——自定义端点
    URL 里同一子串出现多次时拼接错误）；不含该子串时原样返回（保持旧行为，
    请求是否有效由调用方/API 决定）。"""
    if not chat_url or "/chat/completions" not in chat_url:
        return chat_url
    return chat_url.rsplit("/chat/completions", 1)[0] + "/models"


def is_group_chat(chat_name):
    """群聊判定的名称启发式兜底（含「群/集团」字）。

    权威判定在视觉链路：标题区括号人数（visual_backend.parse_title）解析出
    _current_is_group，处理事件内随联合 OCR 刷新；本函数只在读不到标题时
    兜底。已知边界：普通群名（如'哆菈A夢'）不含「群/集团」字会漏判
    （README 技术说明有记）。
    """
    name = chat_name or ""
    return "群" in name or "集团" in name


def strip_model_prefix(model):
    """剥离模型名的厂商前缀：'deepseek:deepseek-v4-flash' → 'deepseek-v4-flash'。

    配置/卡片里模型 id 沿用「厂商:模型」前缀格式（UI 分组展示用），
    但 API 只认纯模型名——透传带前缀的 model 会得到 400。
    """
    if not model or ":" not in model:
        return model
    return model.split(":", 1)[-1]


# token 估算 / 上下文预算裁剪 / 重试常量 / ApiCallError / LLM 调用客户端
# 已抽到 xiaoli_app.llm_client（文件瘦身）；同名 re-export 保持既有导入
# 路径（tests / tools 直接 import 这些名字）。
from xiaoli_app.llm_client import (
    RETRY_AFTER_GIVEUP, BACKOFF_BASE, BACKOFF_CAP, API_WALL_BUDGET_DEFAULT,
    ApiCallError, LlmClient,
    estimate_tokens, fit_messages_in_budget,
)


LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")
# 前端视图日志（INFO 及以上）：GUI 日志页/首页运行日志区只读这个文件，
# 事件流干净；排障看 bot.log 全量（含 DEBUG）。
RUN_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "bot_run.log")

# 日志双轨轮转：bot.log 全量 DEBUG、bot_run.log 前端 INFO，各 2MB 轮转
# 保留 2 份历史。历史缺陷：模块级 import 时即清空 bot.log——每次启动丢
# 日志（排障无法追溯），且 GUI/CLI 并发 import 互清。轮转后 LogPage 的
# size<offset 检测会自动重置增量读取位置，无需改动。
from logging.handlers import RotatingFileHandler

_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024,
                                    backupCount=2, encoding="utf-8")
_run_handler = RotatingFileHandler(RUN_LOG_FILE, maxBytes=2 * 1024 * 1024,
                                   backupCount=2, encoding="utf-8")
_run_handler.setLevel(logging.INFO)
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[_file_handler, _run_handler, logging.StreamHandler()],
    force=True
)
# 终端只显示 INFO 及以上（handlers[2] = StreamHandler），bot.log 保留 DEBUG
logging.getLogger().handlers[2].setLevel(logging.INFO)
# comtypes 的模块导入/缓存 INFO 噪音（视觉链路反复触发）不进日志双轨：
# 前端运行日志区被 "Imported existing <module 'comtypes.gen'...>" 刷屏的根因
logging.getLogger("comtypes").setLevel(logging.WARNING)
logger = logging.getLogger("xiaoli")


# chat_model 为空时的共用兜底（chat / vision 两链路）：DeepSeek vision-exp。
# 必须是 strip 后的纯名——API 只认纯模型名，带厂商前缀的兜底请求必 400
# （历史缺陷：旧常量带 "deepseek:" 前缀，活跃卡缺失被模板补建（chat_model
# 空）后 vision 兜底请求全部 400）。call_chat_ai 同样兜底，防空模型原样发出。
VISION_MODEL_DEFAULT = "deepseek-v4-flash-vision-exp"

# 重试常量与 ApiCallError 由 llm_client re-export 提供（见上方导入）

# vision 工具循环的补全调用上限：搜索 + 抓取（含换源重试一次）+ 作答（防循环烧钱）
VISION_TOOL_ROUNDS = 4

# 回复长度的物理上限（两条链路共用：vision 与 chat）。历史默认 10000 ≈ 没有
# 限制，长回复就是「AI 味」最直接的来源；配合 REPLY_STYLE_RULES 的 1-3 句
# 纪律一起用。带 reasoning 的模型 max_tokens 包含思考 token，压太狠会出空
# 回复（空 content 已有降级保护，不会炸），故留 400 起步。
REPLY_MAX_TOKENS = 400

# always 语音模式纪律（用户定案：模型**必须**经 send_voice 回复——工具是
# 模型挑情绪的唯一通道）。模型偶尔仍会直接输出文字：_deliver_reply 的通用
# 音色自动转语音兜底不撤，回复不丢。
ALWAYS_VOICE_RULES = (
    "【语音模式】你现在的回复会以语音消息发出：每一条回复都必须调用 "
    "send_voice 工具来发——text 写要说出口的内容（口语化、适合念出来，"
    "不要用颜文字/emoji/符号），emotion 按当前语境从可用情绪里挑一个。"
    "不要直接输出文字回复。"
)

# 回复分段的段间间隔（秒，用户定案：固定 2s）。注意底层视觉后端「发一条」
# 本身约 0.6~0.8s（点输入框 / 剪贴板 / 回车），本间隔是在它之上的额外等待；
# 首段前不等、末段后不等。
REPLY_SEGMENT_INTERVAL_SECONDS = 2.0


def _strip_trailing_period(part):
    """剥掉段尾的中文句号（只剥**单个**；`。。` 等连续句号一律不动）。

    微信里真人很少打句号，末尾挂着句号最像「写作文」；感叹号/问号保留
    （用户定案：语气靠它们）。英文句点不处理——避免误伤网址、小数、版本号。
    """
    if part.endswith("。") and not part.endswith("。。"):
        return part[:-1]
    return part


# API 最终失败时的角色内兜底回复（不写入对话历史；池子随机避免机器人复读同句）
FRIENDLY_API_ERROR_REPLIES = (
    "呜…API 那头刚刚没了回应(´･ω･`)，不是不理你，稍后再喊我一次好不好",
    "(>_<) 后端突然开小差了，这句没接住，你再来一次我肯定在",
    "刚刚信号断了一拍(´･ω･`)，等下再发一遍吧，这次一定接住",
)


# memory 键归一化 + memory.json v2 迁移 + 深层 jsonl 文件级操作已抽到
# xiaoli_app.memory_store（文件瘦身）；同名 re-export 保持既有导入路径
# （tests / 记忆管理页直接 from wechat_bot import 这些名字）。
from xiaoli_app.memory_store import (
    MemoryStore,
    _QUOTE_CHARS,
    memory_key as _memory_key,
    migrate_memory_data,
    deep_count_lines,
    deep_read_page,
    deep_delete_line,
)


# 文件名 token 提取/文件文本解析已拆至 xiaoli_app.file_text（re-export 保持
# 既有导入路径：xiaoli_bot 与 tests 直接 from wechat_bot import 这些名字）。


# OCR 对文件名里的分隔符常漏读（真机事故：磁盘名「小漓_深海小剧场.html」
# OCR 成「小漓深海小剧场.html」——下划线在基线上，是 RapidOCR 的经典漏读
# 字符）——按名查找前把两侧的分隔符剥掉再比，容忍 OCR 丢字。
_NAME_SEP_RE = re.compile(r"[ \t\u3000_＿\-－]+")


def _norm_file_stem(stem):
    """文件名主干匹配归一化：剥掉 OCR 易漏读的分隔符（下划线/连字符/空白）。"""
    return _NAME_SEP_RE.sub("", stem or "")


def _find_file_by_display_name_impl(file_dir, display_name):
    """按消息中的显示文件名在接收目录定位对方发来的文件（模块级，单测直测）。

    微信 4.x 下载命名 '<hash>_<msgid>_m_<原名>'，目录文件名包含原名
    （分隔符归一化后包含匹配）。同名文件重复落盘时微信追加 (N) 重名后缀，
    但 (N) 只在同一序列里单调——跨月目录重收时新副本可能不带后缀，编号反而
    更小（真机：2026-10 的新副本输给 2026-09 的 (4) 副本）。改为**下载时间
    优先**：ctime（= 文件落到本机的时间）新者胜，(N) 编号最大只作同刻平局
    的次序（同刻意味着微信硬链接复用同一份文件，取哪个都一样）。

    后缀参与匹配：主干命中后优先取后缀与显示名一致的候选。（真机事故：
    同名 .7z 自己攒出 (2) 编号，跨类型压倒刚收的 .pdf——(N) 最大语义只在
    同类型重收序列里成立，后缀不一致的候选不得参战；OCR 正确读出后缀，
    扔掉它是白白浪费判别信息。）同后缀候选为空（OCR 误读后缀/显示名无
    后缀）时回退全部主干命中，保持既有容错不回归。

    不做 bot 发送副本排除（旧快照/成果登记方案已删）：名字锚定查找下，
    bot 的发送副本只在同名时进候选，而用户回传的那份 ctime 必然更新——
    下载时间优先天然选中用户文件，不再被误排除（真机缺陷：回传落在发送后
    300s 内被「成果副本」规则拦下；后续又发现 OCR 漏读文件名下划线导致
    匹配失败，加分隔符归一化根治）。
    返回路径或 None"""
    if not display_name:
        return None
    if not file_dir or not os.path.isdir(file_dir):
        return None
    dsplit = os.path.splitext(display_name)
    dstem = _norm_file_stem(re.sub(r"\(\d+\)$", "", dsplit[0]))
    dext = dsplit[1].lower()
    if not dstem:
        return None
    best = None
    best_ext = False  # 当前 best 是否与显示名同后缀（True 后异后缀不再参战）
    best_key = (-1.0, -1)  # (下载时间 ctime, 微信重名编号 N)：ctime 新者胜
    try:
        for root, dirs, files in os.walk(file_dir):
            for fname in files:
                fsplit = os.path.splitext(fname)
                fstem_full = fsplit[0]
                fstem = _norm_file_stem(re.sub(r"\(\d+\)$", "", fstem_full))
                if dstem not in fstem:
                    continue
                ext_match = bool(dext) and fsplit[1].lower() == dext
                if best_ext and not ext_match:
                    continue
                full = os.path.join(root, fname)
                try:
                    ts = os.path.getctime(full)
                except OSError:
                    continue
                m_dup = re.search(r"\((\d+)\)$", fstem_full)
                dup = int(m_dup.group(1)) if m_dup else 0
                key = (ts, dup)
                if best is None or (ext_match and not best_ext) \
                        or key > best_key:
                    best_key = key
                    best = full
                    best_ext = ext_match
    except Exception as e:
        logger.error(f"[文件] 按文件名定位失败: {e}")
        return None
    if best:
        logger.info(f"[文件] 按消息文件名定位: {os.path.basename(best)}")
    return best


# 模型偶发复读进回复的开头标记：历史注入的 [time] 前缀、私聊/群聊装饰
# 「私聊 - sender」「群聊 - 群名」。剥除后再记录历史 + 发送，避免污染记忆
# 并防止后续回复继续复读。
# re.M + 行首锚定：时间戳常被模型当「段首标注」复读——多段回复的第二段
# 开头（真机：'诶？\n\n[2026-10-04 17:23:04] 这张图哪来的呀'，只锚字符串
# 开头会漏网、原样发给用户）。行首剥除；行中（正文里的时间表达）不剥，
# 避免误伤用户要 AI 写的带时间正文。^[ \t]* 用空格制表符而非 \s——re.M
# 下 \s 含换行会跨行误剥。
_REPLY_PREFIX_RE = re.compile(
    r'^[ \t]*(?:'
    r'\[\d{4}-\d{1,2}-\d{1,2}\s*\d{1,2}:\d{2}(?::\d{2}|:xx)?\]'
    r'|\[(?:私聊|群聊)(?:\s*[-—:：]\s*[^\]]*)?\]'
    r')[ \t]*\n?',
    re.M,
)


def strip_reply_prefix(reply):
    """剥掉模型偶发复读进回复的开头标记，可叠加（如 [ts][私聊 - 林小满]），循环剥到干净。"""
    if not reply:
        return reply
    while True:
        cleaned = _REPLY_PREFIX_RE.sub("", reply, count=1)
        if cleaned == reply:
            return reply
        reply = cleaned


def _tool_call_name(tc):
    """取 tool_call 的 function.name（畸形结构返回空串）。"""
    try:
        return str((((tc or {}).get("function") or {}).get("name")) or "")
    except AttributeError:
        return ""


def _tool_arg(tc, key):
    """取工具调用 arguments JSON 里的字符串参数（解析失败返回空串）。"""
    try:
        args = json.loads(((tc or {}).get("function") or {}).get("arguments") or "{}")
    except (ValueError, TypeError):
        return ""
    return str(args.get(key) or "").strip() if isinstance(args, dict) else ""


class WeChatBot(MediaCaptureMixin):
    def __init__(self, cfg, stop_event=None, max_connect_retries=None):
        """stop_event：微信连接重试可被外部中断（GUI 引擎停止时用，None=不中断）。
        max_connect_retries：连接失败重试上限（None=无限重试，CLI 模式保留）；
        GUI 传入有限值，超限抛异常 → 引擎进入 error 状态（可重新初始化）。"""
        self._stop_event = stop_event
        self._max_connect_retries = max_connect_retries
        self._connect_retry_interval = 10  # 微信连接失败重试间隔（秒）；测试可调小
        self.nickname = cfg["bot_nickname"]
        self.api_url = cfg["ai_api_url"]
        self.api_key = cfg["ai_api_key"]
        # 视觉端点沿用聊天端点（单模型化：call_vision_api 的 model 取 chat_model）
        self.vision_api_url = cfg.get("vision_api_url") or self.api_url
        self.vision_api_key = cfg.get("vision_api_key") or self.api_key
        self.chat_model = strip_model_prefix(cfg["chat_model"])
        self.chat_temperature = cfg.get("chat_temperature", 0.7)
        self.chat_top_p = cfg.get("chat_top_p", 0.9)
        # 回复长度物理上限：两条链路共用（历史键名 vision_max_tokens 已由
        # config_store 一次性迁移到 reply_max_tokens——单键名，不再双语义）。
        # 带推理的模型 max_tokens 包含思考 token，压太狠会出空回复（空
        # content 已有降级保护），调高场景见 AI_DEFAULTS 注释。
        self.reply_max_tokens = max(
            100, int(cfg.get("reply_max_tokens", REPLY_MAX_TOKENS)))
        self.system_prompt = cfg.get("system_prompt", AI_DEFAULTS["system_prompt"])
        self.max_history = cfg.get("max_history", AI_DEFAULTS["max_history"])
        # 上下文预算（chat/vision 两条链路共用的 fit_messages_in_budget 裁剪依据）
        self.max_context_tokens = max(
            1000, int(cfg.get("max_context_tokens",
                              AI_DEFAULTS["max_context_tokens"])))
        self.cooldown = cfg.get("cooldown", AI_DEFAULTS["cooldown"])
        self.api_retry = cfg.get("api_retry", AI_DEFAULTS["api_retry"])
        self.api_timeout = cfg.get("api_timeout", AI_DEFAULTS["api_timeout"])
        self.api_wall_budget = cfg.get("api_wall_budget", AI_DEFAULTS["api_wall_budget"])
        # 回复分段节奏（_send_parts 段间停留；两开关互斥、可都关——UI 保证
        # 互斥，后端抖动优先于等待）：参数热改即时生效
        self.segment_wait_enabled = bool(cfg.get("segment_wait_enabled", True))
        self.segment_wait_seconds = max(
            0.0, float(cfg.get("segment_wait_seconds",
                               AI_DEFAULTS["segment_wait_seconds"])))
        self.segment_jitter_enabled = bool(
            cfg.get("segment_jitter_enabled", False))
        self.segment_jitter_min = max(
            0.0, float(cfg.get("segment_jitter_min",
                               AI_DEFAULTS["segment_jitter_min"])))
        self.segment_jitter_max = max(
            0.0, float(cfg.get("segment_jitter_max",
                               AI_DEFAULTS["segment_jitter_max"])))
        # 用量统计：每次 LLM 调用终态追加一行 JSONL（埋点在 _post_chat_completions）
        self.usage_store = UsageStore()
        # 微信窗口定位：None=默认右半屏；[x,y,w,h]=自定义；"off"=保持手动
        self.wechat_window_rect = cfg.get("wechat_window_rect", None)
        self.paused = cfg.get("start_paused", True)
        self.memory_file = cfg.get("memory_file", "memory.json")
        # 长记忆（v2）配置：recent 只保留 memory_keep_recent 条，溢出消息
        # append 进 memory_deep/<聊天>.jsonl 永不删除（深层记忆，recall_memory
        # 工具按需检索）；压缩 = 每攒满 memory_compress_batch 条深层消息调
        # 压缩模型提炼「重要记忆（常驻上下文）+ 关键词索引（命中时注入）」。
        # 压缩调用由后台线程（AgentBot 的 MemoryCompressor）执行，所有
        # memory_db/深层文件读写经 _memory_lock 串行化。
        self.memory_deep_enabled = bool(cfg.get("memory_deep_enabled", True))
        self.memory_compress_enabled = bool(cfg.get("memory_compress_enabled", False))
        self.memory_keep_recent = max(5, int(cfg.get("memory_keep_recent", 30)))
        # 阶梯滚动步长（缓存友好）：窗口在 [keep_recent, keep_recent+step]
        # 间波动，攒到上限一次性弹出溢出段归档——两次滚动之间注入序列纯追加，
        # API 前缀缓存命中；0 = 逐条立即滚动（旧行为）
        self.memory_rolling_step = max(
            0, int(cfg.get("memory_rolling_step",
                           AI_DEFAULTS["memory_rolling_step"])))
        self.memory_compress_batch = max(5, int(cfg.get("memory_compress_batch", 30)))
        self.memory_important_max = max(3, int(cfg.get("memory_important_max", 20)))
        self.memory_compress_model = strip_model_prefix(
            cfg.get("memory_compress_model", "") or "")
        # 记忆存储（MemoryStore）在首次访问 _mem 时惰性构建：memory_db/
        # 深层存档/节流落盘/读写锁全部内聚，bot 属性经 property 透传保持
        # 既有调用形态（见下方记忆委托区）
        # 状态监视（set_reminder kind=condition）总开关：会产生额外 API 调用
        # （api 判定模式每轮一次小调用 + 触发回递），是否开启由用户在设置页
        # 决定（默认关）。关闭时工具分支直接友好告知，不降级普通聊天
        # （降级会让模型凭空答应没做到的提醒）。
        self.state_watch_enabled = bool(cfg.get("state_watch_enabled", False))
        # 模型侧触发器管理（manage_reminder 工具）三态：off=模型只能创建
        # （现状）/ lazy=仅注入工具（被问才 list 拿 id）/ eager=工具+活跃
        # 触发器清单注入尾区（模型直接拿 id 一步操作，还能主动说出已有约定）。
        # 默认 off——新能力不主动开，老用户升级零变化。
        self.model_trigger_manage = str(
            cfg.get("model_trigger_manage", "off") or "off").strip().lower()
        # 表情包发送（send_sticker 工具）三态：off / catalog（清单每轮注入）/
        # query（先 search_stickers 再挑）。库位置不进配置——与应用基目录的
        # 「表情包」目录绑定（与壁纸库同一发现模式，见 sticker_store）。
        self.sticker_mode = str(
            cfg.get("sticker_mode", "off") or "off").strip().lower()
        # 语音发送（音源接口化）：voice_mode off/auto/always。端点与音色档案
        # 由用户配置（模型/参考音频在用户自部署的服务端），配置不全时语音
        # 链路整体不激活（工具不注入、回复走文本）。
        self.voice_mode = str(cfg.get("voice_mode", "off") or "off").strip().lower()
        self.tts_endpoint = str(cfg.get("tts_endpoint", "") or "").strip()
        self.tts_timeout = max(10, int(cfg.get("tts_timeout_seconds", 120)))
        self.voice_max_seconds = max(5, int(cfg.get("voice_max_seconds", 55)))
        self.voice_profiles = list(cfg.get("voice_profiles") or [])
        self.active_voice_profile_id = str(
            cfg.get("active_voice_profile_id", "") or "")
        self.web_search_enabled = bool(cfg.get("web_search_enabled", True))
        # per-chat 角色卡绑定：绑定表（聊天名 -> 卡 id）+ 解析后的运行时参数表
        # （config_store 加载/重投影时从 cards/ 生成）。设置页/记忆页改动经
        # AppContext.reproject_and_push 热推送，无需重启。
        self.chat_card_bindings = dict(cfg.get("chat_card_bindings") or {})
        self.chat_card_params = dict(cfg.get("chat_card_params") or {})
        # per-chat 功能覆盖（三态例外）：{memory_key(聊天): {功能名: bool}}，
        # 缺省 = 跟随全局。设置页保存后整体替换热生效；查询走
        # feature_enabled（web_search/task/state_watch）与 voice_state（voice）
        self.chat_feature_overrides = dict(
            cfg.get("chat_feature_overrides") or {})
        # 微信文件接收目录（对方发来的文件按显示名在此定位）
        self.file_storage_path = cfg.get("file_storage_path", "")
        # 图片消息点击偏移校准（竖图点击偏位时手动校正，格式 [dx, dy]，存 config.json）
        self.image_click_offset = cfg.get("image_click_offset", [0, 0])
        # 占位消息计数（"收到任务啦，正在处理中"等）：按 chat_name 隔离，
        # 非全局计数——每个聊天独立记录，绝不跨会话复用
        self._pending_placeholders = {}

        self._model_lock = threading.RLock()

        self._load_memory()
        self.last_reply_time = 0
        self.wx = None
        self._connect_wx()

    def set_chat_temperature(self, value):
        with self._model_lock:
            self.chat_temperature = float(value)

    def set_chat_top_p(self, value):
        with self._model_lock:
            self.chat_top_p = float(value)

    def _chat_overrides(self, chat_id):
        """per-chat 角色卡绑定参数（未绑定/未投影返回空 dict）。

        参数表由 config_store 从 chat_card_bindings + cards/ 投影生成，键与
        memory 键同一归一化口径——OCR 引号/空格差异不会分裂同一会话的绑定。"""
        if not chat_id:
            return {}
        params = getattr(self, "chat_card_params", None) or {}
        # 先按名字原文查（新口径），再退回归一化键（兼容升级前保存的绑定）
        return params.get(chat_id) or params.get(_memory_key(chat_id)) or {}

    # ---------- per-chat 功能开关（三态覆盖，查询口径唯一入口） ----------

    def _feature_override(self, chat_id, feature):
        """该聊天对该功能的覆盖值：None=未设置（跟随全局）/ True / False。"""
        ov = (getattr(self, "chat_feature_overrides", None) or {}).get(
            _memory_key(chat_id or "")) or {}
        v = ov.get(feature)
        return None if v is None else bool(v)

    def feature_enabled(self, chat_id, feature):
        """该聊天的功能有效开关（feature ∈ web_search/task/state_watch）。

        per-chat 三态覆盖优先，缺省跟随全局；强制开优先于全局关（覆盖
        语义是「这个聊天单独放开」，投递/声明链路自足，无能力缺口）。"""
        override = self._feature_override(chat_id, feature)
        if override is not None:
            return override
        base = {"web_search": bool(getattr(self, "web_search_enabled", True)),
                "task": bool(getattr(self, "task_enabled", True)),
                "state_watch": bool(getattr(self, "state_watch_enabled", False)),
                }.get(feature, False)
        return base

    def voice_state(self, chat_id):
        """该聊天的语音有效模式："off" / "auto" / "always"。

        全局 voice_mode 为基线；per-chat 覆盖：强制关 → off（任何基线）；
        强制开 → 基线为 off 且 TTS 配置齐全（_voice_profile_ready 非空，
        不含模式检查）时升为 auto——给特定聊天单独开语音；配置不齐开不
        出能力，维持 off。"""
        override = self._feature_override(chat_id, "voice")
        base = str(getattr(self, "voice_mode", "off") or "off").lower()
        if override is False:
            return "off"
        if override is True:
            if base in ("auto", "always"):
                return base
            if self._voice_profile_ready() is not None:
                return "auto"
            return "off"
        return base if base in ("auto", "always") else "off"

    def _state_watch_polling_active(self):
        """状态监视线程是否应轮询：全局开关开，或存在条件监视条目
        （全局关 + 有条目 = 用户按聊天单独开的，条目本身就是事实源）。"""
        if getattr(self, "state_watch_enabled", False):
            return True
        try:
            return bool(self.reminders.list_conditions())
        except Exception:
            return False

    # ---------- 触发器模型侧管理（manage_reminder 工具） ----------
    # 执行发生在 call_vision_api 的工具循环内（与 web_search 同一机制：结果
    # 作为 tool 消息回填，模型拿到回执后继续作答），绝不在这里直接发送——
    # 发送归主循环节点。只读查询/取消/改字段，固定 fail-closed：
    # 只动当前聊天的触发器、终态条目不可操作、URL 不可改（改 URL 等于重新
    # 看页面，让模型取消后重新创建）。

    @staticmethod
    def _trigger_belongs(item, chat_id):
        """触发器是否属于当前聊天：memory 口径归一化后严格相等（ ownership
        判定宁严勿宽——不用双向子串兜底，防止把相近名字聊天的触发器放给
        别的聊天操作）。"""
        a = _memory_key(str(item.get("chat") or ""))
        b = _memory_key(str(chat_id or ""))
        return bool(a) and a == b

    @staticmethod
    def _trigger_active(item):
        """触发器是否仍可操作：启用中，且条件监视未到终态（met/expired/dead）。"""
        if not item.get("enabled"):
            return False
        if item.get("kind") == "condition":
            return not item.get("done")
        return True

    @staticmethod
    def _trigger_brief_line(item):
        """触发器一行摘要（清单注入与 list 回执共用同一格式，模型两次看到的
        口径一致）。"""
        if item.get("kind") == "condition":
            exp = item.get("expire_at")
            exp_s = time.strftime("%m-%d %H:%M", time.localtime(exp)) \
                if exp else "无截止"
            return (f"[{item.get('id')}] 状态监视"
                    f"「{str(item.get('condition') or '')[:30]}」"
                    f"（每 {item.get('interval_seconds')}s 查一次，截止 {exp_s}）")
        fire = item.get("fire_at") or 0
        return (f"[{item.get('id')}] 定时 "
                f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(fire))}"
                f"（{item.get('repeat')}）")

    def _active_triggers_brief(self, chat_id):
        """eager 注入用：当前聊天活跃触发器清单（system 消息正文）。无条目
        返回空串（不注入空消息）。条目数封顶防 token 膨胀——超出的提示用
        list 查询。"""
        store = getattr(self, "reminders", None)
        if store is None or not chat_id:
            return ""
        try:
            items = [r for r in store.list()
                     if self._trigger_belongs(r, chat_id)
                     and self._trigger_active(r)]
        except Exception as e:
            logger.debug(f"[触发器管理] 清单读取失败: {e}")
            return ""
        if not items:
            return ""
        cap = 12
        lines = [self._trigger_brief_line(r) for r in items[:cap]]
        if len(items) > cap:
            lines.append(f"…另有 {len(items) - cap} 条，用 manage_reminder 的 "
                         "list 查看全部")
        return ("你之前为本聊天创建、仍在进行的触发器（方括号内是 id，"
                "管理时直接引用）：\n" + "\n".join(lines))

    def _exec_manage_reminder(self, chat_id, raw_args):
        """manage_reminder 工具执行入口（call_vision_api 工具循环调用）。
        返回 tool 消息文本（回执），绝不抛异常——失败转文字让模型自行措辞。"""
        store = getattr(self, "reminders", None)
        if store is None or not chat_id:
            return "触发器管理不可用"
        if str(getattr(self, "model_trigger_manage", "off") or "off") == "off":
            return "触发器管理未开启"
        try:
            args = json.loads(raw_args or "{}")
        except (ValueError, TypeError):
            return "manage_reminder 参数解析失败"
        if not isinstance(args, dict):
            return "manage_reminder 参数解析失败"
        action = str(args.get("action") or "").strip().lower()
        try:
            mine = [r for r in store.list() if self._trigger_belongs(r, chat_id)]
        except Exception as e:
            return f"触发器读取失败：{e}"
        if action == "list":
            active = [r for r in mine if self._trigger_active(r)]
            if not active:
                return "当前聊天没有进行中的触发器"
            return "当前聊天的活跃触发器：\n" + "\n".join(
                self._trigger_brief_line(r) for r in active)
        rid = str(args.get("id") or "").strip()
        if not rid:
            return "缺少触发器 id（先 action=list 查询）"
        target = next((r for r in mine if str(r.get("id")) == rid), None)
        if target is None:
            return (f"本聊天没有 id 为 {rid} 的触发器（可能属于别的聊天或"
                    "不存在，用 action=list 核对）")
        if not self._trigger_active(target):
            return f"触发器 {rid} 已结束（已触发/已过期/已失效），不能再操作"
        if action == "cancel":
            desc = self._trigger_brief_line(target)
            if not store.remove(rid):
                return f"取消失败（触发器 {rid} 可能刚被删除）"
            logger.info(f"[触发器管理] {chat_id!r} 取消: {desc}")
            return f"已取消触发器：{desc}"
        if action == "modify":
            return self._modify_trigger(store, target, args, rid)
        return f"未知 action: {action!r}（可用 list/cancel/modify）"

    def _modify_trigger(self, store, target, args, rid):
        """modify 分支：按 kind 校验字段白名单（time 改 time/repeat；condition
        改关键词/间隔/截止/切片——URL 与判定方式不可改），通过后写回。"""
        fields = {}
        if target.get("kind") == "condition":
            if "met_keywords" in args:
                kws = args.get("met_keywords")
                if isinstance(kws, str):
                    kws = re.split(r"[,，、\s]+", kws)
                kws = [str(k).strip() for k in (kws or []) if str(k).strip()]
                if str(target.get("judge") or "local") == "local" and not kws:
                    return "local 判定的状态监视不能清空关键词（要换条件请取消后重建）"
                fields["met_keywords"] = kws
            if "interval_seconds" in args:
                try:
                    fields["interval_seconds"] = max(
                        10, int(float(args["interval_seconds"])))
                except (TypeError, ValueError):
                    return "interval_seconds 非法（秒数）"
            if "expire_at" in args:
                raw = str(args.get("expire_at") or "").strip()
                if raw:
                    try:
                        exp = time.mktime(time.strptime(raw, "%Y-%m-%d %H:%M"))
                    except (ValueError, TypeError):
                        return "expire_at 格式应为 YYYY-MM-DD HH:MM"
                    if exp <= time.time():
                        return "expire_at 已经过去"
                    fields["expire_at"] = exp
                else:
                    fields["expire_at"] = None
            for key in ("scope_start", "scope_end"):
                if key in args:
                    fields[key] = str(args.get(key) or "").strip() or None
        else:
            if "time" in args:
                raw = str(args.get("time") or "").strip()
                try:
                    fire = time.mktime(time.strptime(raw, "%Y-%m-%d %H:%M"))
                except (ValueError, TypeError):
                    return "time 格式应为 YYYY-MM-DD HH:MM"
                if fire <= time.time():
                    return "新触发时间已过去"
                fields["fire_at"] = fire
            if "repeat" in args:
                rep = str(args.get("repeat") or "").strip()
                if rep not in ("once", "daily", "weekly"):
                    return "repeat 只能是 once/daily/weekly"
                fields["repeat"] = rep
        if not fields:
            return "没有给出要修改的字段"
        if not store.modify(rid, fields):
            return f"修改失败（触发器 {rid} 可能刚被删除）"
        if "interval_seconds" in fields:
            # 间隔变了就按新节奏立刻可查，不等旧 next_check_at
            store.schedule_next(rid, time.time(), reset_fail=True)
        merged = dict(target)
        merged.update(fields)
        logger.info(f"[触发器管理] 已修改 {rid}: {', '.join(sorted(fields))}")
        return (f"已修改触发器（{', '.join(sorted(fields))}）。"
                f"现在的样子：{self._trigger_brief_line(merged)}")

    # ---------- 表情包（send_sticker / search_stickers 工具） ----------
    # 三态（sticker_mode）：off 不激活；catalog = 清单每轮注入尾区（模型
    # 零额外往返直接挑）；query = 先 search_stickers 拿候选（多一轮 API，
    # token 有界）。发送执行在 call_vision_api 工具循环内（结果回填，模型
    # 可只发表情不说话、也可表情+文字继续作答）；窗口粘贴实现在
    # AgentBot._send_sticker_file（处理消息期间窗口已停在目标会话，与
    # _send_text 同一假设）。

    def _sticker_store(self):
        """StickerStore 惰性构建（stickers_dir 热改时自动重建）。目录不存在
        返回 None——库整体不激活，工具不声明。"""
        from xiaoli_app.sticker_store import StickerStore, default_stickers_dir
        d = default_stickers_dir()
        if not os.path.isdir(d):
            return None
        store = self.__dict__.get("_sticker_store_inst")
        if store is None or store.dir != d:
            store = StickerStore(d)
            self.__dict__["_sticker_store_inst"] = store
        return store

    def _sticker_catalog_text(self):
        """catalog 态注入正文：清单 + 使用说明。空库返回空串（不注入）。
        正文构造统一走 sticker_store.catalog_text——设置页「预览注入清单」
        复用同一函数，保证预览与实际请求逐字一致。"""
        from xiaoli_app.sticker_store import catalog_text
        store = self._sticker_store()
        if store is None:
            return ""
        try:
            lines = store.catalog_lines()
        except Exception as e:
            logger.debug(f"[表情包] 清单读取失败: {e}")
            return ""
        return catalog_text(lines)

    def _exec_search_stickers(self, query):
        """search_stickers 工具执行：本地关键词搜索（零 API），返回候选行。"""
        if str(getattr(self, "sticker_mode", "off") or "off") == "off":
            return "表情包功能未开启"
        store = self._sticker_store()
        if store is None:
            return "表情包库不可用"
        try:
            hits = store.search(query, limit=5)
        except Exception as e:
            return f"表情包搜索失败：{e}"
        if not hits:
            return "没有匹配的表情包（换个关键词，或不用表情包直接回复）"
        return ("找到这些（用 send_sticker 发送，file 填文件名）：\n"
                + "\n".join(f"{it['file']}｜{it['desc']}"
                            + (f"｜{' '.join(it['tags'])}" if it["tags"] else "")
                            for it in hits))

    def _exec_send_sticker(self, chat_id, fname):
        """send_sticker 工具执行：即时发送（窗口粘贴，实现在
        AgentBot._send_sticker_file）。返回 (ok, tool 回执文本)。fail-closed：
        未开启/无库/无此图/粘贴失败 → False——贴图是点缀，不回退文本。"""
        if str(getattr(self, "sticker_mode", "off") or "off") == "off":
            return False, "表情包功能未开启"
        store = self._sticker_store()
        if store is None:
            return False, "表情包库不可用"
        path = store.resolve(fname)
        if path is None:
            return False, (f"表情包库里没有 {str(fname)[:40]!r}"
                           "（query 模式先 search_stickers；catalog 模式按"
                           "清单里的文件名来）")
        sender = getattr(self, "_send_sticker_file", None)
        if sender is None:
            return False, "当前运行模式不支持发送表情包"
        try:
            sent = bool(sender(path))
        except Exception as e:
            logger.error(f"[表情包] 发送异常: {e}")
            sent = False
        if not sent:
            logger.warning(f"[表情包] 发送未确认，放弃: {fname}")
            return False, "这张没发出去（微信没接住粘贴），你直接文字回复吧"
        logger.info(f"[表情包] 已发送给 {chat_id!r}: {fname}")
        return True, f"表情包已发出：{fname}"

    def _describe_sticker(self, image_path):
        """单张表情包视觉打标（设置页全库优化 / 日后懒补共用）。失败抛异常
        由调用方决定。"""
        from xiaoli_app.sticker_store import describe_sticker
        with self._model_lock:
            model = self.chat_model or VISION_MODEL_DEFAULT
        return describe_sticker(self._post_chat_completions,
                                self.vision_api_url, self.vision_api_key,
                                model, image_path)

    # ---------- 语音发送（音源接口化，实现在 xiaoli_app.tts + 后端 send_voice）----------

    def _voice_profile_ready(self):
        """TTS 配置是否齐全（**不含模式检查**）：端点已配 + 激活档案存在 +
        「通用」参考齐全（ref_audio_path 与 prompt_text 均非空——GPT-SoVITS
        合成的最低要求）。返回档案 dict 或 None。每次现读 bot 属性——设置页
        热改即时生效。per-chat 强制开语音（voice_state）用它判断能否在全局
        mode=off 时单聊启用。"""
        if not getattr(self, "tts_endpoint", ""):
            return None
        pid = getattr(self, "active_voice_profile_id", "")
        for p in (getattr(self, "voice_profiles", None) or []):
            if not isinstance(p, dict) or p.get("id") != pid:
                continue
            base = (p.get("refs") or {}).get("通用") or {}
            if str(base.get("ref_audio_path") or "").strip() \
                    and str(base.get("prompt_text") or "").strip():
                return p
            return None
        return None

    def _voice_profile(self):
        """激活音色档案；模式未开启或配置不全返回 None（语音链路整体不激活）。"""
        if getattr(self, "voice_mode", "off") not in ("auto", "always"):
            return None
        return self._voice_profile_ready()

    def _send_voice_tool(self, profile, state=None):
        """send_voice 工具 schema（auto / always 模式注入，描述按模式区分）。

        state：该聊天语音有效模式（voice_state），缺省回退全局 voice_mode。

        emotion 枚举从档案 refs 键动态生成——用户配了哪些情绪参考，模型
        就能选哪些（不硬编码任何模型包的情绪集）；档案只有「通用」时视为
        没配情绪，工具不带 emotion 参数。
        - auto：工具是「何时发语音」的开关——仅对方要语音等明确语境才调
        - always：语音模式纪律——每一条回复都**必须**经本工具发出，emotion
          是模型挑情绪的唯一通道
        """
        refs = profile.get("refs") or {}
        emotions = [str(k) for k in refs if str(k) != "通用"]
        props = {
            "text": {"type": "string",
                     "description": "要说出口的内容（语音条里念的话）"},
        }
        if emotions:
            props["emotion"] = {
                "type": "string",
                "enum": ["通用"] + emotions,
                "description": "用哪种情绪说话（对应不同的参考音频），"
                               "按消息语境挑一个，不确定就用「通用」",
            }
        if (state or getattr(self, "voice_mode", "off")) == "always":
            description = (
                "语音发送（当前为语音模式：你的每一条回复都必须通过调用"
                "本工具发出）。text 填要说出口的内容（口语化、适合念出来，"
                "不要用颜文字/emoji/符号），emotion 按消息语境挑一个，"
                "一次只发一条")
        else:
            description = (
                "把要说的话用语音消息发给对方。仅当对方要求你发语音、念"
                "一段东西、唱歌等明确语境时调用，一次只发一条；普通聊天"
                "不要调用")
        return {
            "type": "function",
            "function": {
                "name": "send_voice",
                "description": description,
                "parameters": {"type": "object", "properties": props,
                               "required": ["text"]},
            },
        }

    def _recent_cap(self, chat_id=None):
        """近期记忆保留条数：memory_keep_recent 与 max_history 取小
        （角色卡 max_history 若被用户调小仍然生效）。chat_id 绑定了角色卡
        且卡带 max_history 时按卡覆盖。"""
        keep = max(5, int(getattr(self, "memory_keep_recent", 30)))
        max_hist = int(getattr(self, "max_history", 1000))
        if chat_id:
            ov = self._chat_overrides(chat_id)
            try:
                max_hist = int(ov.get("max_history") or max_hist)
            except (TypeError, ValueError):
                pass
        return max(5, min(keep, max_hist))

    def _rolling_step(self, chat_id=None):
        """阶梯滚动步长（memory_rolling_step，热改即时生效）。"""
        return max(0, int(getattr(self, "memory_rolling_step", 30)))

    def _save_memory(self):
        try:
            with open(self.memory_file, "w", encoding="utf-8") as f:
                json.dump(self.memory_db, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存记忆失败: {e}")

    def _connect_wx(self):
        """连接微信（后端抽象层）。失败按 _connect_retry_interval 间隔重试：
        - stop_event 已设置 → 立即抛异常（引擎停止/取消初始化）
        - max_connect_retries 达到 → 抛异常（GUI 初始化超时 → error 状态）
        睡眠分片（0.5s）保证 stop 响应延迟 ≤0.5s。"""
        attempts = 0
        while True:
            if self._stop_event is not None and self._stop_event.is_set():
                raise RuntimeError("微信连接已取消")
            try:
                self.wx = create_backend("auto")
                self._init_position_wechat()
                logger.info(f"✅ 微信连接成功（后端: {self.wx.name}）")
                return
            except Exception as e:
                attempts += 1
                if self._max_connect_retries is not None \
                        and attempts >= self._max_connect_retries:
                    raise RuntimeError(
                        f"微信连接失败（已重试 {attempts} 次，超过上限 "
                        f"{self._max_connect_retries}）：{e}") from e
                logger.error(f"微信连接失败：{e}，{self._connect_retry_interval}秒后重试...")
                remain = self._connect_retry_interval
                while remain > 1e-9:
                    if self._stop_event is not None and self._stop_event.is_set():
                        raise RuntimeError("微信连接已取消")
                    time.sleep(min(0.5, remain))
                    remain -= 0.5

    def _init_position_wechat(self):
        """初始化微信窗口定位（连接成功时执行一次，用户定案）。

        视觉坐标依赖窗口位置/尺寸稳定——初始化即摆到标准位并告知用户
        勿动，替代旧「不移动窗口」约定。配置 wechat_window_rect：
        - None（默认）→ 主屏右半边（README 系统要求）
        - [x, y, w, h] → 自定义矩形（物理像素）
        - "off" → 完全保持手动（旧行为）
        运行期不再核对矩形（用户否决每轮像素核对的成本），仅保留最小化
        哨兵自动恢复（IsIconic 一次系统调用，微秒级）。
        """
        rect = getattr(self, "wechat_window_rect", None)
        if rect == "off":
            logger.info("[定位] wechat_window_rect=off，保持手动窗口位置")
            return
        hwnd = find_window_by_title("微信")
        if not hwnd:
            logger.warning("[定位] 未找到微信窗口，跳过定位")
            return
        ensure_window_visible(hwnd)  # 最小化先拉起，定位才有意义
        try:
            if ctypes.windll.user32.IsZoomed(hwnd):
                # 最大化先还原再定位：SetWindowPos 对最大化窗口行为不可靠
                # （可能只改尺寸不改状态，视觉错乱）
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                time.sleep(0.3)
        except Exception:
            pass
        if isinstance(rect, (list, tuple)) and len(rect) == 4:
            try:
                x, y, w, h = (int(v) for v in rect)
                source = "配置"
            except (TypeError, ValueError):
                x, y, w, h = default_right_half_rect()
                source = "配置非法，回退默认右半屏"
        else:
            x, y, w, h = default_right_half_rect()
            source = "默认右半屏"
        if position_window_visible(hwnd, x, y, w, h):
            # 可见内容语义：自动外扩 DWM 不可见边框，可见部分精确贴合目标
            # （与手动拖窗口到打满的系统行为一致；直接摆窗口矩形会两侧
            # 各缩进 ~10px——用户实测「右侧有缝隙」）
            logger.info(f"[定位] 微信窗口已定位（{source}）：可见区 ({x},{y}) {w}x{h}"
                        "——请勿最小化或调整窗口大小，否则影响消息识别")
        else:
            logger.warning("[定位] 微信窗口定位失败，保持当前位置")

    # ---------- 记忆（实现在 xiaoli_app.memory_store.MemoryStore；这里全部
    # 委托，保持既有调用形态——MemoryCompressor / CLI / 记忆管理页 / tests
    # 直呼 bot 方法或读写下列属性。深层记忆开关 deep_enabled 由调用点传入
    # bot 的现值——设置页热改即时生效，store 不存快照） ----------

    @property
    def _mem(self):
        """MemoryStore 惰性构建：__new__ 直构的测试桩在首次访问时按当前
        属性建库（memory_file 先设后用亦生效，memory_file 属性 setter 会
        同步已建 store）。"""
        store = self.__dict__.get("_mem_store")
        if store is None:
            store = MemoryStore(memory_file=self.memory_file,
                                cap_fn=self._recent_cap,
                                step_fn=self._rolling_step)
            self.__dict__["_mem_store"] = store
        return store

    @property
    def memory_file(self):
        store = self.__dict__.get("_mem_store")
        if store is not None:
            return store.memory_file
        return self.__dict__.get("_memory_file", "memory.json")

    @memory_file.setter
    def memory_file(self, value):
        self.__dict__["_memory_file"] = value
        store = self.__dict__.get("_mem_store")
        if store is not None:
            store.set_memory_file(value)

    @property
    def memory_db(self):
        return self._mem.memory_db

    @memory_db.setter
    def memory_db(self, value):
        self._mem.memory_db = value

    @property
    def _memory_lock(self):
        return self._mem.lock

    @_memory_lock.setter
    def _memory_lock(self, value):
        self._mem.lock = value

    @property
    def _deep_count(self):
        return self._mem.deep_count

    @_deep_count.setter
    def _deep_count(self, value):
        self._mem.deep_count = value

    @property
    def _deep_dir(self):
        return self._mem._deep_dir

    @_deep_dir.setter
    def _deep_dir(self, value):
        self._mem._deep_dir = value

    @property
    def _memory_dirty(self):
        return self._mem._dirty

    @_memory_dirty.setter
    def _memory_dirty(self, value):
        self._mem._dirty = bool(value)

    @property
    def _last_memory_save(self):
        return self._mem._last_save

    @_last_memory_save.setter
    def _last_memory_save(self, value):
        self._mem._last_save = value

    def _load_memory(self):
        self._mem.load(
            deep_enabled=bool(getattr(self, "memory_deep_enabled", True)))

    def _get_history(self, chat_id):
        return self._mem.recent(chat_id)

    def _add_history(self, chat_id, role, content):
        self._mem.add(chat_id, role, content,
                      deep_enabled=bool(getattr(self, "memory_deep_enabled", True)))

    def _deep_path(self, chat_id):
        return self._mem.deep_path(chat_id)

    def _append_deep(self, chat_id, msg):
        self._mem.append_deep(
            chat_id, msg,
            deep_enabled=bool(getattr(self, "memory_deep_enabled", True)))

    def _count_deep(self, chat_id):
        return self._mem.count_deep(chat_id)

    def _iter_deep(self, chat_id):
        return self._mem.iter_deep(chat_id)

    def _read_deep_range(self, chat_id, start, count):
        return self._mem.read_deep_range(chat_id, start, count)

    def _recall_memory(self, chat_id, query):
        if not getattr(self, "memory_deep_enabled", False):
            return "深层记忆未启用，无法检索历史"
        return self._mem.recall(chat_id, query)

    def _important_block(self, chat_id):
        if not getattr(self, "memory_compress_enabled", False):
            return None
        return self._mem.important_block(chat_id)

    def _match_related_memory(self, chat_id, user_text):
        if not getattr(self, "memory_compress_enabled", False):
            return None
        return self._mem.match_related(chat_id, user_text)

    def memory_commit_compression(self, chat_id, consumed, important_new, index_new):
        self._mem.commit_compression(
            chat_id, consumed, important_new, index_new,
            important_max=int(getattr(self, "memory_important_max", 20)))

    def memory_overview(self):
        return self._mem.overview()

    def memory_detail(self, chat_id, deep_offset=0, deep_limit=200,
                      deep_query=None):
        return self._mem.detail(chat_id, deep_offset, deep_limit, deep_query)

    def delete_important(self, chat_id, idx):
        return self._mem.delete_important(chat_id, idx)

    def delete_index_entry(self, chat_id, idx):
        return self._mem.delete_index_entry(chat_id, idx)

    def delete_deep_message(self, chat_id, line_no):
        return self._mem.delete_deep_message(chat_id, line_no)

    def delete_messages(self, chat_id, indices):
        return self._mem.delete_messages(chat_id, indices)

    def clear_history(self, chat_id=None):
        self._mem.clear_history(chat_id)

    def _flush_memory_if_due(self):
        self._mem.flush_if_due()

    def _flush_memory(self):
        self._mem.flush()

    def _post_chat_completions(self, url, headers, payload, timeout, label="api", meta=None):
        """OpenAI 兼容 chat/completions 统一调用入口（实现在 LlmClient.post；
        保留既有调用形态）。重试次数/墙钟预算每次从 bot 属性现读——热改
        即时生效。成功返回解析后的 dict；最终失败抛 ApiCallError。"""
        client = getattr(self, "_llm", None)
        if client is None:
            client = LlmClient(usage_store=getattr(self, "usage_store", None))
            self._llm = client
        return client.post(url, headers, payload, timeout, label=label,
                           meta=meta, retry=int(getattr(self, "api_retry", 2)),
                           wall_budget=float(getattr(
                               self, "api_wall_budget", API_WALL_BUDGET_DEFAULT)))

    def _vision_tools(self, chat_id):
        """call_vision_api 的工具声明集（单一事实源）：每个工具按自己的
        开关单独注入，互不连带——
        - dispatch_task ← 任务桥开关（全局 + per-chat 覆盖）
        - send_voice ← 该聊天语音有效三态（voice_state）+ TTS 配置齐全
        - set_reminder ← 恒注入（定时提醒不产生额外 API 成本，无独立开关；
          状态监视开关只在创建时拦 kind=condition，见 _create_condition_watch）
        - web_search / web_fetch ← 联网搜索开关
        - recall_memory ← 深层记忆开关
        _vision_route 选提示词变体时复用本方法——同一套条件，防两处漂移；
        返回空列表时调用方 payload 不带 tools/tool_choice 键。
        """
        tools = []
        # 任务投递工具：按聊天有效开关声明（全局关 + 单聊强制开 = 该聊天
        # 仍可投；关闭 = 不声明，模型看不到就不会调用）
        if self.feature_enabled(chat_id, "task"):
            tools.append({
                "type": "function",
                "function": {
                    "name": "dispatch_task",
                    "description": "判断用户消息是否为任务，是则投递天枢处理",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {"type": "string", "description": "任务描述"},
                        },
                        "required": ["task"],
                    },
                },
            })
        # 语音发送工具（voice_mode=auto/always 且配置齐全时注入；模式取
        # 该聊天有效状态）。auto 下工具决定「何时发语音」；always 下工具
        # 是**强制回复通道**——每条回复都必须经它发出（纪律见
        # ALWAYS_VOICE_RULES system 消息），模型借 emotion 挑情绪。
        # off 或配置不全时不注入。
        voice_profile = self._voice_profile_ready()
        v_state = self.voice_state(chat_id)
        if voice_profile is not None and v_state in ("auto", "always"):
            tools.append(self._send_voice_tool(voice_profile, v_state))
        if getattr(self, "reminders", None) is not None:
            # 统一触发器工具（kind=time 定时 / kind=condition 状态监视）：
            # 模型依据 system 的「当前时间」消息把相对表达换算成绝对时间。
            # 无 content 参数——到点/达成只回递事件，回复由模型结合创建时
            # 的历史对话自行生成（用户定案：不做预写文案的闹钟）。
            # 状态监视纪律（真机实验定案，见 ConditionWatcher）：创建前必须
            # 用 web_search 挑页面并 web_fetch 看过实际内容，轮询期只访问
            # 固定 URL、不再调用搜索引擎；页面混合当前与未来时段（如天气预报
            # 页「今天暴雨…周六晴」）时必须给 scope 切片，否则本地判定会被
            # 未来预报误触发。
            tools.append({
                "type": "function",
                "function": {
                    "name": "set_reminder",
                    "description": "创建触发器，两种模式二选一（到点/达成时系统会把"
                                   "触发事件回递给你，由你结合创建该触发器时的对话"
                                   "历史按人设自然回复，因此不需要预先写提醒文案）：\n"
                                   "① kind=time 定时：到指定时间触发"
                                   "（如「明天下午3点提醒我查成绩」「每天早上8点叫我起床」）。\n"
                                   "② kind=condition 状态监视：轮询监控一个固定网页，"
                                   "条件达成或到截止时间时触发"
                                   "（如「如果一会儿下雨了提醒我去拿快递」"
                                   "「盯着一个商品页面，降价了告诉我」）。\n"
                                   "状态监视创建规则：先用 web_search 搜索并挑一个内容"
                                   "稳定的页面，用 web_fetch 看过实际内容后再创建"
                                   "（关键词要按页面实际措辞给全同义形式）；轮询期只"
                                   "访问该固定 URL、不再调用搜索引擎；若页面同时包含"
                                   "当前与未来时段的信息（如天气预报页），必须给"
                                   "scope_start/scope_end 圈出与条件相关的当前时段切片"
                                   "（如「今天」到「明天」），绝不能整页匹配；条件无法"
                                   "用关键词判断（需要理解语义或比较数值）时用 judge=api。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": ["time", "condition"],
                                     "description": "触发器类型：time=定时，"
                                                    "condition=状态监视"},
                            "time": {"type": "string",
                                     "description": "kind=time 必填。触发时间，格式"
                                                    " YYYY-MM-DD HH:MM（24 小时制；"
                                                    "依据当前时间换算相对表达）"},
                            "repeat": {"type": "string", "enum": ["once", "daily", "weekly"],
                                       "description": "kind=time：重复规则，默认 once"},
                            "condition": {"type": "string",
                                          "description": "kind=condition 必填。自然语言"
                                                         "条件原文（判定与到期回递的依据）"},
                            "url": {"type": "string",
                                    "description": "kind=condition 必填。要固定轮询监控"
                                                   "的网页 URL（创建时已用 web_fetch 验看过）"},
                            "judge": {"type": "string", "enum": ["local", "api"],
                                      "description": "kind=condition：达成判定方式，默认"
                                                     " local。local=本地关键词匹配（零 API"
                                                     " 成本，适合文字标记型条件）；api=每次"
                                                     "抓取后调模型判定（适合需理解语义或比较"
                                                     "数值的条件）"},
                            "match_type": {"type": "string", "enum": ["present", "absent"],
                                           "description": "kind=condition 且 judge=local："
                                                          "present=切片内出现任一关键词即达成"
                                                          "（如「有货」）；absent=切片内关键词"
                                                          "全部消失即达成（如「等雨停」盯「雨」"
                                                          "消失）。默认 present"},
                            "met_keywords": {"type": "array",
                                             "items": {"type": "string"},
                                             "description": "kind=condition 且 judge=local"
                                                            " 必填：判定关键词数组（如"
                                                            " [\"雨\",\"降雨\",\"雷阵雨\"]），"
                                                            "按页面实际措辞给全同义形式"},
                            "scope_start": {"type": "string",
                                            "description": "kind=condition 可选：切片起始"
                                                           "标记（页面文本原文，如「今天」）"},
                            "scope_end": {"type": "string",
                                          "description": "kind=condition 可选：切片结束标记"
                                                         "（如「明天」），只匹配两标记之间，"
                                                         "隔离未来时段"},
                            "interval_seconds": {"type": "integer",
                                                 "description": "kind=condition：轮询间隔秒，"
                                                                "默认 60，最小 10"},
                            "expire_at": {"type": "string",
                                          "description": "kind=condition 可选：截止时间"
                                                         " YYYY-MM-DD HH:MM，不填默认 7 天后"},
                        },
                        "required": ["kind"],
                    },
                },
            })
            # 触发器模型侧管理（三态开关，默认 off）：list/cancel/modify
            # 在工具循环内执行（_exec_manage_reminder）。eager 态另有活跃
            # 清单注入（call_vision_api 尾区），描述随态区分——有清单就
            # 让模型直接用 id，没清单让它先 list。
            if str(getattr(self, "model_trigger_manage", "off") or "off") != "off":
                manage_eager = str(getattr(self, "model_trigger_manage")) == "eager"
                tools.append({
                    "type": "function",
                    "function": {
                        "name": "manage_reminder",
                        "description": (
                            "管理你之前为当前聊天创建的触发器（定时/状态监视）："
                            "查询、取消、修改。只能操作本聊天的触发器，动不了"
                            "别的聊天的；已结束（触发/过期/失效）的不能操作。"
                            + ("当前活跃触发器清单已在系统消息里给出，直接用"
                               "上面的 id 操作，不确定就先 list 核对。"
                               if manage_eager else
                               "先 action=list 拿到 id，再 cancel/modify。")
                            + "要改状态监视的监控网页（url）时不能直接改——"
                              "先 cancel 再重新创建（新网页要先看过内容）。"),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string",
                                           "enum": ["list", "cancel", "modify"],
                                           "description": "list=查询本聊天活跃"
                                                          "触发器；cancel=取消；"
                                                          "modify=修改"},
                                "id": {"type": "string",
                                       "description": "cancel/modify 必填："
                                                      "触发器 id"},
                                "time": {"type": "string",
                                         "description": "modify 定时触发器："
                                                        "新触发时间 "
                                                        "YYYY-MM-DD HH:MM"},
                                "repeat": {"type": "string",
                                           "enum": ["once", "daily", "weekly"],
                                           "description": "modify 定时触发器："
                                                          "重复规则"},
                                "met_keywords": {"type": "array",
                                                 "items": {"type": "string"},
                                                 "description": "modify 状态监视："
                                                                "新判定关键词数组"},
                                "interval_seconds": {"type": "integer",
                                                     "description": "modify 状态"
                                                                    "监视：轮询间"
                                                                    "隔秒（最小 10）"},
                                "expire_at": {"type": "string",
                                              "description": "modify 状态监视："
                                                             "新截止时间 "
                                                             "YYYY-MM-DD HH:MM"},
                                "scope_start": {"type": "string",
                                                "description": "modify 状态监视："
                                                               "切片起始标记"},
                                "scope_end": {"type": "string",
                                              "description": "modify 状态监视："
                                                             "切片结束标记"},
                            },
                            "required": ["action"],
                        },
                    },
                })
        # 表情包工具（三态，默认 off）：send_sticker 两个非 off 态都声明；
        # search_stickers 仅 query 态（catalog 态清单已在系统消息，不用查）。
        # 库目录不存在 = 整体不激活（模型看不到工具）。
        _sticker_mode = str(getattr(self, "sticker_mode", "off") or "off")
        if _sticker_mode in ("catalog", "query") \
                and self._sticker_store() is not None:
            tools.append({
                "type": "function",
                "function": {
                    "name": "send_sticker",
                    "description": (
                        "发送一个表情包（微信表情图片）。"
                        + ("可用表情包清单已在系统消息里给出，file 直接从"
                           "清单里选。" if _sticker_mode == "catalog" else
                           "先用 search_stickers 搜到合适的，再拿文件名"
                           "发送。")
                        + "可以只发表情包不说话（调用后不再输出文字），"
                          "也可以表情包+文字一起回（表情先发、文字后发）；"
                          "一次一张，别连发"),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string",
                                     "description": "要发送的表情包文件名"
                                                    "（清单/搜索结果里给的）"},
                        },
                        "required": ["file"],
                    },
                },
            })
            if _sticker_mode == "query":
                tools.append({
                    "type": "function",
                    "function": {
                        "name": "search_stickers",
                        "description": "按情绪/场景关键词搜索可发送的表情包"
                                       "（如「生气」「开心」「猫猫」），返回"
                                       "最相关的候选。挑好了用 send_sticker "
                                       "发送",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string",
                                          "description": "情绪或场景关键词"},
                            },
                            "required": ["query"],
                        },
                    },
                })
        # 联网搜索工具（零配置：百度/必应/搜狗并发合并，见 xiaoli_app/web_search）。
        # 有效开关按聊天取（feature_enabled：per-chat 覆盖优先）——关闭时
        # 整体不声明，模型看不到就不会调用。query 描述写明真机校准的措辞
        # 规则：多个主体用空格分开（百度/搜狗对「机构 人名」式查询精准命中，
        # cn.bing 反而会分词失败）；并给出结果无关时的换措辞重试策略——模型
        # 拿到垃圾结果只会硬答或放弃是此前搜索「不精确」的主因之一。
        if self.feature_enabled(chat_id, "web_search"):
            tools.append({
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "联网搜索（搜狗/必应/百度多引擎并发）。需要实时信息"
                                   "（天气/新闻/价格/赛事结果等）或拿不准的事实时调用。"
                                   "query 一次只查一个主题，用简短中文短语，多个主体用"
                                   "空格分开（如「云溪天气」「某高校教师名」），不要堆"
                                   "修饰词。若返回结果与查询主题明显无关（如搜人名得到"
                                   "无关实体），换措辞再搜一次：调整词序、增删限定词"
                                   "（如加「教授」「简介」）或改用更具体的表述",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string",
                                      "description": "简短中文短语，一次一个主题，"
                                                     "多个主体用空格分开"
                                                     "（如「云溪天气」「某高校教师名」）"},
                        },
                        "required": ["query"],
                    },
                },
            })
            # 网页正文抓取（与 web_search 成对）：搜索摘要只有站点介绍，实时数据
            # 在网页正文里——模型从搜索结果挑来源后抓正文自己读
            tools.append({
                "type": "function",
                "function": {
                    "name": "web_fetch",
                    "description": "抓取网页正文全文。搜索结果里挑最相关的来源"
                                   "（如天气网页面）后调用，读取其中具体的数据"
                                   "（气温/天气/比分等）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "要抓取的网页 URL"},
                        },
                        "required": ["url"],
                    },
                },
            })
        # 深层记忆检索工具：仅启用深层记忆且有聊天上下文时声明——模型在
        # 「用户提到过去的事但自己记不清」时主动调用，避免凭空编造
        if chat_id and getattr(self, "memory_deep_enabled", False):
            tools.append({
                "type": "function",
                "function": {
                    "name": "recall_memory",
                    "description": "在本聊天的全部历史记忆中检索（包括很久以前的"
                                   "对话存档）。当用户提到过去聊过的事、之前的约定"
                                   "或承诺、或问你记不记得某件事而你一时想不起来时"
                                   "调用，不要凭空编造",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string",
                                      "description": "检索关键词，如人名/事件/物品名"
                                                     "（可给多个，空格分开）"},
                        },
                        "required": ["query"],
                    },
                },
            })
        return tools

    def call_vision_api(self, content, chat_id=None, related_memory=None):
        """单调用视觉识别（OpenAI 兼容 / chat.completions）。

        content：块列表 list[dict]，格式
          [{"type": "text", "text": ...},
           {"type": "image_url", "image_url": {"url": "data:image/..."}}]
        图片块可选——无图时只含 text 块（调用方构造，本方法原样透传进 user
        消息；DeepSeek vision 限制：图片只能出现在 user 消息，system/assistant
        带图返回 400）。消息布局（缓存友好：稳定前缀在前、每轮变化区在尾）：
          [system 人设] → [system 回复纪律] → [历史(带[ts])] →
          [system 重要记忆] → [system 相关记忆] → [system 当前时间] → [user content]
        人设（self.system_prompt）前置为 system 纯文本消息（空人设则不插入人设
        system 消息）；重要记忆/相关记忆来自长记忆压缩产出（per-chat，未启用
        或无内容时缺省），放在历史之后的尾区——压缩提交会改写重要记忆，插在
        历史之前时每次提交都把整条「人设+历史」前缀缓存作废，挪到尾区后只
        作废尾巴一小截；「当前时间」system 紧贴当前消息——它每秒变化，绝不能
        插在历史之前打断缓存前缀。

        chat_id 可选（默认 None）：非空时注入重要记忆块与 _get_history(chat_id)
        近期历史（语义逐字对齐 call_chat_ai：有 time 字段带 [ts] 前缀，否则
        原文；不重排）。为空时仍注入当前时间 system（图片/文件描述路径自动
        受益；persona 为空时本条保证 messages 至少一条 system）。

        payload 的工具集由 _vision_tools 按开关逐项声明（dispatch_task /
        send_voice / set_reminder / web_search / web_fetch / recall_memory；
        tool_choice=auto；集合为空时 payload 不带 tools/tool_choice 键）。
        web_search/web_fetch/recall_memory 是查询型工具循环：模型调用后本
        方法执行（搜索/抓正文/记忆检索），把结果作为 tool 消息回填、继续
        补全，直到给出最终答复。整个调用最多 VISION_TOOL_ROUNDS 次补全，
        循环耗尽仍未收敛返回 None（调用方降级）。dispatch_task/set_reminder
        调用优先原样返回（既有单次语义不变，查询往返不延迟任务投递）。工具
        往返只存在于本次调用的 messages，不写入对话历史。

        返回结构化结果（供上层按 dict 处理）：
        - message.tool_calls 含 dispatch_task/set_reminder →
          {'kind': 'tool_call', 'name': ..., 'arguments': ...}（arguments 为原始 JSON 字符串）
        - 仅 message.content（含经工具循环后的最终答复）→
          {'kind': 'text', 'content': ...}
        - 非 200 / 无 choices / content 空白 / 循环耗尽 → None
        """
        # per-chat 角色卡绑定：该聊天绑定了卡时，人设/参数/端点以卡为准
        ov = self._chat_overrides(chat_id)
        headers = {"Authorization": f"Bearer {ov.get('ai_api_key') or self.vision_api_key}",
                   "Content-Type": "application/json"}
        # 人设由 system 纯文本消息承载（绝不放图片——DeepSeek 限制图片
        # 只能进 user 消息）；persona 为空时不插入空 system 消息。
        persona = (ov.get("system_prompt")
                   or getattr(self, "system_prompt", "") or "").strip()
        messages = [{"role": "system", "content": persona}] if persona else []
        # 回复风格纪律：运行时统一注入（与角色卡解耦——卡只管「她是谁」）。
        # 位置紧跟人设、在重要记忆之前：仍属稳定前缀区，不破坏缓存布局。
        if REPLY_STYLE_RULES:
            messages.append({"role": "system", "content": REPLY_STYLE_RULES})
        if chat_id:
            # 历史注入：语义逐字对齐 call_chat_ai（system 之后、user 之前；
            # 有 time 字段带 [ts] 前缀，否则原文；不重排——_get_history 返回
            # 列表本身已按时间有序）
            for h in self._get_history(chat_id):
                if "time" in h:
                    ts = h["time"]
                    msg_content = f"[{ts}] {h['content']}"
                else:
                    msg_content = h['content']
                messages.append({"role": h["role"], "content": msg_content})
        # 重要记忆（压缩产出）在历史之后的尾区：缓存布局见 docstring
        important = self._important_block(chat_id) if chat_id else None
        if important:
            messages.append({"role": "system", "content": important})
        if related_memory:
            messages.append({"role": "system", "content": related_memory})
        # 当前时间 system：无条件注入且紧贴当前消息（历史之后——它每秒变化，
        # 插在历史前会打断缓存前缀）；persona 为空时本条保证 messages 至少
        # 一条 system，消除空 messages 隐患。
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")
        messages.append({"role": "system", "content": f"当前时间：{current_time}"})
        # eager 态触发器清单注入（manage_reminder 配套）：紧跟当前时间的尾区
        # ——清单变化极少（创建/取消才变），放这里不打碎「人设+历史」前缀
        # 缓存；模型拿到 id 后取消/修改可一轮完成，还能主动说出已有约定。
        if chat_id and str(getattr(self, "model_trigger_manage", "off")
                           or "off") == "eager":
            listing = self._active_triggers_brief(chat_id)
            if listing:
                messages.append({"role": "system", "content": listing})
        # catalog 态表情包清单注入（同一尾区语义：清单只在库变化时变）
        if chat_id and str(getattr(self, "sticker_mode", "off")
                           or "off") == "catalog":
            cat = self._sticker_catalog_text()
            if cat:
                messages.append({"role": "system", "content": cat})
        # always 语音模式纪律：每条回复必须经 send_voice 发出（模型挑情绪的
        # 唯一通道）。放当前时间之后、user 之前——它不随轮次变化，且位于
        # 每秒变化的时间消息之后，不影响缓存前缀。模式取该聊天的语音有效
        # 状态（voice_state：per-chat 覆盖优先，缺省跟随全局）；配置校验用
        # _voice_profile_ready（不含模式——单聊强制开时全局可为 off）。
        voice_profile = self._voice_profile_ready()
        v_state = self.voice_state(chat_id)
        if voice_profile is not None and v_state == "always":
            messages.append({"role": "system", "content": ALWAYS_VOICE_RULES})
        messages.append({"role": "user", "content": content})
        with self._model_lock:
            # 单模型化：视觉 model 取 chat_model（__init__ 已 strip 前缀），
            # 空则兜底 vision-exp 纯名（防空 model / 带前缀兜底 → API 400）；
            # 温度随聊天温度（vision_temp 已删）；绑定卡带模型/参数时以卡为准
            model = strip_model_prefix(ov.get("chat_model") or "") \
                or self.chat_model or VISION_MODEL_DEFAULT
            temp = float(ov["temperature"]) if ov.get("temperature") is not None \
                else self.chat_temperature
            # top_p 随聊天参数（绑定卡带参数时以卡为准）——对齐 call_chat_ai
            top_p = float(ov["top_p"]) if ov.get("top_p") is not None \
                else getattr(self, "chat_top_p", 0.9)
        # 上下文预算裁剪：超长历史/文件全文会撑爆模型上下文上限
        # （实测请求 272 万 token → API 400 "maximum context length"）。
        # 逐字对齐 call_chat_ai：从最旧历史开始丢弃，保证单次请求不超模型上下文。
        messages = fit_messages_in_budget(
            messages, budget=getattr(self, "max_context_tokens", 100000))
        # 工具按「每个工具单独注入」语义逐项声明——单一事实源在
        # _vision_tools（任务/语音/闹钟/搜索/记忆检索各看各的开关；
        # _vision_route 选提示词变体时复用同一套条件，防两处漂移）
        tools = self._vision_tools(chat_id)
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": self.reply_max_tokens,
            "temperature": temp,
            # top_p 对齐 call_chat_ai：文本回复恒走本链路后，top_p 不能
            # 对文字回复静默失效
            "top_p": top_p,
        }
        # 工具为空时不带 tools/tool_choice 键（部分厂商对空数组 +
        # tool_choice 返回 400；无工具即纯补全）
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            # sticker_sent：本次调用里已成功发出过表情包——随后模型不再输出
            # 文字 = 纯表情包回复（不降级），输出了文字 = 表情包+文字
            sticker_sent = False
            for _round in range(VISION_TOOL_ROUNDS):
                data = self._post_chat_completions(
                    ov.get("ai_api_url") or self.vision_api_url, headers,
                    payload, 60, label="vision",
                    meta={"kind": "vision", "model": model, "messages": messages})
                choices = data.get("choices", [])
                if not choices:
                    logger.warning("视觉模型返回无 choices")
                    return None
                message = choices[0].get("message", {}) or {}
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    content = strip_reply_prefix(
                        (message.get("content") or "").strip())
                    if not content:
                        if sticker_sent:
                            # 纯表情包回复：图已发出（工具循环内），空文本
                            # 是预期终态，绝不降级闲聊
                            return {"kind": "text", "content": "",
                                    "sticker_sent": True}
                        logger.warning("视觉模型返回空 content")
                        return None
                    if sticker_sent:
                        return {"kind": "text", "content": content,
                                "sticker_sent": True}
                    return {"kind": "text", "content": content}
                # 任务/提醒工具调用优先原样返回（既有单次语义不变，不让查询
                # 工具往返延迟任务投递）；查询型工具（搜索/抓取/记忆检索/
                # 触发器管理/表情包）回填结果继续循环——manage_reminder 常要
                # list→cancel/modify 连环操作，必须在循环内回执
                other = [tc for tc in tool_calls
                         if _tool_call_name(tc) not in
                         ("web_search", "web_fetch", "recall_memory",
                          "manage_reminder", "send_sticker",
                          "search_stickers")]
                if other:
                    fn = other[0].get("function", {}) or {}
                    return {"kind": "tool_call",
                            "name": fn.get("name", ""),
                            "arguments": fn.get("arguments", "")}
                # 回填：assistant(tool_calls) + 每个调用的 tool 结果消息。
                # 就地 append——payload["messages"] 与本地列表同引用，末轮
                # 循环耗尽后直接退出（最后一次要求的工具调用不再执行）
                messages.append({"role": "assistant",
                                 "content": message.get("content") or "",
                                 "tool_calls": tool_calls})
                for tc in tool_calls:
                    name = _tool_call_name(tc)
                    if name == "web_search":
                        query = _tool_arg(tc, "query")
                        if not query:
                            tool_text = "web_search 参数缺少 query"
                        else:
                            try:
                                tool_text = format_search_results(
                                    query, web_search(query))
                            except WebSearchError as e:
                                tool_text = f"搜索暂时不可用：{e}"
                        logger.info(f"[搜索] {query!r} -> {tool_text[:80]}")
                    elif name == "recall_memory":
                        query = _tool_arg(tc, "query")
                        if not chat_id:
                            tool_text = "没有可检索的记忆上下文"
                        elif not query:
                            tool_text = "recall_memory 参数缺少 query"
                        else:
                            try:
                                tool_text = self._recall_memory(chat_id, query)
                            except Exception as e:
                                tool_text = f"记忆检索失败：{e}"
                        logger.info(f"[记忆检索] {query!r} -> {tool_text[:60]}")
                    elif name == "manage_reminder":
                        raw_args = ((tc or {}).get("function") or {})\
                            .get("arguments") or "{}"
                        tool_text = self._exec_manage_reminder(chat_id, raw_args)
                        logger.info(
                            f"[触发器管理] {chat_id!r} -> {tool_text[:80]}")
                    elif name == "send_sticker":
                        ok, tool_text = self._exec_send_sticker(
                            chat_id, _tool_arg(tc, "file"))
                        if ok:
                            sticker_sent = True
                    elif name == "search_stickers":
                        tool_text = self._exec_search_stickers(
                            _tool_arg(tc, "query"))
                        logger.info(
                            f"[表情包搜索] {tool_text[:60]}")
                    else:  # web_fetch（任务/提醒已在 other 分支返回）
                        url = _tool_arg(tc, "url")
                        if not url:
                            tool_text = "web_fetch 参数缺少 url"
                        else:
                            try:
                                tool_text = web_fetch(url)
                            except Exception as e:
                                tool_text = f"网页抓取失败：{e}"
                        logger.info(f"[抓取] {url[:60]} -> {tool_text[:60]}")
                    messages.append({"role": "tool",
                                     "tool_call_id": tc.get("id") or "",
                                     "content": tool_text})
            logger.warning("[vision] web_search 循环达补全次数上限，放弃")
            return None
        except Exception as e:
            logger.error(f"视觉模型调用失败: {e}")
            return None

    def _route_vision_result(self, chat_name, sender, result, img_paths=None):
        """vision 单调用结果分流 hook（可覆写）。

        result 为 call_vision_api 的 dict 返回（{'kind':'tool_call',
        'name','arguments'} | {'kind':'text','content'}）或 None；基类默认
        返回 None（未处理），由 AgentBot（xiaoli_bot）覆写：tool_call →
        任务投递，text → 直接回复。img_paths 为截图临时文件路径列表（调用
        方 finally 负责清理，覆写方需在返回前同步消费）。"""
        return None

    # ---------- 文件文本提取 / 媒体捕获 ----------
    # 实现在 xiaoli_app.file_text（模块函数）与 xiaoli_app.media_capture
    # （MediaCaptureMixin）；这里保留方法形态，tests / xiaoli_bot 的实例级
    # 调用与 mock.patch.object 打点不变。

    def _extract_file_text(self, filepath):
        return extract_file_text(filepath)

    def _extract_file_display_name(self, msg):
        return extract_file_display_name(msg)

    def _extract_office_com_text(self, filepath, app_name):
        return extract_office_com_text(filepath, app_name)

    def _find_file_by_display_name(self, display_name):
        """按消息中的显示文件名在接收目录定位对方发来的文件。

        实现在模块级 _find_file_by_display_name_impl（纯函数，单测直测）：
        分隔符归一化包含匹配 + (N) 重名编号最大优先 + ctime 平局。"""
        return _find_file_by_display_name_impl(self.file_storage_path,
                                               display_name)


    def call_chat_ai(self, chat_id, user_msg, sender_name=None, is_group=False, multi_sender=False):
        # per-chat 角色卡绑定：该聊天绑定了卡时，人设/模型/参数/端点以卡为准
        ov = self._chat_overrides(chat_id)
        headers = {
            "Authorization": f"Bearer {ov.get('ai_api_key') or self.api_key}",
            "Content-Type": "application/json"
        }
        if is_group:
            # 群聊格式 = 群聊名 + 发送者名 + 内容（用户原话：群聊：XXX XXX：消息内容）。
            # 显式三分支：
            #   1. multi_sender=True：user_msg 已由 _handle_unread_session 合并成
            #      「发送者A：内容A\n发送者B：内容B」（每条自带发送者名），
            #      只包『群聊：群名』前缀，不再重包 sender（否则双层嵌套）。
            #   2. sender_name 存在：群聊名 + 发送者名 + 内容（单条，现状）。
            #   3. sender_name 缺失（极端 OCR 失败）：群聊名兜底并打日志，
            #      绝不落入「群聊：{user_msg}」无名字退化分支。
            # multi_sender 是唯一显式信号——禁止用 user_msg 含换行等隐式判断
            # （单条多行文本消息会误判）。
            if multi_sender:
                decorated = f"群聊：{chat_id} {user_msg}"
            elif sender_name:
                decorated = f"群聊：{chat_id} {sender_name}：{user_msg}"
            else:
                logger.warning(f"[群聊] {chat_id} 视觉层未读到发送者名，用群聊名兜底")
                decorated = f"群聊：{chat_id}：{user_msg}"
        else:
            decorated = f"私聊 - {sender_name}：{user_msg}" if sender_name else f"私聊：{user_msg}"
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")
        # 消息布局（缓存友好，与 call_vision_api 对齐）：稳定前缀在前
        # （人设 → 回复纪律），每轮变化区在尾（历史 → 重要记忆 → 相关记忆
        # → 当前时间 → 当前消息）。重要记忆在历史之后的尾区——压缩提交
        # 改写它时只作废尾巴，不打碎「人设+历史」前缀缓存。
        messages = [
            {"role": "system",
             "content": ov.get("system_prompt") or self.system_prompt},
        ]
        # 回复风格纪律：与 vision 链路同一份运行时常量、同一位置（紧跟人设），
        # 保证降级/触发器回递时的说话方式不发生突变。
        if REPLY_STYLE_RULES:
            messages.append({"role": "system", "content": REPLY_STYLE_RULES})
        for h in self._get_history(chat_id):
            if "time" in h:
                ts = h["time"]
                msg_content = f"[{ts}] {h['content']}"
            else:
                msg_content = h['content']
            messages.append({"role": h["role"], "content": msg_content})
        important = self._important_block(chat_id)
        if important:
            messages.append({"role": "system", "content": important})
        related = self._match_related_memory(chat_id, user_msg)
        if related:
            messages.append({"role": "system", "content": related})
        messages.append({"role": "system", "content": f"当前时间：{current_time}"})
        messages.append({"role": "user", "content": decorated})

        with self._model_lock:
            # 空模型兜底（活跃卡缺失被模板补建后 chat_model 为空）：空串原样
            # 发出必 400，与 vision 链路共用同一兜底纯名；绑定卡带模型/参数
            # 时以卡为准（per-chat 覆盖）
            model = strip_model_prefix(ov.get("chat_model") or "") \
                or self.chat_model or VISION_MODEL_DEFAULT
            temp = float(ov["temperature"]) if ov.get("temperature") is not None \
                else self.chat_temperature
            top_p = float(ov["top_p"]) if ov.get("top_p") is not None \
                else self.chat_top_p

        # 上下文预算裁剪：文件全文/超长历史会撑爆模型上下文上限
        # （实测请求 272 万 token → API 400 "maximum context length"）。
        # 从最旧历史开始丢弃，保证单次请求不超模型上下文。
        messages = fit_messages_in_budget(
            messages, budget=getattr(self, "max_context_tokens", 100000))

        payload = {
            "model": model,
            "messages": messages,
            # 回复长度物理上限（历史缺陷：chat 链路完全没有 max_tokens，
            # 降级/触发器回递时模型可以无限长——与 vision 链路共用同一上限）
            "max_tokens": int(getattr(self, "reply_max_tokens", REPLY_MAX_TOKENS)),
            "temperature": temp,
            "top_p": top_p
        }
        try:
            data = self._post_chat_completions(
                ov.get("ai_api_url") or self.api_url, headers, payload,
                self.api_timeout, label="chat",
                meta={"kind": "chat", "model": model, "messages": messages})
        except ApiCallError as e:
            logger.error(f"聊天 API 调用失败: {e}")
            return random.choice(FRIENDLY_API_ERROR_REPLIES)
        choices = data.get("choices", [])
        if choices and "message" in choices[0]:
            reply = choices[0]["message"]["content"]
        else:
            logger.error(f"API返回异常 choices 为空: {data}")
            return random.choice(FRIENDLY_API_ERROR_REPLIES)
        reply = strip_reply_prefix(reply)
        self._add_history(chat_id, "user", decorated)
        self._add_history(chat_id, "assistant", reply)
        return reply

    def _split_reply_parts(self, text):
        """把回复文本切成待发送的段：按换行切 → 丢空段 → 剥段尾单个中文句号。

        拆分口径（用户定案，保持不变）：**任意连续换行都切**（含单 \\n）——
        人设要求碎句多段，模型用单换行分句时旧逻辑会整段一起发。
        """
        parts = []
        for raw in re.split(r"\n+", text or ""):
            part = _strip_trailing_period(raw.strip())
            if part:
                parts.append(part)
        return parts

    def _segment_hold(self):
        """段间停留秒数（_send_parts 每段粘贴进输入框之后、回车之前）。

        两个互斥开关（UI 保证互斥，后端抖动优先，可都关）：
        - 间隔抖动开 → [下限, 上限] 均匀随机（活人感：固定间隔是机器味）
        - 段间等待开 → 固定秒数（历史行为，默认 2s）
        - 都关 → 0（下一段立即回车）
        参数热改即时生效；下限>上限时收敛为下限（防设置页存出反区间）。"""
        if getattr(self, "segment_jitter_enabled", False):
            lo = max(0.0, float(getattr(self, "segment_jitter_min", 1.5)))
            hi = max(lo, float(getattr(self, "segment_jitter_max", 3.0)))
            return random.uniform(lo, hi)
        if getattr(self, "segment_wait_enabled", True):
            return max(0.0, float(getattr(self, "segment_wait_seconds", 2.0)))
        return 0.0

    def _send_parts(self, chat, text):
        """逐段发送（段间节奏见 _segment_hold：默认固定 2s，可换上下限随机
        或关闭）。

        节奏口径（用户定案）：间隔发生在**下一段粘贴进输入框之后、回车
        之前**——对方端在这段停留里看到「对方正在输入…」，多条消息有活人
        感；首段之前不停（hold=0）。由后端 send_text 的 hold_after_paste
        参数承载，本方法不再自己 sleep。
        普通回复（_send_text）与触发器旁路（AgentBot._send_trigger_reply）
        共用本方法——两处各写一遍「拆分 + 间隔」必然漂移。本方法**不碰占位
        计数**：占位归零是普通回复的语义，触发器路径不走。
        返回实际发送的段数（0 = 拆不出内容，调用方自行决定兜底）。
        """
        parts = self._split_reply_parts(text)
        for i, part in enumerate(parts):
            hold = 0.0 if i == 0 else self._segment_hold()
            self.wx.send_text(chat, part, hold_after_paste=hold)
            preview = part[:50].replace('\n', ' ')
            logger.info(f"🤖 → [{chat}]: {preview}")
        return len(parts)

    def _send_text(self, text, chat, placeholder=False):
        """发送文本到指定聊天。placeholder=True 表示这是"处理中"占位消息
        （永不拆分、计数 +1）；默认 False（普通回复）按换行拆成多条发送
        （段尾单个句号剥掉、段间固定间隔），并把该聊天的占位计数归零。
        计数按 chat_name 隔离（_pending_placeholders），非全局。"""
        try:
            # 占位消息永不拆分，单条发送，计数语义不变
            if placeholder:
                cleaned_text = text.strip()
                self.wx.send_text(chat, cleaned_text)
                preview = cleaned_text[:50].replace('\n', ' ')
                logger.info(f"🤖 → [{chat}]: {preview}")
                self._pending_placeholders[chat] = self._pending_placeholders.get(chat, 0) + 1
                return

            if not self._send_parts(chat, text):
                # 拆不出任何段（全空白）→ 退回原文单条发送，绝不静默吞消息
                cleaned_text = text.strip()
                self.wx.send_text(chat, cleaned_text)
                preview = cleaned_text[:50].replace('\n', ' ')
                logger.info(f"🤖 → [{chat}]: {preview}")

            # 实质回复归零占位，只执行一次（pop 对未占位过的 chat 安全）
            self._pending_placeholders.pop(chat, None)
        except Exception as e:
            logger.error(f"发送失败: {e}")

    def _send_voice_reply(self, chat, text, emotion=None, text_fallback=None):
        """语音回复：按回复分段逐段合成、逐段发送（段间无额外间隔——语音条
        自带录音时长与尾音节奏）。

        fail-closed 语义（用户定案）：
        - 合成阶段任一段失败 / 单段时长超 voice_max_seconds → **整条**回退
          文本（此刻什么都还没发出，绝不发半截语音 + 半截文字）
        - 发送阶段某段 send_voice 返回 False（= 该段确认未发出，质检/切换/
          胶囊闸门任一未过）→ 剩余段回退文本，已发出的语音段保持
        - 无语音能力（后端不支持/配置不全）→ 整条回退文本，回复永不丢
        - 颜文字/emoji 先经 strip_unspeakable 剥除（TTS 读它们是乱语），
          剥完整条无内容（纯颜文字回复）→ 原文回退文本

        emotion：情绪键（auto 模式由模型从档案 refs 枚举里选；always 模式
        不传 = 固定「通用」参考）。
        text_fallback：回退发送 callable(text)——触发器路径传 _send_parts
        语义（旁路占位归零），缺省走 _send_text（普通回复语义）。
        语音内容**以纯文本写记忆**由调用方负责（这里只管发送）。
        返回 True = 全部段已按语音发出；False = 已（部分）回退文本。
        """
        # 配置齐全即可发送（不含模式检查）：调用方（_deliver_reply 的
        # always 分支 / send_voice 工具注入）已按该聊天 voice_state 把过
        # 关——per-chat 强制开时全局 mode 可为 off，这里若再查模式会把
        # 已注入工具的语音回复静默丢掉
        profile = self._voice_profile_ready()
        if profile is None:
            return False
        parts = [strip_unspeakable(p)
                 for p in self._split_reply_parts(text)]
        parts = [p for p in parts if p]
        if not parts:
            # 剥完颜文字后无可读文本（纯颜文字回复）→ 原文回退文本：
            # 念不出来，但对方至少能「看到」这个表情
            logger.info("[语音] 剥除颜文字后无可读文本，整条回退文本")
            self._voice_text_fallback(chat, text, text_fallback)
            return False
        client = GptSovitsClient(self.tts_endpoint, timeout=self.tts_timeout)
        wavs = []
        try:
            for part in parts:
                wav = synthesize_with_emotion(client, profile, part, emotion)
                dur = wav_duration_seconds(wav)
                if dur > self.voice_max_seconds:
                    # 微信语音条 60s 硬上限前的保险丝（回复纪律下单段一般
                    # 3~15s，超限属异常长文）——整条回退文本
                    raise TtsError(f"单段语音 {dur:.1f}s 超上限 "
                                   f"{self.voice_max_seconds}s")
                wavs.append((wav, dur))
        except TtsError as e:
            logger.error(f"[语音] 合成失败，整条回退文本: {e}")
            self._voice_text_fallback(chat, text, text_fallback)
            return False
        send_voice = getattr(self.wx, "send_voice", None)
        if send_voice is None:
            logger.warning("[语音] 后端不支持语音发送，整条回退文本")
            self._voice_text_fallback(chat, text, text_fallback)
            return False
        tmp_files = []
        try:
            for i, (wav, dur) in enumerate(wavs):
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                    tf.write(wav)
                    path = tf.name
                tmp_files.append(path)
                if not send_voice(chat, path):
                    remaining = "\n".join(parts[i:])
                    logger.error(f"[语音] 第 {i + 1}/{len(wavs)} 段发送未确认"
                                 "（该段未发出），剩余段回退文本")
                    self._voice_text_fallback(chat, remaining, text_fallback)
                    return False
                logger.info(f"🎤 → [{chat}]: 语音条 {i + 1}/{len(wavs)}"
                            f"（{dur:.1f}s）{parts[i][:30]}")
            return True
        finally:
            for p in tmp_files:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _voice_text_fallback(self, chat, text, text_fallback):
        """语音失败后的文本兜底发送（text_fallback 为触发器语义时旁路占位归零）。"""
        try:
            if text_fallback is not None:
                text_fallback(text)
            else:
                self._send_text(text, chat)
        except Exception as e:
            logger.error(f"[语音] 文本回退发送失败: {e}")

    def fetch_models(self):
        url = models_endpoint(self.api_url)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                return [m["id"] for m in resp.json().get("data", [])]
            else:
                logger.warning(f"获取模型列表失败: {resp.status_code}")
                return None
        except Exception as e:
            logger.error(f"请求模型列表异常: {e}")
            return None

    def run(self, stop_event=None, poll_interval=0.5):
        state = "暂停中，输入 resume 开始回复" if self.paused else "运行中"
        logger.info(f"✅ 小漓已启动（{state}）")
        while True:
            if stop_event is not None and stop_event.is_set():
                logger.info("🛑 引擎已停止")
                self._flush_memory()  # 退出前落盘节流窗口内的记忆
                return
            try:
                self.process_new_messages()
            except Exception as e:
                logger.error(f"主循环异常: {e}\n{traceback.format_exc()}")
            # 可中断睡眠：stop 响应延迟 ≤100ms（控制台模式不传 stop_event，行为不变）
            # poll_interval 0.5s 恒定快档（与 GUI 引擎一致，不间断监听）
            remain = poll_interval
            while remain > 1e-9:
                if stop_event is not None and stop_event.is_set():
                    logger.info("🛑 引擎已停止")
                    self._flush_memory()
                    return
                time.sleep(min(0.1, remain))
                remain -= 0.1


class Controller:
    """命令行控制器：命令注册表分发（子类可扩展命令）。

    设计：命令首词 → handler（接收完整命令串含参数）。新增命令 = 注册
    dict 加一项 + 实现 handler，不再复制整段 if/elif 链（历史：Tianshu
    Controller 复制 _listen 150 行只为了加两个命令）。
    quit 语义：置 stop_event + flush 记忆 → bot.run(stop_event) 退出 →
    主线程正常收尾（不再 os._exit 硬退，避免丢节流窗口内的记忆）。"""

    # 命令名 → 一行说明（help 命令展示；子类合并扩展）
    HELP = {
        "pause": "暂停自动回复",
        "resume": "恢复自动回复",
        "model": "切换聊天模型（model 交互选择 / model <名称> 直接切换并持久化）",
        "chat-temp": "设置聊天模型温度 (0~2)",
        "chat-top-p": "设置聊天模型 top_p (0~1)",
        "clear": "清空对话历史（全部 / clear <聊天ID>）",
        "del": "删除聊天中的消息：del <聊天ID> <序号1> [序号2] [3-5]",
        "memory": "查看聊天历史消息（带序号）",
        "status": "查看当前状态（模型信息、温度、top_p）",
        "quit": "退出程序",
        "help": "显示本帮助",
    }

    def __init__(self, bot):
        self.bot = bot
        self.stop_event = threading.Event()  # quit 置位 → bot.run 退出（优雅收尾）
        self._commands = {}
        self._register_commands()

    def _register_commands(self):
        """注册命令处理器（key = 命令首词，handler 接收完整命令串）。"""
        self._commands.update({
            "help": self._cmd_help,
            "pause": self._cmd_pause,
            "resume": self._cmd_resume,
            "model": self._cmd_model,
            "chat-temp": self._cmd_chat_temp,
            "chat-top-p": self._cmd_chat_top_p,
            "clear": self._cmd_clear,
            "del": self._cmd_del,
            "memory": self._cmd_memory,
            "status": self._cmd_status,
            "quit": self._cmd_quit,
        })

    def start(self):
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        while True:
            try:
                cmd = input().strip().lower()
                if not cmd:
                    continue
                name = cmd.split(" ", 1)[0]
                handler = self._commands.get(name)
                if handler is None:
                    logger.info(f"❌ 未知命令: {cmd}")
                    continue
                handler(cmd)
            except (EOFError, KeyboardInterrupt):
                break

    # ---------- 命令处理器 ----------

    def _cmd_help(self, cmd):
        lines = ["可用命令："]
        for name, handler in self._commands.items():
            lines.append(f"  {name:<12} - {self.HELP.get(name, '')}")
        print("\n".join(lines))

    def _cmd_pause(self, cmd):
        self.bot.paused = True
        logger.info("⏸️  已暂停自动回复")

    def _cmd_resume(self, cmd):
        self.bot.paused = False
        logger.info("▶️  已恢复自动回复")

    def _cmd_model(self, cmd):
        rest = cmd[len("model"):].strip()
        if not rest:
            self._select_model("chat")
            return
        with self.bot._model_lock:
            self.bot.chat_model = rest
        logger.info(f"🔄 聊天模型已切换为：{rest}")
        self._persist_model_setting("chat_model", rest)

    def _persist_model_setting(self, key, value):
        """CLI 切模型持久化：写回 config.json + 活跃角色卡（GUI 模式下
        投影字段以卡为准，写卡才能跨重启生效）。失败仅告警不中断。"""
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg[key] = value
            with open("config.json", "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=4)
            try:
                from xiaoli_app import card_store
                cid = cfg.get("active_card_id")
                if cid:
                    cards_dir = os.path.join(
                        os.path.dirname(os.path.abspath("config.json")), "cards")
                    card = card_store.get_card(cards_dir, cid)
                    if card is not None:
                        card[key] = value
                        card_store.save_card(cards_dir, card)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"保存配置失败: {e}")

    def _cmd_chat_temp(self, cmd):
        self._set_numeric(cmd, "chat-temp", 0, 2, "温度", self.bot.set_chat_temperature)

    def _cmd_chat_top_p(self, cmd):
        self._set_numeric(cmd, "chat-top-p", 0, 1, "top_p", self.bot.set_chat_top_p)

    def _set_numeric(self, cmd, name, lo, hi, label, setter):
        rest = cmd[len(name):].strip()
        if not rest:
            logger.warning(f"用法: {name} <值>（{lo}~{hi}）")
            return
        try:
            val = float(rest)
            if lo <= val <= hi:
                setter(val)
            else:
                logger.warning(f"{label}值应在 {lo}~{hi} 之间")
        except ValueError:
            logger.warning("请输入有效的数字")

    def _cmd_clear(self, cmd):
        rest = cmd[len("clear"):].strip()
        if rest:
            self.bot.clear_history(rest)
        else:
            self.bot.clear_history()

    def _cmd_del(self, cmd):
        parts = cmd.split()
        if len(parts) < 3:
            logger.info("用法: del <聊天ID> <序号1> [序号2] ... 或 del <聊天ID> <起始序号-结束序号>")
            return
        chat_id = parts[1]
        indices = []
        for part in parts[2:]:
            if '-' in part:
                try:
                    start, end = map(int, part.split('-'))
                    if start > end:
                        start, end = end, start
                    indices.extend(range(start, end + 1))
                except ValueError:
                    logger.warning(f"无效的范围格式: {part}")
            else:
                try:
                    indices.append(int(part))
                except ValueError:
                    logger.warning(f"无效的序号: {part}")
        if not indices:
            return
        print(f"即将从聊天 '{chat_id}' 中删除序号: {sorted(set(indices))}")
        confirm = input("确认删除？(y/n): ").strip().lower()
        if confirm == 'y':
            self.bot.delete_messages(chat_id, indices)
        else:
            logger.info("已取消删除")

    def _cmd_memory(self, cmd):
        target = cmd[len("memory"):].strip()
        self._show_memory(target)

    def _cmd_status(self, cmd):
        self._show_status()

    def _cmd_quit(self, cmd):
        logger.info("👋 程序退出")
        self.stop_event.set()
        self.bot._flush_memory()  # 节流窗口内的记忆落盘（run 退出路径也会 flush，双保险）

    def _show_status(self):
        total_msgs = sum(
            len(v.get("recent", [])) if isinstance(v, dict) else len(v)
            for v in self.bot.memory_db.values())
        deep_msgs = sum(self.bot._deep_count.values())
        chat_count = len(self.bot.memory_db)
        print(f"当前聊天模型：{self.bot.chat_model}")
        print(f"  聊天温度: {self.bot.chat_temperature}")
        print(f"  聊天 top_p: {self.bot.chat_top_p}")
        print(f"视觉模型：随聊天模型（{self.bot.chat_model}，单模型化）")
        print(f"对话记忆：共 {chat_count} 个聊天，近期 {total_msgs} 条"
              f"（深层存档 {deep_msgs} 条）")
        if chat_count > 0:
            print("具体聊天对象：")
            for chat in self.bot.memory_db.keys():
                print(f"  - {chat}")
        print(f"当前状态：{'运行中' if not self.bot.paused else '已暂停'}")

    def _show_memory(self, target):
        matches = [chat for chat in self.bot.memory_db if target in chat]
        if not matches:
            logger.info(f"❌ 未找到包含 '{target}' 的聊天记录")
            return
        chat_key = matches[0]
        st = self.bot.memory_db[chat_key]
        recent = st.get("recent", []) if isinstance(st, dict) else st
        print(f"📂 聊天对象: {chat_key}，近期 {len(recent)} 条消息：")
        for i, msg in enumerate(recent, 1):
            role = "用户" if msg["role"] == "user" else "小漓"
            ts = msg.get("time", "未知时间")
            content = msg["content"]
            print(f"  {i}. [{ts}] {role}: {content}")
        if isinstance(st, dict):
            imp = [x.get("content") for x in st.get("important", [])
                   if isinstance(x, dict) and x.get("content")]
            if imp:
                print("📌 重要记忆：")
                for c in imp:
                    print(f"  - {c}")
            if st.get("index"):
                print(f"🗂 关键词索引：{len(st['index'])} 条"
                      "（命中时自动注入，memory <聊天ID> 不逐条展示）")

    def _select_model(self, model_type):
        was_paused = self.bot.paused
        self.bot.paused = True
        logger.info(f"⏸️  已暂停，正在获取模型列表...")
        models = self.bot.fetch_models()
        if not models:
            logger.error("无法获取模型列表")
            self.bot.paused = was_paused
            return
        for i, m in enumerate(models):
            print(f"  {i+1}. {m}")
        choice = input("请输入序号或模型名，输入 cancel 取消: ").strip()
        if choice.lower() == "cancel":
            logger.info("已取消")
            self.bot.paused = was_paused
            return
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(models):
                new_model = models[idx]
            else:
                logger.error("序号超出范围")
                self.bot.paused = was_paused
                return
        except ValueError:
            new_model = choice
        with self.bot._model_lock:
            # 单模型化：视觉/分类统一走 chat_model，仅 chat 分支可切换
            self.bot.chat_model = new_model
            logger.info(f"🔄 聊天模型已切换为：{new_model}")
        ask = input("是否清空所有对话历史？(y/n): ").strip().lower()
        if ask == 'y':
            self.bot.clear_history()
        self.bot.paused = was_paused
        if not self.bot.paused:
            logger.info("▶️  已自动恢复回复")