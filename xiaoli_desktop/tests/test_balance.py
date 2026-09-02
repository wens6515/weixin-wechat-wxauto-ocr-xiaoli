# -*- coding: utf-8 -*-
"""平台余额适配器单测：端点解析 / 不支持平台 / 无 key / HTTP 错误 / 坏 JSON。
全部 mock 网络层，不触真实 API（真实端点联通性在实现时人工探活验证）。"""
import unittest
from unittest import mock

from xiaoli_app import balance


def _resp(status=200, data=None, text=""):
    r = mock.Mock()
    r.status_code = status
    if data is not None:
        r.json = lambda: data
    else:
        r.json = mock.Mock(side_effect=ValueError("not json"))
    r.text = text
    return r


class TestBalanceAdapters(unittest.TestCase):
    def test_is_supported(self):
        self.assertTrue(balance.is_supported("deepseek"))
        self.assertTrue(balance.is_supported("DeepSeek"))  # 大小写归一
        self.assertFalse(balance.is_supported("zhipu"))
        self.assertFalse(balance.is_supported(""))

    def test_deepseek_parse(self):
        r = _resp(200, {"is_available": True, "balance_infos": [
            {"currency": "CNY", "total_balance": "110.81",
             "granted_balance": "0.00"}]})
        with mock.patch.object(balance.requests, "get", return_value=r):
            out = balance.fetch_balance("deepseek", "sk-test")
        self.assertTrue(out["ok"])
        self.assertEqual(out["balance"], 110.81)
        self.assertEqual(out["currency"], "CNY")
        self.assertIsNone(out["error"])

    def test_moonshot_parse(self):
        r = _resp(200, {"code": 0, "data": {"available_balance": "12.34"}})
        with mock.patch.object(balance.requests, "get", return_value=r):
            out = balance.fetch_balance("moonshot", "sk-test")
        self.assertTrue(out["ok"])
        self.assertEqual(out["balance"], 12.34)

    def test_siliconflow_parse(self):
        r = _resp(200, {"code": 200, "data": {"balance": "1.00",
                                              "totalBalance": "10.50"}})
        with mock.patch.object(balance.requests, "get", return_value=r):
            out = balance.fetch_balance("siliconflow", "sk-test")
        self.assertTrue(out["ok"])
        self.assertEqual(out["balance"], 10.50)

    def test_unsupported_platform_honest_error(self):
        out = balance.fetch_balance("zhipu", "sk-test")
        self.assertFalse(out["ok"])
        self.assertIsNone(out["balance"])
        self.assertIn("不支持", out["error"])

    def test_missing_key(self):
        out = balance.fetch_balance("deepseek", "")
        self.assertFalse(out["ok"])
        self.assertIn("Key", out["error"])

    def test_http_error(self):
        with mock.patch.object(balance.requests, "get",
                               return_value=_resp(401, {})):
            out = balance.fetch_balance("deepseek", "bad")
        self.assertFalse(out["ok"])
        self.assertIn("401", out["error"])

    def test_bad_json(self):
        with mock.patch.object(balance.requests, "get",
                               return_value=_resp(200, None, text="")):
            out = balance.fetch_balance("deepseek", "k")
        self.assertFalse(out["ok"])
        self.assertIn("JSON", out["error"])

    def test_unrecognized_payload(self):
        with mock.patch.object(balance.requests, "get",
                               return_value=_resp(200, {"foo": 1})):
            out = balance.fetch_balance("deepseek", "k")
        self.assertFalse(out["ok"])
        self.assertIn("余额字段", out["error"])

    def test_network_exception(self):
        import requests
        with mock.patch.object(balance.requests, "get",
                               side_effect=requests.exceptions.Timeout()):
            out = balance.fetch_balance("deepseek", "k")
        self.assertFalse(out["ok"])
        self.assertIn("Timeout", out["error"])


if __name__ == "__main__":
    unittest.main()
