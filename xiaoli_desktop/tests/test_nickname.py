# -*- coding: utf-8 -*-
"""微信名称设置：昵称的**事实源是活跃角色卡**。

钉住的定案：

- 昵称由 `project_config` 用**卡上的 nickname** 投影出 `cfg.bot_nickname`——
  只写 cfg 不写卡，下一次配置加载就被卡上的旧值覆盖回去（历史缺陷：首启向导
  的"微信昵称"输入框填了等于没填）；
- `set_nickname` 一次写三处并立即生效：活跃卡（事实源）→ cfg（下次启动直接
  读到）→ 运行中 `bot.nickname`（群里 @ 判据 + 提示词里的自称）；
- 空 / 超长一律拒绝，且一处都不改；
- 首启向导（save_first_run）同样落卡，不再只写 cfg。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app import card_store, config_store
from xiaoli_app.webbridge import BridgeApi


class _Bot:
    nickname = "小漓"


class _Engine:
    def __init__(self):
        self.bot = _Bot()


class _Ctx:
    def __init__(self, tmp, cards_dir):
        self.cfg_path = os.path.join(tmp, "config.json")
        self.cards_dir = cards_dir
        self.cfg = {"active_card_id": "xiaoli", "bot_nickname": "小漓"}
        self.engine = _Engine()


class TestSetNickname(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cards_dir = os.path.join(self.tmp.name, "cards")
        card_store.save_card(self.cards_dir, {
            "id": "xiaoli", "name": "小漓", "system_prompt": "人设",
            "nickname": "小漓"})
        self.ctx = _Ctx(self.tmp.name, self.cards_dir)
        self.bridge = BridgeApi(self.ctx)

    def _card_nickname(self):
        return card_store.get_card(self.cards_dir, "xiaoli")["nickname"]

    def test_writes_card_cfg_and_running_bot(self):
        r = self.bridge.set_nickname("  大肥鱼  ")
        self.assertTrue(r["ok"])
        self.assertEqual(r["nickname"], "大肥鱼")          # 首尾空白剥掉
        self.assertTrue(r["card_written"])
        self.assertEqual(self._card_nickname(), "大肥鱼")   # 事实源
        self.assertEqual(self.ctx.cfg["bot_nickname"], "大肥鱼")
        self.assertEqual(self.ctx.engine.bot.nickname, "大肥鱼")  # 立即生效
        self.assertTrue(os.path.isfile(self.ctx.cfg_path))  # 已落盘

    def test_card_value_survives_reload_projection(self):
        """重新加载配置后仍是新名字——不再被卡上的旧值覆盖回'小漓'。"""
        self.bridge.set_nickname("大肥鱼")
        cfg = config_store.load_config_store(self.ctx.cfg_path, self.cards_dir)
        self.assertEqual(cfg["bot_nickname"], "大肥鱼")

    def test_rejects_empty_and_keeps_everything(self):
        r = self.bridge.set_nickname("   ")
        self.assertFalse(r["ok"])
        self.assertEqual(self._card_nickname(), "小漓")
        self.assertEqual(self.ctx.cfg["bot_nickname"], "小漓")
        self.assertEqual(self.ctx.engine.bot.nickname, "小漓")

    def test_rejects_too_long(self):
        r = self.bridge.set_nickname("鱼" * 33)
        self.assertFalse(r["ok"])
        self.assertEqual(self._card_nickname(), "小漓")

    def test_ok_without_running_bot(self):
        """引擎未初始化（bot 不存在）时改名仍应成功：卡与 cfg 照写。"""
        self.ctx.engine = None
        r = self.bridge.set_nickname("大肥鱼")
        self.assertTrue(r["ok"])
        self.assertEqual(self._card_nickname(), "大肥鱼")

    def test_missing_card_does_not_block(self):
        """卡不存在（active_card_id 失效）→ 仍写 cfg，只是 card_written=False。"""
        self.ctx.cfg["active_card_id"] = "nope"
        r = self.bridge.set_nickname("大肥鱼")
        self.assertTrue(r["ok"])
        self.assertFalse(r["card_written"])
        self.assertEqual(self.ctx.cfg["bot_nickname"], "大肥鱼")


class TestFirstRunNickname(unittest.TestCase):
    """首启向导的微信名称同样落卡（历史缺陷：只写 cfg → 被投影覆盖）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cards_dir = os.path.join(self.tmp.name, "cards")
        card_store.save_card(self.cards_dir, {
            "id": "xiaoli", "name": "小漓", "system_prompt": "人设",
            "nickname": "小漓"})
        self.ctx = _Ctx(self.tmp.name, self.cards_dir)
        self.bridge = BridgeApi(self.ctx)

    def test_first_run_saves_nickname_to_card(self):
        tasks = os.path.join(self.tmp.name, "wxauto")
        r = self.bridge.save_first_run(tasks, os.path.join(self.tmp.name, "files"),
                                       "memory.json", "小鱼干")
        self.assertTrue(r["ok"])
        self.assertEqual(card_store.get_card(self.cards_dir, "xiaoli")["nickname"],
                         "小鱼干")
        self.assertEqual(self.ctx.cfg["bot_nickname"], "小鱼干")


if __name__ == "__main__":
    unittest.main()
