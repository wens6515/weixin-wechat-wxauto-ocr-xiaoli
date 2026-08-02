# -*- coding: utf-8 -*-
"""环境检测与一键安装：微信/天枢/首轮提示词检测 + 天枢 zip 流式下载安全解压。

面向小白用户：软件内检测依赖 → 缺失时一键下载安装（进度可视化）。
"""
import os
import subprocess
import tempfile
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


def launch_tianshu(cfg):
    """启动天枢 CLI（rivet）：在 tianshu_workdir 下开新 cmd 窗口（标题 Tianshu）运行 rivet。

    返回 (ok, detail)。rivet 命令经 shutil.which 定位（npm 全局安装 tianshu-tui）。
    窗口标题固定 "Tianshu"——与 _list_windows 的"天枢/Tianshu"匹配逻辑兼容。
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
        # CREATE_NEW_CONSOLE：Windows 原生新开控制台窗口（可靠）；
        # title Tianshu 设置窗口标题（窗口匹配用）；& 顺序执行 rivet；cmd /k 保持窗口。
        # 实测：Popen(list) 引号二次转义 → "系统找不到文件 \Tianshu\"；
        #       shell=True 字符串在 python 进程下也不弹窗（仅 Git Bash 手工调用有效）。
        subprocess.Popen(
            ["cmd", "/k", "title Tianshu & rivet"],
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
                if norm.startswith("..") or os.path.isabs(norm):
                    raise ValueError(f"拒绝非法压缩成员: {m.filename}")
            zf.extractall(dest_dir)
    finally:
        try:
            os.unlink(tmp_zip.name)
        except OSError:
            pass
    return find_tianshu_dir(dest_dir) or dest_dir


def _send_trigger_to_window(title, command, hold=0.5):
    """激活窗口 → 剪贴板粘贴 → 回车（延迟 import，测试可 mock）。"""
    from xiaoli_bot import send_trigger_to_window
    return send_trigger_to_window(title, command, hold)


def send_prompt_to_tianshu(text, window_title):
    """把文字发送给天枢窗口（激活→剪贴板粘贴→回车）。返回 bool。"""
    if not window_title or not text:
        return False
    return _send_trigger_to_window(window_title, text)


def open_first_prompt(path):
    """用系统默认应用打开首轮提示词文件。"""
    if not path or not os.path.isfile(path):
        return False
    os.startfile(path)
    return True
