# -*- coding: utf-8 -*-
"""LLM API 客户端（从 wechat_bot 抽出）：OpenAI 兼容 chat/completions 统一
调用入口 + token 预算工具。

chat / vision / watch / memory 四条链路共用同一调用入口：429/5xx/网络异常/
坏 JSON 指数退避重试、服务器 Retry-After 尊重与放弃阈值、墙钟预算封顶、
终态写用量统计。wechat_bot 保留同名 re-export（tests / tools 直接 import
这些名字）。
"""
import json
import logging
import time

import requests

logger = logging.getLogger("xiaoli")

# ---------- 重试策略常量 ----------
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
    """把 messages 裁剪到 token 预算内（从最旧的非 system 消息开始丢弃）。

    根因：文件识别把超大文件全文拼进 prompt（实测请求 272 万 token，
    模型上限 104 万 → API 400 "maximum context length"）。
    规则（保序——消息布局是缓存优化的一部分，中途的 system（重要记忆/
    相关记忆/当前时间）绝不能被搬到最前）：
    - system 消息永不丢弃；单条超预算 20% 时截断其内容（人设可能很长，
      其余 system 都很短不受影响）
    - 非 system 消息保持相对顺序，从最旧（列表前端）开始丢弃直到 ≤ 预算
    - 末条 user 消息（当前消息）不丢弃，仍超预算时截断内容到预算 60%
    返回裁剪后的 messages。
    """
    budget = max(1000, budget)
    cap = max(500, budget - reserve)
    trimmed = []
    for m in messages:
        if m.get("role") == "system":
            content = str(m.get("content") or "")
            limit = max(500, budget // 5)
            if estimate_tokens(content) > limit:
                # 保留开头（system prompt 语义在前）
                trimmed.append({"role": "system", "content": content[:limit]})
            else:
                trimmed.append(m)
        else:
            trimmed.append(m)
    total = sum(estimate_tokens(_content_to_text(m.get("content")))
                for m in trimmed)
    last_idx = len(trimmed) - 1
    for idx in range(last_idx):
        if total <= cap:
            break
        m = trimmed[idx]
        if m.get("role") == "system":
            continue
        total -= estimate_tokens(_content_to_text(m.get("content")))
        trimmed[idx] = None
    out = [m for m in trimmed if m is not None]
    # 末条 user（当前消息，可能含文件全文/多模态块）仍超预算 → 截断到 60%
    if out and out[-1].get("role") == "user":
        keep = max(500, int(budget * 0.6))
        raw = out[-1].get("content")
        if estimate_tokens(_content_to_text(raw)) > keep:
            if isinstance(raw, list):
                content = _trim_blocks(raw, keep)
            else:
                content = str(raw or "")[:keep] + "[内容过长已截断]…"
            out[-1] = dict(out[-1], content=content)
    return out


class LlmClient:
    """OpenAI 兼容 chat/completions 统一调用入口（chat/vision/watch/memory 共用）。

    无状态：重试次数与墙钟预算每次调用传入（bot 属性热改即时生效）；
    usage_store 可为 None（测试桩/未初始化时静默跳过用量统计）。"""

    def __init__(self, usage_store=None):
        self.usage_store = usage_store

    def post(self, url, headers, payload, timeout, label="api", meta=None,
             retry=2, wall_budget=API_WALL_BUDGET_DEFAULT):
        """调用一次 chat/completions（含重试）。成功返回解析后的 dict；
        最终失败抛 ApiCallError。绝不返回「API 错误: xxx」这类会原样发给
        好友的字符串（历史行为，已废）。

        重试策略：
        - 429/5xx/网络异常/坏 JSON → 指数退避重试（1s 起步、封顶 8s）；
          服务器带 Retry-After 时尊重之，超过 RETRY_AFTER_GIVEUP 直接放弃
        - 其余 4xx（鉴权/参数错误）不重试——重试无意义，只会拖慢用户感知
        - 墙钟预算 wall_budget（默认 45s）：重试总时长封顶。历史缺陷：
          每次超时 60s × api_retry 3 次 = 用户干等 3 分钟才收到报错
        meta：dict（kind/model/messages），透传给用量统计埋点。
        """
        wall_budget = float(wall_budget)
        deadline = time.monotonic() + wall_budget
        attempts = int(retry) + 1
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
        """用量统计埋点（post 终态调用一次）。

        usage_store 缺失（测试桩 bot / 未初始化）时静默跳过。API 响应缺
        usage 字段时用 estimate_tokens 兜底估算。缓存字段（命中/未命中/
        推理 tokens）从响应 usage 透传：DeepSeek 用顶层 prompt_cache_hit/
        miss_tokens，OpenAI 系用 prompt_tokens_details.cached_tokens 与
        completion_tokens_details.reasoning_tokens——v2.5.0 的消息布局缓存
        优化效果就靠这几列验证。src 标记数据来源：api=响应实测，est=本地
        估算（可信度分开，不混算）。"""
        store = self.usage_store
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

            def _int(v):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return 0

            p_details = usage.get("prompt_tokens_details") or {}
            c_details = usage.get("completion_tokens_details") or {}
            cache_hit = usage.get("prompt_cache_hit_tokens")
            if cache_hit is None:
                cache_hit = p_details.get("cached_tokens")
            cache_hit = _int(cache_hit)
            cache_miss = usage.get("prompt_cache_miss_tokens")
            if cache_miss is None and prompt_t is not None:
                cache_miss = _int(prompt_t) - cache_hit
            else:
                cache_miss = _int(cache_miss)
            store.record(kind=meta.get("kind"), model=meta.get("model"),
                         prompt_tokens=prompt_t, completion_tokens=completion_t,
                         ok=ok, status=status, latency_ms=latency_ms,
                         cache_hit=cache_hit, cache_miss=cache_miss,
                         reasoning=_int(c_details.get("reasoning_tokens")),
                         total_tokens=usage.get("total_tokens"),
                         src="api" if (usage.get("prompt_tokens") is not None
                                       or usage.get("completion_tokens") is not None)
                         else "est")
        except Exception as e:
            logger.debug(f"[用量] 记录失败: {e}")
