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
chat_model / vision_model / file_model / chat_temperature ...）并写回，
AgentBot 读到的 cfg 与改造前完全同构 → 引擎零改动风险。
"""
import json
import logging
import os

logger = logging.getLogger("xiaoli")

DEFAULT_CARD_ID = "xiaoli"

# 旧字段投影所需的完整键集合（引擎 WeChatBot.__init__ 消费方）
_PROJECT_KEYS = (
    "ai_api_url", "ai_api_key", "chat_model",
    "vision_api_url", "vision_api_key", "vision_model",
    "file_model", "system_prompt", "bot_nickname",
    "chat_temperature", "chat_top_p",
    "vision_temp", "vision_max_tokens", "max_history",
)

CARD_TEMPLATE = {
    "id": DEFAULT_CARD_ID,
    "name": "小漓",
    "emoji": "🐟",
    "system_prompt": "",
    "nickname": "小漓",
    "chat_provider": "default",
    "chat_model": "",
    "vision_provider": "default",
    "vision_model": "",
    "classify_provider": "default",
    "classify_model": "",
    "temperature": 0.7,
    "top_p": 0.9,
    "vision_temp": 0.7,
    "vision_max_tokens": 10000,
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

    # 用旧端点建默认 provider
    providers = [{
        "id": "default",
        "name": "默认",
        "base_url": str(out.get("ai_api_url", "")),
        "api_key": str(out.get("ai_api_key", "")),
        "models": sorted({m for m in (
            out.get("chat_model", ""), out.get("vision_model", ""), out.get("file_model", "")
        ) if m}),
    }]
    out["providers"] = providers

    # 从旧字段建默认卡
    card = dict(CARD_TEMPLATE)
    card["system_prompt"] = str(out.get("system_prompt", ""))
    card["nickname"] = str(out.get("bot_nickname", "小漓"))
    card["chat_model"] = str(out.get("chat_model", ""))
    card["vision_model"] = str(out.get("vision_model", ""))
    card["classify_model"] = str(out.get("file_model", ""))
    card["temperature"] = out.get("chat_temperature", 0.7)
    card["top_p"] = out.get("chat_top_p", 0.9)
    card["vision_temp"] = out.get("vision_temp", 0.7)
    card["vision_max_tokens"] = out.get("vision_max_tokens", 10000)
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


def project_config(cfg, card):
    """根据 providers + 活跃卡重建旧字段投影。返回新 cfg（原 cfg 不变）。

    规则：
    - 聊天/分类沿用 chat provider 端点（模型名可不同）
    - 视觉可跨 provider（生成 vision_api_url / vision_api_key）
    - 未知 provider → 对应 url/key 置空，不崩溃（UI 配置缺失时引擎仍可启动）
    """
    out = dict(cfg)
    card = card or {}

    chat_p = _provider(out, card.get("chat_provider") or "default") or {}
    vision_p = _provider(out, card.get("vision_provider") or card.get("chat_provider") or "default") or {}

    chat_url = str(chat_p.get("base_url", ""))
    chat_key = str(chat_p.get("api_key", ""))
    vision_url = str(vision_p.get("base_url", "")) or chat_url
    vision_key = str(vision_p.get("api_key", "")) or chat_key

    out["ai_api_url"] = chat_url
    out["ai_api_key"] = chat_key
    out["chat_model"] = str(card.get("chat_model", ""))
    out["vision_api_url"] = vision_url
    out["vision_api_key"] = vision_key
    out["vision_model"] = str(card.get("vision_model", ""))
    out["file_model"] = str(card.get("classify_model", "")) or out.get("file_model", "")
    out["system_prompt"] = str(card.get("system_prompt", ""))
    out["bot_nickname"] = str(card.get("nickname", "")) or out.get("bot_nickname", "小漓")
    out["chat_temperature"] = card.get("temperature", out.get("chat_temperature", 0.7))
    out["chat_top_p"] = card.get("top_p", out.get("chat_top_p", 0.9))
    out["vision_temp"] = card.get("vision_temp", out.get("vision_temp", 0.7))
    out["vision_max_tokens"] = card.get("vision_max_tokens", out.get("vision_max_tokens", 10000))
    out["max_history"] = card.get("max_history", out.get("max_history", 1000))
    return out


def save_config(cfg, path="config.json"):
    """写回 config.json（保持与现有代码一致的格式）"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=4)


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

    cfg = migrate_config(cfg, cards_dir)
    # 二期新增默认：天枢安装/下载/首轮提示词（小白引导用）
    for k, v in {
        "tianshu_install_dir": "",
        "tianshu_download_url": "https://codeload.github.com/huiliyi37/Tianshu-Tui/zip/refs/heads/main",
        "first_prompt_path": r"D:\工作间\首轮提示词.txt",
    }.items():
        if k not in cfg:
            cfg[k] = v
    card = _read_card(cards_dir, cfg.get("active_card_id", DEFAULT_CARD_ID))
    if card is None:
        logger.warning(f"[配置] 活跃角色卡不存在: {cfg.get('active_card_id')}，使用空卡投影")
    cfg = project_config(cfg, card)

    try:
        save_config(cfg, path)
    except OSError as e:
        logger.error(f"[配置] 写回 config.json 失败: {e}")
    return cfg
