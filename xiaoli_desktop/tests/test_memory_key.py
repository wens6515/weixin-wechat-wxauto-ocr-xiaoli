# -*- coding: utf-8 -*-
"""memory 键归一化单测：引号与空格全剥、记忆存取同键（含 OCR 丢引号场景）。"""
import unittest

import wechat_bot as wb


class TestMemoryKey(unittest.TestCase):
    def test_quote_variants_stripped(self):
        # 半/全角弯引号、直引号、全角引号、OCR 丢前引号 → 剥掉后同键
        self.assertEqual(wb._memory_key("“强盗”集团"), "强盗集团")
        self.assertEqual(wb._memory_key('强盗"集团'), "强盗集团")
        self.assertEqual(wb._memory_key("＂强盗＂集团"), "强盗集团")
        self.assertEqual(wb._memory_key("「强盗」集团"), "强盗集团")
        self.assertEqual(wb._memory_key("强盗”集团"), "强盗集团")  # OCR 丢前引号

    def test_whitespace_stripped(self):
        self.assertEqual(wb._memory_key('" 强盗 " 集团'), "强盗集团")
        self.assertEqual(wb._memory_key("王文生 "), "王文生")

    def test_plain_name_unchanged(self):
        self.assertEqual(wb._memory_key("王文生"), "王文生")
        self.assertEqual(wb._memory_key(None), "")

    def test_history_roundtrip_same_chat(self):
        bot = wb.WeChatBot.__new__(wb.WeChatBot)
        bot.memory_db = {}
        bot.max_history = 100
        bot._schedule_save_memory = lambda: None
        # 各种 OCR 变体读写的应是同一份历史
        bot._add_history("“强盗”集团", "user", "在吗")
        self.assertEqual(len(bot._get_history("强盗”集团")), 1)   # 丢前引号
        self.assertEqual(len(bot._get_history('强盗"集团')), 1)   # 半角直引号
        self.assertEqual(len(bot._get_history('" 强盗 " 集团')), 1)  # 引号带空格
        # 变体清空同样命中
        bot.clear_history("＂强盗＂集团 ")
        self.assertEqual(len(bot._get_history("「强盗」集团")), 0)


if __name__ == "__main__":
    unittest.main()
