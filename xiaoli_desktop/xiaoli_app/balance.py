# -*- coding: utf-8 -*-
"""平台余额查询适配器：用用户已配置的 API key 查询模型平台账户余额。

「用 key 查余额」没有行业标准端点，逐家适配（公开文档的 key 鉴权端点）：
- DeepSeek     GET https://api.deepseek.com/user/balance
- Moonshot     GET https://api.moonshot.cn/v1/users/me/balance
- SiliconFlow  GET https://api.siliconflow.cn/v1/user/info
其余平台（智谱/千问/豆包等）无公开的 key 鉴权余额端点 → 不适配，UI 明示
「该平台不支持 key 查询，请到控制台查看」，绝不伪造余额。

诚实边界（沿 whale-maid 设计）：查询失败保留上次快照由 UI 处理，本模块
只如实返回错误；返回的余额一律是平台实测值，不做任何推算。
"""
import requests

BALANCE_TIMEOUT = 10


def _parse_deepseek(data):
    """{"is_available":true,"balance_infos":[{"currency":"CNY","total_balance":"110.81",...}]}"""
    infos = (data or {}).get("balance_infos") or []
    if not infos:
        return None
    first = infos[0]
    try:
        return float(first.get("total_balance")), str(first.get("currency") or "CNY")
    except (TypeError, ValueError):
        return None


def _parse_moonshot(data):
    """{"code":0,"data":{"available_balance":"12.34",...}}"""
    d = (data or {}).get("data") or {}
    try:
        return float(d.get("available_balance")), "CNY"
    except (TypeError, ValueError):
        return None


def _parse_siliconflow(data):
    """{"code":200,"data":{"balance":"1.00","totalBalance":"10.00",...}}"""
    d = (data or {}).get("data") or {}
    raw = d.get("totalBalance") or d.get("balance")
    try:
        return float(raw), "CNY"
    except (TypeError, ValueError):
        return None


# provider id（config.json providers[].id，与 PRESET_PROVIDERS 对齐）→ 适配器
ADAPTERS = {
    "deepseek": {
        "endpoint": "https://api.deepseek.com/user/balance",
        "parse": _parse_deepseek,
    },
    "moonshot": {
        "endpoint": "https://api.moonshot.cn/v1/users/me/balance",
        "parse": _parse_moonshot,
    },
    "siliconflow": {
        "endpoint": "https://api.siliconflow.cn/v1/user/info",
        "parse": _parse_siliconflow,
    },
}


def is_supported(provider_id):
    """该 provider 是否有余额适配器。"""
    return str(provider_id or "").strip().lower() in ADAPTERS


def fetch_balance(provider_id, api_key, timeout=BALANCE_TIMEOUT):
    """查询余额。返回 {ok, balance, currency, error}：
    - ok=True：balance（float）+ currency 为平台实测值
    - ok=False：error 为可读原因（不支持的平台 / 无 key / HTTP 错误 / 解析失败），
      balance/currency 为 None——绝不返回伪造的 0
    key 只在本函数内用于请求头，不进日志不进返回值。"""
    pid = str(provider_id or "").strip().lower()
    ad = ADAPTERS.get(pid)
    out = {"ok": False, "balance": None, "currency": None, "error": None}
    if ad is None:
        out["error"] = "该平台不支持 key 查询余额，请到平台控制台查看"
        return out
    if not str(api_key or "").strip():
        out["error"] = "该平台还没有配置 API Key"
        return out
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    try:
        resp = requests.get(ad["endpoint"], headers=headers, timeout=timeout)
    except requests.exceptions.RequestException as e:
        out["error"] = f"查询失败：{type(e).__name__}"
        return out
    if resp.status_code != 200:
        out["error"] = f"查询失败：HTTP {resp.status_code}"
        return out
    try:
        data = resp.json()
    except ValueError:
        out["error"] = "查询失败：响应不是有效 JSON"
        return out
    parsed = ad["parse"](data)
    if parsed is None:
        out["error"] = "查询失败：响应里没有可识别的余额字段"
        return out
    balance, currency = parsed
    out.update(ok=True, balance=balance, currency=currency)
    return out
