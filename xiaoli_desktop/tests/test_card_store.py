# -*- coding: utf-8 -*-
"""card_store 单元测试：角色卡 CRUD、校验、导入导出、apply_role 热切换"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app import card_store
from xiaoli_app.card_store import validate_card, save_card, get_card, list_cards, \
    delete_card, duplicate_card, export_card, import_card, card_path


def make_card(**over):
    base = {
        "id": "xiaoli", "name": "小漓", "emoji": "🐟",
        "system_prompt": "你叫小漓，可爱。",
        "nickname": "小漓",
        "chat_provider": "deepseek", "chat_model": "deepseek-chat",
        "vision_provider": "deepseek", "vision_model": "deepseek-vl",
        "classify_provider": "deepseek", "classify_model": "deepseek-chat",
        "temperature": 0.7, "top_p": 0.9,
        "vision_temp": 0.7, "vision_max_tokens": 10000,
        "max_history": 1000,
    }
    base.update(over)
    return base


class TestCardStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="card_test_")
        self.cards_dir = os.path.join(self.tmp, "cards")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_and_get(self):
        card = make_card()
        save_card(self.cards_dir, card)
        got = get_card(self.cards_dir, "xiaoli")
        self.assertEqual(got["name"], "小漓")
        self.assertEqual(got["system_prompt"], "你叫小漓，可爱。")
        self.assertTrue(os.path.isfile(card_path(self.cards_dir, "xiaoli")))

    def test_list_sorted_by_name(self):
        save_card(self.cards_dir, make_card(id="b", name="乙"))
        save_card(self.cards_dir, make_card(id="a", name="甲"))
        names = [c["name"] for c in list_cards(self.cards_dir)]
        # 按 Unicode 码点排序（确定性，无 locale 依赖）
        self.assertEqual(names, sorted(["甲", "乙"]))

    def test_get_missing_returns_none(self):
        self.assertIsNone(get_card(self.cards_dir, "nope"))

    def test_delete(self):
        save_card(self.cards_dir, make_card())
        self.assertTrue(delete_card(self.cards_dir, "xiaoli"))
        self.assertIsNone(get_card(self.cards_dir, "xiaoli"))
        self.assertFalse(delete_card(self.cards_dir, "xiaoli"))

    def test_validate_required_fields(self):
        with self.assertRaises(ValueError):
            validate_card(make_card(id=""))
        with self.assertRaises(ValueError):
            validate_card({k: v for k, v in make_card().items() if k != "name"})
        with self.assertRaises(ValueError):
            validate_card({k: v for k, v in make_card().items() if k != "system_prompt"})

    def test_validate_forbids_keys(self):
        for bad in ("api_key", "key", "secret", "token"):
            with self.assertRaises(ValueError, msg=bad):
                validate_card(make_card(**{bad: "sk-x"}))

    def test_duplicate(self):
        save_card(self.cards_dir, make_card())
        dup = duplicate_card(self.cards_dir, "xiaoli", new_id="xiaoli2")
        self.assertEqual(dup["id"], "xiaoli2")
        self.assertEqual(dup["name"], "小漓")
        self.assertIsNotNone(get_card(self.cards_dir, "xiaoli2"))
        # 原卡不变
        self.assertEqual(get_card(self.cards_dir, "xiaoli")["id"], "xiaoli")

    def test_duplicate_auto_id(self):
        save_card(self.cards_dir, make_card())
        dup = duplicate_card(self.cards_dir, "xiaoli")
        self.assertNotEqual(dup["id"], "xiaoli")
        self.assertIsNotNone(get_card(self.cards_dir, dup["id"]))

    def test_export_import_roundtrip(self):
        save_card(self.cards_dir, make_card())
        data = export_card(self.cards_dir, "xiaoli")
        # 导出无 key 字段
        self.assertNotIn("api_key", data)
        self.assertNotIn("key", str(data))
        # 导入到新目录
        dir2 = os.path.join(self.tmp, "cards2")
        imported = import_card(dir2, data)
        self.assertEqual(imported["id"], "xiaoli")
        self.assertEqual(get_card(dir2, "xiaoli")["system_prompt"], data["system_prompt"])

    def test_import_from_json_string(self):
        card = make_card()
        imported = import_card(self.cards_dir, json.dumps(card, ensure_ascii=False))
        self.assertEqual(imported["id"], "xiaoli")
        self.assertIsNotNone(get_card(self.cards_dir, "xiaoli"))

    def test_import_rejects_key(self):
        bad = make_card(api_key="sk-leak")
        with self.assertRaises(ValueError):
            import_card(self.cards_dir, bad)
        # 未写盘
        self.assertIsNone(get_card(self.cards_dir, "xiaoli"))


class TestApplyRole(unittest.TestCase):
    """AgentBot.apply_role：热切换人格/模型/参数/端点（不连微信，用 __new__ 造实例）"""

    def _make_bot(self):
        from xiaoli_bot import AgentBot
        bot = AgentBot.__new__(AgentBot)
        bot._model_lock = threading.RLock()
        bot.nickname = "小漓"
        bot.system_prompt = "旧人格"
        bot.api_url = "http://old"
        bot.api_key = "old-key"
        bot.vision_api_url = "http://old"
        bot.vision_api_key = "old-key"
        bot.chat_model = "m1"
        bot.vision_model = "v1"
        bot.file_model = "m1"
        bot.chat_temperature = 0.7
        bot.chat_top_p = 0.9
        bot.vision_temp = 0.7
        bot.vision_max_tokens = 10000
        bot.max_history = 1000
        return bot

    def _providers(self):
        return [
            {"id": "deepseek", "name": "DeepSeek",
             "base_url": "https://api.deepseek.com/v1/chat/completions",
             "api_key": "sk-ds-1", "models": ["deepseek-chat"]},
            {"id": "zhipu", "name": "智谱",
             "base_url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
             "api_key": "sk-zp-2", "models": ["glm-4v-plus"]},
        ]

    def test_apply_role_updates_fields(self):
        bot = self._make_bot()
        card = make_card(
            system_prompt="新人格：你是客服",
            nickname="小助",
            chat_model="deepseek-chat",
            vision_model="glm-4v-plus",
            classify_model="deepseek-reasoner",
            temperature=0.2, top_p=0.5, vision_temp=0.1, vision_max_tokens=5000,
            max_history=200,
        )
        bot.apply_role(card, self._providers())
        self.assertEqual(bot.system_prompt, "新人格：你是客服")
        self.assertEqual(bot.nickname, "小助")
        self.assertEqual(bot.chat_model, "deepseek-chat")
        self.assertEqual(bot.vision_model, "glm-4v-plus")
        self.assertEqual(bot.file_model, "deepseek-reasoner")
        self.assertEqual(bot.chat_temperature, 0.2)
        self.assertEqual(bot.chat_top_p, 0.5)
        self.assertEqual(bot.vision_temp, 0.1)
        self.assertEqual(bot.vision_max_tokens, 5000)
        self.assertEqual(bot.max_history, 200)

    def test_apply_role_cross_provider_endpoints(self):
        bot = self._make_bot()
        card = make_card(
            chat_provider="deepseek", chat_model="deepseek-chat",
            vision_provider="zhipu", vision_model="glm-4v-plus",
        )
        bot.apply_role(card, self._providers())
        self.assertEqual(bot.api_url, "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(bot.api_key, "sk-ds-1")
        self.assertEqual(bot.vision_api_url, "https://open.bigmodel.cn/api/paas/v4/chat/completions")
        self.assertEqual(bot.vision_api_key, "sk-zp-2")

    def test_apply_role_unknown_provider_no_crash(self):
        bot = self._make_bot()
        card = make_card(chat_provider="nope", vision_provider="nope")
        bot.apply_role(card, self._providers())
        self.assertEqual(bot.api_url, "")
        self.assertEqual(bot.vision_api_url, "")

    def test_apply_role_strips_provider_prefix(self):
        """RED 复现：模型名带厂商前缀（deepseek:deepseek-v4-flash）直接进
        API payload 会 400——apply_role 必须剥离为纯模型名（deepseek-v4-flash）。"""
        bot = self._make_bot()
        card = make_card(
            chat_provider="deepseek", chat_model="deepseek:deepseek-v4-flash",
            vision_provider="zhipu", vision_model="zhipu:glm-4.6v",
            classify_provider="deepseek", classify_model="deepseek:deepseek-reasoner",
        )
        bot.apply_role(card, self._providers())
        self.assertEqual(bot.chat_model, "deepseek-v4-flash",
                         "chat_model 必须剥离厂商前缀，否则 API 400")
        self.assertEqual(bot.vision_model, "glm-4.6v",
                         "vision_model 必须剥离厂商前缀，否则 API 400")
        self.assertEqual(bot.file_model, "deepseek-reasoner",
                         "classify_model 必须剥离厂商前缀，否则 API 400")


if __name__ == "__main__":
    unittest.main(verbosity=2)
