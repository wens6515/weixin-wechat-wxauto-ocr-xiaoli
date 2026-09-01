# -*- coding: utf-8 -*-
"""UI 页面冒烟测试：子进程离屏实例化全部导航页并执行 refresh()。

历史缺陷：SettingsPage 删除 _view_memory 方法时按方法边界整段切，误删了
同区间的 _fill_compress_model_options/_save_memory_v2_settings——py_compile
只查语法查不出「构造期引用已删方法」，直到真机启动才炸
（AttributeError: 'SettingsPage' object has no attribute ...）。
本测试让此类断裂在跑测试时就暴露。

用子进程跑（与 test_windowed_import 同模式）：页面冒烟要建 QApplication，
在本进程里跑会在 Qt 解释器收尾时原生崩溃（exit 127 无 summary）——
子进程末尾 os._exit(0) 绕开 teardown，退出码与输出照常断言。"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SMOKE_CODE = r"""
import os, sys, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, HERE)
from PySide6.QtWidgets import QApplication
app = QApplication([])
from xiaoli_app.ui.pages import (HomePage, CardsPage, ModelsPage, MemoryPage,
                                 UsagePage, LogPage, SettingsPage)

class Ctx:
    cfg = {"memory_file": os.path.join(tempfile.gettempdir(), "mem_smoke.json")}
    cfg_path = "config.json"
    bus = None
    cards_dir = tempfile.mkdtemp(prefix="ui_smoke_cards_")
    def providers(self):
        return []
    def active_card_id(self):
        return "xiaoli"
    def theme(self):
        return "abyss"
    def wallpaper(self):
        return ""

ctx = Ctx()
for cls in (HomePage, CardsPage, ModelsPage, MemoryPage, UsagePage,
            LogPage, SettingsPage):
    page = cls(ctx)          # 构造期属性断裂在此暴露
    refresh = getattr(page, "refresh", None)
    if refresh is not None:
        refresh()
    print(cls.__name__, "OK")

page = SettingsPage(ctx)
for name in ("_fill_compress_model_options", "_save_memory_v2_settings",
             "_clear_memory"):
    assert callable(getattr(page, name, None)), f"SettingsPage 缺方法 {name}"
print("ALL PAGES OK")
sys.stdout.flush()
sys.stderr.flush()
os._exit(0)   # 绕开 Qt teardown 原生崩溃（_exit 不刷缓冲，必须先 flush）
""".replace("HERE", repr(HERE))


class TestPagesSmoke(unittest.TestCase):
    def test_all_pages_construct_and_refresh(self):
        """全部导航页：离屏构造 + refresh 不抛异常、SettingsPage 长记忆方法齐备。"""
        r = subprocess.run([sys.executable, "-c", _SMOKE_CODE],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0,
                         f"页面冒烟失败 (exit {r.returncode}):\n{r.stderr}")
        self.assertIn("ALL PAGES OK", r.stdout)
        for name in ("SettingsPage OK", "MemoryPage OK"):
            self.assertIn(name, r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
