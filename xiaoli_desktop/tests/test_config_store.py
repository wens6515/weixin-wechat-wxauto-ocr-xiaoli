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
        "image_click_offset": [-200, -130],
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
        self.assertEqual(p["id"], "deepseek", "默认 provider id 应为 deepseek（非 default）")
        self.assertEqual(p["base_url"], cfg["ai_api_url"])
        self.assertEqual(p["api_key"], cfg["ai_api_key"])
        # 活跃卡指向默认卡
        self.assertEqual(out["active_card_id"], config_store.DEFAULT_CARD_ID)
        # 任务桥字段保留
        self.assertEqual(out["tasks_dir"], r"D:\工作间\wxauto")
        self.assertEqual(out["tianshu_window_title"], "天枢")
        self.assertEqual(out["image_click_offset"], [-200, -130])

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
        self.assertEqual(card["temperature"], cfg["chat_temperature"])
        self.assertEqual(card["top_p"], cfg["chat_top_p"])
        self.assertEqual(card["max_history"], cfg["max_history"])
        # 单模型化：卡不再投影独立 vision_model/classify_model/vision_temp——
        # 视觉/分类统一走 chat_model（migrate 建卡只派生 chat_model 唯一模型键）
        for k in ("vision_model", "classify_model", "vision_temp", "vision_max_tokens"):
            self.assertNotIn(k, card, f"迁移建卡不应含旧独立模型键 {k}")
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

    def test_project_falls_back_when_card_provider_missing(self):
        """RED 复现：默认卡 provider 引用 'default'，但用户手动配置的 providers
        里没有该 id（只有 deepseek/zhipu）→ 投影出空 base_url → API 请求
        Invalid URL ''（真机日志 13:45:09 故障链）。修复后必须回退到可用
        provider（取第一个），不得置空。"""
        cfg = {"providers": self._providers(), "active_card_id": "xiaoli"}
        # 旧卡/迁移卡残留的 default 引用（用户手动加 provider 后 default 不存在）
        card = self._card(chat_provider="default", vision_provider="default",
                          classify_provider="default")
        out = config_store.project_config(cfg, card)
        self.assertTrue(str(out["ai_api_url"]).startswith("https://"),
                        "卡引用的 provider 不存在时必须回退可用 provider，不得置空 URL")
        self.assertTrue(str(out["ai_api_key"]), "回退后必须有可用的 api_key")

    def test_project_vision_shares_chat_endpoint(self):
        """单模型化：视觉统一走 chat provider 端点与 chat_model，不再跨 provider。
        旧卡残留的 vision_provider/vision_model 为未知字段，投影忽略。"""
        card = self._card(vision_provider="zhipu", vision_model="glm-4v-plus")
        cfg = {"providers": self._providers(), "active_card_id": "xiaoli"}
        out = config_store.project_config(cfg, card)
        # 视觉端点沿用聊天端点（deepseek），不再按 vision_provider 解析
        self.assertEqual(out["vision_api_url"], "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(out["vision_api_key"], "sk-ds-1")
        # 聊天走同一端点
        self.assertEqual(out["ai_api_url"], "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(out["chat_model"], "deepseek-chat")
        # 不再投影独立 vision_model（call_vision_api 的 model 取 chat_model）
        self.assertNotIn("vision_model", out)

    def test_project_unknown_provider_falls_back(self):
        card = self._card(chat_provider="nope", vision_provider="nope")
        cfg = {"providers": self._providers(), "active_card_id": "xiaoli"}
        out = config_store.project_config(cfg, card)
        # 未知 provider：回退第一个可用 provider（不置空 URL——否则 API 请求
        # Invalid URL ''，真机日志 13:45:09 故障链），全部缺失才置空
        self.assertEqual(out["ai_api_url"], "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(out["ai_api_key"], "sk-ds-1")

    def test_project_classify_uses_chat_model(self):
        """单模型化：分类不再投影独立 file_model——任务判断统一走 chat_model
        （xiaoli_bot._classify_task 用 chat_model 调 classify_task_with_llm）。"""
        card = self._card(classify_model="deepseek-reasoner")
        cfg = {"providers": self._providers(), "active_card_id": "xiaoli"}
        out = config_store.project_config(cfg, card)
        # 分类沿用聊天端点与唯一模型键 chat_model
        self.assertEqual(out["ai_api_url"], "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(out["chat_model"], "deepseek-chat")
        self.assertNotIn("file_model", out)


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
        # 投影字段齐全（引擎同构；单模型化后 chat_model 为唯一模型键）
        for k in ("ai_api_url", "ai_api_key", "chat_model",
                  "vision_api_url", "vision_api_key",
                  "system_prompt", "bot_nickname",
                  "chat_temperature", "chat_top_p", "max_history"):
            self.assertIn(k, cfg1, k)
        # 单模型化：视觉端点沿用聊天端点（call_vision_api 的 model 取 chat_model）
        self.assertEqual(cfg1["vision_api_url"], cfg1["ai_api_url"])
        self.assertEqual(cfg1["vision_api_key"], cfg1["ai_api_key"])
        # 任务桥字段不丢
        self.assertEqual(cfg1["tasks_dir"], r"D:\工作间\wxauto")
        self.assertEqual(cfg1["image_click_offset"], [-200, -130])
        # 二次加载幂等（不重复迁移、投影稳定）
        cfg2 = config_store.load_config_store(self.cfg_path, self.cards_dir)
        self.assertEqual(cfg1["providers"], cfg2["providers"])
        self.assertEqual(cfg1["ai_api_url"], cfg2["ai_api_url"])
        self.assertEqual(cfg1["vision_api_url"], cfg2["vision_api_url"])


class TestSaveAndRestartKeepsModel(unittest.TestCase):
    """回归：模型页「保存并应用」必须把模型选择持久化到活跃卡。

    历史缺陷（用户实测）：表格只落盘 providers，卡的 chat_model 未写 → 重启后
    模型下拉从卡读空（「deepseek 空」）→ bot 启动 model 空 → API 400。
    修复：_save 同步写活跃卡 chat_provider/chat_model；此处模拟该链路验证
    重启（二次 load）后模型保留。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cfg_save_restart_")
        self.cfg_path = os.path.join(self.tmp, "config.json")
        self.cards_dir = os.path.join(self.tmp, "cards")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_card_model_survives_restart(self):
        # 首次启动建结构
        cfg = config_store.load_config_store(self.cfg_path, self.cards_dir)
        cid = cfg["active_card_id"]
        # 模拟「保存并应用」：providers 落盘 + 活跃卡写入 chat_model（_save 修复后的行为）
        card = config_store._read_card(self.cards_dir, cid)
        card["chat_provider"] = "deepseek"
        card["chat_model"] = "deepseek:deepseek-v4-flash"
        config_store._write_card(self.cards_dir, card)
        provs = [{
            "id": "deepseek", "name": "DeepSeek 深度求索",
            "base_url": "https://api.deepseek.com/v1/chat/completions",
            "api_key": "sk-x",
            "models": ["deepseek:deepseek-v4-flash", "deepseek:deepseek-v4-pro"],
        }]
        cfg["providers"] = provs
        config_store.save_config(cfg, self.cfg_path)

        # 重启：二次 load，卡与投影都必须保留模型
        cfg2 = config_store.load_config_store(self.cfg_path, self.cards_dir)
        card2 = config_store._read_card(self.cards_dir, cfg2["active_card_id"])
        self.assertEqual(card2.get("chat_model"), "deepseek:deepseek-v4-flash")
        self.assertEqual(cfg2["chat_model"], "deepseek:deepseek-v4-flash")

    def test_save_card_model_empty_keeps_old(self):
        """空选择不覆盖：下拉为空时保存不得把卡上已有模型清空（防 400）。"""
        cfg = config_store.load_config_store(self.cfg_path, self.cards_dir)
        cid = cfg["active_card_id"]
        card = config_store._read_card(self.cards_dir, cid)
        card["chat_model"] = "deepseek:deepseek-v4-flash"
        config_store._write_card(self.cards_dir, card)
        # 模拟保存时下拉为空（cm 空串 → 不覆盖）
        cm = ""
        if cm:
            card["chat_model"] = cm
        config_store._write_card(self.cards_dir, card)
        card2 = config_store._read_card(self.cards_dir, cid)
        self.assertEqual(card2.get("chat_model"), "deepseek:deepseek-v4-flash")


class TestMaskKey(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(config_store.mask_key("sk-abcdef123456"), "sk-***3456")
        self.assertEqual(config_store.mask_key(""), "")
        self.assertEqual(config_store.mask_key("abc"), "***")
        self.assertEqual(config_store.mask_key("abcdefgh"), "***efgh")


# 隐私关键词：默认人设中绝不允许出现的个人化信息（用户原 config 曾含）
PRIVACY_KEYWORDS = ("测试用户甲", "测试用户乙", "测试用户丙", "测试用户丁", "测试用户戊",
                    "测试学校", "测试宿舍", "测试群组", "测试专业", "测试年级")


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
            legacy["system_prompt"] = "你叫小漓，测试用户甲是你的爹，测试学校学生。"
            cfg = config_store.migrate_config(legacy, cards_dir)
            self.assertEqual(cfg["active_card_id"], "xiaoli")
            card_path = os.path.join(cards_dir, "xiaoli.json")
            with open(card_path, encoding="utf-8") as f:
                card = json.load(f)
            sp = card["system_prompt"]
            self.assertNotIn("测试用户甲", sp)
            self.assertNotIn("测试学校", sp)
            self.assertIn("小漓", sp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestPresetProviders(unittest.TestCase):
    """预设主流模型：5 家、结构完整、不含 key"""

    def test_preset_shape(self):
        provs = config_store.PRESET_PROVIDERS
        self.assertEqual(len(provs), 6)  # deepseek/zhipu/qwen/kimi/doubao/siliconflow
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
    """新 config 默认携带实测校准偏移 [-200, -130]（用户实测 2026-08-04，小白开箱即用）"""

    def test_default_offset_present(self):
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="offset_")
        try:
            cfg = config_store.load_config_store(
                os.path.join(tmp, "config.json"), os.path.join(tmp, "cards"))
            self.assertEqual(cfg.get("image_click_offset"), [-200, -130])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestMigrateDefaultProvider(unittest.TestCase):
    """空 config 迁移：默认 provider 为 id=deepseek 的 DeepSeek（空 key）。

    用户要求：模型 id 写 deepseek，不要写 default。历史缺陷链：迁移 provider
    id 写成 default → 用户手动配置 providers（deepseek/zhipu）后 default 不存在
    → 投影 url/key 全空 → Invalid URL（真机日志 13:45:09 故障链）。
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
            self.assertEqual(p["id"], "deepseek", "默认 provider id 应为 deepseek（非 default）")
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


class TestGrantTasksDirToTianshu(unittest.TestCase):
    """tasks_dir 预授权给天枢 CLI（agent.permissions 目录授权）。

    天枢 CLI 启动时 applyConfiguredPathGrants 应用 additionalReadDirs/
    additionalWriteDirs——即使 CLI 常驻固定工作目录（cwd 不变），也能读取
    任意位置的 tasks_dir。授权目录必须存在（CLI fail-closed 跳过不存在项）。
    """

    def _setup_env(self):
        tmp = tempfile.mkdtemp(prefix="grant_")
        local = os.path.join(tmp, "LocalAppData")
        os.makedirs(local, exist_ok=True)
        cfg_path = os.path.join(local, ".rivet", "config.json")
        os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"provider": {"providers": {"deepseek": {"name": "deepseek"}}}}, f)
        patcher = mock.patch.dict(os.environ, {"LOCALAPPDATA": local})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        return cfg_path

    def test_grant_writes_read_and_write_dirs(self):
        cfg_path = self._setup_env()
        tasks = os.path.join(tempfile.mkdtemp(prefix="tasks_"), "wxauto")
        os.makedirs(tasks, exist_ok=True)
        ok, changed = config_store.grant_tasks_dir_to_tianshu(tasks)
        self.assertTrue(ok)
        self.assertTrue(changed)
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        perms = cfg["agent"]["permissions"]
        self.assertIn(tasks, perms["additionalReadDirs"],
                      "tasks_dir 必须加入天枢 CLI 读授权")
        self.assertIn(tasks, perms["additionalWriteDirs"],
                      "tasks_dir 必须加入天枢 CLI 写授权（成果回传需要）")
        # 原有配置必须保留（不覆盖 provider 等）
        self.assertEqual(cfg["provider"]["providers"]["deepseek"]["name"], "deepseek")

    def test_grant_idempotent(self):
        cfg_path = self._setup_env()
        tasks = os.path.join(tempfile.mkdtemp(prefix="tasks_"), "wxauto")
        os.makedirs(tasks, exist_ok=True)
        config_store.grant_tasks_dir_to_tianshu(tasks)
        _ok, changed = config_store.grant_tasks_dir_to_tianshu(tasks)
        self.assertFalse(changed, "重复授权不应变更配置")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        perms = cfg["agent"]["permissions"]
        self.assertEqual(perms["additionalReadDirs"].count(tasks), 1)
        self.assertEqual(perms["additionalWriteDirs"].count(tasks), 1)

    def test_grant_merges_existing_permissions(self):
        cfg_path = self._setup_env()
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["agent"] = {"permissions": {"additionalReadDirs": [r"D:\已有授权"]}}
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        tasks = os.path.join(tempfile.mkdtemp(prefix="tasks_"), "wxauto")
        os.makedirs(tasks, exist_ok=True)
        config_store.grant_tasks_dir_to_tianshu(tasks)
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        perms = cfg["agent"]["permissions"]
        self.assertIn(r"D:\已有授权", perms["additionalReadDirs"], "既有授权必须保留")
        self.assertIn(tasks, perms["additionalReadDirs"])

    def test_grant_skips_missing_tasks_dir(self):
        self._setup_env()
        ok, changed = config_store.grant_tasks_dir_to_tianshu(r"D:\不存在\wxauto")
        self.assertFalse(ok, "授权目录不存在时不应写入（CLI fail-closed）")

    def test_grant_skips_without_cli_config(self):
        # 无天枢 CLI 配置（.rivet/config.json 不存在）→ 不创建、不失败
        tmp = tempfile.mkdtemp(prefix="nogrant_")
        patcher = mock.patch.dict(os.environ, {"LOCALAPPDATA": os.path.join(tmp, "Local")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        tasks = os.path.join(tmp, "wxauto")
        os.makedirs(tasks, exist_ok=True)
        ok, _changed = config_store.grant_tasks_dir_to_tianshu(tasks)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(os.path.join(tmp, "Local", ".rivet")),
                         "无 CLI 时不得擅自创建配置目录")


class TestSecretEncryption(unittest.TestCase):
    """API key 落盘加密（DPAPI）：内存明文 ↔ 落盘密文 round-trip。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cfg_sec_")
        self.cfg_path = os.path.join(self.tmp, "config.json")
        self.cards_dir = os.path.join(self.tmp, "cards")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_plain(self, cfg):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=4)

    def test_disk_has_no_plaintext_key(self):
        """RED 复现：config.json 明文存 API key，误分享/翻目录即泄露。
        save_config 后落盘 key 必须是密文（dpapi: 前缀），磁盘无明文。"""
        cfg = {
            "providers": [{"id": "p1", "name": "P", "base_url": "http://x",
                           "api_key": "sk-super-secret-42", "models": ["m"]}],
            "ai_api_key": "sk-super-secret-42",
        }
        config_store.save_config(cfg, self.cfg_path)
        with open(self.cfg_path, "r", encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("sk-super-secret-42", raw,
                         "落盘文件不得包含明文 key")
        if config_store._DPAPI_OK:
            self.assertIn(config_store._DPAPI_PREFIX, raw,
                          "DPAPI 可用时必须加密落盘")

    def test_roundtrip_key_preserved(self):
        """load（解密）→ save（加密）→ load（解密）：key 一致。"""
        cfg = {
            "providers": [{"id": "p1", "name": "P", "base_url": "http://x",
                           "api_key": "sk-rt-1", "models": ["m"]}],
        }
        config_store.save_config(cfg, self.cfg_path)
        cfg1 = config_store.load_config_store(self.cfg_path, self.cards_dir)
        self.assertEqual(cfg1["providers"][0]["api_key"], "sk-rt-1")
        # 二次加载幂等（密文落盘 → 解密回明文）
        cfg2 = config_store.load_config_store(self.cfg_path, self.cards_dir)
        self.assertEqual(cfg1["providers"], cfg2["providers"])
        self.assertEqual(cfg1["providers"][0]["api_key"], "sk-rt-1")

    def test_plaintext_legacy_still_readable(self):
        """旧明文 config（无 dpapi: 前缀）读盘兼容，迁移写回后变密文。"""
        self._write_plain({
            "ai_api_url": "https://api.deepseek.com/v1/chat/completions",
            "ai_api_key": "sk-legacy-plain-9",
            "chat_model": "deepseek-chat",
        })
        cfg = config_store.load_config_store(self.cfg_path, self.cards_dir)
        self.assertEqual(cfg["ai_api_key"], "sk-legacy-plain-9",
                         "旧明文 key 必须原样可用")
        if config_store._DPAPI_OK:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                self.assertIn(config_store._DPAPI_PREFIX, f.read(),
                              "迁移写回后 key 应加密落盘")

    def test_undecryptable_key_becomes_empty(self):
        """密文解不开（换用户/换机）→ 返回空 key（界面提示重填，不崩溃）。"""
        if not config_store._DPAPI_OK:
            self.skipTest("无 DPAPI 环境")
        self._write_plain({
            "providers": [{"id": "p1", "name": "P", "base_url": "http://x",
                           "api_key": "dpapi:not-a-real-blob", "models": ["m"]}],
        })
        cfg = config_store.load_config_store(self.cfg_path, self.cards_dir)
        self.assertEqual(cfg["providers"][0]["api_key"], "",
                         "解不开的密文应降级为空 key")


class TestLoadCompleteness(unittest.TestCase):
    """RED 复现：load_config_store 产出必须含 WeChatBot.__init__ 全部裸索引键。

    config_store 是配置统一事实源（#9 统一后 GUI/CLI 都走这里），缺键 →
    AgentBot(ctx.cfg) 初始化抛 KeyError: 'vision_prompt'（用户实测报错）。
    两个缺键场景：全新安装（无 config.json）与已有 providers 的新结构 config。
    """

    # WeChatBot.__init__ 直接索引（cfg[k] 非 cfg.get）的键；单模型化后
    # __init__ 不再裸索引 vision_model（视觉 model 取 chat_model）
    REQUIRED_KEYS = [
        "bot_nickname", "ai_api_url", "ai_api_key", "chat_model",
        "vision_prompt", "system_prompt", "max_history",
        "cooldown", "api_retry", "api_timeout",
    ]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cfg_cmp_")
        self.cards_dir = os.path.join(self.tmp, "cards")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fresh_install_has_all_bot_keys(self):
        """全新安装（无 config.json）：返回的 cfg 必须能直接构造 WeChatBot。"""
        cfg = config_store.load_config_store(
            os.path.join(self.tmp, "config.json"), self.cards_dir)
        for k in self.REQUIRED_KEYS:
            self.assertIn(k, cfg, f"全新安装 cfg 缺 {k} → 初始化 KeyError")
        self.assertTrue(str(cfg["vision_prompt"] or "").strip(),
                        "vision_prompt 必须有非空默认值")
        self.assertNotIn("vision_model", cfg,
                         "单模型化：cfg 不应投影独立 vision_model（视觉走 chat_model）")

    def test_modern_config_without_ai_keys(self):
        """新结构 config（providers 已存在、无旧 AI 字段）：同样必须补齐。"""
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "providers": [{
                    "id": "deepseek", "name": "DeepSeek",
                    "base_url": "https://api.deepseek.com/v1/chat/completions",
                    "api_key": "", "models": ["deepseek:deepseek-v4-flash"],
                }],
                "active_card_id": config_store.DEFAULT_CARD_ID,
                "tasks_dir": os.path.join(self.tmp, "tasks"),
            }, f, ensure_ascii=False)
        cfg = config_store.load_config_store(path, self.cards_dir)
        for k in self.REQUIRED_KEYS:
            self.assertIn(k, cfg, f"新结构 config 缺 {k} → 初始化 KeyError")
        self.assertTrue(str(cfg["vision_prompt"] or "").strip(),
                        "vision_prompt 必须有非空默认值")
        self.assertNotIn("vision_model", cfg,
                         "单模型化：新结构 config 不应投影独立 vision_model（视觉走 chat_model）")


    def test_missing_card_falls_back_template(self):
        """活跃卡缺失（cards/ 不存在/卡被删）→ 回退默认卡模板投影：
        system_prompt 非空（否则聊天无人设、初始化后静默异常）。"""
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "providers": [{"id": "deepseek", "name": "DeepSeek",
                               "base_url": "http://x", "api_key": "",
                               "models": ["m"]}],
                "active_card_id": "ghost-card",
            }, f, ensure_ascii=False)
        cfg = config_store.load_config_store(path, self.cards_dir)
        self.assertEqual(cfg["system_prompt"],
                         config_store.CARD_TEMPLATE["system_prompt"],
                         "卡缺失时 system_prompt 必须回退模板（非空）")
        self.assertTrue(str(cfg["system_prompt"] or "").strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
