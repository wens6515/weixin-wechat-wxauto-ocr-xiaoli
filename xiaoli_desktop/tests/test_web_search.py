# -*- coding: utf-8 -*-
"""联网搜索单测：bing/DDG HTML 解析、跳转还原、后端链回退与失败语义，
以及 call_vision_api 的 web_search 工具循环（结果回填/任务优先/次数封顶/失败降级）。"""
import base64
import threading
import unittest
from unittest import mock

import wechat_bot as wb
from xiaoli_app import web_search as ws


def _ck_url(real):
    """构造 Bing /ck/a 跳转包裹 URL（u = 'a1' 前缀 + 无填充 base64url）。"""
    return ("https://www.bing.com/ck/a?!&u=a1"
            + base64.urlsafe_b64encode(real.encode()).decode().rstrip("=")
            + "&ntb=1")


BING_HTML = """
<html><body><ol>
<li class="b_algo"><h2><a href="%s" h="ID=SERP,1.1">北京天气</a></h2>
<div class="b_caption"><p class="b_lineclamp4">今天北京晴，气温 25 度</p></div></li>
<li class="b_algo"><h2><a href="https://cn.bing.com/weather" h="ID=SERP,1.2">bing 天气</a></h2><p>内部链接应被过滤</p></li>
<li class="b_algo"><h2><a href="https://example.org/news" h="ID=SERP,1.3">今日新闻</a></h2><p>第二条正常结果</p></li>
</ol></body></html>
""" % _ck_url("https://example.com/weather")

DDG_HTML = """
<html><body>
<div class="result results_links">
<h2 class="result__title"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fnews&amp;rut=abc">头条新闻</a></h2>
<a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">今天发生了一件大事</a>
</div>
</body></html>
"""


def _resp(text, status=200):
    resp = mock.Mock()
    resp.status_code = status
    resp.text = text
    if status >= 400:
        resp.raise_for_status.side_effect = ws.requests.HTTPError(str(status))
    else:
        resp.raise_for_status.return_value = None
    return resp


def _bing_ddg_fake_get(bing_text, ddg_text):
    """按 URL 分流 bing/DDG 的 requests.get 替身。"""
    def fake_get(url, **kwargs):
        if "bing.com" in url:
            return _resp(bing_text)
        return _resp(ddg_text)
    return fake_get


class TestBingParse(unittest.TestCase):
    def test_parse_filters_internal_and_decodes_redirect(self):
        fake = _bing_ddg_fake_get(BING_HTML, "")
        with mock.patch.object(ws.requests, "get", side_effect=fake) as m:
            results = ws.web_search("北京天气", backends=("bing",))
        self.assertEqual(m.call_args.kwargs["params"], {"q": "北京天气"})
        self.assertEqual(len(results), 2)  # bing 内部链接被过滤
        self.assertEqual(results[0]["url"], "https://example.com/weather")
        self.assertEqual(results[0]["title"], "北京天气")
        self.assertEqual(results[0]["snippet"], "今天北京晴，气温 25 度")
        self.assertEqual(results[1]["url"], "https://example.org/news")


class TestDdgParse(unittest.TestCase):
    def test_parse_decodes_uddg_redirect(self):
        fake = _bing_ddg_fake_get("", DDG_HTML)
        with mock.patch.object(ws.requests, "get", side_effect=fake):
            results = ws.web_search("新闻", backends=("duckduckgo",))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://example.com/news")
        self.assertEqual(results[0]["title"], "头条新闻")
        self.assertEqual(results[0]["snippet"], "今天发生了一件大事")


class TestBackendChain(unittest.TestCase):
    def test_bing_error_falls_to_ddg(self):
        def fake_get(url, **kwargs):
            if "bing.com" in url:
                raise ws.requests.ConnectionError("boom")
            return _resp(DDG_HTML)
        with mock.patch.object(ws.requests, "get", side_effect=fake_get):
            results = ws.web_search("q")
        self.assertEqual(results[0]["url"], "https://example.com/news")

    def test_bing_empty_falls_to_ddg(self):
        fake = _bing_ddg_fake_get("<html><body></body></html>", DDG_HTML)
        with mock.patch.object(ws.requests, "get", side_effect=fake):
            results = ws.web_search("q")
        self.assertEqual(results[0]["url"], "https://example.com/news")

    def test_all_backends_error_raises(self):
        with mock.patch.object(ws.requests, "get",
                               side_effect=ws.requests.ConnectionError("boom")):
            with self.assertRaises(ws.WebSearchError):
                ws.web_search("q")

    def test_backend_responded_empty_returns_empty_list(self):
        # bing 正常响应但无结果（其余后端异常）→ 确实没搜到，返回 [] 不抛
        def fake_get(url, **kwargs):
            if "bing.com" in url:
                return _resp("<html><body></body></html>")
            raise ws.requests.ConnectionError("boom")
        with mock.patch.object(ws.requests, "get", side_effect=fake_get):
            self.assertEqual(ws.web_search("q"), [])

    def test_unknown_backend_skipped(self):
        fake = _bing_ddg_fake_get(BING_HTML, "")
        with mock.patch.object(ws.requests, "get", side_effect=fake):
            results = ws.web_search("q", backends=("nope", "bing"))
        self.assertEqual(len(results), 2)


class TestFormat(unittest.TestCase):
    def test_format_with_and_without_results(self):
        text = ws.format_search_results(
            "q", [{"title": "T", "url": "https://u", "snippet": "S"}])
        self.assertIn("1. T", text)
        self.assertIn("S", text)
        self.assertIn("https://u", text)
        self.assertIn("没有搜到结果", ws.format_search_results("q", []))


# ---------------- call_vision_api 的 web_search 工具循环 ----------------

def make_bot():
    bot = wb.WeChatBot.__new__(wb.WeChatBot)
    bot.api_url = "https://api.test/v1/chat/completions"
    bot.api_key = "k"
    bot.chat_model = "test-model"
    bot.chat_temperature = 0.7
    bot.chat_top_p = 0.9
    bot.api_retry = 2
    bot.api_timeout = 5
    bot.api_wall_budget = 45
    bot.system_prompt = "你是小漓"
    bot._model_lock = threading.RLock()
    bot._get_history = lambda chat_id: []
    bot.vision_api_url = bot.api_url
    bot.vision_api_key = bot.api_key
    bot.vision_temp = 0.5
    bot.vision_max_tokens = 100
    return bot


def _search_call(qid="c1", query="北京天气"):
    return {"id": qid, "type": "function",
            "function": {"name": "web_search",
                         "arguments": '{"query": "%s"}' % query}}


class TestVisionSearchLoop(unittest.TestCase):
    def test_search_roundtrip_feeds_results_back(self):
        bot = make_bot()
        payloads = []

        def fake_post(url, headers, payload, timeout, label="api", meta=None):
            payloads.append(payload)
            if len(payloads) == 1:
                return {"choices": [{"message": {
                    "content": "", "tool_calls": [_search_call()]}}]}
            return {"choices": [{"message": {"content": "北京今天晴"}}]}

        bot._post_chat_completions = fake_post
        with mock.patch.object(wb, "web_search", return_value=[
                {"title": "北京天气", "url": "https://u", "snippet": "晴"}]):
            out = bot.call_vision_api([{"type": "text", "text": "hi"}])
        self.assertEqual(out, {"kind": "text", "content": "北京今天晴"})
        self.assertEqual(len(payloads), 2)
        # 首轮 payload 声明了 web_search 工具
        names = [t["function"]["name"] for t in payloads[0]["tools"]]
        self.assertIn("web_search", names)
        # 次轮消息含 assistant(tool_calls) 回填 + tool 结果
        asst = [m for m in payloads[1]["messages"]
                if m.get("role") == "assistant" and m.get("tool_calls")]
        tool_msgs = [m for m in payloads[1]["messages"] if m.get("role") == "tool"]
        self.assertEqual(len(asst), 1)
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["tool_call_id"], "c1")
        self.assertIn("北京天气", tool_msgs[0]["content"])

    def test_dispatch_priority_over_search(self):
        bot = make_bot()
        calls = []

        def fake_post(url, headers, payload, timeout, label="api", meta=None):
            calls.append(1)
            return {"choices": [{"message": {"tool_calls": [
                _search_call(),
                {"id": "c2", "type": "function",
                 "function": {"name": "dispatch_task",
                              "arguments": '{"task": "做网站"}'}},
            ]}}]}

        bot._post_chat_completions = fake_post
        search = mock.Mock()
        with mock.patch.object(wb, "web_search", search):
            out = bot.call_vision_api([{"type": "text", "text": "hi"}])
        self.assertEqual(out["kind"], "tool_call")
        self.assertEqual(out["name"], "dispatch_task")
        self.assertEqual(len(calls), 1)  # 不做搜索往返
        search.assert_not_called()

    def test_loop_cap_returns_none(self):
        bot = make_bot()
        calls = []

        def fake_post(url, headers, payload, timeout, label="api", meta=None):
            calls.append(1)
            return {"choices": [{"message": {"tool_calls": [_search_call()]}}]}

        bot._post_chat_completions = fake_post
        with mock.patch.object(wb, "web_search", return_value=[
                {"title": "t", "url": "https://u", "snippet": ""}]):
            out = bot.call_vision_api([{"type": "text", "text": "hi"}])
        self.assertIsNone(out)
        self.assertEqual(len(calls), wb.VISION_TOOL_ROUNDS)

    def test_search_error_degrades_in_tool_message(self):
        bot = make_bot()
        payloads = []

        def fake_post(url, headers, payload, timeout, label="api", meta=None):
            payloads.append(payload)
            if len(payloads) == 1:
                return {"choices": [{"message": {"tool_calls": [_search_call()]}}]}
            return {"choices": [{"message": {"content": "搜不到，抱歉"}}]}

        bot._post_chat_completions = fake_post
        with mock.patch.object(wb, "web_search",
                               side_effect=ws.WebSearchError("bing: x; duckduckgo: y")):
            out = bot.call_vision_api([{"type": "text", "text": "hi"}])
        self.assertEqual(out["content"], "搜不到，抱歉")
        tool_msgs = [m for m in payloads[1]["messages"] if m.get("role") == "tool"]
        self.assertIn("搜索暂时不可用", tool_msgs[0]["content"])

    def test_bad_arguments_degrade_in_tool_message(self):
        bot = make_bot()
        payloads = []

        def fake_post(url, headers, payload, timeout, label="api", meta=None):
            payloads.append(payload)
            if len(payloads) == 1:
                return {"choices": [{"message": {"tool_calls": [
                    {"id": "c9", "type": "function",
                     "function": {"name": "web_search", "arguments": "not-json"}}]}}]}
            return {"choices": [{"message": {"content": "好"}}]}

        bot._post_chat_completions = fake_post
        search = mock.Mock()
        with mock.patch.object(wb, "web_search", search):
            out = bot.call_vision_api([{"type": "text", "text": "hi"}])
        self.assertEqual(out["content"], "好")
        search.assert_not_called()
        tool_msgs = [m for m in payloads[1]["messages"] if m.get("role") == "tool"]
        self.assertIn("缺少 query", tool_msgs[0]["content"])


# ---------------- web_fetch：网页正文抓取 ----------------

FETCH_HTML = """<html><head><style>.a{color:red}</style></head>
<body><script>var x=1;</script><h1>福州天气</h1>
<!-- comment -->
<p>今天 33℃ 多云，南风 2 级，湿度 71%，空气质量优，适合外出游玩。</p>
<noscript>请开启JS</noscript></body></html>"""


def _fetch_resp(html=FETCH_HTML, status=200, ctype="text/html; charset=utf-8",
                encoding="utf-8"):
    resp = mock.Mock()
    resp.status_code = status
    resp.headers = {"Content-Type": ctype}
    resp.encoding = encoding
    resp.apparent_encoding = None
    resp.text = html
    resp.raise_for_status.return_value = None
    return resp


class TestWebFetch(unittest.TestCase):
    def test_strips_script_style_comments_and_tags(self):
        with mock.patch.object(ws.requests, "get",
                               return_value=_fetch_resp(FETCH_HTML)) as m:
            text = ws.web_fetch("https://example.com/weather")
        self.assertIn("福州天气", text)
        self.assertIn("33℃ 多云", text)
        self.assertNotIn("var x", text)
        self.assertNotIn(".a{", text)
        self.assertNotIn("请开启JS", text)
        self.assertNotIn("comment", text)
        self.assertEqual(m.call_args.args[0], "https://example.com/weather")

    def test_truncates_to_max_chars(self):
        long_html = "<html><body>" + "字" * 5000 + "</body></html>"
        with mock.patch.object(ws.requests, "get",
                               return_value=_fetch_resp(long_html)):
            self.assertEqual(len(ws.web_fetch("https://e.com", max_chars=100)), 100)

    def test_rejects_non_http_url(self):
        with self.assertRaises(ws.WebFetchError):
            ws.web_fetch("ftp://example.com/x")
        with self.assertRaises(ws.WebFetchError):
            ws.web_fetch("file:///C:/x")

    def test_rejects_binary_content_type(self):
        with mock.patch.object(ws.requests, "get",
                               return_value=_fetch_resp(ctype="image/jpeg")):
            with self.assertRaises(ws.WebFetchError):
                ws.web_fetch("https://e.com/a.jpg")

    def test_http_error_raises(self):
        resp = _fetch_resp(FETCH_HTML, status=404)
        resp.raise_for_status.side_effect = ws.requests.HTTPError("404")
        with mock.patch.object(ws.requests, "get", return_value=resp):
            with self.assertRaises(ws.WebFetchError):
                ws.web_fetch("https://e.com/404")

    def test_empty_body_raises(self):
        with mock.patch.object(ws.requests, "get",
                               return_value=_fetch_resp("<html><body> </body></html>")):
            with self.assertRaises(ws.WebFetchError):
                ws.web_fetch("https://e.com/js-page")

    def test_request_exception_raises(self):
        with mock.patch.object(ws.requests, "get",
                               side_effect=ws.requests.ConnectionError("boom")):
            with self.assertRaises(ws.WebFetchError):
                ws.web_fetch("https://e.com/x")


# ---------------- 工具循环：web_fetch 分支 ----------------

class TestVisionFetchLoop(unittest.TestCase):
    def test_fetch_roundtrip_feeds_page_text_back(self):
        bot = make_bot()
        payloads = []

        def fake_post(url, headers, payload, timeout, label="api", meta=None):
            payloads.append(payload)
            if len(payloads) == 1:
                return {"choices": [{"message": {"tool_calls": [
                    {"id": "f1", "type": "function",
                     "function": {"name": "web_fetch",
                                  "arguments": '{"url": "https://www.tianqi.com/fuzhou/today/"}'}},
                ]}}]}
            return {"choices": [{"message": {"content": "福州今天 33℃ 多云"}}]}

        bot._post_chat_completions = fake_post
        fetch = mock.Mock(return_value="福州 33℃ 多云 湿度71%")
        with mock.patch.object(wb, "web_fetch", fetch):
            out = bot.call_vision_api([{"type": "text", "text": "hi"}])
        self.assertEqual(out["content"], "福州今天 33℃ 多云")
        fetch.assert_called_once_with("https://www.tianqi.com/fuzhou/today/")
        tool_msgs = [m for m in payloads[1]["messages"] if m.get("role") == "tool"]
        self.assertEqual(tool_msgs[0]["tool_call_id"], "f1")
        self.assertIn("33℃", tool_msgs[0]["content"])

    def test_search_and_fetch_same_round_both_fed_back(self):
        bot = make_bot()
        payloads = []

        def fake_post(url, headers, payload, timeout, label="api", meta=None):
            payloads.append(payload)
            if len(payloads) == 1:
                return {"choices": [{"message": {"tool_calls": [
                    _search_call("s1", "福州天气"),
                    {"id": "f1", "type": "function",
                     "function": {"name": "web_fetch",
                                  "arguments": '{"url": "https://u"}'}},
                ]}}]}
            return {"choices": [{"message": {"content": "答"}}]}

        bot._post_chat_completions = fake_post
        with mock.patch.object(wb, "web_search", return_value=[]), \
                mock.patch.object(wb, "web_fetch", return_value="页面正文"):
            out = bot.call_vision_api([{"type": "text", "text": "hi"}])
        self.assertEqual(out["content"], "答")
        tool_msgs = [m for m in payloads[1]["messages"] if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 2)
        self.assertIn("没有搜到结果", tool_msgs[0]["content"])
        self.assertEqual(tool_msgs[1]["content"], "页面正文")

    def test_fetch_error_degrades_in_tool_message(self):
        bot = make_bot()
        payloads = []

        def fake_post(url, headers, payload, timeout, label="api", meta=None):
            payloads.append(payload)
            if len(payloads) == 1:
                return {"choices": [{"message": {"tool_calls": [
                    {"id": "f1", "type": "function",
                     "function": {"name": "web_fetch",
                                  "arguments": '{"url": "https://e.com/x"}'}},
                ]}}]}
            return {"choices": [{"message": {"content": "换个说法"}}]}

        bot._post_chat_completions = fake_post
        with mock.patch.object(wb, "web_fetch",
                               side_effect=ws.WebFetchError("页面没有可读正文")):
            out = bot.call_vision_api([{"type": "text", "text": "hi"}])
        self.assertEqual(out["content"], "换个说法")
        tool_msgs = [m for m in payloads[1]["messages"] if m.get("role") == "tool"]
        self.assertIn("网页抓取失败", tool_msgs[0]["content"])


if __name__ == "__main__":
    unittest.main()
