# -*- coding: utf-8 -*-
"""小漓合并版：普通微信聊天 + 天枢任务桥

收到消息 → LLM 判断是否任务：
  - 闲聊/提问 → 原聊天路线（call_chat_ai 回复）
  - 任务请求（如"根据文档做一个网站"）→ 投递 D:\\工作间\\wxauto\\<任务id>\\ + 唤起天枢 CLI 窗口输入"开始处理"
天枢完成任务后写 result.json + 成果文件 → bot 轮询回传（文本 SendMsg + 文件 SendFiles）→ 归档 sent\\

运行：python xiaoli_bot.py --run     自检：python xiaoli_bot.py --test
"""
import json, os, sys, time, re, tempfile, subprocess, logging, traceback, shutil, uuid, struct
import requests as req
import pyautogui
from wechat_bot import WeChatBot, Controller, load_config, logger
from wxauto4.msgs.mtype import ImageMessage, FileMessage, TimeMessage, SystemMessage

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
    "file_send_method": "clipboard",   # 成果文件发送方式: clipboard(剪贴板粘贴,主用) | wxauto(SendFiles,兜底)
    "listen_hold_seconds": 2,   # 任务完成后延迟恢复消息监听的秒数（缓冲文件发送，防止切窗打断）
}


def load_merged_config(path="config.json"):
    """wechat_bot.load_config + 补齐 task_* 默认值；旧 config 自动补默认并写回"""
    cfg = load_config(path)
    changed = False
    for k, v in TASK_DEFAULTS.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=4)
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
        return True
    except Exception as e:
        logger.error(f"[窗口] 激活失败: {e}")
        return False


def send_trigger_to_window(title, command, hold=0.5):
    """激活窗口 → 剪贴板粘贴指令 → 回车。返回 bool"""
    if not activate_window_by_title(title):
        return False
    time.sleep(hold)
    if not clipboard_set_text(command):
        return False
    time.sleep(0.1)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.2)
    pyautogui.press("enter")
    logger.info(f"[天枢] 已向窗口「{title}」发送指令: {command}")
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
        try:
            # 先归档（移入 sent），再发送：发送时文件路径稳定在 sent\<任务id>\ 下，
            # 避免并发进程（如天枢侧移动/归档）在发送期间改动文件位置导致「文件路径不存在」
            if os.path.isdir(task_dir):
                try:
                    shutil.move(task_dir, archived_dir)
                except Exception as move_e:
                    if not os.path.isdir(archived_dir):
                        raise move_e
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


def has_active_tasks(tasks_dir):
    """任务目录顶层是否存在尚未归档的任务（天枢处理中 / 已完成待回传 / 回传失败重试中）。
    只有含 task.json 的子目录才算任务（排除 sent\\ 与 attachments 等非任务目录）。
    全部任务归档进 sent\\ 后返回 False —— 这是恢复消息监听的判据"""
    if not os.path.isdir(tasks_dir):
        return False
    for name in os.listdir(tasks_dir):
        task_dir = os.path.join(tasks_dir, name)
        if os.path.isdir(task_dir) and name != "sent":
            if os.path.isfile(os.path.join(task_dir, "task.json")):
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


def acquire_single_instance(name="XiaoLiBot_SingleInstance"):
    """Windows 会话级互斥体：检测是否已有 bot 实例在运行。
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

class AgentBot(WeChatBot):
    """小漓合并版：继承原 WeChatBot（聊天/图片/文件识别），叠加天枢任务桥"""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.task_enabled = cfg.get("task_enabled", True)
        self.tasks_dir = cfg.get("tasks_dir", r"D:\工作间\wxauto")
        self.tianshu_window_title = cfg.get("tianshu_window_title", "")
        self.tianshu_trigger_command = cfg.get("tianshu_trigger_command", "开始处理")
        self.tianshu_poll_interval = cfg.get("tianshu_poll_interval", 5)
        self._last_poll_time = 0
        self._sending_lock = False  # 成果回传期间置 True，暂停消息轮询防发错联系人
        self.file_send_method = cfg.get("file_send_method", "clipboard")
        self._listen_hold_seconds = cfg.get("listen_hold_seconds", 10)
        self._task_was_active = False  # 是否曾因任务暂停监听（用于任务完成后的缓冲期）
        self._task_end_time = None     # 最后一次任务完成（归档）的时刻
        os.makedirs(self.tasks_dir, exist_ok=True)
        # 是否暂停消息监听由 has_active_tasks 每次实时判断，不保存粘滞状态
        self.dispatched_msg_ids = set()
        self._load_dispatched_ids()
        self._pending_files = {}  # chat_name -> {sender} 群聊文件等待用户指令
        self._sent_back_files = {}  # 回传成果文件 绝对路径 -> mtime（目录扫描时排除，防误当用户发送的文件）
        # 回传成果文件名主干 -> 发送时刻（排除微信写入接收目录的成果副本）。
        # 持久化到 tasks_dir 下，bot 重启后仍生效（否则重启后历史成果副本会再次被误当用户文件）
        self._sent_back_stems = {}
        self._sent_back_stems_file = os.path.join(self.tasks_dir, "sent_back_stems.json")
        self._load_sent_back_stems()
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

    def _load_dispatched_ids(self):
        """启动时扫描已有任务目录，收集已投递的 msg_id（防重启重复投递）"""
        if not os.path.isdir(self.tasks_dir):
            return
        for name in os.listdir(self.tasks_dir):
            task_dir = os.path.join(self.tasks_dir, name)
            if not os.path.isdir(task_dir):
                continue
            tj = os.path.join(task_dir, "task.json")
            if os.path.isfile(tj):
                try:
                    with open(tj, "r", encoding="utf-8") as f:
                        info = json.load(f)
                    mid = info.get("msg_id")
                    if mid:
                        self.dispatched_msg_ids.add(mid)
                except Exception:
                    continue
        logger.info(f"[任务桥] 已加载 {len(self.dispatched_msg_ids)} 条已投递记录")

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
        proj = project_config({"providers": providers or []}, card)
        with self._model_lock:
            self.system_prompt = card.get("system_prompt", self.system_prompt)
            self.nickname = card.get("nickname") or self.nickname
            self.chat_model = card.get("chat_model") or self.chat_model
            self.vision_model = card.get("vision_model") or self.vision_model
            self.file_model = card.get("classify_model") or self.file_model
            self.chat_temperature = float(card.get("temperature", self.chat_temperature))
            self.chat_top_p = float(card.get("top_p", self.chat_top_p))
            self.vision_temp = float(card.get("vision_temp", self.vision_temp))
            self.vision_max_tokens = int(card.get("vision_max_tokens", self.vision_max_tokens))
            self.max_history = int(card.get("max_history", self.max_history))
            self.api_url = proj.get("ai_api_url", self.api_url)
            self.api_key = proj.get("ai_api_key", self.api_key)
            self.vision_api_url = proj.get("vision_api_url", self.vision_api_url)
            self.vision_api_key = proj.get("vision_api_key", self.vision_api_key)
        logger.info(
            f"[角色卡] 已切换: {card.get('name')} "
            f"(chat={self.chat_model}, vision={self.vision_model}, temp={self.chat_temperature})"
        )

    def _classify_task(self, text):
        if not self.task_enabled:
            return {"is_task": False, "task": ""}
        return classify_task_with_llm(self.api_url, self.api_key, self.file_model, text)

    def _latest_received_file(self):
        """微信下载目录里最近收到的文件（作为任务附件）"""
        if not self.file_storage_path:
            return None
        return self._find_latest_file(self.file_storage_path)

    def _dispatch_and_notify(self, chat_name, sender, task_desc, attachment_paths=None, extra=None):
        """投递任务 → 微信告知"处理中" → 唤起天枢窗口。返回是否投递成功"""
        task_info = {
            "msg_id": (extra or {}).get("msg_id"),
            "sender": sender,
            "chat_name": chat_name,
            "is_group": bool("群" in chat_name or "集团" in chat_name),
            "raw_message": (extra or {}).get("raw_message", ""),
            "task": task_desc,
        }
        for k, v in (extra or {}).items():
            if k not in task_info:
                task_info[k] = v
        task_id = dispatch_task(self.tasks_dir, task_info, attachment_paths)
        if task_info.get("msg_id"):
            self.dispatched_msg_ids.add(task_info["msg_id"])
        self._send_text("收到任务啦，正在处理中，稍等一下哦～", chat_name)
        if self.tianshu_window_title:
            ok = send_trigger_to_window(self.tianshu_window_title, self.tianshu_trigger_command)
            if not ok:
                logger.error(f"[天枢] 唤起窗口失败: {self.tianshu_window_title}，任务 {task_id} 保留在 {os.path.join(self.tasks_dir, task_id)}")
                self._send_text("天枢窗口没找到，不过任务已经记下了，处理完我会把结果发给你～", chat_name)
        else:
            logger.warning("[天枢] 未配置窗口标题，任务已投递但未唤起天枢")
        return True

    def _activate_wechat_window(self):
        """把微信主窗口激活到前台（uiautomation SetActive + SetForegroundWindow）。
        必须在前台才能保证 pyautogui 的 Ctrl+V/回车 发到微信而不是别的窗口"""
        import uiautomation as auto
        try:
            win = auto.WindowControl(Name="微信", ClassName="mmui::MainWindow")
            if win.Exists(0, 0):
                win.SetActive()
                time.sleep(0.4)
                try:
                    import ctypes
                    user32 = ctypes.windll.user32
                    user32.SetForegroundWindow(win.NativeWindowHandle)
                    time.sleep(0.2)
                except Exception:
                    pass
                return True
            logger.error("[回传] 未找到微信主窗口")
        except Exception as e:
            logger.error(f"[回传] 激活微信窗口失败: {e}")
        return False

    def _send_file_clipboard(self, fpath, chat):
        """剪贴板 CF_HDROP + Ctrl+V 发送文件（wxauto SendFiles UI 自动化失效时的替代方案）。
        关键：必须先激活微信窗口到前台，否则 Ctrl+V 会发到当前前台窗口（如天枢 CLI）"""
        # 先激活微信到前台
        if not self._activate_wechat_window():
            logger.warning("[回传] 未能激活微信窗口，发送可能发到错误窗口")
        if chat:
            try:
                self.wx.ChatWith(chat)
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f"[回传] 切换聊天窗口失败 {chat}: {e}")
        # ChatWith 后再确保微信前台（ChatWith 内部操作可能改变前台）
        self._activate_wechat_window()
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
                        self.wx.ChatWith(chat)
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
                        if self.file_send_method != "wxauto":
                            # 剪贴板 CF_HDROP 主用（SendFiles 实测不可用）
                            try:
                                sent = self._send_file_clipboard(fpath, chat)
                            except Exception as e:
                                logger.warning(f"[回传] 剪贴板发送异常: {e}")
                        if not sent:
                            # wxauto SendFiles 兜底
                            try:
                                self.wx.SendFiles(fpath, chat)
                                sent = True
                                logger.info(f"[回传] SendFiles 发送: {fname}")
                            except Exception as e:
                                logger.error(f"[回传] SendFiles 失败: {e}")
                        if not sent:
                            logger.error(f"[回传] 文件发送失败，保留在任务目录: {fname}")
                        else:
                            # 登记回传的成果文件（源路径 + 主干+发送时刻）：目录扫描时排除，
                            # 防止把 bot 自己发出去的成果（含微信写入接收目录的副本）误当成"用户本次发送的文件"投递进下一轮任务附件
                            self._register_sent_back(fpath)
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

    def _process_file_with_task(self, chat_name, sender, msg_obj):
        """文件消息：提取文本 → LLM 判断 → 是任务则投递（文件入 attachments）。非任务返回 False 走原路线"""
        file_dir = self.file_storage_path
        if not file_dir or not os.path.isdir(file_dir):
            return False
        time.sleep(3)  # 等微信下载完成
        latest_file = self._find_file_by_display_name(self._extract_file_display_name(msg_obj))
        if latest_file is None:
            latest_file = self._find_latest_file(file_dir)  # 兜底：最新文件扫描
        if not latest_file:
            return False
        filename = os.path.basename(latest_file)
        text_content = self._extract_file_text(latest_file)
        if text_content is None:
            return False
        probe = f"[文件 {filename}]\n{text_content[:2000]}"
        cls = self._classify_task(probe)
        if not cls["is_task"]:
            return False
        logger.info(f"[任务桥] 文件消息判定为任务: {cls['task'][:60]}")
        self._add_history(chat_name, "user", f"[任务] 文件 {filename}: {text_content[:200]}")
        self._add_history(chat_name, "assistant", "[任务已投递天枢处理]")
        self._dispatch_and_notify(
            chat_name, sender, cls["task"],
            attachment_paths=[latest_file],
            extra={
                "msg_id": getattr(msg_obj, "id", None),
                "raw_message": filename,
                "file_text": text_content[:5000],
            },
        )
        return True

    def _process_file_with_instruction(self, chat_name, sender, filepath, filename, text_content, user_instruction):
        """根据用户指令处理文件：LLM 判断指令是否任务 → 天枢投递 或 原文件识别（附带用户指令）"""
        is_group = "群" in chat_name or "集团" in chat_name
        instruction = user_instruction
        if is_group:
            at_tag = f"@{self.nickname}"
            instruction = instruction.replace(at_tag, "").strip()
        if not instruction:
            instruction = user_instruction.strip()

        # 判断用户指令是否为任务
        if self.task_enabled:
            cls = self._classify_task(instruction)
            if cls["is_task"]:
                logger.info(f"[任务桥] 文件+指令判定为任务: {cls['task'][:60]}")
                self._add_history(chat_name, "user", f"[任务] 文件 {filename}: {text_content[:200]}")
                self._add_history(chat_name, "assistant", "[任务已投递天枢处理]")
                self._dispatch_and_notify(
                    chat_name, sender, cls["task"],
                    attachment_paths=[filepath],
                    extra={
                        "msg_id": None,
                        "raw_message": filename,
                        "file_text": text_content[:5000],
                    },
                )
                return True

        # 非任务 → 原文件识别流程，但把用户指令一同发给 bot
        logger.info(f"[文件] 非任务，走文件识别流程，附带用户指令: {instruction[:60]}")
        self._add_history(chat_name, "assistant", f"[文件内容: {filename}] {text_content}")
        refine_prompt = (
            f"用户发来一个文件（{filename}），内容如下：\n\n"
            f"{text_content}\n\n"
            f"用户对文件处理的要求是：{instruction}\n\n"
            f"请根据文件内容和用户的要求，以{self.nickname}的身份回复用户。"
        )
        final_reply = self.call_chat_ai(chat_name, refine_prompt, sender_name=sender, is_group=is_group)
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

    def _find_user_file(self, directory):
        """与 _find_latest_file 相同的目录扫描，但排除 bot 自己回传的成果文件
        （记录在 _sent_back_files：路径+mtime 匹配即跳过）。返回用户发送的最新文件或 None"""
        latest_path = None
        latest_mtime = 0
        skip_patterns = ('.tmp', '.crdownload', '~$')
        stems = self._sent_back_stems
        try:
            for root, dirs, files in os.walk(directory):
                for fname in files:
                    if any(fname.startswith(p) or fname.endswith(p) for p in skip_patterns):
                        continue
                    full_path = os.path.join(root, fname)
                    try:
                        mtime = os.path.getmtime(full_path)
                    except OSError:
                        continue
                    # 排除回传成果：同一路径且 mtime 未变 → 是 bot 自己发出去的，不是用户新发的
                    if full_path in self._sent_back_files and self._sent_back_files[full_path] == mtime:
                        continue
                    # 排除微信写入接收目录的成果副本：文件名主干前缀匹配登记成果
                    #（含 (1)(2) 重名后缀变体与 '-美化版' 等扩展变体），且创建时刻在发送时刻附近
                    fstem = re.sub(r"\(\d+\)$", "", os.path.splitext(fname)[0])
                    hit_stem = None
                    for s in stems:
                        if fstem.startswith(s) or s in fstem:
                            hit_stem = s
                            break
                    if hit_stem:
                        try:
                            ctime = os.path.getctime(full_path)
                        except OSError:
                            ctime = 0
                        if abs(ctime - stems[hit_stem]) <= 300:
                            logger.debug(f"[文件] 排除成果副本: {fname} (stem={hit_stem})")
                            continue
                    if mtime > latest_mtime:
                        latest_mtime = mtime
                        latest_path = full_path
        except Exception as e:
            logger.error(f"[文件] 遍历目录失败: {e}")
            return None
        return latest_path

    def _extract_file_display_name(self, msg):
        """从 FileMessage 提取显示文件名。
        wxauto4 的 content 格式：'文件\\n<文件名>\\n[<大小>\\n]微信电脑版'
        （实测：'文件\\n养生规划表.html\\n微信电脑版' / repattern 可解析）
        返回文件名或 None"""
        try:
            content = getattr(msg, "content", "") or ""
            m = re.search(r"^文件\n([^\n]+)", content, flags=re.M)
            if m:
                return m.group(1).strip()
            # 兜底：按 repattern 解析
            rep = getattr(msg, "repattern", None)
            if rep:
                m2 = re.search(rep, content)
                if m2 and m2.group(1):
                    return m2.group(1).strip()
        except Exception as e:
            logger.error(f"[文件] 提取文件名失败: {e}")
        return None

    def _find_file_by_display_name(self, display_name):
        """按消息中的显示文件名在接收目录精确定位（微信 4.0 下载命名
        '<hash>_<msgid>_m_<原名>'，目录文件名包含原名；bot 回传的成果副本
        是干净原名，不同名时天然不命中）。多个候选（重名 (1)(2)…）取时间戳最新。
        返回路径或 None"""
        if not display_name:
            return None
        file_dir = self.file_storage_path
        if not file_dir or not os.path.isdir(file_dir):
            return None
        dstem = re.sub(r"\(\d+\)$", "", os.path.splitext(display_name)[0])
        best = None
        best_key = (-1, -1)  # (ctime, 微信重名编号)：ctime 相同（微信保留源时间戳）时取编号最大 = 最近下载
        try:
            for root, dirs, files in os.walk(file_dir):
                for fname in files:
                    fstem = re.sub(r"\(\d+\)$", "", os.path.splitext(fname)[0])
                    if dstem not in fstem:
                        continue
                    full = os.path.join(root, fname)
                    try:
                        ts = os.path.getctime(full)
                    except OSError:
                        continue
                    m_dup = re.search(r"\((\d+)\)$", os.path.splitext(fname)[0])
                    dup = int(m_dup.group(1)) if m_dup else 0
                    key = (ts, dup)
                    if key > best_key:
                        best_key = key
                        best = full
        except Exception as e:
            logger.error(f"[文件] 按文件名定位失败: {e}")
            return None
        if best:
            logger.info(f"[文件] 按消息文件名定位: {os.path.basename(best)}")
        return best

    def _acquire_received_file(self, timeout=3, msg=None):
        """获取用户本次接收的文件：优先按文件消息中的文件名精确定位
        （微信下载保留源文件时间戳，时间戳扫描不可靠）；
        拿不到消息对象时回退 sleep + 目录扫描（排除 bot 回传成果）。
        返回 (filepath, filename, text_content) 或 None"""
        file_dir = self.file_storage_path
        if not file_dir or not os.path.isdir(file_dir):
            return None
        latest_file = None
        if msg is not None:
            display = self._extract_file_display_name(msg)
            latest_file = self._find_file_by_display_name(display)
        if latest_file is None:
            time.sleep(timeout)
            latest_file = self._find_user_file(file_dir)
        if not latest_file:
            return None
        text_content = self._extract_file_text(latest_file)
        if text_content is None:
            return None
        return (latest_file, os.path.basename(latest_file), text_content)

    def _wait_pending_instruction(self):
        """pending 文件等待指令期间：暂停其他消息监听，只轮询 pending 聊天，
        从最新往回找用户文本作为指令（跳过 bot 自己的消息）。收到后获取文件并处理"""
        for chat_name in list(self._pending_files.keys()):
            try:
                self.wx.ChatWith(chat_name)
                time.sleep(0.2)
                msgs = self.wx.GetAllMessage()
                if not msgs:
                    continue
                for i in range(len(msgs) - 1, -1, -1):
                    m = msgs[i]
                    s = getattr(m, "sender", None)
                    c = getattr(m, "content", "")
                    if s is None or s == "self" or s == self.nickname:
                        continue
                    if isinstance(m, (ImageMessage, FileMessage, TimeMessage, SystemMessage)):
                        continue
                    if not c.strip():
                        continue
                    msg_key = f"{chat_name}_{s}_{c}"
                    if msg_key in self.recent_msg_ids:
                        continue
                    self.recent_msg_ids.add(msg_key)
                    pending = self._pending_files.pop(chat_name)
                    got = self._acquire_received_file(msg=pending.get("msg"))
                    if got:
                        self._process_file_with_instruction(
                            chat_name, pending["sender"], got[0], got[1], got[2], c.strip())
                    else:
                        # 取不到文件（下载失败等）：把指令按普通文本处理
                        logger.warning(f"[等待指令] 取不到文件，按普通消息处理: {chat_name}")
                        self._handle_text(chat_name, s, c.strip(), getattr(m, "id", None))
                    self.last_reply_time = time.time()
                    return
            except Exception as e:
                logger.error(f"[等待指令] 异常 {chat_name}: {e}")
                continue

    def _handle_text(self, chat_name, sender, content, msg_id=None, attachment_provider=None):
        """文本消息统一处理：任务判断 → 天枢投递（任务时才调用附件提供者取文件）或 普通聊天"""
        is_group = "群" in chat_name or "集团" in chat_name
        if is_group:
            at_tag = f"@{self.nickname}"
            content = content.replace(at_tag, "").strip()
            if not content:
                content = "Hello"
        question = content.strip()
        logger.info(f"[MSG] [{chat_name}] {sender}: {question[:80]}")
        if self.task_enabled and msg_id not in self.dispatched_msg_ids:
            cls = self._classify_task(question)
            if cls["is_task"]:
                logger.info(f"[任务桥] 判定为任务: {cls['task'][:60]}")
                self._add_history(chat_name, "user", f"[任务] {question}")
                self._add_history(chat_name, "assistant", "[任务已投递天枢处理]")
                attachment = attachment_provider() if attachment_provider else None
                self._dispatch_and_notify(
                    chat_name, sender, cls["task"],
                    attachment_paths=[attachment] if attachment else None,
                    extra={"msg_id": msg_id, "raw_message": content},
                )
                return True
        reply = self.call_chat_ai(chat_name, question, sender_name=sender, is_group=is_group)
        self._send_text(reply, chat_name)
        return True

    # ---------- 消息处理（改造） ----------

    def process_new_messages(self):
        if self.paused:
            return
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
        if self._sending_lock:
            # 成果回传期间暂停消息轮询，防止 ChatWith 切走窗口导致发错联系人
            return
        if self._pending_files:
            # 有待处理文件的聊天：暂停其他消息监听，专注等待该聊天的用户指令
            self._wait_pending_instruction()
            return
        if time.time() - self.last_reply_time < self.cooldown:
            return
        try:
            sessions = self.wx.GetSession()
            if not sessions:
                return
            for session in sessions:
                chat_name = session.name if hasattr(session, "name") else (session if isinstance(session, str) else str(session))
                if not chat_name:
                    continue
                self.wx.ChatWith(chat_name)
                time.sleep(0.2)
                msgs = self.wx.GetAllMessage()
                if not msgs:
                    continue
                full_msgs = msgs
                for i in range(len(full_msgs) - 1, -1, -1):
                    msg = full_msgs[i]
                    sender = getattr(msg, "sender", None)
                    content = getattr(msg, "content", "")
                    if sender is None or sender == "self" or sender == self.nickname:
                        continue
                    if "Self" in str(type(msg)):
                        continue
                    if isinstance(msg, ImageMessage):
                        if self._has_bot_reply_after(full_msgs, i, 5):
                            continue
                        self._process_image(chat_name, sender, msg)
                        self.last_reply_time = time.time()
                        return
                    if isinstance(msg, FileMessage):
                        if "upload" in str(content).lower() or "上传" in str(content):
                            continue
                        if self._has_bot_reply_after(full_msgs, i, 5):
                            continue

                        # 检查后三条消息中是否有用户文本（作为文件处理指令）
                        user_instruction = None
                        end = min(i + 4, len(full_msgs))
                        for j in range(i + 1, end):
                            next_msg = full_msgs[j]
                            next_sender = getattr(next_msg, "sender", None)
                            next_content = getattr(next_msg, "content", "")
                            if next_sender and next_sender != "self" and next_sender != self.nickname:
                                if not isinstance(next_msg, (ImageMessage, FileMessage, TimeMessage, SystemMessage)):
                                    if next_content.strip():
                                        user_instruction = next_content.strip()
                                        msg_key = f"{chat_name}_{next_sender}_{next_content}"
                                        self.recent_msg_ids.add(msg_key)
                                        break

                        # 文件下载目录
                        file_dir = self.file_storage_path
                        if not file_dir or not os.path.isdir(file_dir):
                            return

                        if user_instruction:
                            # 用户已给出指令 → 按文件消息中的文件名精确定位（回退 sleep + 目录扫描）
                            got = self._acquire_received_file(msg=msg)
                            if got:
                                self._process_file_with_instruction(
                                    chat_name, sender, got[0], got[1], got[2], user_instruction)
                            else:
                                # 下载失败/取不到文件，走原流程兜底（内含等待与提示）
                                self._process_file(chat_name, sender, msg)
                        else:
                            # 无后续指令 → 存待处理状态（连同文件消息对象，指令到达后按文件名精确定位）
                            self._pending_files[chat_name] = {
                                "sender": sender,
                                "msg": msg,
                            }
                            self._send_text(
                                "文件已收到～请告诉我需要怎么处理呢？",
                                chat_name)
                        self.last_reply_time = time.time()
                        return
                latest = msgs[-1]
                sender = getattr(latest, "sender", None)
                content = getattr(latest, "content", "")
                if sender is None or sender == "self" or sender == self.nickname:
                    continue
                if isinstance(latest, (ImageMessage, FileMessage)):
                    continue
                msg_key = f"{chat_name}_{sender}_{content}"
                if msg_key in self.recent_msg_ids:
                    continue
                self.recent_msg_ids.add(msg_key)
                # 文本统一处理：任务判断（附最近接收文件）/ 普通聊天
                latest_msg_id = getattr(latest, "id", None)
                self._handle_text(chat_name, sender, content, latest_msg_id,
                                  attachment_provider=self._latest_received_file)
                self.last_reply_time = time.time()
                return
        except Exception as e:
            logger.error(f"Message processing error: {e}\n{traceback.format_exc()}")


# =====================================================================
# TianshuController：继承原 Controller，加 tianshu-window / task-status 命令
# =====================================================================

class TianshuController(Controller):
    def _listen(self):
        while True:
            try:
                cmd = input().strip().lower()
                if not cmd:
                    continue
                if cmd == "help":
                    print("""
可用命令：
  pause                     - 暂停自动回复
  resume                    - 恢复自动回复
  model                     - 交互式选择聊天模型
  model <名称>              - 直接切换聊天模型
  vision-model              - 交互式选择视觉模型
  vision-model <名称>       - 直接切换视觉模型
  chat-temp <值>            - 设置聊天模型温度 (0~2)
  chat-top-p <值>           - 设置聊天模型 top_p (0~1)
  vision-temp <值>          - 设置视觉模型温度 (0~2)
  tianshu-window            - 重新选择天枢 CLI 窗口
  task-status               - 查看任务流转状态（等待/完成/归档）
  clear                     - 清空全部对话历史
  clear <聊天ID>            - 清空指定聊天的历史
  del <聊天ID> <序号1> [序号2] [序号3-5] - 删除指定聊天中的消息
  memory <聊天ID>           - 查看指定聊天的历史消息（带序号）
  status                    - 查看当前状态（含模型信息、温度、top_p）
  quit                      - 退出程序
  help                      - 显示本帮助
""")
                elif cmd == "pause":
                    self.bot.paused = True
                    logger.info("⏸️  已暂停自动回复")
                elif cmd == "resume":
                    self.bot.paused = False
                    logger.info("▶️  已恢复自动回复")
                elif cmd == "model":
                    self._select_model("chat")
                elif cmd.startswith("model "):
                    new_model = cmd.split(" ", 1)[1].strip()
                    with self.bot._model_lock:
                        self.bot.chat_model = new_model
                    logger.info(f"🔄 聊天模型已切换为：{new_model}")
                elif cmd == "vision-model":
                    self._select_model("vision")
                elif cmd.startswith("vision-model "):
                    new_vision = cmd.split(" ", 1)[1].strip()
                    with self.bot._model_lock:
                        self.bot.vision_model = new_vision
                    logger.info(f"🔄 视觉模型已切换为：{new_vision}")
                elif cmd.startswith("chat-temp "):
                    try:
                        val = float(cmd.split(" ", 1)[1])
                        if 0 <= val <= 2:
                            self.bot.set_chat_temperature(val)
                        else:
                            logger.warning("温度值应在 0~2 之间")
                    except ValueError:
                        logger.warning("请输入有效的数字")
                elif cmd.startswith("chat-top-p "):
                    try:
                        val = float(cmd.split(" ", 1)[1])
                        if 0 <= val <= 1:
                            self.bot.set_chat_top_p(val)
                        else:
                            logger.warning("top_p 值应在 0~1 之间")
                    except ValueError:
                        logger.warning("请输入有效的数字")
                elif cmd.startswith("vision-temp "):
                    try:
                        val = float(cmd.split(" ", 1)[1])
                        if 0 <= val <= 2:
                            self.bot.set_vision_temperature(val)
                        else:
                            logger.warning("温度值应在 0~2 之间")
                    except ValueError:
                        logger.warning("请输入有效的数字")
                elif cmd == "tianshu-window":
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
                elif cmd == "task-status":
                    self._show_task_status()
                elif cmd == "clear":
                    self.bot.clear_history()
                elif cmd.startswith("clear "):
                    target = cmd.split(" ", 1)[1].strip()
                    self.bot.clear_history(target)
                elif cmd.startswith("del "):
                    parts = cmd.split()
                    if len(parts) < 3:
                        logger.info("用法: del <聊天ID> <序号1> [序号2] ... 或 del <聊天ID> <起始序号-结束序号>")
                        continue
                    chat_id = parts[1]
                    indices = []
                    for part in parts[2:]:
                        if "-" in part:
                            try:
                                start, end = map(int, part.split("-"))
                                if start > end:
                                    start, end = end, start
                                indices.extend(range(start, end + 1))
                            except ValueError:
                                logger.warning(f"无效的范围格式: {part}")
                        else:
                            try:
                                indices.append(int(part))
                            except ValueError:
                                logger.warning(f"无效的序号: {part}")
                    if not indices:
                        continue
                    print(f"即将从聊天 '{chat_id}' 中删除序号: {sorted(set(indices))}")
                    confirm = input("确认删除？(y/n): ").strip().lower()
                    if confirm == "y":
                        self.bot.delete_messages(chat_id, indices)
                    else:
                        logger.info("已取消删除")
                elif cmd.startswith("memory "):
                    target = cmd.split(" ", 1)[1].strip()
                    self._show_memory(target)
                elif cmd == "status":
                    self._show_status()
                elif cmd == "quit":
                    logger.info("👋 程序退出")
                    os._exit(0)
                else:
                    logger.info(f"❌ 未知命令: {cmd}")
            except (EOFError, KeyboardInterrupt):
                break

    def _show_task_status(self):
        tasks_dir = self.bot.tasks_dir
        if not os.path.isdir(tasks_dir):
            print("任务目录不存在")
            return
        waiting = done = 0
        for name in sorted(os.listdir(tasks_dir)):
            task_dir = os.path.join(tasks_dir, name)
            if not os.path.isdir(task_dir) or name == "sent":
                continue
            has_result = os.path.isfile(os.path.join(task_dir, "result.json"))
            state = "✅ 天枢已完成" if has_result else "⏳ 天枢处理中"
            desc = ""
            tj = os.path.join(task_dir, "task.json")
            if os.path.isfile(tj):
                try:
                    with open(tj, "r", encoding="utf-8") as f:
                        desc = json.load(f).get("task", "")[:40]
                except Exception:
                    pass
            print(f"  - {name} [{state}] {desc}")
            if has_result:
                done += 1
            else:
                waiting += 1
        sent_dir = os.path.join(tasks_dir, "sent")
        if os.path.isdir(sent_dir):
            print(f"已归档: {len(os.listdir(sent_dir))} 个任务")
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
        check("T1 旧 config 补齐 task_* 默认", all(k in cfg for k in TASK_DEFAULTS), str(cfg))
        check("T1 默认 tasks_dir", cfg["tasks_dir"] == r"D:\工作间\wxauto", str(cfg.get("tasks_dir")))
        check("T1 默认窗口标题为空", cfg["tianshu_window_title"] == "", str(cfg.get("tianshu_window_title")))

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
        tid2 = dispatch_task(tasks_dir, {"msg_id": "m1", "chat_name": "王文生", "sender": "王", "task": "根据文档做网站"}, [att])
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
        check("T7 deliver 收到正确数据", delivered and delivered[0][0] == "王文生" and delivered[0][1] == "做好了", str(delivered))
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

        # 场景 C：对照——未登记 stem（修复前行为）→ 副本 mtime 最新会被误选（bug 复现）
        dir_c = os.path.join(tmp, "recv_c")
        os.makedirs(dir_c)
        bot_c = make_bot(dir_c)
        shutil.copy2(src_out, os.path.join(dir_c, "成果报告.html"))
        os.utime(os.path.join(dir_c, "成果报告.html"), (time.time() + 10, time.time() + 10))
        target_c = os.path.join(dir_c, "报名表.xlsx")
        with open(target_c, "w", encoding="utf-8") as f:
            f.write("target")
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
        # 完整链路：_acquire_received_file(msg=...) 命中（不 sleep 等待）
        got_f = bot_e._acquire_received_file(timeout=0, msg=fake_msg)
        check("T13 完整链路命中用户文件",
              got_f is not None and got_f[0] == user_file and got_f[1] == os.path.basename(user_file),
              str(got_f))
        # 兜底：显示名不匹配任何文件 → 回退 _find_user_file（时间戳扫描 + 成果排除）
        fake_msg2 = SimpleNamespace(content="文件\n不存在.xlsx\n1KB\n微信电脑版", repattern=None)
        bot_e._register_sent_back(result_file)  # 成果已登记（真实流程中发送时即登记）
        got_g = bot_e._acquire_received_file(timeout=0, msg=fake_msg2)
        check("T13 显示名不匹配时回退扫描（排除成果后选中用户文件）",
              got_g is not None and got_g[0] == user_file, str(got_g))

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
            return b

        mem_dir = os.path.join(tmp, "mem")
        os.makedirs(mem_dir)
        mb = make_mem_bot(mem_dir)
        mb._remember_task_result("王文生", {"status": "success", "reply_text": "网站做好了，见成果文件", "files": ["site.zip"]})
        hist = mb.memory_db.get("王文生", [])
        check("T15 成功结果写入记忆",
              len(hist) == 1 and hist[0]["role"] == "assistant"
              and "[任务结果] 网站做好了，见成果文件，成果文件: site.zip" in hist[0]["content"], str(hist))
        mb._remember_task_result("王文生", {"status": "failed", "reply_text": ""})
        check("T15 失败结果写入记忆（默认文案）",
              len(mb.memory_db["王文生"]) == 2 and "任务处理失败了" in mb.memory_db["王文生"][1]["content"],
              str(mb.memory_db["王文生"]))
        mb._remember_task_result("", {"status": "success", "reply_text": "x"})
        check("T15 空 chat 不写记忆", len(mb.memory_db) == 1, str(mb.memory_db))
        check("T15 记忆落盘", os.path.isfile(os.path.join(mem_dir, "mem.json")))
        mb2 = make_mem_bot(mem_dir)
        mb2._load_memory()
        check("T15 重启后任务结果记忆仍在",
              "王文生" in mb2.memory_db and "[任务结果]" in mb2.memory_db["王文生"][0]["content"],
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
        bot.run()
    else:
        print("用法: python xiaoli_bot.py --test | --run")
