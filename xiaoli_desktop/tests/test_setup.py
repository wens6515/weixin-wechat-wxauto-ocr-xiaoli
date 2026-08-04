# -*- coding: utf-8 -*-
"""setup 模块测试：环境检测、天枢一键安装（下载进度/安全解压）、首轮提示词发送"""
import io
import json
import os
import shutil
import subprocess
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
        # 桌面端缺失且 CLI(rivet) 也不存在 → 未安装
        with mock.patch("shutil.which", return_value=None):
            r = self._report(make_cfg(tianshu_install_dir=self.tmp))
        self.assertFalse(r["tianshu"]["ok"])

    def test_tianshu_cli_detected(self):
        # CLI(rivet) 存在即算已安装（即使桌面端缺失）
        with mock.patch("shutil.which", return_value=r"C:\npm\rivet.cmd"):
            r = self._report(make_cfg(tianshu_install_dir=self.tmp))
        self.assertTrue(r["tianshu"]["ok"])
        self.assertIn("rivet", r["tianshu"]["detail"])

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

    def test_double_dot_filename_allowed(self):
        # 合法双点文件名（..foo.txt）不应被 zip-slip 守卫误拒——守卫只挡
        # normpath 后首段为 ".." 的目录穿越，不挡以 ".." 开头的普通文件名
        data = self._zip_bytes([("..foo.txt", "hi")])
        with mock.patch("requests.get", return_value=self._fake_response(data)):
            dest = setup.install_tianshu(self.tmp)
        self.assertTrue(os.path.isfile(os.path.join(dest, "..foo.txt")),
                        f"合法双点文件名应正常解压: {os.listdir(dest)}")

    def test_http_error_raises(self):
        resp = mock.Mock()
        resp.raise_for_status = mock.Mock(side_effect=RuntimeError("404"))
        with mock.patch("requests.get", return_value=resp):
            with self.assertRaises(RuntimeError):
                setup.install_tianshu(self.tmp)


class TestLaunchTianshu(unittest.TestCase):
    """launch_tianshu：启动天枢 CLI（rivet），在 tianshu_workdir 下开新窗口"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="launch_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_launch_cli_in_workdir(self):
        started = {}

        def fake_popen(cmd, **kw):
            started["cmd"] = cmd
            started["cwd"] = kw.get("cwd")
            started["flags"] = kw.get("creationflags")
            return mock.Mock()

        cfg = {"tianshu_workdir": self.tmp,
               "tianshu_download_url": setup.DEFAULT_TIANSHU_URL}
        # rivet 命令存在（mock 探测）
        with mock.patch("shutil.which", return_value=r"C:\npm\rivet.cmd"), \
             mock.patch("subprocess.Popen", side_effect=fake_popen) as m:
            ok, detail = setup.launch_tianshu(cfg)
        self.assertTrue(ok, detail)
        self.assertEqual(started["cwd"], self.tmp, "应在 tianshu_workdir 下启动")
        flags = started.get("flags") or 0
        self.assertTrue(flags & getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                        "应使用 CREATE_NEW_CONSOLE 开新控制台窗口")
        joined = " ".join(str(x) for x in started["cmd"]).lower()
        self.assertNotIn("tianshu", joined,
                         f"不得把 CLI 窗口标题设为 Tianshu（与 _is_desktop 冲突→CLI 被误判桌面端）: {started['cmd']}")
        self.assertIn("rivet", joined, f"应启动 rivet CLI: {started['cmd']}")
        m.assert_called_once()

    def test_launch_rivet_missing(self):
        cfg = {"tianshu_workdir": self.tmp,
               "tianshu_download_url": setup.DEFAULT_TIANSHU_URL}
        with mock.patch("shutil.which", return_value=None):
            ok, detail = setup.launch_tianshu(cfg)
        self.assertFalse(ok)
        self.assertIn("rivet", detail)
        self.assertIn("npm", detail, "提示应包含 npm install 指引")


class TestResolveCliWindow(unittest.TestCase):
    """resolve_cli_window：定位 CLI 窗口，杜绝把提示词发给桌面端窗口"""

    def setUp(self):
        # launch 冷却护栏使用模块级状态，测试间必须重置（否则前一个测试的
        # launch 会让后续测试的 resolve 跳过第 3 级 launch，断言误红）
        setup._last_launch_mono = None

    def test_uses_manual_config_title(self):
        cfg = {"tianshu_window_title": "npm"}  # 用户手动配置的 CLI 标题
        title, detail = setup.resolve_cli_window(
            cfg, list_windows_fn=lambda: ["npm", "微信"],
            console_windows_fn=lambda: ["npm"])  # CLI 是控制台窗口
        self.assertEqual(title, "npm")
        self.assertEqual(detail, "")

    def test_ignores_desktop_polluted_title(self):
        # RED 复现：config.json 被环境检查污染为桌面端「天枢 · Tianshu」时，
        # 必须忽略该值并启动 CLI，而不是把提示词发给桌面端窗口
        cfg = {"tianshu_window_title": "天枢 · Tianshu"}
        called = {"launch": False}
        wins = ["天枢 · Tianshu", "微信"]  # 启动前：桌面端在运行

        def fake_list():
            return list(wins)

        def fake_launch(cfg2):
            called["launch"] = True
            wins.append("npm")  # 启动后新增 CLI 窗口
            return True, "ok"

        def fake_console():
            # 控制台枚举：启动前无 CLI 窗口（逼走第 3 级），启动后 CLI 出现
            return ["npm"] if called["launch"] else []

        title, detail = setup.resolve_cli_window(
            cfg, list_windows_fn=fake_list,
            launch_fn=fake_launch, sleep_fn=lambda s: None,
            console_windows_fn=fake_console)  # 无控制台 CLI → 走第 3 级启动
        self.assertTrue(called["launch"], "桌面端污染值不应阻止 CLI 启动")
        self.assertEqual(title, "npm", "应把提示词发给 CLI 新增窗口而非桌面端")

    def test_launches_cli_and_finds_new_window(self):
        cfg = {}
        wins = ["微信"]

        def fake_list():
            return list(wins)

        def fake_launch(cfg2):
            wins.append("npm")
            return True, "ok"

        def fake_console():
            return ["npm"] if "npm" in wins else []

        title, _ = setup.resolve_cli_window(
            cfg, list_windows_fn=fake_list,
            launch_fn=fake_launch, sleep_fn=lambda s: None,
            console_windows_fn=fake_console)
        self.assertEqual(title, "npm")

    def test_launch_failure_reports(self):
        cfg = {}
        title, detail = setup.resolve_cli_window(
            cfg, list_windows_fn=lambda: [],
            launch_fn=lambda c: (False, "未找到 rivet 命令"),
            sleep_fn=lambda s: None, console_windows_fn=lambda: [])
        self.assertEqual(title, "")
        self.assertIn("rivet", detail)

    def test_skips_desktop_aux_window(self):
        # 桌面端 Electron 辅助窗口标题不含中文「天枢」但含 tianshu——
        # 上一版 CLI 特征匹配会误命中它，把提示词发到桌面端
        cfg = {}
        called = {"launch": False}
        wins = ["天枢 · Tianshu", "app.tianshu.desktop-siw", "微信"]

        def fake_list():
            return list(wins)

        def fake_launch(cfg2):
            called["launch"] = True
            wins.append("npm")  # CLI 启动后新增窗口（npm prefix）
            return True, "ok"

        def fake_console():
            # 桌面端辅助窗口不是控制台类，天然不进控制台枚举；启动后 CLI 进入
            return ["npm"] if called["launch"] else []

        title, _ = setup.resolve_cli_window(
            cfg, list_windows_fn=fake_list,
            launch_fn=fake_launch, sleep_fn=lambda s: None,
            console_windows_fn=fake_console)  # 第 2 级天然排除桌面端辅助窗口
        self.assertTrue(called["launch"], "桌面端辅助窗口不应被当作 CLI")
        self.assertEqual(title, "npm", "应跳过 app.tianshu.* 辅助窗口，只认 CLI 新增窗口")

    def test_skips_english_only_desktop_title(self):
        # 纯英文桌面端窗口标题「Tianshu」（无中文「天枢」）也不得被第 2 级
        # CLI 特征匹配误判为 CLI 窗口——首轮提示词发错目标是 commit c67b995 的场景
        cfg = {}
        called = {"launch": False}
        wins = ["Tianshu", "微信"]  # 启动前：纯英文标题的桌面端在运行

        def fake_list():
            return list(wins)

        def fake_launch(cfg2):
            called["launch"] = True
            wins.append("npm")  # 启动后新增 CLI 窗口
            return True, "ok"

        def fake_console():
            # 纯英文桌面端「Tianshu」不是控制台类，不进控制台枚举；启动后 CLI 进入
            return ["npm"] if called["launch"] else []

        title, _ = setup.resolve_cli_window(
            cfg, list_windows_fn=fake_list,
            launch_fn=fake_launch, sleep_fn=lambda s: None,
            console_windows_fn=fake_console)
        self.assertTrue(called["launch"], "纯英文桌面端标题不应阻止 CLI 启动")
        self.assertEqual(title, "npm", "应把提示词发给 CLI 新增窗口而非纯英文桌面端")

    def test_never_sends_to_new_desktop_window(self):
        # RED 复现：CLI 启动后新增的全是桌面端窗口（桌面端慢启动、CLI 未出现）——
        # 绝不得把提示词发给桌面端；第 3 级必须过滤桌面端并继续轮询，
        # 最终报「窗口未出现」而非把首轮提示词发到桌面端窗口。
        cfg = {}
        wins = ["微信"]

        def fake_list():
            return list(wins)

        def fake_launch(cfg2):
            wins.append("天枢 · Tianshu")  # 桌面端慢启动，CLI 未出现
            return True, "ok"

        def fake_console():
            # 桌面端不是控制台类，CLI 未出现 → 控制台枚举始终为空
            return []

        title, detail = setup.resolve_cli_window(
            cfg, list_windows_fn=fake_list,
            launch_fn=fake_launch, sleep_fn=lambda s: None,
            console_windows_fn=fake_console)
        self.assertEqual(title, "", "新增窗口全是桌面端时不得把提示词发给桌面端")
        self.assertIn("未出现", detail)

    def test_skips_decoy_npm_window(self):
        # RED 复现（对抗审查反例 C1，fail-open）：浏览器/编辑器等非控制台窗口
        # 标题含 "npm"（如 "npm docs - Mozilla Firefox"）且枚举先于真实 CLI——
        # 修复前第 2 级对全量窗口裸子串匹配会选中诱饵，提示词粘贴进错误窗口。
        # 修复后第 2 级只匹配控制台窗口（console_windows_fn），诱饵天然排除。
        cfg = {}
        decoy_first = ["npm docs - Mozilla Firefox", "微信", "npm"]  # 诱饵先于真实 CLI
        title, detail = setup.resolve_cli_window(
            cfg,
            list_windows_fn=lambda: list(decoy_first),
            console_windows_fn=lambda: ["npm"],  # 真实 CLI 是控制台窗口
            sleep_fn=lambda s: None)
        self.assertEqual(title, "npm",
                         "第 2 级应跳过诱饵窗口，只匹配控制台里的真实 CLI")

    def test_console_no_cli_skips_decoy(self):
        # RED 复现：控制台枚举正常但没有 CLI 窗口——即使全量里有标题含 npm 的
        # 诱饵窗口，也不得从中选（宁缺毋滥），应进入第 3 级启动 CLI。
        # 修复前第 2 级全量裸子串匹配会选中诱饵，launch 不被调用。
        cfg = {}
        called = {"launch": False}
        wins = ["npm docs - Mozilla Firefox", "微信"]

        def fake_list():
            return list(wins)

        def fake_launch(cfg2):
            called["launch"] = True
            wins.append("npm")  # 真实 CLI 启动后新增窗口
            return True, "ok"

        def fake_console():
            # 启动前系统无控制台 CLI（诱饵是浏览器，非控制台类）；
            # 启动后真实 CLI（npm）进入控制台枚举
            return ["npm"] if called["launch"] else []

        title, _ = setup.resolve_cli_window(
            cfg, list_windows_fn=fake_list,
            console_windows_fn=fake_console,
            launch_fn=fake_launch, sleep_fn=lambda s: None)
        self.assertTrue(called["launch"], "无控制台 CLI 时应启动而非选诱饵窗口")
        self.assertEqual(title, "npm", "应把提示词发给启动后新增的 CLI 窗口")

    def test_stage3_duplicate_title_fallback(self):
        # RED 复现（第 3 级兜底 dead code）：新 CLI 窗口标题与既有窗口重复
        # （同为 "npm"）时，旧实现用 set 差集 + set 长度判断——标题重复则差集为空、
        # 长度不变，len(wins) > len(before_wins) 恒 False，兜底永不触发，最终报
        # 「窗口未出现」。修复后按列表保留重复标题、比较窗口总数并选 CLI 特征窗口。
        cfg = {}
        wins = ["微信", "npm"]  # 启动前已有一个 "npm" 窗口

        def fake_list():
            return list(wins)

        def fake_launch(cfg2):
            wins.append("npm")  # 新 CLI 窗口标题与既有 "npm" 重复
            return True, "ok"

        def fake_console():
            # 启动前控制台里没有 CLI（既有 "npm" 是桌面端/非控制台窗口——
            # 第 2 级不会命中它）；启动后真实 CLI（npm）进入控制台枚举。
            # 新实现第 3 级按 CLI 特征窗口名定位，不依赖"新增窗口差集"——
            # 标题重复时差集为空会漏判，按名称定位天然免疫。
            return ["npm"] if len(wins) > 2 else []

        title, detail = setup.resolve_cli_window(
            cfg, list_windows_fn=fake_list,
            console_windows_fn=fake_console,
            launch_fn=fake_launch, sleep_fn=lambda s: None)
        self.assertEqual(title, "npm",
                         "新 CLI 标题与既有窗口重复时按 CLI 特征窗口名定位应命中")
        self.assertEqual(detail, "")

    def test_no_repeat_launch_within_cooldown(self):
        # RED 复现：CLI 窗口枚举不到/标题不匹配时（用户实测：初始化发首轮
        # 提示词，resolve 每次都走到第 3 级 launch → 每轮都开新 CLI 窗口，
        # 窗口一个接一个地开，循环不止）。冷却期内第 3 级不得重复 launch，
        # 只轮询已启动的窗口——launch 幂等护栏，从机制上切断窗口风暴。
        cfg = {}
        launch_count = {"n": 0}
        wins = ["微信"]

        def fake_list():
            return list(wins)

        def fake_launch(cfg2):
            launch_count["n"] += 1
            wins.append("npm")
            return True, "ok"

        def fake_console():
            # 模拟枚举异常/标题不匹配：CLI 窗口存在但从未被枚举到
            return []

        # 第一次：launch 1 次，轮询 15 轮也找不到 → 返回失败
        title1, _ = setup.resolve_cli_window(
            cfg, list_windows_fn=fake_list, launch_fn=fake_launch,
            sleep_fn=lambda s: None, console_windows_fn=fake_console)
        self.assertEqual(title1, "")
        self.assertEqual(launch_count["n"], 1)

        # 冷却期内第二次：不得再 launch（窗口风暴护栏）
        title2, _ = setup.resolve_cli_window(
            cfg, list_windows_fn=fake_list, launch_fn=fake_launch,
            sleep_fn=lambda s: None, console_windows_fn=fake_console)
        self.assertEqual(launch_count["n"], 1,
                         "冷却期内不得重复 launch 新 CLI 窗口（窗口风暴护栏）")
        self.assertEqual(title2, "")


class TestSendPrompt(unittest.TestCase):
    def test_send_prompt_returns_ok(self):
        sent = {}

        def fake_trigger(title, command, hold=0.5, enter_times=1):
            sent["title"] = title
            sent["command"] = command
            sent["enter_times"] = enter_times
            return True

        with mock.patch.object(setup, "_send_trigger_to_window", side_effect=fake_trigger):
            ok = setup.send_prompt_to_tianshu("你好天枢", "天枢窗口")
        self.assertTrue(ok)
        self.assertEqual(sent["title"], "天枢窗口")
        self.assertEqual(sent["command"], "你好天枢")

    def test_send_prompt_presses_enter_twice(self):
        """首轮提示词：粘贴后连续按两次回车（CLI 实测一次回车不提交）"""
        sent = {}

        def fake_trigger(title, command, hold=0.5, enter_times=1):
            sent["enter_times"] = enter_times
            return True

        with mock.patch.object(setup, "_send_trigger_to_window", side_effect=fake_trigger):
            ok = setup.send_prompt_to_tianshu("首轮提示词", "npm")
        self.assertTrue(ok)
        self.assertEqual(sent["enter_times"], 2, "首轮提示词必须按两次回车")

    def test_send_prompt_window_missing(self):
        with mock.patch.object(setup, "_send_trigger_to_window", return_value=False):
            ok = setup.send_prompt_to_tianshu("x", "不存在")
        self.assertFalse(ok)


class TestConfigureTianshuAuto(unittest.TestCase):
    """已有天枢 CLI 时配置完全自动（YOLO）：任务处理无需手动确认回车。

    背景：天枢 CLI 默认 approval=suggest/Auto——高风险工具仍需用户手动确认，
    小漓无人值守投递任务后任务会卡在确认等待，无法全自动回复。
    配置 dangerously-skip-permissions 后启动即 YOLO，全程无刹车。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="auto_approval_")
        self.rivet_dir = os.path.join(self.tmp, ".rivet")
        os.makedirs(self.rivet_dir, exist_ok=True)
        self.config_path = os.path.join(self.rivet_dir, "config.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _call(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp}), \
             mock.patch.object(setup, "_tianshu_config_paths",
                               side_effect=lambda p: [p]):
            return setup.configure_tianshu_auto_approval({})

    def test_no_rivet_skips(self):
        # 无 rivet 命令 → 跳过（返回 False，不报错）
        with mock.patch("shutil.which", return_value=None):
            ok, detail = self._call()
        self.assertFalse(ok)
        self.assertIn("rivet", detail)

    def test_already_configured_skips(self):
        # config.json 已是 dangerously-skip-permissions（agent 子对象）→ 不重复执行命令
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"agent": {"approval": "dangerously-skip-permissions"}}, f)
        ran = []
        with mock.patch("shutil.which", return_value="rivet"), \
             mock.patch.object(subprocess, "run", side_effect=lambda *a, **k: ran.append(a)):
            ok, detail = self._call()
        self.assertTrue(ok)
        self.assertEqual(ran, [], "已配置时不应重复执行 set-approval")

    def test_not_configured_runs_command(self):
        # config 缺失或未配置 → 执行 rivet config set-approval dangerously-skip-permissions
        ran = []

        class FakeCompleted:
            returncode = 0

        def fake_run(args, **kw):
            ran.append(args)
            return FakeCompleted()

        with mock.patch("shutil.which", return_value="rivet"), \
             mock.patch.object(subprocess, "run", side_effect=fake_run):
            ok, detail = self._call()
        self.assertTrue(ok)
        self.assertEqual(ran, [["rivet", "config", "set-approval",
                                "dangerously-skip-permissions"]],
                         "未配置时应执行 set-approval 命令")
        self.assertIn("YOLO", detail)

    def test_top_level_approval_not_enough(self):
        # 幂等判定必须读 agent.approval（真实结构）；顶层 approval 是旧/错误结构，
        # 视为未配置 → 执行 set-approval，避免结构变化导致静默跳过
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"approval": "dangerously-skip-permissions"}, f)
        ran = []

        class FakeCompleted:
            returncode = 0

        def fake_run(args, **kw):
            ran.append(args)
            return FakeCompleted()

        with mock.patch("shutil.which", return_value="rivet"), \
             mock.patch.object(subprocess, "run", side_effect=fake_run):
            ok, detail = self._call()
        self.assertTrue(ok)
        self.assertEqual(ran, [["rivet", "config", "set-approval",
                                "dangerously-skip-permissions"]],
                         "顶层 approval 不算已配置，应执行 set-approval")

    def test_desktop_data_root_also_configured(self):
        """桌面端便携根（TianshuData\.rivet）未配置时也必须执行 set-approval。

        用户环境：CLI 数据根已 YOLO，但桌面端 TianshuData\.rivet\config.json
        仍 suggest——切到桌面端运行天枢时同样会卡确认。多数据根任一未配置
        都必须执行（rivet config set-approval 写 CLI 主根；桌面端根由
        后续 _tianshu_config_paths 检查驱动重跑直至全配）。
        """
        # 主根已配置
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump({"agent": {"approval": "dangerously-skip-permissions"}}, f)
        # 模拟存在一个未配置的桌面端根
        desk_root = os.path.join(self.tmp, "TianshuData", ".rivet")
        os.makedirs(desk_root, exist_ok=True)
        with open(os.path.join(desk_root, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"agent": {"approval": "suggest"}}, f)
        ran = []

        class FakeCompleted:
            returncode = 0

        def fake_run(args, **kw):
            ran.append(args)
            return FakeCompleted()

        with mock.patch("shutil.which", return_value="rivet"), \
             mock.patch.object(subprocess, "run", side_effect=fake_run), \
             mock.patch.dict(os.environ, {"LOCALAPPDATA": self.tmp}), \
             mock.patch.object(
                 setup, "_tianshu_config_paths",
                 side_effect=lambda p: [p, os.path.join(desk_root, "config.json")]):
            ok, detail = setup.configure_tianshu_auto_approval({})
        self.assertTrue(ok)
        self.assertEqual(ran, [["rivet", "config", "set-approval",
                                "dangerously-skip-permissions"]],
                         "桌面端根未配置时必须执行 set-approval")

    def test_command_failure_reports(self):
        # set-approval 失败 → 返回失败与原因（不崩溃）
        class FakeCompleted:
            returncode = 1

        with mock.patch("shutil.which", return_value="rivet"), \
             mock.patch.object(subprocess, "run", return_value=FakeCompleted()):
            ok, detail = self._call()
        self.assertFalse(ok)
        self.assertIn("失败", detail)


class TestUnattendedMode(unittest.TestCase):
    """完全自动（无人值守）配置：CLI 启动禁 Plan Mode + 提示词禁审批等待。"""

    def test_launch_tianshu_disables_plan_mode(self):
        # RED 复现：任务卡在确认的第二个来源——复杂任务自动进入 Plan Mode
        # 等 /plan-approve 审批（README：RIVET_PLAN_MODE_SUGGEST 默认 auto）。
        # 修复后启动命令必须注入 RIVET_PLAN_MODE_SUGGEST=0。
        seen = {}

        def fake_popen(cmd, **kw):
            seen["cmd"] = cmd
            return mock.MagicMock()

        with mock.patch("shutil.which", return_value="rivet"), \
             mock.patch.object(subprocess, "Popen", side_effect=fake_popen):
            ok, detail = setup.launch_tianshu({})
        self.assertTrue(ok)
        cmd_str = " ".join(seen["cmd"])
        self.assertIn("RIVET_PLAN_MODE_SUGGEST=0", cmd_str,
                      "CLI 启动必须关闭自动进入 Plan Mode，否则任务卡在审批")

    def test_first_prompt_forbids_approval_wait(self):
        # 提示词必须声明无人值守：不进入 Plan Mode、不请求确认、直接执行
        prompt = setup.build_first_prompt({"tasks_dir": r"D:\tasks"})
        self.assertIn("无人值守", prompt)
        self.assertIn("Plan Mode", prompt)
        self.assertIn("不要向用户请求任何确认", prompt)


class TestFirstRunGuide(unittest.TestCase):
    """首次启动一次性引导：/yes 全自动（持久化）+ 关闭 CLI 窗口"""

    def test_send_yes_and_close_sends_yes_then_closes(self):
        calls = []

        def fake_send(title, command, hold=0.5, enter_times=1):
            calls.append(("send", title, command, enter_times))
            return True

        def fake_close(title):
            calls.append(("close", title))
            return True

        ok = setup.send_yes_and_close(
            "npm prefix", sleep_fn=lambda s: None,
            send_fn=fake_send, close_fn=fake_close)
        self.assertTrue(ok)
        self.assertEqual(calls, [("send", "npm prefix", "/yes", 1), ("close", "npm prefix")],
                         "必须先发 /yes 再关闭窗口（/yes 持久化全自动，重启后仍生效）")

    def test_close_window_by_title_posts_wm_close_to_matching(self):
        from unittest import mock
        import ctypes
        posted = []

        class FakeUser32:
            _titles = {1: "npm prefix", 2: "微信"}

            def GetWindowTextW(self, hwnd, buf, n):
                t = self._titles.get(hwnd, "")
                buf.value = t[:n]
                return len(t)

            def EnumWindows(self, proc, _lp):
                proc(1, 0)
                proc(2, 0)
                return True

            def PostMessageW(self, hwnd, msg, _w, _l):
                posted.append((hwnd, msg))
                return True

        with mock.patch.object(ctypes.windll, "user32", FakeUser32()), \
             mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)):
            ok = setup.close_window_by_title("npm", sleep_fn=lambda s: None)
        self.assertTrue(ok)
        self.assertEqual(posted, [(1, 0x0010)],
                         "只向标题匹配的窗口发 WM_CLOSE（等效用户点 X），不匹配的窗口不动")

    def test_run_first_run_guide_marks_guided_on_confirm(self):
        import tempfile
        import os
        tmp = tempfile.mkdtemp(prefix="guide_")
        cfg_path = os.path.join(tmp, "config.json")
        try:
            cfg = {"tasks_dir": r"D:\tasks"}
            flow = []

            def fake_detect():
                return r"C:\Users\me\AppData\Roaming\npm\rivet"

            def fake_resolve(c):
                flow.append(("resolve", c))
                return "npm prefix", ""

            def fake_send(title, command, hold=0.5, enter_times=1):
                flow.append(("send", command))
                return True

            def fake_close(title):
                flow.append(("close",))
                return True

            def fake_dialog(title, text, buttons=None):
                flow.append(("dialog", title))
                return "确认完成"

            ok = setup.run_first_run_guide(
                cfg, cfg_path=cfg_path, detect_fn=fake_detect,
                resolve_fn=fake_resolve, send_fn=fake_send,
                close_fn=fake_close, sleep_fn=lambda s: None,
                dialog_fn=fake_dialog)
            self.assertTrue(ok)
            self.assertTrue(cfg.get("tianshu_guided"),
                            "确认完成后必须标记 tianshu_guided（此后初始化不再切 YOLO）")
            self.assertIn(("send", "/yes"), flow, "确认后必须发送 /yes")
            self.assertIn(("close",), flow, "发送 /yes 后必须关闭 CLI 窗口")
            # 顺序：先定位窗口，再弹窗，再 /yes，再关闭
            self.assertEqual(flow[0], ("resolve", cfg))
            self.assertEqual(flow[1], ("dialog", "天枢 CLI 首次配置引导"))
            self.assertEqual(flow[-2], ("send", "/yes"))
            self.assertEqual(flow[-1], ("close",))
            # 标记已落盘
            with open(cfg_path, "r", encoding="utf-8") as f:
                import json
                disk = json.load(f)
            self.assertTrue(disk.get("tianshu_guided"), "tianshu_guided 必须写入 config.json")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_run_first_run_guide_later_does_not_mark(self):
        cfg = {}

        def fake_dialog(title, text, buttons=None):
            return "稍后再说"

        ok = setup.run_first_run_guide(
            cfg, detect_fn=lambda: "rivet",
            resolve_fn=lambda c: ("npm prefix", ""),
            sleep_fn=lambda s: None, dialog_fn=fake_dialog)
        self.assertFalse(ok)
        self.assertFalse(cfg.get("tianshu_guided", False),
                         "「稍后再说」不得标记 guided（设置页可重跑引导）")

    def test_run_first_run_guide_installs_cli_when_missing(self):
        cfg = {}
        flow = []

        def fake_detect():
            return ""  # 未安装 rivet

        def fake_install():
            flow.append("install")
            return True

        def fake_resolve(c):
            flow.append("resolve")
            return "npm prefix", ""

        def fake_dialog(title, text, buttons=None):
            flow.append("dialog")
            return "确认完成"

        ok = setup.run_first_run_guide(
            cfg, detect_fn=fake_detect, install_fn=fake_install,
            resolve_fn=fake_resolve, send_fn=lambda *a, **k: True,
            close_fn=lambda *a, **k: True,
            sleep_fn=lambda s: None, dialog_fn=fake_dialog)
        self.assertTrue(ok)
        self.assertEqual(flow[0], "install", "CLI 缺失时必须先尝试安装")
        self.assertTrue(cfg.get("tianshu_guided"))

    def test_run_first_run_guide_install_failure_reports(self):
        cfg = {}
        shown = []

        def fake_detect():
            return ""

        def fake_install():
            return False

        def fake_dialog(title, text, buttons=None):
            shown.append((title, text))
            return ""

        ok = setup.run_first_run_guide(
            cfg, detect_fn=fake_detect, install_fn=fake_install,
            sleep_fn=lambda s: None, dialog_fn=fake_dialog)
        self.assertFalse(ok)
        self.assertEqual(shown[0][0], "未找到天枢 CLI", "安装失败必须弹窗提示手动安装")
        self.assertIn("npm install -g tianshu-tui", shown[0][1])


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
