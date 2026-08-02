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

    # ---- 天枢：配置目录 → 自动探测 → 窗口 ----
    tianshu_dir = (cfg.get("tianshu_install_dir") or "").strip()
    if tianshu_dir:
        # 显式指定安装目录：只认该目录（避免用户已配置却误判自动探测）
        exe = os.path.join(tianshu_dir, TIANSHU_EXE)
        tianshu_ok = os.path.isfile(exe)
        detail = f"已安装：{exe}" if tianshu_ok else f"未找到 {TIANSHU_EXE}：{tianshu_dir}"
    else:
        found = detect_tianshu_dir()
        if found:
            tianshu_ok, detail = True, f"已安装：{os.path.join(found, TIANSHU_EXE)}"
        else:
            tianshu_ok, detail = False, f"未找到天枢（{TIANSHU_EXE}）"
    tianshu_win = ""
    try:
        for name in _list_windows():
            if "天枢" in name or "Tianshu" in name:
                tianshu_win = name
                break
    except Exception:
        pass
    out["tianshu"] = {"ok": tianshu_ok, "detail": detail, "window": tianshu_win}

    # ---- 首轮提示词 ----
    fp = (cfg.get("first_prompt_path") or "").strip()
    out["first_prompt"] = {
        "ok": bool(fp) and os.path.isfile(fp),
        "detail": fp if os.path.isfile(fp) else f"文件不存在：{fp}",
        "path": fp,
    }
    return out


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
