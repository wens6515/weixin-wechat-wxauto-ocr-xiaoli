# -*- coding: utf-8 -*-
"""任务桥核心（模块函数，可独立测试，不依赖微信）：任务投递 / 回传轮询 /
天枢窗口触发 / 剪贴板 CF_HDROP / 单实例互斥 / 任务状态扫描。

从 xiaoli_bot.py 拆出（纯移动，零逻辑改动）；xiaoli_bot 显式 re-import
保持既有导入路径（tests / xiaoli_app.setup / 旧调用方）不变。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import struct
import sys
import time
import uuid

import pyautogui
import requests as req

logger = logging.getLogger("xiaoli")

# 任务分类 prompt（classify_task_with_llm 的 system 消息，自 xiaoli_bot 迁入）
CLASSIFY_PROMPT = (
    "你是微信机器人小漓的『任务路由器』。判断用户发来的消息是否是一个需要由 AI 代理（天枢）"
    "实际执行的任务请求——例如：根据文档做一个网站、做一个 PPT、写一段代码、分析一份数据、"
    "整理文件、生成文档、下载并处理内容等需要动手完成的工作。\n"
    "普通的闲聊、打招呼、问问题、要资料等不算任务。\n"
    "只输出一个 JSON 对象，不要输出任何其他文字：\n"
    '{"is_task": true 或 false, "task": "当 is_task 为 true 时，用一句清晰的话描述要完成的任务"}'
)

def generate_task_id():
    """时间戳 + 4 位随机：YYYYmmddHHMMSSxxxx"""
    return time.strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:4]

def _parse_classify_json(raw):
    """解析 LLM 分类输出。任何异常都兜底为 is_task=False（不误投递、不崩溃）"""
    if not raw:
        return {"is_task": False, "task": ""}
    text = raw.strip()
    # 剥 markdown 代码围栏
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    m = re.search(r"\{.*\}", text, flags=re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            return {
                "is_task": bool(data.get("is_task", False)),
                "task": str(data.get("task", "")).strip(),
            }
        except Exception:
            pass  # JSON 非法 → 落到正则兜底
    # 正则兜底：无完整 JSON 时提取 is_task / task 字段（不依赖大括号闭合）
    is_task = bool(re.search(r'"is_task"\s*:\s*true', text, flags=re.I))
    tm = re.search(r'"task"\s*:\s*"([^"]*)"', text)
    task = tm.group(1) if tm else ""
    return {"is_task": is_task, "task": task}


def classify_task_with_llm(api_url, api_key, model, text, timeout=30):
    """LLM 任务判断。API 异常/解析失败一律返回 is_task=False"""
    if not text or not text.strip():
        return {"is_task": False, "task": ""}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    messages = [
        {"role": "system", "content": CLASSIFY_PROMPT},
        {"role": "user", "content": text},
    ]
    payload = {"model": model, "messages": messages, "temperature": 0, "max_tokens": 200}
    try:
        resp = req.post(api_url, headers=headers, json=payload, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            choices = data.get("choices", [])
            if choices:
                raw = choices[0].get("message", {}).get("content", "")
                result = _parse_classify_json(raw)
                logger.info(f"[任务判断] is_task={result['is_task']} task={result['task'][:60]!r}")
                return result
            logger.warning("[任务判断] API 返回无 choices")
        else:
            logger.warning(f"[任务判断] API 错误: {resp.status_code}")
    except Exception as e:
        logger.error(f"[任务判断] 异常: {e}")
    return {"is_task": False, "task": ""}


def dispatch_task(tasks_dir, task_info, attachment_paths=None):
    """创建任务目录并写入 task.json + 复制附件。返回 task_id"""
    task_id = generate_task_id()
    task_dir = os.path.join(tasks_dir, task_id)
    os.makedirs(task_dir, exist_ok=True)
    info = dict(task_info)
    info["task_id"] = task_id
    info["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    # 复制附件
    att_dir = os.path.join(task_dir, "attachments")
    copied = []
    for p in (attachment_paths or []):
        if p and os.path.isfile(p):
            os.makedirs(att_dir, exist_ok=True)
            dest = os.path.join(att_dir, os.path.basename(p))
            try:
                shutil.copy2(p, dest)
                # 微信接收目录的文件是只读副本，copy2 会把只读位一起带过来——
                # 留着它，任务目录之后删不掉（打包脚本清 dist、用户/界面清理
                # 任务都会撞 PermissionError [WinError 5]，已实测）。落盘后
                # 立即清掉只读位（Windows 只看只读位，0o666 即「可写」）。
                try:
                    os.chmod(dest, 0o666)
                except OSError:
                    pass
                copied.append(os.path.basename(p))
            except Exception as e:
                logger.error(f"[投递] 复制附件失败 {p}: {e}")
    info["attachments"] = copied
    with open(os.path.join(task_dir, "task.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    logger.info(f"[投递] 任务已创建: {task_dir}")
    return task_id


def list_windows():
    """枚举顶层窗口，返回 [(标题, 句柄)]，过滤空标题"""
    import uiautomation as auto
    root = auto.GetRootControl()
    wins = []
    for w in root.GetChildren():
        try:
            name = w.Name
            if name and name.strip():
                wins.append((name.strip(), w.NativeWindowHandle))
        except Exception:
            continue
    return wins


def find_window_by_title(title):
    """按标题子串匹配窗口控件，返回控件或 None"""
    import uiautomation as auto
    try:
        win = auto.WindowControl(searchDepth=1, SubName=title)
        if win.Exists(0, 0):
            return win
    except Exception as e:
        logger.error(f"[窗口] 查找失败: {e}")
    return None


def clipboard_set_text(text):
    """把文本放入剪贴板（pyperclip 优先，失败退回 ctypes）"""
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        pass
    try:
        import ctypes
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not user32.OpenClipboard(0):
            return False
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        p = kernel32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(h)
        user32.SetClipboardData(CF_UNICODETEXT, h)
        user32.CloseClipboard()
        return True
    except Exception as e:
        logger.error(f"[剪贴板] 失败: {e}")
        return False


def set_clipboard_files(filepaths):
    """把文件列表放入剪贴板（CF_HDROP），供微信聊天框 Ctrl+V 粘贴发送。返回 bool"""
    import ctypes
    CF_HDROP = 15
    GMEM_MOVEABLE = 0x0002
    try:
        paths = [os.path.abspath(p) for p in filepaths if os.path.isfile(p)]
        if not paths:
            return False
        # DROPFILES 结构（20 字节头）+ UTF-16 路径列表（双空结尾）
        header = bytearray(20)
        struct.pack_into("<I", header, 0, 20)  # pFiles: 结构起始偏移
        header[16] = 1                          # fWide=1 → Unicode
        data = bytes(header)
        for p in paths:
            data += p.encode("utf-16-le") + b"\x00\x00"
        data += b"\x00\x00"
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # 显式声明 64 位句柄签名，避免默认 c_int 截断
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p
        h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h_mem:
            return False
        p_mem = kernel32.GlobalLock(h_mem)
        if not p_mem:
            kernel32.GlobalFree(h_mem)
            return False
        ctypes.memmove(p_mem, data, len(data))
        kernel32.GlobalUnlock(h_mem)
        if not user32.OpenClipboard(0):
            kernel32.GlobalFree(h_mem)
            return False
        user32.EmptyClipboard()
        ok = bool(user32.SetClipboardData(CF_HDROP, h_mem))
        user32.CloseClipboard()
        return ok
    except Exception as e:
        logger.error(f"[剪贴板] 设置文件失败: {e}")
        return False


def read_clipboard_files():
    """读回剪贴板 CF_HDROP 文件列表（自检 round-trip 用）。返回路径列表"""
    import ctypes
    CF_HDROP = 15
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        user32.GetClipboardData.restype = ctypes.c_void_p
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
        kernel32.GlobalSize.restype = ctypes.c_size_t
        if not user32.OpenClipboard(0):
            return []
        try:
            h = user32.GetClipboardData(CF_HDROP)
            if not h:
                return []
            p = kernel32.GlobalLock(h)
            size = kernel32.GlobalSize(h)
            buf = ctypes.string_at(p, size)
            kernel32.GlobalUnlock(h)
            data = buf[20:]
            parts = data.decode("utf-16-le", errors="ignore").split("\x00")
            return [x for x in parts if x]
        finally:
            user32.CloseClipboard()
    except Exception as e:
        logger.error(f"[剪贴板] 读取失败: {e}")
        return []


def activate_window_by_title(title):
    """激活匹配标题的窗口，返回 bool"""
    win = find_window_by_title(title)
    if win is None:
        return False
    try:
        win.SetActive()
        # SetActive 之外再强制 SetForegroundWindow：终端类窗口（天枢 CLI 跑在
        # Windows Terminal/cmd）仅 SetActive 有时不把焦点真正切到前台，导致
        # 后续 Ctrl+V/回车发到错误窗口（用户实测 /yes、开始处理均未提交）。
        try:
            hwnd = win.NativeWindowHandle
            if hwnd:
                import ctypes
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                time.sleep(0.2)
        except Exception:
            pass
        return True
    except Exception as e:
        logger.error(f"[窗口] 激活失败: {e}")
        return False


def send_trigger_to_window(title, command, hold=0.5, enter_times=1):
    """激活窗口 → 剪贴板粘贴指令 → 回车（enter_times 次）。返回 bool

    enter_times=2：天枢 CLI 首轮提示词实测需连续两次回车才提交；
    日常唤起指令保持 1 次（默认），避免重复触发。
    """
    if not activate_window_by_title(title):
        return False
    time.sleep(hold)
    if not clipboard_set_text(command):
        return False
    time.sleep(0.1)
    pyautogui.hotkey("ctrl", "v")
    # 粘贴后等待：长文本（首轮提示词几百字多行）粘贴比短指令慢，按内容长度
    # 自适应；等待不足时回车会按在粘贴未完成/输入框未就绪的状态（丢回车）。
    paste_wait = 1.0 if len(command) > 200 else 0.4
    time.sleep(paste_wait)
    for _ in range(enter_times):
        pyautogui.press("enter")
        time.sleep(0.3)
    # 前端日志（INFO 轨）只留一行摘要：窗口标题可能是整条命令行（WT 默认
    # 标题形态），指令全文首轮提示词有上千字——直接打会把前端日志页刷爆。
    # 全文与完整窗口标题落 bot.log（DEBUG 轨），排障看全量。
    _command = command or ""
    _head = _command.splitlines()[0].strip() if _command else ""
    _title_show = title if len(title) <= 60 else title[:60] + "…"
    logger.info(f"[天枢] 已向窗口「{_title_show}」发送指令: "
                f"{_head[:40]}{'…' if len(_command) > 40 else ''}"
                f"（共 {len(_command)} 字，回车 {enter_times} 次）")
    logger.debug(f"[天枢] 指令全文（窗口「{title}」）：{_command}")
    return True


def resolve_result_file(task_dir, sent_dir, fname):
    """定位成果文件：优先任务目录，其次 sent 归档目录。
    并发/重试场景下任务可能已被归档，成果文件实际在 sent\\<任务id>\\ 下——
    直接引用归档前的旧路径会报「文件路径不存在」"""
    p = os.path.join(task_dir, fname)
    if os.path.isfile(p):
        return p
    alt = os.path.join(sent_dir, os.path.basename(task_dir), fname)
    if os.path.isfile(alt):
        return alt
    return p


def poll_outbox(tasks_dir, deliver, sent_dir=None):
    """轮询任务目录：发现 result.json → 先归档进 sent → deliver(sent\\任务id, task_info, result) 发送。
    先归档后发送保证文件路径稳定（不被并发移动）；deliver 抛异常则回滚归档、下轮重试。
    返回已处理的任务 id 列表"""
    if not os.path.isdir(tasks_dir):
        return []
    sent_dir = sent_dir or os.path.join(tasks_dir, "sent")
    os.makedirs(sent_dir, exist_ok=True)
    handled = []
    for name in sorted(os.listdir(tasks_dir)):
        task_dir = os.path.join(tasks_dir, name)
        if not os.path.isdir(task_dir) or name == "sent":
            continue
        result_path = os.path.join(task_dir, "result.json")
        if not os.path.isfile(result_path):
            continue  # 天枢处理中
        try:
            with open(result_path, "r", encoding="utf-8") as f:
                result = json.load(f)
        except Exception as e:
            logger.error(f"[回传] result.json 解析失败 {name}: {e}")
            continue
        task_info = {}
        task_json = os.path.join(task_dir, "task.json")
        if os.path.isfile(task_json):
            try:
                with open(task_json, "r", encoding="utf-8") as f:
                    task_info = json.load(f)
            except Exception:
                pass
        archived_dir = os.path.join(sent_dir, name)
        # 幂等守卫：sent 下已有同名目录 = 该任务之前已回传归档。顶层残留的
        # task_dir 是上次 move 失败留下的残骸（deliver 已执行过），直接清理
        # 跳过，绝不二次投递（真机根因：15:03 同一任务重复回传两次）。
        if os.path.isdir(archived_dir):
            try:
                shutil.rmtree(task_dir)
            except Exception:
                pass
            logger.warning(f"[回传] 任务 {name} 已在 sent 归档，跳过残留顶层目录")
            continue
        try:
            # 先归档（移入 sent）再发送：move 是幂等标记，只有 move 成功
            # （任务真正离开顶层）才 deliver。move 失败必须抛异常回滚重试，
            # 不得吞异常继续发送（旧逻辑吞异常导致任务残留顶层、重复投递）。
            shutil.move(task_dir, archived_dir)
            deliver(archived_dir, task_info, result)
            handled.append(name)
            logger.info(f"[回传] 任务 {name} 已回传并归档")
        except Exception as e:
            # deliver 失败：回滚归档（移回顶层），下轮重试
            try:
                if os.path.isdir(archived_dir) and not os.path.isdir(task_dir):
                    shutil.move(archived_dir, tasks_dir)
            except Exception:
                pass
            logger.error(f"[回传] 任务 {name} 回传失败，下轮重试: {e}")
    return handled


def has_active_tasks(tasks_dir, stale_after=7200):
    """任务目录顶层是否存在尚未归档的任务（天枢处理中 / 已完成待回传 / 回传失败重试中）。
    只有含 task.json 的子目录才算任务（排除 sent\\ 与 attachments 等非任务目录）。
    全部任务归档进 sent\\ 后返回 False —— 这是恢复消息监听的判据。

    stale_after：卡死任务兜底——task.json 创建超过该秒数（默认 2 小时）仍无
    result.json，视为天枢未处理/失联的死任务，不再阻塞消息轮询（否则一次
    投递失败会让 bot 从此永不监听微信消息）。
    """
    if not os.path.isdir(tasks_dir):
        return False
    now = time.time()
    for name in os.listdir(tasks_dir):
        task_dir = os.path.join(tasks_dir, name)
        if os.path.isdir(task_dir) and name != "sent":
            tj = os.path.join(task_dir, "task.json")
            if not os.path.isfile(tj):
                continue
            if os.path.isfile(os.path.join(task_dir, "result.json")):
                return True  # 已完成待回传：仍算活跃（成果未发回微信前不恢复监听）
            try:
                with open(tj, "r", encoding="utf-8") as f:
                    info = json.load(f)
                created = float(info.get("created_at") or 0)
            except (OSError, ValueError, TypeError):
                created = 0
            if created and (now - created) > stale_after:
                logger.warning(
                    f"[任务桥] 任务 {name} 卡死超过 {stale_after}s 未完成，"
                    "不再阻塞消息轮询（可在任务页查看/清理）")
                continue
            return True
    return False


def should_resume_listen(has_active, was_active, end_time, now, hold):
    """消息监听恢复状态机（任务完成后延迟 hold 秒再恢复，缓冲文件发送）。
    返回 (resume, new_was_active, new_end_time)：
    - 任务进行中（has_active=True）→ 不恢复，记录曾暂停（was_active=True）
    - 任务刚完成（was_active=True 且 end_time 为 None）→ 从 now 起进入缓冲期
    - 缓冲期内（now - end_time < hold）→ 不恢复
    - 缓冲期满 → 恢复监听，状态复位"""
    if has_active:
        return False, True, None
    if was_active:
        if end_time is None:
            end_time = now
        if now - end_time < hold:
            return False, True, end_time
        return True, False, None
    return True, False, None


_SINGLE_INSTANCE_MUTEX = None


def acquire_single_instance(name="XiaoLi_SingleInstance"):
    """Windows 会话级互斥体：检测是否已有小漓实例在运行（GUI 与 CLI 共用
    同一互斥体名——否则双开互不排斥，两个进程会同时抢微信窗口/发消息）。
    返回 True = 获得唯一实例；False = 已有实例，应退出。
    进程退出（含被杀）时内核自动释放句柄，无残留锁问题"""
    global _SINGLE_INSTANCE_MUTEX
    if sys.platform != "win32":
        return True  # 非 Windows 不启用
    import ctypes
    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.windll.kernel32
    h = kernel32.CreateMutexW(None, False, name)
    if not h:
        return True  # 创建失败不阻塞运行（保守）
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(h)
        return False
    _SINGLE_INSTANCE_MUTEX = h
    return True


def release_single_instance():
    """释放互斥体（正常流程无需调用——进程退出自动释放；供自检/测试用）"""
    global _SINGLE_INSTANCE_MUTEX
    if _SINGLE_INSTANCE_MUTEX:
        import ctypes
        ctypes.windll.kernel32.CloseHandle(_SINGLE_INSTANCE_MUTEX)
        _SINGLE_INSTANCE_MUTEX = None

def scan_task_status(tasks_dir):
    """扫描任务目录，返回 (entries, waiting, done, archived)。
    entries: [(name, state, desc, mtime_str)]，state ∈ {"waiting", "done",
    "archived"}。waiting/done 来自顶层任务目录（有 task.json）；archived
    来自 sent\\ 归档目录（任务 ID + 描述 + 归档时刻，供任务页列出与删除）。
    CLI task-status 命令与 GUI 任务页共用（历史缺陷：两处各扫一遍任务目录，
    逻辑漂移）。无 task.json 的非任务目录不进入 entries。"""
    waiting = done = 0
    entries = []
    if not os.path.isdir(tasks_dir):
        return entries, waiting, done, 0
    for name in sorted(os.listdir(tasks_dir), reverse=True):
        task_dir = os.path.join(tasks_dir, name)
        if not os.path.isdir(task_dir) or name == "sent":
            continue
        tj = os.path.join(task_dir, "task.json")
        if not os.path.isfile(tj):
            continue
        try:
            with open(tj, "r", encoding="utf-8") as f:
                info = json.load(f)
        except Exception:
            continue
        desc = str(info.get("task", ""))[:50]
        has_result = os.path.isfile(os.path.join(task_dir, "result.json"))
        state = "done" if has_result else "waiting"
        mtime = time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(tj)))
        entries.append((name, state, desc, mtime))
        if has_result:
            done += 1
        else:
            waiting += 1
    archived = 0
    sent_dir = os.path.join(tasks_dir, "sent")
    if os.path.isdir(sent_dir):
        for name in sorted(os.listdir(sent_dir), reverse=True):
            task_dir = os.path.join(sent_dir, name)
            if not os.path.isdir(task_dir):
                continue
            desc = ""
            tj = os.path.join(task_dir, "task.json")
            if os.path.isfile(tj):
                try:
                    with open(tj, "r", encoding="utf-8") as f:
                        desc = str(json.load(f).get("task", ""))[:50]
                except Exception:
                    pass
            try:
                mtime = time.strftime(
                    "%m-%d %H:%M", time.localtime(os.path.getmtime(task_dir)))
            except OSError:
                mtime = ""
            entries.append((name, "archived", desc, mtime))
            archived += 1
    return entries, waiting, done, archived
