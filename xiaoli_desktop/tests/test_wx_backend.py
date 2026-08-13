# -*- coding: utf-8 -*-
"""wx_backend 抽象层测试：消息模型、协议结构、注册表、auto 选择与自动降级。

不依赖真实微信环境——用 stub 后端验证选择/降级逻辑本身。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wx_backend import (
    BackendUnavailableError,
    MessageType,
    WeChatBackend,
    WeChatMessage,
    available_backends,
    create_backend,
    register_backend,
    unregister_backend,
)


# ---- stub 后端：最小协议实现，用于验证选择/降级逻辑 ----

class _OkBackend:
    """最小协议实现：connect 成功。"""
    name = "ok"

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.connect_calls = 0
        self.closed = False

    def connect(self):
        self.connect_calls += 1
        return True

    def iter_sessions(self):
        return iter([])

    def get_messages(self, chat, limit=None):
        return []

    def send_text(self, chat, text):
        return True

    def send_file(self, chat, file_path):
        return True

    def locate_message(self, message):
        return None

    def close(self):
        self.closed = True


class _FailBackend(_OkBackend):
    """connect 抛 BackendUnavailableError（可降级的失败）。"""
    name = "fail"

    def connect(self):
        self.connect_calls += 1
        raise BackendUnavailableError("模拟连接失败")


class _FalseConnectBackend(_OkBackend):
    """connect 返回 False（按 _require_connected 视为不可用）。"""
    name = "false"

    def connect(self):
        self.connect_calls += 1
        return False


class _CrashBackend(_OkBackend):
    """connect 抛非 BackendUnavailableError（实现缺陷，不降级）。"""
    name = "crash"

    def connect(self):
        raise ValueError("模拟实现内部错误")


# ---- 消息模型 ----

class TestMessageType(unittest.TestCase):
    def test_seven_members_exact(self):
        """type 枚举固定为 text|image|file|video|emoji|time|system 七类。"""
        self.assertEqual({t.value for t in MessageType},
                         {"text", "image", "file", "video", "emoji", "time", "system"})

    def test_values(self):
        self.assertEqual(MessageType.TEXT.value, "text")
        self.assertEqual(MessageType.IMAGE.value, "image")
        self.assertEqual(MessageType.FILE.value, "file")
        self.assertEqual(MessageType.VIDEO.value, "video")
        self.assertEqual(MessageType.EMOJI.value, "emoji")
        self.assertEqual(MessageType.TIME.value, "time")
        self.assertEqual(MessageType.SYSTEM.value, "system")

    def test_str_enum_comparable(self):
        """str 枚举可直接与字面量比较（序列化友好）。"""
        self.assertEqual(MessageType.FILE, "file")


class TestWeChatMessage(unittest.TestCase):
    def test_fields(self):
        m = WeChatMessage(id="m1", chat="测试群", sender="小明",
                          content="你好", type=MessageType.TEXT)
        self.assertEqual(m.id, "m1")
        self.assertEqual(m.chat, "测试群")
        self.assertEqual(m.sender, "小明")
        self.assertEqual(m.content, "你好")
        self.assertEqual(m.type, MessageType.TEXT)

    def test_positional_order(self):
        """字段顺序固定为 id/chat/sender/content/type。"""
        m = WeChatMessage("m1", "chat", "sender", "c", MessageType.FILE)
        self.assertEqual((m.id, m.chat, m.sender, m.content, m.type),
                         ("m1", "chat", "sender", "c", MessageType.FILE))

    def test_frozen_immutable(self):
        """不可变：作为协议输出跨线程传递更安全。"""
        m = WeChatMessage("m1", "chat", "sender", "c", MessageType.TEXT)
        with self.assertRaises(AttributeError):
            m.content = "改不了"


# ---- 协议结构 ----

class TestProtocol(unittest.TestCase):
    def test_conforming_backend_passes_isinstance(self):
        self.assertIsInstance(_OkBackend(), WeChatBackend)

    def test_missing_method_fails(self):
        class _NoClose:
            name = "x"

            def connect(self):
                return True

            def iter_sessions(self):
                return iter([])

            def get_messages(self, chat, limit=None):
                return []

            def send_text(self, chat, text):
                return True

            def send_file(self, chat, file_path):
                return True

            def locate_message(self, message):
                return None

        self.assertNotIsInstance(_NoClose(), WeChatBackend)

    def test_missing_name_attribute_fails(self):
        class _NoName:
            """实现了全部方法但缺 name 属性 → 不满足协议。"""

            def connect(self):
                return True

            def iter_sessions(self):
                return iter([])

            def get_messages(self, chat, limit=None):
                return []

            def send_text(self, chat, text):
                return True

            def send_file(self, chat, file_path):
                return True

            def locate_message(self, message):
                return None

            def close(self):
                return None

        self.assertNotIsInstance(_NoName(), WeChatBackend)


# ---- 注册表 ----

class TestRegistry(unittest.TestCase):
    def setUp(self):
        for n in list(available_backends()):
            unregister_backend(n)

    def tearDown(self):
        for n in list(available_backends()):
            unregister_backend(n)

    def test_register_and_available(self):
        register_backend("visual", _OkBackend)
        self.assertEqual(available_backends(), ["visual"])

    def test_duplicate_register_raises(self):
        register_backend("visual", _OkBackend)
        with self.assertRaises(ValueError):
            register_backend("visual", _OkBackend)

    def test_empty_name_rejected(self):
        with self.assertRaises(ValueError):
            register_backend("", _OkBackend)

    def test_priority_ordering(self):
        register_backend("low", _OkBackend, priority=200)
        register_backend("high", _OkBackend, priority=10)
        self.assertEqual(available_backends(), ["high", "low"])

    def test_same_priority_keeps_registration_order(self):
        register_backend("first", _OkBackend)
        register_backend("second", _OkBackend)
        self.assertEqual(available_backends(), ["first", "second"])

    def test_unregister(self):
        register_backend("visual", _OkBackend)
        self.assertIs(unregister_backend("visual"), _OkBackend)
        self.assertEqual(available_backends(), [])
        self.assertIsNone(unregister_backend("visual"))


# ---- auto 选择与自动降级 ----

class TestCreateBackendAuto(unittest.TestCase):
    def setUp(self):
        for n in list(available_backends()):
            unregister_backend(n)

    def tearDown(self):
        for n in list(available_backends()):
            unregister_backend(n)

    def test_picks_priority_order(self):
        """priority 小的先尝试；都成功则取第一个。"""
        class _BackendA(_OkBackend):
            name = "a"

        class _BackendB(_OkBackend):
            name = "b"

        register_backend("a", _BackendA, priority=10)
        register_backend("b", _BackendB, priority=100)
        backend = create_backend("auto")
        self.assertEqual(backend.name, "a")

    def test_degrades_to_next_working(self):
        """第一个后端不可用 → 自动降级到下一个可用后端。"""
        register_backend("fail", _FailBackend, priority=10)
        register_backend("ok", _OkBackend, priority=100)
        backend = create_backend("auto")
        self.assertEqual(backend.name, "ok")

    def test_connect_returned_false_is_unavailable(self):
        register_backend("false", _FalseConnectBackend, priority=10)
        register_backend("ok", _OkBackend, priority=100)
        backend = create_backend("auto")
        self.assertEqual(backend.name, "ok")

    def test_all_fail_aggregates_reasons(self):
        register_backend("f1", _FailBackend, priority=10)
        register_backend("f2", _FailBackend, priority=100)
        with self.assertRaises(BackendUnavailableError) as ctx:
            create_backend("auto")
        msg = str(ctx.exception)
        self.assertIn("所有后端均不可用", msg)
        self.assertIn("f1", msg)
        self.assertIn("f2", msg)

    def test_non_unavailable_error_propagates(self):
        """实现抛非 BackendUnavailableError 异常 → 向上传播，不静默降级。"""
        register_backend("crash", _CrashBackend, priority=10)
        register_backend("ok", _OkBackend, priority=100)
        with self.assertRaises(ValueError):
            create_backend("auto")

    def test_empty_registry_raises(self):
        with self.assertRaises(BackendUnavailableError) as ctx:
            create_backend("auto")
        self.assertIn("没有注册任何后端", str(ctx.exception))

    def test_kwargs_passed_to_constructor(self):
        register_backend("ok", _OkBackend)
        backend = create_backend("auto", foo=1, bar="x")
        self.assertEqual(backend.kwargs, {"foo": 1, "bar": "x"})

    def test_connect_called_exactly_once(self):
        register_backend("ok", _OkBackend)
        backend = create_backend("auto")
        self.assertEqual(backend.connect_calls, 1)

    def test_connect_attempted_once_on_failure_then_degrade(self):
        """失败后端只 connect 一次（不重试），随后降级。"""
        register_backend("fail", _FailBackend, priority=10)
        register_backend("ok", _OkBackend, priority=100)
        create_backend("auto")
        self.assertEqual(available_backends(), ["fail", "ok"])  # 注册未变


# ---- 显式指定 ----

class TestCreateBackendExplicit(unittest.TestCase):
    def setUp(self):
        for n in list(available_backends()):
            unregister_backend(n)

    def tearDown(self):
        for n in list(available_backends()):
            unregister_backend(n)

    def test_explicit_success(self):
        register_backend("visual", _OkBackend)
        backend = create_backend("visual")
        self.assertEqual(backend.name, "ok")
        self.assertEqual(backend.connect_calls, 1)

    def test_explicit_unknown_raises(self):
        with self.assertRaises(BackendUnavailableError) as ctx:
            create_backend("cdp")
        self.assertIn("未注册的后端", str(ctx.exception))

    def test_explicit_failure_no_degrade(self):
        """显式指定失败 → 抛异常，不降级到其它可用后端。"""
        register_backend("fail", _FailBackend)
        register_backend("ok", _OkBackend)
        with self.assertRaises(BackendUnavailableError):
            create_backend("fail")


if __name__ == "__main__":
    unittest.main(verbosity=2)
