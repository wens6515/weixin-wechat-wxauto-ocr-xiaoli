# -*- coding: utf-8 -*-
"""环境检测与一键安装：微信/天枢/首轮提示词检测 + 天枢 zip 流式下载安全解压。

面向小白用户：软件内检测依赖 → 缺失时一键下载安装（进度可视化）。
"""
import json
import os
import subprocess
import tempfile
import time
import zipfile

import requests

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
5. 不要写 result.json 以外的状态文件——小漓检测到 result.json 就会把成果发回微信并把目录归档。

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

## 归档

小漓检测到 result.json 后，会把任务目录移入 sent\\ 归档，并把成果发回微信。
"""


def _list_windows():
    """窗口名枚举（延迟 import，避免触发 xiaoli_bot 顶层副作用）。"""
    from xiaoli_bot import list_windows
    return [name for name, _h in list_windows()]


def _console_windows():
    """枚举控制台/终端类顶层窗口标题（Win32：ConsoleWindowClass / CASCADIA / mintty / WindowClass_）。

    返回标题列表；Win32 不可用（非 Windows / 枚举失败）时返回 None，调用方据此回退全量匹配。
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
                        titles.append(buf.value.strip())
            except Exception:
                pass
            return True

        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(_cb)
        user32.EnumWindows(enum_proc, 0)
        return [t for t in titles if t]
    except Exception:
        return None


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
            r = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=10)
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


def configure_tianshu_auto_approval(cfg, config_path=None):
    """已有天枢 CLI 时配置为完全自动（YOLO）：任务处理全程无需手动确认。

    天枢 CLI 默认 approval=suggest/Auto——高风险工具（rm/mv/git 写等）仍需
    用户在终端手动回车确认。小漓是无人值守的后台机器人，投递任务后不会有人
    去按回车，任务会卡在确认等待，导致全自动回复链路断裂。
    把 approval 设为 dangerously-skip-permissions（启动即 YOLO）后：
    所有工具自动执行、无刹车无打扰（回滚兜底 /rollback + git 检查点）。
    幂等：config.json 已是该值则跳过；无 rivet 命令返回 (False, 原因)。

    实现走 `rivet config set-approval` 子命令（实测非 TTY 可用，小漓无终端）；
    config.json 位置与 README 一致：Windows 为 %LOCALAPPDATA%\\.rivet。
    返回 (ok, detail)。
    """
    import shutil
    rivet = shutil.which("rivet") or shutil.which("rivet.cmd")
    if not rivet:
        return False, "未找到 rivet 命令（无需配置，或先 npm install -g tianshu-tui）"
    target = "dangerously-skip-permissions"
    # 幂等：读 config.json，已配置则跳过（不重复写盘/执行）
    # 数据根有多个：CLI 用 %LOCALAPPDATA%\.rivet（rivet logs 实测）；
    # 桌面端便携版用 exe 旁 TianshuData\.rivet。全部配置，任一缺失都补。
    if config_path is None:
        data_root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        config_path = os.path.join(data_root, ".rivet", "config.json")
    all_configured = True
    for cp in _tianshu_config_paths(config_path):
        try:
            if os.path.isfile(cp):
                with open(cp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                agent = data.get("agent") if isinstance(data, dict) else None
                current = agent.get("approval") if isinstance(agent, dict) else None
                if current == target:
                    continue  # 该数据根已配置
        except Exception:
            pass
        all_configured = False
    if all_configured:
        return True, "天枢 CLI 已是完全自动（YOLO），无需重复配置"
    try:
        r = subprocess.run(
            [rivet, "config", "set-approval", target],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=30)
        if r.returncode == 0:
            return True, "天枢 CLI 已配置为完全自动（YOLO），任务处理不再需要手动确认"
        return False, f"配置天枢 CLI 自动模式失败（{r.returncode}）"
    except Exception as e:
        return False, f"配置天枢 CLI 自动模式失败: {e}"


def _tianshu_config_paths(primary):
    """天枢各数据根的 config.json 路径（CLI 主根 + 桌面端便携根）。

    桌面端便携版（D:\\AI\\Tianshu\\TianshuData\\.rivet）的 approval 也必须
    配置——用户可能在桌面端而非 CLI 运行天枢。
    """
    paths = [primary]
    for cand in (r"D:\AI\Tianshu", os.path.expanduser("~")):
        p = os.path.join(cand, "TianshuData", ".rivet", "config.json")
        if os.path.isfile(p) and p not in paths:
            paths.append(p)
    return paths


def launch_tianshu(cfg):
    """启动天枢 CLI（rivet）：在 tianshu_workdir 下开新 cmd 窗口运行 rivet。

    返回 (ok, detail)。rivet 命令经 shutil.which 定位（npm 全局安装 tianshu-tui）。
    窗口标题保持 CLI 自然值（实测为 npm prefix，含 npm）——不得用 title 设为
    "Tianshu"：那会与 _is_desktop 的 tianshu 关键词冲突，把 CLI 误判为桌面端，
    首轮提示词发不出去或误发到桌面端窗口。
    """
    import shutil
    import subprocess
    rivet = shutil.which("rivet") or shutil.which("rivet.cmd")
    if not rivet:
        return False, "未找到 rivet 命令，请先执行：npm install -g tianshu-tui"
    workdir = (cfg.get("tianshu_workdir") or "").strip() or os.path.expanduser("~")
    try:
        os.makedirs(workdir, exist_ok=True)
    except OSError:
        pass
    try:
        # CREATE_NEW_CONSOLE：Windows 原生新开控制台窗口（可靠）；cmd /k 保持窗口。
        # 窗口标题保持 CLI 自然值（实测为 npm prefix，含 npm）——不设 title Tianshu，
        # 避免与 _is_desktop 的 tianshu 关键词冲突（CLI 被误判桌面端、提示词误发）。
        # 实测：Popen(list) 引号二次转义 → "系统找不到文件 \Tianshu\"；
        #       shell=True 字符串在 python 进程下也不弹窗（仅 Git Bash 手工调用有效）。
        # RIVET_PLAN_MODE_SUGGEST=0：关闭复杂任务自动进入 Plan Mode——
        # 无人值守场景任务不能卡在 plan 审批（README：默认 auto 命中多模块/
        # 重构/安全任务时自主进入，等 /plan-approve 确认）。
        subprocess.Popen(
            ["cmd", "/k", "set RIVET_PLAN_MODE_SUGGEST=0 && rivet"],
            cwd=workdir,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
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
    """
    if not window_title or not text:
        return False
    return _send_trigger_to_window(window_title, text, enter_times=2)


def resolve_cli_window(cfg, list_windows_fn=None, launch_fn=None, sleep_fn=None,
                       console_windows_fn=None):
    """定位天枢 CLI 窗口标题，返回 (title, detail)。

    title 非空 = 找到可发送的 CLI 窗口；空 = detail 含失败原因（launch 失败/窗口未出现）。
    三级定位，逐级降级：
    1. 配置的 tianshu_window_title（排除桌面端污染值——标题含 tianshu/天枢 的窗口是桌面端）；
       优先在控制台窗口里匹配，控制台枚举不可用时回退全量（用户显式配置是强信号）；
       命中时返回匹配到的实际窗口标题（而非配置子串，避免 find_window_by_title 子串误选）；
    2. CLI 特征控制台窗口（标题含 npm/rivet 且非桌面端——CLI 窗口标题实测为 npm prefix，
       是 cmd/rivet 启动的控制台窗口；Tianshu/天枢 是桌面端特征，不在此列）；
       只在控制台窗口里匹配——标题含 npm 的浏览器/编辑器等诱饵窗口不是控制台类，
       绝不可选（fail-open 反例）；仅当控制台枚举不可用（None）才回退全量；
    3. 启动 CLI（rivet）后按 CLI 特征窗口名（npm prefix）轮询定位
       （过滤桌面端；第 2 级已确认无 CLI 特征窗口才走到这里，启动后出现的
       CLI 特征窗口就是刚启动的 CLI，宁缺毋滥——不得把提示词发给桌面端/诱饵窗口）。
    """
    list_windows_fn = list_windows_fn or _list_windows
    launch_fn = launch_fn or launch_tianshu
    sleep_fn = sleep_fn or time.sleep
    console_windows_fn = console_windows_fn or _console_windows
    cfg = cfg or {}

    def _is_desktop(name):
        """桌面端窗口特征：中文「天枢」、纯英文 Tianshu、app.tianshu.* Electron
        辅助窗口、tianshu-desktop 进程窗口——统一按 tianshu 关键词识别。
        纯英文标题（Tianshu）漏判会被第 2 级 CLI 特征误选，首轮提示词发错目标。"""
        return "天枢" in name or "tianshu" in name.lower()

    def _is_cli_feature(name):
        low = name.lower()
        return "npm" in low or "rivet" in low

    def _cli_candidates():
        """CLI 窗口候选：控制台类窗口标题；控制台枚举不可用（None）时回退全量。

        返回 None（枚举不可用）以外的任何值均视为"枚举正常"——空列表表示系统里
        没有控制台窗口，此时不得从全量里挑诱饵（宁缺毋滥，走下一级启动 CLI）。"""
        cons = console_windows_fn()
        if cons is None:
            return list_windows_fn()
        return cons

    # 1) 用户手动配置的窗口标题（污染值「天枢 · Tianshu」= 桌面端，忽略）。
    #    优先在控制台窗口里匹配（CLI 是 cmd/rivet 控制台，浏览器等诱饵窗口天然排除）；
    #    控制台枚举不可用时回退全量（用户显式配置是强信号）。
    t = (cfg.get("tianshu_window_title") or "").strip()
    if t and not _is_desktop(t):
        try:
            for name in _cli_candidates():
                if t.lower() in name.lower() and not _is_desktop(name):
                    return name, ""
        except Exception:
            pass

    # 2) CLI 特征窗口（npm/rivet——CLI 窗口标题实测为 npm prefix；Tianshu 是桌面端特征）。
    #    只在控制台窗口里匹配：标题含 npm 的浏览器/编辑器等诱饵窗口不是控制台类，
    #    绝不可选（fail-open 反例 C1）；仅当控制台枚举不可用（None）才回退全量。
    try:
        for name in _cli_candidates():
            if _is_desktop(name):
                continue
            if _is_cli_feature(name):
                return name, ""
    except Exception:
        pass

    # 3) 启动 CLI + 按 CLI 特征窗口名（npm prefix）轮询定位
    ok_launch, detail = launch_fn(cfg)
    if not ok_launch:
        return "", f"首轮提示词准备就绪，但{detail}（点「重试发送」）"
    sleep_fn(8)  # CLI 启动 + 加载工作目录
    for _ in range(15):
        try:
            # 直接按 CLI 特征窗口名定位：天枢 CLI 窗口标题实测为 npm prefix
            # （含 npm/rivet）。只在控制台窗口里匹配（_cli_candidates 控制台优先，
            # 枚举不可用时回退全量）——标题含 npm 的浏览器/编辑器等诱饵窗口
            # 不是控制台类，天然排除（test_skips_decoy_npm_window 场景）。
            # 不依赖"新增窗口差集"——标题与既有窗口重复时差集为空会漏判
            # （test_stage3_duplicate_title_fallback 场景），且第 2 级已确认
            # 无 CLI 特征窗口才会走到这里，启动后出现的 CLI 特征窗口就是刚
            # 启动的 CLI。桌面端（天枢/Tianshu）绝不可选，否则首轮提示词
            # 误发到桌面端窗口（commit c67b995/d0b4ca6 的场景）。
            for name in _cli_candidates():
                if _is_desktop(name):
                    continue
                if _is_cli_feature(name):
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
