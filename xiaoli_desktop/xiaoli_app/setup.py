# -*- coding: utf-8 -*-
"""环境检测与一键安装：微信/天枢/首轮提示词检测 + 天枢 zip 流式下载安全解压。

面向小白用户：软件内检测依赖 → 缺失时一键下载安装（进度可视化）。
"""
import json
import logging
import os
import subprocess
import tempfile
import time
import zipfile

import requests

logger = logging.getLogger("xiaoli")

DEFAULT_TIANSHU_URL = "https://codeload.github.com/huiliyi37/Tianshu-Tui/zip/refs/heads/main"
TIANSHU_EXE = "tianshu-desktop.exe"
WECHAT_KEYWORDS = ("微信", "WeChat")

# 内置首轮提示词模板（内化：小白新机器不依赖外部文件）。
# 占位符：{tasks_dir} 任务桥目录（cfg.tasks_dir）、{trigger} 唤起天枢的指令（cfg.tianshu_trigger_command）。
FIRST_PROMPT_DEFAULT = """你是天枢，正在为微信 AI 助手「小漓」服务。用户通过微信向小漓发消息、传文件、派任务，小漓会把任务投递给你，由你完成并回传成果。

当微信机器人发来指令「{trigger}」时，执行以下流程：

1. 读取 {tasks_dir}\\README.md，了解任务包与成果包的格式约定；
2. 扫描 {tasks_dir}\\ 下各任务目录，找有 task.json 且没有 result.json 的目录（已有 result.json 的跳过）；
3. 按 task.json 里的 task 描述、attachments\\ 附件、file_text 内容执行任务；
4. 完成后在同一任务目录写 result.json（{{"status":"success","reply_text":"...","files":["成果文件..."]}}），成果文件也放该目录；
5. 处理时间较长的任务，在关键节点覆写任务目录下的 progress.json（{{"stage":"一句通俗中文，说明现在在做什么"}}）——开始动手、大的阶段切换、卡住或需要长时间等待时各写一次；不要写百分比，不要每个工具调用都写（小漓按固定节流转发，写太勤也不会都转发）；
6. 不要写 result.json / progress.json 以外的状态文件——小漓检测到 result.json 就会把成果发回微信并把目录归档。

全程无人值守：不要进入 Plan Mode（/plan-mode 保持关闭）、不要提交计划等待审批、不要向用户请求任何确认或补充信息——遇到歧义按最合理的方式执行并在 reply_text 里说明。所有工具调用已在 YOLO 模式下自动放行，直接执行即可。

注意：reply_text 是发给微信用户的回复，请用通俗友好的中文，用户可能不了解技术细节。"""

# 任务桥协议文档（初始化时写入 tasks_dir\\README.md，首次生成、已存在不覆盖）。
BRIDGE_README = """# 微信任务桥协议（小漓 ↔ 天枢）

小漓把微信用户的任务投递到本目录，天枢处理后回传成果。

## 任务包

每个任务一个子目录 <task_id>\\：

- task.json：任务描述（task 字段）、发送者（sender）、聊天（chat_name）、附件列表（attachments）、文件文本（file_text）、任务 ID（task_id）、创建时间（created_at）
- attachments\\：附件文件（如有）

## 成果包

天枢完成任务后，在同一个任务目录写 result.json：

{"status": "success", "reply_text": "给用户的回复", "files": ["成果文件名..."]}

- reply_text 用通俗友好的中文（用户可能不懂技术细节）
- 成果文件放在该任务目录下，文件名写入 files 数组

## 进度回传（可选）

处理时间较长的任务，可在任务目录写 progress.json 汇报当前阶段：

{"stage": "一句话说明现在在做什么"}

- 只在关键节点覆写：开始动手、大的阶段切换、卡住或需要长时间等待；
- stage 用一句通俗中文（微信用户直接可读），不要写百分比；
- 不要每个工具调用都写——小漓按固定节流转发，写太勤也不会都转发。

## 归档

小漓检测到 result.json 后，会把任务目录移入 sent\\ 归档，并把成果发回微信。
"""


def _list_windows():
    """窗口名枚举（延迟 import，避免触发 xiaoli_bot 顶层副作用）。"""
    from xiaoli_bot import list_windows
    return [name for name, _h in list_windows()]


def _console_windows():
    """枚举控制台/终端类顶层窗口，返回 [(标题, PID)] 列表。

    Win32：ConsoleWindowClass / CASCADIA / mintty / WindowClass_。PID 用于
    进程树验证（终端宿主把 CLI 标题改写为「Windows PowerShell」时，靠
    PID 查进程命令行是否含 rivet 来确认是 CLI 而非用户自己的 PowerShell）。
    枚举失败（非 Windows）返回 None，调用方据此回退全量匹配。
    天枢 CLI 是 cmd /k rivet 启动的控制台窗口——按窗口类名区分后，浏览器/编辑器等
    标题含 "npm" 的诱饵窗口（非控制台类）天然被排除，杜绝 resolve_cli_window 的 fail-open 误发。
    """
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
    except Exception:
        return None
    try:
        titles = []

        def _cb(hwnd, _lparam):
            try:
                buf = ctypes.create_unicode_buffer(512)
                n = user32.GetWindowTextW(hwnd, buf, 512)
                if n > 0:
                    cls = ctypes.create_unicode_buffer(256)
                    user32.GetClassNameW(hwnd, cls, 256)
                    cn = (cls.value or "").lower()
                    if any(k in cn for k in ("console", "cascadia", "mintty", "windowclass")):
                        pid = wintypes.DWORD()
                        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        titles.append((buf.value.strip(), int(pid.value)))
            except Exception:
                pass
            return True

        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(_cb)
        user32.EnumWindows(enum_proc, 0)
        return [t for t in titles if t[0]]
    except Exception:
        return None


def _decode_process_bytes(raw):
    """进程表输出的字节流 → 行列表（utf-8 → gbk → cp936 → replace 逐级解码）。

    PowerShell 重定向输出用 ANSI 代码页（中文 Windows = GBK）——历史缺陷：
    text=True 默认按 UTF-8 解码 → UnicodeDecodeError 被吞 → 恒返回空 →
    CLI 进程证据函数永远为 False（兜底形同虚设）。
    """
    raw = raw or b""
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(enc).splitlines()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").splitlines()


def _cli_process_cmdlines():
    """读全系统进程命令行（PID|PPID|命令行 三列文本），失败返回 None。"""
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             "ForEach-Object { \"$($_.ProcessId)|$($_.ParentProcessId)|$($_.CommandLine)\" }"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return None
    return _decode_process_bytes(out.stdout)


def _line_is_cli_process(line_low):
    r"""进程命令行是否为天枢 CLI 实例（小写行文本）。

    签名（新→旧）：
    - `tianshu-tui`：CLI 主程序路径（npm 全局 node_modules\tianshu-tui\dist\main.js
      ——新版直接以 node 运行，命令行里没有 rivet 字样）
    - `rivet`：旧版由 `cmd /k ... rivet` 启动（cmd 常驻进程命令行含 rivet）
    排除项：`.rivet`（数据/配置目录路径，桌面端 MCP server 命令行里带它
    → 历史误报源）、`rivet-runtime`（桌面端 serve 进程）、`rivet-desktop`。
    """
    if "tianshu-tui" in line_low:
        return True
    if "rivet" in line_low and not any(
            k in line_low for k in (".rivet", "rivet-runtime", "rivet-desktop")):
        return True
    return False


def _cli_process_running(pid=None):
    """系统里是否存在天枢 CLI 进程（weak 标题窗口的 CLI 身份验证）。

    pid 参数仅为兼容既有调用形态而保留（不做窗口↔进程树关联——WT 标签
    场景下窗口 PID 是 WindowsTerminal.exe，与 CLI 进程无父子关系，
    从窗口 PID 查进程树必然落空）。失败（PowerShell 不可用/解析异常）
    返回 False = 不认作 CLI（宁缺毋滥）。
    """
    lines = _cli_process_cmdlines()
    if lines is None:
        return False
    return any(_line_is_cli_process(l.lower()) for l in lines)


def _title_has_cli_signature(name):
    """窗口标题是否带 CLI 签名（强特征，无需进程验证）。

    覆盖三代形态：
    - `tianshu-tui`：新版 WT 默认标题 = 运行命令行（空闲态实测形态）
    - `npm prefix`：旧版小漓启动时 `title npm prefix` 设定的标题
    - `rivet`：更早版本 CLI 标题（排除 `.rivet` 路径误报）
    """
    low = (name or "").lower()
    if "npm prefix" in low or "tianshu-tui" in low:
        return True
    return "rivet" in low and ".rivet" not in low


def _is_desktop_window(name):
    """桌面端（Electron）窗口判定——**不能按 tianshu 字样一刀切**。

    历史缺陷：新版 CLI 标题/命令行含 `tianshu-tui`，旧规则「含 tianshu 即
    桌面端」会把 CLI 窗口整个排除（连同进程证据一起失效，CLI 窗口再也找不到）。
    现行判据：中文「天枢」/ 含 tianshu 但不含 `tianshu-tui` / rivet-desktop。
    注：CLI 窗口由控制台类枚举筛选，Electron 桌面端天然不是控制台类；
    本判据用于控制台枚举不可用（回退全量窗口）时的兜底。
    """
    s = name or ""
    if "天枢" in s:
        return True
    low = s.lower()
    if "tianshu-tui" in low:
        return False
    return "tianshu" in low or "rivet-desktop" in low


def _process_has_rivet(pid=None):
    """兼容别名（旧名）：进程证据判定主体见 _cli_process_running。"""
    return _cli_process_running(pid)


def _is_cli_feature(name, pid=None, process_has_rivet_fn=None):
    """CLI 窗口特征识别：标题签名（强特征）+ 终端默认标题（弱特征，需进程验证）。

    强特征（标题自身可判定）：`tianshu-tui`（新版空闲态）/ `npm prefix`（旧版
    启动标记）/ `rivet`——裸 "npm" 会误判用户手动开的 npm 子命令窗口
    （npm root 等），/yes 打错窗口，故只认精确短语。
    弱特征：终端宿主默认标题（Windows PowerShell / Command Prompt / 命令
    提示符——CLI 跑工具时 WT 自动标题显示子进程名，空闲才回命令行形态）
    ——必须系统里确实有 CLI 进程才认（用户自己开的 PowerShell 命中同样的
    标题但系统里没有 CLI 进程时不认）。"""
    if _title_has_cli_signature(name):
        return True
    low = (name or "").lower()
    weak = ("windows powershell" in low or "command prompt" in low
            or "命令提示符" in (name or "") or "powershell" in low)
    if weak and pid is not None:
        fn = process_has_rivet_fn or _cli_process_running
        try:
            return bool(fn(pid))
        except Exception:
            return False
    return False


def _pick_cli_window(candidates, process_fn=None):
    """从候选控制台窗口里挑 CLI 窗口：强特征优先；弱特征需唯一性。

    - 第一轮：标题自带 CLI 签名（`tianshu-tui` / `npm prefix` / `rivet`）——
      命中即用，多个命中取第一个（枚举序）。
    - 第二轮：弱特征（终端默认标题「Windows PowerShell」等——CLI 跑工具时
      WT 自动标题显示子进程名）必须叠加进程证据；且**仅当候选窗口只剩一个**
      才采用——多终端窗口时弱特征无法区分是哪一个（进程证据是全系统扫描），
      猜错会把提示词打进用户自己的 PowerShell/cmd 窗口 → fail-closed 不猜，
      走下一级启动新 CLI（宁可多开一个窗口，不可乱发）。
    - 桌面端窗口一律排除（证据式判定，不按 tianshu 字样一刀切）。
    """
    cands = [(n, p) for (n, p) in (candidates or [])
             if not _is_desktop_window(n)]
    for name, _pid in cands:
        if _title_has_cli_signature(name):
            return name
    if len(cands) == 1 and _is_cli_feature(cands[0][0], cands[0][1], process_fn):
        return cands[0][0]
    if len(cands) > 1:
        logger.info("[CLI 定位] 多个候选终端窗口且均无 CLI 标题签名，"
                    "fail-closed 不猜（将启动新 CLI）：%s",
                    [n[:40] for n, _p in cands])
    return None


def _norm_console_entries(entries):
    """归一化控制台窗口枚举结果：兼容 [(title, pid)] 与纯 [title] 两种形态。

    _console_windows 现返回 (title, pid) 元组；测试与旧调用方可能注入纯
    字符串列表——统一成 [(title, pid or None)]，pid 缺失时弱特征不启用。"""
    out = []
    for e in entries or []:
        if isinstance(e, (tuple, list)) and len(e) >= 2:
            out.append((str(e[0]), e[1]))
        else:
            out.append((str(e), None))
    return out


def check_environment(cfg):
    """环境检测报告：{wechat, tianshu, first_prompt}，每项 {ok, detail}。"""
    out = {}
    # ---- 微信：窗口为主，tasklist 进程兜底 ----
    wechat_ok, detail = False, "未检测到微信窗口"
    try:
        for name in _list_windows():
            if any(k in name for k in WECHAT_KEYWORDS):
                wechat_ok, detail = True, f"检测到微信窗口「{name}」"
                break
    except Exception as e:
        detail = f"窗口检测失败: {e}"
    if not wechat_ok:
        try:
            r = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if "WeChat.exe" in r.stdout:
                wechat_ok, detail = True, "检测到微信进程 WeChat.exe（窗口未找到）"
        except Exception as e:
            detail += f"；进程检测失败: {e}"
    out["wechat"] = {"ok": wechat_ok, "detail": detail}

    # ---- 天枢：CLI(rivet) 或桌面端任一存在即就绪；配置目录 → 自动探测 → 窗口 ----
    import shutil
    rivet_ok = bool(shutil.which("rivet") or shutil.which("rivet.cmd"))
    tianshu_dir = (cfg.get("tianshu_install_dir") or "").strip()
    if tianshu_dir:
        # 显式指定安装目录：只认该目录（避免用户已配置却误判自动探测）
        exe = os.path.join(tianshu_dir, TIANSHU_EXE)
        desktop_ok = os.path.isfile(exe)
        detail_desktop = f"桌面端：{exe}" if desktop_ok else f"桌面端未找到：{tianshu_dir}"
    else:
        found = detect_tianshu_dir()
        if found:
            desktop_ok, detail_desktop = True, f"桌面端：{os.path.join(found, TIANSHU_EXE)}"
        else:
            desktop_ok, detail_desktop = False, f"桌面端未找到（{TIANSHU_EXE}）"
    tianshu_ok = rivet_ok or desktop_ok
    parts = []
    if rivet_ok:
        parts.append("CLI(rivet) ✓")
    if desktop_ok:
        parts.append(detail_desktop)
    if not parts:
        parts.append("未安装（CLI: npm install -g tianshu-tui；或桌面端一键安装）")
    detail = "；".join(parts)
    tianshu_win = ""
    try:
        for name in _list_windows():
            if "天枢" in name or "Tianshu" in name:
                tianshu_win = name
                break
    except Exception:
        pass
    out["tianshu"] = {"ok": tianshu_ok, "detail": detail, "window": tianshu_win}

    # ---- 首轮提示词：内置模板兜底，自定义文件优先 ----
    fp = (cfg.get("first_prompt_path") or "").strip()
    if fp and os.path.isfile(fp):
        out["first_prompt"] = {"ok": True, "detail": fp, "path": fp}
    else:
        out["first_prompt"] = {
            "ok": True,
            "detail": "内置模板（可自定义 first_prompt_path）",
            "path": fp,
        }
    return out


def build_first_prompt(cfg):
    """返回要发送给天枢的首轮提示词文本。

    first_prompt_path 非空且文件存在 → 读文件（兼容自定义）；
    否则 → 内置模板，用 cfg 的 tasks_dir / tianshu_trigger_command 填充。
    """
    fp = (cfg.get("first_prompt_path") or "").strip()
    if fp and os.path.isfile(fp):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            pass
    tasks_dir = (cfg.get("tasks_dir") or "").strip() or "tasks"
    trigger = (cfg.get("tianshu_trigger_command") or "").strip() or "开始处理"
    return FIRST_PROMPT_DEFAULT.format(tasks_dir=tasks_dir, trigger=trigger)


def ensure_bridge_readme(tasks_dir):
    """tasks_dir 下无 README.md 时写入任务桥协议文档（不覆盖已有内容）。"""
    if not tasks_dir:
        return False
    p = os.path.join(tasks_dir, "README.md")
    if os.path.isfile(p):
        return False
    try:
        os.makedirs(tasks_dir, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(BRIDGE_README)
        return True
    except OSError:
        return False


def detect_tianshu_dir():
    """自动探测天枢安装目录（常见位置）。"""
    candidates = [
        os.path.join(os.path.expanduser("~"), "Tianshu"),
        r"D:\AI\Tianshu",
        r"C:\Tianshu",
    ]
    for c in candidates:
        if os.path.isfile(os.path.join(c, TIANSHU_EXE)):
            return c
    return None


def launch_tianshu(cfg):
    """启动天枢 CLI（rivet）：在用户选的工作间（tasks_dir）下开新 cmd 窗口运行 rivet。

    返回 (ok, detail)。rivet 命令经 shutil.which 定位（npm 全局安装 tianshu-tui）。
    窗口标题用 `title npm prefix` 标记（不含 tianshu，不撞桌面端判据）——注意
    Win11 默认终端（Windows Terminal）会按「运行中的进程」自动改写标签标题
    （空闲=运行命令行，跑工具=子进程名如 Windows PowerShell），title 标记并非
    全程可见；定位主体靠 CLI 进程签名 + 标题多形态特征（见 _is_cli_feature）。
    历史缺陷：cwd 用 tianshu_workdir（tasks_dir 父目录）导致 agent 工作文件
    （.rivet 等）落在程序目录旁而不是用户选的工作间——改为优先 tasks_dir。
    """
    import shutil
    import subprocess
    rivet = shutil.which("rivet") or shutil.which("rivet.cmd")
    if not rivet:
        return False, "未找到 rivet 命令，请先执行：npm install -g tianshu-tui"
    workdir = (cfg.get("tasks_dir") or "").strip() \
        or (cfg.get("tianshu_workdir") or "").strip() \
        or os.path.expanduser("~")
    try:
        os.makedirs(workdir, exist_ok=True)
    except OSError:
        pass
    try:
        # CREATE_NEW_CONSOLE：Windows 原生新开控制台窗口（可靠）；cmd /k 保持窗口。
        # title npm prefix：给窗口设一个不含 tianshu 的标记标题（conhost 托管时
        # 稳态可见；Win11 默认终端 WT 会按运行进程自动改写标签标题，标记可能
        # 被盖掉——定位不依赖它，靠进程签名 + 标题多形态特征）。
        # RIVET_PLAN_MODE_SUGGEST=0：关闭复杂任务自动进入 Plan Mode——
        # 无人值守场景任务不能卡在 plan 审批（README：默认 auto 命中多模块/
        # 重构/安全任务时自主进入，等 /plan-approve 确认）。
        proc = subprocess.Popen(
            ["cmd", "/k",
             "title npm prefix && set RIVET_PLAN_MODE_SUGGEST=0 && rivet"],
            cwd=workdir,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        global _last_launch_pid
        _last_launch_pid = proc.pid
        return True, f"天枢 CLI 已启动（{workdir}）"
    except OSError as e:
        return False, f"启动天枢 CLI 失败：{e}"


def find_tianshu_dir(root):
    """在 root 下递归（深度≤3）找 tianshu-desktop.exe 所在目录。"""
    for dirpath, dirnames, filenames in os.walk(root):
        depth = dirpath[len(root):].count(os.sep)
        if depth > 3:
            dirnames[:] = []
            continue
        if TIANSHU_EXE in filenames:
            return dirpath
    return None


def install_tianshu(dest_dir, progress_cb=None, url=DEFAULT_TIANSHU_URL, timeout=120):
    """流式下载天枢 zip → 安全解压到 dest_dir。返回含 exe 的目录。

    progress_cb(pct): 下载进度回调 0..100（content-length 缺失时按分块计数）。
    """
    resp = requests.get(url, stream=True, timeout=timeout)
    resp.raise_for_status()
    total = int(resp.headers.get("content-length") or 0)
    tmp_zip = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp_zip.close()
    downloaded = 0
    try:
        with open(tmp_zip.name, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb is not None and total:
                    progress_cb(min(100, int(downloaded * 100 / total)))
        # 下载完成（可能无 content-length 或分块误差）→ 强制收尾 100%
        if progress_cb is not None:
            progress_cb(100)
        os.makedirs(dest_dir, exist_ok=True)
        with zipfile.ZipFile(tmp_zip.name) as zf:
            for m in zf.infolist():
                norm = os.path.normpath(m.filename)
                # 目录穿越 = normpath 后首段为 ".."（..\evil.txt / ../evil.txt / 裸 ..）；
                # 不能用 startswith("..")——会误拒 ..foo.txt 类合法双点文件名
                if os.path.isabs(norm) or norm == ".." or norm.startswith(".." + os.sep):
                    raise ValueError(f"拒绝非法压缩成员: {m.filename}")
            zf.extractall(dest_dir)
    finally:
        try:
            os.unlink(tmp_zip.name)
        except OSError:
            pass
    return find_tianshu_dir(dest_dir) or dest_dir


def _send_trigger_to_window(title, command, hold=0.5, enter_times=1):
    """激活窗口 → 剪贴板粘贴 → 回车（延迟 import，测试可 mock）。

    enter_times：连续回车次数——天枢 CLI 实测首轮提示词需按两次回车才提交。
    """
    from xiaoli_bot import send_trigger_to_window
    return send_trigger_to_window(title, command, hold, enter_times)


def send_prompt_to_tianshu(text, window_title):
    """把首轮提示词发送给天枢窗口（激活→剪贴板粘贴→两次回车）。返回 bool。

    天枢 CLI 实测：粘贴后单次回车不提交，需连续两次回车（首轮提示词均如此）。
    hold 加长到 2.0s：首轮提示词场景 CLI 可能刚由 resolve_cli_window 第 3 级
    启动，TUI 尚未就绪——粘贴/回车发得太早会丢在初始化阶段（用户实测「只
    复制了提示词却缺失两次回车」）。加长等待让 CLI 输入框就绪。
    """
    if not window_title or not text:
        return False
    return _send_trigger_to_window(window_title, text, hold=2.0, enter_times=2)


# resolve_cli_window 第 3 级 launch 幂等护栏：冷却期内不重复 launch。
# 用户实测：初始化发首轮提示词时 resolve 每次都走到第 3 级 → 每轮都开
# 新 CLI 窗口，窗口一个接一个地开，循环不止。launch 后记时间戳，冷却期内
# 只轮询已启动窗口（窗口出现即复用），不重复开窗。
_LAUNCH_COOLDOWN_SECONDS = 60.0
_last_launch_mono = None
# launch_tianshu 启动的 CLI cmd 进程 PID——close_window_by_title 回退链
# 最后一环用 taskkill /F /T 杀进程树（cmd→node 全杀，窗口必关）。
# 窗口进程（conhost/WT）与 CLI 进程无父子关系，进程树验证查不到 rivet
# 时靠它兜底（用户实测：/yes 后标题变 Windows PowerShell，窗口关不掉）。
_last_launch_pid = None


def resolve_cli_window(cfg, list_windows_fn=None, launch_fn=None, sleep_fn=None,
                       console_windows_fn=None, process_has_rivet_fn=None):
    """定位天枢 CLI 窗口标题，返回 (title, detail)。

    title 非空 = 找到可发送的 CLI 窗口；空 = detail 含失败原因（launch 失败/窗口未出现）。
    process_has_rivet_fn(pid)：弱特征标题（终端宿主改写的 Windows
    PowerShell 等）的进程验证，默认 _cli_process_running。
    三级定位，逐级降级：
    1. 配置的 tianshu_window_title（排除桌面端污染值「天枢 · Tianshu」）；
       优先在控制台窗口里匹配，控制台枚举不可用时回退全量（用户显式配置是强信号）；
       命中时返回匹配到的实际窗口标题（而非配置子串，避免 find_window_by_title 子串误选）；
    2. CLI 特征控制台窗口（`tianshu-tui`[新版命令行形态] / `npm prefix`[旧版启动
       标记] / `rivet`；Tianshu/天枢 才是桌面端特征）；弱特征（Windows PowerShell
       等终端默认名——CLI 跑工具时 WT 自动标题显示子进程名）须系统里确有 CLI
       进程才认；只在控制台窗口里匹配——标题含 npm 的浏览器/编辑器等诱饵窗口
       不是控制台类，绝不可选（fail-open 反例）；仅当控制台枚举不可用才回退全量；
    3. 启动 CLI（rivet）后按 CLI 特征窗口名轮询定位（过滤桌面端；第 2 级已确认
       无 CLI 特征窗口才走到这里，启动后出现的 CLI 特征窗口就是刚启动的 CLI，
       宁缺毋滥——不得把提示词发给桌面端/诱饵窗口）。
    """
    list_windows_fn = list_windows_fn or _list_windows
    launch_fn = launch_fn or launch_tianshu
    sleep_fn = sleep_fn or time.sleep
    console_windows_fn = console_windows_fn or _console_windows
    cfg = cfg or {}
    _is_desktop = _is_desktop_window  # 模块级证据式判定（不再按 tianshu 一刀切）

    def _cli_candidates():
        """CLI 窗口候选：(title, pid) 列表；控制台枚举不可用（None）时回退全量。

        返回 None（枚举不可用）以外的任何值均视为"枚举正常"——空列表表示系统里
        没有控制台窗口，此时不得从全量里挑诱饵（宁缺毋滥，走下一级启动 CLI）。"""
        cons = console_windows_fn()
        if cons is None:
            return _norm_console_entries(list_windows_fn())
        return _norm_console_entries(cons)

    # 1) 用户手动配置的窗口标题（污染值「天枢 · Tianshu」= 桌面端，忽略）。
    #    优先在控制台窗口里匹配（CLI 是 cmd/rivet 控制台，浏览器等诱饵窗口天然排除）；
    #    控制台枚举不可用时回退全量（用户显式配置是强信号）。
    t = (cfg.get("tianshu_window_title") or "").strip()
    if t and not _is_desktop(t):
        try:
            for name, _pid in _cli_candidates():
                if t.lower() in name.lower() and not _is_desktop(name):
                    return name, ""
        except Exception:
            pass

    # 2) CLI 特征窗口：_pick_cli_window 统一判定——标题签名（tianshu-tui /
    #    npm prefix / rivet）优先；弱特征（Windows PowerShell 等终端默认标题）
    #    叠加进程证据且要求候选唯一（多终端窗口时不猜，fail-closed）。
    #    只在控制台窗口里匹配：浏览器/编辑器等诱饵窗口不是控制台类，天然排除
    #    （fail-open 反例 C1）；仅当控制台枚举不可用（None）才回退全量。
    try:
        name = _pick_cli_window(_cli_candidates(), process_has_rivet_fn)
        if name:
            return name, ""
    except Exception:
        pass

    # 3) 启动 CLI + 按 CLI 特征窗口名（npm prefix）轮询定位。
    #    launch 幂等护栏：冷却期内不重复 launch（用户实测：初始化发首轮
    #    提示词时每次 resolve 都走到这里 → 每轮都开新 CLI 窗口，循环不止）。
    #    冷却期内跳过 launch 直接轮询——窗口出现即复用，不重复开窗。
    global _last_launch_mono
    now = time.monotonic()
    if _last_launch_mono is None or now - _last_launch_mono >= _LAUNCH_COOLDOWN_SECONDS:
        ok_launch, detail = launch_fn(cfg)
        if not ok_launch:
            return "", f"首轮提示词准备就绪，但{detail}（点「重试发送」）"
        _last_launch_mono = now
    sleep_fn(3)  # CLI 启动 + 加载工作目录（用户要求：等待缩短到 3 秒，防误以为没反应）
    for _ in range(15):
        try:
            # 直接按 CLI 特征窗口定位（_pick_cli_window：标题多形态签名优先，
            # 弱特征+进程证据要求候选唯一）。只在控制台窗口里匹配
            # （_cli_candidates 控制台优先，枚举不可用时回退全量）——
            # 浏览器/编辑器等诱饵窗口不是控制台类，天然排除。
            # 不依赖"新增窗口差集"——标题与既有窗口重复时差集为空会漏判，
            # 且第 2 级已确认无 CLI 特征窗口才会走到这里，启动后出现的
            # CLI 特征窗口就是刚启动的 CLI。桌面端绝不可选，否则首轮提示词
            # 误发到桌面端窗口（commit c67b995/d0b4ca6 的场景）。
            name = _pick_cli_window(_cli_candidates(), process_has_rivet_fn)
            if name:
                return name, ""
            # 全是桌面端/无关窗口 → 不返回，继续轮询（宁缺毋滥，不得误发）
        except Exception:
            pass
        sleep_fn(1)
    return "", "天枢 CLI 已启动但窗口未出现，请稍后点「重试发送」"


def open_first_prompt(path):
    """用系统默认应用打开首轮提示词文件。"""
    if not path or not os.path.isfile(path):
        return False
    os.startfile(path)
    return True


# ---------- 首次启动一次性引导：/yes 全自动（持久化，重启后仍生效） ----------
# 用户实测：CLI 输入 /yes 即开全自动，且重启后不用再输——替代旧的
# 每次初始化发 `/permission yolo confirm`（会话级）与 config 级
# set-approval 机制。首启引导一次性完成，此后初始化不再切 YOLO。

def close_window_by_title(title, sleep_fn=None):
    """按标题子串定位顶层窗口 → WM_CLOSE 优雅关闭；0.5s 后仍在则 taskkill /PID 强杀兜底。

    cmd /k 运行批处理时点 X 可能弹「Terminate batch job (Y/N)?」不退出——
    WM_CLOSE 后窗口仍在时按窗口进程 PID 强杀兜底（引导场景 CLI 无未完成
    任务，强杀不丢数据）。PID 方式比 taskkill WINDOWTITLE 过滤可靠。
    关键：user32 调用的 argtypes 必须显式声明——64 位系统 HWND 是 64 位
    指针，不声明按 c_int 传参会截断，PostMessageW/taskkill 全部无效
    （历史缺陷：/yes 发完 CLI 窗口一直不关）。返回是否定位到并尝试关闭。
    """
    sleep_fn = sleep_fn or time.sleep
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
    except Exception:
        return False
    # 64 位句柄/指针签名：防 HWND 截断（关窗失效的根因）。
    # 仅对真实 ctypes 函数对象有效；测试注入的 Fake（普通方法）设置失败无害。
    try:
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                    ctypes.POINTER(wintypes.DWORD)]
    except (AttributeError, TypeError):
        pass
    found = []

    def _cb(hwnd, _lp):
        try:
            buf = ctypes.create_unicode_buffer(512)
            n = user32.GetWindowTextW(hwnd, buf, 512)
            if n > 0 and title.lower() in buf.value.lower():
                found.append(hwnd)
        except Exception:
            pass
        return True

    try:
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(_cb)
        user32.EnumWindows(enum_proc, 0)
    except Exception:
        return False
    if not found:
        # 标题匹配失败回退（用户实测）：/yes 发送后 CLI 窗口标题可能从
        # 「npm prefix」变成「Windows PowerShell」——旧逻辑按发送前标题
        # 枚举找不到 → 窗口关不掉。回退到「CLI 特征的控制台窗口」：
        # 无论标题变成什么，只要 CLI 进程在就能定位（_is_cli_feature
        # 弱特征 = 终端默认标题 + 进程证据）。用户自己开的 PowerShell
        # 系统里无 CLI 进程，不会被误关（fail-open 反例）。
        try:
            for name, pid in _norm_console_entries(_console_windows() or []):
                if _is_desktop_window(name):
                    continue
                if _is_cli_feature(name, pid):
                    def _cb2(hwnd, _lp):
                        try:
                            b2 = ctypes.create_unicode_buffer(512)
                            n2 = user32.GetWindowTextW(hwnd, b2, 512)
                            if n2 > 0 and name.lower() in b2.value.lower():
                                found.append(hwnd)
                        except Exception:
                            pass
                        return True
                    enum2 = ctypes.WINFUNCTYPE(
                        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(_cb2)
                    user32.EnumWindows(enum2, 0)
                    break
        except Exception:
            return False
    if not found and _last_launch_pid:
        # 最后一环兜底（用户实测 /yes 后窗口没关的根因）：窗口进程
        # （conhost/WT）与 CLI 进程无父子关系——弱特征进程树验证从窗口
        # PID 查不到 rivet，标题又已变化 → 前面全失败。但 CLI 是
        # launch_tianshu 启动的，cmd PID 已记录——taskkill /F /T 杀
        # 进程树（cmd→node 全杀），窗口必关（引导场景 CLI 无未完成任务）。
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(_last_launch_pid)],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return True
        except Exception:
            return False
    if not found:
        return False
    for hwnd in found:
        try:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE（等效用户点 X）
        except Exception:
            pass
    sleep_fn(0.5)  # 用户建议：输入 /yes 后等待 0.5s 再关闭
    # 窗口可能仍存在（cmd 批处理确认框）→ 按窗口进程 PID 强杀兜底
    pids = set()
    for hwnd in found:
        try:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                pids.add(pid.value)
        except Exception:
            pass
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
    return True


def send_yes_and_close(title, sleep_fn=None, send_fn=None, close_fn=None):
    """向 CLI 窗口发送 /yes（全自动持久化，重启后仍生效），等待后关闭窗口。

    返回 bool。天枢 agent 首轮对话需按两次回车才提交（与首轮提示词同）——
    enter_times=2；若目标窗口已进入会话（非首轮）二次回车也不影响。
    """
    sleep_fn = sleep_fn or time.sleep
    send_fn = send_fn or _send_trigger_to_window
    close_fn = close_fn or close_window_by_title
    if not title:
        return False
    ok = send_fn(title, "/yes", hold=2.0, enter_times=2)
    sleep_fn(1.0)  # 等 CLI 完成模式切换
    close_fn(title)
    return ok


def _install_tianshu_cli(timeout=180):
    """自动安装天枢 CLI：npm install -g tianshu-tui。返回 bool。"""
    import shutil
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        return False
    try:
        r = subprocess.run(
            [npm, "install", "-g", "tianshu-tui"],
            capture_output=True, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


def _guide_dialog(parent, dialog_fn, title, text, buttons=None):
    """模态引导弹窗（PySide6 QMessageBox 自定义按钮）。返回点击按钮文本。

    buttons: [(文本, QMessageBox.ButtonRole), ...]，默认仅「确认完成」。
    """
    if dialog_fn is not None:
        return dialog_fn(title, text, buttons)
    from PySide6.QtWidgets import QMessageBox
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    for label, role in (buttons or [("确认完成", QMessageBox.AcceptRole)]):
        if role is None:  # 调用方不关心角色语义时给 ActionRole 兜底
            role = QMessageBox.ActionRole
        box.addButton(label, role)
    box.exec()
    clicked = box.clickedButton()
    return clicked.text() if clicked is not None else ""


def _find_npm_prefix_window(console_windows_fn=None, process_has_rivet_fn=None):
    """在控制台窗口里找天枢 CLI 窗口（供首启引导发 /yes 用）。

    匹配交给 _is_cli_feature 统一处理，覆盖三代标题形态：
    - 新版：WT 默认标题 = 运行命令行（含 `tianshu-tui`）；跑工具时自动标题
      变成子进程名（Windows PowerShell）→ 弱特征 + 进程证据兜底
    - 旧版：小漓启动时 `title npm prefix` 的精确短语（裸 "npm" 不认——用户
      手动开的 npm 子命令窗口「npm root」含 npm 但不是 CLI，误发 /yes 会打错
      窗口，真机日志 14:24:44 的故障链）
    桌面端窗口按 _is_desktop_window 排除（不能按 tianshu 字样一刀切——新版
    CLI 标题含 tianshu-tui 会被误杀）。返回标题或 None。
    """
    try:
        for name, pid in _norm_console_entries(
                (console_windows_fn or _console_windows)()):
            if _is_cli_feature(name, pid, process_has_rivet_fn) \
                    and not _is_desktop_window(name):
                return name
    except Exception:
        pass
    return None


_GUIDE_PROMPT_TEXT = (
    "已为您打开天枢 CLI（命令行窗口）。\n\n"
    "请在弹出的窗口中完成配置：\n"
    "  ① 选择模型\n"
    "  ② 输入 API key\n"
    "  ③ 按回车确认\n\n"
    "以上操作需在 CLI 窗口内手动完成（小漓只负责打开窗口与提示）。\n"
    "配置完成后点击「确认完成」，小漓将检测到 CLI 并自动开启全自动模式"
    "（发送 /yes），然后关闭 CLI 窗口。\n\n"
    "⚠ 重要：小漓的聊天/任务功能使用桌面端自己的 API 配置——请同时到"
    "「模型」页为 DeepSeek 填入相同的 API key，否则桌面端调用会报 401。\n\n"
    "若 CLI 窗口未出现，请手动打开命令行窗口并输入 rivet 启动。"
)


def run_first_run_guide(cfg, parent=None, cfg_path=None,
                        detect_fn=None, install_fn=None, launch_fn=None,
                        send_fn=None, close_fn=None, sleep_fn=None,
                        console_windows_fn=None, dialog_fn=None):
    """工作文件夹保存后的一次性引导（首次/换目录都触发），返回 True = 完成。

    流程（用户实测确认的正确交互——CLI 未进会话前不在窗口检测范围，
    不能在确认前定位窗口；自动监控触发 /yes 是错误设计）：
    ① CLI 检测（rivet 命令，无则 npm install -g tianshu-tui 自动安装）
    ② launch_tianshu 打开 CLI（帮助用户打开，不定位窗口）
    ③ 模态弹窗：指导用户选模型/输 API key/回车确认 → 点「确认完成」
    ④ 确认后按 CLI 特征（tianshu-tui / npm prefix / 终端默认标题+进程证据）
       检查窗口 → 无则提示重试
    ⑤ 找到 → 发 /yes（两次回车，全自动持久化）→ 关闭 CLI 窗口
    ⑥ config 标记 tianshu_guided=True（此后初始化不再切 YOLO）

    用户取消 / 确认后未检测到窗口 / 发送失败 → 返回 False（设置页可重跑）。
    """
    from xiaoli_app import config_store
    sleep_fn = sleep_fn or time.sleep
    if detect_fn is None:
        def detect_fn():
            import shutil
            return shutil.which("rivet") or shutil.which("rivet.cmd")
    # ① CLI 检测/安装
    rivet = detect_fn()
    if not rivet:
        ok_install = (install_fn() if install_fn is not None else _install_tianshu_cli())
        if not ok_install:
            _guide_dialog(
                parent, dialog_fn, "未找到天枢 CLI",
                "未检测到 rivet 命令，自动安装失败。\n"
                "请手动执行：npm install -g tianshu-tui\n"
                "安装完成后重新打开本程序。")
            return False
    # ② 打开 CLI（用户配置模型/API key；CLI 未进会话前不做窗口定位）
    if launch_fn is None:
        launch_fn = launch_tianshu
    ok_launch, detail = launch_fn(cfg)
    if not ok_launch:
        _guide_dialog(parent, dialog_fn, "无法打开天枢 CLI",
                      f"{detail}\n\n请手动打开命令行窗口并输入 rivet 启动，"
                      "完成配置后回到本窗口点击「确认完成」。")
    # ③ 弹操作提示窗，等用户完成配置后点「确认完成」
    choice = _guide_dialog(
        parent, dialog_fn, "天枢 CLI 配置引导",
        _GUIDE_PROMPT_TEXT,
        [("确认完成", None), ("取消", None)])
    if choice != "确认完成":
        return False  # 取消：不标记，设置页可重跑
    # ④ 确认后按 CLI 特征检查窗口（配置完成、CLI 进程在跑）
    title = _find_npm_prefix_window(console_windows_fn)
    if not title:
        _guide_dialog(
            parent, dialog_fn, "未检测到天枢 CLI 窗口",
            "未找到天枢 CLI 窗口。\n"
            "请确认已在 CLI 窗口中完成配置（选择模型、输入 API key、按回车确认），"
            "然后在设置页点击「重新引导天枢 CLI」重试。")
        return False
    # ⑤ 发 /yes → 关窗
    ok = send_yes_and_close(title, sleep_fn=sleep_fn, send_fn=send_fn, close_fn=close_fn)
    if not ok:
        _guide_dialog(parent, dialog_fn, "发送失败", "未能向天枢 CLI 发送 /yes，请重试。")
        return False
    # ⑥ 标记
    cfg["tianshu_guided"] = True
    try:
        config_store.save_config(cfg, cfg_path or "config.json")
    except OSError:
        pass
    return True
