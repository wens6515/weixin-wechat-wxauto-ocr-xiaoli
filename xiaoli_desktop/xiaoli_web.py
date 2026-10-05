# -*- coding: utf-8 -*-
"""小漓 · Web 前端入口（pywebview 壳 + pystray 托盘）。

替代 xiaoli_gui.py 的 Qt 启动链：单实例互斥 → 配置加载 → EngineThread
（不自动启动，等首页「初始化」）→ pywebview 无边框窗口（js_api 桥）→
pystray 托盘。引擎线程模型、bot 工厂、DPAPI 配置链路与 Qt 版完全一致。

窗口控制约定：HTML 自绘标题栏（-webkit-app-region 拖拽）；关闭按钮 =
隐藏到托盘；退出走托盘菜单（engine.stop 收尾后进程退出）。

用法::
    python xiaoli_web.py [--debug]   # --debug 打开 WebView2 开发者工具
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import sys
import threading
import traceback

import webview

from xiaoli_app import card_store, config_store, web_search
from xiaoli_app.engine import EngineBus, EngineThread
from xiaoli_app.webbridge import BridgeApi, app_base_dir, default_wallpaper_path, webui_dir


class WebContext:
    """轻量 AppContext（无 Qt 依赖版）：cfg / engine / bus + 重投影。"""

    def __init__(self, cfg_path: str, cards_dir: str):
        self.cfg_path = cfg_path
        self.cards_dir = cards_dir
        self.cfg = {}
        self.engine = None
        self.bus = None

    def providers(self):
        return self.cfg.get("providers", [])

    def active_card_id(self):
        return self.cfg.get("active_card_id", "")

    def reproject_and_push(self):
        """重载配置并热推送绑定/参数/覆盖表（对齐旧 AppContext 语义）。"""
        cfg = config_store.load_config_store(self.cfg_path, self.cards_dir)
        self.cfg = cfg
        eng = self.engine
        bot = getattr(eng, "bot", None)
        if bot is not None:
            bot.chat_card_bindings = cfg.get("chat_card_bindings", {})
            bot.chat_card_params = cfg.get("chat_card_params", {})
            bot.chat_feature_overrides = cfg.get("chat_feature_overrides", {})
            card = card_store.get_card(self.cards_dir,
                                       cfg.get("active_card_id", ""))
            if card:
                try:
                    eng.apply_role(card, cfg.get("providers"))
                except Exception:
                    pass
        return cfg


# ---------- 托盘（pystray + Pillow 画状态角标 logo） ----------

_TRAY_STATE_COLORS = {
    "idle": (148, 163, 184), "running": (16, 185, 129),
    "paused": (245, 158, 11), "error": (239, 68, 68),
}


def _render_logo(size: int = 64, state: str = "idle"):
    """蓝紫渐变圆底 + 白色「漓」+ 右上角状态角标（对齐旧托盘 render_logo）。"""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    top, bottom = (79, 179, 255), (123, 92, 255)
    grad = Image.new("RGBA", (size, size))
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        t = y / max(1, size - 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) \
            + (255,)
        gd.line([(0, y), (size, y)], fill=color)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    im.paste(grad, (0, 0), mask)
    # 「漓」字：优先内置思源黑体，缺省回退系统字体（画不出就留纯圆底）
    font = None
    base = app_base_dir()
    for d in (os.path.join(base, "fonts"),
              os.path.join(os.path.dirname(base), "fonts")):
        fp = os.path.join(d, "NotoSansSC-Bold.otf")
        if os.path.isfile(fp):
            try:
                font = ImageFont.truetype(fp, int(size * 0.52))
                break
            except OSError:
                pass
    if font is None:
        try:
            font = ImageFont.truetype("msyh.ttc", int(size * 0.52))
        except OSError:
            font = None
    if font is not None:
        d = ImageDraw.Draw(im)
        bbox = d.textbbox((0, 0), "漓", font=font)
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text(((size - w) / 2 - bbox[0], (size - h) / 2 - bbox[1]), "漓",
               font=font, fill=(255, 255, 255, 255))
    # 状态角标：右上角圆点 + 白描边
    dot_r = max(4, size // 9)
    cx, cy = size - dot_r - 2, dot_r + 2
    color = _TRAY_STATE_COLORS.get(state, _TRAY_STATE_COLORS["idle"])
    d = ImageDraw.Draw(im)
    d.ellipse((cx - dot_r - 1, cy - dot_r - 1, cx + dot_r + 1, cy + dot_r + 1),
              fill=(255, 255, 255, 255))
    d.ellipse((cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r),
              fill=color + (255,))
    return im


def _make_tray(ctx: WebContext, bridge: BridgeApi):
    import pystray

    def paused() -> bool:
        bot = getattr(ctx.engine, "bot", None)
        return bool(getattr(bot, "paused", False))

    state = {"cur": "idle", "win_visible": True}

    def refresh_icon():
        try:
            tray.icon = _render_logo(64, state["cur"])
        except Exception:
            _tray_log(traceback.format_exc())

    def _tray_log(msg: str):
        """托盘动作走日志 DEBUG 轨（bot.log 全量，含成败），不再另落托盘日志文件。"""
        logging.getLogger("xiaoli").debug("[托盘] %s", msg.rstrip())

    def show_window(icon=None, item=None):
        try:
            # 窗口引用从 bridge 取：main 里 win 在 _make_tray 之后才创建，
            # 闭包引用 main 局部变量 = NameError（历史事故：回调异常被
            # pystray 静默吞掉，窗口唤不回且没有任何报错）
            w = bridge._win
            if w is None:
                _tray_log("show: window not ready")
                return
            w.show()
            _tray_log("show: ok")
        except Exception:
            _tray_log("show FAILED:\n" + traceback.format_exc())

    def toggle_pause(icon=None, item=None):
        try:
            eng = ctx.engine
            if paused():
                eng.resume()
                _tray_log("resume: ok")
            else:
                eng.pause()
                _tray_log("pause: ok")
            refresh_icon()
        except Exception:
            _tray_log("toggle FAILED:\n" + traceback.format_exc())

    def quit_app(icon=None, item=None):
        try:
            bridge.quit_app()
            _tray_log("quit: ok")
        except Exception:
            _tray_log("quit FAILED:\n" + traceback.format_exc())

    menu = pystray.Menu(
        pystray.MenuItem("打开控制面板", show_window, default=True),
        pystray.MenuItem(lambda item: "恢复回复" if paused() else "暂停回复",
                         toggle_pause),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出小漓", quit_app),
    )
    tray = pystray.Icon("xiaoli", _render_logo(64, "idle"), "小漓控制面板",
                        menu)
    tray.RUNNING = refresh_icon   # 入口在状态变化时调用

    def watch_state():
        """跟随引擎状态换角标（轻轮询：1s 一次，读字符串零成本）。"""
        while not bridge._stop.wait(1.0):
            s = ctx.engine.state
            cur = ("running" if s == "running" else
                   "paused" if s == "paused" else
                   "error" if s == "error" else "idle")
            if cur != state["cur"]:
                state["cur"] = cur
                refresh_icon()

    threading.Thread(target=watch_state, daemon=True,
                     name="xiaoli-tray-watch").start()
    return tray


# ---------- 启动链 ----------

def build_bot_factory(ctx: WebContext):
    def factory(stop_event=None):
        from xiaoli_bot import AgentBot
        return AgentBot(ctx.cfg, stop_event=stop_event, max_connect_retries=30)
    return factory


def main():
    ap = argparse.ArgumentParser(description="小漓控制面板（Web 前端）")
    ap.add_argument("--debug", action="store_true",
                    help="打开 WebView2 开发者工具")
    args = ap.parse_args()

    # 单实例互斥（与 Qt 版共用同一互斥体名；弹窗用系统 MessageBox）
    from xiaoli_bot import acquire_single_instance
    if not acquire_single_instance("XiaoLi_SingleInstance"):
        ctypes.windll.user32.MessageBoxW(
            0, "小漓已在运行（在系统托盘图标处查看）。", "小漓", 0x30)
        return

    base = app_base_dir()
    ctx = WebContext(os.path.join(base, "config.json"),
                     os.path.join(base, "cards"))
    ctx.cfg = config_store.load_config_store(ctx.cfg_path, ctx.cards_dir)
    web_search.set_proxy(str(ctx.cfg.get("web_proxy", "") or ""))

    # 无壁纸配置 → 内置默认壁纸兜底（对齐旧入口语义）
    if not str(ctx.cfg.get("wallpaper_path", "") or "").strip():
        wp = default_wallpaper_path()
        if wp:
            ctx.cfg["wallpaper_path"] = wp
            config_store.save_config(ctx.cfg, ctx.cfg_path)

    ctx.bus = EngineBus()
    ctx.engine = EngineThread(build_bot_factory(ctx), bus=ctx.bus)

    bridge = BridgeApi(ctx)
    tray = _make_tray(ctx, bridge)
    bridge.bind_tray(tray)

    index = os.path.join(webui_dir(), "index.html")
    win = webview.create_window(
        "小漓", index,
        js_api=bridge, width=1180, height=760, min_size=(980, 680),
        frameless=True, easy_drag=False, background_color="#0B2540",
        confirm_close=False)
    bridge.bind_window(win)
    bridge.start_push_loop()

    threading.Thread(target=tray.run, daemon=True,
                     name="xiaoli-tray").start()

    try:
        webview.start(debug=bool(args.debug))
    finally:
        # 收尾：停推送循环 → 停引擎（5s 超时）→ 停托盘
        bridge.stop()
        try:
            ctx.engine.stop(timeout=5)
        except Exception:
            pass
        try:
            tray.stop()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # 打包版无控制台：启动崩溃落盘到 exe 旁，否则报错黑箱
        import traceback
        err = traceback.format_exc()
        try:
            with open(os.path.join(app_base_dir(), "webui_boot_error.log"),
                      "w", encoding="utf-8") as f:
                f.write(err)
        except OSError:
            pass
        ctypes.windll.user32.MessageBoxW(0, err[-1200:], "小漓启动失败", 0x10)
        raise
