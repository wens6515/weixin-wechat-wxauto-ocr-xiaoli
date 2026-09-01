# -*- coding: utf-8 -*-
"""小漓合并版：普通微信聊天 + 天枢任务桥

收到消息 → LLM 判断是否任务：
  - 闲聊/提问 → 原聊天路线（call_chat_ai 回复）
  - 任务请求（如"根据文档做一个网站"）→ 投递 D:\\工作间\\wxauto\\<任务id>\\ + 唤起天枢 CLI 窗口输入"开始处理"
天枢完成任务后写 result.json + 成果文件 → bot 轮询回传（文本 SendMsg + 文件 SendFiles）→ 归档 sent\\

运行：python xiaoli_bot.py --run     自检：python xiaoli_bot.py --test
"""
import json, os, sys, time, re, tempfile, subprocess, logging, traceback, shutil, uuid, struct, base64
import queue
import threading
import requests as req
import pyautogui
from wechat_bot import (WeChatBot, Controller, load_config, logger,
                        is_group_chat, _extract_file_name_token,
                        VISION_MODEL_DEFAULT)
from wx_backend.models import MessageType
from xiaoli_app.reminders_store import RemindersStore, GRACE_SECONDS

if getattr(sys.stdout, "encoding", "utf-8") != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# =====================================================================
# 任务桥配置
# =====================================================================

TASK_DEFAULTS = {
    "task_enabled": True,
    "tasks_dir": r"D:\工作间\wxauto",
    "tianshu_window_title": "",            # 空 = 启动时交互选择
    "tianshu_trigger_command": "开始处理",
    "tianshu_poll_interval": 5,
    "file_send_method": "clipboard",   # 成果文件发送方式（UI 保存；业务恒走剪贴板，wxauto 后端已移除）
    "listen_hold_seconds": 2,   # 任务完成后延迟恢复消息监听的秒数（缓冲文件发送，防止切窗打断）
}


def load_merged_config(path="config.json"):
    """CLI 配置入口：委托 config_store（迁移/投影/写回——与 GUI 同一事实源），
    再补齐 task_* 默认。历史缺陷：CLI 走 wechat_bot.load_config、GUI 走
    config_store.load_config_store，两套默认值并存（tasks_dir 默认都不一致），
    字段补全逻辑漂移。统一后 CLI 也支持角色卡/多 provider。
    """
    from xiaoli_app import config_store as _cs
    base = os.path.dirname(os.path.abspath(path)) or "."
    cfg = _cs.load_config_store(path, os.path.join(base, "cards"))
    changed = False
    for k, v in TASK_DEFAULTS.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    if changed:
        try:
            _cs.save_config(cfg, path)
        except OSError as e:
            logger.error(f"[配置] 写回失败: {e}")
        logger.info("[配置] 已补齐 task_* 配置项")
    return cfg


# =====================================================================
# 任务桥核心（模块函数，可独立测试，不依赖微信）
# =====================================================================

CLASSIFY_PROMPT = (
    "你是微信机器人小漓的『任务路由器』。判断用户发来的消息是否是一个需要由 AI 代理（天枢）"
    "实际执行的任务请求——例如：根据文档做一个网站、做一个 PPT、写一段代码、分析一份数据、"
    "整理文件、生成文档、下载并处理内容等需要动手完成的工作。\n"
    "普通的闲聊、打招呼、问问题、要资料等不算任务。\n"
    "只输出一个 JSON 对象，不要输出任何其他文字：\n"
    '{"is_task": true 或 false, "task": "当 is_task 为 true 时，用一句清晰的话描述要完成的任务"}'
)


VISION_ROUTE_PROMPT = (
    "请严格遵守以上人设（包括不用 emoji、改用颜文字）。现在判断用户的消息：\n"
    "如果用户的消息是需要由 AI 代理（天枢）实际执行的任务——例如：根据文档做一个网站、"
    "做一个 PPT、写一段代码、分析一份数据、整理文件、生成文档等需要动手完成的工作——"
    "调用 dispatch_task 工具投递任务，任务描述写在工具参数里。\n"
    "否则（普通的闲聊、打招呼、问问题、要资料等）直接以你的身份回复用户，"
    "不需要调用任何工具。"
)


def generate_task_id():
    """时间戳 + 4 位随机：YYYYmmddHHMMSSxxxx"""
    return time.strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:4]


def _looks_like_file_text(text):
    """判断 OCR 文本是否为文件消息的显示文本（含常见文档扩展名）。

    视觉后端把文件消息 OCR 成文件名文本（多条合并如
    '新宣传.docx 部门简介+纳新宣传.docx W'，type 可能是 TEXT）——
    _wait_pending_instruction 找用户指令时须跳过这类文本，否则会把
    文件名当指令（LLM 对文件名判 is_task=False 且真实指令被错过）。
    """
    return bool(re.search(
        r"\.(?:docx?|xlsx?|pptx?|pdf|txt|md|html?|json|csv|zip|rar|7z|png|jpe?g|gif|mp4|mp3)\b",
        text or "", flags=re.I))


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
    logger.info(f"[天枢] 已向窗口「{title}」发送指令: {command}（回车 {enter_times} 次）")
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


# =====================================================================
# AgentBot：普通聊天 + 天枢任务桥
# =====================================================================

def scan_task_status(tasks_dir):
    """扫描任务目录，返回 (entries, waiting, done, archived)。
    entries: [(name, state, desc, mtime_str)]，state ∈ {"waiting", "done"}。
    CLI task-status 命令与 GUI 任务页共用（历史缺陷：两处各扫一遍任务目录，
    逻辑漂移）。sent 归档目录与无 task.json 的非任务目录不进入 entries。"""
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
        archived = len(os.listdir(sent_dir))
    return entries, waiting, done, archived


class ReminderScheduler(threading.Thread):
    """定时消息闹钟线程（用户定案模型）：只算时间、只投队列，绝不触碰
    微信窗口——发送必须发生在主循环节点内（与红圈扫描/成果回传共享微信
    窗口，闹钟线程发消息会撞车，_sending_lock 先例同理）。

    每 ≤scan_seconds 重扫一次 store：到期条目推入队列；(id, fire_at)
    内存去重防重发；扫描间隔同时兜住设置页的增删改（文件为事实源）。"""

    def __init__(self, store, out_queue, stop_event=None, scan_seconds=5.0):
        super().__init__(name="xiaoli-reminders", daemon=True)
        self.store = store
        self.out_queue = out_queue
        # 注意不可命名为 _stop——threading.Thread.join 内部调用 self._stop()
        self._stop_evt = stop_event if stop_event is not None else threading.Event()
        self._scan_seconds = scan_seconds
        self._claimed = set()

    def run(self):
        while not self._stop_evt.is_set():
            try:
                now = time.time()
                pending, _missed = self.store.due(now)
                for r in pending:
                    key = (r.get("id"), r.get("fire_at"))
                    if key in self._claimed:
                        continue
                    self._claimed.add(key)
                    self.out_queue.put(r)
                    logger.info(f"[定时] 到期入队: {r.get('chat')} <- {str(r.get('content'))[:40]}")
                if len(self._claimed) > 512:
                    self._claimed = {k for k in self._claimed
                                     if (k[1] or 0) >= now - 86400}
            except Exception as e:
                logger.error(f"[定时] 调度扫描失败: {e}")
            self._stop_evt.wait(self._scan_seconds)


# 长记忆压缩：提炼「必须记住的重要记忆 + 关键词记忆索引」（一次调用双产出）。
# 每攒满 memory_compress_batch 条深层消息触发一次；输入带上已有条目供模型
# 去重（不重复提炼、不重复建索引）。
MEMORY_COMPRESS_PROMPT = (
    "你是微信机器人小漓的记忆整理器。下面是与某位联系人的一段聊天记录"
    "（按时间顺序，[用户]/[小漓] 标记说话方）。请完成两件事：\n"
    "1. 提炼「必须记住的重要记忆」：身份信息、长期偏好、重要承诺或约定、"
    "重大事件、称呼与关系变化等长期有效的信息。只提炼确实重要且长期有效的，"
    "每条不超过 50 字；已给出的重要记忆里已有的不要再输出；没有就输出空数组。\n"
    "2. 为这段记录配置「关键词记忆索引」：挑出用户日后可能再次提起的话题，"
    "每个话题给 2-5 个具体检索关键词（人名/事件/物品名等，不要宽泛词如"
    "「聊天」「事情」），并写一段对应的记忆描述（保留关键细节，不超过 80 字）。\n"
    "只输出一个 JSON 对象，不要输出任何其他文字：\n"
    '{"important": [{"content": "重要记忆"}], "index": [{"kw": ["关键词1", "关键词2"], "mem": "记忆描述"}]}'
)


def parse_compress_json(raw):
    """解析压缩模型输出。任何异常/缺字段都兜底为空产出（不中断压缩循环）。"""
    if not raw:
        return [], []
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw).strip(), flags=re.S)
    m = re.search(r"\{.*\}", text, flags=re.S)
    data = None
    if m:
        try:
            data = json.loads(m.group(0))
        except Exception:
            data = None
    if not isinstance(data, dict):
        return [], []
    important = []
    for item in (data.get("important") or []):
        if isinstance(item, dict) and str(item.get("content") or "").strip():
            important.append({"content": str(item["content"]).strip()})
        elif isinstance(item, str) and item.strip():
            important.append({"content": item.strip()})
    index = []
    for item in (data.get("index") or []):
        if not isinstance(item, dict):
            continue
        kws = [str(k).strip() for k in (item.get("kw") or item.get("keywords") or [])
               if str(k).strip()]
        mem = str(item.get("mem") or item.get("memory") or "").strip()
        if kws and mem:
            index.append({"kw": kws, "mem": mem})
    return important, index


class MemoryCompressor(threading.Thread):
    """长记忆压缩线程：扫描各聊天的深层记忆增量，攒满 batch 条调压缩模型
    提炼「重要记忆 + 关键词索引」，结果经 bot.memory_commit_compression
    锁内写回。API 调用不持锁（只持锁快照段与写回结果）；每轮最多压缩一个
    聊天（串行 API，失败不重试——下轮扫描重新尝试同一段，索引边界不推进）。"""

    def __init__(self, bot, stop_event=None, scan_seconds=20.0):
        super().__init__(name="xiaoli-memory-compress", daemon=True)
        self.bot = bot
        self._stop_evt = stop_event if stop_event is not None else threading.Event()
        self._scan_seconds = scan_seconds

    def run(self):
        while not self._stop_evt.is_set():
            try:
                self._scan_once()
            except Exception as e:
                logger.error(f"[记忆压缩] 扫描异常: {e}")
            self._stop_evt.wait(self._scan_seconds)

    def _scan_once(self):
        bot = self.bot
        if not getattr(bot, "memory_compress_enabled", False):
            return
        if not getattr(bot, "memory_deep_enabled", False):
            return  # 深层记忆关着则没有溢出归档，无从压缩
        batch = int(bot.memory_compress_batch)
        with bot._memory_lock:
            chats = list(bot.memory_db.keys())
        for chat in chats:
            st = bot.memory_db.get(chat)
            if not isinstance(st, dict):
                continue
            total = int(bot._deep_count.get(chat, 0))
            if total - int(st.get("indexed") or 0) < batch:
                continue
            self._compress_chat(chat)
            return  # 每轮最多压缩一个聊天（串行 API 调用）

    def _compress_chat(self, chat):
        bot = self.bot
        st = bot.memory_db.get(chat) or {}
        indexed = int(st.get("indexed") or 0)
        msgs, consumed = bot._read_deep_range(
            chat, indexed, int(bot.memory_compress_batch))
        if not msgs:
            # 文件行数与计数不一致（外部改动）：把边界推进到计数处防死循环
            bot.memory_commit_compression(
                chat, int(bot._deep_count.get(chat, 0)), [], [])
            return
        existing_imp = [str(x.get("content") or "").strip()
                        for x in (st.get("important") or [])]
        existing_kw = [str(k)
                       for e in (st.get("index") or [])
                       for k in (e.get("kw") or [])]
        parts = []
        if existing_imp:
            parts.append("已有重要记忆（勿重复提炼）：\n"
                         + "\n".join(f"- {x}" for x in existing_imp if x))
        if existing_kw:
            parts.append("已有关键词（勿重复建索引）：\n"
                         + "、".join(dict.fromkeys(existing_kw)))
        parts.append("聊天记录：\n" + "\n".join(
            f"[{'用户' if m.get('role') == 'user' else '小漓'}]"
            f"[{m.get('time', '')}] {str(m.get('content') or '')[:300]}"
            for m in msgs))
        model = bot.memory_compress_model or bot.chat_model \
            or VISION_MODEL_DEFAULT
        headers = {"Authorization": f"Bearer {bot.api_key}",
                   "Content-Type": "application/json"}
        messages = [{"role": "system", "content": MEMORY_COMPRESS_PROMPT},
                    {"role": "user", "content": "\n\n".join(parts)}]
        payload = {"model": model, "messages": messages,
                   "temperature": 0.3, "max_tokens": 2000}
        try:
            data = bot._post_chat_completions(
                bot.api_url, headers, payload, 60, label="memory",
                meta={"kind": "memory", "model": model, "messages": messages})
            raw = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.warning(f"[记忆压缩] {chat} 调用失败（下轮重试）: {e}")
            return
        important, index = parse_compress_json(raw)
        bot.memory_commit_compression(chat, consumed, important, index)
        logger.info(f"[记忆压缩] {chat} 已压缩 {consumed - indexed} 条"
                    f"（重要 +{len(important)}，索引 +{len(index)}）")


class AgentBot(WeChatBot):
    """小漓合并版：继承原 WeChatBot（聊天/图片/文件识别），叠加天枢任务桥"""

    def __init__(self, cfg, stop_event=None, max_connect_retries=None):
        """stop_event/max_connect_retries：透传给 WeChatBot（GUI 引擎停止
        可中断微信连接重试；重试超限抛异常 → 引擎 error 状态）。CLI 模式
        不传：保留无限重试（用户开着 bot 等微信启动）。"""
        super().__init__(cfg, stop_event=stop_event,
                         max_connect_retries=max_connect_retries)
        self.task_enabled = cfg.get("task_enabled", True)
        self.tasks_dir = cfg.get("tasks_dir", r"D:\工作间\wxauto")
        self.tianshu_window_title = cfg.get("tianshu_window_title", "")
        self.tianshu_trigger_command = cfg.get("tianshu_trigger_command", "开始处理")
        self.tianshu_workdir = cfg.get("tianshu_workdir", "")  # CLI（rivet）工作目录，resolve_cli_window 第 3 级启动时使用
        self.tianshu_poll_interval = cfg.get("tianshu_poll_interval", 5)
        self._last_poll_time = 0
        self._sending_lock = False  # 成果回传期间置 True，暂停消息轮询防发错联系人
        self._listen_hold_seconds = cfg.get("listen_hold_seconds", 10)
        self._task_was_active = False  # 是否曾因任务暂停监听（用于任务完成后的缓冲期）
        self._task_end_time = None     # 最后一次任务完成（归档）的时刻
        try:
            os.makedirs(self.tasks_dir, exist_ok=True)
        except OSError as e:
            logger.warning(f"[AgentBot] tasks_dir 创建失败 {self.tasks_dir}: {e}，回退用户目录")
            from xiaoli_app import config_store as _cs
            self.tasks_dir = _cs.default_tasks_dir()
            os.makedirs(self.tasks_dir, exist_ok=True)
        # 首轮提示词引导天枢读 tasks_dir\README.md：首次生成协议文档（已存在不覆盖）
        try:
            from xiaoli_app import setup as _setup
            _setup.ensure_bridge_readme(self.tasks_dir)
        except Exception as e:
            logger.warning(f"[AgentBot] 生成任务桥 README 失败: {e}")
        # 全自动模式由首启一次性引导的 /yes 保证（持久化，重启后仍生效，
        # 见 run_first_run_guide）——不再配置 config 级 YOLO（旧机制：
        # rivet config set-approval 只影响下次启动，且 /yes 已覆盖此需求）。
        # 是否暂停消息监听由 has_active_tasks 每次实时判断，不保存粘滞状态
        self._pending_files = {}  # chat_name -> {sender} 群聊文件等待用户指令
        # 占位回复计数（_pending_placeholders）由基类 WeChatBot 提供（基类
        # __init__ 初始化；placeholder 发送 +1 / 实质回复归零，按 chat_name 隔离）
        self._sent_back_files = {}  # 回传成果文件 绝对路径 -> mtime（目录扫描时排除，防误当用户发送的文件）
        # 回传成果文件名主干 -> 发送时刻（排除微信写入接收目录的成果副本）。
        # 持久化到 tasks_dir 下，bot 重启后仍生效（否则重启后历史成果副本会再次被误当用户文件）
        self._sent_back_stems = {}
        self._sent_back_stems_file = os.path.join(self.tasks_dir, "sent_back_stems.json")
        self._load_sent_back_stems()
        # 失败退避：处理未产出回复（红圈滞留）的会话 8s 内不重复处理——
        # 防滞留红圈在 0.5s 快档下每轮都打一遍整条 OCR 管线，把单核打满
        self._chat_fail_at = {}
        self._fail_backoff = 8.0
        # 定时消息：存储 + 闹钟线程 + 到期队列（发送在主循环节点消费）
        self.reminders = RemindersStore()
        self._reminder_queue = queue.Queue()
        self._reminder_sched = ReminderScheduler(
            self.reminders, self._reminder_queue, stop_event=stop_event)
        self._reminder_sched.start()
        # 长记忆压缩线程：深层记忆攒满 batch 条后提炼重要记忆/关键词索引
        # （memory_compress_enabled 关闭时线程空转等待，设置热改后自动生效）
        self._memory_compressor = MemoryCompressor(self, stop_event=stop_event)
        self._memory_compressor.start()
        logger.info(f"[AgentBot] tasks_dir={self.tasks_dir}, task_enabled={self.task_enabled}")

    def _load_sent_back_stems(self):
        """启动时加载成果登记（重启后排除仍生效）"""
        try:
            if os.path.isfile(self._sent_back_stems_file):
                with open(self._sent_back_stems_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._sent_back_stems = {str(k): float(v) for k, v in data.items()}
                logger.info(f"[任务桥] 已加载 {len(self._sent_back_stems)} 条成果登记")
        except Exception as e:
            logger.error(f"[任务桥] 加载成果登记失败: {e}")

    # ---------- 任务桥 ----------

    def set_tianshu_window_title(self, title):
        """记住天枢窗口标题（写回 config.json）"""
        self.tianshu_window_title = title
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg["tianshu_window_title"] = title
            with open("config.json", "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=4)
            logger.info(f"[天枢] 窗口标题已保存到 config: {title}")
        except Exception as e:
            logger.error(f"[天枢] 保存窗口标题失败: {e}")

    def _select_window(self):
        """交互式选择天枢窗口（启动时/命令调用）。返回所选标题或 None"""
        wins = list_windows()
        if not wins:
            logger.error("未找到任何窗口")
            return None
        for i, (name, _h) in enumerate(wins, 1):
            print(f"  {i}. {name}")
        choice = input("请输入序号选择天枢窗口，输入 cancel 取消: ").strip()
        if choice.lower() == "cancel":
            return None
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(wins):
                title = wins[idx][0]
                self.set_tianshu_window_title(title)
                return title
            logger.error("序号超出范围")
        except ValueError:
            logger.error("请输入有效序号")
        return None

    def apply_role(self, card, providers=None):
        """热切换角色卡：人格/昵称/模型/参数/端点（带锁）。

        card 由 card_store 规范化；providers 为 config.json 的 providers 路由表。
        端点投影：聊天/视觉按各自 provider 解析 base_url + key（config_store.project_config）。
        """
        from xiaoli_app.config_store import project_config
        from wechat_bot import strip_model_prefix
        proj = project_config({"providers": providers or []}, card)
        with self._model_lock:
            self.system_prompt = card.get("system_prompt", self.system_prompt)
            self.nickname = card.get("nickname") or self.nickname
            # 单模型化：卡只派生 chat_model（视觉/分类统一走 chat_model，
            # vision_model/classify_model/vision_temp/vision_max_tokens 已从卡删除）
            self.chat_model = strip_model_prefix(card.get("chat_model") or self.chat_model)
            self.chat_temperature = float(card.get("temperature", self.chat_temperature))
            self.chat_top_p = float(card.get("top_p", self.chat_top_p))
            self.max_history = int(card.get("max_history", self.max_history))
            self.api_url = proj.get("ai_api_url", self.api_url)
            self.api_key = proj.get("ai_api_key", self.api_key)
            self.vision_api_url = proj.get("vision_api_url", self.vision_api_url)
            self.vision_api_key = proj.get("vision_api_key", self.vision_api_key)
        logger.info(
            f"[角色卡] 已切换: {card.get('name')} "
            f"(chat={self.chat_model}, temp={self.chat_temperature})"
        )

    def _classify_task(self, text):
        if not self.task_enabled:
            return {"is_task": False, "task": ""}
        # 单模型化后任务判断统一用 chat_model（无独立 classify_model）
        return classify_task_with_llm(self.api_url, self.api_key, self.chat_model, text)

    def _vision_route(self, chat_name, sender, text, img_path=None, msg_id=None,
                      raw_message=None, attachments=None, is_group=None,
                      multi_sender=False):
        """vision-exp 单调用分流：任务判断 + 回复一次完成（替代两段式）。

        契约（基类 call_vision_api 由并行维度实现）：
          content 块 = [{"type": "text", "text": ...},
                        {"type": "image_url", "image_url": {"url": "data:image/..."}}]
          call_vision_api(content) 返回 dict 或 None：
            {"kind": "tool_call", "name": "dispatch_task",
             "arguments": '{"task": "..."}'} → 投递天枢（task 从 arguments JSON 解析）
            {"kind": "text", "content": "..."} → 直接回复（实质回复 → 占位归零）
            None / 异常                          → 返回 None，调用方降级

        返回 True = 已处理；None = 调用失败，交调用方降级。
        """
        # 方案二：人设由 call_vision_api 的 system 消息承载，这里不再重复注入
        # （避免同段人设同时出现在 system 与 user prompt 造成冗余/冲突）
        # sender 分流：对齐 call_chat_ai（wechat_bot.py）三分支——群聊带发送者
        # 名、多发送者只包群名前缀不重包 sender、私聊带发送者；否则模型不知
        # 道谁发的消息（历史缺陷：sender 与群聊名完全没进 prompt）。
        if is_group is None:
            is_group = bool(
                getattr(getattr(self, "wx", None), "_current_is_group", None)
                or is_group_chat(chat_name))
        if is_group:
            if multi_sender:
                decorated = f"群聊：{chat_name} {text}"
            elif sender:
                decorated = f"群聊：{chat_name} {sender}：{text}"
            else:
                decorated = f"群聊：{chat_name}：{text}"
        else:
            decorated = f"私聊 - {sender}：{text}" if sender else f"私聊：{text}"
        prompt = f"{VISION_ROUTE_PROMPT}\n\n用户消息：\n{decorated}"
        content = [{"type": "text", "text": prompt}]
        # 关键词记忆索引：用原始用户消息（非装饰串）匹配，命中的相关记忆
        # 由 call_vision_api 注入到历史之后
        related = self._match_related_memory(chat_name, text)
        if img_path:
            try:
                with open(img_path, "rb") as f:
                    img_b64 = base64.b64encode(f.read()).decode()
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                })
            except OSError as e:
                logger.error(f"[vision] 图片读取失败，忽略图片: {e}")
        try:
            resp = self.call_vision_api(content, chat_id=chat_name,
                                        related_memory=related)
        except Exception as e:
            logger.error(f"[vision] 调用异常: {e}")
            return None
        return self._apply_vision_result(
            chat_name, sender, resp, img_path=img_path, msg_id=msg_id,
            raw_message=raw_message, attachments=attachments, user_text=decorated)

    def _apply_vision_result(self, chat_name, sender, result, img_path=None,
                             msg_id=None, raw_message=None, attachments=None,
                             user_text=None):
        """vision 单调用结果分流核心（_vision_route 与 _route_vision_result 共用）。

        result: call_vision_api 的 dict 返回（{'kind':'tool_call'|'text',...}）
        或 None；user_text 为用户消息原文（tool_call JSON 解析失败降级用、
        text 分支记历史用；纯图路径为 None 时降级用 arguments 原文）。
        返回 True = 已处理；None = 调用失败/未识别，交调用方降级。
        """
        if not result:
            return None
        kind = result.get("kind")
        if kind == "tool_call" and result.get("name") == "set_reminder":
            # 定时提醒工具（与 dispatch_task 并列的节点 I 出口）
            return self._handle_set_reminder(chat_name, sender, result, user_text)
        if kind == "tool_call":
            # 契约：name 必须为 dispatch_task；task 从 arguments JSON 解析
            # （json.loads 后取 task 字段；解析失败降级用原文）
            if result.get("name") != "dispatch_task":
                logger.warning(f"[vision] 未知工具调用 name={result.get('name')!r}，降级")
                return None
            task_desc = user_text or ""
            raw_args = result.get("arguments") or "{}"
            try:
                args = json.loads(raw_args)
                task_desc = str(args.get("task") or user_text or "").strip()
            except (ValueError, TypeError):
                task_desc = (user_text or raw_args).strip()
            logger.info(f"[任务桥] vision 判定为任务: {task_desc[:60]}")
            self._add_history(chat_name, "assistant", "[任务已投递天枢处理]")
            atts = attachments if attachments is not None \
                else ([img_path] if img_path else None)
            self._dispatch_and_notify(
                chat_name, sender, task_desc,
                attachment_paths=atts,
                extra={"msg_id": msg_id, "raw_message": raw_message or task_desc},
            )
            return True
        if kind == "text":
            reply = str(result.get("content") or "").strip()
            if not reply:
                logger.warning("[vision] text 响应无回复文本，降级")
                return None
            logger.info(f"[vision] 判定非任务，直接回复: {reply[:60]}")
            if user_text:
                self._add_history(chat_name, "user", user_text)
            self._add_history(chat_name, "assistant", reply)
            self._send_text(reply, chat_name)
            return True
        logger.warning(f"[vision] 未知响应 kind={kind!r}，降级")
        return None

    def _route_vision_result(self, chat_name, sender, result, img_path=None):
        """纯图路径 hook 覆写：复用 _apply_vision_result 分流核心。

        纯图路径（_process_pure_image：捕获媒体 → vision 单调用 → 本 hook）
        的 vision 单
        调用结果分流：tool_call → 投递天枢（attachment_paths=[img_path]）；
        text → 直接回复（实质回复 → 占位归零）；None → 返回 None，调用方降级。
        无用户文字，JSON 解析失败降级用 arguments 原文。
        """
        return self._apply_vision_result(
            chat_name, sender, result, img_path=img_path)

    def _dispatch_and_notify(self, chat_name, sender, task_desc, attachment_paths=None, extra=None):
        """投递任务 → 微信告知"处理中" → 唤起天枢窗口。返回是否投递成功"""
        task_info = {
            "msg_id": (extra or {}).get("msg_id"),
            "sender": sender,
            "chat_name": chat_name,
            "is_group": bool(is_group_chat(chat_name)),
            "raw_message": (extra or {}).get("raw_message", ""),
            "task": task_desc,
        }
        for k, v in (extra or {}).items():
            if k not in task_info:
                task_info[k] = v
        task_id = dispatch_task(self.tasks_dir, task_info, attachment_paths)
        # 投递后幂等预授权天枢 CLI 读取任务目录（配置化授权 agent.permissions，
        # 常驻 CLI 重启后生效；dispatch 已确保 tasks_dir 存在，fail-closed 不跳过）
        try:
            from xiaoli_app import config_store as _cs
            _cs.grant_tasks_dir_to_tianshu(str(self.tasks_dir or "").strip())
        except Exception:
            pass
        self._send_text("收到任务啦，正在处理中，稍等一下哦～", chat_name, placeholder=True)
        # 唤起天枢：统一走 resolve_cli_window——tianshu_window_title 可能被
        # 旧版本污染为桌面端「天枢 · Tianshu」，直接 send 会激活桌面端窗口。
        # resolve 三级定位（手动配置→CLI 特征→启动后新增窗口），含桌面端排除。
        try:
            from xiaoli_app import setup as _setup
            # tianshu_workdir 必须传入——第 3 级自动启动 CLI 时 launch_tianshu 用它
            # 决定 cwd，缺失会落入 ~ 而非用户配置的工作目录。
            title, detail = _setup.resolve_cli_window({
                "tianshu_window_title": self.tianshu_window_title,
                "tianshu_workdir": getattr(self, "tianshu_workdir", ""),
                "tasks_dir": getattr(self, "tasks_dir", ""),  # launch cwd 优先用户选的工作间
            })
            if not title:
                logger.warning(f"[天枢] 未定位到 CLI 窗口（{detail}），任务已投递但未唤起")
                return True
            # 全自动模式由首启一次性引导的 /yes 保证（持久化，重启后仍生效，
            # 见 run_first_run_guide）——这里不再会话内切 YOLO（旧机制：
            # /permission yolo confirm 每次投递都要重发）。直接发触发指令。
            ok = send_trigger_to_window(title, self.tianshu_trigger_command, hold=2.0)
            if not ok:
                logger.error(f"[天枢] 唤起窗口失败: {title}，任务 {task_id} 保留在 {os.path.join(self.tasks_dir, task_id)}")
                self._send_text("天枢窗口没找到，不过任务已经记下了，处理完我会把结果发给你～", chat_name, placeholder=True)
        except Exception as e:
            logger.error(f"[天枢] 唤起异常: {e}，任务已投递")
        return True

    def _switch_to_chat(self, chat):
        """切到目标会话（发送前必须）。visual 后端（当前唯一可用通道）有
        _switch_chat（点击会话列表）。wxauto4 在微信 4.1.12 结构性失效，
        不作为可用后端——无切换能力时 fail-closed 返回 False，不假装能切。"""
        switcher = getattr(self.wx, "_switch_chat", None)
        if switcher is not None:
            return bool(switcher(chat))
        logger.warning(f"[切换] 后端无 _switch_chat 能力，无法切到会话 {chat!r}")
        return False

    def _send_file_clipboard(self, fpath, chat):
        """剪贴板 CF_HDROP + Ctrl+V 发送文件（SendFiles UI 自动化失效时的替代方案）。

        置前微信走 visual 后端 _foreground（Win32 SetForegroundWindow，不依赖
        UIA 窗口类名，微信 4.x 下可靠）；无 _foreground 能力时兜底不阻塞发送。
        """
        fg = getattr(self.wx, "_foreground", None)
        if fg is not None:
            fg()
        if chat:
            try:
                self._switch_to_chat(chat)
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f"[回传] 切换聊天窗口失败 {chat}: {e}")
        # 切会话后再确保微信前台（_switch_chat 内部操作可能改变前台）
        if fg is not None:
            fg()
        if not set_clipboard_files([fpath]):
            logger.error(f"[回传] 剪贴板设置文件失败: {fpath}")
            return False
        time.sleep(0.2)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.6)  # 等微信把文件加入待发送区
        pyautogui.press("enter")
        logger.info(f"[回传] 剪贴板方式已发送文件: {os.path.basename(fpath)}")
        return True

    def _poll_outbox(self):
        """封装 poll_outbox：从 task.json 取 chat_name 回传"""
        if not self.task_enabled:
            return []

        def deliver(task_dir, task_info, result):
            chat = task_info.get("chat_name", "")
            status = result.get("status", "success")
            reply_text = str(result.get("reply_text", "")).strip()
            if status == "failed":
                reply_text = reply_text or "任务处理失败了，不好意思呀～"
            self._sending_lock = True
            try:
                # 发送前显式切到目标聊天，防止轮询把窗口切走导致发错联系人
                if chat:
                    try:
                        self._switch_to_chat(chat)
                        time.sleep(0.3)
                    except Exception as e:
                        logger.error(f"[回传] 切换聊天窗口失败 {chat}: {e}")
                if reply_text:
                    self._send_text(reply_text, chat)
                for fname in (result.get("files") or []):
                    # 并发/重试场景：任务可能已被归档进 sent，文件需从 sent 下兜底定位
                    fpath = resolve_result_file(task_dir, os.path.join(self.tasks_dir, "sent"), fname)
                    if os.path.isfile(fpath):
                        sent = False
                        # 剪贴板 CF_HDROP 主用（wxauto SendFiles 在新版微信实测不可用）
                        try:
                            sent = self._send_file_clipboard(fpath, chat)
                        except Exception as e:
                            logger.warning(f"[回传] 剪贴板发送异常: {e}")
                        if not sent:
                            # 协议 send_file 兜底（visual 后端未实现返回 False）
                            try:
                                sent = self.wx.send_file(fpath, chat)
                                if sent:
                                    logger.info(f"[回传] send_file 发送: {fname}")
                            except Exception as e:
                                logger.error(f"[回传] send_file 失败: {e}")
                        if not sent:
                            logger.error(f"[回传] 文件发送失败，保留在任务目录: {fname}")
                        else:
                            # 登记回传的成果文件（源路径 + 主干+发送时刻）：目录扫描时排除，
                            # 防止把 bot 自己发出去的成果（含微信写入接收目录的副本）误当成"用户本次发送的文件"投递进下一轮任务附件
                            self._register_sent_back(fpath)
                            # 成果副本落盘后刷新快照：把发送产生的副本固化进已见集合，
                            # 下次目录扫描不作为增量出现（与 stem 排除构成双保险）。
                            # 基类快照能力（AgentBot 继承）；快照未启用/目录不可用时内部静默跳过。
                            try:
                                self._refresh_file_snapshot()
                            except Exception as e:
                                logger.warning(f"[回传] 快照刷新失败（不影响排除登记）: {e}")
                # 无论发送成败，任务结果都写入对话记忆（记忆记录的是任务产出，不是发送状态）
                self._remember_task_result(chat, result)
            finally:
                self._sending_lock = False

        try:
            return poll_outbox(self.tasks_dir, deliver)
        except Exception as e:
            logger.error(f"[回传] 轮询异常: {e}")
            return []

    def _remember_task_result(self, chat, result):
        """任务回传后把结果写入该聊天的对话记忆，让小漓后续能回忆任务成果。
        与投递时写入的 '[任务] ...' / '[任务已投递天枢处理]' 构成完整对话流"""
        if not chat:
            return
        status = result.get("status", "success")
        reply_text = str(result.get("reply_text", "")).strip()
        if status == "failed":
            reply_text = reply_text or "任务处理失败了，不好意思呀～"
        summary = f"[任务结果] {reply_text}" if reply_text else "[任务结果] 任务完成"
        files = result.get("files") or []
        if files:
            summary += "，成果文件: " + "、".join(str(f) for f in files)
        self._add_history(chat, "assistant", summary[:500])

    def _tick_poll_outbox(self):
        """按间隔轮询 outbox（不受消息 cooldown / 任务暂停限制）"""
        now = time.time()
        if now - self._last_poll_time >= self.tianshu_poll_interval:
            self._last_poll_time = now
            self._poll_outbox()

    def _process_file_with_instruction(self, chat_name, sender, filepath, filename, user_instruction, extra_attachments=None, multi_sender=False):
        """根据用户指令处理文件：vision-exp 单调用判断任务 → 天枢投递 或 原文件识别。

        任务分支只投文件本体（天枢 CLI 自行读附件），不提取文件文字、不写
        file_text；只有非任务分支（把文件内容喂给 AI 生成回复）才提取文字。
        vision 调用失败（None）时降级回退原两段式（_classify_task + 文件识别）。
        """
        is_group = is_group_chat(chat_name)
        instruction = user_instruction
        if is_group:
            at_tag = f"@{self.nickname}"
            instruction = instruction.replace(at_tag, "").strip()
        if not instruction:
            instruction = user_instruction.strip()

        # 任务判断输入 = 文件名 + 处理要求
        # （LLM 需知道「处理的对象」才能判断是否动手类任务；仅指令如
        # '把这个做成网页' 缺少对象，单独看会被误判闲聊）：
        #   '[文件]部门简介+纳新宣传(6).docx 把这个做成一个赛博朋克风格的网页'
        classify_input = f"[文件]{filename} {instruction}"
        if self.task_enabled:
            resp = self._vision_route(
                chat_name, sender, classify_input,
                msg_id=None, raw_message=filename,
                attachments=[filepath] + (extra_attachments or []),
                is_group=is_group, multi_sender=multi_sender,
            )
            if resp is not None:
                return True
            # vision 降级（None）：回退原两段式任务判断
            cls = self._classify_task(classify_input)
            if cls["is_task"]:
                logger.info(f"[任务桥] 文件+指令判定为任务: {cls['task'][:60]}")
                self._add_history(chat_name, "assistant", "[任务已投递天枢处理]")
                atts = [filepath] + (extra_attachments or [])
                self._dispatch_and_notify(
                    chat_name, sender, cls["task"],
                    attachment_paths=atts,
                    extra={"msg_id": None, "raw_message": filename},
                )
                return True

        # 非任务 → 此时才提取文件文字，连同用户指令一起喂给 AI
        logger.info(f"[文件] 非任务，走文件识别流程，附带用户指令: {instruction[:60]}")
        file_content = self._extract_file_text(filepath)
        if file_content is None:
            self._send_text(f"收到文件「{filename}」，但这个格式我看不懂呢～", chat_name)
            return True
        self._add_history(chat_name, "assistant", f"[文件内容: {filename}] {file_content}")
        refine_prompt = (
            f"用户发来一个文件（{filename}），内容如下：\n\n"
            f"{file_content}\n\n"
            f"用户对文件处理的要求是：{instruction}\n\n"
            f"请根据文件内容和用户的要求，以{self.nickname}的身份回复用户。"
        )
        final_reply = self.call_chat_ai(chat_name, refine_prompt, sender_name=sender, is_group=is_group, multi_sender=multi_sender)
        self._send_text(final_reply, chat_name)
        return True

    def _register_sent_back(self, fpath):
        """登记一条已回传的成果文件，供目录扫描排除。
        微信 PC 版会把 bot 发出的文件也写入接收目录（msg\\file 下，重名加 (1)(2) 后缀）——
        仅登记源路径（sent 目录下）不够，接收目录里的副本路径永远匹配不上。
        因此额外登记「文件名主干 → 发送时刻」，扫描时按主干匹配 + ctime 落在发送时刻附近排除副本。"""
        try:
            mtime = os.path.getmtime(fpath)
        except OSError:
            mtime = None
        self._sent_back_files[fpath] = mtime
        stem = re.sub(r"\(\d+\)$", "", os.path.splitext(os.path.basename(fpath))[0])
        self._sent_back_stems[stem] = time.time()
        # 持久化：bot 重启后排除仍生效（否则重启后历史成果副本会再次被误当用户文件）
        try:
            with open(self._sent_back_stems_file, "w", encoding="utf-8") as f:
                json.dump(self._sent_back_stems, f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[回传] 持久化成果登记失败: {e}")


    def _drain_reminders(self):
        """消费到期提醒队列（主循环节点）。

        发送走 wx.send_text 直发——旁路 _send_text 的占位计数（定时消息
        不得破坏 skip_bot/N[chat] 语义：占位挂起时定时消息若归零计数，
        占位会被当成实质回复，用户其后的消息将被漏读）。发送失败也确认
        出队（mark_fired），避免死循环重发刷屏。"""
        while True:
            try:
                r = self._reminder_queue.get_nowait()
            except queue.Empty:
                break
            rid = r.get("id")
            try:
                now = time.time()
                if now - (r.get("fire_at") or now) > GRACE_SECONDS:
                    logger.warning(f"[定时] 提醒 {rid} 超宽限（暂停/滞留），按错过处理")
                    continue
                self.wx.send_text(r.get("chat"), f"【定时提醒】{r.get('content')}")
                logger.info(f"[定时] 已发送 -> {r.get('chat')}: {str(r.get('content'))[:40]}")
            except Exception as e:
                logger.error(f"[定时] 发送失败 {rid}: {e}")
            finally:
                self.reminders.mark_fired(rid)

    def _handle_set_reminder(self, chat_name, sender, result, user_text):
        """节点 I 的 set_reminder 工具分支：解析时间/内容 → 入库 → 角色内确认。

        时间解析失败 / 已过 / reminders 不可用 → 返回 None 降级普通聊天。"""
        try:
            args = json.loads(result.get("arguments") or "{}")
        except (ValueError, TypeError):
            return None
        raw_time = str(args.get("time") or "").strip()
        content = str(args.get("content") or "").strip()
        repeat = str(args.get("repeat") or "once").strip()
        if not raw_time or not content:
            return None
        try:
            fire_at = time.mktime(time.strptime(raw_time, "%Y-%m-%d %H:%M"))
        except (ValueError, TypeError):
            logger.info(f"[定时] 时间解析失败: {raw_time!r}，降级聊天")
            return None
        if fire_at <= time.time():
            logger.info(f"[定时] 模型给的触发时间已过: {raw_time}，降级聊天")
            return None
        if getattr(self, "reminders", None) is None:
            return None
        self.reminders.add(chat_name, content, fire_at, repeat)
        if user_text:
            self._add_history(chat_name, "user", user_text)
        self._add_history(chat_name, "assistant", f"[已设定时提醒 {raw_time} {content}]")
        reply = f"记住啦，{raw_time} 我会提醒你「{content}」(๑•̀ㅁ•́๑)"
        logger.info(f"[定时] 已创建提醒 -> {chat_name} @ {raw_time}: {content[:40]}")
        self._send_text(reply, chat_name)
        return True

    def _handle_text(self, chat_name, sender, content, msg_id=None, multi_sender=False):
        """文本消息统一处理：vision-exp 单调用判断+回复 → 天枢投递 或 普通聊天。

        附件只由文件消息路径投递（_process_file_with_instruction，用户确实
        发了文件时才带）。纯文字任务不带任何附件——历史缺陷：文本路径无条件
        找接收目录"最新"文件，用户没发文件时把无关旧文件投给 agent 造成误判。
        vision 调用失败（None）时降级回退普通聊天（API 失败默认非任务，与现状一致）。
        """
        is_group = getattr(self.wx, "_current_is_group", None)
        if is_group is None:
            is_group = is_group_chat(chat_name)
        if is_group:
            at_tag = f"@{self.nickname}"
            content = content.replace(at_tag, "").strip()
            if not content:
                content = "你好呀～"  # 与基类 process_new_messages 群聊空内容文案一致
        question = content.strip()
        logger.info(f"[MSG] [{chat_name}] {sender}: {question[:80]}")
        if self.task_enabled:
            resp = self._vision_route(
                chat_name, sender, question,
                msg_id=msg_id, raw_message=content,
                is_group=is_group, multi_sender=multi_sender)
            if resp is not None:
                return True
            # vision 降级（None）：API 失败默认非任务，回退普通聊天（与现状一致）
        reply = self.call_chat_ai(chat_name, question, sender_name=sender, is_group=is_group, multi_sender=multi_sender)
        self._send_text(reply, chat_name)
        return True

    # ---------- 消息处理（改造） ----------

    def process_new_messages(self):
        self._flush_memory_if_due()
        if self.paused:
            return
        if self._sending_lock:
            # 成果回传期间彻底暂停：不轮询任务目录、不遍历会话，
            # 防止 ChatWith 切走窗口打断文件发送/发错联系人
            return
        # 定时消息消费节点：必须在主循环内发送（窗口互斥）；暂停态走到
        # 不了这里（上面 return），错过的提醒由宽限/滚动逻辑兜底
        if getattr(self, "_reminder_queue", None) is not None:
            self._drain_reminders()
        # 任务成果轮询不受 cooldown / 任务暂停限制，必须最先执行
        self._tick_poll_outbox()
        resume, self._task_was_active, self._task_end_time = should_resume_listen(
            has_active_tasks(self.tasks_dir),
            self._task_was_active,
            self._task_end_time,
            time.time(),
            self._listen_hold_seconds,
        )
        if not resume:
            # 天枢任务进行中或刚完成（缓冲期内）：暂停消息监听，
            # 缓冲期满后自动恢复，避免 ChatWith 切走窗口打断文件发送
            return
        if time.time() - self.last_reply_time < self.cooldown:
            return
        try:
            # A: 红圈检测
            if hasattr(self.wx, "iter_unread_sessions"):
                sessions = list(self.wx.iter_unread_sessions())
            else:
                sessions = list(self.wx.iter_sessions())
            if not sessions:
                return
            for chat_name in sessions:
                if not chat_name:
                    continue
                # 失败退避：上次处理未产出回复（红圈滞留）的会话暂跳过
                if time.time() - self._chat_fail_at.get(chat_name, 0.0) < self._fail_backoff:
                    logger.debug(f"[退避] {chat_name} 上次处理失败未满 {self._fail_backoff:.0f}s，跳过")
                    continue
                logger.info(f"🔔 发现新消息：{chat_name}")
                try:
                    handled = self._handle_unread_session(chat_name)
                except Exception as e:
                    logger.error(f"处理会话 {chat_name} 异常: {e}\n{traceback.format_exc()}")
                    handled = False
                if handled:
                    self._chat_fail_at.pop(chat_name, None)  # 处理成功，解除退避
                    self.last_reply_time = time.time()
                    return  # 每轮只处理一个会话，回复后回到红点监听
                self._chat_fail_at[chat_name] = time.time()
        except Exception as e:
            logger.error(f"Message processing error: {e}\n{traceback.format_exc()}")

    def _handle_unread_session(self, chat_name):
        """处理一个未读会话：窗口边界（bot 最后回复之后的对方消息）+ 分类分发。

        返回 True = 已处理（回复/投递）；False = 无待处理（回到红点监听）。
        """
        at_tag = f"@{self.nickname}"

        def _window_msgs(win):
            # assume_switched：analyze_window 刚完成切换+读标题（同一处理
            # 事件），跳过重切与标题重读——事件热路径省一次点击两次 OCR
            msgs = self.wx.get_messages(chat_name, assume_switched=True)
            return [
                m for m in msgs
                if m.sender not in (None, "self", self.nickname)
                and (win.get("bot_bottom") is None
                     or _looks_like_file_text(m.content)
                     or (m.y is not None and m.y >= win.get("bot_bottom")))
            ]

        # D/R: 截图 + 气泡/媒体分析（无 OCR）。skip_bot：跳过最近 N 条 bot
        # 占位回复（_pending_placeholders，占位发送时 +1、实质回复归零），
        # 占位"正在处理中"不顶掉用户的新消息。
        win = self.wx.analyze_window(
            chat_name, skip_bot=self._pending_placeholders.get(chat_name, 0))
        if not (win.get("has_other") or win.get("has_text") or win.get("has_media")):
            logger.info(f"[跳过] {chat_name} 窗口空（bot 已回复或无对方消息）")
            return False
        # 先读一次窗口内文字，判断是否有文件（OCR 扩展名）。文件卡片在视觉层
        # 被判普通气泡（has_text 而非 has_media），但文件下载/渲染同样需防抖。
        # 注意：_window_msgs → get_messages 内部 read_title 会刷新
        # _current_is_group 为本次会话权威值——群聊判定必须在这之后读取，
        # 否则私聊被上一轮群聊残留误判为群聊（实测日志「私聊王文生被判
        # 群聊消息未 @小漓」；analyze_window 本身无 read_title 不刷新）。
        window_msgs = _window_msgs(win)
        has_file_initial = any(_looks_like_file_text(m.content) for m in window_msgs)
        # F/H: 有图片（媒体）或有文件 → sleep 10s 防话没说完/文件没下载完，再分析
        if win.get("has_media") or has_file_initial:
            time.sleep(10)
            win = self.wx.analyze_window(
                chat_name, skip_bot=self._pending_placeholders.get(chat_name, 0))
            if not (win.get("has_other") or win.get("has_text") or win.get("has_media")):
                return False
            window_msgs = _window_msgs(win)
        # 会话身份以标题区读取为主（用户定案）：列表条带名只是锚点，标题
        # 解析出的权威名统一用于记忆/回复/日志——否则条带 OCR 漏字产生的
        # 残缺名（真机 '强盗”集'）会落进 memory 键
        canonical = (getattr(self.wx, "_current_title", "") or "").strip()
        if canonical and canonical != chat_name:
            logger.info(f"[身份] 会话名以标题为准: {chat_name!r} -> {canonical!r}")
            chat_name = canonical
        # 群聊判定（本次会话权威值）：联合 OCR 的标题解析已在 _window_msgs
        # 内刷新 _current_is_group（标题不再在 analyze 阶段单独读——事件内
        # OCR 两次封顶）；缺失时回退名称启发式。
        is_group = getattr(self.wx, "_current_is_group", None)
        if is_group is None:
            is_group = is_group_chat(chat_name)
        # 群聊 @ 过滤：只有 @小漓 的消息才处理
        if is_group:
            window_msgs = [m for m in window_msgs if at_tag in m.content]
            if not window_msgs:
                logger.info(f"[跳过] {chat_name} 群聊消息未 {at_tag}")
                # 无 @ 跳过回复 = 全程零点击，而微信只在会话获得交互时标记
                # 已读——红圈原样滞留 → 8s 退避后无限循环重处理（其余流程
                # 发回复时点输入框顺带清圈）。点一次输入框标记已读防循环
                mark_read = getattr(self.wx, "mark_session_read", None)
                if mark_read is not None:
                    try:
                        mark_read()
                    except Exception as e:
                        logger.warning(f"[已读] 标记已读失败: {e}")
                return False
        sender = window_msgs[-1].sender if window_msgs else chat_name
        # 文件识别：OCR 文本含文件扩展名
        file_text = next(
            (m.content.strip() for m in window_msgs if _looks_like_file_text(m.content)),
            None)
        # 文字部分（排除文件名的 OCR 文本）
        text_candidates = [
            m for m in window_msgs
            if m.content.strip() and not _looks_like_file_text(m.content)
        ]
        if len(text_candidates) > 1:
            # 多发送者合并：每条带各自发送者名（群聊名兜底，不整批只带最后一条）
            multi_sender = True
            text_parts = [
                f"{m.sender or chat_name}：{m.content.strip()}"
                for m in text_candidates
            ]
        else:
            multi_sender = False
            text_parts = [m.content.strip() for m in text_candidates]
        text_content = "\n".join(text_parts)
        has_media = bool(win.get("has_media"))
        # sender 关联：该发送者之前发过文件、现在发来文字指令
        if sender in self._pending_files and text_content:
            pending = self._pending_files.pop(sender)
            logger.info(f"[文件] {sender} 发来处理指令，关联到待处理文件 {pending['filename']}")
            return self._process_file_with_instruction(
                pending["chat_name"], sender, pending["file_path"],
                pending["filename"], text_content, multi_sender=multi_sender)
        # ============ 分类分发 ============
        if file_text:
            # 文件（可能同时有图片）：有图片则截图一并投递，不再被文件分支吞掉
            extra_attachments = []
            if has_media:
                img_path = self._capture_latest_image(chat_name)
                if img_path:
                    extra_attachments.append(img_path)
            logger.info(f"📁 判断为文件消息：{chat_name}（{file_text[:40]}）")
            return self._handle_file_message(
                chat_name, sender, file_text, text_content,
                extra_attachments=extra_attachments, multi_sender=multi_sender)
        if has_media and text_content:
            # 图片 + 文字 → 文字 LLM 判任务，任务投递（图截图），非任务组装
            logger.info(f"🖼💬 判断为图片+文字消息：{chat_name}")
            return self._handle_image_with_text(
                chat_name, sender, text_content, multi_sender=multi_sender)
        if has_media and not text_content:
            # 无文字 + 有媒体（图片/视频/表情统一当图片）→ Ctrl+C 图片路径
            logger.info(f"🖼 判断为图片消息：{chat_name}")
            if self._process_pure_image(chat_name):
                return True
            # 失败必须回一句：发送点输入框顺带清红圈，防滞留循环
            self._send_text("图片识别失败了，可能是什么地方出了问题呀～", chat_name)
            return True
        if text_content:
            # 纯文字 → 任务判断 + 聊天
            logger.info(f"💬 判断为文字消息：{chat_name} {text_content[:40]!r}")
            return self._handle_text(
                chat_name, sender, text_content, None, multi_sender=multi_sender)
        return False

    def _handle_file_message(self, chat_name, sender, file_text, text_content, extra_attachments=None, multi_sender=False):
        """文件消息处理：按显示名/快照增量定位 → 回复收到并询问。

        文字提取延迟到真正需要时（非任务分支喂 AI）才做，任务分支只投
        文件本体——避免对任务文件做多余的 _extract_file_text。
        """
        file_dir = self.file_storage_path
        if not file_dir or not os.path.isdir(file_dir):
            self._send_text("文件下载失败，请重试～", chat_name)
            return True
        # 拆出干净文件名（OCR 可能读到 "文件名.docx 20.1K W" 含大小+图标字符，
        # 20.1K 的小数点会坑 splitext——先 token 拆出真实文件名）
        clean_name = _extract_file_name_token(file_text) or file_text
        file_path = self._find_file_by_display_name(clean_name)
        if not file_path:
            time.sleep(3)
            file_path = self._find_user_file(file_dir)
        if not file_path:
            self._send_text("文件下载失败，请重试～", chat_name)
            return True
        filename = os.path.basename(file_path)
        if text_content:
            # 文件 + 伴随文字 → 按指令处理（任务判断 or 文件识别附带指令）
            return self._process_file_with_instruction(
                chat_name, sender, file_path, filename, text_content,
                extra_attachments=extra_attachments, multi_sender=multi_sender)
        # 无伴随文字 → 回复收到 + 记录 sender 关联（不停摆，等该 sender 后续指令）
        self._pending_files[sender] = {
            "chat_name": chat_name,
            "file_path": file_path,
            "filename": filename,
        }
        self._send_text("文件已收到～请告诉我需要怎么处理呢？", chat_name)
        return True

    def _handle_image_with_text(self, chat_name, sender, text_content, multi_sender=False):
        """图片 + 文字：vision-exp 单调用（图 + 文字一次判断 + 回复）。

        任务 → 图截图 + 文字投递（tool_call 不发送回复文本）；非任务 → 直接
        回复（不再两段式：GLM-4V 描述转述 + 主模型二次回复）。vision 调用
        失败（None）→ 降级回退纯文字处理（保持现状）。
        """
        if self.task_enabled:
            img_path = self._capture_latest_image(chat_name)
            resp = self._vision_route(
                chat_name, sender, text_content, img_path=img_path,
                msg_id=None, raw_message=text_content, multi_sender=multi_sender)
            if resp is not None:
                return True
        # 降级：vision 失败或任务桥关闭 → 回退纯文字处理
        return self._handle_text(
            chat_name, sender, text_content, None, multi_sender=multi_sender)

class TianshuController(Controller):
    HELP = {
        **Controller.HELP,
        "tianshu-window": "重新选择天枢 CLI 窗口",
        "task-status": "查看任务流转状态（等待/完成/归档）",
    }

    def _register_commands(self):
        """继承基类全部命令，扩展天枢任务桥命令（历史缺陷：整段复制
        Controller._listen 的 if/elif 链，新增命令全靠复制粘贴）。"""
        super()._register_commands()
        self._commands.update({
            "tianshu-window": self._cmd_tianshu_window,
            "task-status": self._cmd_task_status,
        })

    def _cmd_tianshu_window(self, cmd):
        was_paused = self.bot.paused
        self.bot.paused = True
        logger.info("⏸️  已暂停，正在列出窗口...")
        title = self.bot._select_window()
        if title:
            logger.info(f"🔄 天枢窗口已切换为：{title}")
        else:
            logger.info("已取消")
        self.bot.paused = was_paused
        if not self.bot.paused:
            logger.info("▶️  已自动恢复回复")

    def _cmd_task_status(self, cmd):
        self._show_task_status()


    def _show_task_status(self):
        """任务状态展示（与 GUI 任务页共用 scan_task_status）。"""
        entries, waiting, done, archived = scan_task_status(self.bot.tasks_dir)
        if not os.path.isdir(self.bot.tasks_dir):
            print("任务目录不存在")
            return
        for name, state, desc, _mtime in entries:
            tag = "✅ 天枢已完成" if state == "done" else "⏳ 天枢处理中"
            print(f"  - {name} [{tag}] {desc}")
        if archived:
            print(f"已归档: {archived} 个任务")
        print(f"统计: {waiting} 等待中 / {done} 待回传")


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

        # ---- T12: 回传成果文件（含微信写入接收目录的副本）不被误当用户新发的文件 ----
        # 上一轮成果：源文件在任务目录（不在接收目录），bot 发送时登记 stem+发送时刻
        src_out = os.path.join(tmp, "成果报告.html")
        with open(src_out, "w", encoding="utf-8") as f:
            f.write("result")

        def make_bot(dirpath):
            b = AgentBot.__new__(AgentBot)
            b._sent_back_files = {}
            b._sent_back_stems = {}
            b._sent_back_stems_file = None
            b._file_snapshot = {"__baseline__": [0, 0]}  # 非空哨兵：模拟已有历史快照（空快照=首启，_find_user_file 只建基线返回 None）
            b.file_storage_path = dirpath
            b.nickname = "小漓"
            return b

        # 场景 A：只有成果副本（目标文件未下载成功）→ 返回 None，不误选副本投递
        dir_a = os.path.join(tmp, "recv_a")
        os.makedirs(dir_a)
        bot_a = make_bot(dir_a)
        bot_a._register_sent_back(src_out)
        time.sleep(0.1)  # 确保副本 ctime 晚于登记时刻（发送时刻）
        shutil.copy2(src_out, os.path.join(dir_a, "成果报告.html"))
        shutil.copy2(src_out, os.path.join(dir_a, "成果报告(1).html"))
        got_a = bot_a._find_user_file(dir_a)
        check("T12 仅成果副本时返回 None（不投递错文件）", got_a is None, str(got_a))

        # 场景 B：副本 + 目标文件，且副本 mtime 比目标文件新（真实时序：任务完成晚于用户发文件）
        # → 登记排除副本后必须选中目标文件（修复生效的判定）
        dir_b = os.path.join(tmp, "recv_b")
        os.makedirs(dir_b)
        bot_b = make_bot(dir_b)
        bot_b._register_sent_back(src_out)
        time.sleep(0.1)
        copy_b = os.path.join(dir_b, "成果报告(1).html")
        shutil.copy2(src_out, copy_b)
        os.utime(copy_b, (time.time() + 10, time.time() + 10))  # 副本 mtime 最新（模拟任务完成时刻）
        target_b = os.path.join(dir_b, "报名表.xlsx")
        with open(target_b, "w", encoding="utf-8") as f:
            f.write("target")
        got_b = bot_b._find_user_file(dir_b)
        check("T12 排除成果副本，选中用户目标文件", got_b == target_b, str(got_b))

        # 场景 C：对照——未登记 stem → 成果副本被误选（登记必要性的反证）。
        # 先建 target 再写副本：副本 ctime 严格晚于 target（模拟微信把成果副本
        # 写入接收目录在用户文件之后）+ 副本 mtime 最新（+10s）→ (dup, ctime, mtime)
        # 排序下副本必胜。已登记时（场景 B）stem 排除救回 target。
        dir_c = os.path.join(tmp, "recv_c")
        os.makedirs(dir_c)
        bot_c = make_bot(dir_c)
        target_c = os.path.join(dir_c, "报名表.xlsx")
        with open(target_c, "w", encoding="utf-8") as f:
            f.write("target")
        shutil.copy2(src_out, os.path.join(dir_c, "成果报告.html"))
        os.utime(os.path.join(dir_c, "成果报告.html"), (time.time() + 10, time.time() + 10))
        got_c = bot_c._find_user_file(dir_c)
        check("T12 对照：未登记时成果副本被误选（bug 复现）",
              got_c == os.path.join(dir_c, "成果报告.html"), str(got_c))

        # 场景 D：发送已久（>60s）后用户新下载的同名文件不被误杀（双向窗口边界）
        dir_d = os.path.join(tmp, "recv_d")
        os.makedirs(dir_d)
        bot_d = make_bot(dir_d)
        bot_d._sent_back_stems["成果报告"] = time.time() - 3600  # 1 小时前发送过同名成果
        late_d = os.path.join(dir_d, "成果报告(2).html")
        with open(late_d, "w", encoding="utf-8") as f:
            f.write("late")
        got_d = bot_d._find_user_file(dir_d)
        check("T12 发送已久后新下载的同名文件不被误杀", got_d == late_d, str(got_d))

        # ---- T13: 按文件消息显示名精确定位（微信 4.0 目录命名 <hash>_<msgid>_m_<原名>）----
        dir_e = os.path.join(tmp, "recv_e")
        os.makedirs(dir_e)
        bot_e = make_bot(dir_e)
        # 用户最新下载的文件：时间戳是旧的（微信保留源时间戳），目录名带 hash 前缀 + (4) 重名后缀
        user_file = os.path.join(
            dir_e,
            "c4fc4da4edbb4f3b87cfbe02e564a2d3_3252231687582084619_m_企业账户-投递人数2078758432532791296(4).txt")
        with open(user_file, "w", encoding="utf-8") as f:
            f.write("x")
        old = time.time() - 7 * 86400  # 7 天前的时间戳
        os.utime(user_file, (old, old))
        # 上一轮成果副本：时间戳最新（今天），但文件名不同 → 不应命中
        result_file = os.path.join(dir_e, "贪吃蛇.html")
        with open(result_file, "w", encoding="utf-8") as f:
            f.write("result")
        # 提取显示名（真实 content 格式：'文件\n<名>\n<大小>\n微信电脑版'）
        from types import SimpleNamespace
        fake_msg = SimpleNamespace(content="文件\n企业账户-投递人数2078758432532791296.txt\n123KB\n微信电脑版",
                                   repattern=r"^文件\n([^\n]+)\n(\d+(\.\d+)?)(B|KB|MB|GB|TB)\n微信电脑版$")
        display = bot_e._extract_file_display_name(fake_msg)
        check("T13 从 content 提取显示名", display == "企业账户-投递人数2078758432532791296.txt", str(display))
        got_e = bot_e._find_file_by_display_name(display)
        check("T13 按显示名定位用户文件（不中成果副本）", got_e == user_file, str(got_e))

        # ---- T14: 成果登记持久化（重启后仍排除）+ 文件名前缀变体排除 ----
        # 前缀变体：登记"成果报告"，目录里"成果报告-美化版"（ctime 在发送时刻附近）也应被排除
        dir_h = os.path.join(tmp, "recv_h")
        os.makedirs(dir_h)
        bot_h = make_bot(dir_h)
        src_h = os.path.join(tmp, "成果报告.html")
        with open(src_h, "w", encoding="utf-8") as f:
            f.write("r")
        bot_h._register_sent_back(src_h)
        time.sleep(0.1)
        variant = os.path.join(dir_h, "成果报告-美化版.html")
        shutil.copy2(src_h, variant)  # ctime = 现在 ≈ 发送时刻 → 前缀匹配 + 窗口内 → 排除
        target_h = os.path.join(dir_h, "报名表.xlsx")
        with open(target_h, "w", encoding="utf-8") as f:
            f.write("t")
        got_h = bot_h._find_user_file(dir_h)
        check("T14 前缀变体被排除，选中目标文件", got_h == target_h, str(got_h))
        # 持久化 round-trip：新实例从文件加载登记（模拟 bot 重启）→ 排除仍生效
        stems_file = os.path.join(tmp, "stems.json")
        bot_h._sent_back_stems_file = stems_file
        bot_h._register_sent_back(src_h)  # 触发写盘
        bot_h2 = make_bot(dir_h)
        bot_h2._sent_back_stems_file = stems_file
        bot_h2._load_sent_back_stems()
        check("T14 重启后加载持久化登记", bot_h2._sent_back_stems.get("成果报告") is not None,
              str(bot_h2._sent_back_stems))
        got_h2 = bot_h2._find_user_file(dir_h)
        check("T14 重启后前缀变体仍被排除", got_h2 == target_h, str(got_h2))

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


# =====================================================================
# 入口
# =====================================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="小漓合并版（聊天 + 天枢任务桥）")
    parser.add_argument("--test", action="store_true", help="运行自检（不连微信、不唤起窗口）")
    parser.add_argument("--run", action="store_true", help="启动微信机器人（合并版）")
    args = parser.parse_args()

    if args.test:
        sys.exit(run_self_test())
    elif args.run:
        if not acquire_single_instance():
            logger.error("检测到已有小漓实例在运行（双开会导致任务并发处理冲突、文件回传失败），本实例退出")
            sys.exit(1)
        cfg = load_merged_config("config.json")
        bot = AgentBot(cfg)
        # 首次启动：选择天枢 CLI 窗口
        if cfg.get("task_enabled") and not cfg.get("tianshu_window_title"):
            logger.info("首次启动：请选择天枢 CLI 窗口")
            bot._select_window()
        ctrl = TianshuController(bot)
        ctrl.start()
        # quit 命令 → stop_event → bot.run 优雅退出（节流窗口内记忆 flush 落盘）
        bot.run(stop_event=ctrl.stop_event)
    else:
        print("用法: python xiaoli_bot.py --test | --run")
