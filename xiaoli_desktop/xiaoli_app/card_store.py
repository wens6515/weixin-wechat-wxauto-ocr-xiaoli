# -*- coding: utf-8 -*-
"""角色卡存储层：cards/ 目录 CRUD、校验、导入导出。

角色卡 = 人格 + 模型引用 + 参数预设，一次激活一张（config.json 的 active_card_id）。
卡内**不存 API key**（只引用 provider id），导出/分享不泄密。
"""
import json
import os
import re
import shutil
import time
import uuid

# 卡允许的字段白名单（校验时未知字段静默丢弃）
CARD_FIELDS = {
    "id", "name", "emoji", "system_prompt", "nickname",
    "chat_provider", "chat_model",
    "vision_provider", "vision_model",
    "classify_provider", "classify_model",
    "temperature", "top_p", "vision_temp", "vision_max_tokens", "max_history",
}
# 必填字段
REQUIRED_FIELDS = {"id", "name", "system_prompt"}
# 禁止字段：任何形式的密钥都不允许进卡
FORBIDDEN_KEYWORDS = ("api_key", "apikey", "key", "secret", "token", "authorization")

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def card_path(cards_dir, card_id):
    """卡文件路径：cards/<id>.json（id 已校验过，安全拼接）"""
    return os.path.join(cards_dir, f"{card_id}.json")


def _normalize_id(card_id):
    if not card_id or not _ID_RE.match(str(card_id)):
        raise ValueError(f"非法卡 id: {card_id!r}（仅允许字母数字_-，最长 64）")
    return str(card_id)


def validate_card(card):
    """校验卡数据。缺必填 / 含密钥字段 → ValueError。返回规范化副本。"""
    if not isinstance(card, dict):
        raise ValueError("卡必须是 JSON 对象")
    missing = REQUIRED_FIELDS - set(card.keys())
    if missing:
        raise ValueError(f"卡缺少必填字段: {sorted(missing)}")
    for k in card.keys():
        if k.lower() in FORBIDDEN_KEYWORDS:
            raise ValueError(f"卡不允许包含密钥字段: {k}")
    _normalize_id(card["id"])
    return {k: v for k, v in card.items() if k in CARD_FIELDS}


def save_card(cards_dir, card):
    """校验并写盘。返回规范化卡。"""
    clean = validate_card(card)
    os.makedirs(cards_dir, exist_ok=True)
    with open(card_path(cards_dir, clean["id"]), "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    return clean


def get_card(cards_dir, card_id):
    if not card_id:
        return None
    try:
        _normalize_id(card_id)
    except ValueError:
        return None
    path = card_path(cards_dir, card_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def list_cards(cards_dir):
    """列出全部卡（按名称排序）。跳过非 JSON 文件与非法卡。"""
    if not os.path.isdir(cards_dir):
        return []
    out = []
    for name in os.listdir(cards_dir):
        if not name.endswith(".json"):
            continue
        path = os.path.join(cards_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                card = json.load(f)
            if isinstance(card, dict) and card.get("id") and card.get("name"):
                out.append(card)
        except Exception:
            continue
    return sorted(out, key=lambda c: c.get("name", ""))


def delete_card(cards_dir, card_id):
    try:
        _normalize_id(card_id)
    except ValueError:
        return False
    path = card_path(cards_dir, card_id)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def duplicate_card(cards_dir, card_id, new_id=None):
    """复制卡。new_id 缺省自动生成（原 id + 时间戳/随机后缀）。"""
    card = get_card(cards_dir, card_id)
    if card is None:
        raise ValueError(f"卡不存在: {card_id}")
    if not new_id:
        new_id = f"{card_id}_{time.strftime('%H%M%S')}{uuid.uuid4().hex[:3]}"
    dup = dict(card)
    dup["id"] = new_id
    return save_card(cards_dir, dup)


def export_card(cards_dir, card_id):
    """导出卡数据（深拷贝；卡内本就无 key）。"""
    card = get_card(cards_dir, card_id)
    if card is None:
        raise ValueError(f"卡不存在: {card_id}")
    return json.loads(json.dumps(card, ensure_ascii=False))


def import_card(cards_dir, data):
    """从 dict 或 JSON 字符串导入。校验失败抛 ValueError 且不写盘。"""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as e:
            raise ValueError(f"导入的 JSON 无法解析: {e}")
    clean = validate_card(data)
    return save_card(cards_dir, clean)
