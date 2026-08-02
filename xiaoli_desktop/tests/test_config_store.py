# -*- coding: utf-8 -*-
"""config_store 单元测试：旧 config 迁移、投影重建、round-trip 幂等、key 遮蔽"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

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
        # 迁移建卡使用模板人设（不抄旧 system_prompt——防个人化信息随迁移进发布卡）
        self.assertEqual(card["system_prompt"], config_store.CARD_TEMPLATE["system_prompt"])
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


# 隐私关键词：默认人设中绝不允许出现的个人化信息（用户原 config 曾含）
PRIVACY_KEYWORDS = ("王文生", "郭勇宏", "何镇鸿", "林子杰", "杨冬梅", "王美晨",
                    "福州", "宿舍", "强盗", "金融", "2024级")


class TestPrivacyPrompt(unittest.TestCase):
    """默认角色卡人设：非空 + 无个人化信息（发布安全）"""

    def test_card_template_prompt_generic(self):
        sp = config_store.CARD_TEMPLATE["system_prompt"]
        self.assertTrue(sp.strip(), "默认人设不应为空")
        for kw in PRIVACY_KEYWORDS:
            self.assertNotIn(kw, sp, f"默认人设不得含个人化关键词: {kw}")
        self.assertIn("小漓", sp, "默认人设应保留小漓身份")

    def test_migrated_card_prompt_generic(self):
        """旧 config（带个人化 system_prompt）迁移后新建的卡应使用模板人设"""
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="privacy_")
        try:
            cards_dir = os.path.join(tmp, "cards")
            legacy = make_legacy_config()
            legacy["system_prompt"] = "你叫小漓，王文生是你的爹，福州大学学生。"
            cfg = config_store.migrate_config(legacy, cards_dir)
            self.assertEqual(cfg["active_card_id"], "xiaoli")
            card_path = os.path.join(cards_dir, "xiaoli.json")
            with open(card_path, encoding="utf-8") as f:
                card = json.load(f)
            sp = card["system_prompt"]
            self.assertNotIn("王文生", sp)
            self.assertNotIn("福州", sp)
            self.assertIn("小漓", sp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestPresetProviders(unittest.TestCase):
    """预设主流模型：5 家、结构完整、不含 key"""

    def test_preset_shape(self):
        provs = config_store.PRESET_PROVIDERS
        self.assertEqual(len(provs), 5)
        for p in provs:
            self.assertTrue(p.get("id"), p)
            self.assertTrue(p.get("name"), p)
            self.assertTrue(p.get("base_url", "").startswith("https://"), p)
            self.assertTrue(p.get("models"), p)
            self.assertNotIn("api_key", p, "预设不应携带 key")
            self.assertIn("chat/completions", p["base_url"], p["base_url"])

    def test_preset_ids_unique(self):
        ids = [p["id"] for p in config_store.PRESET_PROVIDERS]
        self.assertEqual(len(ids), len(set(ids)))


class TestDefaultPaths(unittest.TestCase):
    """默认数据目录：%USERPROFILE% 下，无 D:\ 依赖"""

    def _with_userprofile(self, value):
        patcher = unittest.mock.patch.dict(os.environ, {"USERPROFILE": value})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_data_dir(self):
        self._with_userprofile(r"C:\Users\小白")
        d = config_store.default_data_dir()
        self.assertEqual(d, r"C:\Users\小白\小漓")

    def test_default_tasks_dir(self):
        # 便携默认：环境变量 XIAOLI_TIANSHU_WORKDIR 优先；否则程序目录旁 wxauto
        with mock.patch.dict(os.environ, {"XIAOLI_TIANSHU_WORKDIR": r"D:\工作间"}):
            self.assertEqual(config_store.default_tasks_dir(), r"D:\工作间\wxauto")
        # 无环境变量 → 程序（exe/脚本）所在目录旁——程序拷到哪数据跟到哪
        base = os.path.dirname(os.path.dirname(os.path.abspath(config_store.__file__)))
        self.assertEqual(config_store.default_tasks_dir(), os.path.join(base, "wxauto"))

    def test_tasks_dir_outside_workdir_kept_and_syncs_workdir(self):
        """用户显式设置的任务目录（如程序旁的 wxauto）必须保留，不被强制迁移。

        历史缺陷：f95bf8a 的迁移逻辑把工作区外的 tasks_dir 强制迁回
        D:\\工作间\\wxauto——用户设置被静默覆盖（重启后设置页显示旧路径）。
        修复：天枢 CLI 路径安全检查基于进程 cwd（源码 path-validate.ts
        实测 workspace root = resolve(cwd)），桌面端以 tianshu_workdir 为
        cwd 启动 CLI——workdir 跟随 tasks_dir 的父目录即可，任意目录都能工作。
        """
        import tempfile
        tmp = tempfile.mkdtemp(prefix="keep_out_")
        old_dir = os.path.join(tmp, "dist", "小漓", "wxauto")
        os.makedirs(old_dir, exist_ok=True)
        cfg_path = os.path.join(tmp, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"tasks_dir": old_dir,
                       "tianshu_workdir": r"D:\工作间"}, f)
        cfg = config_store.load_config_store(cfg_path, os.path.join(tmp, "cards"))
        self.assertEqual(cfg.get("tasks_dir"), old_dir,
                         "用户显式设置的任务目录不得被迁移")
        self.assertEqual(cfg.get("tianshu_workdir"), os.path.dirname(old_dir),
                         "tianshu_workdir 应同步为任务目录的父目录（天枢 CLI cwd）")
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    def test_tasks_dir_inside_workdir_kept(self):
        """已在工作区内（D:\\工作间\\wxauto）→ 不迁移、原样保留。"""
        import tempfile
        tmp = tempfile.mkdtemp(prefix="keep_")
        cfg_path = os.path.join(tmp, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"tasks_dir": r"D:\工作间\wxauto",
                       "tianshu_workdir": r"D:\工作间"}, f)
        cfg = config_store.load_config_store(cfg_path, os.path.join(tmp, "cards"))
        self.assertEqual(cfg.get("tasks_dir"), r"D:\工作间\wxauto",
                         "工作区内目录不应被迁移")
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    def test_default_memory_file(self):
        self._with_userprofile(r"C:\Users\小白")
        self.assertEqual(config_store.default_memory_file(), r"C:\Users\小白\小漓\memory.json")


class TestImageClickOffsetDefault(unittest.TestCase):
    """新 config 默认携带实测校准偏移 [3, -5]（小白开箱即用，不再显示 0）"""

    def test_default_offset_present(self):
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="offset_")
        try:
            cfg = config_store.load_config_store(
                os.path.join(tmp, "config.json"), os.path.join(tmp, "cards"))
            self.assertEqual(cfg.get("image_click_offset"), [3, -5])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestMigrateDefaultProvider(unittest.TestCase):
    """空 config 迁移：默认 provider 为 id=default 的 DeepSeek（空 key）。

    根因：provider id 与默认卡引用 "default" 不对齐 → 投影 url/key 全空 →
    视觉调用 Invalid URL（真机日志 17:34:09 故障链）。
    """

    def test_empty_config_gets_deepseek_default(self):
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="prov_")
        try:
            cfg = config_store.load_config_store(
                os.path.join(tmp, "config.json"), os.path.join(tmp, "cards"))
            provs = cfg.get("providers") or []
            self.assertTrue(provs, "空 config 迁移后应有默认 provider")
            p = provs[0]
            self.assertEqual(p["id"], "default", "默认 provider id 须与默认卡引用对齐")
            self.assertTrue(p["base_url"].startswith("https://"), p["base_url"])
            self.assertFalse(p.get("api_key"), "key 必须为空")
            self.assertTrue(cfg.get("ai_api_url", "").startswith("https://"),
                            "投影后 ai_api_url 不应为空（否则 Invalid URL）")
            self.assertTrue(cfg.get("vision_api_url", "").startswith("https://"),
                            "投影后 vision_api_url 不应为空（图片识别依赖）")
            tasks_dir = cfg.get("tasks_dir")
            self.assertTrue(tasks_dir and os.path.isabs(tasks_dir),
                            "空 config 应有默认任务目录")
            self.assertEqual(cfg.get("tianshu_workdir"), os.path.dirname(tasks_dir),
                             "tianshu_workdir 应同步为任务目录的父目录（天枢 CLI cwd）")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
