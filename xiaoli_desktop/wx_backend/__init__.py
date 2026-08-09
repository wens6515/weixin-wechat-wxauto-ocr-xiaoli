# -*- coding: utf-8 -*-
"""微信后端抽象层：协议、注册表、后端选择与自动降级。

设计动机（见 wxauto_new_feasibility.md 第 9 节）：微信自动化没有官方 API，
任何方案都面临版本升级失效风险，且已验证 wxauto/UIA 通道在新版微信
4.1.12 上结构性失效、视觉方案为可行保底。因此把"微信层"（连接/收/发/定位）
从业务逻辑中解耦：业务只面向 WeChatBackend 协议与 WeChatMessage 模型编程，
具体实现（wxauto、视觉、CDP 等）注册进注册表，由 create_backend 选择并
在 auto 模式下自动降级。

用法::

    from wx_backend import create_backend

    backend = create_backend("auto")      # 按注册优先级尝试，全部失败抛异常
    backend = create_backend("visual")    # 显式指定，失败即抛（不降级）
"""
from __future__ import annotations

from typing import Any, Iterator, Protocol, runtime_checkable

from .models import MessageType, WeChatMessage

__all__ = [
    "BackendError",
    "BackendUnavailableError",
    "MessageType",
    "WeChatBackend",
    "WeChatMessage",
    "available_backends",
    "create_backend",
    "register_backend",
    "unregister_backend",
]


class BackendError(RuntimeError):
    """后端层错误基类。"""


class BackendUnavailableError(BackendError):
    """后端不可用（环境不支持/连接失败），可触发 auto 模式降级。"""


@runtime_checkable
class WeChatBackend(Protocol):
    """微信后端协议。所有实现必须满足该接口（可经 isinstance 结构检查）。

    name —— 后端标识（如 "wxauto" / "visual" / "cdp"）

    可选扩展（不在协议方法集中，bot 层用 hasattr 探测，缺省自动降级）：
    - iter_unread_sessions() —— 仅迭代有新消息（未读红圈角标）的会话；
      visual 后端实现（列表区红圈像素检测驱动），wxauto 等无此能力可不实现，
      bot 层探测不到时走旧的"全量 iter_sessions + get_messages"路径。
    """

    name: str

    def connect(self) -> bool:
        """建立与微信的连接。成功返回 True。

        环境不支持或连接失败应抛 BackendUnavailableError（携带可读原因，
        供 auto 降级记录）；幂等——已连接时再次调用应直接返回 True。"""
        ...

    def iter_sessions(self) -> Iterator[str]:
        """迭代当前会话名（群名或联系人名，与 WeChatMessage.chat 一致）。"""
        ...

    def get_messages(
        self, chat: str, limit: int | None = None
    ) -> list[WeChatMessage]:
        """返回会话 chat 的消息列表，全部转换为统一 WeChatMessage。

        limit 限制条数（None=全部）；返回顺序（新→旧或旧→新）由实现
        定义并在实现 docstring 中说明。"""
        ...

    def send_text(self, chat: str, text: str) -> bool:
        """向会话 chat 发送文本。成功返回 True。"""
        ...

    def send_file(self, chat: str, file_path: str) -> bool:
        """向会话 chat 发送文件（本地绝对路径）。成功返回 True。"""
        ...

    def locate_message(self, message: WeChatMessage) -> Any:
        """定位消息在界面上的位置（如控件矩形/屏幕坐标），供点击等操作。

        无法定位返回 None；返回结构由实现定义（视觉方案为坐标区域）。"""
        ...

    def close(self) -> None:
        """释放后端资源（窗口句柄/进程/连接）。可重复调用。"""
        ...


# 注册表：name -> (priority, order, cls)。order 为注册序号，保证同优先级按注册先后。
_REGISTRY: dict[str, tuple[int, int, type[WeChatBackend]]] = {}
_ORDER = 0


def _sorted_entries() -> list[tuple[str, tuple[int, int, type[WeChatBackend]]]]:
    """按 auto 选择优先级排序：priority 升序，同优先级按注册先后。"""
    return sorted(_REGISTRY.items(), key=lambda kv: (kv[1][0], kv[1][1]))


def register_backend(
    name: str, cls: type[WeChatBackend], priority: int = 100
) -> None:
    """注册后端实现。priority 越小 auto 选择时越优先（同优先级按注册先后）。

    重复注册同名后端抛 ValueError。"""
    global _ORDER
    if not isinstance(name, str) or not name:
        raise ValueError("后端名必须为非空字符串")
    if name in _REGISTRY:
        raise ValueError(f"后端已注册: {name!r}")
    _REGISTRY[name] = (priority, _ORDER, cls)
    _ORDER += 1


def unregister_backend(name: str) -> type[WeChatBackend] | None:
    """注销后端（测试清理用）。未注册时返回 None。"""
    entry = _REGISTRY.pop(name, None)
    return entry[2] if entry else None


def available_backends() -> list[str]:
    """按 auto 选择优先级返回后端名列表。"""
    return [name for name, *_ in _sorted_entries()]


def _require_connected(inst: WeChatBackend, name: str) -> None:
    """校验 connect 结果；返回非 True 视为不可用（可降级）。"""
    ok = inst.connect()
    if ok is not True:
        raise BackendUnavailableError(f"{name}: connect() 返回 {ok!r}")


def _register_default_backends() -> None:
    """注册内置后端（幂等：已注册同名后端时跳过）。

    顺序即 auto 降级链：visual（新版微信 4.1.12，唯一通道，无窗口副作用）
    → wxauto（旧版微信兼容路径）。注意：wxauto 的 WeChat() 实例化会触发微信
    窗口恢复到记忆位置（实测新版微信上 connect 失败后窗口 rect 被改动），
    因此 visual 必须排在 wxauto 之前，避免每次连接都被 wxauto 干扰窗口。
    """
    from . import visual_backend
    from . import wxauto_backend
    if "visual" not in _REGISTRY:
        visual_backend.register()
    if "wxauto" not in _REGISTRY and wxauto_backend._WXAUTO_AVAILABLE:
        wxauto_backend.register()


# 模块导入即注册默认后端——create_backend("auto") 开箱即用。
_register_default_backends()


def create_backend(backend: str = "auto", **kwargs: Any) -> WeChatBackend:
    """创建并连接微信后端。

    backend == "auto"：按注册优先级逐个尝试，第一个 connect 成功的后端胜出；
    全部失败抛 BackendUnavailableError（聚合各后端失败原因）。
    显式名称（如 "visual"）：只尝试该后端，失败即抛（不降级——用户明确指定）。

    注意：auto 模式下仅 BackendUnavailableError 触发降级；实现抛出的其它
    异常向上传播，避免实现缺陷被静默掩盖。kwargs 透传给后端构造器。"""
    if backend != "auto":
        entry = _REGISTRY.get(backend)
        if entry is None:
            names = ", ".join(available_backends()) or "（无）"
            raise BackendUnavailableError(
                f"未注册的后端: {backend!r}，可用: {names}")
        inst = entry[2](**kwargs)
        _require_connected(inst, backend)
        return inst

    if not _REGISTRY:
        raise BackendUnavailableError(
            "没有注册任何后端，无法选择（请先 register_backend）")
    failures: list[str] = []
    for name, (_, _, cls) in _sorted_entries():
        try:
            inst = cls(**kwargs)
            _require_connected(inst, name)
            return inst
        except BackendUnavailableError as e:
            failures.append(f"{name}: {e}")
    raise BackendUnavailableError(
        "所有后端均不可用: " + " | ".join(failures))
