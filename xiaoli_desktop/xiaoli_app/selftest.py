# -*- coding: utf-8 -*-
"""xiaoli_bot 自检（--test）：不连微信、不唤起窗口，覆盖任务桥 / 剪贴板 /
文件定位 / 记忆回写等核心逻辑。

从 xiaoli_bot.py 拆出（纯移动，零逻辑改动）；入口在 xiaoli_bot.__main__
内懒加载——本模块反向 import xiaoli_bot，模块级互导会成环。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time

import requests as req

from xiaoli_bot import (
    AgentBot, TASK_DEFAULTS, _parse_classify_json, acquire_single_instance,
    classify_task_with_llm, dispatch_task, generate_task_id,
    has_active_tasks, list_windows, load_merged_config, poll_outbox,
    read_clipboard_files, release_single_instance, resolve_result_file,
    set_clipboard_files, should_resume_listen,
)

# =====================================================================
# 自检（不连微信、不唤起窗口）
# =====================================================================

def run_self_test():
    print("=" * 60)
    print("小漓合并版自检（不连微信）")
    print("=" * 60)
    passed = 0
    failed = 0

    def check(name, cond, detail=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name} {detail}")

    tmp = tempfile.mkdtemp(prefix="xiaoli_selftest_")
    try:
        # ---- T1: load_merged_config ----
        test_cfg = os.path.join(tmp, "config.json")
        with open(test_cfg, "w", encoding="utf-8") as f:
            json.dump({"bot_nickname": "x"}, f)
        cfg = load_merged_config(test_cfg)
        from xiaoli_app import config_store as _cs
        check("T1 旧 config 补齐 task_* 默认", all(k in cfg for k in TASK_DEFAULTS), str(cfg))
        check("T1 默认 tasks_dir 便携默认（与 GUI 同一事实源）",
              cfg["tasks_dir"] == _cs.default_tasks_dir(), str(cfg.get("tasks_dir")))
        check("T1 默认窗口标题为空", cfg["tianshu_window_title"] == "", str(cfg.get("tianshu_window_title")))
        check("T1 迁移出 providers + 活跃卡", bool(cfg.get("providers")) and bool(cfg.get("active_card_id")),
              str({k: cfg.get(k) for k in ("providers", "active_card_id")}))

        # ---- T2: task_id ----
        tid = generate_task_id()
        check("T2 id 格式", re.fullmatch(r"\d{14}[0-9a-f]{4}", tid) is not None, tid)

        # ---- T3: _parse_classify_json ----
        r1 = _parse_classify_json('{"is_task": true, "task": "做个网站"}')
        check("T3 正常 JSON", r1 == {"is_task": True, "task": "做个网站"}, str(r1))
        r2 = _parse_classify_json('```json\n{"is_task": false}\n```')
        check("T3 markdown 围栏剥离", r2 == {"is_task": False, "task": ""}, str(r2))
        r3 = _parse_classify_json("完全不是 JSON")
        check("T3 非 JSON 兜底", r3 == {"is_task": False, "task": ""}, str(r3))
        r4 = _parse_classify_json('{"is_task": true}')
        check("T3 缺 task 字段兜底", r4 == {"is_task": True, "task": ""}, str(r4))
        r5 = _parse_classify_json("")
        check("T3 空输入兜底", r5 == {"is_task": False, "task": ""}, str(r5))
        r6 = _parse_classify_json('garbage "is_task": true garbage "task": "x" garbage')
        check("T3 畸形 JSON 正则兜底", r6 == {"is_task": True, "task": "x"}, str(r6))

        # ---- T3b: classify_task_with_llm（mock API） ----
        class FakeResp:
            status_code = 200

            def json(self):
                return {"choices": [{"message": {"content": '{"is_task": true, "task": "做PPT"}'}}]}

        original_post = req.post
        req.post = lambda *a, **k: FakeResp()
        r7 = classify_task_with_llm("http://x", "k", "m", "帮我做个PPT")
        req.post = original_post
        check("T3 LLM mock 判断", r7 == {"is_task": True, "task": "做PPT"}, str(r7))
        req.post = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        r8 = classify_task_with_llm("http://x", "k", "m", "hi")
        req.post = original_post
        check("T3 API 异常兜底", r8 == {"is_task": False, "task": ""}, str(r8))

        # ---- T4: dispatch_task ----
        att = os.path.join(tmp, "att.docx")
        with open(att, "w", encoding="utf-8") as f:
            f.write("document content")
        tasks_dir = os.path.join(tmp, "tasks")
        tid2 = dispatch_task(tasks_dir, {"msg_id": "m1", "chat_name": "小明", "sender": "王", "task": "根据文档做网站"}, [att])
        task_dir = os.path.join(tasks_dir, tid2)
        check("T4 任务目录存在", os.path.isdir(task_dir))
        with open(os.path.join(task_dir, "task.json"), "r", encoding="utf-8") as f:
            info = json.load(f)
        check("T4 task.json 字段", info["msg_id"] == "m1" and info["task_id"] == tid2, str(info))
        check("T4 附件已复制", os.path.isfile(os.path.join(task_dir, "attachments", "att.docx")))
        check("T4 attachments 清单", info["attachments"] == ["att.docx"], str(info.get("attachments")))

        # ---- T5: list_windows（真实枚举，不激活） ----
        wins = list_windows()
        check("T5 列出窗口", len(wins) > 0 and all(isinstance(n, str) and n for n, _ in wins), f"{len(wins)} 个窗口")

        # ---- T7: poll_outbox ----
        with open(os.path.join(task_dir, "result.json"), "w", encoding="utf-8") as f:
            json.dump({"status": "success", "reply_text": "做好了", "files": ["att.docx"]}, f)
        delivered = []

        def deliver(td, ti, res):
            delivered.append((ti.get("chat_name"), res.get("reply_text"), td))

        handled = poll_outbox(tasks_dir, deliver)
        check("T7 回传解析并归档", handled == [tid2], str(handled))
        check("T7 deliver 收到正确数据", delivered and delivered[0][0] == "小明" and delivered[0][1] == "做好了", str(delivered))
        check("T7 归档到 sent", os.path.isdir(os.path.join(tasks_dir, "sent", tid2)))

        # T7b: failed 也回传
        tid3 = dispatch_task(tasks_dir, {"msg_id": "m2", "chat_name": "测试群", "task": "x"}, None)
        with open(os.path.join(tasks_dir, tid3, "result.json"), "w", encoding="utf-8") as f:
            json.dump({"status": "failed", "reply_text": "失败了"}, f)
        delivered2 = []
        poll_outbox(tasks_dir, lambda td, ti, res: delivered2.append((res.get("status"), res.get("reply_text"))))
        check("T7 failed 状态也回传", delivered2 and delivered2[0][0] == "failed", str(delivered2))

        # ---- T8: has_active_tasks（消息监听暂停/恢复的判据） ----
        check("T8 全部归档后无活跃任务", has_active_tasks(tasks_dir) is False, str(os.listdir(tasks_dir)))
        # 处理中（无 result.json）
        tid4 = dispatch_task(tasks_dir, {"msg_id": "m4", "chat_name": "测试", "task": "y"}, None)
        check("T8 处理中任务判定为活跃", has_active_tasks(tasks_dir) is True, str(os.listdir(tasks_dir)))
        # 已完成待回传（有 result.json 未归档）
        with open(os.path.join(tasks_dir, tid4, "result.json"), "w", encoding="utf-8") as f:
            json.dump({"status": "success", "reply_text": "ok"}, f)
        check("T8 待回传任务判定为活跃", has_active_tasks(tasks_dir) is True, str(os.listdir(tasks_dir)))
        # 归档进 sent 后恢复非活跃
        shutil.move(os.path.join(tasks_dir, tid4), os.path.join(tasks_dir, "sent", tid4))
        check("T8 归档后无活跃任务", has_active_tasks(tasks_dir) is False, str(os.listdir(tasks_dir)))
        # 非任务目录（如 attachments，无 task.json）不误判为活跃
        os.makedirs(os.path.join(tasks_dir, "attachments"), exist_ok=True)
        check("T8 非任务目录不误判为活跃", has_active_tasks(tasks_dir) is False, str(os.listdir(tasks_dir)))
        check("T8 空/不存在目录为无活跃", has_active_tasks(os.path.join(tmp, "nope")) is False)

        # ---- T9: should_resume_listen（任务完成后延迟 hold 秒恢复监听） ----
        now0 = 1000.0
        r, w, e = should_resume_listen(False, False, None, now0, 10)
        check("T9 无任务从未暂停 → 直接监听", r is True and w is False and e is None, str((r, w, e)))
        r, w, e = should_resume_listen(True, False, None, now0, 10)
        check("T9 任务进行中 → 暂停并记录", r is False and w is True and e is None, str((r, w, e)))
        r, w, e = should_resume_listen(False, True, None, now0, 10)
        check("T9 任务刚完成 → 缓冲开始", r is False and w is True and e == now0, str((r, w, e)))
        r, w, e2 = should_resume_listen(False, True, e, now0 + 5, 10)
        check("T9 缓冲 5s < 10s → 仍暂停", r is False and w is True and e2 == now0, str((r, w, e2)))
        r, w, e3 = should_resume_listen(False, True, e, now0 + 10, 10)
        check("T9 缓冲满 10s → 恢复并复位", r is True and w is False and e3 is None, str((r, w, e3)))
        r, w, e4 = should_resume_listen(False, True, None, now0, 0)
        check("T9 hold=0 立即恢复", r is True and w is False and e4 is None, str((r, w, e4)))

        # ---- T10: 单实例互斥体（防双开） ----
        if sys.platform == "win32":
            m1 = acquire_single_instance("XiaoLiBot_SelfTest_Mutex")
            check("T10 首次获取互斥体成功", m1 is True, str(m1))
            m2 = acquire_single_instance("XiaoLiBot_SelfTest_Mutex")
            check("T10 二次获取被拒（已有实例）", m2 is False, str(m2))
            release_single_instance()
            m3 = acquire_single_instance("XiaoLiBot_SelfTest_Mutex")
            check("T10 释放后可再次获取", m3 is True, str(m3))
            release_single_instance()
        else:
            check("T10 非 Windows 跳过", True, "")

        # ---- T11: 成果文件 sent 兜底定位 + 并发归档竞态 ----
        tid5 = dispatch_task(tasks_dir, {"msg_id": "m5", "chat_name": "测试", "task": "z"}, None)
        t5_dir = os.path.join(tasks_dir, tid5)
        with open(os.path.join(t5_dir, "result.json"), "w", encoding="utf-8") as f:
            json.dump({"status": "success", "reply_text": "ok", "files": ["out.docx"]}, f)
        with open(os.path.join(t5_dir, "out.docx"), "w", encoding="utf-8") as f:
            f.write("x")
        p1 = resolve_result_file(t5_dir, os.path.join(tasks_dir, "sent"), "out.docx")
        check("T11 文件在任务目录命中", p1 == os.path.join(t5_dir, "out.docx"), p1)
        # 模拟并发：任务被归档进 sent，文件随之移动 → 从 sent 兜底命中
        shutil.move(t5_dir, os.path.join(tasks_dir, "sent", tid5))
        p2 = resolve_result_file(t5_dir, os.path.join(tasks_dir, "sent"), "out.docx")
        check("T11 文件在 sent 兜底命中", p2 == os.path.join(tasks_dir, "sent", tid5, "out.docx"), p2)
        check("T11 都不存在返回原路径",
              resolve_result_file(t5_dir, os.path.join(tasks_dir, "sent"), "nope.docx") == os.path.join(t5_dir, "nope.docx"),
              str(resolve_result_file(t5_dir, os.path.join(tasks_dir, "sent"), "nope.docx")))
        # 先归档后发送：deliver 收到 sent 下的路径，发送时文件已稳定
        tid6 = dispatch_task(tasks_dir, {"msg_id": "m6", "chat_name": "测试", "task": "w"}, None)
        with open(os.path.join(tasks_dir, tid6, "out.docx"), "w", encoding="utf-8") as f:
            f.write("y")
        with open(os.path.join(tasks_dir, tid6, "result.json"), "w", encoding="utf-8") as f:
            json.dump({"status": "success", "reply_text": "ok", "files": ["out.docx"]}, f)
        captured_td = []

        def deliver_capture(td, ti, res):
            captured_td.append(td)

        handled6 = poll_outbox(tasks_dir, deliver_capture)
        check("T11 先归档后发送：deliver 收到 sent 路径",
              handled6 == [tid6] and captured_td and captured_td[0] == os.path.join(tasks_dir, "sent", tid6),
              str((handled6, captured_td)))
        check("T11 发送时文件在 sent 下存在",
              os.path.isfile(os.path.join(tasks_dir, "sent", tid6, "out.docx")),
              str(os.listdir(os.path.join(tasks_dir, "sent", tid6))))
        # T9: 剪贴板文件发送方案（CF_HDROP round-trip，不连微信）
        clip_file = os.path.join(tmp, "clip_test.txt")
        with open(clip_file, "w", encoding="utf-8") as f:
            f.write("clipboard test")
        ok_set = set_clipboard_files([clip_file])
        check("T9 设置剪贴板文件", ok_set)
        files_back = read_clipboard_files()
        check("T9 读回文件列表", clip_file in files_back, str(files_back))
        # 清空剪贴板，避免污染
        try:
            import ctypes
            user32 = ctypes.windll.user32
            if user32.OpenClipboard(0):
                user32.EmptyClipboard()
                user32.CloseClipboard()
        except Exception:
            pass

        # ---- T12: 按显示名定位（重名取 (N) 最大）：用户回传 bot 发过的文件、
        # 同名文件连续接收，都不再被「成果排除登记」误杀（该机制已整体删除）----
        def make_bot(dirpath):
            b = AgentBot.__new__(AgentBot)
            b.file_storage_path = dirpath
            b.nickname = "小漓"
            return b

        # 场景 A：bot 发过 X.html（微信写入接收目录的发送副本是干净原名），
        # 用户回传同名文件 → 下载副本带 hash 前缀且 ctime 更新 → 平局 ctime
        # 新者优先选中回传文件（旧登记方案会把回传误杀成「文件下载失败」）
        dir_a = os.path.join(tmp, "recv_a")
        os.makedirs(dir_a)
        bot_a = make_bot(dir_a)
        artifact = os.path.join(dir_a, "小漓深海小剧场.html")
        with open(artifact, "w", encoding="utf-8") as f:
            f.write("bot sent")
        old = time.time() - 3600  # 发送副本 ctime = 1 小时前
        os.utime(artifact, (old, old))
        returned = os.path.join(
            dir_a, "c4fc4da4edbb4f3b87cfbe02e564a2d3_3252231687582084619_m_小漓深海小剧场.html")
        with open(returned, "w", encoding="utf-8") as f:
            f.write("returned")  # ctime = 现在（回传下载时刻）
        got_a = bot_a._find_file_by_display_name("小漓深海小剧场.html")
        check("T12 回传 bot 发过的文件按显示名命中（不再被登记排除）",
              got_a == returned, str(got_a))

        # 场景 B：同名文件连续接收 → (N) 编号最大 = 最近下载优先（ctime 平局）
        dir_b = os.path.join(tmp, "recv_b")
        os.makedirs(dir_b)
        bot_b = make_bot(dir_b)
        first_b = os.path.join(dir_b, "h1_1_m_报告.txt")
        with open(first_b, "w", encoding="utf-8") as f:
            f.write("first")
        second_b = os.path.join(dir_b, "h2_2_m_报告(1).txt")
        with open(second_b, "w", encoding="utf-8") as f:
            f.write("second")
        got_b = bot_b._find_file_by_display_name("报告.txt")
        check("T12 同名重收取 (N) 最大（最近下载）", got_b == second_b, str(got_b))

        # 场景 C：目录里没有显示名对应的文件 → 返回 None（上层回复失败，
        # 不全目录乱选旧文件——旧行为已删）
        dir_c = os.path.join(tmp, "recv_c")
        os.makedirs(dir_c)
        bot_c = make_bot(dir_c)
        with open(os.path.join(dir_c, "无关文件.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        got_c = bot_c._find_file_by_display_name("报名表.xlsx")
        check("T12 无同名候选返回 None（不乱选）", got_c is None, str(got_c))

        # 场景 D：OCR 漏读文件名分隔符仍能命中（真机事故：磁盘名
        # 「小漓_深海小剧场.html」OCR 成「小漓深海小剧场.html」——下划线
        # 在基线上被 RapidOCR 漏掉，包含匹配被断开 → 误报「文件下载失败」）
        dir_d = os.path.join(tmp, "recv_d")
        os.makedirs(dir_d)
        bot_d = make_bot(dir_d)
        underscore_d = os.path.join(dir_d, "小漓_深海小剧场.html")
        with open(underscore_d, "w", encoding="utf-8") as f:
            f.write("x")
        got_d = bot_d._find_file_by_display_name("小漓深海小剧场.html")
        check("T12 OCR 丢下划线的显示名命中带下划线的磁盘文件",
              got_d == underscore_d, str(got_d))

        # 场景 E：同主干不同后缀——目录里同名 .7z 自己攒出 (2) 编号，跨类型
        # 压倒刚收的 .pdf（真机事故：bot 把 PDF 任务导向了同名压缩包）。
        # 后缀必须参与匹配，(N)/ctime 只在同后缀组内竞争。
        dir_f = os.path.join(tmp, "recv_f")
        os.makedirs(dir_f)
        bot_f = make_bot(dir_f)
        for name in ("纳新宣传海报.7z", "纳新宣传海报(1).7z",
                     "纳新宣传海报(2).7z", "纳新宣传海报.pdf"):
            with open(os.path.join(dir_f, name), "w") as f:
                f.write("x")
        got_f = bot_f._find_file_by_display_name("纳新宣传海报.pdf")
        check("T12 同主干异后缀选同后缀（(2).7z 不参战）",
              got_f == os.path.join(dir_f, "纳新宣传海报.pdf"), str(got_f))

        # ---- T13: 显示名提取（真实 content 格式：'文件\n<名>\n<大小>\n微信电脑版'）----
        dir_e = os.path.join(tmp, "recv_e")
        os.makedirs(dir_e)
        bot_e = make_bot(dir_e)
        user_file = os.path.join(
            dir_e,
            "c4fc4da4edbb4f3b87cfbe02e564a2d3_3252231687582084619_m_企业账户-投递人数2078758432532791296(4).txt")
        with open(user_file, "w", encoding="utf-8") as f:
            f.write("x")
        old = time.time() - 7 * 86400  # 7 天前的源时间戳（微信保留源时间戳）
        os.utime(user_file, (old, old))
        from types import SimpleNamespace
        fake_msg = SimpleNamespace(content="文件\n企业账户-投递人数2078758432532791296.txt\n123KB\n微信电脑版",
                                   repattern=r"^文件\n([^\n]+)\n(\d+(\.\d+)?)(B|KB|MB|GB|TB)\n微信电脑版$")
        display = bot_e._extract_file_display_name(fake_msg)
        check("T13 从 content 提取显示名", display == "企业账户-投递人数2078758432532791296.txt", str(display))
        got_e = bot_e._find_file_by_display_name(display)
        check("T13 按显示名定位 hash 前缀下载文件", got_e == user_file, str(got_e))

        # ---- T15: 任务结果回写对话记忆（deliver 闭包调用 _remember_task_result） ----
        def make_mem_bot(dirpath):
            b = AgentBot.__new__(AgentBot)
            b.memory_db = {}
            b.memory_file = os.path.join(dirpath, "mem.json")
            b.max_history = 1000
            b._memory_lock = threading.RLock()
            b.memory_deep_enabled = False
            b.memory_compress_enabled = False
            b.memory_keep_recent = 30
            b._deep_count = {}
            b._deep_dir = ""
            return b

        mem_dir = os.path.join(tmp, "mem")
        os.makedirs(mem_dir)
        mb = make_mem_bot(mem_dir)
        mb._remember_task_result("小明", {"status": "success", "reply_text": "网站做好了，见成果文件", "files": ["site.zip"]})
        hist = mb.memory_db.get("小明", {}).get("recent", [])
        check("T15 成功结果写入记忆",
              len(hist) == 1 and hist[0]["role"] == "assistant"
              and "[任务结果] 网站做好了，见成果文件，成果文件: site.zip" in hist[0]["content"], str(hist))
        mb._remember_task_result("小明", {"status": "failed", "reply_text": ""})
        check("T15 失败结果写入记忆（默认文案）",
              len(mb.memory_db["小明"]["recent"]) == 2
              and "任务处理失败了" in mb.memory_db["小明"]["recent"][1]["content"],
              str(mb.memory_db["小明"]))
        mb._remember_task_result("", {"status": "success", "reply_text": "x"})
        check("T15 空 chat 不写记忆", len(mb.memory_db) == 1, str(mb.memory_db))
        check("T15 记忆落盘", os.path.isfile(os.path.join(mem_dir, "mem.json")))
        mb2 = make_mem_bot(mem_dir)
        mb2._load_memory()
        check("T15 重启后任务结果记忆仍在",
              "小明" in mb2.memory_db
              and "[任务结果]" in mb2.memory_db["小明"]["recent"][0]["content"],
              str(mb2.memory_db.keys()))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("=" * 60)
    print(f"结果: {passed} 通过, {failed} 失败")
    return 0 if failed == 0 else 1
