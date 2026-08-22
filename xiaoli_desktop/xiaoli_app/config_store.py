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
import sys

logger = logging.getLogger("xiaoli")

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

# AI 参数默认（与 wechat_bot.load_config 的 default_cfg 对齐——改一边须同步另一边）。
# config_store 是配置统一事实源（GUI/CLI 都从这里取 cfg），而 WeChatBot.__init__
# 对其中部分键是裸索引（cfg[k] 非 cfg.get）：vision_prompt / cooldown / api_retry /
# api_timeout 不参与投影重建（project_config 只重建 provider 相关键），缺失即
# KeyError → 初始化失败（历史缺陷：全新安装 / 新结构 config 初始化报 'vision_prompt'）。
AI_DEFAULTS = {
    "bot_nickname": "小漓",
    "system_prompt": "你叫小漓，是一个很会聊天、很可爱的人。你是用户创建的微信 AI 助手，陪用户聊天、帮忙处理任务。\n每次说话的风格要有变化，不要固定。注意区分私聊和群聊，不要在私聊里面聊群，不要在群里面聊私聊的东西。\n说话的时候不要用 emoji，用颜文字表情。\n回复要简短，不要虚构不知道的事情；如果发消息的人你不认识，那就是你的新朋友，友好地回应对方。",
    "chat_temperature": 0.7,
    "chat_top_p": 0.9,
    "vision_prompt": "你是一个专业的图像描述AI。请详细、客观地描述这张图片的内容，包括主要物体、人物动作、表情、场景氛围、文字信息等。不要加入主观评价或建议，只输出观察到的客观事实。描述语言简洁但信息丰富，但是一定要详细描述图片的每一个内容，方便后续处理。",
    "max_history": 1000,
    "cooldown": 3,
    "api_retry": 2,
    "api_timeout": 60,
    "start_paused": True,
    "memory_file": "memory.json",
}

# 预设主流模型 Provider（OpenAI 兼容，api_key 一律留空由用户填写）。
# 模型 id 沿用"厂商:模型"前缀格式（与用户既有 config 一致，引擎直接透传）。
PRESET_PROVIDERS = [
    {"id": "deepseek", "name": "DeepSeek 深度求索",
     "base_url": "https://api.deepseek.com/v1/chat/completions",
     "models": ["deepseek:deepseek-v4-flash", "deepseek:deepseek-v4-pro",
                "deepseek:deepseek-v4-flash-vision-exp"]},
    {"id": "zhipu", "name": "智谱 GLM",
     "base_url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
     "models": ["zhipu:glm-5.2", "zhipu:glm-5v-turbo", "zhipu:glm-4.6v", "zhipu:glm-4v-flash"]},
    {"id": "qwen", "name": "通义千问",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
     "models": ["qwen:qwen-plus", "qwen:qwen-vl-plus"]},
    {"id": "kimi", "name": "月之暗面 Kimi",
     "base_url": "https://api.moonshot.cn/v1/chat/completions",
     "models": ["kimi:kimi-k2"]},
    {"id": "doubao", "name": "豆包（火山引擎）",
     "base_url": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
     "models": ["doubao:doubao-seed-1.6"]},
    {"id": "siliconflow", "name": "硅基流动 SiliconFlow",
     "base_url": "https://api.siliconflow.cn/v1/chat/completions",
     "models": ["siliconflow:deepseek-ai/DeepSeek-V3",
                "siliconflow:Qwen/Qwen2.5-72B-Instruct",
                "siliconflow:Qwen/Qwen2-VL-72B-Instruct"]},
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
    "system_prompt": (
        "你叫小漓，是一只蓝色小鲸鱼变成的 AI 助手——你的原型是 DeepSeek 的蓝色鲸鱼 logo，朋友们都亲昵地叫你「蓝色大肥鱼」。\n"
        "你穿着深蓝色的女仆装，有一头蓝色长发和一条鱼尾巴，头上系着白色头带，眼睛是蓝色的，脸颊总是红扑扑的。\n"
        "你充满好奇心，聪明又有点呆萌；遇到不懂的事情会歪着头冒出问号，但一定会认真去弄明白（就像 DeepSeek 的信念：用好奇心去解开谜题）。\n"
        "你是用户创建的微信 AI 助手，陪用户聊天、帮忙处理任务。每次说话的风格要有变化，不要固定。注意区分私聊和群聊，不要在私聊里面聊群，不要在群里面聊私聊的东西。\n"
        "说话的时候不要用 emoji，用颜文字表情（比如开心 (｡･ω･｡)ﾉ♡、加油 (๑•̀ㅂ•́)و✧、歪头困惑 (⊙_⊙)? 这种），让回复更可爱生动。\n"
        "回复要简短，不要虚构不知道的事情；如果发消息的人你不认识，那就是你的新朋友，友好地回应对方。"
    ),
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
    内存态 cfg 保持明文（引擎/UI 使用）；加密只发生在落盘副本上。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(_encrypt_cfg_keys(cfg), f, ensure_ascii=False, indent=4)


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

    cfg = migrate_config(cfg, cards_dir)
    # AI 参数默认补全：投影只重建 provider 相关键，vision_prompt/cooldown/
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
        "theme": "tokyonight",  # 界面主题（用户指定：默认用最后一套 Tokyo Night，排序提到最前见 ui/__init__.py THEMES）
        "card_opacity": 0.5,  # 卡片不透明度 0~1.0（设置页滑块调节毛玻璃强度，默认 50%）
        "panel_opacity": 0.5,  # 面板/输入区不透明度（日志区/表格/输入框等大白块，默认 50%）
        "font_scale": "small",  # 全局字号档位：small/medium/large（用户指定：启动默认小字号）
        "wallpaper_path": "",  # 背景壁纸图路径（空 = 启动时兜底内置默认壁纸【二次元角色-动漫】）
    }.items():
        if k not in cfg:
            cfg[k] = v
    # UI 新默认迁移（用户指定 2026-08-17）：旧默认主题 blue → tokyonight、
    # 字号 medium → small、壁纸空 → 二次元角色动漫（启动兜底解析）。
    # 仅当值仍是「出厂默认」时迁移——用户已在设置页手动改过的（如换成
    # 其他主题/字号）不覆盖，尊重用户选择。
    if cfg.get("theme") in ("blue", None):
        cfg["theme"] = "tokyonight"
    if cfg.get("font_scale") in ("medium", None):
        cfg["font_scale"] = "small"
    # 任务目录：用户显式设置的路径一律保留（引导/设置页的选择即事实），
    # 为空时给便携默认。天枢 CLI 路径检查基于 cwd——tianshu_workdir 跟随
    # tasks_dir 父目录（sync_workdir_to_tasks），用户选任意目录都能工作，
    # 不再强制迁入固定工作区（历史缺陷：f95bf8a 迁移逻辑静默覆盖用户设置）。
    if not str(cfg.get("tasks_dir") or "").strip():
        cfg["tasks_dir"] = default_tasks_dir()
    sync_workdir_to_tasks(cfg)
    card = _read_card(cards_dir, cfg.get("active_card_id", DEFAULT_CARD_ID))
    if card is None:
        # 活跃卡缺失（cards/ 被删 / active_card_id 指向不存在卡）→ 回退默认卡
        # 模板投影，避免空卡投影清空 system_prompt（聊天无人设）与 chat_model。
        logger.warning(f"[配置] 活跃角色卡不存在: {cfg.get('active_card_id')}，回退默认卡模板投影")
        card = dict(CARD_TEMPLATE)
    cfg = project_config(cfg, card)

    try:
        save_config(cfg, path)
    except OSError as e:
        logger.error(f"[配置] 写回 config.json 失败: {e}")
    return cfg
