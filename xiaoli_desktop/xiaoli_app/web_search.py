# -*- coding: utf-8 -*-
"""联网搜索：零配置零 key 的多引擎抓取链（并发合并 + 兜底）。

引擎选择由真机探针定案（「福州大学 唐勇」等查询实测）：
- cn.bing 对中文实体查询（人名/机构名）分词失败——「唐勇 福州大学」返回
  「唐朝(历史朝代)」「比亚迪唐」等无关实体卡，加引号/换 mkt/setlang 参数
  均无效；「福州天气」类简单查询则完全正常。2026-07 改版后部分布局
  b_algo 块内 <h2> 消失（标题链接变裸 <a>，见 Tianshu-harness 同款适配），
  解析带新布局兜底。
- 百度、搜狗对人名/机构/错别字查询全部精准命中（百度甚至比必应强），
  国内直连零配置——但百度对自动化访问高频触发「安全验证」页（真机实测
  升级为 302 → wappass 图形验证码，高频环境下基本全灭），不能当唯一源；
  搜狗在真机高频下仍稳定供给。因此主链顺序搜狗 > 必应 > 百度（合并结果
  按此优先级拼接）。
- html.duckduckgo 国内直连被墙，仅代理环境可用。

因此主链 = sogou + bing + baidu 三源并发、按源优先级合并去重（任一源
被验证页拦截/超时只损失该源，其余照常），DDG 降级为三源全空时的顺序
兜底。所有引擎都是 GET + 正则解析 HTML，无第三方依赖。抓取式搜索没有
SLA——搜索引擎改版或反爬升级会失效，靠多源 + 调用方失败降级兜底
（工具结果里返回失败文本，模型自行向用户说明）。

另含 web_fetch：抓取网页正文纯文本，与 web_search 成对——搜索摘要只有
站点介绍，具体实时数据（气温/雨情等）在网页正文里，模型挑来源后抓来读。
真机事故定案：搜狗 /link 重定向返回的是 JS 跳转桩
（window.location.replace("目标URL")），requests 不执行 JS → 剥完标签
零正文 → 误判「无正文」失败。web_fetch 对正文过短的页面自动提取跳转
目标（window.location / meta refresh）跟随再抓（上限 3 跳），根治该场景。
"""
import base64
import html as _html
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests

# 并发主链（按优先级排序：合并结果按此顺序拼接）+ 兜底链（主链全空才试）。
# 搜狗第一：真机高频实测唯一定期供给的源（百度 302 图形验证码、必应逢
# 中文实体查询分词失败），且对中文人名/机构/错别字查询全部精准命中。
DEFAULT_BACKENDS = ("sogou", "bing", "baidu")
FALLBACK_BACKENDS = ("duckduckgo",)
SEARCH_TIMEOUT = 15          # 单引擎超时（秒）；并发墙钟 = 最慢者
MAX_RESULTS = 8              # 合并后喂给模型的条数上限
PER_SOURCE_RESULTS = 5       # 每引擎参与的条数上限
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


def _is_engine_internal(url):
    """搜索引擎内部链接（导航/验证页/站内搜索/官网自条目）不是搜索结果，
    跳过。细则：
    - /link（百度/搜狗）与 /ck/a（必应）是真实结果的重定向入口，不算内部
    - 百度/搜狗只有 www 主域的非重定向路径是内部（导航/站内搜索/安全验证
      页）；baike./zhidao./wenku. 等内容子域是真实结果页，不得误杀
    - 必应/微软域全部按内部处理（真实结果已经 /ck/a 解码还原）
    """
    try:
        u = urllib.parse.urlparse(url or "")
        host = (u.hostname or "").lower()
        path = u.path or "/"
    except ValueError:
        return True
    if "/link" in path or "/ck/a" in path:
        return False
    if host in ("www.baidu.com", "baidu.com", "www.sogou.com", "sogou.com"):
        return True
    return (host == "bing.com" or host.endswith(".bing.com")
            or host.endswith(".microsoft.com") or host == "go.microsoft.com"
            or host == "r.bing.com")


# ---------- 百度 / 搜狗：h3 块通用提取 ----------

def _extract_h3_results(page, base_url, count):
    """百度/搜狗通用解析：按 <h3> 切块，块内第一个 <a> 是标题链接，
    块内纯文本去掉标题与尾部噪声后作摘要。

    两个引擎的结果标题都在 <h3><a href=...> 里（百度 href 多为
    www.baidu.com/link?url= 重定向、搜狗为 /link?url=——requests 跟随
    302，web_fetch 可直接用）；摘要/日期/展示网址跟在标题后的同一容器里。
    """
    positions = [m.start() for m in re.finditer(r"<h3", page or "")]
    results = []
    for i, start in enumerate(positions):
        if len(results) >= count:
            break
        end = positions[i + 1] if i + 1 < len(positions) else min(len(page), start + 4000)
        chunk = page[start:end]
        m = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
        if not m:
            continue
        href = _html.unescape(m.group(1))
        if href.startswith("/"):
            href = urllib.parse.urljoin(base_url, href)
        title = _strip_html(m.group(2))
        if not title or not href.startswith("http") or _is_engine_internal(href):
            continue
        # 摘要：块内纯文本去标题前缀，截掉站点名/网址/日期之后的噪声段
        plain = _strip_html(chunk)
        if plain.startswith(title):
            plain = plain[len(title):]
        snippet = re.split(r"(?:推荐您搜索|点击进入|https?://\S+)", plain)[0]
        results.append({"title": title, "url": href,
                        "snippet": snippet.strip()[:SNIPPET_MAX_CHARS]})
    return results


def _search_baidu(query, count):
    resp = requests.get("https://www.baidu.com/s",
                        params={"wd": query, "rn": 10},
                        headers=_HEADERS, timeout=SEARCH_TIMEOUT)
    resp.raise_for_status()
    # 高频自动化访问会拿到「安全验证」页（200 + 无 h3 结果块）——
    # 解析为空结果即自然降级，由其余引擎补位
    return _extract_h3_results(resp.text, "https://www.baidu.com", count)


def _search_sogou(query, count):
    resp = requests.get("https://www.sogou.com/web", params={"query": query},
                        headers=_HEADERS, timeout=SEARCH_TIMEOUT)
    resp.raise_for_status()
    return _extract_h3_results(resp.text, "https://www.sogou.com", count)


# ---------- 必应（b_algo 解析，与天枢复刻版一致） ----------

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


def _bing_host_internal(url):
    """主机属于 Bing/微软基础设施（图标/导航链接）——新布局兜底扫描用。
    与 _is_engine_internal 不同：/ck/a 包裹的跳转入口在兜底扫描里同样视为
    内部链接（cn.bing 新布局的标题链接是直链，块头 /ck/a 只会是图标）。"""
    try:
        host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    except ValueError:
        return True
    return (host == "bing.com" or host.endswith(".bing.com")
            or host == "microsoft.com" or host.endswith(".microsoft.com"))


def _extract_bing_title_link(block):
    """b_algo 块的标题链接 (raw_href, title_html)。

    旧版：块内 <h2><a>（h2 锚点天然跳过块首 tilk 图标链接）。2026-07 改版
    后部分布局 <h2> 消失（Tianshu-harness 同款适配）：标题链接是块头裸
    <a> 直链——取块头 4KB 内第一个非 Bing/微软主机的 http(s) 链接。"""
    h2 = re.search(r"<h2[^>]*>(.*?)</h2>", block, re.S)
    if h2:
        m = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', h2.group(1), re.S)
        if m:
            return m.group(1), m.group(2)
    head = block[:4096]
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', head, re.S):
        href = _html.unescape(m.group(1))
        if href.startswith("http") and not _bing_host_internal(href):
            return href, m.group(2)
    return None


def _search_bing(query, count):
    resp = requests.get("https://cn.bing.com/search", params={"q": query},
                        headers=_HEADERS, timeout=SEARCH_TIMEOUT)
    resp.raise_for_status()
    results = []
    for block in resp.text.split('<li class="b_algo')[1:]:
        if len(results) >= count:
            break
        link = _extract_bing_title_link(block)
        if not link:
            continue
        url = _decode_bing_url(_html.unescape(link[0]))
        if _is_engine_internal(url):
            continue
        title = _strip_html(link[1])
        if not title:
            continue
        # 摘要取块内第一个 <p>（b_caption/b_lineclamp 等），无则留空
        sm = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        snippet = _strip_html(sm.group(1))[:SNIPPET_MAX_CHARS] if sm else ""
        results.append({"title": title, "url": url, "snippet": snippet})
    return results


# ---------- DuckDuckGo（海外兜底，国内需代理） ----------

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
        url = _decode_ddg_url(_html.unescape(m.group(1)))
        title = _strip_html(m.group(2))
        if not url.startswith("http") or not title:
            continue
        sm = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        snippet = _strip_html(sm.group(1))[:SNIPPET_MAX_CHARS] if sm else ""
        results.append({"title": title, "url": url, "snippet": snippet})
    return results


_BACKENDS = {
    "baidu": _search_baidu,
    "bing": _search_bing,
    "sogou": _search_sogou,
    "duckduckgo": _search_duckduckgo,
}


def _search_parallel(query, per_source, names):
    """并发跑多引擎，按 names 优先级顺序合并去重。

    返回 (合并结果, 错误列表, 正常响应的引擎数)。futures 按 names 序
    result()——全部已并发提交，等待顺序即优先级顺序（墙钟 = 最慢引擎）。
    去重：URL 与标题各设一个已见集合（不同引擎对同一结果的 URL 形式
    可能不同——重定向 vs 直链，标题相同即视为重复）。
    """
    merged, errors = [], []
    responded = 0
    fns = [(n, _BACKENDS[n]) for n in names if n in _BACKENDS]
    if not fns:
        return merged, errors, responded
    with ThreadPoolExecutor(max_workers=len(fns)) as ex:
        futs = {n: ex.submit(fn, query, per_source) for n, fn in fns}
        seen_urls, seen_titles = set(), set()
        for n, _fn in fns:
            try:
                items = futs[n].result()
            except Exception as e:
                errors.append(f"{n}: {e}")
                continue
            responded += 1
            for item in items:
                url = item.get("url") or ""
                title = item.get("title") or ""
                if not url or url in seen_urls or (title and title in seen_titles):
                    continue
                seen_urls.add(url)
                seen_titles.add(title)
                merged.append(dict(item, source=n))
    return merged, errors, responded


def web_search(query, count=MAX_RESULTS, backends=None):
    """并发多引擎搜索 + 按优先级合并去重。返回 [{title, url, snippet, source}]。

    - 主链（baidu/bing/sogou）并发执行，按优先级拼接去重，截到 count 条
    - 主链全部为空（全被拦截/无结果）→ 顺序试兜底链（DDG，代理环境可用）
    - 任一引擎正常响应即视为「搜索可用」：最终非空返回结果，全空返回 []
    - 所有引擎都请求失败（无一正常响应）→ 抛 WebSearchError（搜索不可用）
    """
    names = list(backends) if backends is not None else list(DEFAULT_BACKENDS)
    fb = [b for b in FALLBACK_BACKENDS if b not in names]
    merged, errors, responded = _search_parallel(
        query, min(count, PER_SOURCE_RESULTS), names)
    if not merged:
        for name in fb:
            if name not in _BACKENDS:
                continue
            try:
                items = _BACKENDS[name](query, count)
            except Exception as e:
                errors.append(f"{name}: {e}")
                continue
            responded += 1
            if items:
                merged = [dict(item, source=name) for item in items[:count]]
                break
    if not merged and errors and not responded:
        raise WebSearchError("; ".join(errors))
    return merged[:count]


def format_search_results(query, results):
    """把结果格式化为 tool 消息文本（喂给模型的紧凑列表，带来源引擎）。"""
    if not results:
        return f"「{query}」没有搜到结果"
    lines = []
    for i, r in enumerate(results, 1):
        line = f"{i}. {r['title']}"
        if r.get("source"):
            line += f"（{r['source']}）"
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
FETCH_MAX_HOPS = 3         # 跳转桩跟随上限（搜狗 /link 一跳即达，3 足够）

# 跳转桩目标提取：搜狗 /link 返回 window.location.replace("URL") 桩；
# 通用形式还有 location.href 赋值与 <meta http-equiv="refresh" content="0;url=URL">
# （URL 可能被单引号再包一层，如 content="0;URL='https://…'"）。
_JS_REDIRECT_RE = re.compile(
    r"""(?:window\.)?location(?:\.replace\(\s*|\.href\s*=\s*)["']([^"']+)["']""")
_META_REFRESH_RE = re.compile(
    r"""<meta[^>]+http-equiv\s*=\s*["']?refresh["']?[^>]*"""
    r"""content\s*=\s*["']\s*\d+\s*;\s*url\s*=\s*['\"]?([^"'>]+)""", re.I)


def _extract_redirect_target(html_text):
    """从跳转桩 HTML 提取目标 URL（无桩返回 None）。"""
    m = _JS_REDIRECT_RE.search(html_text or "")
    if not m:
        m = _META_REFRESH_RE.search(html_text or "")
    if not m:
        return None
    target = next((g for g in m.groups() if g), None)
    if not target:
        return None
    return target.strip().strip("'\"").strip()


def web_fetch(url, max_chars=FETCH_MAX_CHARS):
    """抓取网页正文纯文本：剥 script/style/标签，截断到 max_chars。

    只接受 http/https 的文本类页面（text/*、xml、json）；二进制内容、
    HTTP 错误、无可读正文（需 JS 渲染的空壳页）抛 WebFetchError——
    调用方把失败文本喂回模型，模型自行换来源或向用户说明。

    跳转桩跟随（真机事故定案）：搜狗 /link 等重定向返回的是
    window.location.replace("目标URL") 的 JS 桩，requests 不执行 JS——
    剥完标签零正文。正文过短时自动提取桩内跳转目标（含相对 URL 补全）
    跟随再抓，最多 FETCH_MAX_HOPS 跳；无桩仍短才报「无正文」。
    百度/搜狗的 HTTP 302 重定向链由 requests 自动跟随。"""
    u = str(url or "").strip()
    if not re.match(r"^https?://", u, re.I):
        raise WebFetchError(f"仅支持 http/https URL: {u[:80]!r}")
    text = ""
    for hop in range(FETCH_MAX_HOPS):
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
        if len(text) >= 30 or hop + 1 >= FETCH_MAX_HOPS:
            break
        target = _extract_redirect_target(resp.text)
        if not target:
            break
        u = urllib.parse.urljoin(resp.url, target)
        if not re.match(r"^https?://", u, re.I):
            break
    if len(text) < 30:
        raise WebFetchError("页面没有可读正文（可能需要 JS 渲染）")
    return text[:max_chars]


def resolve_redirect(url, timeout=FETCH_TIMEOUT):
    """解析重定向到最终真实 URL（best-effort，不抛异常）。

    用于状态监视创建时把搜狗 /link 等跳转链接换算成真实目标页——轮询一个
    会过期的搜索引擎跳转链接既不稳妥、展示也无意义。requests 自动跟 HTTP
    302；返回体是 JS/meta 跳转桩时手动提目标再跟（最多 2 跳）。任何失败
    都原样返回入参 URL（拿不到最终地址就按原地址轮询）。"""
    u = str(url or "").strip()
    for _hop in range(2):
        try:
            resp = requests.get(u, headers=_HEADERS, timeout=timeout)
        except requests.exceptions.RequestException:
            return u
        final = str(getattr(resp, "url", "") or u)
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if ctype and not (ctype.startswith("text/")
                          or "xml" in ctype or "json" in ctype):
            return final  # 二进制内容：不再解析桩
        if len(_strip_page(resp.text)) < 30:
            target = _extract_redirect_target(resp.text)
            if target:
                nxt = urllib.parse.urljoin(final, target)
                if re.match(r"^https?://", nxt, re.I):
                    u = nxt
                    continue
        return final
    return u
