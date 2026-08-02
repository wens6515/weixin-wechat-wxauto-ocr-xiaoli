# -*- coding: utf-8 -*-
"""setup 模块测试：环境检测、天枢一键安装（下载进度/安全解压）、首轮提示词发送"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app import setup


def make_cfg(**over):
    cfg = {
        "tianshu_install_dir": "",
        "tianshu_download_url": setup.DEFAULT_TIANSHU_URL,
        "first_prompt_path": "",
    }
    cfg.update(over)
    return cfg


class TestCheckEnvironment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="setup_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _report(self, cfg):
        with mock.patch.object(setup, "_list_windows", return_value=[]):
            return setup.check_environment(cfg)

    def test_report_shape(self):
        r = self._report(make_cfg())
        for k in ("wechat", "tianshu", "first_prompt"):
            self.assertIn(k, r)
            self.assertIn("ok", r[k])
            self.assertIn("detail", r[k])

    def test_wechat_window_detected(self):
        with mock.patch.object(setup, "_list_windows", return_value=["微信", "QQ"]):
            r = setup.check_environment(make_cfg())
        self.assertTrue(r["wechat"]["ok"])

    def test_wechat_missing(self):
        with mock.patch.object(setup, "_list_windows", return_value=["QQ"]):
            r = setup.check_environment(make_cfg())
        self.assertFalse(r["wechat"]["ok"])

    def test_tianshu_dir_detected(self):
        exe = os.path.join(self.tmp, "tianshu-desktop.exe")
        open(exe, "w").close()
        r = self._report(make_cfg(tianshu_install_dir=self.tmp))
        self.assertTrue(r["tianshu"]["ok"])

    def test_tianshu_dir_missing(self):
        r = self._report(make_cfg(tianshu_install_dir=self.tmp))
        self.assertFalse(r["tianshu"]["ok"])

    def test_first_prompt_detected(self):
        p = os.path.join(self.tmp, "首轮提示词.txt")
        open(p, "w", encoding="utf-8").write("你好")
        r = self._report(make_cfg(first_prompt_path=p))
        self.assertTrue(r["first_prompt"]["ok"])
        self.assertEqual(r["first_prompt"]["detail"], p)

    def test_first_prompt_builtin_fallback(self):
        # 自定义文件缺失 → 回退内置模板，检测仍就绪（内化后不再依赖外部文件）
        r = self._report(make_cfg(first_prompt_path=os.path.join(self.tmp, "nope.txt")))
        self.assertTrue(r["first_prompt"]["ok"])
        self.assertIn("内置", r["first_prompt"]["detail"])


class TestBuildFirstPrompt(unittest.TestCase):
    """build_first_prompt：自定义文件优先，否则内置模板（tasks_dir/trigger 填充）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="prompt_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self, **over):
        cfg = {
            "first_prompt_path": "",
            "tasks_dir": os.path.join(self.tmp, "tasks"),
            "tianshu_trigger_command": "开始处理",
        }
        cfg.update(over)
        return cfg

    def test_file_priority(self):
        p = os.path.join(self.tmp, "自定义.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("自定义内容")
        self.assertEqual(setup.build_first_prompt(self._cfg(first_prompt_path=p)), "自定义内容")

    def test_builtin_when_path_empty(self):
        text = setup.build_first_prompt(self._cfg())
        self.assertIn("开始处理", text)
        self.assertIn(os.path.join(self.tmp, "tasks"), text)
        self.assertNotIn("{tasks_dir}", text)
        self.assertNotIn("{trigger}", text)

    def test_builtin_when_file_missing(self):
        text = setup.build_first_prompt(self._cfg(first_prompt_path=os.path.join(self.tmp, "nope.txt")))
        self.assertIn("开始处理", text)
        self.assertNotIn("{trigger}", text)

    def test_builtin_custom_trigger(self):
        text = setup.build_first_prompt(self._cfg(tianshu_trigger_command="开工"))
        self.assertIn("开工", text)
        self.assertNotIn("{trigger}", text)


class TestBridgeReadme(unittest.TestCase):
    """ensure_bridge_readme：首次生成协议文档，已存在不覆盖"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="readme_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_readme(self):
        tasks_dir = os.path.join(self.tmp, "tasks")
        setup.ensure_bridge_readme(tasks_dir)
        p = os.path.join(tasks_dir, "README.md")
        self.assertTrue(os.path.isfile(p))
        with open(p, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("task.json", content)
        self.assertIn("result.json", content)

    def test_does_not_overwrite_existing(self):
        tasks_dir = os.path.join(self.tmp, "tasks")
        os.makedirs(tasks_dir, exist_ok=True)
        p = os.path.join(tasks_dir, "README.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("用户自定义")
        setup.ensure_bridge_readme(tasks_dir)
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read(), "用户自定义")


class TestInstallTianshu(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="setup_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _zip_bytes(self, members):
        """构造内存 zip：members = [(name, content), ...]"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, content in members:
                zf.writestr(name, content)
        return buf.getvalue()

    def _fake_response(self, data, total=None):
        resp = mock.Mock()
        resp.status_code = 200
        resp.headers = {"content-length": str(total if total is not None else len(data))}
        resp.raise_for_status = mock.Mock()
        resp.iter_content = mock.Mock(return_value=iter([data]))
        return resp

    def test_download_progress_monotonic(self):
        data = self._zip_bytes([("Tianshu-Tui-main/readme.txt", "hi")])
        progress = []

        def cb(p):
            progress.append(p)

        with mock.patch("requests.get", return_value=self._fake_response(data, total=1000)):
            dest = setup.install_tianshu(self.tmp, progress_cb=cb)
        self.assertTrue(progress, "应有进度回调")
        self.assertEqual(progress, sorted(progress), "进度应单调不减")
        self.assertEqual(progress[-1], 100)

    def test_extract_creates_files(self):
        data = self._zip_bytes([
            ("Tianshu-Tui-main/tianshu-desktop.exe", "MZ..."),
            ("Tianshu-Tui-main/README.md", "readme"),
        ])
        with mock.patch("requests.get", return_value=self._fake_response(data)):
            dest = setup.install_tianshu(self.tmp)
        # 返回含 exe 的目录
        self.assertTrue(os.path.isfile(os.path.join(dest, "tianshu-desktop.exe")), str(os.listdir(self.tmp)))

    def test_zip_slip_rejected(self):
        data = self._zip_bytes([("../evil.txt", "pwn")])
        with mock.patch("requests.get", return_value=self._fake_response(data)):
            with self.assertRaises(ValueError):
                setup.install_tianshu(self.tmp)
        self.assertFalse(os.path.isfile(os.path.join(os.path.dirname(self.tmp), "evil.txt")),
                         "zip-slip 成员不得写出")

    def test_http_error_raises(self):
        resp = mock.Mock()
        resp.raise_for_status = mock.Mock(side_effect=RuntimeError("404"))
        with mock.patch("requests.get", return_value=resp):
            with self.assertRaises(RuntimeError):
                setup.install_tianshu(self.tmp)


class TestSendPrompt(unittest.TestCase):
    def test_send_prompt_returns_ok(self):
        sent = {}

        def fake_trigger(title, command, hold=0.5):
            sent["title"] = title
            sent["command"] = command
            return True

        with mock.patch.object(setup, "_send_trigger_to_window", side_effect=fake_trigger):
            ok = setup.send_prompt_to_tianshu("你好天枢", "天枢窗口")
        self.assertTrue(ok)
        self.assertEqual(sent["title"], "天枢窗口")
        self.assertEqual(sent["command"], "你好天枢")

    def test_send_prompt_window_missing(self):
        with mock.patch.object(setup, "_send_trigger_to_window", return_value=False):
            ok = setup.send_prompt_to_tianshu("x", "不存在")
        self.assertFalse(ok)


class TestConfigDefaults(unittest.TestCase):
    def test_new_defaults_present(self):
        from xiaoli_app import config_store
        tmp = tempfile.mkdtemp(prefix="cfg_")
        try:
            cfg_path = os.path.join(tmp, "config.json")
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"bot_nickname": "x"}, f)
            cfg = config_store.load_config_store(cfg_path, os.path.join(tmp, "cards"))
            for k in ("tianshu_install_dir", "tianshu_download_url", "first_prompt_path"):
                self.assertIn(k, cfg, k)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
