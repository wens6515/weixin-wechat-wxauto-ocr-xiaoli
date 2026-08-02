# -*- coding: utf-8 -*-
"""首次启动引导（FirstRunDialog / needs_first_run）测试。

缺陷场景（RED 复现）：旧版本生成的 config.json 已存在但 tasks_dir /
file_storage_path 为空——原实现只判断 config 文件是否存在，导致升级用户
永远看不到引导，任务桥目录与附件目录静默为空。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

QMessageBox.information = staticmethod(lambda *a, **k: None)
QMessageBox.warning = staticmethod(lambda *a, **k: None)
QMessageBox.critical = staticmethod(lambda *a, **k: None)

from xiaoli_gui import FirstRunDialog, default_wechat_files_dir, needs_first_run

_app = QApplication.instance() or QApplication(sys.argv)


class TestNeedsFirstRun(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="first_run_")
        self.cfg_path = os.path.join(self.tmp, "config.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_config_missing_needs_first_run(self):
        # 全新机器：config.json 不存在 → 必须引导
        self.assertTrue(needs_first_run(self.cfg_path, {}))

    def test_config_with_dirs_needs_no_first_run(self):
        # 已配置完整（tasks_dir + file_storage_path 都有值）→ 不引导
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"tasks_dir": r"D:\工作间\wxauto",
                       "file_storage_path": r"D:\微信文件"}, f)
        self.assertFalse(needs_first_run(self.cfg_path, None))

    def test_old_config_missing_dirs_needs_first_run(self):
        # RED 复现：旧 config 存在但没有目录字段 → 必须引导（原实现漏判）
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"bot_nickname": "小漓"}, f)
        self.assertTrue(needs_first_run(self.cfg_path, None),
                        "旧 config 无 tasks_dir/file_storage_path 时应引导")

    def test_empty_dir_values_needs_first_run(self):
        # dist 现状：字段存在但为空字符串 → 必须引导
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"tasks_dir": "", "file_storage_path": ""}, f)
        self.assertTrue(needs_first_run(self.cfg_path, None),
                        "目录字段为空时应引导")

    def test_only_tasks_dir_empty_needs_first_run(self):
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump({"tasks_dir": "", "file_storage_path": r"D:\微信文件"}, f)
        self.assertTrue(needs_first_run(self.cfg_path, None))


class TestFirstRunDialog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="first_run_dlg_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_defaults_when_no_cfg(self):
        dlg = FirstRunDialog()
        self.assertEqual(dlg.ed_tasks.text(), config_store_default_tasks())
        self.assertEqual(dlg.ed_memory.text(), config_store_default_memory())
        # 微信目录默认值 = default_wechat_files_dir() 的非空探测结果
        self.assertEqual(dlg.ed_files.text(), default_wechat_files_dir())

    def test_preserves_existing_config(self):
        # 已有配置（如 memory_file）必须预填保留，不让用户重选
        cfg = {"tasks_dir": r"D:\已有任务目录",
               "file_storage_path": r"D:\已有微信目录",
               "memory_file": r"D:\已有记忆\memory.json"}
        dlg = FirstRunDialog(cfg=cfg)
        self.assertEqual(dlg.ed_tasks.text(), r"D:\已有任务目录")
        self.assertEqual(dlg.ed_files.text(), r"D:\已有微信目录")
        self.assertEqual(dlg.ed_memory.text(), r"D:\已有记忆\memory.json")

    def test_result_cfg_roundtrip(self):
        cfg = {"tasks_dir": r"D:\A", "file_storage_path": r"D:\B",
               "memory_file": r"D:\C\m.json"}
        dlg = FirstRunDialog(cfg=cfg)
        out = dlg.result_cfg()
        self.assertEqual(out["tasks_dir"], r"D:\A")
        self.assertEqual(out["file_storage_path"], r"D:\B")
        self.assertEqual(out["memory_file"], r"D:\C\m.json")


class TestFirstRunBridgeInit(unittest.TestCase):
    """首次引导选完工作文件夹后，任务桥目录应自动初始化（README.md 协议文档）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="first_run_bridge_")
        self.tasks_dir = os.path.join(self.tmp, "wxauto")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bridge_readme_generated_for_new_tasks_dir(self):
        # RED 复现：旧实现引导只保存 config，不初始化任务桥——新用户选完目录后
        # tasks_dir 不存在、README.md 缺失，天枢 CLI 首次处理任务时无协议可读。
        # 修复后引导保存配置即调用 ensure_bridge_readme，目录+README.md 一次到位。
        from xiaoli_app import setup as _setup
        made = _setup.ensure_bridge_readme(self.tasks_dir)
        self.assertTrue(made, "新任务目录应生成 README.md")
        self.assertTrue(os.path.isfile(os.path.join(self.tasks_dir, "README.md")),
                        "tasks_dir 下应有 README.md 协议文档")
        with open(os.path.join(self.tasks_dir, "README.md"), "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("task.json", content)
        self.assertIn("result.json", content)

    def test_bridge_readme_idempotent(self):
        # 已存在 README.md 时不覆盖（协议文档首次生成、内容不因重复引导变化）
        from xiaoli_app import setup as _setup
        os.makedirs(self.tasks_dir, exist_ok=True)
        p = os.path.join(self.tasks_dir, "README.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("自定义内容")
        made = _setup.ensure_bridge_readme(self.tasks_dir)
        self.assertFalse(made, "已存在 README.md 时不应覆盖")
        with open(p, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "自定义内容")


def config_store_default_tasks():
    from xiaoli_app import config_store
    return config_store.default_tasks_dir()


def config_store_default_memory():
    from xiaoli_app import config_store
    return config_store.default_memory_file()


if __name__ == "__main__":
    unittest.main()
