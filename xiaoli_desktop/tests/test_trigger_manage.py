# -*- coding: utf-8 -*-
"""模型侧触发器管理（manage_reminder）测试：
- RemindersStore.modify 字段写回；
- 工具声明三态（off 不声明 / lazy 仅工具 / eager 工具+尾区清单注入）；
- eager 清单只含本聊天活跃条目；
- 工具循环执行回执：list / cancel / modify；归属隔离（别聊天的 id 拒绝）；
  终态拒绝；字段校验（时间格式/过去时间/local 空关键词）。
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wechat_bot import REPLY_MAX_TOKENS, WeChatBot
from xiaoli_app.reminders_store import RemindersStore

CHAT = "林小满"
OTHER = "王文生"


def _make_bot(store=None, manage="off"):
    bot = WeChatBot.__new__(WeChatBot)
    bot.memory_file = os.path.join(tempfile.mkdtemp(), "memory.json")
    bot.system_prompt = "人设"
    bot.nickname = "小漓"
    bot.api_url = "https://api.test/v1/chat/completions"
    bot.api_key = "k"
    bot.vision_api_url = bot.api_url
    bot.vision_api_key = "k"
    bot.chat_model = "test-model"
    bot.chat_temperature = 0.7
    bot.chat_top_p = 0.9
    bot.reply_max_tokens = REPLY_MAX_TOKENS
    bot._model_lock = threading.RLock()
    bot.memory_compress_enabled = False
    bot.memory_deep_enabled = False
    bot.reminders = store
    bot.model_trigger_manage = manage
    bot.web_search_enabled = False
    bot.task_enabled = False
    bot.voice_mode = "off"
    bot.tts_endpoint = ""
    bot.voice_profiles = []
    bot.active_voice_profile_id = ""
    bot._post_chat_completions = mock.Mock(
        return_value={"choices": [{"message": {"content": "好"}}]})
    return bot


def _store():
    return RemindersStore(path=os.path.join(tempfile.mkdtemp(),
                                            "reminders.json"))


def _payload_messages(bot, chat=CHAT):
    bot.call_vision_api([{"type": "text", "text": "hi"}], chat_id=chat)
    return bot._post_chat_completions.call_args.args[2]["messages"]


def _tool_names(bot):
    bot.call_vision_api([{"type": "text", "text": "hi"}], chat_id=CHAT)
    payload = bot._post_chat_completions.call_args.args[2]
    return [t["function"]["name"] for t in payload.get("tools", [])]


def _tool_call_response(name, args):
    return {"choices": [{"message": {"tool_calls": [{
        "id": "call1", "type": "function",
        "function": {"name": name,
                     "arguments": json.dumps(args, ensure_ascii=False)},
    }]}}]}


def _run_tool(bot, args):
    """把 manage_reminder 调用喂进 call_vision_api 工具循环，返回 tool 回执
    文本（第二次补全返回普通文本收尾）。"""
    bot._post_chat_completions = mock.Mock(side_effect=[
        _tool_call_response("manage_reminder", args),
        {"choices": [{"message": {"content": "搞定啦"}}]},
    ])
    bot.call_vision_api([{"type": "text", "text": "hi"}], chat_id=CHAT)
    tool_msgs = [m for m in
                 bot._post_chat_completions.call_args.args[2]["messages"]
                 if m.get("role") == "tool"]
    return tool_msgs[-1]["content"] if tool_msgs else ""


class TestStoreModify(unittest.TestCase):
    def test_modify_updates_fields(self):
        st = _store()
        item = st.add(CHAT, "", time.time() + 3600, "once")
        new_fire = time.time() + 7200
        self.assertTrue(st.modify(item["id"], {"fire_at": new_fire,
                                               "repeat": "daily"}))
        row = next(r for r in st.list() if r["id"] == item["id"])
        self.assertEqual(row["fire_at"], new_fire)
        self.assertEqual(row["repeat"], "daily")

    def test_modify_missing_id_returns_false(self):
        self.assertFalse(_store().modify("不存在", {"repeat": "daily"}))


class TestToolDeclaration(unittest.TestCase):
    def test_off_no_manage_tool(self):
        self.assertNotIn("manage_reminder", _tool_names(_make_bot(manage="off")))

    def test_store_missing_no_manage_tool(self):
        bot = _make_bot(store=None, manage="eager")
        self.assertNotIn("manage_reminder", _tool_names(bot))

    def test_lazy_declares_manage_tool(self):
        bot = _make_bot(store=_store(), manage="lazy")
        names = _tool_names(bot)
        self.assertIn("manage_reminder", names)
        self.assertIn("set_reminder", names)

    def test_eager_injects_listing_with_id(self):
        st = _store()
        item = st.add(CHAT, "", time.time() + 3600, "once")
        msgs = _payload_messages(_make_bot(store=st, manage="eager"))
        listing = [m["content"] for m in msgs
                   if m["role"] == "system" and item["id"] in m.get("content", "")]
        self.assertTrue(listing)

    def test_eager_empty_store_no_injection(self):
        msgs = _payload_messages(_make_bot(store=_store(), manage="eager"))
        self.assertFalse(any("触发器" in m.get("content", "")
                             for m in msgs if m["role"] == "system"))

    def test_eager_listing_excludes_other_chat(self):
        st = _store()
        mine = st.add(CHAT, "", time.time() + 3600, "once")
        st.add(OTHER, "", time.time() + 3600, "once")
        msgs = _payload_messages(_make_bot(store=st, manage="eager"))
        listing = [m["content"] for m in msgs
                   if m["role"] == "system" and "触发器" in m.get("content", "")]
        self.assertEqual(len(listing), 1)
        self.assertIn(mine["id"], listing[0])
        self.assertNotIn(OTHER, listing[0])


class TestToolExecution(unittest.TestCase):
    def test_list_returns_active_ids(self):
        st = _store()
        item = st.add(CHAT, "", time.time() + 3600, "once")
        text = _run_tool(_make_bot(store=st, manage="lazy"),
                         {"action": "list"})
        self.assertIn(item["id"], text)
        self.assertIn("定时", text)

    def test_list_empty(self):
        text = _run_tool(_make_bot(store=_store(), manage="lazy"),
                         {"action": "list"})
        self.assertIn("没有", text)

    def test_cancel_removes_trigger(self):
        st = _store()
        item = st.add(CHAT, "", time.time() + 3600, "once")
        text = _run_tool(_make_bot(store=st, manage="lazy"),
                         {"action": "cancel", "id": item["id"]})
        self.assertIn("已取消", text)
        # cancel = 从存储删除（与设置页删除同一 remove 语义），不留残行
        self.assertNotIn(item["id"], [r["id"] for r in st.list()])

    def test_cancel_rejects_foreign_chat_id(self):
        st = _store()
        foreign = st.add(OTHER, "", time.time() + 3600, "once")
        text = _run_tool(_make_bot(store=st, manage="lazy"),
                         {"action": "cancel", "id": foreign["id"]})
        self.assertIn("没有", text)
        row = next(r for r in st.list() if r["id"] == foreign["id"])
        self.assertTrue(row["enabled"])

    def test_cancel_rejects_terminal(self):
        st = _store()
        item = st.add(CHAT, "", time.time() + 3600, "once")
        st.set_enabled(item["id"], False)
        text = _run_tool(_make_bot(store=st, manage="lazy"),
                         {"action": "cancel", "id": item["id"]})
        self.assertIn("已结束", text)

    def test_modify_time_updates_store(self):
        st = _store()
        item = st.add(CHAT, "", time.time() + 3600, "once")
        new_fire = time.time() + 7200
        text = _run_tool(_make_bot(store=st, manage="lazy"),
                         {"action": "modify", "id": item["id"],
                          "time": time.strftime("%Y-%m-%d %H:%M",
                                                time.localtime(new_fire)),
                          "repeat": "daily"})
        self.assertIn("已修改", text)
        row = next(r for r in st.list() if r["id"] == item["id"])
        self.assertEqual(row["repeat"], "daily")

    def test_modify_rejects_bad_time_format(self):
        st = _store()
        item = st.add(CHAT, "", time.time() + 3600, "once")
        text = _run_tool(_make_bot(store=st, manage="lazy"),
                         {"action": "modify", "id": item["id"],
                          "time": "明天早上"})
        self.assertIn("格式", text)

    def test_modify_rejects_past_time(self):
        st = _store()
        item = st.add(CHAT, "", time.time() + 3600, "once")
        text = _run_tool(_make_bot(store=st, manage="lazy"),
                         {"action": "modify", "id": item["id"],
                          "time": "2020-01-01 08:00"})
        self.assertIn("过去", text)

    def test_modify_condition_empty_keywords_rejected(self):
        st = _store()
        item = st.add_condition(CHAT, "", "https://example.com", "下雨",
                                judge="local", met_keywords=["雨"])
        text = _run_tool(_make_bot(store=st, manage="lazy"),
                         {"action": "modify", "id": item["id"],
                          "met_keywords": []})
        self.assertIn("不能清空", text)

    def test_modify_condition_interval_and_keywords(self):
        st = _store()
        item = st.add_condition(CHAT, "", "https://example.com", "下雨",
                                judge="local", met_keywords=["雨"])
        text = _run_tool(_make_bot(store=st, manage="lazy"),
                         {"action": "modify", "id": item["id"],
                          "met_keywords": ["暴雨", "雷阵雨"],
                          "interval_seconds": 30})
        self.assertIn("已修改", text)
        row = next(r for r in st.list() if r["id"] == item["id"])
        self.assertEqual(row["met_keywords"], ["暴雨", "雷阵雨"])
        self.assertEqual(row["interval_seconds"], 30)

    def test_modify_ignores_url_field(self):
        """URL 不可改：传入 url 字段被忽略（白名单外不写回），其余字段照常。"""
        st = _store()
        item = st.add_condition(CHAT, "", "https://example.com", "下雨",
                                judge="local", met_keywords=["雨"])
        _run_tool(_make_bot(store=st, manage="lazy"),
                  {"action": "modify", "id": item["id"],
                   "url": "https://evil.example.com",
                   "interval_seconds": 45})
        row = next(r for r in st.list() if r["id"] == item["id"])
        self.assertEqual(row["url"], "https://example.com")
        self.assertEqual(row["interval_seconds"], 45)

    def test_off_mode_exec_rejects(self):
        st = _store()
        item = st.add(CHAT, "", time.time() + 3600, "once")
        text = _run_tool(_make_bot(store=st, manage="off"),
                         {"action": "cancel", "id": item["id"]})
        self.assertIn("未开启", text)

    def test_missing_id_hint(self):
        text = _run_tool(_make_bot(store=_store(), manage="lazy"),
                         {"action": "cancel"})
        self.assertIn("id", text)


if __name__ == "__main__":
    unittest.main()
