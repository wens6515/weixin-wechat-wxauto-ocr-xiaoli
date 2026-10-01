# -*- coding: utf-8 -*-
"""回复风格纪律与分段发送契约（回复精简拟人化）。

钉住四件事（改动即测试红）：
1. REPLY_STYLE_RULES 由运行时注入 vision / chat 两条链路，位置**紧跟人设**
   —— 角色卡只管人设，说话纪律不再随卡落盘（老用户的卡改了也管用）；
2. 回复长度物理上限：两条 payload 都带 max_tokens（历史缺陷：chat 链路
   根本没有 max_tokens，降级/触发器回递时模型可以无限长）；
3. 分段规则：换行切段 + 段尾**单个**中文句号剥离（`。。`、`！`、`？`、
   英文句点一律不动）；
4. 段间固定间隔 2s：只有中间才等，首段前、末段后不等。
另覆盖老角色卡的旧沉浸要求迁移（逐字剥离 + 备份 + 幂等）。
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from wechat_bot import (REPLY_MAX_TOKENS, REPLY_SEGMENT_INTERVAL_SECONDS,
                        WeChatBot, _strip_trailing_period)
from xiaoli_app import config_store


class _CaptureWx:
    """捕获 send_text 的假后端"""

    def __init__(self):
        self.sent = []

    def send_text(self, chat, text):
        self.sent.append((chat, text))


def _build_bot(memory_dir, persona="你是小漓，蓝色大肥鱼。"):
    """__new__ 绕过 __init__（不连微信），补齐两条链路用到的依赖。

    记忆走真实 MemoryStore（临时目录、空数据）——`_match_related_memory` /
    `_add_history` 是真实调用；只桩掉 `_important_block` / `_get_history`，
    让「人设 → 风格纪律 → 当前时间 → 用户」的索引断言稳定（含重要记忆与
    历史的完整布局由 test_memory_v2 覆盖）。
    """
    bot = WeChatBot.__new__(WeChatBot)
    bot.memory_file = os.path.join(memory_dir, "memory.json")
    bot.system_prompt = persona
    bot.nickname = "小漓"
    bot.api_url = "https://api.test/v1/chat/completions"
    bot.api_key = "k"
    bot.vision_api_url = bot.api_url
    bot.vision_api_key = "k"
    bot.chat_model = "test-model"
    bot.chat_temperature = 0.7
    bot.chat_top_p = 0.9
    bot.vision_max_tokens = REPLY_MAX_TOKENS
    bot.reply_max_tokens = REPLY_MAX_TOKENS
    bot.api_timeout = 60
    bot._model_lock = threading.RLock()
    bot._memory_lock = threading.RLock()
    bot._deep_count = {}
    bot.memory_deep_enabled = False
    bot.memory_compress_enabled = False
    bot._deep_dir = ""
    bot.memory_db = {}
    bot._important_block = lambda chat_id: None
    bot._get_history = lambda chat_id: []
    bot.wx = _CaptureWx()
    bot._pending_placeholders = {}
    return bot


class _BotCase(unittest.TestCase):
    """需要 bot 的用例基类：每个用例一个临时目录，结束显式清理。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="reply_style_")
        self.addCleanup(self._tmp.cleanup)

    def bot(self, persona="你是小漓，蓝色大肥鱼。"):
        return _build_bot(self._tmp.name, persona)

    @staticmethod
    def capture_payloads(bot):
        """桩掉 _post_chat_completions，返回收集 payload 的列表。"""
        captured = []

        def fake_post(url, headers, payload, timeout, label="api", meta=None):
            captured.append(payload)
            return {"choices": [{"message": {"content": "嗨"}}]}

        bot._post_chat_completions = fake_post
        return captured


class TestStyleRulesConstant(unittest.TestCase):
    def test_style_rules_carry_key_disciplines(self):
        rules = config_store.REPLY_STYLE_RULES
        for token in ("【回复风格纪律】", "默认只回一条", "最多两条", "颜文字",
                      "括号", "不要复述", "markdown", "短叹词"):
            self.assertIn(token, rules, f"风格纪律缺少关键条目: {token}")

    def test_verbosity_example_matches_real_case(self):
        """啰嗦的反面教材必须逐字保留真机案例（三连发 → 一条）。

        真机现象：对方只发「有点想吃烤鱼了」，回复被拆成「诶，大半夜的…」
        「不过说真的…」「明天白天…」三条。示例里必须留着这三条当反面，
        再给一条正面——否则模型没有「合并成一条」的可模仿样本。
        """
        rules = config_store.REPLY_STYLE_RULES
        self.assertIn("1.对方一条消息，你默认只回一条", rules)
        self.assertIn("别学：不过说真的，这个点哪还有店开着呀呆呆", rules)
        self.assertIn("别学：明天白天去吃嘛，你吃你的，我在旁边看着就好，别点我那种", rules)
        self.assertIn("你：大半夜的想烤鱼，你是不是冲着我来的嗷", rules)

    def test_standalone_interjection_example(self):
        """短叹词独立成段——且是唯一该发两条的情况。"""
        rules = config_store.REPLY_STYLE_RULES
        self.assertIn("单独占一行", rules)
        self.assertIn("这是唯一该发两条的情况", rules)
        self.assertIn("别学：诶？改我配置做什么呀呆呆", rules)
        self.assertIn("你：诶？\n你：改哪儿了呀，跟小鱼说说嘛", rules)

    def test_default_persona_has_no_immersion_block(self):
        """纪律已迁出：默认人设（卡模板 / AI_DEFAULTS）不再自带沉浸要求。"""
        self.assertEqual(config_store._DEFAULT_SYSTEM_PROMPT,
                         config_store._DEFAULT_PERSONA)
        for prompt in (config_store.AI_DEFAULTS["system_prompt"],
                       config_store.CARD_TEMPLATE["system_prompt"]):
            self.assertNotIn("【角色沉浸要求】", prompt)
            self.assertNotIn("回复风格纪律", prompt,
                             "纪律只由运行时注入，不得烘焙进卡模板")
        self.assertIn("永远热爱探索", config_store.CARD_TEMPLATE["system_prompt"],
                      "人设本体必须保留（只迁走纪律）")

    def test_reply_max_tokens_constant(self):
        self.assertEqual(REPLY_MAX_TOKENS, 400)
        self.assertEqual(REPLY_SEGMENT_INTERVAL_SECONDS, 2.0)


class TestStyleInjection(_BotCase):
    """两条链路：人设 → 风格纪律 → （重要记忆/历史/相关记忆）→ 当前时间 → 用户。"""

    def test_chat_injects_style_right_after_persona(self):
        bot = self.bot()
        captured = self.capture_payloads(bot)
        bot.call_chat_ai("小明", "在吗")

        msgs = captured[0]["messages"]
        self.assertEqual(msgs[0]["content"], bot.system_prompt, "人设仍是第一条 system")
        self.assertEqual(msgs[1], {"role": "system",
                                   "content": config_store.REPLY_STYLE_RULES},
                         "风格纪律必须紧跟人设（稳定前缀区，缓存不受影响）")
        self.assertIn("当前时间：", msgs[2]["content"], "当前时间仍紧贴用户消息")
        self.assertEqual(msgs[3]["role"], "user")

    def test_vision_injects_style_right_after_persona(self):
        bot = self.bot()
        captured = self.capture_payloads(bot)
        bot.call_vision_api([{"type": "text", "text": "你好"}])

        msgs = captured[0]["messages"]
        self.assertEqual(msgs[0]["content"], bot.system_prompt)
        self.assertEqual(msgs[1]["content"], config_store.REPLY_STYLE_RULES)
        self.assertIn("当前时间：", msgs[2]["content"])
        self.assertEqual(msgs[3]["role"], "user")

    def test_empty_persona_still_gets_style(self):
        """空人设：不插空 system（防 API 400），但纪律照常注入。"""
        bot = self.bot(persona="")
        captured = self.capture_payloads(bot)
        bot.call_vision_api([{"type": "text", "text": "你好"}])

        msgs = captured[0]["messages"]
        self.assertEqual(msgs[0]["content"], config_store.REPLY_STYLE_RULES)
        for m in msgs:
            self.assertTrue(str(m.get("content") or "").strip(),
                            "不得出现空 system 消息")

    def test_sampling_params_unaffected(self):
        """风格块不改变采样参数（温度/top_p 仍由卡与配置决定）。"""
        bot = self.bot()
        captured = self.capture_payloads(bot)
        bot.call_chat_ai("小明", "在吗")
        self.assertEqual(captured[0]["temperature"], bot.chat_temperature)
        self.assertEqual(captured[0]["top_p"], bot.chat_top_p)


class TestMaxTokens(_BotCase):
    def test_chat_payload_has_max_tokens(self):
        bot = self.bot()
        captured = self.capture_payloads(bot)
        bot.call_chat_ai("小明", "在吗")
        self.assertEqual(captured[0]["max_tokens"], REPLY_MAX_TOKENS)

    def test_vision_payload_has_max_tokens(self):
        bot = self.bot()
        captured = self.capture_payloads(bot)
        bot.call_vision_api([{"type": "text", "text": "你好"}])
        self.assertEqual(captured[0]["max_tokens"], bot.vision_max_tokens)

    def test_chat_max_tokens_falls_back_to_constant(self):
        """__new__ 构造（无 __init__）的 bot 也必须有限额，不能压根不发。"""
        bot = self.bot()
        del bot.reply_max_tokens
        captured = self.capture_payloads(bot)
        bot.call_chat_ai("小明", "在吗")
        self.assertEqual(captured[0]["max_tokens"], REPLY_MAX_TOKENS)


class TestTrailingPeriodStrip(unittest.TestCase):
    def test_single_period_stripped(self):
        self.assertEqual(_strip_trailing_period("好吧。"), "好吧")

    def test_double_period_kept(self):
        self.assertEqual(_strip_trailing_period("真的吗。。"), "真的吗。。")

    def test_exclamation_question_kept(self):
        self.assertEqual(_strip_trailing_period("原来是这样！！"), "原来是这样！！")
        self.assertEqual(_strip_trailing_period("在吗？"), "在吗？")

    def test_ascii_period_kept(self):
        self.assertEqual(_strip_trailing_period("看看 www.a.com"),
                         "看看 www.a.com")
        self.assertEqual(_strip_trailing_period("版本 1.2."), "版本 1.2.")

    def test_period_in_middle_kept(self):
        self.assertEqual(_strip_trailing_period("你好。今天不错"),
                         "你好。今天不错")

    def test_period_only_part(self):
        self.assertEqual(_strip_trailing_period("。"), "")


class TestSplitReplyParts(_BotCase):
    def test_newline_split_and_period_strip(self):
        self.assertEqual(self.bot()._split_reply_parts("第一句。\n第二句。"),
                         ["第一句", "第二句"])

    def test_single_newline_splits(self):
        """单换行也分次发送（用户定案：模型用单 \\n 分句不能整段一起发）。"""
        self.assertEqual(self.bot()._split_reply_parts("第一句\n第二句"),
                         ["第一句", "第二句"])

    def test_blank_segments_dropped(self):
        self.assertEqual(self.bot()._split_reply_parts("\n\n开头。\n \n结尾。\n\n"),
                         ["开头", "结尾"])

    def test_blank_only_returns_empty(self):
        self.assertEqual(self.bot()._split_reply_parts("   \n\n  "), [])
        self.assertEqual(self.bot()._split_reply_parts(""), [])

    def test_no_punctuation_loss(self):
        self.assertEqual(self.bot()._split_reply_parts("！原来是这样！！\n好吧。。。"),
                         ["！原来是这样！！", "好吧。。。"])


class TestSegmentInterval(_BotCase):
    def test_interval_only_between_parts(self):
        """3 段 → 2 次等待，且都发生在两段之间（首段前/末段后不等）。"""
        bot = self.bot()
        order = []

        def fake_send(chat, text):
            order.append(("send", text))

        def fake_sleep(seconds):
            order.append(("sleep", seconds))

        bot.wx.send_text = fake_send
        with mock.patch("wechat_bot.time.sleep", side_effect=fake_sleep):
            bot._send_parts("小明", "甲。\n乙\n丙。")

        self.assertEqual(order, [("send", "甲"), ("sleep", 2.0),
                                 ("send", "乙"), ("sleep", 2.0),
                                 ("send", "丙")])

    def test_single_part_no_wait(self):
        bot = self.bot()
        with mock.patch("wechat_bot.time.sleep") as sleeper:
            bot._send_text("就一句话", "小明")
        self.assertEqual(bot.wx.sent, [("小明", "就一句话")])
        sleeper.assert_not_called()

    def test_send_text_waits_between_parts(self):
        bot = self.bot()
        with mock.patch("wechat_bot.time.sleep") as sleeper:
            bot._send_text("甲。\n乙", "小明")
        self.assertEqual(bot.wx.sent, [("小明", "甲"), ("小明", "乙")])
        sleeper.assert_called_once_with(REPLY_SEGMENT_INTERVAL_SECONDS)

    def test_placeholder_single_send_no_wait(self):
        bot = self.bot()
        with mock.patch("wechat_bot.time.sleep") as sleeper:
            bot._send_text("收到任务啦\n稍等一下", "小明", placeholder=True)
        self.assertEqual(bot.wx.sent, [("小明", "收到任务啦\n稍等一下")])
        self.assertEqual(bot._pending_placeholders, {"小明": 1})
        sleeper.assert_not_called()

    def test_blank_text_still_sends_one_message(self):
        """全空白回复：退回原文单条发送，绝不静默吞掉。"""
        bot = self.bot()
        bot._send_text("   ", "小明")
        self.assertEqual(bot.wx.sent, [("小明", "")])


class TestLegacyImmersionMigration(unittest.TestCase):
    """老卡里烘焙的旧沉浸要求：逐字剥离 + 备份 + 幂等。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cards_migrate_")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.legacy = config_store._LEGACY_ROLE_IMMERSION
        self.persona = config_store._DEFAULT_PERSONA

    def _write_card(self, card_id, prompt, name="小漓"):
        path = os.path.join(self.tmp, card_id + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"id": card_id, "name": name, "system_prompt": prompt},
                      f, ensure_ascii=False, indent=2)
        return path

    def _read(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_strips_legacy_block_and_backs_up(self):
        legacy_prompt = self.persona + "\n\n" + self.legacy
        path = self._write_card("xiaoli", legacy_prompt)
        bak = path + ".bak"

        migrated = config_store.strip_legacy_immersion(self.tmp)

        self.assertEqual(migrated, ["xiaoli"])
        self.assertEqual(self._read(path)["system_prompt"], self.persona,
                         "旧块应被逐字剥离，人设本体原样保留")
        self.assertTrue(os.path.exists(bak), "改写前必须留备份")
        self.assertEqual(self._read(bak)["system_prompt"], legacy_prompt,
                         "备份必须是改写前的原件")

    def test_idempotent(self):
        legacy_prompt = self.persona + "\n\n" + self.legacy
        path = self._write_card("xiaoli", legacy_prompt)
        bak = path + ".bak"
        config_store.strip_legacy_immersion(self.tmp)
        with open(bak, "rb") as f:
            first_bak = f.read()

        second = config_store.strip_legacy_immersion(self.tmp)

        self.assertEqual(second, [], "重复迁移必须无动作")
        with open(bak, "rb") as f:
            self.assertEqual(f.read(), first_bak, "备份不得被二次覆盖")

    def test_untouched_when_no_legacy_block(self):
        path = self._write_card("custom", "你是别的角色，随便聊。")
        migrated = config_store.strip_legacy_immersion(self.tmp)
        self.assertEqual(migrated, [])
        self.assertEqual(self._read(path)["system_prompt"], "你是别的角色，随便聊。")
        self.assertFalse(os.path.exists(path + ".bak"), "无需迁移就不写备份")

    def test_migrates_every_card(self):
        self._write_card("a", self.persona + "\n\n" + self.legacy)
        self._write_card("b", self.persona + "\n\n" + self.legacy)
        migrated = config_store.strip_legacy_immersion(self.tmp)
        self.assertEqual(sorted(migrated), ["a", "b"])

    def test_missing_dir_is_noop(self):
        self.assertEqual(
            config_store.strip_legacy_immersion(os.path.join(self.tmp, "nope")), [])


if __name__ == "__main__":
    unittest.main()
