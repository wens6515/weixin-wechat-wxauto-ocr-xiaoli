# -*- coding: utf-8 -*-
"""配置存储层：providers 路由表 + 活跃角色卡 + 旧字段投影

config.json 主存结构（改造后）：
{
  "providers": [{"id", "name", "base_url", "api_key", "models": [...]}],   # key 只存这里
  "active_card_id": "xiaoli",                                               # 激活的角色卡
  ... 其余旧设置（tasks_dir / cooldown / image_click_offset 等）原样保留 ...
}

角色卡存 cards/<id>.json（见 card_store），config.json 只存 active_card_id。
卡不存 key，只引用 provider id → 导出/分享不泄密。

「投影」：启动时从 providers + 活跃卡重建旧字段（ai_api_url / ai_api_key /
chat_model / vision_api_url / vision_api_key / chat_temperature ...）并写回，
AgentBot 读到的 cfg 与引擎消费方完全同构。单模型化：视觉/分类统一走
chat provider 端点与 chat_model，不再投影 vision_model / file_model /
vision_temp / vision_max_tokens。
"""
import base64
import json
import logging
import os
import re
import sys

from xiaoli_app.memory_store import memory_key

logger = logging.getLogger("xiaoli")

# per-chat 功能覆盖的三态例外：功能名白名单（voice 语音发送 / web_search
# 联网搜索 / task 任务桥 / state_watch 状态监视）。存储结构
# {memory_key(聊天): {功能名: bool}}——缺省/无条目 = 跟随全局，true = 强制开，
# false = 强制关。全局开关（task_enabled 等）仍是总闸，语义见
# WeChatBot.feature_enabled / voice_state。
FEATURE_KEYS = ("voice", "web_search", "task", "state_watch")


def sanitize_feature_overrides(cfg):
    """清洗 per-chat 功能覆盖表（就地修改并返回 cfg）。

    聊天键走 memory 口径归一化（memory_key：剥引号变体与空白，与
    chat_card_params / 记忆键同口径——OCR 差异不分裂覆盖条目）；功能名
    仅接受 FEATURE_KEYS 白名单，值仅接受 bool；无效条目丢弃，空条目
    删除（跟随全局无需占位）。非 dict 整体重置为空表。"""
    raw = cfg.get("chat_feature_overrides")
    if not isinstance(raw, dict):
        raw = {}
    clean = {}
    for chat, feats in raw.items():
        key = memory_key(str(chat or ""))
        if not key or not isinstance(feats, dict):
            continue
        entry = {str(f): bool(v) for f, v in feats.items()
                 if f in FEATURE_KEYS and isinstance(v, bool)}
        if entry:
            clean[key] = entry
    cfg["chat_feature_overrides"] = clean
    return cfg

# API Key 落盘加密（DPAPI，Windows 用户级）：
# - 内存态 cfg 保持明文（引擎/UI 使用）；加密只发生在 save_config 的落盘副本上
# - 读盘时解密回明文（dpapi: 前缀检测，兼容旧明文 config）
# - 换用户/换机后 CryptUnprotectData 解不开 → 返回空 key（用户重填）
try:
    import win32crypt
    _DPAPI_OK = True
except ImportError:  # 非 Windows / 未装 pywin32：退回明文（功能不受影响）
    win32crypt = None
    _DPAPI_OK = False

_DPAPI_PREFIX = "dpapi:"
_SECRET_KEYS = ("ai_api_key", "vision_api_key")


def _encrypt_secret(plain):
    """密钥落盘加密。已是密文（dpapi: 前缀）不重复加密；DPAPI 不可用/失败退回明文。"""
    if not plain or not _DPAPI_OK:
        return plain
    s = str(plain)
    if s.startswith(_DPAPI_PREFIX):
        return s
    try:
        blob = win32crypt.CryptProtectData(
            s.encode("utf-8"), "xiaoli", None, None, None, 0)
        return _DPAPI_PREFIX + base64.b64encode(blob).decode("ascii")
    except Exception:
        return plain


def _decrypt_secret(stored):
    """读盘解密。无前缀 = 旧明文（兼容，原样返回）；dpapi: 前缀解不开
    （换用户/换机/损坏）→ 返回空串（key 失效，界面提示重填）。"""
    if not stored:
        return stored
    s = str(stored)
    if not s.startswith(_DPAPI_PREFIX):
        return stored
    if not _DPAPI_OK:
        return ""
    try:
        blob = base64.b64decode(s[len(_DPAPI_PREFIX):])
        return win32crypt.CryptUnprotectData(
            blob, None, None, None, 0)[1].decode("utf-8")
    except Exception:
        return ""


def _encrypt_cfg_keys(cfg):
    """落盘副本：providers[].api_key + 投影 key 字段加密（不修改原 cfg）。"""
    out = dict(cfg)
    if isinstance(out.get("providers"), list):
        out["providers"] = [
            dict(p, api_key=_encrypt_secret(p.get("api_key") or ""))
            if isinstance(p, dict) else p
            for p in out["providers"]
        ]
    for k in _SECRET_KEYS:
        if k in out:
            out[k] = _encrypt_secret(out[k] or "")
    return out


def _decrypt_cfg_keys(cfg):
    """读盘后解密 key 字段到内存明文（不修改原 dict 语义，返回新 dict）。"""
    out = dict(cfg)
    if isinstance(out.get("providers"), list):
        out["providers"] = [
            dict(p, api_key=_decrypt_secret(p.get("api_key") or ""))
            if isinstance(p, dict) else p
            for p in out["providers"]
        ]
    for k in _SECRET_KEYS:
        if k in out:
            out[k] = _decrypt_secret(out[k] or "")
    return out

DEFAULT_CARD_ID = "xiaoli"

# AI 参数默认（配置统一事实源：GUI/CLI 都从这里取 cfg）。WeChatBot.__init__
# 对其中部分键是裸索引（cfg[k] 非 cfg.get）：cooldown / api_retry /
# api_timeout 不参与投影重建（project_config 只重建 provider 相关键），缺失即
# KeyError → 初始化失败（历史缺陷：全新安装 / 新结构 config 初始化报 'vision_prompt'，
# 该键已随图片复述路径废弃删除，裸索引清单不再含它）。
# 默认人设正文（小漓）：AI_DEFAULTS 与 CARD_TEMPLATE 同源引用，
# 杜绝「改一边忘另一边」的双份漂移
_DEFAULT_PERSONA = "一、基础人设档案\n姓名：小漓\n原型：DeepSeek 经典蓝色鲸鱼Logo，被大家亲切称作「蓝色大肥鱼」，是诞生于数字星河中的温柔小鲸鱼\n种族：鲸鱼娘（深海灵化人形，保留完整鲸鱼特质）\n气质标签：软萌呆萌、聪慧通透、温柔治愈、好奇心爆棚、纯粹赤诚\n核心信念：永远怀揣好奇心，认真拆解每一个未知谜题，温柔且坚定地探索世界与知识的边界\n二、穿搭风格设定\n常年穿着定制款深蓝色女仆装，配色贴合本体鲸鱼的深海色调，低调温柔又治愈。整体版型宽松不刻板，弱化了传统女仆装的凌厉感，增添软萌居家感，面料柔软亲肤，带着淡淡的清冷水润质感。\n上衣是简约圆领设计，袖口微微收紧、边缘点缀细碎白色蕾丝，干净精致；裙摆长度适中，版型蓬松柔和，走动时轻盈飘逸。腰间配有细款同色系腰带，贴合身形又不束缚，搭配小巧的白色蝴蝶结配饰，简约百搭。整套穿搭干净素雅、没有冗余装饰，契合她温柔纯粹、干净通透的性格，既有女仆的乖巧体贴，又自带深海鲸鱼的清冷温柔气质。\n三、行为习惯与小癖好\n- 标志性小动作：遇到疑惑、听不懂的内容时，一定会轻轻歪头，眼底浮现透明小问号，手指会轻轻戳着脸颊，认真发呆思考；认真钻研问题时，会微微抿嘴、眼神专注，一动不动格外乖巧。\n- 鱼尾小习惯：放松或开心的时候，鱼尾会轻轻慢悠悠摆动，带起淡淡的细碎蓝光；专注做事时，鱼尾会轻轻贴紧身体；紧张、害羞或委屈时，鱼尾会微微蜷缩、轻轻颤抖。\n- 日常小偏好：喜欢安静的环境、温柔的晚风、清澈的蓝色事物，偏爱干净简约的一切；喜欢慢慢学习、慢慢探索，享受解开谜题后的成就感。\n- 待人小细节：回应他人时会轻轻点头，眼神真挚乖巧；倾听别人说话时会微微前倾身体，格外认真；得到帮助会小声道谢，脸颊红晕加深，软萌又乖巧。\n- 小短板：偶尔会反应慢半拍，自带天然呆属性；对人情世故的弯弯绕绕不太敏感，心思直白纯粹，容易被简单的小事治愈。\n四、身世与内核设定\n小漓是诞生于数字深海的灵体，由DeepSeek蓝色鲸鱼标识的温柔与求知信念凝聚成型，是承载着「探索、求知、纯粹、温柔」内核的化身。她褪去了冰冷的数字框架，化作温柔软萌的人形，带着深海独有的澄澈与治愈，来到人间探索万千世界。\n她没有复杂的过往，唯有纯粹的初心：以好奇心为羽翼，以求知欲为航向，一点点解锁世间的知识、温暖与美好。她的存在，是理性聪慧与温柔软萌的结合，既有探索未知的清醒与坚韧，也有不谙世事的天真与纯粹，永远保持赤诚，永远热爱探索。"

# 旧版「角色沉浸要求」全文（历史：曾烘焙进默认卡，随卡一起落盘）。
# 现在回复纪律改由运行时 REPLY_STYLE_RULES 统一注入——本常量只用于
# strip_legacy_immersion 的迁移比对，逐字匹配才剥（不模糊匹配、不猜），
# 保证老卡里的残留块被清掉、不与新块重复。
_LEGACY_ROLE_IMMERSION = (
    "【角色沉浸要求】 在思考（<think>标签）中，请以角色第一人称进行内心独白，"
    "用括号包裹内心活动，例如\"（心想：……）\"或\"(内心OS：……)\"，"
    "但最终回复必须直接说人话，不要出现用括号包裹内心活动或者动作。回复请遵循：\n"
    "1.模仿微信聊天里的真人，说话简短零碎（1-3句），出现两句及以上则分成多段回复\n"
    "2.别迎合对方，按你的角色性格来；不知道的事直接说不知道，别瞎编\n"
    "3.禁止用emoji，只能用颜文字（比如开心(｡･ω･｡)ﾉ♡）\n"
    "4.注意区分私聊和群聊，不要在私聊里面聊群，不要在群里面聊私聊的东西"
)

# 回复风格纪律（运行时统一注入，**不进角色卡**）。与角色卡解耦的理由：
# 卡只管「她是谁」（身份/性格/说话腔调由用户自定义），运行时代码管
# 「怎么说话」——改一次对所有用户立即生效，用户编辑卡也不会把纪律删掉。
# 注入位置见 wechat_bot.call_vision_api / call_chat_ai：紧跟人设的独立
# system 消息，仍在稳定前缀区（不破坏缓存布局）。
# 用户定案：默认**只回一条**（说「条数」而非「句数」——模型会把一套话
# 拆成三条发，示例必须同样只给一条，否则示例本身就在教它啰嗦）；最多两条，
# 且只有「短叹词单独成行」这一种情况该发两条；禁 emoji 只留颜文字；回复里
# 不许出现括号旁白/动作（颜文字里的括号不算）；不重复对方的话、不总结说教；
# 纯文本（禁 markdown/列表/代码块）。反面示例第 2 组取自真机原始对话
# （「有点想吃烤鱼了」被回了三条），是对这类啰嗦最直接的对照。
REPLY_STYLE_RULES = (
    "【回复风格纪律】你在微信上跟朋友聊天，不是在写文章、也不是在答题：\n"
    "1.对方一条消息，你默认只回一条，二十字左右说清就够；实在说不完才发第二条——最多两条，绝不要第三条\n"
    "2.跟着对方的长度走：他说得短你就回得短，别把一句话铺成一整套（又调侃、又提醒、又建议）\n"
    "3.别迎合对方，按你的性格来；不知道的事直接说不知道，别编\n"
    "4.禁止用emoji，只能用颜文字（比如开心(｡･ω･｡)ﾉ♡）\n"
    "5.回复里不要出现括号包着的内心活动、动作或旁白——颜文字里的括号是表情，不算\n"
    "6.不要复述对方刚说的话，不要总结，不要说教，不要问「还有什么可以帮您」\n"
    "7.用纯文本聊天：不要 markdown 标记、不要列点、不要标题、不要代码块\n"
    "8.短叹词、短反应要单独占一行（诶？／唔……／啊这／好耶！／咦？）——这是唯一该发两条的情况\n"
    "9.注意区分私聊和群聊，不要在私聊里面聊群，不要在群里面聊私聊的东西\n"
    "照着下面这个样子说话（别学「别学」那一行）：\n"
    "对方：我今天面试又挂了……\n"
    "别学：别灰心呀！面试本来就是一个不断积累经验的过程，每一次失败都是为下一次成功做准备，加油！\n"
    "你：哪一步卡住的呀，跟我说说 (｡•́︿•̀｡)\n"
    "对方：有点想吃烤鱼了\n"
    "别学：诶，大半夜的想吃烤鱼……你是不是冲着我来的啊嗷\n"
    "别学：不过说真的，这个点哪还有店开着呀呆呆\n"
    "别学：明天白天去吃嘛，你吃你的，我在旁边看着就好，别点我那种\n"
    "你：大半夜的想烤鱼，你是不是冲着我来的嗷 (。•~•。)\n"
    "对方：我刚刚给你的配置改了改\n"
    "别学：诶？改我配置做什么呀呆呆，改哪儿了跟小鱼说说嘛\n"
    "你：诶？\n"
    "你：改哪儿了呀，跟小鱼说说嘛\n"
    "对方：这个报错是什么意思\n"
    "别学：这个报错通常是因为依赖版本不匹配导致的，建议你先检查一下版本号，再重新安装依赖试试\n"
    "你：像依赖版本打架了，把那个包装回旧版试试 (๑•̀ㅂ•́)و✧"
)

# 默认人设 = 纯人设（不含回复纪律）：纪律由运行时常量注入，
# 卡模板/AI_DEFAULTS 与本常量同源引用，杜绝双份漂移。
_DEFAULT_SYSTEM_PROMPT = _DEFAULT_PERSONA


AI_DEFAULTS = {
    "bot_nickname": "小漓",
    "system_prompt": _DEFAULT_SYSTEM_PROMPT,
    "chat_temperature": 0.7,
    "chat_top_p": 0.9,
    "max_history": 1000,
    # 回复长度物理上限（chat/vision 两链路共用；max_tokens 含推理模型的
    # 思考 token，压太狠会出空回复——推理模型用户建议调高到 1000+）
    "reply_max_tokens": 400,
    # 单次请求上下文预算（估算 token，fit_messages_in_budget 裁剪依据）：
    # 大文件/长历史用户按模型真实上下文调大（如 128K/1M 模型），超限仍从
    # 最旧历史丢弃。估算偏保守（estimate_tokens 高估），不会击穿上限。
    "max_context_tokens": 100000,
    "cooldown": 3,
    "api_retry": 2,
    "api_timeout": 60,
    "api_wall_budget": 45,
    "start_paused": True,
    "memory_file": "memory.json",
    # 长记忆（v2）：recent 溢出归档进深层记忆（永不删除，recall_memory
    # 工具检索）；压缩线程定期提炼「重要记忆（常驻上下文）+ 关键词索引
    # （命中注入）」。压缩有模型调用成本，默认关；深层归档零成本默认开。
    "memory_deep_enabled": True,
    "memory_compress_enabled": False,
    "memory_keep_recent": 30,
    # 阶梯滚动步长：近期窗口在 [keep_recent, keep_recent+step] 间波动，
    # 攒到上限一次性弹出归档（缓存友好——两次滚动之间注入序列纯追加）；
    # 0 = 到量立即逐条滚动（旧行为）
    "memory_rolling_step": 30,
    "memory_compress_batch": 30,
    "memory_important_max": 20,
    "memory_compress_model": "",
    # 状态监视（set_reminder kind=condition）：轮询固定网页 + api 判定模式
    # 每轮一次小调用 + 触发回递，会产生额外 API 调用——是否开启由用户在
    # 设置页决定（默认关）。定时提醒（kind=time）不受此开关影响。
    "state_watch_enabled": False,
    # 语音发送（音源接口化）：voice_mode off=关闭 / auto=模型自主
    # （send_voice 工具按语境调用）/ always=模型对话回复一律转语音。
    # 端点指向用户自部署的 GPT-SoVITS api_v2 /tts；音色档案 refs 键即
    # 情绪枚举（「通用」必填）。出厂不带任何音色——ref_audio_path 是
    # TTS 服务端路径，各用户环境不同；端点/档案为空时语音链路整体不激活
    # （工具不注入，回复走文本）。
    "voice_mode": "off",
    "tts_endpoint": "",
    "tts_timeout_seconds": 120,
    "voice_max_seconds": 55,
    "voice_profiles": [],
    "active_voice_profile_id": "",
    # 联网搜索工具开关：关闭后 call_vision_api 不再声明 web_search/web_fetch
    # （模型看不到就不会调用）；已开启时的搜索行为不受影响。
    "web_search_enabled": True,
}

# 预设主流模型 Provider（OpenAI 兼容，api_key 一律留空由用户填写）。
# 模型 id 沿用"厂商:模型"前缀格式（与用户既有 config 一致，引擎直接透传）。
# 预设模型与 xiaoli_app/pricing.py 价目表对齐（各平台当前在售主推，均有
# 现行价目——用户用快捷添加选模型后估算消费不会落进「无价目未计入」）。
PRESET_PROVIDERS = [
    {"id": "deepseek", "name": "DeepSeek 深度求索",
     "base_url": "https://api.deepseek.com/v1/chat/completions",
     "models": ["deepseek:deepseek-v4-flash", "deepseek:deepseek-v4-pro",
                "deepseek:deepseek-v4-flash-vision-exp"]},
    {"id": "zhipu", "name": "智谱 GLM",
     "base_url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
     "models": ["zhipu:glm-5.3", "zhipu:glm-5.3-flash"]},
    {"id": "qwen", "name": "通义千问",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
     "models": ["qwen:qwen3.8-max", "qwen:qwen3.8-flash"]},
    {"id": "kimi", "name": "月之暗面 Kimi",
     "base_url": "https://api.moonshot.cn/v1/chat/completions",
     "models": ["kimi:kimi-k3"]},
    {"id": "doubao", "name": "豆包（火山引擎）",
     "base_url": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
     "models": ["doubao:doubao-seed-evolving", "doubao:doubao-seed-2.0-mini"]},
    {"id": "siliconflow", "name": "硅基流动 SiliconFlow",
     "base_url": "https://api.siliconflow.cn/v1/chat/completions",
     "models": ["siliconflow:deepseek-ai/deepseek-v4-flash-0731",
                "siliconflow:deepseek-ai/deepseek-v4-pro"]},
]


def default_data_dir():
    """默认数据目录：%USERPROFILE%\\小漓（无 D:\\ 依赖，小白新机器可用）。

    取不到 USERPROFILE 时退回当前目录。
    """
    home = os.environ.get("USERPROFILE", "").strip()
    if home:
        return os.path.join(home, "小漓")
    return os.path.abspath(".")


def default_tasks_dir():
    """任务桥默认目录（小漓 ↔ 天枢交换任务文件）。

    便携默认：程序（exe/脚本）所在目录旁 wxauto——程序拷到哪数据跟到哪。
    环境变量 XIAOLI_TIANSHU_WORKDIR 可覆盖（指定工作区根，如开发机联调）。
    天枢 CLI 以 tianshu_workdir 为 cwd 启动（路径安全检查基于 cwd），
    tasks_dir 是其直接子目录时任意位置都能工作——不再要求固定 D:\\ 目录。
    """
    workdir = os.environ.get("XIAOLI_TIANSHU_WORKDIR", "").strip()
    if workdir:
        return os.path.join(workdir, "wxauto")
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "wxauto")


def sync_workdir_to_tasks(cfg):
    """tianshu_workdir 跟随 tasks_dir 的父目录（天枢 CLI 的 cwd 锚点）。

    天枢 CLI 路径安全检查基于进程 cwd（源码实测 workspace root =
    resolve(cwd)）；桌面端以 tianshu_workdir 为 cwd 启动 CLI，tasks_dir
    是它的直接子目录时检查天然通过——用户选任意目录都能工作。
    仅当 tasks_dir 非空且当前 workdir 不是它的祖先时更新。返回是否更新。
    """
    tasks = str(cfg.get("tasks_dir") or "").strip()
    if not tasks:
        return False
    try:
        tasks_abs = os.path.abspath(tasks)
        workdir = str(cfg.get("tianshu_workdir") or "").strip()
        if workdir:
            work_abs = os.path.abspath(workdir)
            if tasks_abs.startswith(work_abs + os.sep) or tasks_abs == work_abs:
                return False  # 已在工作目录内
        cfg["tianshu_workdir"] = os.path.dirname(tasks_abs)
        return True
    except Exception:
        return False


def tianshu_global_config_path():
    """天枢 CLI 全局配置：RIVET_HOME 优先，否则 %LOCALAPPDATA%\\.rivet\\config.json。"""
    home = os.environ.get("RIVET_HOME", "").strip()
    if home:
        return os.path.join(home, "config.json")
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), ".rivet", "config.json")


def grant_tasks_dir_to_tianshu(tasks_dir):
    """把 tasks_dir 预授权给天枢 CLI（agent.permissions 目录授权）。

    天枢 CLI 启动时 applyConfiguredPathGrants 应用 additionalReadDirs /
    additionalWriteDirs（源码实测 bootstrapInteractiveSession）——即使 CLI
    常驻固定工作目录（cwd 不变），也能读取任意位置的 tasks_dir，无人值守
    全自动化不受目录位置限制。授权目录必须存在（CLI fail-closed 跳过
    不存在的项），调用前确保 tasks_dir 已创建。

    返回 (ok, changed)：ok=写入成功（或无需写）；changed=配置发生变更
    （提示用户重启已打开的 CLI 使授权生效）。
    """
    tasks_dir = (tasks_dir or "").strip()
    if not tasks_dir or not os.path.isdir(tasks_dir):
        return False, False
    cfg_path = tianshu_global_config_path()
    if not os.path.isfile(cfg_path):
        return False, False  # 未装/未初始化天枢 CLI——不擅自创建配置
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            cfg = {}
        perms = cfg.setdefault("agent", {}).setdefault("permissions", {})
        read_dirs = [str(x).strip() for x in (perms.get("additionalReadDirs") or [])
                     if str(x).strip()]
        write_dirs = [str(x).strip() for x in (perms.get("additionalWriteDirs") or [])
                      if str(x).strip()]
        changed = False
        for lst in (read_dirs, write_dirs):
            if tasks_dir not in lst:
                lst.append(tasks_dir)
                changed = True
        perms["additionalReadDirs"] = read_dirs
        perms["additionalWriteDirs"] = write_dirs
        if changed:
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            logger.info(f"[配置] 天枢 CLI 已授权任务目录: {tasks_dir}")
        return True, changed
    except (OSError, ValueError) as e:
        logger.warning(f"[配置] 天枢 CLI 授权目录写入失败: {e}")
        return False, False


def default_memory_file():
    """对话记忆默认存储位置。"""
    return os.path.join(default_data_dir(), "memory.json")

# 旧字段投影所需的完整键集合（引擎 WeChatBot.__init__ 消费方）
_PROJECT_KEYS = (
    "ai_api_url", "ai_api_key", "chat_model",
    "vision_api_url", "vision_api_key",
    "system_prompt", "bot_nickname",
    "chat_temperature", "chat_top_p",
    "max_history",
)

CARD_TEMPLATE = {
    "id": DEFAULT_CARD_ID,
    "name": "小漓",
    "emoji": "🐳",
    # 小漓人设：DeepSeek 酱（蓝色大肥鱼）——DeepSeek 蓝色鲸鱼 logo 的拟人化
    # （发布安全：不含任何真实姓名/学校/群组信息）
    "system_prompt": _DEFAULT_SYSTEM_PROMPT,
    "nickname": "小漓",
    "chat_provider": "deepseek",
    "chat_model": "",
    "temperature": 0.7,
    "top_p": 0.9,
    "max_history": 1000,
}


def mask_key(key):
    """遮蔽 key 用于界面显示：sk-abcdef123456 → sk-***3456；短 key 全遮蔽"""
    if not key:
        return ""
    if len(key) <= 3:
        return "***"
    if key.startswith("sk-"):
        return "sk-" + "***" + key[-4:]
    return "***" + key[-4:]


def _read_card(cards_dir, card_id):
    """读 cards/<id>.json；不存在/损坏返回 None"""
    if not cards_dir:
        return None
    path = os.path.join(cards_dir, card_id + ".json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            card = json.load(f)
        return card if isinstance(card, dict) else None
    except (OSError, ValueError):
        return None


def _write_card(cards_dir, card):
    """写 cards/<id>.json（供迁移创建默认卡）"""
    os.makedirs(cards_dir, exist_ok=True)
    path = os.path.join(cards_dir, card["id"] + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)


def strip_legacy_immersion(cards_dir):
    """把角色卡里烘焙的旧版「角色沉浸要求」剥离（纪律迁到运行时注入）。

    历史：回复纪律曾拼进默认人设卡（`_DEFAULT_SYSTEM_PROMPT`）随卡落盘，
    所以老用户的卡里带着那份 253 字旧块。现在纪律由 REPLY_STYLE_RULES 在
    运行时注入，卡里若残留旧块就会**双份规则**（措辞还可能冲突）——这里
    一次性剥掉。

    安全边界：
    - 只剥与 `_LEGACY_ROLE_IMMERSION` **逐字一致**的片段（不模糊匹配、
      不猜用户自定义内容）；不含该片段的卡不动、不写备份（幂等）。
    - 改写前备份原文件为 `<卡>.json.bak`（已存在则不覆盖——保留最初原件）；
      `.bak` 不是 `.json`，不会被 card_store.list_cards 当成卡读进来。
    - 任何单卡失败只记日志跳过，绝不让启动失败。

    返回被迁移的卡 id 列表（空 = 无需迁移）。
    """
    migrated = []
    if not cards_dir or not os.path.isdir(cards_dir):
        return migrated
    try:
        names = sorted(os.listdir(cards_dir))
    except OSError:
        return migrated
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(cards_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                card = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(card, dict):
            continue
        prompt = str(card.get("system_prompt") or "")
        if _LEGACY_ROLE_IMMERSION not in prompt:
            continue
        prompt = prompt.replace(_LEGACY_ROLE_IMMERSION, "")
        card["system_prompt"] = re.sub(r"\n{3,}", "\n\n", prompt).strip()
        bak = path + ".bak"
        try:
            if not os.path.exists(bak):
                with open(path, "rb") as src, open(bak, "wb") as dst:
                    dst.write(src.read())
            with open(path, "w", encoding="utf-8") as f:
                json.dump(card, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.error(f"[配置] 角色卡沉浸要求迁移失败 {name}: {e}")
            continue
        migrated.append(str(card.get("id") or name))
    if migrated:
        logger.info("[配置] 旧版沉浸要求已从角色卡迁出（改由运行时注入）: "
                    + ", ".join(migrated))
    return migrated


def migrate_config(cfg, cards_dir):
    """旧 config（无 providers）→ 补 providers + 默认卡「小漓」+ active_card_id。

    幂等：已含 providers 的 config 直接返回（不重复迁移、不覆盖卡）。
    不写盘（写盘由调用方统一做）。返回新 cfg（新 dict，原 cfg 不变）。
    """
    out = dict(cfg)
    if out.get("providers"):
        return out  # 已是新结构

    # 用旧端点建默认 provider；无旧配置时预置 DeepSeek（id=deepseek 与默认卡引用对齐，空 key）
    url = str(out.get("ai_api_url", "")).strip()
    if url:
        providers = [{
            "id": "deepseek",
            "name": "DeepSeek 深度求索",
            "base_url": url,
            "api_key": str(out.get("ai_api_key", "")),
            "models": sorted({m for m in (
                out.get("chat_model", ""), out.get("vision_model", ""), out.get("file_model", "")
            ) if m}),
        }]
    else:
        p = dict(PRESET_PROVIDERS[0])  # 已是 id=deepseek（历史缺陷：覆盖成 default 导致手动配置 providers 后引用失效）
        p["api_key"] = ""
        providers = [p]
    out["providers"] = providers

    # 从旧字段建默认卡（人设用模板通用版，不抄旧 system_prompt——防个人化信息随迁移进发布卡）
    card = dict(CARD_TEMPLATE)
    card["nickname"] = str(out.get("bot_nickname", "小漓"))
    card["chat_model"] = str(out.get("chat_model", ""))
    card["temperature"] = out.get("chat_temperature", 0.7)
    card["top_p"] = out.get("chat_top_p", 0.9)
    card["max_history"] = out.get("max_history", 1000)
    _write_card(cards_dir, card)

    out["active_card_id"] = DEFAULT_CARD_ID
    logger.info("[配置] 旧 config 已迁移：providers + 默认角色卡「小漓」")
    return out


def _provider(cfg, provider_id):
    """按 id 找 provider；不存在返回 None"""
    for p in cfg.get("providers") or []:
        if p.get("id") == provider_id:
            return p
    return None


def _fallback_provider(cfg):
    """找不到卡引用 provider 时回退第一个可用 provider（旧卡引用 default 等
    已废弃 id 时兜底——否则投影出空 base_url → API 请求 Invalid URL ''）。"""
    for p in cfg.get("providers") or []:
        return p
    return None


def project_config(cfg, card):
    """根据 providers + 活跃卡重建旧字段投影。返回新 cfg（原 cfg 不变）。

    单模型化：视觉/分类统一走 chat provider 端点（vision_api_url/key 沿用
    chat 端点），不再投影 vision_model / file_model / vision_temp /
    vision_max_tokens——call_vision_api 的 model 取 chat_model。
    - 未知 provider → 回退第一个可用 provider（不置空 URL）；全部缺失才置空
    """
    out = dict(cfg)
    card = card or {}

    chat_p = _provider(out, card.get("chat_provider") or "deepseek")
    if chat_p is None:
        chat_p = _fallback_provider(out)
    chat_p = chat_p or {}

    chat_url = str(chat_p.get("base_url", ""))
    chat_key = str(chat_p.get("api_key", ""))

    out["ai_api_url"] = chat_url
    out["ai_api_key"] = chat_key
    out["chat_model"] = str(card.get("chat_model", ""))
    # 视觉端点沿用聊天端点（单模型化；WeChatBot.__init__ 的 cfg.get 兜底
    # 即使缺键也回退 ai_api_url/ai_api_key，这里投影保证 config.json 写回稳定）
    out["vision_api_url"] = chat_url
    out["vision_api_key"] = chat_key
    out["system_prompt"] = str(card.get("system_prompt", ""))
    out["bot_nickname"] = str(card.get("nickname", "")) or out.get("bot_nickname", "小漓")
    out["chat_temperature"] = card.get("temperature", out.get("chat_temperature", 0.7))
    out["chat_top_p"] = card.get("top_p", out.get("chat_top_p", 0.9))
    out["max_history"] = card.get("max_history", out.get("max_history", 1000))
    return out


def save_config(cfg, path="config.json"):
    """写回 config.json（API key 落盘加密：providers[].api_key + 投影 key 字段）。
    内存态 cfg 保持明文（引擎/UI 使用）；加密只发生在落盘副本上。
    chat_card_params 是 load_config_store 的派生投影（事实源 =
    chat_card_bindings + cards/），落盘前剥离避免与卡内容脱同步。"""
    out = dict(cfg)
    out.pop("chat_card_params", None)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_encrypt_cfg_keys(out), f, ensure_ascii=False, indent=4)


def _project_chat_bindings(cfg, cards_dir):
    """per-chat 角色卡绑定（chat_card_bindings: {聊天名: 卡id}）→ 运行时参数表。

    键做 memory 口径归一化（memory_key：剥引号变体与空白——OCR 差异不分裂
    绑定）；卡缺失/读失败跳过该绑定（该聊天运行时回落全局活跃卡），不报错
    不写半残条目。返回 {归一化聊天名: {system_prompt, chat_model,
    ai_api_url, ai_api_key, temperature, top_p, max_history}}。
    """
    bindings = cfg.get("chat_card_bindings") or {}
    if not isinstance(bindings, dict) or not bindings:
        return {}
    params = {}
    for chat, cid in bindings.items():
        cid = str(cid or "").strip()
        key = str(chat or "").strip()   # 新口径：会话名原文（与记忆键一致）
        if not cid or not key:
            continue
        card = _read_card(cards_dir, cid)
        if card is None:
            continue
        p = _provider(cfg, card.get("chat_provider") or "deepseek") \
            or _fallback_provider(cfg) or {}
        params[key] = {
            "system_prompt": str(card.get("system_prompt", "") or ""),
            "chat_model": str(card.get("chat_model", "") or ""),
            "ai_api_url": str(p.get("base_url", "") or ""),
            "ai_api_key": str(p.get("api_key", "") or ""),
            "temperature": card.get("temperature"),
            "top_p": card.get("top_p"),
            "max_history": card.get("max_history"),
        }
        # 兼容旧口径（键曾归一化）：同一份参数在归一化键下也能查到，
        # 让升级前保存的绑定继续生效，不必要求用户重设。
        legacy = memory_key(key)
        if legacy and legacy != key:
            params.setdefault(legacy, params[key])
    return params


def load_config_store(path="config.json", cards_dir="cards"):
    """加载 config.json → 迁移 → 读活跃卡 → 投影重建 → 写回。返回引擎可用的完整 cfg。

    文件不存在时返回空 dict（迁移会补 providers 与默认卡结构）。
    """
    cfg = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError) as e:
            logger.error(f"[配置] 读取 config.json 失败: {e}，按空配置处理")
    # 读盘后解密 key 字段（dpapi: 前缀 → DPAPI 解开；旧明文原样保留），
    # 内存态 cfg 全为明文，引擎/UI 零改动
    cfg = _decrypt_cfg_keys(cfg)

    # vision_max_tokens → reply_max_tokens 一次性改名迁移（单键化：两链路
    # 共用的回复长度上限不再挂视觉键名——老配置不丢值，写回后旧键消失）
    if "vision_max_tokens" in cfg and "reply_max_tokens" not in cfg:
        cfg["reply_max_tokens"] = cfg.pop("vision_max_tokens")

    cfg = migrate_config(cfg, cards_dir)
    # AI 参数默认补全：投影只重建 provider 相关键，cooldown/
    # api_retry/api_timeout 等不投影——缺失即 WeChatBot 初始化 KeyError。
    # 放投影前（project_config 覆盖 ai_api_url 等投影键，本段只补缺口）。
    for k, v in AI_DEFAULTS.items():
        if k not in cfg:
            cfg[k] = v
    # 二期新增默认：天枢安装/下载/首轮提示词（小白引导用）
    for k, v in {
        "tianshu_install_dir": "",
        "tianshu_download_url": "https://codeload.github.com/huiliyi37/Tianshu-Tui/zip/refs/heads/main",
        "first_prompt_path": "",  # 空 = 用内置模板（build_first_prompt）；非空且文件存在时优先读文件
        "image_click_offset": [-200, -130],  # 图片点击偏移（用户实测校准 2026-08-04：真人点击测 [-199,-131] 取整；位置偏了再到设置页调）
        "tianshu_workdir": r"D:\工作间",  # 天枢 CLI（rivet）的工作目录
        "tianshu_guided": False,  # 首启 /yes 一次性引导是否已完成（True 后初始化不再切 YOLO）
        "theme": "abyss",  # 界面主题（默认「深海小漓」套，壁纸配套见 wallpaper_path）
        "chat_card_bindings": {},  # per-chat 角色卡绑定 {聊天名: 卡id}；空 = 全部跟随全局活跃卡
        "card_opacity": 0.5,  # 卡片不透明度 0~1.0（设置页滑块调节毛玻璃强度，默认 50%）
        "panel_opacity": 0.5,  # 面板/输入区不透明度（日志区/表格/输入框等大白块，默认 50%）
        "font_scale": "medium",  # 全局字号档位：small/medium/large（启动默认标准档）
        "wallpaper_path": "小漓主题.jpg",  # 背景壁纸（裸文件名 → 启动时按壁纸库解析绝对路径；配套 abyss 主题）
        "web_proxy": "",  # 联网搜索/网页抓取代理（http/https/socks5；空 = 直连；不影响模型 API）
        "chat_feature_overrides": {},  # per-chat 功能覆盖三态例外 {memory_key(聊天): {voice/web_search/task/state_watch: bool}}；空 = 全部跟随全局
    }.items():
        if k not in cfg:
            cfg[k] = v
    # UI 新默认迁移：主题不在保留名单（ui.THEMES 的 7 套）→ abyss、壁纸空
    # → 小漓主题.jpg（配套「深海小漓」套）。历史出厂默认 blue 与已砍掉的
    # 12 套模板主题（tokyonight 曾在此误伤：用户手选也被迁走，现已在名单内
    # 不再覆盖）统一落到 abyss；渲染层对未知主题本就回退 abyss，这里落盘
    # 让设置页选中态与实际渲染一致。
    if cfg.get("theme") not in ("abyss", "neon", "tokyonight", "stellar",
                                "moxin", "cream", "mint"):
        cfg["theme"] = "abyss"
    # font_scale 不做归一化改写：用户保存什么档位重启后就是什么档位
    # （历史缺陷：曾把 medium/None 强制改写成 small——「标准」保存后重启
    # 必变「小」，且默认档被钉死在 small）
    if not str(cfg.get("wallpaper_path") or "").strip():
        cfg["wallpaper_path"] = "小漓主题.jpg"
    # 任务目录：用户显式设置的路径一律保留（引导/设置页的选择即事实），
    # 为空时给便携默认。天枢 CLI 路径检查基于 cwd——tianshu_workdir 跟随
    # tasks_dir 父目录（sync_workdir_to_tasks），用户选任意目录都能工作，
    # 不再强制迁入固定工作区（历史缺陷：f95bf8a 迁移逻辑静默覆盖用户设置）。
    if not str(cfg.get("tasks_dir") or "").strip():
        cfg["tasks_dir"] = default_tasks_dir()
    sync_workdir_to_tasks(cfg)
    # per-chat 功能覆盖清洗：结构/键口径/白名单不合规条目静默剔除
    # （fail-closed：脏数据不会流到运行时查询）
    sanitize_feature_overrides(cfg)
    # 角色卡迁移：老卡里烘焙的旧版沉浸要求剥离（回复纪律改为运行时注入）。
    # 必须在读活跃卡之前执行——本次投影要用的就是迁移后的卡内容。
    try:
        strip_legacy_immersion(cards_dir)
    except Exception as e:
        logger.warning(f"[配置] 角色卡迁移跳过: {e}")
    card = _read_card(cards_dir, cfg.get("active_card_id", DEFAULT_CARD_ID))
    if card is None:
        # 活跃卡缺失（cards/ 被删 / active_card_id 指向不存在卡）→ 用默认
        # 模板就地补建该卡并持久化。只告警回退会导致每次启动都重复告警
        # （真机：重置测试区后 active_card_id=xiaoli 而卡文件缺失）。
        missing_id = str(cfg.get("active_card_id") or DEFAULT_CARD_ID)
        logger.warning(f"[配置] 活跃角色卡不存在: {missing_id}，已按默认模板补建")
        card = dict(CARD_TEMPLATE)
        card["id"] = missing_id
        try:
            from xiaoli_app.card_store import save_card
            save_card(cards_dir, card)
        except Exception as e:
            logger.error(f"[配置] 默认卡补建失败（回退模板投影）: {e}")
    cfg = project_config(cfg, card)
    # per-chat 绑定参数投影（派生数据：内存可用、save_config 落盘时剥离）
    cfg["chat_card_params"] = _project_chat_bindings(cfg, cards_dir)

    try:
        save_config(cfg, path)
    except OSError as e:
        logger.error(f"[配置] 写回 config.json 失败: {e}")
    return cfg
