# -*- coding: utf-8 -*-
"""主题系统测试：7 套主题 config 迁移 + 离屏渲染冒烟。

历史缺陷防回归：砍主题后渲染层兜底仍指 THEMES["blue"]（KeyError 级断裂）、
config 迁移条件误伤用户手选 tokyonight——钉住两件事：
  1) config 迁移：blue/None/已砍主题 → abyss；保留主题不被覆盖；
  2) 每套保留主题：QSS 可生成、缩略图可渲染、Backdrop 静态层（含主题专属
     装饰）带/不带壁纸都渲染成功，未知主题兜底不抛异常。
Backdrop/QSS 冒烟要建 QApplication，子进程离屏跑（test_ui_pages 同模式：
os._exit 绕 Qt teardown，_exit 不刷缓冲须先 flush）。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app import config_store

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_RENDER_CODE = r"""
import os, sys, tempfile
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, HERE)
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QColor, QPixmap
app = QApplication([])
from xiaoli_app.ui import THEMES, build_qss, render_theme_thumb
from xiaoli_app.ui.backdrop import Backdrop

assert set(THEMES) == {"abyss", "neon", "tokyonight", "stellar",
                       "moxin", "cream", "mint"}, sorted(THEMES)

wp_path = os.path.join(tempfile.mkdtemp(prefix="bkdr_"), "wp.png")
pm = QPixmap(64, 64)
pm.fill(QColor("#445566"))
assert pm.save(wp_path)

for key in THEMES:
    assert build_qss(key).strip(), key + " QSS 为空"
    assert not render_theme_thumb(key).isNull(), key + " 缩略图渲染失败"
    for wp in ("", wp_path):
        bd = Backdrop()
        bd.resize(980, 640)
        bd.set_theme(key)
        bd.set_wallpaper(wp)
        out = bd.grab()   # 触发 paintEvent → 静态层合成（主题装饰在此执行）
        assert not out.isNull() and out.size().width() == 980, key + " 渲染失败"
    print(key, "OK", flush=True)

# 未知主题（已砍的 nord）：QSS 与 Backdrop 均回退 abyss 兜底，不抛异常
assert build_qss("nord").strip()
bd = Backdrop()
bd.resize(800, 600)
bd.set_theme("nord")
assert not bd.grab().isNull()
print("FALLBACK OK", flush=True)
sys.stdout.flush()
sys.stderr.flush()
os._exit(0)
""".replace("HERE", repr(HERE))


class TestThemeMigration(unittest.TestCase):
    """主题迁移：不在保留名单 → abyss；保留主题（含用户手选 tokyonight）不动。

    迁移逻辑在 load_config_store（读→迁移→写回整链），走临时 config 文件。"""

    _KEPT = ("abyss", "neon", "tokyonight", "stellar", "moxin", "cream", "mint")

    def _load(self, theme):
        tmp = tempfile.mkdtemp(prefix="thm_")
        path = os.path.join(tmp, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"theme": theme} if theme is not None else {}, f)
        return config_store.load_config_store(path, os.path.join(tmp, "cards"))

    def test_legacy_default_blue_migrates_to_abyss(self):
        self.assertEqual(self._load("blue")["theme"], "abyss")

    def test_missing_theme_defaults_to_abyss(self):
        self.assertEqual(self._load(None)["theme"], "abyss")

    def test_cut_theme_migrates_to_abyss(self):
        for cut in ("nord", "dracula", "paper", "aurora", "cyberpunk"):
            self.assertEqual(self._load(cut)["theme"], "abyss",
                             f"已砍主题 {cut} 应回退 abyss")

    def test_kept_themes_preserved(self):
        for key in self._KEPT:
            self.assertEqual(self._load(key)["theme"], key,
                             f"保留主题 {key} 不应被迁移覆盖")


class TestRenderSmoke(unittest.TestCase):
    def test_all_themes_render(self):
        """7 套主题 QSS/缩略图/静态层（含装饰）渲染 + 未知主题兜底。"""
        r = subprocess.run([sys.executable, "-c", _RENDER_CODE],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0,
                         f"主题渲染冒烟失败 (exit {r.returncode}):\n{r.stderr}")
        for key in ("abyss", "neon", "tokyonight", "stellar",
                    "moxin", "cream", "mint"):
            self.assertIn(key + " OK", r.stdout)
        self.assertIn("FALLBACK OK", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
