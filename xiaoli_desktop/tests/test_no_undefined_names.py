# -*- coding: utf-8 -*-
"""拆分/搬迁回归闸：undefined name 一律拦截 + 启动链冒烟。

背景：task_bridge 从 xiaoli_bot 拆出时漏 import sys（acquire_single_instance
用 sys.platform），unittest 全绿——没有任何测试调用过它，而它是桌面端启动
必经路径，打包版一启动就 NameError 炸掉。函数级覆盖追不上搬迁节奏，用
pyflakes 静态检查把「名字未定义」类缺陷整类拦住（re-export 门面的
"imported but unused" 是有意为之，不属于本闸范围）。

冒烟两条补的是当年恰好没被任何测试走过的启动/调用路径：
- acquire_single_instance：xiaoli_web.main 与 CLI --run 的第一步
- classify_task_with_llm：任务判断链（CLASSIFY_PROMPT 的实际消费点）
"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 拆分涉及的全部顶层模块（子包 ui/ 是退役中的 Qt 遗留，不在闸内）
SCAN_TOPS = ("xiaoli_app", "wx_backend")
SCAN_ROOTS = ("xiaoli_bot.py", "wechat_bot.py", "xiaoli_web.py", "xiaoli_gui.py")


def _collect_targets():
    targets = []
    for top in SCAN_TOPS:
        d = os.path.join(ROOT, top)
        for name in sorted(os.listdir(d)):
            if name.endswith(".py"):
                targets.append(os.path.join(d, name))
    for name in SCAN_ROOTS:
        p = os.path.join(ROOT, name)
        if os.path.isfile(p):
            targets.append(p)
    return targets


class TestNoUndefinedNames(unittest.TestCase):
    def test_pyflakes_no_undefined_names(self):
        try:
            import pyflakes  # noqa: F401
        except ImportError:
            self.skipTest("pyflakes 未安装（.venv）")
        r = subprocess.run(
            [sys.executable, "-m", "pyflakes", *_collect_targets()],
            capture_output=True, text=True)
        bad = [ln for ln in (r.stdout or "").splitlines()
               if "undefined name" in ln]
        self.assertEqual(bad, [],
                         "源码树存在 undefined name（拆分漏 import/常量）：\n"
                         + "\n".join(bad[:20]))


class TestStartupPathSmoke(unittest.TestCase):
    def test_acquire_single_instance_roundtrip(self):
        from xiaoli_bot import acquire_single_instance, release_single_instance
        name = "XiaoLi_Test_Mutex_NoUndefinedNames"
        self.assertTrue(acquire_single_instance(name))
        try:
            from xiaoli_bot import acquire_single_instance as again
            self.assertFalse(again(name), "同进程二次获取必须被拒（互斥体生效）")
        finally:
            release_single_instance()
        self.assertTrue(acquire_single_instance(name), "释放后可再次获取")
        release_single_instance()

    def test_classify_task_with_llm_prompt_reachable(self):
        """CLASSIFY_PROMPT 的实际消费点可调用（mock HTTP，不联网）。
        拆分事故回归：CLASSIFY_PROMPT 留在 xiaoli_bot、消费函数搬到
        task_bridge，调用即 NameError——unittest 全绿（无人调用过）。"""
        import requests
        from xiaoli_app.task_bridge import CLASSIFY_PROMPT as tb_prompt
        from xiaoli_bot import CLASSIFY_PROMPT as xb_prompt
        self.assertEqual(tb_prompt, xb_prompt)
        self.assertTrue(tb_prompt.strip())

        class FakeResp:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content":
                        '{"is_task": true, "task": "做PPT"}'}}]}

        from xiaoli_app import task_bridge
        with mock.patch.object(requests, "post", return_value=FakeResp()), \
                tempfile.TemporaryDirectory() as tmp:
            # classify 走 requests（模块内 import requests as req 是同一模块对象）
            r = task_bridge.classify_task_with_llm("http://x", "k", "m", "帮我做个PPT")
        self.assertEqual(r, {"is_task": True, "task": "做PPT"})


if __name__ == "__main__":
    unittest.main()
