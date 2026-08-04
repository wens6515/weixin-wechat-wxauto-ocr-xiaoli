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

    def test_launch_cli_prefers_tasks_dir_as_cwd(self):
        """RED 复现：CLI 必须在用户选的工作间（tasks_dir）打开——历史用
        tianshu_workdir（tasks_dir 父目录）导致 agent 工作文件（.rivet 等）
        落在程序目录旁而不是用户选的工作间（用户实测观察）。"""
        started = {}

        def fake_popen(cmd, **kw):
            started["cwd"] = kw.get("cwd")
            return mock.Mock()

        cfg = {"tasks_dir": self.tmp,
               "tianshu_workdir": os.path.dirname(self.tmp),  # 历史行为：父目录
               "tianshu_download_url": setup.DEFAULT_TIANSHU_URL}
        with mock.patch("shutil.which", return_value=r"C:\npm\rivet.cmd"), \
             mock.patch("subprocess.Popen", side_effect=fake_popen):
            ok, detail = setup.launch_tianshu(cfg)
        self.assertTrue(ok, detail)
        self.assertEqual(started["cwd"], self.tmp,
                         "CLI 应在用户选的工作间（tasks_dir）打开，而非其父目录")

    def test_launch_cli_sets_stable_title_npm_prefix(self):
        """RED 复现：Win11 默认终端（Windows Terminal）下 cmd /k 新窗口标题
        不稳定（用户实测：窗口名变了，看起来像 PowerShell 启动）→ resolve/
        监控找不到 npm prefix，引导/初始化链路断裂。修复：启动命令显式
        title npm prefix——任何终端宿主（conhost/WT）下窗口标题都稳定。"""
        seen = {}

        def fake_popen(cmd, **kw):
            seen["cmd"] = cmd
            return mock.Mock()

        with mock.patch("shutil.which", return_value=r"C:\npm\rivet.cmd"), \
             mock.patch("subprocess.Popen", side_effect=fake_popen):
            setup.launch_tianshu({"tianshu_workdir": self.tmp,
                                  "tianshu_download_url": setup.DEFAULT_TIANSHU_URL})
        cmd_str = " ".join(str(x) for x in seen["cmd"])
        self.assertIn("title npm prefix", cmd_str,
                      "启动命令必须显式设置窗口标题 npm prefix（防终端宿主差异）")
        self.assertIn("cmd", cmd_str, "必须用 cmd 启动（不是 PowerShell）")
        self.assertNotIn("tianshu", cmd_str.lower(),
                         "标题不得含 tianshu（与 _is_desktop 冲突→CLI 被误判桌面端）")

    def test_launch_records_pid_for_close(self):
        """RED 复现：close 需要按进程树杀 CLI（cmd→node）——但窗口进程
        （conhost/WT）与 CLI 进程无父子关系，进程树验证从窗口 PID 查不到
        rivet → close 回退失败、窗口关不掉。修复：launch_tianshu 记录
        启动的 cmd PID（_last_launch_pid），close 兜底用 taskkill /T 杀树。"""
        fake = mock.Mock()
        fake.pid = 4321
        with mock.patch("shutil.which", return_value=r"C:\npm\rivet.cmd"), \
             mock.patch("subprocess.Popen", return_value=fake):
            setup.launch_tianshu({"tianshu_workdir": self.tmp,
                                  "tianshu_download_url": setup.DEFAULT_TIANSHU_URL})
        self.assertEqual(setup._last_launch_pid, 4321,
                         "launch 后必须记录 CLI cmd 进程 PID（close 进程树兜底用）")

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
        cfg = {"tianshu_window_title": "npm prefix"}  # 用户手动配置的 CLI 标题
        title, detail = setup.resolve_cli_window(
            cfg, list_windows_fn=lambda: ["npm prefix", "微信"],
            console_windows_fn=lambda: ["npm prefix"])  # CLI 是控制台窗口
        self.assertEqual(title, "npm prefix")
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
            wins.append("npm prefix")  # 启动后新增 CLI 窗口
            return True, "ok"

        def fake_console():
            # 控制台枚举：启动前无 CLI 窗口（逼走第 3 级），启动后 CLI 出现
            return ["npm prefix"] if called["launch"] else []

        title, detail = setup.resolve_cli_window(
            cfg, list_windows_fn=fake_list,
            launch_fn=fake_launch, sleep_fn=lambda s: None,
            console_windows_fn=fake_console)  # 无控制台 CLI → 走第 3 级启动
        self.assertTrue(called["launch"], "桌面端污染值不应阻止 CLI 启动")
        self.assertEqual(title, "npm prefix", "应把提示词发给 CLI 新增窗口而非桌面端")

    def test_launches_cli_and_finds_new_window(self):
        cfg = {}
        wins = ["微信"]

        def fake_list():
            return list(wins)

        def fake_launch(cfg2):
            wins.append("npm prefix")
            return True, "ok"

        def fake_console():
            return ["npm prefix"] if "npm prefix" in wins else []

        title, _ = setup.resolve_cli_window(
            cfg, list_windows_fn=fake_list,
            launch_fn=fake_launch, sleep_fn=lambda s: None,
            console_windows_fn=fake_console)
        self.assertEqual(title, "npm prefix")

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
            wins.append("npm prefix")  # CLI 启动后新增窗口（npm prefix）
            return True, "ok"

        def fake_console():
            # 桌面端辅助窗口不是控制台类，天然不进控制台枚举；启动后 CLI 进入
            return ["npm prefix"] if called["launch"] else []

        title, _ = setup.resolve_cli_window(
            cfg, list_windows_fn=fake_list,
            launch_fn=fake_launch, sleep_fn=lambda s: None,
            console_windows_fn=fake_console)  # 第 2 级天然排除桌面端辅助窗口
        self.assertTrue(called["launch"], "桌面端辅助窗口不应被当作 CLI")
        self.assertEqual(title, "npm prefix", "应跳过 app.tianshu.* 辅助窗口，只认 CLI 新增窗口")

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
            wins.append("npm prefix")  # 启动后新增 CLI 窗口
            return True, "ok"

        def fake_console():
            # 纯英文桌面端「Tianshu」不是控制台类，不进控制台枚举；启动后 CLI 进入
            return ["npm prefix"] if called["launch"] else []

        title, _ = setup.resolve_cli_window(
            cfg, list_windows_fn=fake_list,
            launch_fn=fake_launch, sleep_fn=lambda s: None,
            console_windows_fn=fake_console)
        self.assertTrue(called["launch"], "纯英文桌面端标题不应阻止 CLI 启动")
        self.assertEqual(title, "npm prefix", "应把提示词发给 CLI 新增窗口而非纯英文桌面端")

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

    def test_process_has_rivet_checks_child_process(self):
        """RED 复现（用户实测）：CLI 是 cmd /k ... rivet 启动的——窗口 PID
        （conhost/WT/cmd）自身命令行不含 rivet，rivet 在子进程 node 里。
        旧实现只查窗口进程自身命令行（wmic 单进程）→ 永远 False →
        弱特征（Windows PowerShell 标题）认不出 CLI → 唤起时新开窗口。
        修复：进程树验证——自身或任一子进程命令行含 rivet 即命中。"""
        from unittest import mock
        # 模拟进程表（WT 场景父子链）：5000(WindowsTerminal) →
        # 5001(cmd /k rivet，命令行含 rivet) → 5002(node 跑 tianshu-tui)
        fake_ps = (
            '5000|0|C:\\\\Windows\\\\System32\\\\WindowsTerminal.exe\n'
            '5001|5000|cmd /k title npm prefix && set RIVET_PLAN_MODE_SUGGEST=0 && rivet\n'
            '5002|5001|"C:\\\\node.exe" --expose-gc "C:\\\\...\\\\tianshu-tui\\\\dist\\\\main.js" serve\n'
        )
        with mock.patch("subprocess.run") as m:
            m.return_value = mock.Mock(stdout=fake_ps, returncode=0)
            got = setup._process_has_rivet(5000)
        self.assertTrue(got,
                        "窗口进程自身命令行不含 rivet 时，子进程（node ... rivet）含 rivet 也应命中")

    def test_process_has_rivet_rejects_unrelated(self):
        """fail-open 反例：窗口进程树完全不含 rivet（用户自己开的 PowerShell）
        不得命中——提示词误发到无关窗口是 c67b995 场景的延续。"""
        from unittest import mock
        fake_ps = '6000|0|powershell.exe -NoLogo\n'
        with mock.patch("subprocess.run") as m:
            m.return_value = mock.Mock(stdout=fake_ps, returncode=0)
            got = setup._process_has_rivet(6000)
        self.assertFalse(got, "无 rivet 的进程树不得命中弱特征")

    def test_finds_cli_with_powershell_title(self):
        """RED 复现（用户实测）：Win11 默认终端（Windows Terminal）下 CLI
        窗口标题可能显示为「Windows PowerShell」（终端宿主接管标题，npm
        prefix 未生效）——修复前 _is_cli_feature 只认 npm prefix/rivet，
        resolve 找不到 CLI → 首轮提示词发不出去。
        修复：弱特征标题（Windows PowerShell 等终端默认名）在进程树含
        rivet 时认作 CLI（CLI 就是 cmd /k rivet 启动的窗口）。"""
        cfg = {}
        title, detail = setup.resolve_cli_window(
            cfg,
            console_windows_fn=lambda: [("Windows PowerShell", 1234)],
            process_has_rivet_fn=lambda pid: True,
            sleep_fn=lambda s: None)
        self.assertEqual(title, "Windows PowerShell",
                         "CLI 标题被终端宿主改写为 Windows PowerShell 时也应识别")
        self.assertEqual(detail, "")

    def test_skips_user_powershell_without_rivet(self):
        """fail-open 反例：用户自己开的 PowerShell（进程树不含 rivet）
        不得被当作 CLI——首轮提示词误发到无关窗口是 c67b995 场景的延续。
        弱特征标题必须进程验证通过才认。"""
        cfg = {}
        called = {"launch": False}
        wins = ["微信"]

        def fake_list():
            return list(wins)

        def fake_launch(cfg2):
            called["launch"] = True
            wins.append("npm prefix")
            return True, "ok"

        def fake_console():
            # 用户 PowerShell（9999）始终在；CLI 启动后新增 npm prefix 窗口
            base = [("Windows PowerShell", 9999)]
            return base + (["npm prefix"] if called["launch"] else [])

        title, _ = setup.resolve_cli_window(
            cfg, list_windows_fn=fake_list, launch_fn=fake_launch,
            sleep_fn=lambda s: None, console_windows_fn=fake_console,
            process_has_rivet_fn=lambda pid: False)
        self.assertTrue(called["launch"], "用户 PowerShell 不应被当作 CLI，应走第 3 级启动")
        self.assertEqual(title, "npm prefix",
                         "跳过用户 PowerShell 后应找到真正的 CLI 窗口")

    def test_skips_decoy_npm_window(self):
        # RED 复现（对抗审查反例 C1，fail-open）：浏览器/编辑器等非控制台窗口
        # 标题含 "npm prefix"（如 "npm docs - Mozilla Firefox"）且枚举先于真实 CLI——
        # 修复前第 2 级对全量窗口裸子串匹配会选中诱饵，提示词粘贴进错误窗口。
        # 修复后第 2 级只匹配控制台窗口（console_windows_fn），诱饵天然排除。
        cfg = {}
        decoy_first = ["npm docs - Mozilla Firefox", "微信", "npm prefix"]  # 诱饵先于真实 CLI
        title, detail = setup.resolve_cli_window(
            cfg,
            list_windows_fn=lambda: list(decoy_first),
            console_windows_fn=lambda: ["npm prefix"],  # 真实 CLI 是控制台窗口
            sleep_fn=lambda s: None)
        self.assertEqual(title, "npm prefix",
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
            wins.append("npm prefix")  # 真实 CLI 启动后新增窗口
            return True, "ok"

        def fake_console():
            # 启动前系统无控制台 CLI（诱饵是浏览器，非控制台类）；
            # 启动后真实 CLI（npm）进入控制台枚举
            return ["npm prefix"] if called["launch"] else []

        title, _ = setup.resolve_cli_window(
            cfg, list_windows_fn=fake_list,
            console_windows_fn=fake_console,
            launch_fn=fake_launch, sleep_fn=lambda s: None)
        self.assertTrue(called["launch"], "无控制台 CLI 时应启动而非选诱饵窗口")
        self.assertEqual(title, "npm prefix", "应把提示词发给启动后新增的 CLI 窗口")

    def test_stage3_duplicate_title_fallback(self):
        # RED 复现（第 3 级兜底 dead code）：新 CLI 窗口标题与既有窗口重复
        # （同为 "npm prefix"）时，旧实现用 set 差集 + set 长度判断——标题重复则差集为空、
        # 长度不变，len(wins) > len(before_wins) 恒 False，兜底永不触发，最终报
        # 「窗口未出现」。修复后按列表保留重复标题、比较窗口总数并选 CLI 特征窗口。
        cfg = {}
        wins = ["微信", "npm prefix"]  # 启动前已有一个 "npm prefix" 窗口

        def fake_list():
            return list(wins)

        def fake_launch(cfg2):
            wins.append("npm prefix")  # 新 CLI 窗口标题与既有 "npm prefix" 重复
            return True, "ok"

        def fake_console():
            # 启动前控制台里没有 CLI（既有 "npm prefix" 是桌面端/非控制台窗口——
            # 第 2 级不会命中它）；启动后真实 CLI（npm）进入控制台枚举。
            # 新实现第 3 级按 CLI 特征窗口名定位，不依赖"新增窗口差集"——
            # 标题重复时差集为空会漏判，按名称定位天然免疫。
            return ["npm prefix"] if len(wins) > 2 else []

        title, detail = setup.resolve_cli_window(
            cfg, list_windows_fn=fake_list,
            console_windows_fn=fake_console,
            launch_fn=fake_launch, sleep_fn=lambda s: None)
        self.assertEqual(title, "npm prefix",
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
            wins.append("npm prefix")
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
            ok = setup.send_prompt_to_tianshu("首轮提示词", "npm prefix")
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
        self.assertEqual(calls, [("send", "npm prefix", "/yes", 2), ("close", "npm prefix")],
                         "必须先发 /yes 再关闭窗口；天枢 agent 首轮对话需两次回车（enter_times=2）")

    def test_close_window_by_title_sets_64bit_argtypes(self):
        """RED 复现：user32 调用未声明 argtypes 时，64 位 HWND 按 c_int
        传参截断 → PostMessageW/taskkill 全部无效 → /yes 发完 CLI 窗口
        一直不关（真机实测）。修复后必须显式声明 64 位句柄签名。"""
        setup.close_window_by_title("__no_such_window__", sleep_fn=lambda s: None)
        import ctypes
        from ctypes import wintypes
        pt = ctypes.windll.user32.PostMessageW.argtypes
        self.assertIsNotNone(pt, "PostMessageW 必须声明 argtypes（64 位句柄防截断）")
        self.assertEqual(pt[0], wintypes.HWND, "hwnd 参数必须是 64 位 HWND")
        gt = ctypes.windll.user32.GetWindowThreadProcessId.argtypes
        self.assertIsNotNone(gt, "GetWindowThreadProcessId 必须声明 argtypes")
        self.assertEqual(gt[0], wintypes.HWND)

    def test_close_window_by_title_posts_wm_close_to_matching(self):
        from unittest import mock
        import ctypes
        posted = []
        killed = []

        class FakeUser32:
            _titles = {1: "npm prefix", 2: "微信"}
            _pids = {1: 1234, 2: 5678}

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

            def GetWindowThreadProcessId(self, hwnd, pid_out):
                # pid_out 是 ctypes.byref 包装——cast 成 DWORD 指针写值
                try:
                    ptr = ctypes.cast(pid_out, ctypes.POINTER(ctypes.wintypes.DWORD))
                    ptr.contents.value = self._pids.get(hwnd, 0)
                except Exception:
                    pass
                return 0

        def fake_taskkill(args, **kw):
            killed.append(args)
            return mock.Mock(returncode=0)

        with mock.patch.object(ctypes.windll, "user32", FakeUser32()), \
             mock.patch("subprocess.run", side_effect=fake_taskkill):
            ok = setup.close_window_by_title("npm prefix", sleep_fn=lambda s: None)
        self.assertTrue(ok)
        self.assertEqual(posted, [(1, 0x0010)],
                         "只向标题匹配的窗口发 WM_CLOSE（等效用户点 X），不匹配的窗口不动")
        self.assertEqual(killed, [["taskkill", "/F", "/PID", "1234"]],
                         "WM_CLOSE 后兜底按匹配窗口的进程 PID 强杀（比 WINDOWTITLE 过滤可靠）")

    def test_close_window_after_title_changed_to_powershell(self):
        """RED 复现（用户实测）：/yes 发送后 CLI 窗口标题可能从「npm prefix」
        变成「Windows PowerShell」——旧 close_window_by_title 只按发送前的
        标题枚举窗口，找不到 → 窗口关不掉。
        修复：标题匹配失败时回退「进程树含 rivet 的控制台窗口」——
        无论标题变成什么，只要 CLI 进程树在就能定位并关闭。"""
        from unittest import mock
        import ctypes
        posted = []
        killed = []

        class FakeUser32:
            _titles = {1: "Windows PowerShell", 2: "微信"}  # 标题已变化
            _pids = {1: 1234, 2: 5678}
            _classes = {1: "ConsoleWindowClass", 2: "WeChatMainWndForPC"}

            def GetWindowTextW(self, hwnd, buf, n):
                t = self._titles.get(hwnd, "")
                buf.value = t[:n]
                return len(t)

            def GetClassNameW(self, hwnd, buf, n):
                c = self._classes.get(hwnd, "")
                buf.value = c[:n]
                return len(c)

            def EnumWindows(self, proc, _lp):
                proc(1, 0)
                proc(2, 0)
                return True

            def PostMessageW(self, hwnd, msg, _w, _l):
                posted.append((hwnd, msg))
                return True

            def GetWindowThreadProcessId(self, hwnd, pid_out):
                try:
                    ptr = ctypes.cast(pid_out, ctypes.POINTER(ctypes.wintypes.DWORD))
                    ptr.contents.value = self._pids.get(hwnd, 0)
                except Exception:
                    pass
                return 0

        def fake_taskkill(args, **kw):
            killed.append(args)
            return mock.Mock(returncode=0)

        with mock.patch.object(ctypes.windll, "user32", FakeUser32()), \
             mock.patch("subprocess.run", side_effect=fake_taskkill), \
             mock.patch("xiaoli_app.setup._process_has_rivet", return_value=True):
            ok = setup.close_window_by_title("npm prefix", sleep_fn=lambda s: None)
        self.assertTrue(ok, "标题从 npm prefix 变成 Windows PowerShell 后仍应能关闭 CLI 窗口")
        self.assertEqual(posted, [(1, 0x0010)],
                         "标题不匹配时按进程树（含 rivet）定位到 CLI 窗口并 WM_CLOSE")
        self.assertEqual(killed, [["taskkill", "/F", "/PID", "1234"]])

    def test_close_window_title_mismatch_without_rivet_returns_false(self):
        """fail-open 反例：标题不匹配且进程树无 rivet（用户自己的 PowerShell）
        ——不得误关，返回 False。"""
        from unittest import mock
        import ctypes

        class FakeUser32:
            _titles = {1: "Windows PowerShell", 2: "微信"}
            _pids = {1: 9999, 2: 5678}

            def GetWindowTextW(self, hwnd, buf, n):
                t = self._titles.get(hwnd, "")
                buf.value = t[:n]
                return len(t)

            def EnumWindows(self, proc, _lp):
                proc(1, 0)
                proc(2, 0)
                return True

            def GetWindowThreadProcessId(self, hwnd, pid_out):
                try:
                    ptr = ctypes.cast(pid_out, ctypes.POINTER(ctypes.wintypes.DWORD))
                    ptr.contents.value = self._pids.get(hwnd, 0)
                except Exception:
                    pass
                return 0

        with mock.patch.object(ctypes.windll, "user32", FakeUser32()), \
             mock.patch("xiaoli_app.setup._process_has_rivet", return_value=False):
            ok = setup.close_window_by_title("npm prefix", sleep_fn=lambda s: None)
        self.assertFalse(ok, "进程树无 rivet 的窗口不得被误关")

    def test_close_falls_back_to_last_launch_pid_tree_kill(self):
        """RED 复现（用户实测 /yes 后窗口没关）：标题从 npm prefix 变成
        Windows PowerShell → 标题匹配失败；弱特征进程树验证也失败（窗口
        进程 conhost 与 CLI 无父子关系）→ close 返回 False、窗口关不掉。
        修复：回退链最后一环用 launch 时记录的 cmd PID（_last_launch_pid）
        taskkill /F /T 杀进程树——cmd→node 全杀，窗口必关。"""
        from unittest import mock
        import ctypes
        killed = []
        setup._last_launch_pid = 4321  # launch_tianshu 记录的 CLI cmd PID

        class FakeUser32:
            _titles = {1: "Windows PowerShell", 2: "微信"}  # 标题已变化
            _pids = {1: 9999, 2: 5678}

            def GetWindowTextW(self, hwnd, buf, n):
                t = self._titles.get(hwnd, "")
                buf.value = t[:n]
                return len(t)

            def EnumWindows(self, proc, _lp):
                proc(1, 0)
                proc(2, 0)
                return True

            def GetWindowThreadProcessId(self, hwnd, pid_out):
                try:
                    ptr = ctypes.cast(pid_out, ctypes.POINTER(ctypes.wintypes.DWORD))
                    ptr.contents.value = self._pids.get(hwnd, 0)
                except Exception:
                    pass
                return 0

        def fake_taskkill(args, **kw):
            killed.append(args)
            return mock.Mock(returncode=0)

        with mock.patch.object(ctypes.windll, "user32", FakeUser32()), \
             mock.patch("subprocess.run", side_effect=fake_taskkill), \
             mock.patch("xiaoli_app.setup._process_has_rivet", return_value=False):
            ok = setup.close_window_by_title("npm prefix", sleep_fn=lambda s: None)
        self.assertTrue(ok, "标题/弱特征都失败时应用 launch PID 进程树兜底关闭")
        self.assertIn(["taskkill", "/F", "/T", "/PID", "4321"], killed,
                      "兜底必须 taskkill /T 杀进程树（cmd→node 全杀）")
        setup._last_launch_pid = None

    def test_find_npm_prefix_window_skips_npm_subcommand_windows(self):
        """RED 复现：用户手动开的 npm 子命令窗口（「npm root」等）标题含
        "npm" 但**不是**天枢 CLI——监控必须只认「npm prefix」/rivet 特征，
        否则 /yes 误发到无关窗口（真机日志 14:24:44 向「npm root」发送）。"""
        self.assertIsNone(
            setup._find_npm_prefix_window(
                console_windows_fn=lambda: ["npm root", "微信"]),
            "「npm root」是 npm 子命令窗口，不是天枢 CLI，不得命中")
        self.assertEqual(
            setup._find_npm_prefix_window(
                console_windows_fn=lambda: ["npm prefix", "微信"]),
            "npm prefix")
        self.assertIsNone(
            setup._find_npm_prefix_window(
                console_windows_fn=lambda: ["npm docs - Mozilla Firefox"]),
            "浏览器等含 npm 的非 CLI 窗口不得命中")

    def test_run_first_run_guide_checks_window_after_confirm(self):
        """RED 复现：自动监控 npm prefix 触发 /yes 是错误设计（用户实测）。
        正确流程：弹窗指导 → 用户操作完点「确认完成」→ 确认后才检查 npm
        prefix（配置完成窗口标题才变）→ 有则发 /yes → 关窗。
        取消（即使窗口已出现）→ 不检查不发送不标记。
        """
        cfg = {}
        wins = ["微信"]  # 配置中：窗口标题还不是 npm prefix
        flow = []

        def fake_detect():
            return "rivet"

        def fake_launch(c):
            flow.append("launch")
            return True, "ok"

        def fake_console():
            return list(wins)

        def fake_dialog(title, text, buttons=None):
            flow.append("dialog")
            # 弹窗期间用户完成配置（窗口变 npm prefix），然后点确认
            wins.append("npm prefix")
            return "确认完成"

        sent = []

        def fake_send(t, cmd, hold=0.5, enter_times=1):
            sent.append(cmd)
            return True

        ok = setup.run_first_run_guide(
            cfg, detect_fn=fake_detect, launch_fn=fake_launch,
            console_windows_fn=fake_console, sleep_fn=lambda s: None,
            send_fn=fake_send, close_fn=lambda t: True,
            dialog_fn=fake_dialog)
        self.assertTrue(ok)
        self.assertEqual(sent, ["/yes"], "确认完成后检测到 npm prefix 必须发送 /yes")
        self.assertTrue(cfg.get("tianshu_guided"))
        # 顺序：先打开 CLI → 弹窗（用户确认）→ 再 /yes
        self.assertEqual(flow, ["launch", "dialog"])

    def test_run_first_run_guide_cancel_skips_even_if_window_appeared(self):
        """用户点「取消」（即使 CLI 窗口已出现）→ 不得发 /yes、不得标记。
        监控自动触发版在此场景会误发（窗口出现即触发）——RED 锚点。"""
        cfg = {}
        wins = ["npm prefix"]  # 窗口已出现（用户配完）

        def fake_launch(c):
            return True, "ok"

        def fake_console():
            return list(wins)

        def fake_dialog(title, text, buttons=None):
            return "取消"

        sent = []

        def fake_send(t, cmd, hold=0.5, enter_times=1):
            sent.append(cmd)
            return True

        ok = setup.run_first_run_guide(
            cfg, detect_fn=lambda: "rivet", launch_fn=fake_launch,
            console_windows_fn=fake_console, sleep_fn=lambda s: None,
            send_fn=fake_send, close_fn=lambda t: True,
            dialog_fn=fake_dialog)
        self.assertFalse(ok)
        self.assertEqual(sent, [], "用户取消不得发送 /yes")
        self.assertFalse(cfg.get("tianshu_guided", False))

    def test_run_first_run_guide_confirm_but_no_window_reports(self):
        """用户点确认但未检测到 npm prefix（没配完/窗口未变）→ 提示、不标记。"""
        shown = []
        cfg = {}

        def fake_console():
            return ["微信"]  # 无 npm prefix

        def fake_dialog(title, text, buttons=None):
            shown.append(title)
            return "确认完成"

        ok = setup.run_first_run_guide(
            cfg, detect_fn=lambda: "rivet", launch_fn=lambda c: (True, "ok"),
            console_windows_fn=fake_console, sleep_fn=lambda s: None,
            send_fn=lambda *a, **k: True, close_fn=lambda t: True,
            dialog_fn=fake_dialog)
        self.assertFalse(ok)
        self.assertFalse(cfg.get("tianshu_guided", False))
        self.assertIn("未检测到", " ".join(shown),
                      "确认后未检测到 CLI 窗口必须弹提示（可重试）")

    def test_run_first_run_guide_marks_guided_on_confirm(self):
        import tempfile
        import os
        tmp = tempfile.mkdtemp(prefix="guide_")
        cfg_path = os.path.join(tmp, "config.json")
        try:
            cfg = {"tasks_dir": r"D:\tasks"}
            flow = []
            wins = ["微信"]

            def fake_detect():
                return r"C:\Users\me\AppData\Roaming\npm\rivet"

            def fake_launch(c):
                flow.append(("launch", c))
                return True, "ok"

            def fake_console():
                return list(wins)

            def fake_send(title, command, hold=0.5, enter_times=1):
                flow.append(("send", command))
                return True

            def fake_close(title):
                flow.append(("close",))
                return True

            def fake_dialog(title, text, buttons=None):
                flow.append(("dialog", title))
                # 弹窗期间：用户完成配置，窗口标题变 npm prefix，然后点确认
                wins.append("npm prefix")
                return "确认完成"

            ok = setup.run_first_run_guide(
                cfg, cfg_path=cfg_path, detect_fn=fake_detect,
                launch_fn=fake_launch, console_windows_fn=fake_console,
                send_fn=fake_send, close_fn=fake_close,
                sleep_fn=lambda s: None, dialog_fn=fake_dialog)
            self.assertTrue(ok)
            self.assertTrue(cfg.get("tianshu_guided"),
                            "引导完成后必须标记 tianshu_guided（此后初始化不再切 YOLO）")
            self.assertIn(("send", "/yes"), flow, "监控到 npm prefix 后必须发送 /yes")
            self.assertIn(("close",), flow, "发送 /yes 后必须关闭 CLI 窗口")
            # 顺序：先打开 CLI，再弹窗，再 /yes，再关闭
            self.assertEqual(flow[0], ("launch", cfg))
            self.assertEqual(flow[1], ("dialog", "天枢 CLI 配置引导"))
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
        # 「稍后再说」已改为自动监控：弹窗期间始终未出现 npm prefix（取消/超时）
        cfg = {}
        wins = ["微信"]

        def fake_launch(c):
            return True, "ok"

        def fake_console():
            return list(wins)

        def fake_dialog(title, text, buttons=None):
            return ""  # 用户取消，窗口从未出现

        ok = setup.run_first_run_guide(
            cfg, detect_fn=lambda: "rivet", launch_fn=fake_launch,
            console_windows_fn=fake_console,
            sleep_fn=lambda s: None, dialog_fn=fake_dialog)
        self.assertFalse(ok)
        self.assertFalse(cfg.get("tianshu_guided", False),
                         "未监控到 npm prefix 不得标记 guided（设置页可重跑引导）")

    def test_run_first_run_guide_cancel_without_window_does_not_mark(self):
        """弹窗期间始终未出现 npm prefix（用户取消/超时）→ 不标记 guided。"""
        cfg = {}
        wins = ["微信"]

        def fake_detect():
            return "rivet"

        def fake_launch(c):
            return True, "ok"

        def fake_console():
            return list(wins)

        def fake_dialog(title, text, buttons=None):
            return ""  # 用户点了取消，窗口从未出现

        ok = setup.run_first_run_guide(
            cfg, detect_fn=fake_detect, launch_fn=fake_launch,
            console_windows_fn=fake_console, sleep_fn=lambda s: None,
            send_fn=lambda *a, **k: True, close_fn=lambda t: True,
            dialog_fn=fake_dialog)
        self.assertFalse(ok)
        self.assertFalse(cfg.get("tianshu_guided", False),
                         "未监控到 npm prefix 不得标记 guided")

    def test_run_first_run_guide_installs_cli_when_missing(self):
        cfg = {}
        flow = []
        wins = ["微信"]

        def fake_detect():
            return ""  # 未安装 rivet

        def fake_install():
            flow.append("install")
            return True

        def fake_launch(c):
            flow.append("launch")
            return True, "ok"

        def fake_console():
            return list(wins)

        def fake_dialog(title, text, buttons=None):
            flow.append("dialog")
            wins.append("npm prefix")  # 弹窗期间用户配置完成
            return "确认完成"

        ok = setup.run_first_run_guide(
            cfg, detect_fn=fake_detect, install_fn=fake_install,
            launch_fn=fake_launch, console_windows_fn=fake_console,
            send_fn=lambda *a, **k: True,
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
