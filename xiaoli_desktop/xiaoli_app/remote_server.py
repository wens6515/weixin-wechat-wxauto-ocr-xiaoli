# -*- coding: utf-8 -*-
"""远程面板（默认关）：把 BridgeApi 方法面以 HTTP 暴露出来，同网设备
（手机/平板浏览器）看状态、日志、触发器，暂停/恢复回复。

传输设计：
- 纯标准库（ThreadingHTTPServer）——不新增安装包依赖，PyInstaller 零改动；
- REST = POST /api/<方法名>，body {"args": [...]}，返回桥方法的原样 dict
  （与 pywebview js_api 同一返回契约）；
- 推送 = GET /events（SSE）：pywebview 走 evaluate_js 注入 window.__push，
  远程走 SSE 转 window.__push——前端两种通道进同一个处理函数，页面零分叉；
- 静态 = webui 目录白名单（index.html / app.js / api.js / style.css / assets），
  webui 本就支持浏览器直开，远程模式复用同一份前端。

安全边界（用户定案）：
- 默认关；开启 = 显式设置项，token 首次开启自动生成（secrets，设置页可重置，
  旧链接立即失效）；
- 默认只绑 127.0.0.1；「局域网开放」是独立开关（绑全部网卡）——手机访问
  才需要它。不做公网映射：无 HTTPS（局域网明文 + token），出外网走
  Tailscale 之类组网；
- 方法白名单：quit_app / win_hide（进程退出与藏窗口 = 远程断联）/ 本地
  对话框类（pick_* / import_wallpaper）/ reveal_path（在服务器开资源管理器）
  / install_tianshu 不可远程调用；下划线方法一律不暴露。
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import queue
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("xiaoli")

# 远程不可调用的桥方法（本地语义：退出/藏窗口/原生对话框/服务器开窗/装机；
# 基础设施：stop 会断掉远程连接自身、push/notify 能伪造事件流、bind_* 改
# 内部引用——一律不暴露）
REMOTE_EXCLUDED = frozenset({
    "quit_app", "win_hide", "pick_folder", "pick_file", "import_wallpaper",
    "reveal_path", "install_tianshu",
    "stop", "start_push_loop", "push", "notify", "bind_window", "bind_tray",
})

# webui 静态白名单：路径 → 文件名（assets 整目录放行——前端展示图）
_STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/api.js": "api.js",
    "/style.css": "style.css",
}
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


def lan_ip() -> str:
    """本机局域网 IP（UDP connect 探测默认路由出口，不发任何包）；失败回环。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def remote_allowed(bridge) -> dict:
    """BridgeApi 的远程白名单 {方法名: callable}：公开方法剔除黑名单。"""
    out = {}
    for name in dir(bridge):
        if name.startswith("_") or name in REMOTE_EXCLUDED:
            continue
        fn = getattr(bridge, name)
        if callable(fn):
            out[name] = fn
    return out


class RemoteHub:
    """push 事件订阅 hub。SSE 订阅者各持一个有界队列，publish 非阻塞投递
    （慢消费者丢事件：tick 心跳 1s 一条，丢一条下一轮就补上，不阻塞主推送）。"""

    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=256)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def publish(self, evt: str, payload: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait((evt, payload))
            except queue.Full:
                pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


class _RemoteHandler(BaseHTTPRequestHandler):
    """请求处理：静态白名单 / SSE 推送 / REST 方法面。token 逐请求校验
    （X-Auth-Token 头或 ?t= 查询参数——浏览器导航带不了自定义头，链接里
    带查询参数；hmac.compare_digest 防时序侧信道）。"""

    server_version = "XiaoLiRemote"

    # ---- 基础设施 ----

    def _authed(self, qs: dict) -> bool:
        token = getattr(self.server, "token", "")
        if not token:
            return False
        given = ""
        if self.headers.get("X-Auth-Token"):
            given = str(self.headers.get("X-Auth-Token"))
        elif qs.get("t"):
            given = str(qs["t"][0])
        return bool(given) and hmac.compare_digest(given, token)

    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, fs_path: str) -> None:
        try:
            with open(fs_path, "rb") as f:
                body = f.read()
        except OSError:
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        ext = os.path.splitext(fs_path)[1].lower()
        if ext == ".html":
            # 远程模式标记：前端靠它区分「远程面板」与「pywebview 本地窗口」。
            # 不能靠 location.protocol——pywebview 加载本地文件时也会自建
            # 随机端口的 HTTP 服务，本地窗口的 protocol 同样是 http:，只看
            # 协议会把本地窗口误判成远程（API 全打去 /api/* 拿到 index.html）。
            # 只有本服务器发出的页面才有这个标记。
            marker = "<script>window.__XL_REMOTE__=true;</script>"
            try:
                text = body.decode("utf-8")
                i = text.lower().find("<head>")
                text = (text[:i + 6] + marker + text[i + 6:]
                        if i >= 0 else marker + text)
                body = text.encode("utf-8")
            except UnicodeDecodeError:
                pass
        self.send_response(200)
        self.send_header("Content-Type",
                         _CONTENT_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: 覆盖默认 stderr 噪音
        logger.debug("[远程] " + fmt % args)

    # ---- 路由 ----

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path
        if path == "/api/ping":
            if not self._authed(qs):
                self._send_json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._send_json({"ok": True})
            return
        if path == "/events":
            if not self._authed(qs):
                self._send_json({"ok": False, "error": "unauthorized"}, 401)
                return
            self._handle_sse()
            return
        # 静态资源公开：HTML/CSS/JS/图片不含敏感信息，令牌只保护 API 与 SSE。
        # 关键原因：子资源请求（<link>/<script>/<img>）会丢掉 index 的 ?t=
        # 查询串——静态也校验令牌的话，手机端只能拿到裸 HTML（无 CSS/JS），
        # 页面完全不可用（真机截图：默认样式 + 巨大黑图标）。
        name = _STATIC_FILES.get(path)
        if name is None and path.startswith("/assets/"):
            # 白名单目录内取基名，防 ../ 逃逸
            name = "assets/" + os.path.basename(path)
        if name is not None:
            self._send_static(os.path.join(self.server.webui_dir, name))
            return
        if not self._authed(qs):
            self._send_json({"ok": False, "error": "unauthorized"}, 401)
            return
        self._send_json({"ok": False, "error": "not found"}, 404)

    def _handle_sse(self):
        """SSE 推送流：hub 队列 → event: push。客户端断开/写失败即退，
        hub 退订；无事件时 15s 一次 keepalive 注释行，兼做断连探测。
        Connection: close（SSE 无 Content-Length，不进 keep-alive）；
        浏览器 EventSource 断开后自动重连，等效天然重订阅。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        hub = getattr(self.server, "hub", None)
        q = hub.subscribe() if hub is not None else None
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while self.server.serving:
                if q is None:
                    break
                try:
                    evt, payload = q.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                data = json.dumps({"evt": evt, "payload": payload},
                                  ensure_ascii=False)
                self.wfile.write(f"event: push\ndata: {data}\n\n"
                                 .encode("utf-8"))
                self.wfile.flush()
        except (ConnectionError, BrokenPipeError, OSError):
            pass  # 客户端断开是常态（手机锁屏/离开页面）
        finally:
            if hub is not None and q is not None:
                hub.unsubscribe(q)

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if not u.path.startswith("/api/"):
            self._send_json({"ok": False, "error": "not found"}, 404)
            return
        if not self._authed(qs):
            self._send_json({"ok": False, "error": "unauthorized"}, 401)
            return
        name = u.path[len("/api/"):]
        fn = getattr(self.server, "allowed", {}).get(name)
        if fn is None:
            self._send_json({"ok": False,
                             "error": "方法不存在或不可远程调用"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") \
                if length else {}
            args = body.get("args") or []
            if not isinstance(args, list):
                raise ValueError("args 必须是数组")
            result = fn(*args)
        except Exception as e:  # noqa: 桥方法异常转 JSON（对齐 _safe 语义）
            self._send_json({"ok": False,
                             "error": f"{type(e).__name__}: {e}"})
            return
        self._send_json(result if isinstance(result, dict)
                        else {"ok": True, "result": result})


class RemoteServer:
    """远程面板服务：包装 ThreadingHTTPServer 为可启停组件（保存配置时
    按 remote_enabled/remote_lan/remote_port 变更启停/重启）。"""

    def __init__(self, bridge, host: str, port: int, token: str,
                 webui_dir_path: str):
        self.hub = bridge.remote_hub
        self.token = token
        httpd = ThreadingHTTPServer((host, port), _RemoteHandler)
        httpd.token = token
        httpd.hub = bridge.remote_hub
        httpd.allowed = remote_allowed(bridge)
        httpd.webui_dir = webui_dir_path
        # serving 挂在 httpd 上（handler 经 self.server 读它）；RemoteServer
        # 只是包装层，handler 看不到它
        httpd.serving = True
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, daemon=True, name="xiaoli-remote")

    def start(self) -> None:
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        logger.info(f"[远程] 面板服务已启动 http://{host}:{port}/"
                    f"（绑定 {host}）")

    def stop(self) -> None:
        self._httpd.serving = False
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except OSError:
            pass
        logger.info("[远程] 面板服务已停止")
