# -*- coding: utf-8 -*-
"""联网搜索：零配置零 key 的抓取式后端链（设计复刻自天枢 web_search/web_fetch）。

后端链 bing → duckduckgo 顺序尝试，第一个返回非空结果的后端获胜，其余跳过：
- cn.bing.com 国内直连可达、无需 API key、返回直接 URL（墙内首选）
- html.duckduckgo.com 海外兜底（国内被墙，墙外/代理用户可用）
两者都是 GET + 正则解析 HTML，无第三方依赖。抓取式搜索没有 SLA——搜索
引擎改版或反爬升级会失效，靠双后端链 + 调用方失败降级兜底（工具结果里
返回失败文本，模型自行向用户说明）。

另含 web_fetch：抓取网页正文纯文本，与 web_search 成对——搜索摘要只有
站点介绍，具体实时数据（气温/雨情等）在网页正文里，模型挑来源后抓来读。
"""
import base64
import html as _html
import re
import urllib.parse

import requests

# 与天枢一致：bing 在链头（国内可达），DDG 海外兜底
DEFAULT_BACKENDS = ("bing", "duckduckgo")
SEARCH_TIMEOUT = 15          # 单后端超时（秒）
MAX_RESULTS = 5              # 喂给模型的条数上限
SNIPPET_MAX_CHARS = 200      # 单条摘要截断（控制 tool 消息体积）

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_HEADERS = {"User-Agent": _BROWSER_UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}


class WebSearchError(Exception):
    """所有后端均请求失败（网络异常/HTTP 错误）——区别于「正常响应但无结果」。"""


class WebFetchError(Exception):
    """网页正文抓取失败（非 http/https、HTTP 错误、二进制内容、无可读正文）。"""


def _strip_html(text):
    """去 HTML 标签 + 反转义实体 + 压缩空白。"""
    text = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", _html.unescape(text)).strip()


def _strip_page(html_text):
    """网页正文提取：先剥 script/style/noscript 整块与注释（其内容是
    JS/CSS，去标签后会当正文残留、把真实数据挤出截断线），再去标签。"""
    html_text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1\s*>",
                       " ", html_text or "")
    html_text = re.sub(r"<!--.*?-->", " ", html_text, flags=re.S)
    return _strip_html(html_text)


def _decode_bing_url(url):
    """还原 Bing /ck/a 跳转包裹的真实 URL（u 参数 = 'a1' 前缀 + base64url）。"""
    if "/ck/a" not in (url or ""):
        return url
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        u = (qs.get("u") or [""])[0]
        if len(u) <= 2:
            return url
        b64 = u[2:].replace("-", "+").replace("_", "/")
        real = base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8", "replace")
        return real if real.startswith("http") else url
    except Exception:
        return url


def _is_bing_internal(url):
    """Bing 内部链接（导航/官网自条目）不是搜索结果，跳过。"""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return True
    return (host.endswith(".bing.com") or host == "bing.com"
            or host.endswith(".microsoft.com") or host == "go.microsoft.com"
            or host == "r.bing.com")


def _search_bing(query, count):
    resp = requests.get("https://cn.bing.com/search", params={"q": query},
                        headers=_HEADERS, timeout=SEARCH_TIMEOUT)
    resp.raise_for_status()
    results = []
    for block in resp.text.split('<li class="b_algo')[1:]:
        if len(results) >= count:
            break
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                      block, re.S)
        if not m:
            continue
        url = _decode_bing_url(m.group(1))
        if _is_bing_internal(url):
            continue
        title = _strip_html(m.group(2))
        if not title:
            continue
        # 摘要取块内第一个 <p>（b_caption/b_lineclamp 等），无则留空
        sm = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = _strip_html(sm.group(1))[:SNIPPET_MAX_CHARS] if sm else ""
        results.append({"title": title, "url": url, "snippet": snippet})
    return results


def _decode_ddg_url(url):
    """还原 DDG 重定向（//duckduckgo.com/l/?uddg=<encoded>）的真实 URL。"""
    if "duckduckgo.com/l/" not in (url or ""):
        return url
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        for key in ("uddg", "u"):
            vals = qs.get(key)
            if vals and str(vals[0]).startswith("http"):
                return vals[0]  # parse_qs 已 unquote
    except Exception:
        pass
    return url


def _search_duckduckgo(query, count):
    resp = requests.get("https://html.duckduckgo.com/html/", params={"q": query},
                        headers=_HEADERS, timeout=SEARCH_TIMEOUT)
    resp.raise_for_status()
    results = []
    for block in resp.text.split('<h2 class="result__title">')[1:]:
        if len(results) >= count:
            break
        m = re.search(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                      block, re.S)
        if not m:
            continue
        url = _decode_ddg_url(m.group(1))
        title = _strip_html(m.group(2))
        if not url.startswith("http") or not title:
            continue
        sm = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        snippet = _strip_html(sm.group(1))[:SNIPPET_MAX_CHARS] if sm else ""
        results.append({"title": title, "url": url, "snippet": snippet})
    return results


_BACKENDS = {
    "bing": _search_bing,
    "duckduckgo": _search_duckduckgo,
}


def web_search(query, count=MAX_RESULTS, backends=DEFAULT_BACKENDS):
    """按后端链顺序搜索，第一个非空结果获胜。返回 [{title, url, snippet}]。

    - 任一后端返回非空结果 → 直接返回（后续后端跳过）
    - 有后端正常响应但无结果、其余异常 → 返回 []（确实没搜到）
    - 所有后端都请求失败（无一正常响应）→ 抛 WebSearchError（搜索不可用）
    """
    errors = []
    responded = 0
    for name in backends:
        fn = _BACKENDS.get(name)
        if fn is None:
            continue
        try:
            results = fn(query, count)
        except Exception as e:
            errors.append(f"{name}: {e}")
            continue
        responded += 1
        if results:
            return results
    if errors and not responded:
        raise WebSearchError("; ".join(errors))
    return []


def format_search_results(query, results):
    """把结果格式化为 tool 消息文本（喂给模型的紧凑列表）。"""
    if not results:
        return f"「{query}」没有搜到结果"
    lines = []
    for i, r in enumerate(results, 1):
        line = f"{i}. {r['title']}"
        if r.get("snippet"):
            line += f"\n   {r['snippet']}"
        line += f"\n   来源: {r['url']}"
        lines.append(line)
    return "\n".join(lines)


# ---------- 网页正文抓取（web_fetch，与 web_search 成对） ----------
# 搜索摘要只有站点介绍，实时数据（气温/雨情/比分等）在网页正文里——
# 模型从搜索结果挑来源后用 web_fetch 抓正文自己读。

FETCH_TIMEOUT = 15         # 抓取超时（秒）
FETCH_MAX_CHARS = 3000     # 喂给模型的正文字数上限（约 1.5K token）


def web_fetch(url, max_chars=FETCH_MAX_CHARS):
    """抓取网页正文纯文本：剥 script/style/标签，截断到 max_chars。

    只接受 http/https 的文本类页面（text/*、xml、json）；二进制内容、
    HTTP 错误、无可读正文（需 JS 渲染的空壳页）抛 WebFetchError——
    调用方把失败文本喂回模型，模型自行换来源或向用户说明。
    """
    u = str(url or "").strip()
    if not re.match(r"^https?://", u, re.I):
        raise WebFetchError(f"仅支持 http/https URL: {u[:80]!r}")
    try:
        resp = requests.get(u, headers=_HEADERS, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise WebFetchError(f"请求失败: {e}") from e
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if ctype and not (ctype.startswith("text/")
                      or "xml" in ctype or "json" in ctype):
        raise WebFetchError(f"非网页内容: {ctype[:60]}")
    # 中文站常见 GBK：HTTP 头未声明时 requests 退回 iso-8859-1，按内容探测纠正
    if (resp.encoding or "").lower() in ("iso-8859-1", "ascii"):
        resp.encoding = resp.apparent_encoding or resp.encoding
    text = _strip_page(resp.text)
    if len(text) < 30:
        raise WebFetchError("页面没有可读正文（可能需要 JS 渲染）")
    return text[:max_chars]
