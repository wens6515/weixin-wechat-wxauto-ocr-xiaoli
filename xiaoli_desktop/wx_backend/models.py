# -*- coding: utf-8 -*-
"""统一微信消息模型。

独立于具体后端实现（wxauto/UIA、视觉、CDP），所有后端必须把原始消息
转换为本模型输出，上层业务只依赖本模型，不感知微信版本差异。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class MessageType(str, enum.Enum):
    """消息类型枚举。值为字符串字面量，可直接序列化/与外部文本比较。

    - text  ：文本消息，content 为正文
    - image ：图片消息，content 为实现提供的描述或占位（文件名/提示）
    - file  ：文件消息，content 为显示文件名
    - time  ：时间分隔消息（微信 UI 的时间标签，无业务内容）
    - system：系统消息（如"你已添加对方为好友""对方撤回了一条消息"）
    """

    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    VIDEO = "video"
    EMOJI = "emoji"
    TIME = "time"
    SYSTEM = "system"


@dataclass(frozen=True)
class WeChatMessage:
    """后端输出的统一消息模型。

    id      —— 消息唯一标识（同一会话内用于去重；实现可用底层消息 id 或派生键）
    chat    —— 会话名（群名或联系人名，与 WeChatBackend.iter_sessions 返回一致）
    sender  —— 发送者：对方消息为发送者昵称；自己发送的消息为 "self"
    content —— 文本正文；image/file 为描述或显示名；time/system 为原始文本
    type    —— MessageType 枚举
    """

    id: str
    chat: str
    sender: str
    content: str
    type: MessageType
