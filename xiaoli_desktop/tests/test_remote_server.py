# -*- coding: utf-8 -*-
"""B6 远程面板测试：token 逐请求校验、方法白名单、静态白名单、hub 投递。"""
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app.remote_server import (REMOTE_EXCLUDED, RemoteHub,
                                      RemoteServer, remote_allowed)


class DummyBridge:
    """最小桥桩：几个方法覆盖白名单各分支（普通/带参/黑名单/下划线）。"""

    def __init__(self):
        self.remote_hub = RemoteHub()

    def get_status(self):
        return {"ok": True, "state": "idle"}

    def echo(self, a, b=None):
        return {"ok": True, "a": a, "b": b}

    def quit_app(self):
        return {"ok": True}

    def push(self, evt, payload):
        return {"ok": True}

    def _private(self):
        return {"ok": True}


class TestRemoteAllowed(unittest.TestCase):
    def test_whitelist_excludes_blacklist_and_private(self):
        allowed = remote_allowed(DummyBridge())
        self.assertIn("get_status", allowed)
        self.assertIn("echo", allowed)
        self.assertNotIn("quit_app", allowed)
        self.assertNotIn("push", allowed)
        self.assertNotIn("_private", allowed)

    def test_blacklist_covers_infra(self):
        for name in ("stop", "start_push_loop", "notify", "bind_window",
                     "win_hide", "pick_folder", "reveal_path"):
            self.assertIn(name, REMOTE_EXCLUDED)


class TestRemoteHub(unittest.TestCase):
    def test_publish_delivers_to_subscribers(self):
        hub = RemoteHub()
        q = hub.subscribe()
        hub.publish("tick", {"state": "running"})
        evt, payload = q.get(timeout=1)
        self.assertEqual(evt, "tick")
        self.assertEqual(payload, {"state": "running"})

    def test_full_queue_dropped_not_blocked(self):
        hub = RemoteHub()
        q = hub.subscribe()
        for i in range(300):   # 超过 256 上限：慢消费者丢事件不阻塞
            hub.publish("tick", {"i": i})
        self.assertEqual(hub.subscriber_count, 1)

    def test_unsubscribe(self):
        hub = RemoteHub()
        q = hub.subscribe()
        hub.unsubscribe(q)
        hub.publish("tick", {})
        self.assertTrue(q.empty())


class TestRemoteServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.webui = tempfile.mkdtemp()
        with open(os.path.join(cls.webui, "index.html"), "w",
                  encoding="utf-8") as f:
            f.write("<html><head></head><body>xiaoli-remote-test</body></html>")
        with open(os.path.join(cls.webui, "style.css"), "w",
                  encoding="utf-8") as f:
            f.write("body{color:#fff}")
        cls.bridge = DummyBridge()
        cls.server = RemoteServer(cls.bridge, "127.0.0.1", 0, "tok123",
                                  cls.webui)
        cls.port = cls.server._httpd.server_address[1]
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def _url(self, path, token="tok123"):
        sep = "&" if "?" in path else "?"
        return f"http://127.0.0.1:{self.port}{path}{sep}t={token}"

    def _post(self, path, body, token="tok123"):
        req = urllib.request.Request(
            self._url(path, token), method="POST",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-Auth-Token": token})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_ping_requires_token(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/ping")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("无 token 应 401")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_ping_ok(self):
        # ping 是 GET 路由（前端拿它探活），不走方法面
        with urllib.request.urlopen(self._url("/api/ping"), timeout=5) as resp:
            status = resp.status
            data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_method_call_with_args(self):
        status, data = self._post("/api/echo",
                                  {"args": ["你好", {"k": 1}]})
        self.assertEqual(status, 200)
        self.assertEqual(data, {"ok": True, "a": "你好", "b": {"k": 1}})

    def test_method_call_no_args(self):
        status, data = self._post("/api/get_status", {})
        self.assertEqual(status, 200)
        self.assertEqual(data["state"], "idle")

    def test_blacklisted_method_404(self):
        try:
            self._post("/api/quit_app", {})
            self.fail("quit_app 不可远程调用")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_unknown_method_404(self):
        try:
            self._post("/api/no_such", {})
            self.fail("未知方法应 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)

    def test_bad_token_401(self):
        try:
            self._post("/api/get_status", {}, token="wrong")
            self.fail("错 token 应 401")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_index_served_with_token(self):
        with urllib.request.urlopen(self._url("/"), timeout=5) as resp:
            body = resp.read().decode("utf-8")
        self.assertIn("xiaoli-remote-test", body)
        # 远程模式标记：前端靠它区分远程面板与 pywebview 本地窗口
        self.assertIn("window.__XL_REMOTE__", body)

    def test_sse_connect_and_event_delivery(self):
        """SSE：连接即收到 connected 注释行；hub 投递的事件流内可达。"""
        stream = urllib.request.urlopen(self._url("/events"), timeout=5)
        try:
            self.assertEqual(stream.headers.get("Content-Type"),
                             "text/event-stream; charset=utf-8")
            head = stream.read(len(b": connected\n\n"))
            self.assertEqual(head, b": connected\n\n")
            self.bridge.remote_hub.publish("tick", {"state": "running"})
            buf = b""
            while b"\n\n" not in buf:
                buf += stream.read(1)
            self.assertIn(b"event: push", buf)
            self.assertIn(b"tick", buf)
        finally:
            stream.close()

    def test_sse_requires_token(self):
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/events", timeout=5)
            self.fail("无 token 的 SSE 应 401")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_static_served_without_token(self):
        """子资源请求会丢查询串：静态必须公开，否则手机端只有裸 HTML
        （无 CSS/JS，真机表现：默认样式 + 巨大黑图标）。API 仍要令牌。"""
        for path in ("/", "/style.css"):
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
                self.assertEqual(resp.status, 200, path)
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/ping", timeout=5)
            self.fail("API 无 token 应 401")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)

    def test_path_escape_rejected(self):
        try:
            urllib.request.urlopen(
                self._url("/assets/..%2F..%2Fconfig_store.py"), timeout=5)
            body = urllib.request.urlopen(
                self._url("/assets/../remote_server.py"), timeout=5).read()
            self.fail("路径逃逸应被拒")
        except (urllib.error.HTTPError, urllib.error.URLError):
            pass


if __name__ == "__main__":
    unittest.main()
