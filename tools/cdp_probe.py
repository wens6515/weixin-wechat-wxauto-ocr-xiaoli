#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CDP / Accessibility 注入探针 — 微信 4.x (Chromium 渲染) 自动化通道验证
=====================================================================

背景: 微信 4.1.12 把 UI 渲染切换为 "Qt 壳 + Chromium 内容", UIA 控件树默认关闭,
      wxauto4 整条 UIA 通道失效。本探针验证两条未实测的高杠杆通道:

      ① --remote-debugging-port=<port>
         注入后 DevToolsActivePort 是否生成? /json 是否可达?
         Runtime.evaluate 能否读 DOM?
      ② --force-renderer-accessibility
         注入后 UIA 子树是否恢复业务控件?

用法:
  python tools/cdp_probe.py check [--port 9222] [--wechat-path PATH]
                                  [--user-data-dir DIR] [--timeout 90]
      只读检查当前状态(不重启微信): DevToolsActivePort 文件 / 端口可达 /
      CDP 读 DOM / UIA 子树业务控件数

  python tools/cdp_probe.py inject --args="--remote-debugging-port=9222"
      结束微信 → 带 args 重启 → 等待就绪 → 自动跑 check 验证
      例: --args="--force-renderer-accessibility"
          --args="--remote-debugging-port=9222 --force-renderer-accessibility"

  python tools/cdp_probe.py restore
      结束微信 → 不带参数重启(恢复正常启动, 用于实验后还原)

安全: inject/restore 会结束当前运行的全部 Weixin.exe 进程(用户正在使用的
      微信会话会被中断)。微信 4.x 登录态存于 xwechat/login, 是否自动登录
      取决于用户设置——脚本会检测并报告登录状态。
      破坏性参数(如 --no-sandbox 之外的自定义 flag)请自行评估风险。

输出: 人类可读 + 末尾 JSON 汇总(便于决策文档引用)。
"""

import argparse
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ----------------------------- 常量与默认值 -----------------------------

DEFAULT_WECHAT_PATH = r"D:\腾讯\Weixin\Weixin.exe"
DEFAULT_USER_DATA_DIR = os.path.join(
    os.environ.get("APPDATA", ""), "Tencent", "xwechat", "radium", "web"
)
DEFAULT_PORT = 9222
PROC_NAME = "Weixin.exe"
DEVTOLS_FILE = "DevToolsActivePort"

def _detect_ascii():
    """GBK/ASCII 控制台(Windows cmd 默认)无法打印 emoji, 降级为 ASCII 标记。"""
    try:
        enc = (sys.stdout.encoding or "").lower()
        return "utf" not in enc
    except Exception:
        return True


USE_ASCII = _detect_ascii()
PASS = "[OK] " if USE_ASCII else "OK "
FAIL = "[FAIL] " if USE_ASCII else "FAIL "
WARN = "[WARN] " if USE_ASCII else "WARN "


def log(msg):
    print(msg, flush=True)


# ----------------------------- 进程管理 -----------------------------


def wechat_pids():
    """返回当前运行的 Weixin.exe 进程 PID 列表(只读)。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {PROC_NAME}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception as exc:
        log(f"{WARN} tasklist 失败: {exc}")
        return []
    pids = []
    for line in out.strip().splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == PROC_NAME.lower():
            pids.append(int(parts[1]))
    return pids


def wechat_ready():
    """是否已进入主界面: 出现 Chromium 子进程(主进程会 spawn --type=wx* 子进程)。

    登录窗口阶段只有主进程, 登录后 spawn 子进程(与可行性报告一致)。
    """
    pids = wechat_pids()
    return len(pids) >= 2


def kill_wechat():
    """结束全部 Weixin.exe 进程。返回被杀 PID 数。"""
    pids = wechat_pids()
    if not pids:
        log("没有运行中的 Weixin.exe")
        return 0
    log(f"结束 {PROC_NAME} 进程: {pids}")
    subprocess.run(["taskkill", "/F", "/IM", PROC_NAME], capture_output=True, text=True)
    deadline = time.time() + 20
    while time.time() < deadline:
        if not wechat_pids():
            log("已全部退出")
            return len(pids)
        time.sleep(0.5)
    log(f"{WARN} 20s 内进程未完全退出, 剩余: {wechat_pids()}")
    return len(pids)


def start_wechat(extra_args, wechat_path):
    """以 extra_args 启动微信(不等待)。"""
    cmd = [wechat_path] + extra_args
    log(f"启动: {' '.join(repr(c) for c in cmd)}")
    workdir = os.path.dirname(wechat_path) or None
    subprocess.Popen(cmd, cwd=workdir)
    time.sleep(2)  # 给进程创建留时间


def wait_ready(timeout):
    """轮询等待微信就绪(主进程 + 子进程出现 = 已登录进主界面)。"""
    log(f"等待微信就绪(最长 {timeout}s)...")
    deadline = time.time() + timeout
    last_n = 0
    while time.time() < deadline:
        n = len(wechat_pids())
        if n != last_n:
            log(f"  Weixin.exe 进程数: {n}")
            last_n = n
        if n >= 2:
            log(f"{PASS} 微信已就绪(进入主界面)")
            return True
        time.sleep(2)
    log(f"{WARN} 超时未见主界面子进程, 可能停留在登录窗口(需扫码)或启动失败")
    return False


# ----------------------------- CDP 验证 -----------------------------


def devtools_port_file(user_data_dir):
    path = os.path.join(user_data_dir, DEVTOLS_FILE)
    if not os.path.isfile(path):
        return None, None
    with open(path, "r", encoding="utf-8") as fh:
        lines = [l.strip() for l in fh.readlines() if l.strip()]
    if not lines:
        return None, None
    try:
        return int(lines[0]), lines[1] if len(lines) > 1 else None
    except ValueError:
        return None, None


def http_get(url, timeout=5):
    req = urllib.request.Request(url, headers={"User-Agent": "cdp-probe/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def http_json(url, timeout=5):
    status, body = http_get(url, timeout)
    return status, json.loads(body)


def _ws_handshake(host, port, path):
    sock = socket.create_connection((host, port), timeout=6)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("WebSocket 握手连接被关闭")
        buf += chunk
    head = buf.split(b"\r\n", 1)[0]
    if b" 101 " not in head:
        raise RuntimeError(f"WebSocket 升级失败: {head.decode(errors='replace')}")
    return sock


def _ws_send(sock, payload):
    data = payload.encode("utf-8")
    mask = os.urandom(4)
    header = bytearray([0x81])  # FIN + text
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    sock.sendall(bytes(header) + masked)


def _ws_recv(sock):
    """读取一个完整消息(处理分片)。假定 server→client 无 mask。"""
    payload = b""
    while True:
        hdr = sock.recv(2)
        if len(hdr) < 2:
            raise RuntimeError("WebSocket 连接关闭")
        fin = hdr[0] & 0x80
        opcode = hdr[0] & 0x0F
        n = hdr[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", sock.recv(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", sock.recv(8))[0]
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise RuntimeError("WebSocket 数据读取中断")
            data += chunk
        if opcode in (0x1, 0x2):  # text / binary 起始帧
            payload = data
        elif opcode == 0x0:       # continuation
            payload += data
        if fin:
            return payload.decode("utf-8", errors="replace")


def cdp_evaluate(ws_url, expression, timeout=15):
    """连接 page target 的 webSocketDebuggerUrl, 执行 Runtime.evaluate。

    返回 (ok, result_value, error_text)。
    """
    try:
        path = ws_url.split("/", 3)[3]
        sock = _ws_handshake("127.0.0.1", DEFAULT_PORT, "/" + path)
        msg = json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True},
        })
        _ws_send(sock, msg)
        sock.settimeout(timeout)
        resp = json.loads(_ws_recv(sock))
        sock.close()
        if "error" in resp:
            return False, None, json.dumps(resp["error"])
        result = resp.get("result", {})
        if "exceptionDetails" in result:
            return False, None, json.dumps(result["exceptionDetails"])[:300]
        value = result.get("result", {}).get("value")
        return True, value, None
    except Exception as exc:
        return False, None, str(exc)


def probe_cdp(port, user_data_dir):
    """验证 CDP 通道: DevToolsActivePort 文件 / HTTP /json / Runtime.evaluate。"""
    log(f"\n[CDP 探测] port={port} user_data_dir={user_data_dir}")
    result = {
        "devtools_port_file": False,
        "devtools_file_port": None,
        "json_version": False,
        "json_targets": 0,
        "dom_readable": False,
        "dom_title": None,
        "dom_body_len": None,
    }

    # 1) DevToolsActivePort 文件
    fp, path_hint = devtools_port_file(user_data_dir)
    if fp is not None:
        result["devtools_port_file"] = True
        result["devtools_file_port"] = fp
        log(f"{PASS} DevToolsActivePort 存在: port={fp} browser={path_hint}")
        if fp != port:
            log(f"{WARN} 文件端口 {fp} != 参数端口 {port} (文件为准)")
    else:
        log(f"{FAIL} DevToolsActivePort 不存在: {os.path.join(user_data_dir, DEVTOLS_FILE)}")

    # 2) HTTP /json/version 与 /json
    try:
        st, ver = http_json(f"http://127.0.0.1:{port}/json/version")
        result["json_version"] = True
        log(f"{PASS} /json/version HTTP {st}: Browser={ver.get('Browser', '?')}")
    except Exception as exc:
        log(f"{FAIL} /json/version 不可达: {exc}")
    try:
        st, targets = http_json(f"http://127.0.0.1:{port}/json")
        pages = [t for t in targets if t.get("type") == "page"]
        result["json_targets"] = len(targets)
        log(f"{PASS} /json HTTP {st}: {len(targets)} targets (page={len(pages)})")
        for t in pages[:5]:
            log(f"      page: {t.get('url', '')[:80]} title={t.get('title', '')[:40]!r}")
        if pages:
            ws = pages[0].get("webSocketDebuggerUrl")
            if ws:
                ok, value, err = cdp_evaluate(ws, "document.title")
                result["dom_readable"] = ok
                if ok:
                    result["dom_title"] = value
                    log(f"{PASS} Runtime.evaluate(document.title) = {value!r}")
                else:
                    log(f"{FAIL} Runtime.evaluate 失败: {err}")
                ok2, v2, err2 = cdp_evaluate(
                    ws, "document.body ? document.body.innerText.length : -1"
                )
                if ok2:
                    result["dom_body_len"] = v2
                    log(f"{PASS} body.innerText.length = {v2}")
                else:
                    log(f"{FAIL} body 读取失败: {err2}")
            else:
                log(f"{WARN} page target 无 webSocketDebuggerUrl")
        else:
            log(f"{WARN} 无 page target, 无法做 Runtime.evaluate")
    except Exception as exc:
        log(f"{FAIL} /json 不可达: {exc}")

    return result


# ----------------------------- UIA 验证 -----------------------------


def probe_uia():
    """枚举微信主窗口 UIA 子树, 统计业务控件(非 Window/Pane)。"""
    log("\n[UIA 探测]")
    result = {"main_window": False, "business_controls": 0, "sample": []}
    try:
        import uiautomation as uia
    except ImportError:
        log(f"{WARN} uiautomation 未安装, 跳过 UIA 探测 (pip install uiautomation)")
        return result

    def find_main():
        for w in uia.GetRootControl().GetChildren():
            try:
                cn = w.ClassName or ""
                name = w.Name or ""
                if "Weixin" in cn or "微信" in name:
                    return w
            except Exception:
                continue
        return None

    w = find_main()
    if w is None:
        log(f"{FAIL} 未找到微信主窗口")
        return result
    result["main_window"] = True
    log(f"{PASS} 主窗口: class={w.ClassName!r} name={w.Name!r} handle=0x{w.NativeWindowHandle:x}")

    business = []

    def walk(c, depth, maxd=4):
        if depth > maxd:
            return
        for ch in c.GetChildren():
            try:
                ctn = ch.ControlTypeName or ""
            except Exception:
                ctn = "?"
            if ctn not in ("WindowControl", "PaneControl"):
                business.append(f"{ctn}({ch.Name or ''})")
            walk(ch, depth + 1, maxd)

    walk(w, 1, 4)
    result["business_controls"] = len(business)
    result["sample"] = business[:8]
    if business:
        log(f"{PASS} 子树含 {len(business)} 个业务控件, 前 8: {business[:8]}")
    else:
        log(f"{FAIL} 子树为空壳(仅 Pane), accessibility 未开启或未桥接")
    return result


# ----------------------------- 主流程 -----------------------------


def run_check(port, user_data_dir):
    cdp = probe_cdp(port, user_data_dir)
    uia = probe_uia()
    return {"cdp": cdp, "uia": uia}


def cmd_check(args):
    log("=== 只读检查当前状态(不重启微信) ===")
    log(f"微信进程数: {len(wechat_pids())}  主界面就绪: {wechat_ready()}")
    res = run_check(args.port, args.user_data_dir)
    print("\n=== JSON 汇总 ===")
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_inject(args):
    extra = args.args.split()
    log("=== 注入重启验证 ===")
    log(f"注入参数: {extra}")
    kill_wechat()
    start_wechat(extra, args.wechat_path)
    ready = wait_ready(args.timeout)
    res = run_check(args.port, args.user_data_dir)
    res["login_ok"] = ready
    res["injected_args"] = extra
    if not ready:
        log(f"{WARN} 主界面未就绪 → 重启注入可能要求重新扫码")
    print("\n=== JSON 汇总 ===")
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_restore(args):
    log("=== 恢复正常启动(无注入参数) ===")
    kill_wechat()
    start_wechat([], args.wechat_path)
    ready = wait_ready(args.timeout)
    if not ready:
        log(f"{WARN} 主界面未就绪, 请人工确认登录状态")
    log("还原完成(微信已以默认参数重启)")


def main():
    ap = argparse.ArgumentParser(description="微信 4.x CDP / Accessibility 注入探针")
    ap.add_argument("--wechat-path", default=DEFAULT_WECHAT_PATH,
                    help=f"微信可执行路径 (默认 {DEFAULT_WECHAT_PATH})")
    ap.add_argument("--user-data-dir", default=DEFAULT_USER_DATA_DIR,
                    help=f"Chromium user-data-dir (默认 {DEFAULT_USER_DATA_DIR})")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"CDP 调试端口 (默认 {DEFAULT_PORT})")
    ap.add_argument("--timeout", type=int, default=90, help="等待微信就绪秒数 (默认 90)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="只读检查当前状态")
    p_check.set_defaults(func=cmd_check)

    p_inject = sub.add_parser("inject", help="结束微信→带参数重启→验证")
    p_inject.add_argument("--args", required=True, help="注入的启动参数, 如 --remote-debugging-port=9222")
    p_inject.set_defaults(func=cmd_inject)

    p_restore = sub.add_parser("restore", help="恢复正常启动")
    p_restore.set_defaults(func=cmd_restore)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
