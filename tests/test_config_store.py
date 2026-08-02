# -*- coding: utf-8 -*-
"""config_store 单元测试：旧 config 迁移、投影重建、round-trip 幂等、key 遮蔽"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app import config_store


def make_legacy_config():
    """模拟改造前的 config.json（含任务桥字段）"""
    return {
        "bot_nickname": "小漓",
        "ai_api_url": "https://api.deepseek.com/v1/chat/completions",
        "ai_api_key": "sk-legacy-key-123456",
        "chat_model": "deepseek-chat",
        "chat_temperature": 0.7,
        "chat_top_p": 0.9,
        "vision_model": "glm-4v-plus",
        "vision_temp": 0.7,
        "vision_max_tokens": 10000,
        "vision_prompt": "描述图片",
        "system_prompt": "你叫小漓，可爱。",
        "max_history": 1000,
        "cooldown": 3,
        "api_retry": 2,
        "api_timeout": 60,
        "start_paused": True,
        "memory_file": "memory.json",
        "file_model": "deepseek-chat",
        "file_temp": 1.0,
        "file_max_tokens": 10000,
        "file_prompt": "",
        "file_storage_path": "",
        "image_click_offset": [3, -5],
        "task_enabled": True,
        "tasks_dir": r"D:\工作间\wxauto",
        "tianshu_window_title": "天枢",
        "tianshu_trigger_command": "开始处理",
        "tianshu_poll_interval": 5,
        "file_send_method": "clipboard",
        "listen_hold_seconds": 2,
    }


class TestMigrate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cfg_test_")
        self.cards_dir = os.path.join(self.tmp, "cards")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_legacy_migrate_creates_provider_and_card(self):
        cfg = make_legacy_config()
        out = config_store.migrate_config(cfg, self.cards_dir)
        # provider 从旧端点生成
        self.assertEqual(len(out["providers"]), 1)
        p = out["providers"][0]
        self.assertEqual(p["id"], "default")
        self.assertEqual(p["base_url"], cfg["ai_api_url"])
        self.assertEqual(p["api_key"], cfg["ai_api_key"])
        # 活跃卡指向默认卡
        self.assertEqual(out["active_card_id"], config_store.DEFAULT_CARD_ID)
        # 任务桥字段保留
        self.assertEqual(out["tasks_dir"], r"D:\工作间\wxauto")
        self.assertEqual(out["tianshu_window_title"], "天枢")
        self.assertEqual(out["image_click_offset"], [3, -5])

    def test_migrate_writes_default_card_file(self):
        cfg = make_legacy_config()
        config_store.migrate_config(cfg, self.cards_dir)
        card_path = os.path.join(self.cards_dir, config_store.DEFAULT_CARD_ID + ".json")
        self.assertTrue(os.path.isfile(card_path))
        with open(card_path, "r", encoding="utf-8") as f:
            card = json.load(f)
        self.assertEqual(card["system_prompt"], cfg["system_prompt"])
        self.assertEqual(card["nickname"], cfg["bot_nickname"])
        self.assertEqual(card["chat_model"], cfg["chat_model"])
        self.assertEqual(card["vision_model"], cfg["vision_model"])
        self.assertEqual(card["classify_model"], cfg["file_model"])
        self.assertEqual(card["temperature"], cfg["chat_temperature"])
        self.assertEqual(card["top_p"], cfg["chat_top_p"])
        self.assertEqual(card["vision_temp"], cfg["vision_temp"])
        self.assertEqual(card["max_history"], cfg["max_history"])
        # 卡不存 key
        self.assertNotIn("api_key", card)
        self.assertNotIn("key", str(card))

    def test_migrate_idempotent(self):
        cfg = make_legacy_config()
        out1 = config_store.migrate_config(cfg, self.cards_dir)
        out2 = config_store.migrate_config(out1, self.cards_dir)
        # 已迁移的 config 不再重复建 provider / 覆盖卡
        self.assertEqual(out1["providers"], out2["providers"])
        self.assertEqual(out1["active_card_id"], out2["active_card_id"])

    def test_migrate_already_modern_passthrough(self):
        cfg = make_legacy_config()
        cfg["providers"] = [{"id": "p1", "name": "P", "base_url": "http://x", "api_key": "k", "models": ["m"]}]
        cfg["active_card_id"] = "p1-card"
        out = config_store.migrate_config(cfg, self.cards_dir)
        self.assertEqual(len(out["providers"]), 1)
        self.assertEqual(out["active_card_id"], "p1-card")


class TestProject(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cfg_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _card(self, **over):
        base = {
            "id": "xiaoli", "name": "小漓", "emoji": "🐟",
            "system_prompt": "你叫小漓。",
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

    def _providers(self):
        return [
            {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1/chat/completions",
             "api_key": "sk-ds-1", "models": ["deepseek-chat", "deepseek-vl"]},
            {"id": "zhipu", "name": "智谱", "base_url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
             "api_key": "sk-zp-2", "models": ["glm-4v-plus"]},
        ]

    def test_project_basic_fields(self):
        cfg = {"providers": self._providers(), "active_card_id": "xiaoli",
               "tasks_dir": r"D:\工作间\wxauto", "cooldown": 3}
        out = config_store.project_config(cfg, self._card())
        self.assertEqual(out["ai_api_url"], "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(out["ai_api_key"], "sk-ds-1")
        self.assertEqual(out["chat_model"], "deepseek-chat")
        self.assertEqual(out["system_prompt"], "你叫小漓。")
        self.assertEqual(out["bot_nickname"], "小漓")
        self.assertEqual(out["chat_temperature"], 0.7)  # 引擎读 chat_temperature
        self.assertEqual(out["chat_top_p"], 0.9)
        # 非模型字段保留
        self.assertEqual(out["tasks_dir"], r"D:\工作间\wxauto")

    def test_project_vision_cross_provider(self):
        card = self._card(vision_provider="zhipu", vision_model="glm-4v-plus")
        cfg = {"providers": self._providers(), "active_card_id": "xiaoli"}
        out = config_store.project_config(cfg, card)
        self.assertEqual(out["vision_api_url"], "https://open.bigmodel.cn/api/paas/v4/chat/completions")
        self.assertEqual(out["vision_api_key"], "sk-zp-2")
        self.assertEqual(out["vision_model"], "glm-4v-plus")
        # 聊天仍走 deepseek
        self.assertEqual(out["ai_api_url"], "https://api.deepseek.com/v1/chat/completions")

    def test_project_unknown_provider_falls_back(self):
        card = self._card(chat_provider="nope", vision_provider="nope")
        cfg = {"providers": self._providers(), "active_card_id": "xiaoli"}
        out = config_store.project_config(cfg, card)
        # 未知 provider：ai_api_url 置空，不崩溃
        self.assertEqual(out["ai_api_url"], "")
        self.assertEqual(out["ai_api_key"], "")

    def test_project_classify_uses_chat_endpoint(self):
        card = self._card(classify_model="deepseek-reasoner")
        cfg = {"providers": self._providers(), "active_card_id": "xiaoli"}
        out = config_store.project_config(cfg, card)
        self.assertEqual(out["file_model"], "deepseek-reasoner")
        # 分类沿用聊天端点（ai_api_url 即聊天端点）
        self.assertEqual(out["ai_api_url"], "https://api.deepseek.com/v1/chat/completions")


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cfg_test_")
        self.cfg_path = os.path.join(self.tmp, "config.json")
        self.cards_dir = os.path.join(self.tmp, "cards")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_store_roundtrip_stable(self):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(make_legacy_config(), f, ensure_ascii=False, indent=4)
        cfg1 = config_store.load_config_store(self.cfg_path, self.cards_dir)
        # 迁移完成
        self.assertIn("providers", cfg1)
        self.assertEqual(cfg1["active_card_id"], config_store.DEFAULT_CARD_ID)
        # 投影字段齐全（引擎同构）
        for k in ("ai_api_url", "ai_api_key", "chat_model", "vision_model",
                  "file_model", "system_prompt", "bot_nickname",
                  "chat_temperature", "chat_top_p", "vision_temp", "vision_max_tokens", "max_history"):
            self.assertIn(k, cfg1, k)
        # 任务桥字段不丢
        self.assertEqual(cfg1["tasks_dir"], r"D:\工作间\wxauto")
        self.assertEqual(cfg1["image_click_offset"], [3, -5])
        # 二次加载幂等（不重复迁移、投影稳定）
        cfg2 = config_store.load_config_store(self.cfg_path, self.cards_dir)
        self.assertEqual(cfg1["providers"], cfg2["providers"])
        self.assertEqual(cfg1["ai_api_url"], cfg2["ai_api_url"])
        self.assertEqual(cfg1["vision_api_url"], cfg2["vision_api_url"])


class TestMaskKey(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(config_store.mask_key("sk-abcdef123456"), "sk-***3456")
        self.assertEqual(config_store.mask_key(""), "")
        self.assertEqual(config_store.mask_key("abc"), "***")
        self.assertEqual(config_store.mask_key("abcdefgh"), "***efgh")


if __name__ == "__main__":
    unittest.main(verbosity=2)
