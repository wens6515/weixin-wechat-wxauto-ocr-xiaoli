# -*- coding: utf-8 -*-
"""windowed（无控制台）模式导入测试。

回归目标：PyInstaller --windowed / pythonw 启动时 sys.stdout 为 None，
xiaoli_bot.py 顶部 `if sys.stdout.encoding != "utf-8"` 会抛
AttributeError: 'NoneType' object has no attribute 'encoding'（双击 exe 直接报错）。
修复：getattr(sys.stdout, "encoding", "utf-8") 保护。

用子进程复现（本进程 import 会被模块缓存掩盖——with_stdout 测试先跑会缓存模块）。
"""
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestWindowedImport(unittest.TestCase):
    def test_xiaoli_bot_importable_without_stdout(self):
        """模拟 windowed：独立子进程里 sys.stdout=None 再 import xiaoli_bot"""
        code = (
            "import sys\n"
            f"sys.path.insert(0, {HERE!r})\n"
            "sys.stdout = None\n"
            "import xiaoli_bot\n"
            "sys.exit(0)\n"
        )
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, timeout=90)
        self.assertEqual(r.returncode, 0,
                         f"windowed 导入失败:\n{r.stderr}")

    def test_xiaoli_bot_importable_with_stdout(self):
        """控制台模式（python xiaoli_bot.py）行为不变"""
        code = (
            "import sys\n"
            f"sys.path.insert(0, {HERE!r})\n"
            "import xiaoli_bot\n"
            "sys.exit(0)\n"
        )
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True, timeout=90)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
