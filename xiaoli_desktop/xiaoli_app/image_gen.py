# -*- coding: utf-8 -*-
"""画图客户端（B1，默认关）：OpenAI images 兼容端点生成一张图，落临时文件。

「画图接口化」对齐语音的「音源接口化」：小漓不内置画图模型，端点/模型由
用户配置（自部署或任一 OpenAI images 兼容服务）；端点或模型为空时工具
整体不声明（模型看不到就不会调用）。响应兼容两种形态：b64_json 直接
解码；url 则下载（大小封顶防异常端点拖爆内存）。直连不走 web_proxy——
画图端点是用户自己填的（多为本地/内网服务），与联网搜索的代理语义无关。
"""
from __future__ import annotations

import base64
import logging
import tempfile

import requests

logger = logging.getLogger("xiaoli")

# 生成图下载/生成的体积封顶：异常端点返回超大自然文件时不再往下走
MAX_IMAGE_BYTES = 20 * 1024 * 1024

# 图片格式嗅探表（magic → 扩展名）：微信按扩展名/内容识别图片，落盘前先验明
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),
)


class ImageGenError(Exception):
    """画图失败（HTTP 非 200 / 无图片数据 / 格式不识别 / 超体积）。"""


def sniff_image_ext(raw: bytes) -> str | None:
    """按 magic 嗅探图片格式，返回扩展名（含点）或 None。"""
    for magic, ext in _MAGIC:
        if raw.startswith(magic):
            return ext
    return None


def generate_image_file(endpoint: str, model: str, prompt: str,
                        size: str = "1024x1024", api_key: str = "",
                        timeout: int = 120) -> str:
    """调用 images 生成端点生成一张图，写入临时文件并返回路径。

    抛 ImageGenError（调用方转回执文本，不把异常发出去）。
    """
    if not endpoint or not model:
        raise ImageGenError("画图端点或模型未配置")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        resp = requests.post(endpoint, headers=headers,
                             json={"model": model, "prompt": prompt,
                                   "n": 1, "size": size},
                             timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise ImageGenError(f"画图端点请求失败: {type(e).__name__}") from e
    if resp.status_code != 200:
        raise ImageGenError(f"画图端点 HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as e:
        raise ImageGenError("画图端点响应不是 JSON") from e
    item = (data.get("data") or [{}])[0]
    raw = None
    if isinstance(item, dict) and item.get("b64_json"):
        try:
            raw = base64.b64decode(item["b64_json"])
        except (ValueError, TypeError) as e:
            raise ImageGenError("b64_json 解码失败") from e
    elif isinstance(item, dict) and item.get("url"):
        try:
            dl = requests.get(item["url"], timeout=timeout)
        except requests.exceptions.RequestException as e:
            raise ImageGenError(f"图片下载失败: {type(e).__name__}") from e
        if dl.status_code != 200:
            raise ImageGenError(f"图片下载 HTTP {dl.status_code}")
        raw = dl.content
    else:
        raise ImageGenError("画图端点响应无图片数据（b64_json/url 均缺失）")
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ImageGenError("图片体积异常（空或超上限）")
    ext = sniff_image_ext(raw)
    if ext is None:
        raise ImageGenError("响应不是可识别的图片格式")
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tf:
        tf.write(raw)
        return tf.name
