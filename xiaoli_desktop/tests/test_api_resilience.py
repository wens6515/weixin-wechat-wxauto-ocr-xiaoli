# -*- coding: utf-8 -*-
"""API 韧性单测：_post_chat_completions 重试/预算/Retry-After、
call_chat_ai 友好文案兜底（错误字符串绝不直发好友）、用量埋点终态记录。"""
import json
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import wechat_bot as wb


def fake_resp(status=200, body=None, headers=None):
    if body is None:
        body = {"choices": [{"message": {"content": "ok"}}]}
    return SimpleNamespace(
        status_code=status,
        headers=headers or {},
        text=json.dumps(body),
        json=lambda: body,
    )


def make_bot(**extra):
    bot = wb.WeChatBot.__new__(wb.WeChatBot)
    bot.api_url = "https://api.test/v1/chat/completions"
    bot.api_key = "test-key"
    bot.chat_model = "test-model"
    bot.chat_temperature = 0.7
    bot.chat_top_p = 0.9
    bot.api_retry = 2
    bot.api_timeout = 5
    bot.api_wall_budget = 45
    bot.system_prompt = "你是小漓"
    bot._model_lock = threading.RLock()
    bot._memory_lock = threading.RLock()
    bot._deep_count = {}
    bot.memory_deep_enabled = False
    bot.memory_compress_enabled = False
    bot._deep_dir = ""
    bot._get_history = lambda chat_id: []
    history = []
    bot._add_history = lambda chat_id, role, content: history.append((role, content))
    bot.history = history
    for k, v in extra.items():
        setattr(bot, k, v)
    return bot


class FakeUsageStore:
    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)


class TestPostChatCompletions(unittest.TestCase):
    def test_success_first_try_no_sleep(self):
        bot = make_bot()
        with mock.patch("wechat_bot.requests.post", return_value=fake_resp()) as m, \
                mock.patch("wechat_bot.time.sleep") as ms:
            data = bot._post_chat_completions(bot.api_url, {}, {}, 5)
        self.assertEqual(data["choices"][0]["message"]["content"], "ok")
        self.assertEqual(m.call_count, 1)
        ms.assert_not_called()

    def test_429_retry_then_success(self):
        bot = make_bot()
        responses = [fake_resp(429, headers={"Retry-After": "1"}),
                     fake_resp(429), fake_resp()]
        with mock.patch("wechat_bot.requests.post", side_effect=responses) as m, \
                mock.patch("wechat_bot.time.sleep") as ms:
            data = bot._post_chat_completions(bot.api_url, {}, {}, 5)
        self.assertEqual(data["choices"][0]["message"]["content"], "ok")
        self.assertEqual(m.call_count, 3)
        self.assertEqual(ms.call_count, 2)

    def test_5xx_exhausted_raises_with_status(self):
        bot = make_bot(api_retry=1)
        with mock.patch("wechat_bot.requests.post",
                        side_effect=[fake_resp(500), fake_resp(500)]), \
                mock.patch("wechat_bot.time.sleep"):
            with self.assertRaises(wb.ApiCallError) as ctx:
                bot._post_chat_completions(bot.api_url, {}, {}, 5)
        self.assertEqual(ctx.exception.status, 500)

    def test_4xx_no_retry(self):
        bot = make_bot(api_retry=5)
        with mock.patch("wechat_bot.requests.post",
                        return_value=fake_resp(401, {"error": "bad key"})) as m, \
                mock.patch("wechat_bot.time.sleep") as ms:
            with self.assertRaises(wb.ApiCallError) as ctx:
                bot._post_chat_completions(bot.api_url, {}, {}, 5)
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(m.call_count, 1)
        ms.assert_not_called()

    def test_retry_after_over_giveup_aborts(self):
        bot = make_bot(api_retry=5)
        with mock.patch("wechat_bot.requests.post",
                        return_value=fake_resp(429, headers={"Retry-After": "120"})) as m, \
                mock.patch("wechat_bot.time.sleep") as ms:
            with self.assertRaises(wb.ApiCallError) as ctx:
                bot._post_chat_completions(bot.api_url, {}, {}, 5)
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(m.call_count, 1)
        ms.assert_not_called()

    def test_network_error_retries_then_raises(self):
        bot = make_bot(api_retry=1)
        import requests as _requests
        with mock.patch("wechat_bot.requests.post",
                        side_effect=_requests.exceptions.ConnectionError("refused")), \
                mock.patch("wechat_bot.time.sleep"):
            with self.assertRaises(wb.ApiCallError) as ctx:
                bot._post_chat_completions(bot.api_url, {}, {}, 5)
        self.assertIsNone(ctx.exception.status)

    def test_wall_budget_stops_retries(self):
        bot = make_bot(api_retry=10, api_wall_budget=5)
        # 假时钟：sleep 真实推进 monotonic，预算耗尽逻辑才可测
        fake_time = SimpleNamespace(t=1000.0,
                                    monotonic=lambda: fake_time.t,
                                    sleep=lambda s: setattr(fake_time, "t", fake_time.t + s))
        with mock.patch("wechat_bot.requests.post",
                        return_value=fake_resp(500)) as m, \
                mock.patch.object(wb, "time", fake_time):
            with self.assertRaises(wb.ApiCallError) as ctx:
                bot._post_chat_completions(bot.api_url, {}, {}, 5)
        # 退避 1+2+2s 恰好用满 5s 预算：第 3 次请求后停止，不再消耗剩余重试
        self.assertEqual(m.call_count, 3)
        self.assertIn("预算", str(ctx.exception))

    def test_bad_json_counts_as_retryable(self):
        resp = fake_resp(200, body={"choices": [{"message": {"content": "ok"}}]})
        resp.json = lambda: (_ for _ in ()).throw(ValueError("bad json"))
        bot = make_bot(api_retry=1)
        with mock.patch("wechat_bot.requests.post", side_effect=[resp, fake_resp()]), \
                mock.patch("wechat_bot.time.sleep"):
            data = bot._post_chat_completions(bot.api_url, {}, {}, 5)
        self.assertEqual(data["choices"][0]["message"]["content"], "ok")


class TestFriendlyErrorReply(unittest.TestCase):
    def test_failure_returns_friendly_not_error_string(self):
        bot = make_bot(api_retry=0)
        with mock.patch("wechat_bot.requests.post", return_value=fake_resp(503)):
            reply = bot.call_chat_ai("测试好友", "在吗")
        self.assertIn(reply, wb.FRIENDLY_API_ERROR_REPLIES)
        self.assertNotIn("HTTP", reply)
        # 失败不写历史（错误文案不该被模型下次引用）
        self.assertEqual(bot.history, [])

    def test_choices_empty_returns_friendly(self):
        bot = make_bot(api_retry=0)
        with mock.patch("wechat_bot.requests.post",
                        return_value=fake_resp(200, body={"choices": []})):
            reply = bot.call_chat_ai("测试好友", "在吗")
        self.assertIn(reply, wb.FRIENDLY_API_ERROR_REPLIES)

    def test_success_still_writes_history(self):
        bot = make_bot(api_retry=0)
        with mock.patch("wechat_bot.requests.post", return_value=fake_resp()):
            bot.call_chat_ai("测试好友", "在吗")
        roles = [r for r, _ in bot.history]
        self.assertEqual(roles, ["user", "assistant"])


class TestEmptyModelFallback(unittest.TestCase):
    """chat_model 为空（活跃卡缺失被模板补建）时的兜底：两链路都必须发
    纯名模型，空串/带厂商前缀原样发出必 400（真机实测）。"""

    def test_vision_default_model_is_plain_name(self):
        self.assertNotIn(":", wb.VISION_MODEL_DEFAULT)
        self.assertTrue(wb.VISION_MODEL_DEFAULT)

    def test_chat_empty_model_falls_back(self):
        bot = make_bot(chat_model="")

        def fake_post(url, headers, payload, timeout, label="api", meta=None):
            captured["model"] = payload["model"]
            return {"choices": [{"message": {"content": "好呀"}}]}

        captured = {}
        bot._post_chat_completions = fake_post
        reply = bot.call_chat_ai("测试好友", "在吗")
        self.assertEqual(reply, "好呀")
        self.assertEqual(captured["model"], wb.VISION_MODEL_DEFAULT)

    def test_vision_empty_model_falls_back(self):
        bot = make_bot(chat_model="")
        bot.vision_api_url = "https://api.test/v1/chat/completions"
        bot.vision_api_key = "test-key"
        bot.chat_temperature = 0.7
        bot.vision_max_tokens = 1024

        def fake_post(url, headers, payload, timeout, label="api", meta=None):
            captured["model"] = payload["model"]
            return {"choices": [{"message": {"content": "好的"}}]}

        captured = {}
        bot._post_chat_completions = fake_post
        out = bot.call_vision_api([{"type": "text", "text": "hi"}], chat_id=None)
        self.assertEqual(out["kind"], "text")
        self.assertEqual(captured["model"], wb.VISION_MODEL_DEFAULT)


class TestUsageHook(unittest.TestCase):
    def test_success_records_usage(self):
        store = FakeUsageStore()
        bot = make_bot(api_retry=0, usage_store=store)
        body = {"choices": [{"message": {"content": "你好呀"}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30,
                          "total_tokens": 150}}
        with mock.patch("wechat_bot.requests.post", return_value=fake_resp(200, body)):
            bot.call_chat_ai("测试好友", "在吗")
        self.assertEqual(len(store.records), 1)
        rec = store.records[0]
        self.assertEqual(rec["kind"], "chat")
        self.assertEqual(rec["model"], "test-model")
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["prompt_tokens"], 120)
        self.assertEqual(rec["completion_tokens"], 30)

    def test_missing_usage_estimated_from_content(self):
        store = FakeUsageStore()
        bot = make_bot(api_retry=0, usage_store=store)
        body = {"choices": [{"message": {"content": "好"}}]}
        with mock.patch("wechat_bot.requests.post", return_value=fake_resp(200, body)):
            bot.call_chat_ai("测试好友", "在吗")
        rec = store.records[0]
        self.assertGreater(rec["prompt_tokens"], 0)
        self.assertGreater(rec["completion_tokens"], 0)

    def test_failure_records_error_status(self):
        store = FakeUsageStore()
        bot = make_bot(api_retry=0, usage_store=store)
        with mock.patch("wechat_bot.requests.post", return_value=fake_resp(401)):
            bot.call_chat_ai("测试好友", "在吗")
        rec = store.records[0]
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["status"], 401)

    def test_vision_failure_returns_none_and_records(self):
        store = FakeUsageStore()
        bot = make_bot(api_retry=0, usage_store=store)
        bot.vision_api_url = "https://api.test/v1/chat/completions"
        bot.vision_api_key = "test-key"
        bot.chat_temperature = 0.7
        bot.vision_max_tokens = 1024
        with mock.patch("wechat_bot.requests.post", return_value=fake_resp(500)):
            out = bot.call_vision_api([{"type": "text", "text": "hi"}], chat_id=None)
        self.assertIsNone(out)
        rec = store.records[0]
        self.assertEqual(rec["kind"], "vision")
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["status"], 500)


if __name__ == "__main__":
    unittest.main()
