# -*- coding: utf-8 -*-
"""任务桥 README 的生成契约。

用户定案：程序启动时按测试区那份 `dist\\小漓\\wxauto\\README.md` 的内容
生成 tasks_dir\\README.md，**只有最下面那行的扫描范围路径**换成用户实际
保存的 tasks_dir。

本组测试钉住三件事：
1. 生成内容与测试区那份**逐字一致**（只差末行路径）——模板漂移会被抓住；
2. `{tasks_dir}` 被真实路径替换（不留占位符）；
3. JSON 示例里的大括号不被 str.format 吃掉（模板转义正确）。
"""
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app import setup as xsetup

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DIST_README = os.path.join(_ROOT, "dist", "小漓", "wxauto", "README.md")


class TestBridgeReadme(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bridge_readme_")
        self.tasks_dir = os.path.join(self.tmp, "wxauto")
        self.path = os.path.join(self.tasks_dir, "README.md")

    def read(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return f.read()

    def test_generates_in_fresh_dir(self):
        self.assertTrue(xsetup.ensure_bridge_readme(self.tasks_dir))
        text = self.read()
        self.assertIn("# 微信任务桥协议", text)
        self.assertIn("如果未扫描到任务包则默认小漓未投递任务，直接结束本轮轮次",
                      text)
        self.assertNotIn("{tasks_dir}", text, "占位符必须已被真实路径替换")
        self.assertIn(self.tasks_dir, text, "扫描范围行应写入实际 tasks_dir")

    def test_json_braces_survive_format(self):
        """JSON 示例必须原样落地（模板里的 {{ }} 转义正确）。"""
        xsetup.ensure_bridge_readme(self.tasks_dir)
        text = self.read()
        self.assertIn('{"status": "success"', text)
        self.assertIn('{"stage": "一句话说明现在在做什么"}', text)
        self.assertNotIn("{{", text, "不得残留转义后的双大括号")

    def test_not_overwritten_when_exists(self):
        os.makedirs(self.tasks_dir, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("用户自定义内容")
        self.assertFalse(xsetup.ensure_bridge_readme(self.tasks_dir))
        self.assertEqual(self.read(), "用户自定义内容")

    def test_empty_tasks_dir_noop(self):
        self.assertFalse(xsetup.ensure_bridge_readme(""))
        self.assertFalse(xsetup.ensure_bridge_readme(None))

    @unittest.skipUnless(os.path.isfile(DIST_README),
                         "测试区 dist\\小漓\\wxauto\\README.md 不存在，跳过逐字比对")
    def test_matches_dist_readme_verbatim(self):
        """生成内容 == 测试区那份（只把末行扫描范围路径换成新的 tasks_dir）。"""
        with open(DIST_README, "r", encoding="utf-8") as f:
            dist_text = f.read()
        expected = re.sub(r"范围只有.+?！！！！",
                          lambda m: "范围只有%s！！！！" % self.tasks_dir,
                          dist_text)
        xsetup.ensure_bridge_readme(self.tasks_dir)
        self.assertEqual(self.read(), expected,
                         "生成内容与测试区 README 不一致——模板漂移了")


if __name__ == "__main__":
    unittest.main()
