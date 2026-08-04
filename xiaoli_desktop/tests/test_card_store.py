# -*- coding: utf-8 -*-
"""card_store 单元测试：角色卡 CRUD、校验、导入导出、apply_role 热切换"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

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
        # 未知 provider：回退第一个可用 provider（不置空 URL——否则 API 请求
        # Invalid URL ''，真机日志 13:45:09 故障链）
        self.assertEqual(bot.api_url, "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(bot.vision_api_url, "https://api.deepseek.com/v1/chat/completions")

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


class TestTaskDispatchWindow(unittest.TestCase):
    """任务投递后唤起天枢：tianshu_window_title 被污染为桌面端标题时，
    必须走 resolve_cli_window 定位 CLI（npm 窗口），不得激活桌面端。"""

    def setUp(self):
        # 隔离天枢 CLI 授权写入：投递任务前 grant_tasks_dir_to_tianshu 会写
        # %LOCALAPPDATA%\.rivet\config.json——测试不得污染真实 CLI 配置
        self._grant_tmp = tempfile.mkdtemp(prefix="grant_iso_")
        patcher = mock.patch.dict(os.environ, {"LOCALAPPDATA": self._grant_tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        # resolve_cli_window 的 launch 冷却护栏是模块级状态，测试间必须重置，
        # 否则前一个测试的 launch 会让本类测试跳过第 3 级 launch，断言误红
        from xiaoli_app import setup as _setup
        _setup._last_launch_mono = None

    def _make_bot(self):
        from xiaoli_bot import AgentBot
        bot = AgentBot.__new__(AgentBot)
        bot.tianshu_window_title = "天枢 · Tianshu"   # 旧版本污染的桌面端标题
        bot.tianshu_trigger_command = "开始处理"
        bot.tasks_dir = tempfile.mkdtemp(prefix="dispatch_")
        bot.dispatched_msg_ids = set()
        bot._sending_lock = False
        bot._model_lock = threading.RLock()
        return bot

    def tearDown(self):
        shutil.rmtree(getattr(self, "_tmp", ""), ignore_errors=True)
        shutil.rmtree(getattr(self, "_grant_tmp", ""), ignore_errors=True)

    def test_stale_task_does_not_block_polling(self):
        """RED 复现：任务卡死（无 result.json 且超过阈值）不应永久阻塞消息轮询。
        用户实测：投递"贪吃蛇"后任务未归档，bot 从此不再轮询微信消息。"""
        import xiaoli_bot as xb
        tmp = tempfile.mkdtemp(prefix="stale_task_")
        self._tmp = tmp
        # 卡死任务：只有 task.json、无 result.json，创建于很久以前
        task_dir = os.path.join(tmp, "2026080220352060b5")
        os.makedirs(task_dir)
        with open(os.path.join(task_dir, "task.json"), "w", encoding="utf-8") as f:
            json.dump({"task": "做一个贪吃蛇", "created_at": time.time() - 60}, f)
        self.assertTrue(xb.has_active_tasks(tmp), "新任务应判为活跃（基线）")
        # 卡死任务：创建时间超过阈值且无 result.json → 不再活跃 → 轮询可恢复
        with open(os.path.join(task_dir, "task.json"), "w", encoding="utf-8") as f:
            json.dump({"task": "做一个贪吃蛇", "created_at": time.time() - 99999}, f)
        self.assertFalse(xb.has_active_tasks(tmp, stale_after=3600),
                         "卡死超过阈值的任务不得阻塞消息轮询")
        # 正常完成的判定不受影响
        with open(os.path.join(task_dir, "result.json"), "w", encoding="utf-8") as f:
            json.dump({"status": "success"}, f)
        self.assertFalse(xb.has_active_tasks(tmp, stale_after=3600))

    def test_classify_task_uses_chat_model(self):
        """RED 复现：任务判断必须用文字模型（chat_model）——classify_model
        常为空导致 payload model="" → API 400 → 异常静默降级 is_task=False，
        任务消息全被当聊天处理（用户实测：发两次都当聊天）。"""
        from unittest import mock
        import xiaoli_bot as xb
        bot = self._make_bot()
        bot.task_enabled = True
        bot.chat_model = "deepseek-chat"
        bot.file_model = ""  # classify_model 为空的历史场景
        bot.api_url = "https://api.deepseek.com/v1/chat/completions"
        bot.api_key = "sk-test"
        seen = {}

        def fake_classify(api_url, api_key, model, text, timeout=30):
            seen["model"] = model
            seen["text"] = text
            return {"is_task": True, "task": "做个PPT"}
        with mock.patch.object(xb, "classify_task_with_llm", side_effect=fake_classify):
            r = bot._classify_task("帮我做个PPT")
        self.assertTrue(r["is_task"])
        self.assertEqual(seen["model"], "deepseek-chat",
                         "任务判断必须用 chat_model（文字模型），而非可能为空的 classify_model")

    def test_dispatch_uses_cli_window_not_desktop(self):
        import xiaoli_bot as xb
        bot = self._make_bot()
        self._tmp = bot.tasks_dir
        sent = {"title": None}
        wins = ["天枢 · Tianshu", "微信"]  # 桌面端在运行

        def fake_list():
            return list(wins)

        def fake_launch(cfg):
            wins.append("npm prefix")  # CLI 启动后新增窗口
            return True, "ok"

        def fake_console():
            # 控制台枚举：启动前无 CLI（桌面端/微信不是控制台类），启动后 CLI 进入
            return ["npm prefix"] if len(wins) > 2 else []

        def fake_trigger(title, command, hold=0.5, enter_times=1):
            sent["title"] = title
            return True

        from xiaoli_app import setup as _setup
        with mock.patch.object(_setup, "_list_windows", side_effect=fake_list), \
             mock.patch.object(_setup, "_console_windows", side_effect=fake_console), \
             mock.patch.object(_setup, "launch_tianshu", side_effect=fake_launch), \
             mock.patch.object(xb, "send_trigger_to_window", side_effect=fake_trigger), \
             mock.patch.object(bot, "_send_text", return_value=None), \
             mock.patch.object(xb, "dispatch_task", return_value="T1"):
            bot._dispatch_and_notify("测试群", "老王", "做一个贪吃蛇")
        self.assertEqual(sent["title"], "npm prefix",
                         "任务唤起必须发给 CLI 窗口（npm），不得激活桌面端「天枢 · Tianshu」")

    def test_dispatch_switches_cli_to_yolo_before_trigger(self):
        """投递任务时唤起天枢 CLI：全自动由首启 /yes 持久化保证（见
        run_first_run_guide），投递路径不再会话内切 YOLO，直接发触发指令。
        回归锚点：resolve 定位 npm 窗口的链路必须保持可用。
        """
        import xiaoli_bot as xb
        bot = self._make_bot()
        self._tmp = bot.tasks_dir
        cmds = []
        wins = ["天枢 · Tianshu", "微信"]  # 桌面端 + 微信在运行（无 CLI）

        def fake_list():
            return list(wins)

        def fake_launch(cfg):
            wins.append("npm prefix")  # CLI 启动后新增窗口
            return True, "ok"

        def fake_console():
            # 控制台枚举：启动前无 CLI，启动后 CLI（npm）进入
            return ["npm prefix"] if len(wins) > 2 else []

        def fake_trigger(title, command, hold=0.5, enter_times=1):
            cmds.append(command)
            return True

        from xiaoli_app import setup as _setup
        with mock.patch.object(_setup, "_list_windows", side_effect=fake_list), \
             mock.patch.object(_setup, "_console_windows", side_effect=fake_console), \
             mock.patch.object(_setup, "launch_tianshu", side_effect=fake_launch), \
             mock.patch.object(xb, "send_trigger_to_window", side_effect=fake_trigger), \
             mock.patch.object(bot, "_send_text", return_value=None), \
             mock.patch.object(xb, "dispatch_task", return_value="T1"):
            bot._dispatch_and_notify("测试群", "老王", "做一个贪吃蛇")
        self.assertEqual(cmds, ["开始处理"],
                         "全自动由首启 /yes 持久化保证（见 run_first_run_guide），投递时不再切 YOLO，直接发触发指令")


if __name__ == "__main__":
    unittest.main(verbosity=2)
