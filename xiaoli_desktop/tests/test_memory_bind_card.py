# -*- coding: utf-8 -*-
"""绑定角色卡的闭环：写入绑定 → 重投影 → 运行中 bot 参数表命中。

钉住的定案（对应真机事故「绑定过但从没生效，一直用全局卡」）：

- 前端 selectModal 曾把「确定」写死返回布尔 true（见 webui/app.js 注释），
  后端把 str(True)="True" 当卡 id 存进 chat_card_bindings → 投影时
  _read_card 找不到该卡、静默跳过 → 运行时永远回落全局活跃卡；
- 现在写入端只接受真实卡 id（本测试覆盖写→投影→命中的完整链路），
  存量脏数据由 config_store.sanitize_chat_bindings 清理（单独测试）；
- 键口径 = memory_key 归一化（引号/空格变体不分裂绑定），运行时
  WeChatBot._chat_overrides 先按原文键查、再按归一化键查。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app import card_store, config_store
from xiaoli_app.webbridge import BridgeApi


class _Engine:
    bot = None


class _Ctx:
    """最小 AppContext 桩：reproject_and_push 与 WebContext 同语义
    （重载配置 → 热推绑定/参数表给运行中的 bot）。"""

    def __init__(self, tmp, cards_dir):
        self.cfg_path = os.path.join(tmp, "config.json")
        self.cards_dir = cards_dir
        self.cfg = {"active_card_id": "xiaoli", "providers": []}
        self.engine = _Engine()

    def reproject_and_push(self):
        cfg = config_store.load_config_store(self.cfg_path, self.cards_dir)
        self.cfg = cfg
        bot = getattr(self.engine, "bot", None)
        if bot is not None:
            bot.chat_card_bindings = cfg.get("chat_card_bindings", {})
            bot.chat_card_params = cfg.get("chat_card_params", {})
        return cfg


class _Bot:
    """只留绑定相关的运行期属性（_chat_overrides 的查询口径）。"""

    def __init__(self):
        self.chat_card_bindings = {}
        self.chat_card_params = {}

    def _chat_overrides(self, chat_id):
        from xiaoli_app.memory_store import memory_key
        params = getattr(self, "chat_card_params", None) or {}
        return params.get(chat_id) or params.get(memory_key(chat_id)) or {}


class TestBindCardRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cards_dir = os.path.join(self.tmp.name, "cards")
        card_store.save_card(self.cards_dir, {
            "id": "xiaoli", "name": "小漓", "system_prompt": "全局人设",
            "nickname": "小漓", "chat_model": "x:m1"})
        card_store.save_card(self.cards_dir, {
            "id": "assist", "name": "助手", "system_prompt": "专属人设",
            "nickname": "小助手", "chat_model": "x:m2"})
        self.ctx = _Ctx(self.tmp.name, self.cards_dir)
        self.bridge = BridgeApi(self.ctx)
        self.bot = _Bot()
        self.ctx.engine.bot = self.bot

    def test_bind_then_runtime_lookup_hits(self):
        r = self.bridge.memory_bind_card("王文生", "assist")
        self.assertTrue(r["ok"])
        # 事实源：config.json 里存的是真实卡 id
        self.assertEqual(self.ctx.cfg["chat_card_bindings"]["王文生"], "assist")
        # 热推：运行中 bot 的参数表已带上该会话
        self.assertEqual(self.bot._chat_overrides("王文生")["system_prompt"],
                         "专属人设")
        self.assertEqual(self.bot._chat_overrides("王文生")["chat_model"], "x:m2")

    def test_lookup_tolerates_quote_variants(self):
        """绑定与查询的会话名带不同引号变体时仍命中（memory_key 口径）。"""
        self.bridge.memory_bind_card("王文生", "assist")
        self.assertEqual(
            self.bot._chat_overrides('" 王文生 "')["system_prompt"], "专属人设")

    def test_unbind_falls_back_to_global(self):
        self.bridge.memory_bind_card("王文生", "assist")
        self.bridge.memory_bind_card("王文生", "")
        self.assertEqual(self.bot._chat_overrides("王文生"), {})
        self.assertEqual(self.ctx.cfg["chat_card_bindings"], {})

    def test_persisted_binding_survives_reload(self):
        self.bridge.memory_bind_card("王文生", "assist")
        cfg = config_store.load_config_store(self.ctx.cfg_path, self.cards_dir)
        self.assertEqual(cfg["chat_card_bindings"], {"王文生": "assist"})
        self.assertIn("王文生", cfg["chat_card_params"])

    def test_unbind_cancel_sentinel_from_frontend(self):
        """前端「解除绑定」传空串（selectModal 的 value=""）→ 解除而非存脏值。"""
        self.bridge.memory_bind_card("王文生", "assist")
        self.bridge.memory_bind_card("王文生", "")
        cfg = config_store.load_config_store(self.ctx.cfg_path, self.cards_dir)
        self.assertFalse(cfg["chat_card_bindings"])


if __name__ == "__main__":
    unittest.main()
