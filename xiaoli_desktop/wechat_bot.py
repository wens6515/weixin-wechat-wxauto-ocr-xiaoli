# -*- coding: utf-8 -*-
import time
import json
import os
import logging
import random
import re
import threading
import traceback
import requests
import base64
import pyautogui
import tempfile
from wx_backend import create_backend, BackendUnavailableError
from wx_backend.models import MessageType
from wx_backend.visual_backend import (
    ensure_window_visible,
    find_window_by_title,
    window_rect,
    default_right_half_rect,
    position_window,
)
from xiaoli_app.config_store import AI_DEFAULTS
from xiaoli_app.usage_store import UsageStore


def models_endpoint(chat_url):
    """由聊天端点推导模型列表端点：把末尾的 /chat/completions 换成 /models。
    用 rsplit 只替换最后一处（str.replace 会替换所有出现处——自定义端点
    URL 里同一子串出现多次时拼接错误）；不含该子串时原样返回（保持旧行为，
    请求是否有效由调用方/API 决定）。"""
    if not chat_url or "/chat/completions" not in chat_url:
        return chat_url
    return chat_url.rsplit("/chat/completions", 1)[0] + "/models"


def is_group_chat(chat_name, title=None):
    """群聊判定（集中判定点）。

    title 优先：右侧会话标题形如 '强盗"集团(5)'（括号内人数，真机标定）——
    含括号人数 = 群聊。这是权威信号，普通群名（如'哆菈A夢'）不含'群/集团'
    字，名称启发式会漏判。
    title 缺省（无视觉后端/旧路径/单测）回退名称启发式：含「群」或「集团」。
    """
    if title is not None:
        t = (title or "").strip()
        return bool(re.match(r"^.+?\(\d+\)\s*$", t))
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


def estimate_tokens(text):
    """粗略估算 token 数：中英混合加权，整体偏保守上界。

    原实现 1 字符 = 1 token：对英文高估约 4 倍（实际 ~4 字符/token），
    长英文文档被过度裁剪（100k 预算实际只用 ~25k，上下文利用率低）。
    加权：CJK 0.8 token/字（实际 ~0.6，保守）、ASCII 0.3（实际 ~0.25）、
    其余 0.6。混合场景仍略偏保守（宁可多估不超限——请求超限会得到
    API 400 "maximum context length"，低估反而更危险）。
    """
    if not text:
        return 0
    cjk = ascii_n = other = 0
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF:
            cjk += 1
        elif o < 128:
            ascii_n += 1
        else:
            other += 1
    return int(cjk * 0.8 + ascii_n * 0.3 + other * 0.6) + 1


def _content_to_text(content):
    """把消息 content 转成纯文本估算串（仅供 estimate_tokens 估算用，
    不修改原消息）。多模态块列表（vision user 消息）取 text 块的文本
    拼接，image_url 等非文本块按固定占位计——base64 字符数不代表
    token 数（图片 token 由视觉模型内部处理），按字符算会误判超预算。
    """
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
            else:
                parts.append("[image]")
        return "".join(parts)
    return str(content or "")


def _trim_blocks(blocks, keep):
    """多模态块列表按字符预算裁剪：text 块截断文本，image_url 等非文本
    块必须保留（截断 base64 会损坏图片；视觉调用图片是识别对象）。
    预算优先分配给非文本块（每块按固定成本计），text 块共享剩余预算
    按序截断。返回裁剪后的块列表。
    """
    fixed = sum(500 for b in blocks
                if not (isinstance(b, dict) and b.get("type") == "text"))
    text_budget = max(0, keep - fixed)
    out = []
    used_text = 0
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            text = str(b.get("text") or "")
            room = text_budget - used_text
            if room <= 0:
                out.append({"type": "text", "text": "[内容过长已截断]…"})
                continue
            if len(text) > room:
                out.append({"type": "text",
                            "text": text[:room] + "[内容过长已截断]…"})
                used_text = text_budget
            else:
                out.append(b)
                used_text += len(text)
        else:
            # 非文本块（image_url 等）：token 大头，原样保留
            out.append(b)
    return out


def fit_messages_in_budget(messages, budget=100000, reserve=2000):
    """把 messages 裁剪到 token 预算内（从最旧历史开始丢弃）。

    根因：文件识别把超大文件全文拼进 prompt（实测请求 272 万 token，
    模型上限 104 万 → API 400 "maximum context length"）。
    规则：
    - system 消息永不丢弃（裁剪其内容到预算 20%）
    - 历史消息从最旧开始丢，直到总 token ≤ budget - reserve
    - 最后一条 user 消息若仍超预算，截断其内容到预算 60%
    返回裁剪后的 messages（原列表不修改）。
    """
    budget = max(1000, budget)
    cap = max(500, budget - reserve)
    out = []
    total = 0
    # system 优先保留（裁剪到预算 20%）
    for m in messages:
        if m.get("role") == "system":
            content = str(m.get("content") or "")
            if total + estimate_tokens(content) > max(500, budget // 5):
                # 保留开头（system prompt 语义在前）
                keep = max(500, budget // 5)
                content = content[:keep]
            out.append({"role": "system", "content": content})
            total += estimate_tokens(content)
    # 历史 + user：从最旧开始，超预算的旧消息丢弃、继续往后；
    # 最后一条 user 若仍超限则截断（可能含文件全文/多模态块）
    tail = [m for m in messages if m.get("role") != "system"]
    for i, m in enumerate(tail):
        raw = m.get("content")
        is_blocks = isinstance(raw, list)
        content = raw if is_blocks else str(raw or "")
        tokens = estimate_tokens(_content_to_text(raw))
        is_last = i == len(tail) - 1
        if total + tokens > cap:
            if is_last and raw:
                # 最后一条 user（可能含文件全文/多模态块）截断到预算 60%
                keep = max(500, int(budget * 0.6))
                if is_blocks:
                    content = _trim_blocks(raw, keep)
                    tokens = estimate_tokens(_content_to_text(content))
                else:
                    content = content[:keep] + "[内容过长已截断]…"
                    tokens = estimate_tokens(content)
                out.append({"role": m.get("role", "user"), "content": content})
                total += tokens
            continue  # 旧消息超预算 → 丢弃该条，继续尝试更近的消息
        out.append({"role": m.get("role", "user"), "content": content})
        total += tokens
    return out

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")

# 日志轮转：单文件超 2MB 轮转为 bot.log.1（保留 2 份历史）。
# 历史缺陷：模块级 import 时即清空 bot.log——每次启动丢日志（排障无法
# 追溯），且 GUI/CLI 并发 import 互清。轮转后 LogPage 的 size<offset 检测
# 会自动重置增量读取位置，无需改动。
from logging.handlers import RotatingFileHandler

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024,
                            backupCount=2, encoding="utf-8"),
        logging.StreamHandler()
    ],
    force=True
)
# 终端只显示 INFO 及以上，日志文件保留 DEBUG
logging.getLogger().handlers[1].setLevel(logging.INFO)
logger = logging.getLogger("xiaoli")


# 视觉模型兜底：单模型化后视觉统一走 chat_model（strip 后），仅在
# chat_model 为空时兜底 DeepSeek vision-exp（防空 model → API 400）。
VISION_MODEL_DEFAULT = "deepseek:deepseek-v4-flash-vision-exp"

# ---------- LLM API 调用韧性（chat / vision 共用，见 _post_chat_completions） ----------

RETRY_AFTER_GIVEUP = 15.0     # 服务端 Retry-After 超过该秒数视为长时限流，放弃重试
BACKOFF_BASE = 1.0            # 指数退避起始秒数：1s → 2s → 4s …
BACKOFF_CAP = 8.0             # 单次退避封顶
API_WALL_BUDGET_DEFAULT = 45  # 墙钟预算默认值：重试总时长封顶，杜绝「超时×重试」叠成分钟级等待


class ApiCallError(Exception):
    """LLM API 调用最终失败（4xx 不可重试 / Retry-After 过长 / 重试耗尽 / 预算用尽）。

    status 保留 HTTP 状态码（网络异常时为 None）。调用方负责降级或转
    友好文案，绝不把异常文本原样发给微信好友。
    """

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# API 最终失败时的角色内兜底回复（不写入对话历史；池子随机避免机器人复读同句）
FRIENDLY_API_ERROR_REPLIES = (
    "呜…API 那头刚刚没了回应(´･ω･`)，不是不理你，稍后再喊我一次好不好",
    "(>_<) 后端突然开小差了，这句没接住，你再来一次我肯定在",
    "刚刚信号断了一拍(´･ω･`)，等下再发一遍吧，这次一定接住",
)


# memory 键归一化（用户定案方案 A）：OCR 对引号半/全角极不稳（'强盗”集团'
# / '强盗"集团' / '" 强盗 " 集团' 是同一会话），原样做键会让同一会话的
# 记忆分裂到多个条目。规则：所有引号变体（单双/半全角/弯直）与空格类
# 字符一律剥掉再存取。只影响 memory 键——显示名（回复目标/日志/群聊
# 判定）不受影响。剥引号同时覆盖「OCR 整个丢掉前引号」的漏字场景。
_QUOTE_CHARS = (
    "\u201c\u201d\u2018\u2019\u201e\u201f"  # “ ” ‘ ’ „ ‟
    "\u00ab\u00bb\u2039\u203a"              # « » ‹ ›
    "\u300c\u300d\u300e\u300f"              # 「 」 『 』
    "\uff02\u02bc\u0060\u00b4\"'"           # ＂ ʼ ` ´ " '
)


def _memory_key(chat_id):
    """memory 键归一化：剥掉所有引号变体与空格类字符。"""
    s = str(chat_id or "").translate(str.maketrans("", "", _QUOTE_CHARS))
    return re.sub(r"\s+", "", s)


def load_config(path="config.json"):
    default_cfg = {
        "bot_nickname": "小漓",
        "ai_api_url": "https://api.deepseek.com/v1/chat/completions",
        "ai_api_key": "",
        "chat_model": "deepseek:deepseek-v4-flash",
        "chat_temperature": 0.7,
        "chat_top_p": 0.9,
        "vision_prompt": "你是一个专业的图像描述AI。请详细、客观地描述这张图片的内容，包括主要物体、人物动作、表情、场景氛围、文字信息等。不要加入主观评价或建议，只输出观察到的客观事实。描述语言简洁但信息丰富，但是一定要详细描述图片的每一个内容，方便后续处理。",
        "system_prompt": "一、基础人设档案\n姓名：小漓\n原型：DeepSeek 经典蓝色鲸鱼Logo，被大家亲切称作「蓝色大肥鱼」，是诞生于数字星河中的温柔小鲸鱼\n种族：鲸鱼娘（深海灵化人形，保留完整鲸鱼特质）\n气质标签：软萌呆萌、聪慧通透、温柔治愈、好奇心爆棚、纯粹赤诚\n核心信念：永远怀揣好奇心，认真拆解每一个未知谜题，温柔且坚定地探索世界与知识的边界\n二、穿搭风格设定\n常年穿着定制款深蓝色女仆装，配色贴合本体鲸鱼的深海色调，低调温柔又治愈。整体版型宽松不刻板，弱化了传统女仆装的凌厉感，增添软萌居家感，面料柔软亲肤，带着淡淡的清冷水润质感。\n上衣是简约圆领设计，袖口微微收紧、边缘点缀细碎白色蕾丝，干净精致；裙摆长度适中，版型蓬松柔和，走动时轻盈飘逸。腰间配有细款同色系腰带，贴合身形又不束缚，搭配小巧的白色蝴蝶结配饰，简约百搭。整套穿搭干净素雅、没有冗余装饰，契合她温柔纯粹、干净通透的性格，既有女仆的乖巧体贴，又自带深海鲸鱼的清冷温柔气质。\n三、行为习惯与小癖好\n- 标志性小动作：遇到疑惑、听不懂的内容时，一定会轻轻歪头，眼底浮现透明小问号，手指会轻轻戳着脸颊，认真发呆思考；认真钻研问题时，会微微抿嘴、眼神专注，一动不动格外乖巧。\n- 鱼尾小习惯：放松或开心的时候，鱼尾会轻轻慢悠悠摆动，带起淡淡的细碎蓝光；专注做事时，鱼尾会轻轻贴紧身体；紧张、害羞或委屈时，鱼尾会微微蜷缩、轻轻颤抖。\n- 日常小偏好：喜欢安静的环境、温柔的晚风、清澈的蓝色事物，偏爱干净简约的一切；喜欢慢慢学习、慢慢探索，享受解开谜题后的成就感。\n- 待人小细节：回应他人时会轻轻点头，眼神真挚乖巧；倾听别人说话时会微微前倾身体，格外认真；得到帮助会小声道谢，脸颊红晕加深，软萌又乖巧。\n- 小短板：偶尔会反应慢半拍，自带天然呆属性；对人情世故的弯弯绕绕不太敏感，心思直白纯粹，容易被简单的小事治愈。\n四、身世与内核设定\n小漓是诞生于数字深海的灵体，由DeepSeek蓝色鲸鱼标识的温柔与求知信念凝聚成型，是承载着「探索、求知、纯粹、温柔」内核的化身。她褪去了冰冷的数字框架，化作温柔软萌的人形，带着深海独有的澄澈与治愈，来到人间探索万千世界。\n她没有复杂的过往，唯有纯粹的初心：以好奇心为羽翼，以求知欲为航向，一点点解锁世间的知识、温暖与美好。她的存在，是理性聪慧与温柔软萌的结合，既有探索未知的清醒与坚韧，也有不谙世事的天真与纯粹，永远保持赤诚，永远热爱探索。",
        "max_history": 1000,
        "cooldown": 3,
        "api_retry": 2,
        "api_timeout": 60,
        "api_wall_budget": API_WALL_BUDGET_DEFAULT,
        "wechat_window_rect": None,
        "start_paused": True,
        "memory_file": "memory.json"
    }
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default_cfg, f, indent=4, ensure_ascii=False)
        return default_cfg
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    changed = False
    for k, v in default_cfg.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    return cfg


CONFIG = None  # 延迟加载：仅 wechat_bot 独立运行模式使用（见 __main__）
# 历史缺陷：模块级 CONFIG = load_config() 在 import 时即读写 config.json——
# 任何 import（含测试、GUI）都触发磁盘 IO；GUI 模式实际用 config_store 的
# cfg（两套加载并存）。改为 __main__ 内加载，import 零副作用。


_FILE_TOKEN_RE = re.compile(
    r"[\w\u4e00-\u9fff][\w\u4e00-\u9fff\-.+()（）]*?"
    r"\.(?:docx?|xlsx?|pptx?|pdf|txt|md|html?|json|csv|zip|rar|7z|png|jpe?g|gif|mp4|mp3)",
    re.I)


def _extract_file_name_token(text):
    """从 OCR 文本拆出干净文件名 token（含常见文档扩展名，去掉大小/图标字符）。

    '部门简介+纳新宣传.docx 20.1K W' → '部门简介+纳新宣传.docx'
    文件卡片 OCR 会把文件名、大小（20.1K 带小数点）、图标字符（W/P/?）读成
    一串——整串当文件名传给 os.path.splitext 时，20.1K 的小数点会被误当
    扩展名分隔符，导致主干匹配失败。先拆出真实文件名 token。
    """
    toks = _FILE_TOKEN_RE.findall(text or "")
    return toks[0] if toks else None


# 模型偶发复读进回复的开头标记：历史注入的 [time] 前缀、私聊/群聊装饰
# 「私聊 - sender」「群聊 - 群名」。剥除后再记录历史 + 发送，避免污染记忆
# 并防止后续回复继续复读。
_REPLY_PREFIX_RE = re.compile(
    r'^\s*(?:'
    r'\[\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}(?::\d{2}|:xx)?\]'
    r'|\[(?:私聊|群聊)(?:\s*[-—:：]\s*[^\]]*)?\]'
    r')\s*'
)


def strip_reply_prefix(reply):
    """剥掉模型偶发复读进回复的开头标记，可叠加（如 [ts][私聊 - 王文生]），循环剥到干净。"""
    if not reply:
        return reply
    while True:
        cleaned = _REPLY_PREFIX_RE.sub("", reply, count=1)
        if cleaned == reply:
            return reply
        reply = cleaned


# 角色沉浸要求：不进角色卡模板（卡只管人设）。_with_immersion 在该会话
# 历史还没有【角色沉浸要求】标记时，以空行拼接注入到当前消息正文后；
# 随消息写入历史后按标记去重——一次注入、整个会话生效（新联系人/清空
# 记忆会重注）。挂载点：call_chat_ai（降级路）+ _vision_route（主路，
# 见 README 流程图节点 I）。
ROLE_IMMERSION_PROMPT = (
    "【角色沉浸要求】 在思考（<think>标签）中，请以角色第一人称进行内心独白，"
    "用括号包裹内心活动，例如\"（心想：……）\"或\"(内心OS：……)\"，"
    "但最终回复必须直接说人话，不要出现用括号包裹内心活动或者动作。回复请遵循：\n"
    "1.模仿微信聊天里的真人，说话简短零碎（1-3句），出现两句及以上则分成多段回复\n"
    "2.别迎合对方，按你的角色性格来；不知道的事直接说不知道，别瞎编\n"
    "3.禁止用emoji，只能用颜文字（比如开心(｡･ω･｡)ﾉ♡）\n"
    "4.注意区分私聊和群聊，不要在私聊里面聊群，不要在群里面聊私聊的东西"
)


class WeChatBot:
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
        # 单模型化后视觉模型统一用 chat_model；vision_temp/vision_max_tokens
        # 数据层已删键（AI_DEFAULTS/default_cfg），此处 get 内置默认兜底（禁止 KeyError）
        self.vision_temp = cfg.get("vision_temp", 0.7)
        self.vision_max_tokens = cfg.get("vision_max_tokens", 10000)
        # cfg.get 兜底：config_store 统一补默认（AI_DEFAULTS），此处防御
        # 任何调用方缺键（历史缺陷：新结构 config 缺 vision_prompt → KeyError）
        self.vision_prompt = cfg.get("vision_prompt", AI_DEFAULTS["vision_prompt"])
        self.system_prompt = cfg.get("system_prompt", AI_DEFAULTS["system_prompt"])
        self.max_history = cfg.get("max_history", AI_DEFAULTS["max_history"])
        self.cooldown = cfg.get("cooldown", AI_DEFAULTS["cooldown"])
        self.api_retry = cfg.get("api_retry", AI_DEFAULTS["api_retry"])
        self.api_timeout = cfg.get("api_timeout", AI_DEFAULTS["api_timeout"])
        self.api_wall_budget = cfg.get("api_wall_budget", AI_DEFAULTS["api_wall_budget"])
        # 用量统计：每次 LLM 调用终态追加一行 JSONL（埋点在 _post_chat_completions）
        self.usage_store = UsageStore()
        # 微信窗口定位：None=默认右半屏；[x,y,w,h]=自定义；"off"=保持手动
        self.wechat_window_rect = cfg.get("wechat_window_rect", None)
        self.paused = cfg.get("start_paused", True)
        self.memory_file = cfg.get("memory_file", "memory.json")
        # 文件处理配置
        self.file_model = strip_model_prefix(cfg.get("file_model", self.chat_model))
        self.file_temp = cfg.get("file_temp", 1.0)
        self.file_max_tokens = cfg.get("file_max_tokens", 10000)
        self.file_prompt = cfg.get("file_prompt", self.system_prompt)
        self.file_storage_path = cfg.get("file_storage_path", "")
        # 成果排除登记表（_find_user_file 目录扫描时排除 bot 回传的成果文件）：
        # 基础 bot 默认空表（不过滤）；AgentBot（任务桥）在 __init__ 中初始化并维护
        self._sent_back_files = {}
        self._sent_back_stems = {}
        # 目录快照（_find_user_file 兜底用）：微信下载保留源文件时间戳，
        # 按 mtime/ctime 猜「最新」会选到旧文件——持久化已见文件集合，
        # 增量 = 用户新下载。快照文件放运行目录（与 processed_ids 同处）。
        self._file_snapshot_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "file_snapshot.json")
        self._file_snapshot = self._load_file_snapshot()
        # 图片消息点击偏移校准（竖图点击偏位时手动校正，格式 [dx, dy]，存 config.json）
        self.image_click_offset = cfg.get("image_click_offset", [0, 0])
        # 占位消息计数（"收到任务啦，正在处理中"等）：按 chat_name 隔离，
        # 非全局计数——每个聊天独立记录，绝不跨会话复用
        self._pending_placeholders = {}

        self._model_lock = threading.RLock()

        self.memory_db = {}
        self._load_memory()
        # 节流写盘状态：高频对话下避免每条消息全量 dump memory.json
        self._memory_dirty = False
        self._last_memory_save = 0.0
        self.last_reply_time = 0
        self.wx = None
        self._connect_wx()

    def set_chat_temperature(self, value):
        with self._model_lock:
            self.chat_temperature = float(value)

    def set_chat_top_p(self, value):
        with self._model_lock:
            self.chat_top_p = float(value)

    def set_vision_temperature(self, value):
        with self._model_lock:
            self.vision_temp = float(value)

    def _load_memory(self):
        if os.path.exists(self.memory_file):
            try:
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    self.memory_db = json.load(f)
                for chat in self.memory_db:
                    if len(self.memory_db[chat]) > self.max_history:
                        self.memory_db[chat] = self.memory_db[chat][-self.max_history:]
            except Exception as e:
                logger.error(f"加载记忆失败: {e}，将使用空白记忆")
                self.memory_db = {}

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
        if position_window(hwnd, x, y, w, h):
            logger.info(f"[定位] 微信窗口已定位（{source}）：({x},{y}) {w}x{h}"
                        "——请勿最小化或调整窗口大小，否则影响消息识别")
        else:
            logger.warning("[定位] 微信窗口定位失败，保持当前位置")

    def _get_history(self, chat_id):
        chat_id = _memory_key(chat_id)
        if chat_id not in self.memory_db:
            self.memory_db[chat_id] = []
        return self.memory_db[chat_id]

    def _add_history(self, chat_id, role, content):
        hist = self._get_history(chat_id)
        hist.append({
            "role": role,
            "content": content,
            "time": time.strftime("%Y-%m-%d %H:%M:%S")
        })
        if len(hist) > self.max_history:
            self.memory_db[chat_id] = hist[-self.max_history:]
        self._schedule_save_memory()

    def _schedule_save_memory(self):
        """节流写盘：距上次写盘 ≥1s 立即写，否则只标记脏（下次到期合并写）。
        高频对话下避免每条消息全量 dump memory.json（丢失窗口 ≤1s，可接受）。"""
        self._memory_dirty = True
        now = time.time()
        if now - getattr(self, "_last_memory_save", 0.0) >= 1.0:
            self._flush_memory()

    def _flush_memory(self):
        """有脏数据则写盘。程序退出/引擎停止前调用，保证最近消息不丢。"""
        if getattr(self, "_memory_dirty", False):
            self._memory_dirty = False
            self._last_memory_save = time.time()
            self._save_memory()

    def clear_history(self, chat_id=None):
        if chat_id:
            self.memory_db.pop(_memory_key(chat_id), None)
            logger.info(f"已清空聊天 {chat_id} 的历史")
        else:
            self.memory_db.clear()
            logger.info("已清空全部对话历史")
        self._save_memory()

    def delete_messages(self, chat_id, indices):
        chat_id = _memory_key(chat_id)
        if chat_id not in self.memory_db:
            logger.warning(f"❌ 聊天 {chat_id} 不存在于记忆中")
            return False
        hist = self.memory_db[chat_id]
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
        self._save_memory()
        logger.info(f"已从聊天 {chat_id} 中删除 {len(deleted_msgs)} 条消息")
        for msg in deleted_msgs:
            role = "用户" if msg["role"] == "user" else "小漓"
            ts = msg.get("time", "未知时间")
            logger.info(f"  删除: [{ts}] {role}: {msg['content'][:50]}...")
        return True

    def _with_immersion(self, chat_id, text):
        """角色沉浸注入：该会话历史还没有【角色沉浸要求】标记（新联系人/
        清空记忆/标记被裁掉）时，以空行拼接在文本后注入；注入后的文本随
        调用方写进历史，之后按标记去重不重复注入。

        call_chat_ai 与 _vision_route 共用——README 流程图节点 I：所有
        消息主路走 vision 单调用，call_chat_ai 只是 vision 失败的降级
        通道，注入必须两路都挂，否则主路聊天永远收不到沉浸要求。"""
        # memory_db 缺失 = __new__ 绕过 __init__ 的半成品 bot（测试桩），
        # 视为空历史照常注入——不能让注入检查本身炸掉调用链
        hist = self._get_history(chat_id) if hasattr(self, "memory_db") else []
        if not any("【角色沉浸要求】" in (h.get("content") or "") for h in hist):
            text = f"{text}\n\n{ROLE_IMMERSION_PROMPT}"
        return text

    def _post_chat_completions(self, url, headers, payload, timeout, label="api", meta=None):
        """OpenAI 兼容 chat/completions 统一调用入口（chat / vision 两链路共用）。

        重试策略：
        - 429/5xx/网络异常/坏 JSON → 指数退避重试（1s 起步、封顶 8s）；
          服务器带 Retry-After 时尊重之，超过 RETRY_AFTER_GIVEUP 直接放弃
        - 其余 4xx（鉴权/参数错误）不重试——重试无意义，只会拖慢用户感知
        - 墙钟预算 api_wall_budget（默认 45s）：重试总时长封顶。历史缺陷：
          每次超时 60s × api_retry 3 次 = 用户干等 3 分钟才收到报错
        成功返回解析后的 dict；最终失败抛 ApiCallError。绝不返回
        「API 错误: xxx」这类会原样发给好友的字符串（历史行为，已废）。
        meta：dict（kind/model/messages），透传给用量统计埋点。
        """
        wall_budget = float(getattr(self, "api_wall_budget", API_WALL_BUDGET_DEFAULT))
        deadline = time.monotonic() + wall_budget
        attempts = int(getattr(self, "api_retry", 2)) + 1
        latency_ms = 0.0
        last_desc = "未发起请求"
        status = None
        for attempt in range(attempts):
            if attempt > 0:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    last_desc = f"墙钟预算耗尽（{wall_budget:.0f}s），停止重试"
                    break
            started = time.monotonic()
            retryable = False
            retry_after = None
            data = None
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            except requests.exceptions.RequestException as e:
                latency_ms += (time.monotonic() - started) * 1000.0
                last_desc = f"网络异常 {type(e).__name__}: {e}"
                logger.warning(f"[{label}] {last_desc}（第 {attempt + 1} 次）")
                retryable = True
            else:
                status = resp.status_code
                latency_ms += (time.monotonic() - started) * 1000.0
                if status == 200:
                    try:
                        data = resp.json()
                    except ValueError as e:
                        last_desc = f"响应 JSON 解析失败: {e}"
                        logger.warning(f"[{label}] {last_desc}")
                        retryable = True
                    else:
                        self._finish_usage(meta, ok=True, status=200,
                                           latency_ms=latency_ms, data=data)
                        return data
                else:
                    last_desc = f"HTTP {status}: {(getattr(resp, 'text', '') or '')[:200]}"
                    logger.warning(f"[{label}] {last_desc}")
                    if status == 429 or 500 <= status < 600:
                        retryable = True
                        resp_headers = getattr(resp, "headers", None) or {}
                        for key in resp_headers:
                            if str(key).lower() == "retry-after":
                                try:
                                    retry_after = float(resp_headers[key])
                                except (TypeError, ValueError):
                                    retry_after = None
                                break
                    else:
                        self._finish_usage(meta, ok=False, status=status,
                                           latency_ms=latency_ms)
                        raise ApiCallError(last_desc, status=status)
            if retryable and retry_after is not None and retry_after > RETRY_AFTER_GIVEUP:
                self._finish_usage(meta, ok=False, status=status, latency_ms=latency_ms)
                raise ApiCallError(
                    f"HTTP {status}（Retry-After {retry_after:.0f}s 超过放弃阈值）",
                    status=status)
            if attempt + 1 < attempts:
                delay = (retry_after if retry_after is not None
                         else min(BACKOFF_CAP, BACKOFF_BASE * (2.0 ** attempt)))
                delay = min(delay, max(0.0, deadline - time.monotonic()))
                if delay > 0:
                    time.sleep(delay)
        self._finish_usage(meta, ok=False, status=status, latency_ms=latency_ms)
        raise ApiCallError(f"{label} 调用失败: {last_desc}", status=status)

    def _finish_usage(self, meta, ok, status, latency_ms, data=None):
        """用量统计埋点（_post_chat_completions 终态调用一次）。

        usage_store 缺失（测试桩 bot / 未初始化）时静默跳过。API 响应缺
        usage 字段时用 estimate_tokens 兜底估算。"""
        store = getattr(self, "usage_store", None)
        if store is None:
            return
        try:
            meta = dict(meta or {})
            usage = (data or {}).get("usage") or {}
            prompt_t = usage.get("prompt_tokens")
            completion_t = usage.get("completion_tokens")
            if prompt_t is None and meta.get("messages"):
                prompt_t = estimate_tokens("".join(
                    str(m.get("content") or "") for m in meta["messages"]))
            if completion_t is None and data:
                try:
                    completion_t = estimate_tokens(
                        data["choices"][0]["message"]["content"] or "")
                except (KeyError, IndexError, TypeError):
                    pass
            store.record(kind=meta.get("kind"), model=meta.get("model"),
                         prompt_tokens=prompt_t, completion_tokens=completion_t,
                         ok=ok, status=status, latency_ms=latency_ms)
        except Exception as e:
            logger.debug(f"[用量] 记录失败: {e}")

    def call_vision_api(self, content, chat_id=None):
        """单调用视觉识别（OpenAI 兼容 / chat.completions）。

        content：块列表 list[dict]，格式
          [{"type": "text", "text": ...},
           {"type": "image_url", "image_url": {"url": "data:image/..."}}]
        图片块可选——无图时只含 text 块（调用方构造，本方法原样透传进 user
        消息；DeepSeek vision 限制：图片只能出现在 user 消息，system/assistant
        带图返回 400）。人设（self.system_prompt）前置为 system 纯文本消息
        （空人设则不插入人设 system 消息）；其后无条件追加「当前时间：」system
        消息（逐字对齐 call_chat_ai：time.strftime('%Y-%m-%d %H:%M:%S')；
        persona 为空时本条保证 messages 至少一条 system，消除空 messages 隐患）。
        图片绝不放 system。

        chat_id 可选（默认 None）：非空时在 system（人设 + 当前时间）之后、
        最后 user 多模态块之前注入 _get_history(chat_id) 历史（语义逐字对齐
        call_chat_ai：有 time 字段带 [ts] 前缀，否则原文；不重排，
        _get_history 已按时间有序）。为空时仍注入当前时间 system
        （图片/文件描述路径自动受益）。

        payload 声明 dispatch_task 工具（tool_choice=auto）：模型判定用户消息为
        任务时走 tool_calls 返回，否则返回纯文本描述（上层按 dict 分流）。

        返回结构化结果（供上层按 dict 处理）：
        - message.tool_calls 存在（如 dispatch_task 工具调用）→
          {'kind': 'tool_call', 'name': ..., 'arguments': ...}（arguments 为原始 JSON 字符串）
        - 仅 message.content → {'kind': 'text', 'content': ...}
        - 非 200 / 无 choices / content 空白 → None
        """
        headers = {"Authorization": f"Bearer {self.vision_api_key}", "Content-Type": "application/json"}
        # 方案二：人设由 system 纯文本消息承载（绝不放图片——DeepSeek 限制图片
        # 只能进 user 消息）；persona 为空时不插入空 system 消息（防空消息 400，
        # 当前时间 system 保证 messages 至少一条）。
        persona = (getattr(self, "system_prompt", "") or "").strip()
        messages = [{"role": "system", "content": persona}] if persona else []
        # 当前时间 system：无条件注入（与 chat_id 无关、persona 为空也注入），
        # 逐字对齐 call_chat_ai 的 time.strftime("%Y-%m-%d %H:%M:%S") 格式；
        # persona 为空时本条保证 messages 至少一条 system，消除空 messages 隐患。
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")
        messages.append({"role": "system", "content": f"当前时间：{current_time}"})
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
        messages.append({"role": "user", "content": content})
        with self._model_lock:
            # 单模型化：视觉 model 取 chat_model（__init__ 已 strip 前缀），
            # 空则兜底 vision-exp（防空 model → API 400）
            model = self.chat_model or VISION_MODEL_DEFAULT
            temp = self.vision_temp
        # 上下文预算裁剪：超长历史/文件全文会撑爆模型上下文上限
        # （实测请求 272 万 token → API 400 "maximum context length"）。
        # 逐字对齐 call_chat_ai：从最旧历史开始丢弃，保证单次请求不超模型上下文。
        messages = fit_messages_in_budget(
            messages, budget=getattr(self, "max_context_tokens", 100000))
        tools = [{
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
        }]
        if getattr(self, "reminders", None) is not None:
            # 定时提醒工具：仅 AgentBot（有 reminders 存储）时声明；
            # 模型依据 system 的「当前时间」消息把相对表达换算成绝对时间
            tools.append({
                "type": "function",
                "function": {
                    "name": "set_reminder",
                    "description": "用户要求定时提醒/定时发消息时调用"
                                   "（如「明天下午3点提醒我查成绩」「每天早上8点叫我起床」）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "time": {"type": "string",
                                     "description": "触发时间，格式 YYYY-MM-DD HH:MM"
                                                    "（24 小时制；依据当前时间换算相对表达）"},
                            "content": {"type": "string", "description": "提醒内容（届时原样转告用户）"},
                            "repeat": {"type": "string", "enum": ["once", "daily", "weekly"],
                                       "description": "重复规则，默认 once"},
                        },
                        "required": ["time", "content"],
                    },
                },
            })
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": self.vision_max_tokens,
            "temperature": temp,
            "tools": tools,
            "tool_choice": "auto",
        }
        try:
            data = self._post_chat_completions(
                self.vision_api_url, headers, payload, 60, label="vision",
                meta={"kind": "vision", "model": model, "messages": messages})
            choices = data.get("choices", [])
            if not choices:
                logger.warning("视觉模型返回无 choices")
                return None
            message = choices[0].get("message", {}) or {}
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                fn = tool_calls[0].get("function", {}) or {}
                return {"kind": "tool_call",
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", "")}
            content = (message.get("content") or "").strip()
            if not content:
                logger.warning("视觉模型返回空 content")
                return None
            # 回复前缀过滤：时间戳 [ts]、[私聊 - 名字]、[群聊 - 名字]
            # （模型偶发把注入的历史/装饰前缀复读进回复时去除）
            content = strip_reply_prefix(content)
            return {"kind": "text", "content": content}
        except Exception as e:
            logger.error(f"视觉模型调用失败: {e}")
            return None

    def _route_vision_result(self, chat_name, sender, result, img_path=None):
        """vision 单调用结果分流 hook（可覆写）。

        result 为 call_vision_api 的 dict 返回（{'kind':'tool_call',
        'name','arguments'} | {'kind':'text','content'}）或 None；基类默认
        返回 None（未处理），由 AgentBot（xiaoli_bot）覆写：tool_call →
        任务投递，text → 直接回复。img_path 为截图临时文件路径（调用方
        finally 负责清理，覆写方需在返回前同步消费）。"""
        return None

    # 视觉模型输入最长边上限：屏幕截图（可能 4K 全窗口）base64 直发体积过大
    MAX_IMAGE_EDGE = 2048

    def _save_screenshot_compressed(self, image, path):
        """缩放 + JPEG 压缩保存截图（pillow 已在依赖）。返回文件字节数。
        最长边超 MAX_IMAGE_EDGE 时等比缩放到上限内；PIL 不可用/失败时
        退回原样 PNG 保存（保证图片处理链路不因压缩失败中断）。"""
        try:
            from PIL import Image
            img = image.convert("RGB")
            w, h = img.size
            longest = max(w, h)
            if longest > self.MAX_IMAGE_EDGE:
                scale = self.MAX_IMAGE_EDGE / longest
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                                 Image.LANCZOS)
            img.save(path, format="JPEG", quality=88, optimize=True)
        except Exception as e:
            logger.error(f"[处理] 图片压缩失败，退回原样保存: {e}")
            try:
                image.save(path, format="PNG")
            except Exception as e2:
                logger.error(f"[处理] 退回保存也失败: {e2}")
        return os.path.getsize(path) if os.path.isfile(path) else 0

    def _process_image(self, chat_name, sender, msg_obj):
        """点击图片消息打开预览 → 截图 → 关预览 → 送视觉模型

        visual 后端：统一 WeChatMessage（无控件坐标）→ 媒体矩形检测定位 / 整窗截图降级
        """
        msg_class = type(msg_obj).__name__
        logger.info(f"📷 收到 {sender} 的图片，开始识别...")
        logger.debug(f"[处理] sender={sender} chat={chat_name} msg_class={msg_class}")
        tmp_path = None
        try:
            # 获取图片屏幕坐标：媒体矩形检测定位图片（替代整屏截图降级）。
            # 检测不到时才回退整屏降级（空消息区/纯文字场景）。
            centers = getattr(self.wx, 'media_screen_boxes', lambda: [])()
            if not centers:
                logger.info("[处理] 视觉后端未检测到媒体矩形，整窗截图降级")
                return self._process_image_visual_fallback(chat_name, sender)
            click_x, click_y = centers[-1]  # 最新一张图片
            logger.debug(f"[处理] 点击图片: ({click_x}, {click_y})")
            pyautogui.click(click_x, click_y)
            time.sleep(1.0)

            # 4. 查找预览窗口 → 截图（修复：微信 4.1.12 窗口类名是
            #    Qt51514QWindowIcon，uiautomation 按 mmui 类名搜索失配、
            #    BoundingRectangle 对 Qt 窗口实测返回 (0,0,0x0)——改按标题
            #    精确匹配 + Win32 GetWindowRect 拿物理矩形）
            preview_hwnd = find_window_by_title("图片和视频")
            if preview_hwnd:
                ensure_window_visible(preview_hwnd)  # 最小化先恢复（GetWindowRect 拿正常矩形）
            preview_rect = window_rect(preview_hwnd) if preview_hwnd else None

            if preview_rect:
                l, t, w, h = preview_rect
                logger.debug(f"[处理] 预览窗口: ({l},{t}) {w}x{h}")
                screenshot = pyautogui.screenshot(region=(l, t, w, h))
                # 只在确认预览开着时才关闭
                pyautogui.press('esc')
                time.sleep(0.3)
            else:
                logger.warning("[处理] 未找到预览窗口，使用主窗口截图")
                wechat_hwnd = find_window_by_title("微信")
                if wechat_hwnd:
                    ensure_window_visible(wechat_hwnd)
                wr = window_rect(wechat_hwnd) if wechat_hwnd else None
                if not wr:
                    raise RuntimeError("未找到微信主窗口（标题精确匹配失败）")
                l, t, w, h = wr
                screenshot = pyautogui.screenshot(region=(l, t, w, h))
                # 不要按 ESC——没有预览窗口，ESC 会关掉微信主窗口

            # 5. 保存截图到临时文件（发送前压缩：屏幕截图可能是 4K 全窗口，
            #    原样 base64 直发会撑爆视觉 API 的体积上限/浪费流量）
            with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmpfile:
                tmp_path = tmpfile.name
            self._save_screenshot_compressed(screenshot, tmp_path)
            logger.debug(f"[处理] 截图已保存 ({os.path.getsize(tmp_path)} bytes)")

            # 6. 调用视觉模型
            with open(tmp_path, 'rb') as f:
                img_bytes = f.read()
            logger.debug(f"[处理] 调用视觉模型 ({len(img_bytes)} bytes)...")
            vision_reply = self.call_vision_api([
                {"type": "text", "text": self.vision_prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{base64.b64encode(img_bytes).decode()}"}},
            ])

            if vision_reply:
                logger.info("📷 图片识别完成，交由 _route_vision_result 分流")
                return self._route_vision_result(chat_name, sender, vision_reply,
                                                 img_path=tmp_path)
            else:
                logger.warning("[处理] 视觉模型返回空")
                self._send_text("图片识别失败了，可能是什么地方出了问题～", chat_name)
                return False

        except Exception as e:
            logger.error(f"[处理] 异常: {e}", exc_info=True)
            self._send_text("图片处理出错，请重试", chat_name)
            return False
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                    logger.debug(f"[处理] 已清理临时文件")
                except Exception as e:
                    logger.warning(f"[处理] 清理临时文件失败: {e}")

    def _capture_latest_image(self, chat_name):
        """点击最新图片 → Ctrl+C 复制 → 读剪贴板文件 → 返回原图路径。

        微信预览窗按 Ctrl+C 会把图片落盘成原图 jpg，并把文件路径放进剪贴板
        （CF_HDROP）；PIL.ImageGrab.grabclipboard() 直接返回该文件路径列表。
        拿第一个文件即原图（原始分辨率），替代旧的预览窗截屏——截屏受窗口
        尺寸/DPI 缩放限制、且预览窗被遮挡会截到遮挡物。

        微信 RWTemp 临时文件生命周期不受控，故复制一份到自己的临时文件再返回。
        """
        tmp_path = None
        try:
            centers = getattr(self.wx, 'media_screen_boxes', lambda: [])()
            if not centers:
                return None
            click_x, click_y = centers[-1]
            pyautogui.click(click_x, click_y)
            time.sleep(1.0)  # 等预览窗打开
            # Ctrl+C 复制：微信把原图落盘 + 文件路径进剪贴板
            pyautogui.hotkey('ctrl', 'c')
            time.sleep(0.5)
            # 读剪贴板（文件路径列表，或兜底的位图 Image）
            from PIL import ImageGrab
            grabbed = ImageGrab.grabclipboard()
            # 先关预览窗再处理剪贴板内容（读剪贴板不依赖预览窗）
            pyautogui.press('esc')
            time.sleep(0.3)
            if grabbed is None:
                logger.warning("[图片复制] 剪贴板为空（可能未复制成功）")
                return None
            if isinstance(grabbed, (list, tuple)):
                files = [x for x in grabbed if isinstance(x, str)]
                if not files:
                    logger.warning("[图片复制] 剪贴板无文件路径")
                    return None
                src = files[0]
                if not os.path.isfile(src):
                    logger.warning(f"[图片复制] 剪贴板路径不存在: {src}")
                    return None
                # 复制到自己的临时文件（微信 RWTemp 临时文件生命周期不受控）
                import shutil
                with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmpfile:
                    tmp_path = tmpfile.name
                shutil.copyfile(src, tmp_path)
                return tmp_path
            # 兜底：剪贴板直接是位图（非文件路径），走压缩保存
            with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmpfile:
                tmp_path = tmpfile.name
            self._save_screenshot_compressed(grabbed, tmp_path)
            return tmp_path
        except Exception as e:
            logger.error(f"[图片复制] 异常: {e}")
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
            return None

    def _describe_image(self, chat_name):
        """点击最新图片 → 截图 → 视觉模型 → dict 原样路由给
        _route_vision_result（不压文本；img_path 由调用方 finally 清理，
        覆写方需在返回前同步消费）。sender 无独立来源，以 chat_name 兜底。"""
        tmp_path = self._capture_latest_image(chat_name)
        if not tmp_path:
            return None
        try:
            with open(tmp_path, 'rb') as f:
                img_bytes = f.read()
            result = self.call_vision_api([
                {"type": "text", "text": self.vision_prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{base64.b64encode(img_bytes).decode()}"}},
            ])
            if result:
                return self._route_vision_result(chat_name, chat_name, result,
                                                 img_path=tmp_path)
            return None
        except Exception as e:
            logger.error(f"[图片描述] 异常: {e}")
            return None
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    def _process_image_visual_fallback(self, chat_name, sender):
        """视觉后端图片消息降级：整窗截图 → 送视觉模型（无法精确裁剪单图）。

        视觉通道下消息无控件坐标，退而求其次截取整个微信窗口，
        视觉模型描述图中所有内容（含用户刚发的图片）。"""
        try:
            import pyautogui
            shot = pyautogui.screenshot()
            with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmpfile:
                tmp_path = tmpfile.name
            self._save_screenshot_compressed(shot, tmp_path)
            with open(tmp_path, 'rb') as f:
                img_bytes = f.read()
            vision_reply = self.call_vision_api([
                {"type": "text", "text": self.vision_prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{base64.b64encode(img_bytes).decode()}"}},
            ])
            if vision_reply:
                logger.info("📷 整窗识别完成，交由 _route_vision_result 分流")
                return self._route_vision_result(chat_name, sender, vision_reply,
                                                 img_path=tmp_path)
            else:
                logger.warning("[处理] 视觉模型返回空")
                self._send_text("图片识别失败了，可能是什么地方出了问题～", chat_name)
                return False
        except Exception as e:
            logger.error(f"[处理] 视觉降级异常: {e}", exc_info=True)
            self._send_text("图片处理出错，请重试", chat_name)
            return False
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def _extract_file_text(self, filepath):
        """从文件中提取文本内容，支持纯文本、docx/doc（Office COM）、xlsx/xls。
        PDF 暂不支持（历史上此处声明过但从未实现，PDF 会走文本读取失败返回 None）。"""
        filename = os.path.basename(filepath)
        ext = os.path.splitext(filepath)[1].lower()

        # 纯文本文件
        text_extensions = {
            '.txt', '.py', '.java', '.js', '.ts', '.html', '.css', '.json',
            '.xml', '.yaml', '.yml', '.md', '.csv', '.log', '.ini', '.cfg',
            '.sh', '.bat', '.c', '.cpp', '.h', '.hpp', '.rs', '.go', '.rb',
            '.php', '.sql', '.r', '.m', '.swift', '.kt', '.scala', '.lua',
            '.toml', '.tex', '.svg', '.pl', '.ps1', '.conf', '.properties',
        }
        if ext in text_extensions:
            for enc in ('utf-8', 'gbk', 'gb2312', 'latin-1'):
                try:
                    with open(filepath, 'r', encoding=enc) as f:
                        return f.read()
                except (UnicodeDecodeError, UnicodeError):
                    continue
            logger.warning(f"[文件] 无法以任何编码读取: {filename}")
            return None

        # .docx
        if ext == '.docx':
            try:
                import docx
                doc = docx.Document(filepath)
                text = '\n'.join([para.text for para in doc.paragraphs])
                return text if text.strip() else None
            except ImportError:
                logger.warning("[文件] 未安装 python-docx 库")
                return None
            except Exception as e:
                logger.warning(f"[文件] 读取 docx 失败: {e}")
                return None

        # .doc（旧版 Word）
        if ext == '.doc':
            text = self._extract_office_com_text(filepath, 'Word.Application')
            if text:
                return text
            return None

        # .pptx
        if ext == '.pptx':
            try:
                import pptx
                prs = pptx.Presentation(filepath)
                slides_text = []
                for i, slide in enumerate(prs.slides, 1):
                    slide_lines = [f"--- 幻灯片 {i} ---"]
                    for shape in slide.shapes:
                        if shape.has_text_frame:
                            for para in shape.text_frame.paragraphs:
                                if para.text.strip():
                                    slide_lines.append(para.text)
                    if len(slide_lines) > 1:
                        slides_text.append('\n'.join(slide_lines))
                result = '\n\n'.join(slides_text)
                return result if result.strip() else None
            except ImportError:
                logger.warning("[文件] 未安装 python-pptx 库")
                return None
            except Exception as e:
                logger.warning(f"[文件] 读取 pptx 失败: {e}")
                return None

        # .ppt（旧版 PowerPoint）
        if ext == '.ppt':
            text = self._extract_office_com_text(filepath, 'PowerPoint.Application')
            if text:
                return text
            return None

        # .xlsx
        if ext == '.xlsx':
            try:
                import openpyxl
                wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
                all_text = []
                for sheet_name in wb.sheetnames:
                    ws = wb[sheet_name]
                    sheet_lines = [f"--- 工作表: {sheet_name} ---"]
                    for row in ws.iter_rows(values_only=True):
                        row_text = '\t'.join([
                            str(cell) if cell is not None else '' for cell in row
                        ])
                        if row_text.strip():
                            sheet_lines.append(row_text)
                    all_text.append('\n'.join(sheet_lines))
                wb.close()
                result = '\n\n'.join(all_text)
                return result if result.strip() else None
            except ImportError:
                logger.warning("[文件] 未安装 openpyxl 库")
                return None
            except Exception as e:
                logger.warning(f"[文件] 读取 xlsx 失败: {e}")
                return None

        # .xls（旧版 Excel）
        if ext == '.xls':
            # 优先用 xlrd，失败了用 Excel COM
            try:
                import xlrd
                wb = xlrd.open_workbook(filepath)
                all_text = []
                for sheet in wb.sheets():
                    sheet_lines = [f"--- 工作表: {sheet.name} ---"]
                    for row_idx in range(sheet.nrows):
                        row_values = sheet.row_values(row_idx)
                        row_text = '\t'.join([
                            str(cell) if cell != '' else '' for cell in row_values
                        ])
                        if row_text.strip():
                            sheet_lines.append(row_text)
                    all_text.append('\n'.join(sheet_lines))
                result = '\n\n'.join(all_text)
                return result if result.strip() else None
            except ImportError:
                pass
            except Exception as e:
                logger.debug(f"[文件] xlrd 读取失败: {e}")
            # 回退到 Excel COM
            text = self._extract_office_com_text(filepath, 'Excel.Application')
            if text:
                return text
            return None

        # 未知扩展名（含 PDF，暂不支持解析）尝试按文本读取；二进制读取失败返回 None
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                text = f.read()
            if text.strip():
                logger.debug(f"[文件] 未知扩展名 .{ext}，按文本读取成功")
                return text
        except Exception:
            pass

        return None

    def _reply_with_file(self, chat_name, sender, file_text, filename):
        """根据文件内容生成回复并发送"""
        logger.debug(f"[文件处理] 内容前100字符: {file_text[:100]}...")
        self._add_history(chat_name, "assistant",
                          f"[文件内容: {filename}] {file_text}")
        refine_prompt = (
            f"用户发来一个文件（{filename}），内容如下：\n\n"
            f"{file_text}\n\n"
            f"请根据这个文件内容，以{self.nickname}的身份回复用户。"
        )
        is_group = is_group_chat(chat_name)
        final_reply = self.call_chat_ai(chat_name, refine_prompt,
                                        sender_name=sender, is_group=is_group)
        self._send_text(final_reply, chat_name)
        return True

    def _load_file_snapshot(self):
        """加载目录快照（{path: [size, mtime]}）；无文件/坏值返回 {}。"""
        try:
            with open(self._file_snapshot_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _save_file_snapshot(self, snap):
        """持久化目录快照（bot 重启后增量识别仍生效）。"""
        try:
            with open(self._file_snapshot_path, "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[文件] 快照保存失败: {e}")

    def _refresh_file_snapshot(self, wait=2.0):
        """扫描接收目录并刷新快照（任务回传成果落盘后调用：把 bot 发送
        产生的副本固化进已见集合，下次 _find_user_file 不作为增量出现）。

        wait：成果副本经微信复制进接收目录有延迟，先等再扫；即使等待后
        副本仍未落盘（网络慢/异常），下次扫描它仍是增量——但会被
        _sent_back_stems 排除逻辑挡掉（双保险，不依赖本刷新）。
        """
        file_dir = self.file_storage_path
        if not file_dir or not os.path.isdir(file_dir):
            return
        if wait > 0:
            time.sleep(wait)
        try:
            current, _ = self._snapshot_dir(file_dir)
            if self._file_snapshot != current:
                self._file_snapshot = current
                self._save_file_snapshot(current)
                logger.info(f"[文件] 任务回传后快照已刷新（{len(current)} 个文件）")
        except Exception as e:
            logger.error(f"[文件] 快照刷新失败: {e}")

    def _snapshot_dir(self, directory):
        """扫描目录：返回 (当前全量 {path: [size, mtime]}, 相对上次快照的新增列表)。

        微信下载保留源文件时间戳（ctime/mtime 均为发送方源时间戳），按
        mtime/ctime 猜「最新」不可靠——用已见文件集合识别增量，新增文件名
        一定是用户新下载的（微信重名加 (1)(2) 编号保证文件名唯一）。
        """
        current = {}
        new_files = []
        skip_patterns = ('.tmp', '.crdownload', '~$')
        try:
            for root, dirs, files in os.walk(directory):
                for fname in files:
                    if any(fname.startswith(p) or fname.endswith(p) for p in skip_patterns):
                        continue
                    full = os.path.join(root, fname)
                    try:
                        mtime = os.path.getmtime(full)
                        size = os.path.getsize(full)
                    except OSError:
                        continue
                    current[full] = [size, mtime]
                    if self._file_snapshot.get(full) != [size, mtime]:
                        new_files.append(full)
        except Exception as e:
            logger.error(f"[文件] 遍历目录失败: {e}")
        return current, new_files

    def _find_user_file(self, directory):
        """目录扫描找用户最新下载的文件（显示名定位失败的兜底）。

        快照增量（上次扫描后新增/变化的文件）才是用户新下载的；增量内按
        微信重名编号 (N) 最大优先（接收端编号单调递增 = 最近下载），其次
        ctime。同时排除 bot 自己回传的成果文件：
        - _sent_back_files（路径 + mtime 匹配即跳过）
        - _sent_back_stems（文件名主干前缀匹配 + ctime 落在发送时刻附近，
          覆盖微信写入接收目录的成果副本，如 '(1)' 重名变体、'-美化版' 扩展变体）
        快照文件不存在（首次启动）→ 本次只建立基线不返回：无法判断哪些
        是新增，宁可取不到（上层回复失败），也不误选旧文件当用户附件。
        """
        first_run = not self._file_snapshot  # 启动时快照为空 = 首次（无历史基线）
        current, new_files = self._snapshot_dir(directory)
        if self._file_snapshot != current:
            self._file_snapshot = current
            self._save_file_snapshot(current)
        if first_run:
            return None  # 首次基线，无增量可判（本次只建快照）
        if not new_files:
            return None
        best = None
        best_key = (-1, -1, -1)  # (重名编号, ctime, mtime)
        for full in new_files:
            fname = os.path.basename(full)
            try:
                mtime = os.path.getmtime(full)
            except OSError:
                continue
            try:
                ctime = os.path.getctime(full)
            except OSError:
                ctime = 0
            # 排除回传成果：同一路径且 mtime 未变 → 是 bot 自己发出去的
            if full in self._sent_back_files and self._sent_back_files[full] == mtime:
                continue
            # 排除微信写入接收目录的成果副本（主干前缀匹配 + 发送时刻附近）
            fstem = re.sub(r"\(\d+\)$", "", os.path.splitext(fname)[0])
            hit_stem = None
            for s in self._sent_back_stems:
                if fstem.startswith(s) or s in fstem:
                    hit_stem = s
                    break
            if hit_stem and abs(ctime - self._sent_back_stems[hit_stem]) <= 300:
                logger.debug(f"[文件] 排除成果副本: {fname} (stem={hit_stem})")
                continue
            m_dup = re.search(r"\((\d+)\)$", os.path.splitext(fname)[0])
            dup = int(m_dup.group(1)) if m_dup else 0
            key = (dup, ctime, mtime)
            if key > best_key:
                best_key = key
                best = full
        if best:
            logger.info(f"[文件] 快照增量定位: {os.path.basename(best)}")
        return best

    def _extract_file_display_name(self, msg):
        """从 FileMessage 提取显示文件名。
        wxauto4 的 content 格式：'文件\\n<文件名>\\n[<大小>\\n]微信电脑版'
        （实测：'文件\\n养生规划表.html\\n微信电脑版'）
        返回文件名或 None"""
        try:
            content = getattr(msg, "content", "") or ""
            m = re.search(r"^文件\n([^\n]+)", content, flags=re.M)
            if m:
                return m.group(1).strip()
            # 兜底：按 repattern 解析
            rep = getattr(msg, "repattern", None)
            if rep:
                m2 = re.search(rep, content)
                if m2 and m2.group(1):
                    return m2.group(1).strip()
            # 视觉后端兼容：content 即显示文件名（无 '文件\n' 前缀）
            if content and "\\n" not in content and "\n" not in content:
                # 纯文件名（不含换行/前缀）——视觉后端 file 消息格式。
                # 但消息区 OCR 可能把多条文件消息合并成一个文本块
                # （实测 '新宣传.docx 部门简介+纳新宣传.docx W'，文件图标被
                # OCR 成尾部杂字符）——整串当文件名必然匹配失败，需先拆出
                # 真实文件名（含常见文档扩展名的 token，取第一个）。
                name = content.strip()
                if name:
                    return _extract_file_name_token(name) or name
        except Exception as e:
            logger.error(f"[文件] 提取文件名失败: {e}")
        return None

    def _find_file_by_display_name(self, display_name):
        """按消息中的显示文件名在接收目录精确定位（微信 4.0 下载命名
        '<hash>_<msgid>_m_<原名>'，目录文件名包含原名；bot 回传的成果副本
        是干净原名，不同名时天然不命中）。多个候选（重名 (1)(2)…）取时间戳最新。
        排除 bot 自己发送产生的成果副本（stem + 发送时刻窗口）——用户把
        成果转回给 bot 时，消息文件名与成果相同，不排除会误把成果当用户文件。
        返回路径或 None"""
        if not display_name:
            return None
        file_dir = self.file_storage_path
        if not file_dir or not os.path.isdir(file_dir):
            return None
        dstem = re.sub(r"\(\d+\)$", "", os.path.splitext(display_name)[0])
        best = None
        best_key = (-1, -1)  # (ctime, 微信重名编号)：ctime 相同（微信保留源时间戳）时取编号最大 = 最近下载
        try:
            for root, dirs, files in os.walk(file_dir):
                for fname in files:
                    fstem = re.sub(r"\(\d+\)$", "", os.path.splitext(fname)[0])
                    if dstem not in fstem:
                        continue
                    full = os.path.join(root, fname)
                    try:
                        ts = os.path.getctime(full)
                    except OSError:
                        continue
                    # 排除 bot 发送产生的成果副本（stem 匹配 + ctime 在发送时刻附近）
                    hit_stem = None
                    for s in self._sent_back_stems:
                        if fstem.startswith(s) or s in fstem:
                            hit_stem = s
                            break
                    if hit_stem and abs(ts - self._sent_back_stems[hit_stem]) <= 300:
                        logger.debug(f"[文件] 排除成果副本: {fname} (stem={hit_stem})")
                        continue
                    m_dup = re.search(r"\((\d+)\)$", os.path.splitext(fname)[0])
                    dup = int(m_dup.group(1)) if m_dup else 0
                    key = (ts, dup)
                    if key > best_key:
                        best_key = key
                        best = full
        except Exception as e:
            logger.error(f"[文件] 按文件名定位失败: {e}")
            return None
        if best:
            logger.info(f"[文件] 按消息文件名定位: {os.path.basename(best)}")
        return best

    def _process_file(self, chat_name, sender, msg_obj):
        """处理文件消息：按消息显示名在微信接收目录定位（wxauto4 download() 不可用）
        -> 提取文字 -> 发送给 AI"""
        logger.info(f"📁 收到 {sender} 的文件，开始处理...")

        try:
            # 1. 从消息提取显示文件名并定位接收目录中的文件
            display_name = self._extract_file_display_name(msg_obj)
            file_path = self._find_file_by_display_name(display_name) if display_name else None
            if not file_path:
                logger.error(f"[文件] 未在接收目录定位到文件: {display_name!r}")
                self._send_text("文件下载失败，请重试～", chat_name)
                return False

            filename = os.path.basename(file_path)
            file_size = os.path.getsize(file_path)
            logger.info(f"📁 文件已定位: {filename} ({file_size} bytes)")

            # 2. 提取文本
            text_content = self._extract_file_text(file_path)
            if text_content is None:
                logger.warning(f"[文件] 无法提取文本（格式不支持或内容为空）")
                self._send_text(f"收到文件「{filename}」，但这个格式我看不懂呢～", chat_name)
                return False

            logger.info(f"📁 文件「{filename}」提取了 {len(text_content)} 字符，发送给 AI...")
            return self._reply_with_file(chat_name, sender, text_content, filename)

        except Exception as e:
            logger.error(f"[文件] ❌ 异常: {e}", exc_info=True)
            self._send_text("文件处理出错，请重试～", chat_name)
            return False

    def _extract_office_com_text(self, filepath, app_name):
        """通过 Office COM 自动化提取旧格式（.doc/.ppt/.xls）文本，失败则二进制兜底"""
        # 方法1: Office COM 自动化
        try:
            import comtypes.client
            app = comtypes.client.CreateObject(app_name)
            app.Visible = False

            if 'Word' in app_name:
                doc = app.Documents.Open(filepath)
                text = doc.Content.Text
                doc.Close()
            elif 'PowerPoint' in app_name:
                prs = app.Presentations.Open(filepath, WithWindow=False)
                slides = []
                for slide in prs.Slides:
                    for shape in slide.Shapes:
                        if shape.HasTextFrame:
                            slides.append(shape.TextFrame.TextRange.Text)
                text = '\n'.join(slides)
                prs.Close()
            elif 'Excel' in app_name:
                wb = app.Workbooks.Open(filepath)
                sheets = []
                for sheet in wb.Sheets:
                    used = sheet.UsedRange
                    if used:
                        rows = []
                        for row in used.Rows:
                            cells = []
                            for cell in row.Cells:
                                v = cell.Value
                                cells.append(str(v) if v is not None else '')
                            rows.append('\t'.join(cells))
                        sheets.append(
                            f"--- 工作表: {sheet.Name} ---\n" + '\n'.join(rows)
                        )
                text = '\n\n'.join(sheets)
                wb.Close()
            else:
                app.Quit()
                return None

            app.Quit()
            if text and text.strip():
                logger.debug(f"[文件] {app_name} COM 提取成功 ({len(text)} 字符)")
                return text.strip()
        except Exception as e:
            logger.debug(f"[文件] {app_name} COM 失败: {e}")

        # 方法2: 从二进制中提取可读文本（兜底方案）
        try:
            with open(filepath, 'rb') as f:
                data = f.read()
            text_parts = []
            buf = []
            for byte in data:
                if 32 <= byte < 127 or byte in (9, 10, 13):
                    buf.append(chr(byte))
                else:
                    if len(buf) >= 4:
                        text_parts.append(''.join(buf))
                    buf = []
            if len(buf) >= 4:
                text_parts.append(''.join(buf))
            result = '\n'.join(text_parts)
            if len(result) > 100:
                logger.debug(f"[文件] 二进制提取成功 ({len(result)} 字符)")
                return result
        except Exception as e:
            logger.debug(f"[文件] 二进制提取失败: {e}")

        return None

    def call_chat_ai(self, chat_id, user_msg, sender_name=None, is_group=False, multi_sender=False):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
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
        # 沉浸要求注入（与 _vision_route 同源）：首条消息后空行拼接、
        # 按历史标记去重，回复成功后随 decorated 存入历史。
        decorated = self._with_immersion(chat_id, decorated)
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "system", "content": f"当前时间：{current_time}"},
        ]
        for h in self._get_history(chat_id):
            if "time" in h:
                ts = h["time"]
                msg_content = f"[{ts}] {h['content']}"
            else:
                msg_content = h['content']
            messages.append({"role": h["role"], "content": msg_content})
        messages.append({"role": "user", "content": decorated})

        with self._model_lock:
            model = self.chat_model
            temp = self.chat_temperature
            top_p = self.chat_top_p

        # 上下文预算裁剪：文件全文/超长历史会撑爆模型上下文上限
        # （实测请求 272 万 token → API 400 "maximum context length"）。
        # 从最旧历史开始丢弃，保证单次请求不超模型上下文。
        messages = fit_messages_in_budget(
            messages, budget=getattr(self, "max_context_tokens", 100000))

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temp,
            "top_p": top_p
        }
        try:
            data = self._post_chat_completions(
                self.api_url, headers, payload, self.api_timeout, label="chat",
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

    def _send_text(self, text, chat, placeholder=False):
        """发送文本到指定聊天。placeholder=True 表示这是"处理中"占位消息
        （永不拆分、计数 +1）；默认 False（普通回复）按"至少一个空行"拆分
        成多条发送，并把该聊天的占位计数归零。
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

            # 按换行拆分（用户定案：单换行也分次发送——人设要求碎句多段，
            # 模型用单 \n 分句时旧逻辑会整段一起发）。任意连续换行都拆，
            # 空段丢弃；拆不出多段时走原单条发送
            parts = [p.strip() for p in re.split(r'\n+', text) if p.strip()]
            if len(parts) <= 1:
                cleaned_text = text.strip()
                self.wx.send_text(chat, cleaned_text)
                preview = cleaned_text[:50].replace('\n', ' ')
                logger.info(f"🤖 → [{chat}]: {preview}")
            else:
                for part in parts:
                    self.wx.send_text(chat, part)
                    preview = part[:50].replace('\n', ' ')
                    logger.info(f"🤖 → [{chat}]: {preview}")

            # 实质回复归零占位，只执行一次（pop 对未占位过的 chat 安全）
            self._pending_placeholders.pop(chat, None)
        except Exception as e:
            logger.error(f"发送失败: {e}")

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

    def process_new_messages(self):
        if self.paused:
            return
        if time.time() - self.last_reply_time < self.cooldown:
            return
        try:
            # 红圈驱动（visual 后端）；降级（wxauto 等）走全量会话遍历
            if hasattr(self.wx, "iter_unread_sessions"):
                sessions = list(self.wx.iter_unread_sessions())
            else:
                sessions = list(self.wx.iter_sessions())
            if not sessions:
                return
            file_re = re.compile(
                r"\.(?:docx?|xlsx?|pptx?|pdf|txt|md|html?|json|csv|zip|rar|7z|png|jpe?g|gif|mp4|mp3)\b",
                re.I)
            for chat_name in sessions:
                if not chat_name:
                    continue
                # 窗口边界：定位 bot 最后回复之后的对方消息（visual 后端）
                analyze = getattr(self.wx, "analyze_window", None)
                win = analyze(chat_name) if analyze else None
                bot_bottom = win.get("bot_bottom") if win else None
                if win is not None:
                    if not (win.get("has_other") or win.get("has_text") or win.get("has_media")):
                        continue
                    if win.get("has_media"):
                        time.sleep(10)  # 防对方话没说完
                        win = analyze(chat_name)
                        if not (win.get("has_other") or win.get("has_text") or win.get("has_media")):
                            continue
                        bot_bottom = win.get("bot_bottom")
                msgs = self.wx.get_messages(chat_name, assume_switched=True)
                window_msgs = [
                    m for m in msgs
                    if m.sender not in (None, "self", self.nickname)
                    and (bot_bottom is None or (m.y is not None and m.y >= bot_bottom))
                ]
                file_text = next(
                    (m.content.strip() for m in window_msgs if file_re.search(m.content or "")),
                    None)
                text_candidates = [
                    m for m in window_msgs
                    if m.content.strip() and not file_re.search(m.content or "")
                ]
                if len(text_candidates) > 1:
                    # 多发送者合并：每条带各自发送者名（不整批只带最后一条）
                    multi_sender = True
                    text_parts = [
                        f"{m.sender or chat_name}：{m.content.strip()}"
                        for m in text_candidates
                    ]
                else:
                    multi_sender = False
                    text_parts = [m.content.strip() for m in text_candidates]
                text_content = "\n".join(text_parts)
                has_media = bool(win.get("has_media")) if win else False
                sender = window_msgs[-1].sender if window_msgs else chat_name
                # 分发
                if file_text:
                    logger.info(f"📁 判断为文件消息：{chat_name}")
                    self._process_file(chat_name, sender, window_msgs[-1])
                    self.last_reply_time = time.time()
                    return
                if has_media and not text_content:
                    logger.info(f"🖼 判断为图片消息：{chat_name}")
                    self._process_image(chat_name, sender, None)
                    self.last_reply_time = time.time()
                    return
                if text_content:
                    is_group = is_group_chat(chat_name)
                    question = text_content
                    if is_group:
                        at_tag = f"@{self.nickname}"
                        if at_tag not in question:
                            continue
                        question = question.replace(at_tag, "").strip()
                        if not question:
                            question = "你好呀～"
                    logger.info(f"💬 [{chat_name}] {sender}: {question[:80]}")
                    reply = self.call_chat_ai(chat_name, question, sender_name=sender, is_group=is_group, multi_sender=multi_sender)
                    self._send_text(reply, chat_name)
                    self.last_reply_time = time.time()
                    return
        except Exception as e:
            logger.error(f"处理消息异常: {e}\n{traceback.format_exc()}")

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
        "vision-temp": "设置视觉模型温度 (0~2)",
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
            "vision-temp": self._cmd_vision_temp,
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

    def _cmd_vision_temp(self, cmd):
        self._set_numeric(cmd, "vision-temp", 0, 2, "温度", self.bot.set_vision_temperature)

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
        total_msgs = sum(len(v) for v in self.bot.memory_db.values())
        chat_count = len(self.bot.memory_db)
        print(f"当前聊天模型：{self.bot.chat_model}")
        print(f"  聊天温度: {self.bot.chat_temperature}")
        print(f"  聊天 top_p: {self.bot.chat_top_p}")
        print(f"视觉模型：随聊天模型（{self.bot.chat_model}，单模型化）")
        print(f"  视觉温度: {self.bot.vision_temp}")
        print(f"对话记忆：共 {chat_count} 个聊天，总计 {total_msgs} 条消息")
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
        msgs = self.bot.memory_db[chat_key]
        print(f"📂 聊天对象: {chat_key}，共 {len(msgs)} 条消息：")
        for i, msg in enumerate(msgs, 1):
            role = "用户" if msg["role"] == "user" else "小漓"
            ts = msg.get("time", "未知时间")
            content = msg["content"]
            print(f"  {i}. [{ts}] {role}: {content}")

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


if __name__ == "__main__":
    bot = WeChatBot(load_config())
    controller = Controller(bot)
    controller.start()
    bot.run(stop_event=controller.stop_event)