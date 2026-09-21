# -*- coding: utf-8 -*-
"""触发器发送路径：发送前必须切到目标会话并验证（真机事故回归）。

真机事故：定时提醒「拿快递」意图发给「林小满」，却落进了「摸鱼集团」。根因是 _send_trigger_reply 直接调 wx.send_text(chat)，
而 visual 后端 send_text 收下 chat 却不用它——只往「当前窗口」的输入框
打字。触发是异步的（闹钟线程入队 → 主循环消费），那一刻窗口停在最后
处理过的会话上，消息就发给了那个人。

本组测试钉住：发送前必须确认窗口已在目标会话；确认不了就不发。
"""
import unittest

from xiaoli_bot import AgentBot


class _FakeWx:
    """最小前端桩：模拟「切换会话会改变标题」的微信行为。"""

    def __init__(self, current="", coord=None, switch_ok=True,
                 resolve_raises=False):
        self._current = current
        self._coord = coord
        self._switch_ok = switch_ok
        self._resolve_raises = resolve_raises
        self.sent = []
        self.switch_calls = []

    def read_title(self, foreground=False):
        return self._current or None

    def resolve_chat_coord(self, chat):
        if self._resolve_raises:
            raise RuntimeError("ocr boom")
        return self._coord

    def _switch_chat(self, chat, force=False):
        self.switch_calls.append(chat)
        if not self._switch_ok:
            return False
        self._current = chat          # 切换成功 → 标题变成目标会话
        return True

    def send_text(self, chat, text):
        self.sent.append((chat, text))
        return True


def _bot(wx):
    bot = AgentBot.__new__(AgentBot)   # 不跑 __init__（会连微信）
    bot.wx = wx
    bot.nickname = "小漓"
    bot._pending_placeholders = {}
    return bot


class TestTriggerSendSwitchesChat(unittest.TestCase):
    def test_sends_without_click_when_already_in_target(self):
        """已在该会话：直接发，不点击（重复点已选中条目会 toggle 取消选中）。"""
        wx = _FakeWx(current="林小满")
        _bot(wx)._send_trigger_reply("五点四十啦，去拿快递", "林小满")
        self.assertEqual([c for c, _ in wx.sent], ["林小满"])
        self.assertEqual(wx.switch_calls, [], "已在该会话时不应触发切换点击")

    def test_switches_then_sends_when_window_is_elsewhere(self):
        """窗口停在别的会话：先切换（标题随之变化 → 验证通过）再发送。"""
        wx = _FakeWx(current="“摸鱼”集团", coord=(255, 179), switch_ok=True)
        _bot(wx)._send_trigger_reply("五点四十啦，去拿快递", "林小满")
        self.assertEqual(wx.switch_calls, ["林小满"])
        self.assertEqual([c for c, _ in wx.sent], ["林小满"])

    def test_aborts_when_cannot_reach_target_chat(self):
        """切不过去（定位失败）→ 一条都不发（fail-closed，防发错人）。"""
        wx = _FakeWx(current="“摸鱼”集团", coord=None, switch_ok=False)
        _bot(wx)._send_trigger_reply("五点四十啦，去拿快递", "林小满")
        self.assertEqual(wx.sent, [], "切不到目标会话时绝不能发送")

    def test_aborts_when_title_verification_fails(self):
        """切完标题仍不是目标（切换无效）→ 不发。"""
        wx = _FakeWx(current="“摸鱼”集团", coord=(255, 179), switch_ok=False)
        _bot(wx)._send_trigger_reply("五点四十啦，去拿快递", "林小满")
        self.assertEqual(wx.sent, [])

    def test_aborts_when_resolver_raises(self):
        """定位过程抛异常 → 不发（异常不得退化成「发到当前窗口」）。"""
        wx = _FakeWx(current="“摸鱼”集团", resolve_raises=True, switch_ok=False)
        _bot(wx)._send_trigger_reply("五点四十啦，去拿快递", "林小满")
        self.assertEqual(wx.sent, [])

    def test_split_into_parts_still_works(self):
        """分段发送语义保留（按换行拆，空段丢弃）。"""
        wx = _FakeWx(current="林小满")
        _bot(wx)._send_trigger_reply("第一句\n\n第二句\n", "林小满")
        self.assertEqual([t for _, t in wx.sent], ["第一句", "第二句"])
