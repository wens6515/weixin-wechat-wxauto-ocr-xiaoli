# -*- coding: utf-8 -*-
"""天枢一键安装改走 CLI：npm install -g tianshu-tui + 失败原因透出到界面。

钉住的定案：

- 旧实现下载 GitHub 源码 zip 解压到 ~/Tianshu——包里没有可执行文件，
  检测（rivet 命令 / tianshu-desktop.exe）必然判「未安装」，且失败原因被
  丢弃，用户看到「安装完成 + 卡片未安装」（真机反馈）；
- 现走 CLI 安装：成败判据与检测同一条命令（rivet 由 npm 全局 bin 提供），
  npm 缺失/非零退出/超时都把原因带回（前端 install 事件的 error 字段）；
- 安装成功后不再写 `tianshu_install_dir`（CLI 安装没有桌面端目录），
  卡片状态由 check_environment 的 rivet 判据自然转「CLI(rivet) ✓」；
- 桌面端（Electron）检测已整体移除：任务桥全链路走 CLI，卡片只认 rivet，
  不再显示「桌面端：...	ianshu-desktop.exe」这类路径（用户实机反馈）。
"""
import os
import subprocess
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app import setup
from xiaoli_app.webbridge import BridgeApi


class TestInstallTianshuCli(unittest.TestCase):
    def test_npm_missing_reports_guidance(self):
        with mock.patch.object(setup.shutil, "which", return_value=None):
            ok, detail = setup.install_tianshu_cli()
        self.assertFalse(ok)
        self.assertIn("Node.js", detail)

    def test_success_returns_ok_and_detail(self):
        r = mock.Mock(returncode=0, stdout=b"added 1 package", stderr=b"")
        with mock.patch.object(setup.shutil, "which",
                               return_value=r"C:\npm\npm.cmd"), \
                mock.patch.object(setup.subprocess, "run", return_value=r) as run:
            ok, detail = setup.install_tianshu_cli()
        self.assertTrue(ok)
        self.assertIn("tianshu-tui", detail)
        argv = run.call_args[0][0]
        self.assertEqual(argv[1:], ["install", "-g", "tianshu-tui"])

    def test_failure_surfaces_npm_stderr_tail(self):
        """npm 非零退出：原因（stderr 尾部）必须带回界面，不再静默。"""
        r = mock.Mock(returncode=1, stdout=b"",
                      stderr="npm ERR! 网络超时 ETIMEDOUT".encode("gbk"))
        with mock.patch.object(setup.shutil, "which",
                               return_value=r"C:\npm\npm.cmd"), \
                mock.patch.object(setup.subprocess, "run", return_value=r):
            ok, detail = setup.install_tianshu_cli()
        self.assertFalse(ok)
        self.assertIn("退出码 1", detail)
        self.assertIn("ETIMEDOUT", detail)

    def test_timeout_reports_timeout(self):
        with mock.patch.object(setup.shutil, "which",
                               return_value=r"C:\npm\npm.cmd"), \
                mock.patch.object(setup.subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired("npm", 600)):
            ok, detail = setup.install_tianshu_cli()
        self.assertFalse(ok)
        self.assertIn("超时", detail)

    def test_oserror_reports(self):
        with mock.patch.object(setup.shutil, "which",
                               return_value=r"C:\npm\npm.cmd"), \
                mock.patch.object(setup.subprocess, "run",
                                  side_effect=OSError("拒绝访问")):
            ok, detail = setup.install_tianshu_cli()
        self.assertFalse(ok)
        self.assertIn("启动失败", detail)


class _Engine:
    bot = None


class _Ctx:
    def __init__(self, tmp):
        self.cfg_path = os.path.join(tmp, "config.json")
        self.cards_dir = os.path.join(tmp, "cards")
        self.cfg = {}
        self.engine = _Engine()


class TestInstallBridgeWiring(unittest.TestCase):
    """BridgeApi.install_tianshu：后台线程 + install 事件（含 error 透出）。"""

    def _bridge(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        b = BridgeApi(_Ctx(tmp))
        events = []
        b.push = lambda evt, payload: events.append((evt, payload))
        return b, events

    def _run_worker(self, bridge):
        """install_tianshu 起线程；等线程结束（join 通过事件计数判断）。"""
        r = bridge.install_tianshu()
        self.assertTrue(r["ok"])
        for t in threading.enumerate():
            if t.name == "xiaoli-install":
                t.join(timeout=5)

    def test_success_pushes_done_ok(self):
        bridge, events = self._bridge()
        with mock.patch.object(setup, "install_tianshu_cli",
                               return_value=(True, "安装完成")):
            self._run_worker(bridge)
        self.assertEqual(events[0][0], "install")
        self.assertIn("npm", events[0][1].get("text", ""))   # 阶段文案
        self.assertTrue(events[-1][1]["done"])
        self.assertTrue(events[-1][1]["ok"])

    def test_failure_pushes_error_detail(self):
        bridge, events = self._bridge()
        with mock.patch.object(setup, "install_tianshu_cli",
                               return_value=(False, "npm 安装失败（退出码 1）：ETIMEDOUT")):
            self._run_worker(bridge)
        last = events[-1][1]
        self.assertTrue(last["done"])
        self.assertFalse(last["ok"])
        self.assertIn("ETIMEDOUT", last["error"])
        self.assertFalse(bridge._installing)   # 标记复位，可重试

    def test_concurrent_install_rejected(self):
        bridge, _ = self._bridge()
        bridge._installing = True
        r = bridge.install_tianshu()
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
