# -*- coding: utf-8 -*-
"""Web 前端桥接层：pywebview js_api 方法面 + 后端推送循环。

替代 PySide6 UI 的数据层：前端经 window.pywebview.api.* 调用本模块方法
（pywebview 自动 Promise 化，返回值须 JSON 可序列化），后端经
evaluate_js 注入 window.__push(evt, payload) 推送事件。

设计约定：
- 统一返回 {ok: bool, ...}，失败 {ok: False, error}（_safe 装饰器兜底，
  桥内异常绝不炸掉调用线程）。
- 长操作（天枢安装/发首轮提示词/余额查询）起 daemon 线程，结果经推送
  事件回前端——对齐旧 UI 的「工作线程 + 结果回写」模式。
- bot 属性热写保持旧 UI 语义：多数为 GIL 原子赋值；chat_card_* /
  chat_feature_overrides 整表替换；providers 变更后统一走
  AppContext.reproject_and_push 重投影。
- 不 import xiaoli_app.ui（那个包依赖 PySide6）——壁纸库扫描/主题推荐
  壁纸清单在此就地重写，保证将来删除旧 UI 后本层无 Qt 依赖。
"""
from __future__ import annotations

import base64
import functools
import io
import json
import os
import shutil
import sys
import threading
import time

from PIL import Image

from . import balance, config_store, pricing, setup, update_check, web_search
from .card_store import (delete_card as cs_delete_card,
                         duplicate_card as cs_duplicate_card,
                         export_card as cs_export_card,
                         get_card as cs_get_card,
                         import_card as cs_import_card,
                         list_cards as cs_list_cards,
                         save_card as cs_save_card)
from .memory_store import MemoryStore, memory_key, render_export_markdown
from .reminders_store import RemindersStore
from .usage_store import UsageStore, hit_ratio
from .version import APP_VERSION

# wechat_bot 是仓库根的顶层模块（与 xiaoli_app 平级），只在用到处延迟导入，
# 避免 import 环（wechat_bot 反向引用 xiaoli_app.*）。
_MODEL_TIP = "strip_model_prefix / models_endpoint 延迟导入自 wechat_bot"


# ---------- 路径与壁纸库（自 ui 包摘出的无 Qt 版本） ----------

def app_base_dir() -> str:
    """应用基目录：打包态 = exe 所在目录；源码态 = xiaoli_desktop 根，
    可用 XIAOLI_HOME 环境变量重定向（开发/验证时隔离真实 config.json）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    env = os.environ.get("XIAOLI_HOME")
    if env:
        return env
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def webui_dir() -> str:
    """Web 前端静态资产目录。打包态 PyInstaller datas 落在 sys._MEIPASS
    （onedir 模式 = _internal）；源码态固定跟模块位置（xiaoli_desktop/webui），
    不随 XIAOLI_HOME 重定向——那是数据目录，前端资产是程序文件。"""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "webui")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "webui")


_WALLPAPER_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".webp")
_DEFAULT_WALLPAPER_KEY = "二次元角色-动漫"
_WALLPAPER_DIR_NAMES = ("壁纸", "wallpapers")


def wallpapers_dir() -> str:
    """内置壁纸目录：基目录及其父目录下的「壁纸」/「wallpapers」。"""
    base = app_base_dir()
    for p in (base, os.path.dirname(base)):
        for name in _WALLPAPER_DIR_NAMES:
            d = os.path.join(p, name)
            if os.path.isdir(d):
                return d
    return os.path.join(base, "壁纸")


def list_wallpapers() -> list[tuple[str, str]]:
    """扫描内置壁纸目录，返回 [(绝对路径, 文件名)] 按文件名排序。"""
    d = wallpapers_dir()
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    out = []
    for f in names:
        if f.lower().endswith(_WALLPAPER_EXTS) and os.path.isfile(os.path.join(d, f)):
            out.append((os.path.join(d, f), f))
    return out


def default_wallpaper_path() -> str:
    """默认壁纸：文件名含「二次元角色-动漫」的那张；否则第一张；空目录返回 ""。"""
    items = list_wallpapers()
    for p, f in items:
        if _DEFAULT_WALLPAPER_KEY in f:
            return p
    return items[0][0] if items else ""


def resolve_wallpaper_path(wp: str) -> str:
    """壁纸路径解析：绝对路径原样；裸文件名在内置目录里找同名文件。"""
    if not wp or os.path.isabs(wp):
        return wp
    base = os.path.basename(wp)
    for p, f in list_wallpapers():
        if f == wp or f == base:
            return p
    return wp


def wallpaper_short_name(fname: str) -> str:
    """去「哲风壁纸」前缀与扩展名的展示名。"""
    for prefix in ("【哲风壁纸】", "哲风壁纸-"):
        if fname.startswith(prefix):
            fname = fname[len(prefix):]
            break
    return os.path.splitext(fname)[0] or fname


# 每套主题的推荐壁纸（对齐旧 UI ui/__init__.py THEMES 的 wallpaper 键；
# tokyonight 无推荐——切到它不动壁纸）。
THEME_WALLPAPER = {
    "abyss": "小漓主题.jpg",
    "neon": "赛博朋克-壁纸.jpg",
    "tokyonight": "",
    "stellar": "典雅暗黑-暗夜.jpg",
    "moxin": "东方水墨-仙侠-古风.jpg",
    "cream": "软萌治愈-动物-可爱.jpg",
    "mint": "户外-活力少女-清新.jpg",
}
THEME_LABELS = {
    "abyss": "深海小漓", "neon": "霓虹夜行", "tokyonight": "Tokyo Night",
    "stellar": "星夜紫金", "moxin": "墨染", "cream": "奶油晨光", "mint": "薄荷气泡",
}
THEME_GROUPS = {
    "abyss": "深色", "neon": "深色", "tokyonight": "深色", "stellar": "深色",
    "moxin": "浅色", "cream": "浅色", "mint": "浅色",
}

# save_config 允许前端写入的键白名单（其余键只读——providers/tasks_dir 等
# 走各自专用方法，避免整包 patch 误伤派生键）。
_ALLOWED_KEYS = {
    "theme", "follow_system", "wallpaper_path", "card_opacity", "panel_opacity",
    "font_scale", "wall_opacity", "blur_level",
    "memory_deep_enabled", "memory_compress_enabled", "memory_keep_recent",
    "memory_compress_batch", "memory_important_max", "memory_compress_model",
    "memory_rolling_step",
    "max_context_tokens", "reply_max_tokens",
    "segment_wait_enabled", "segment_wait_seconds",
    "segment_jitter_enabled", "segment_jitter_min", "segment_jitter_max",
    "file_storage_path", "web_search_enabled", "web_proxy", "task_enabled",
    "state_watch_enabled", "model_trigger_manage", "sticker_mode",
    "voice_mode", "tts_endpoint", "tts_timeout_seconds", "voice_max_seconds",
    "voice_profiles", "active_voice_profile_id",
    "chat_feature_overrides", "first_prompt_path", "bot_nickname",
    "start_paused", "tianshu_install_dir",
}

# 写 cfg 后需要同步热写到 bot 属性的键（旧 UI 设置页语义：先落盘再热写）。
_BOT_HOT_KEYS = {
    "web_search_enabled", "task_enabled", "state_watch_enabled",
    "model_trigger_manage", "sticker_mode",
    "memory_deep_enabled", "memory_compress_enabled", "memory_keep_recent",
    "memory_compress_batch", "memory_important_max", "memory_rolling_step",
    "max_context_tokens", "reply_max_tokens",
    "segment_wait_enabled", "segment_wait_seconds",
    "segment_jitter_enabled", "segment_jitter_min", "segment_jitter_max",
    "file_storage_path",
    "voice_mode", "tts_endpoint", "voice_max_seconds", "voice_profiles",
    "active_voice_profile_id",
}

_TERMINAL_STATES = ("已触发", "已错过", "已达成", "已到期", "已失效")


def trigger_state_text(r: dict, now: float | None = None) -> str:
    """触发器状态徽章文本（自旧 UI pages.py 搬入的纯函数）。"""
    now = time.time() if now is None else now
    if r.get("kind") == "condition":
        done = r.get("done")
        if done == "met":
            return "已达成"
        if done == "expired":
            return "已到期"
        if done == "dead":
            return "已失效"
        return "已暂停" if not r.get("enabled") else "监视中"
    if not r.get("enabled"):
        if r.get("missed"):
            return "已错过"
        return "已触发" if r.get("last_fired") else "已暂停"
    return "触发中" if (r.get("fire_at") or 0) <= now else "待触发"


def count_active_triggers(items) -> int:
    """活跃触发器数（首页徽章）：启用中的定时 + 进行中的监视。"""
    n = 0
    for r in items or []:
        if not r.get("enabled"):
            continue
        if r.get("kind") == "condition":
            if not r.get("done"):
                n += 1
        else:
            n += 1
    return n


def default_wechat_files_dir() -> str:
    """微信文件接收目录默认值（自 xiaoli_gui 摘出的无 Qt 版本）。"""
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    candidates = []
    for base in (os.path.join(docs, "xwechat_files"),
                 os.path.join(docs, "WeChat Files")):
        if os.path.isdir(base):
            try:
                for sub in os.listdir(base):
                    p = os.path.join(base, sub)
                    if os.path.isdir(p):
                        candidates.append(p)
            except OSError:
                pass
            candidates.append(base)
    if candidates:
        return candidates[0]
    return os.path.join(docs, "WeChat Files")


def _first_run_needed(cfg_path: str, cfg: dict | None) -> bool:
    """首次启动判定：config 文件不存在，或任务/微信文件目录未配置。"""
    if not os.path.isfile(cfg_path):
        return True
    if cfg is None or not str(cfg.get("tasks_dir") or "").strip() \
            or not str(cfg.get("file_storage_path") or "").strip():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                return not str(disk.get("tasks_dir") or "").strip() or \
                    not str(disk.get("file_storage_path") or "").strip()
        except (OSError, ValueError):
            pass
    return False


def _safe(fn):
    """js_api 方法兜底：异常转 {ok: False, error}，不让桥炸掉调用线程。"""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return wrapper


class BridgeApi:
    """pywebview js_api 入口：webview.create_window(js_api=self)。

    生命周期：xiaoli_web.main() 构造（ctx 就绪后）→ 挂 _win（窗口创建后）
    → start_push_loop()。quit_app() 请求退出：销毁窗口令 webview.start()
    返回，由入口收尾（停引擎/停托盘）。
    """

    def __init__(self, ctx):
        self.ctx = ctx
        self._win = None            # pywebview Window（启动后挂入）
        self._tray = None           # pystray Icon（入口挂入，用于托盘气泡）
        self._log_offset = 0        # bot_run.log 增量 tail 位移（字节）
        self._img_cache: dict = {}  # (path, kind) -> (mtime, size, dataURI)
        self._stop = threading.Event()
        self._installing = False

    # ---------- 基础设施 ----------

    def bind_window(self, win):
        self._win = win

    def bind_tray(self, tray):
        self._tray = tray

    def push(self, evt: str, payload: dict):
        """向前端注入事件（窗口未就绪/正在销毁时静默跳过）。"""
        win = self._win
        if win is None:
            return
        try:
            js = ("window.__push && window.__push(%s, %s);"
                  % (json.dumps(evt), json.dumps(payload, ensure_ascii=False)))
            win.evaluate_js(js)
        except Exception:
            pass

    def notify(self, title: str, message: str):
        """托盘气泡（托盘不可用时静默）。"""
        tray = self._tray
        if tray is None:
            return
        try:
            tray.notify(title, message)
        except Exception:
            pass

    def start_push_loop(self):
        threading.Thread(target=self._push_loop, daemon=True,
                         name="xiaoli-webpush").start()

    def stop(self):
        self._stop.set()

    def _push_loop(self):
        """1s 心跳：引擎状态 + bus 事件 + 日志增量 → 前端 tick 事件。
        引擎进入 initialized（初始化成功）时自动发一轮首轮提示词——
        任务桥关闭则跳过（对齐旧 UI：环境检查通过且引擎就绪后自动发送）。"""
        prev_state = None
        while not self._stop.wait(1.0):
            try:
                eng = self.ctx.engine
                payload = {"state": eng.state, "error": eng.error}
                bot = getattr(eng, "bot", None)
                payload["paused"] = (bool(bot.paused) is True) if bot else None
                if eng.state == "initialized" and prev_state != "initialized" \
                        and not getattr(self, "_auto_prompted", False):
                    self._auto_prompted = True
                    self.send_first_prompt()
                prev_state = eng.state
                try:
                    if self.ctx.bus is not None:
                        for kind, pl in self.ctx.bus.drain():
                            if kind == "error":
                                payload["bus_error"] = str(pl.get("message") or "")
                except Exception:
                    pass
                try:
                    lines = self._tail_log_lines()
                    if lines:
                        payload["log"] = lines
                except Exception:
                    pass
                self.push("tick", payload)
            except Exception:
                pass

    # ---------- 日志 ----------

    def _log_path(self) -> str:
        # 与 bot 写入端同源：RUN_LOG_FILE 按 wechat_bot.__file__ 定位，
        # frozen 态落在 _internal —— 前端 tail 必须读同一个文件
        from wechat_bot import RUN_LOG_FILE
        return RUN_LOG_FILE

    def _tail_log_lines(self, cap: int = 400) -> list[str]:
        p = self._log_path()
        size = os.path.getsize(p)
        if size < self._log_offset:      # 文件轮转/清空 → 从头重读
            self._log_offset = 0
        if size == self._log_offset:
            return []
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            f.seek(self._log_offset)
            chunk = f.read()
        self._log_offset = size
        lines = [l for l in chunk.splitlines() if l.strip()]
        return lines[-cap:]

    @_safe
    def tail_log(self) -> dict:
        """前端首次进入日志页全量拉取（上限 800 行，之后靠 tick 增量）。"""
        try:
            self._log_offset = 0
            lines = self._tail_log_lines(cap=800)
        except OSError:
            lines = []
        return {"ok": True, "lines": lines}

    # ---------- 状态与引擎 ----------

    @_safe
    def get_status(self) -> dict:
        eng = self.ctx.engine
        bot = getattr(eng, "bot", None)
        items = RemindersStore().list()
        return {"ok": True, "state": eng.state, "error": eng.error,
                "bot_ready": bot is not None,
                "paused": (bool(bot.paused) is True) if bot else None,
                "version": APP_VERSION,
                "trigger_active": count_active_triggers(items)}

    @_safe
    def engine_action(self, action: str) -> dict:
        eng = self.ctx.engine
        if action == "initialize":
            ok = eng.initialize()
        elif action == "start":
            ok = eng.start_bot()
        elif action == "pause":
            eng.pause()
            ok = True
        elif action == "resume":
            eng.resume()
            ok = True
        else:
            return {"ok": False, "error": f"未知操作: {action}"}
        return {"ok": bool(ok), "state": eng.state}

    # ---------- 配置 ----------

    @_safe
    def get_config(self) -> dict:
        cfg = self.ctx.cfg
        co = cfg.get("card_opacity")
        po = cfg.get("panel_opacity")
        ui = {
            "theme": cfg.get("theme", "abyss"),
            "follow_system": bool(cfg.get("follow_system")),
            "wallpaper_path": cfg.get("wallpaper_path", ""),
            # 0.5 是旧版默认值 → 视为未定制，跟随主题推荐 alpha
            "card_opacity": None if co in (None, 0.5) else co,
            "panel_opacity": None if po in (None, 0.5) else po,
            "wall_opacity": cfg.get("wall_opacity"),
            "blur_level": cfg.get("blur_level"),
            "font_scale": cfg.get("font_scale", "medium"),
        }
        misc = {
            "memory_deep_enabled": cfg.get("memory_deep_enabled", True),
            "memory_compress_enabled": cfg.get("memory_compress_enabled", False),
            "memory_keep_recent": cfg.get("memory_keep_recent", 30),
            "memory_rolling_step": cfg.get("memory_rolling_step", 30),
            "memory_compress_batch": cfg.get("memory_compress_batch", 30),
            "memory_important_max": cfg.get("memory_important_max", 20),
            "memory_compress_model": cfg.get("memory_compress_model", ""),
            "max_context_tokens": cfg.get("max_context_tokens", 100000),
            "reply_max_tokens": cfg.get("reply_max_tokens", 400),
            "segment_wait_enabled": cfg.get("segment_wait_enabled", True),
            "segment_wait_seconds": cfg.get("segment_wait_seconds", 2.0),
            "segment_jitter_enabled": cfg.get("segment_jitter_enabled", False),
            "segment_jitter_min": cfg.get("segment_jitter_min", 1.5),
            "segment_jitter_max": cfg.get("segment_jitter_max", 3.0),
            "file_storage_path": cfg.get("file_storage_path", ""),
            "tasks_dir": cfg.get("tasks_dir", ""),
            "tianshu_workdir": cfg.get("tianshu_workdir", ""),
            "web_proxy": cfg.get("web_proxy", ""),
            "first_prompt_path": cfg.get("first_prompt_path", ""),
            "bot_nickname": cfg.get("bot_nickname", "小漓"),
            "start_paused": cfg.get("start_paused", True),
            "web_search_enabled": cfg.get("web_search_enabled", True),
            "task_enabled": cfg.get("task_enabled", True),
            "state_watch_enabled": cfg.get("state_watch_enabled", False),
            "model_trigger_manage": cfg.get("model_trigger_manage", "off"),
            "sticker_mode": cfg.get("sticker_mode", "off"),
        }
        voice = {
            "voice_mode": cfg.get("voice_mode", "off"),
            "tts_endpoint": cfg.get("tts_endpoint", ""),
            "tts_timeout_seconds": cfg.get("tts_timeout_seconds", 120),
            "voice_max_seconds": cfg.get("voice_max_seconds", 55),
            "voice_profiles": cfg.get("voice_profiles", []),
            "active_voice_profile_id": cfg.get("active_voice_profile_id", ""),
        }
        return {"ok": True, "ui": ui, "misc": misc, "voice": voice,
                "overrides": cfg.get("chat_feature_overrides", {}),
                "active_card_id": cfg.get("active_card_id", ""),
                "first_run_needed": _first_run_needed(self.ctx.cfg_path, cfg),
                "cfg_path": self.ctx.cfg_path}

    @_safe
    def save_config(self, patch: dict) -> dict:
        """白名单键写入 + 落盘 + bot 热写（保持旧 UI「保存即热生效」语义）。"""
        if not isinstance(patch, dict):
            return {"ok": False, "error": "patch 必须是对象"}
        cfg = self.ctx.cfg
        applied = [k for k in patch if k in _ALLOWED_KEYS]
        for k in applied:
            cfg[k] = patch[k]
        if "chat_feature_overrides" in applied:
            # 键归一化对齐旧 UI：memory_key（剥引号变体与空白）
            from .memory_store import memory_key
            ov = patch["chat_feature_overrides"] or {}
            cfg["chat_feature_overrides"] = {
                memory_key(k): v for k, v in ov.items()}
        config_store.save_config(cfg, self.ctx.cfg_path)
        if "web_proxy" in applied:
            web_search.set_proxy(str(patch["web_proxy"] or ""))
        bot = getattr(self.ctx.engine, "bot", None)
        hot = []
        if bot is not None:
            for k in _BOT_HOT_KEYS:
                if k in applied:
                    setattr(bot, k, patch[k])
                    hot.append(k)
            if "memory_compress_model" in applied:
                from wechat_bot import strip_model_prefix
                bot.memory_compress_model = strip_model_prefix(
                    str(patch["memory_compress_model"] or ""))
                hot.append("memory_compress_model")
            if "tts_timeout_seconds" in applied:
                bot.tts_timeout = patch["tts_timeout_seconds"]
                hot.append("tts_timeout_seconds")
            if "chat_feature_overrides" in applied:
                bot.chat_feature_overrides = patch["chat_feature_overrides"]
                hot.append("chat_feature_overrides")
        return {"ok": True, "applied": applied, "bot_hot": hot}

    @_safe
    def get_providers(self) -> dict:
        """Provider 表真值（仅本窗口 JS 可调，与旧 UI 表格同级的可见性）。"""
        return {"ok": True, "providers": self.ctx.cfg.get("providers", [])}

    @_safe
    def set_theme(self, key: str) -> dict:
        """切主题（配置落盘）+ 推荐壁纸联动（对齐旧 UI：库里有同名文件才应用）。"""
        if key not in THEME_WALLPAPER:
            return {"ok": False, "error": f"未知主题: {key}"}
        cfg = self.ctx.cfg
        cfg["theme"] = key
        applied_wp = ""
        rec = THEME_WALLPAPER.get(key)
        if rec:
            p = resolve_wallpaper_path(rec)
            if p and os.path.isfile(p):
                cfg["wallpaper_path"] = p
                applied_wp = p
        config_store.save_config(cfg, self.ctx.cfg_path)
        return {"ok": True, "theme": key, "wallpaper_applied": applied_wp}

    # ---------- 壁纸 ----------

    def _image_uri(self, path: str, kind: str) -> str | None:
        """图片 → data URI（Pillow 编码；按 (path, kind) + mtime/size 缓存）。

        thumb = 240x152 cover 裁剪缩略；full = 最长边 1600 等比缩。
        WebView2 内置 http server 只 serve 前端目录，跨目录的壁纸图只能
        以 data URI 注入，故不走 <img src> 文件路径。
        """
        try:
            st = os.stat(path)
        except OSError:
            return None
        key = (path, kind)
        hit = self._img_cache.get(key)
        if hit and hit[0] == (st.st_mtime, st.st_size):
            return hit[1]
        try:
            im = Image.open(path)
            im.load()
            im = im.convert("RGB")
            if kind == "thumb":
                tw, th = 240, 152
                target = tw / th
                w, h = im.size
                if w / h > target:
                    nw = int(h * target)
                    x = (w - nw) // 2
                    im = im.crop((x, 0, x + nw, h))
                else:
                    nh = int(w / target)
                    y = (h - nh) // 2
                    im = im.crop((0, y, w, y + nh))
                im = im.resize((tw, th), Image.LANCZOS)
            else:
                im.thumbnail((1600, 1600), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85)
            uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        except Exception:
            return None
        if len(self._img_cache) > 24:          # 简单防膨胀：超限整体清空
            self._img_cache.clear()
        self._img_cache[key] = ((st.st_mtime, st.st_size), uri)
        return uri

    @_safe
    def list_wallpapers(self) -> dict:
        items = []
        for p, f in list_wallpapers():
            items.append({"name": wallpaper_short_name(f), "path": p,
                          "thumb": self._image_uri(p, "thumb")})
        cur = str(self.ctx.cfg.get("wallpaper_path", "") or "")
        cur_resolved = resolve_wallpaper_path(cur)
        custom = None
        if cur_resolved and os.path.isfile(cur_resolved) and \
                not any(it["path"] == cur_resolved for it in items):
            custom = {"name": "自定义: " + wallpaper_short_name(
                          os.path.basename(cur_resolved)),
                      "path": cur_resolved,
                      "thumb": self._image_uri(cur_resolved, "thumb")}
        return {"ok": True, "items": items, "custom": custom,
                "current": cur, "current_resolved": cur_resolved,
                "current_data": self._image_uri(cur_resolved, "full")
                if cur_resolved and os.path.isfile(cur_resolved) else None}

    @_safe
    def set_wallpaper(self, path: str) -> dict:
        """设置壁纸（空串 = 无壁纸，恢复纯渐变背景）。"""
        cfg = self.ctx.cfg
        cfg["wallpaper_path"] = str(path or "")
        config_store.save_config(cfg, self.ctx.cfg_path)
        return {"ok": True, "wallpaper_path": cfg["wallpaper_path"]}

    @_safe
    def wallpaper_data(self, path: str) -> dict:
        """取指定壁纸的全图 data URI（前端预览/切换时用）。"""
        p = resolve_wallpaper_path(str(path or ""))
        if not p or not os.path.isfile(p):
            return {"ok": False, "error": "壁纸文件不存在"}
        return {"ok": True, "path": p, "data": self._image_uri(p, "full")}

    @_safe
    def import_wallpaper(self) -> dict:
        """系统文件对话框选图 → 存绝对路径（对齐旧 UI：自定义壁纸不复制）。"""
        if self._win is None:
            return {"ok": False, "error": "窗口未就绪"}
        import webview
        res = self._win.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=("图片 (*.jpg;*.jpeg;*.png;*.bmp;*.webp)",))
        p = None
        if isinstance(res, (list, tuple)) and res:
            p = res[0]
        elif isinstance(res, str) and res:
            p = res
        if not p:
            return {"ok": True, "canceled": True}
        p = os.path.abspath(p)
        cfg = self.ctx.cfg
        cfg["wallpaper_path"] = p
        config_store.save_config(cfg, self.ctx.cfg_path)
        return {"ok": True, "path": p, "name": wallpaper_short_name(os.path.basename(p)),
                "thumb": self._image_uri(p, "thumb"),
                "data": self._image_uri(p, "full")}

    # ---------- 触发器 ----------

    @_safe
    def list_triggers(self) -> dict:
        items = RemindersStore().list()
        now = time.time()
        rows = []
        for r in items:
            st = trigger_state_text(r, now)
            rows.append({**r, "state_text": st,
                         "terminal": st in _TERMINAL_STATES})
        rows.sort(key=lambda r: (r["terminal"],
                                 r.get("fire_at") or r.get("expire_at")
                                 or r.get("next_check_at") or 0))
        return {"ok": True, "rows": rows,
                "active": count_active_triggers(items)}

    @_safe
    def add_trigger(self, chat: str, content: str, fire_at: float,
                    repeat: str = "once") -> dict:
        r = RemindersStore().add(str(chat or "").strip(), str(content or "").strip(),
                                 float(fire_at), str(repeat or "once"))
        return {"ok": True, "item": r}

    @_safe
    def add_condition(self, chat: str, content: str, url: str, condition: str,
                      judge: str = "local", match_type: str = "present",
                      met_keywords=None, scope_start=None, scope_end=None,
                      interval_seconds: int = 60, expire_at=None) -> dict:
        r = RemindersStore().add_condition(
            str(chat or "").strip(), str(content or "").strip(),
            str(url or "").strip(), str(condition or "").strip(),
            judge=str(judge or "local"), match_type=str(match_type or "present"),
            met_keywords=met_keywords, scope_start=scope_start,
            scope_end=scope_end, interval_seconds=int(interval_seconds or 60),
            expire_at=float(expire_at) if expire_at else None)
        return {"ok": True, "item": r}

    @_safe
    def set_trigger_enabled(self, rid: str, enabled: bool) -> dict:
        return {"ok": bool(RemindersStore().set_enabled(str(rid), bool(enabled)))}

    @_safe
    def delete_trigger(self, rid: str) -> dict:
        return {"ok": bool(RemindersStore().remove(str(rid)))}

    # ---------- 角色卡 ----------

    @_safe
    def list_cards(self) -> dict:
        cards = cs_list_cards(self.ctx.cards_dir)
        return {"ok": True, "cards": cards,
                "active_id": self.ctx.cfg.get("active_card_id", "")}

    @_safe
    def get_card(self, card_id: str) -> dict:
        card = cs_get_card(self.ctx.cards_dir, str(card_id))
        if card is None:
            return {"ok": False, "error": "卡片不存在"}
        return {"ok": True, "card": card}

    @_safe
    def save_card(self, card: dict) -> dict:
        """新建/保存卡片；新卡 id 后缀用随机 hex（不在产物里写时间戳）。"""
        card = dict(card or {})
        cid = str(card.get("id") or "").strip()
        if not cid:
            base = "".join(c for c in str(card.get("name") or "")
                           if c.isascii() and (c.isalnum() or c in "_-"))
            cid = (base or "card") + "_" + os.urandom(3).hex()
            card["id"] = cid
        saved = cs_save_card(self.ctx.cards_dir, card)
        new_cfg = self.ctx.reproject_and_push()
        return {"ok": True, "card": saved, "active_card_id": new_cfg.get("active_card_id")}

    @_safe
    def delete_card(self, card_id: str) -> dict:
        if str(card_id) == self.ctx.cfg.get("active_card_id"):
            return {"ok": False, "error": "活跃卡不能删除（先激活别的卡）"}
        return {"ok": bool(cs_delete_card(self.ctx.cards_dir, str(card_id)))}

    @_safe
    def duplicate_card(self, card_id: str) -> dict:
        card = cs_duplicate_card(self.ctx.cards_dir, str(card_id))
        return {"ok": True, "card": card}

    @_safe
    def activate_card(self, card_id: str) -> dict:
        card = cs_get_card(self.ctx.cards_dir, str(card_id))
        if card is None:
            return {"ok": False, "error": "卡片不存在"}
        cfg = self.ctx.cfg
        cfg["active_card_id"] = str(card_id)
        config_store.save_config(cfg, self.ctx.cfg_path)
        applied = False
        try:
            applied = bool(self.ctx.engine.apply_role(card, cfg.get("providers")))
        except Exception:
            applied = False
        return {"ok": True, "hot_applied": applied}

    @_safe
    def export_card(self, card_id: str) -> dict:
        card = cs_export_card(self.ctx.cards_dir, str(card_id))
        return {"ok": True, "card": card}

    @_safe
    def import_card(self, data) -> dict:
        saved = cs_import_card(self.ctx.cards_dir, data)
        return {"ok": True, "card": saved}

    # ---------- 模型 Provider ----------

    @_safe
    def save_providers(self, providers, chat_provider=None, chat_model=None) -> dict:
        cfg = self.ctx.cfg
        norm = []
        for i, p in enumerate(providers or []):
            if not isinstance(p, dict):
                continue
            models = p.get("models") or []
            if isinstance(models, str):
                models = [m.strip() for m in models.split(",") if m.strip()]
            norm.append({"id": str(p.get("id") or f"p{i + 1}").strip(),
                         "name": str(p.get("name") or "").strip(),
                         "base_url": str(p.get("base_url") or "").strip(),
                         "api_key": str(p.get("api_key") or "").strip(),
                         "models": list(models)})
        cfg["providers"] = norm
        if chat_provider and chat_model:
            card = cs_get_card(self.ctx.cards_dir,
                               cfg.get("active_card_id", ""))
            if card is not None:
                card["chat_provider"] = str(chat_provider)
                card["chat_model"] = str(chat_model)
                cs_save_card(self.ctx.cards_dir, card)
        config_store.save_config(cfg, self.ctx.cfg_path)
        self.ctx.reproject_and_push()
        return {"ok": True, "providers": norm}

    @_safe
    def test_provider(self, base_url: str, api_key: str) -> dict:
        """连接测试：GET {models 端点}，列前 20 个模型（15s 超时）。"""
        import requests as rq
        from wechat_bot import models_endpoint
        url = models_endpoint(str(base_url or "").strip())
        if not url:
            return {"ok": False, "error": "Base URL 为空"}
        try:
            resp = rq.get(url, headers={"Authorization": f"Bearer {api_key or ''}"},
                          timeout=15)
        except rq.exceptions.RequestException as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if resp.status_code != 200:
            return {"ok": False, "error": f"HTTP {resp.status_code}"}
        try:
            data = resp.json().get("data") or []
            names = [str(m.get("id") or "") for m in data if m.get("id")]
        except ValueError:
            return {"ok": False, "error": "响应不是有效 JSON"}
        return {"ok": True, "models": names[:20], "total": len(names)}

    # ---------- 记忆 ----------

    def _mem_channel(self):
        """bot 通道优先（运行中写盘节流一致）；未运行新建文件通道实例。"""
        bot = getattr(self.ctx.engine, "bot", None)
        if bot is not None and hasattr(bot, "memory_overview"):
            return bot, True
        m = MemoryStore(self.ctx.cfg.get("memory_file") or "memory.json")
        m.load(deep_enabled=bool(self.ctx.cfg.get("memory_deep_enabled", True)))
        return m, False

    @_safe
    def memory_overview(self) -> dict:
        ch, is_bot = self._mem_channel()
        data = (ch.memory_overview() if is_bot else ch.overview()) or {}
        binds = self.ctx.cfg.get("chat_card_bindings", {}) or {}
        cards = {c["id"]: c for c in cs_list_cards(self.ctx.cards_dir)}
        rows = []
        for chat, cnt in data.items():
            cid = binds.get(memory_key(chat)) or binds.get(chat)
            rows.append({"chat": chat, **(cnt or {}),
                         "card_name": cards[cid]["name"] if cid in cards else None})
        rows.sort(key=lambda r: r["chat"])
        return {"ok": True, "rows": rows}

    @_safe
    def memory_detail(self, chat: str, deep_offset: int = 0, query: str = "") -> dict:
        ch, is_bot = self._mem_channel()
        d = (ch.memory_detail(str(chat), deep_offset, 200, str(query or "") or None)
             if is_bot else
             ch.detail(str(chat), deep_offset, 200, str(query or "") or None))
        return {"ok": True, "chat": chat, **(d or {})}

    @_safe
    def memory_delete(self, kind: str, chat: str, idx: int) -> dict:
        ch, _ = self._mem_channel()
        chat = str(chat)
        idx = int(idx)
        if kind == "important":
            ok = ch.delete_important(chat, idx)
        elif kind == "recent":
            ok = ch.delete_messages(chat, [idx])
        elif kind == "index":
            ok = ch.delete_index_entry(chat, idx)
        elif kind == "deep":
            ok = ch.delete_deep_message(chat, idx)
        else:
            return {"ok": False, "error": f"未知类别: {kind}"}
        return {"ok": bool(ok)}

    @_safe
    def memory_clear_chat(self, chat: str) -> dict:
        ch, _ = self._mem_channel()
        return {"ok": bool(ch.clear_history(str(chat)))}

    @_safe
    def memory_clear_all(self) -> dict:
        """清空全部记忆：引擎通道优先（绕开会旧记忆被节流写盘覆盖的坑）。"""
        eng = self.ctx.engine
        if hasattr(eng, "clear_memory"):
            ok = bool(eng.clear_memory())
            if ok:
                return {"ok": True}
        m, _ = self._mem_channel()
        return {"ok": bool(m.clear_history(None))}

    @_safe
    def memory_export(self, chat: str, mode: str = "md") -> dict:
        ch, _ = self._mem_channel()
        data = ch.export_chat_data(str(chat))
        if mode == "json":
            content = json.dumps(data, ensure_ascii=False, indent=2)
            name = f"{chat}-记忆.json"
        else:
            content = render_export_markdown(data)
            name = f"{chat}-记忆.md"
        return {"ok": True, "filename": name, "content": content}

    @_safe
    def memory_bind_card(self, chat: str, card_id: str) -> dict:
        cfg = self.ctx.cfg
        binds = cfg.setdefault("chat_card_bindings", {})
        key = memory_key(str(chat))
        if str(card_id or "").strip():
            binds[key] = str(card_id)
        else:
            binds.pop(key, None)
        config_store.save_config(cfg, self.ctx.cfg_path)
        self.ctx.reproject_and_push()
        return {"ok": True}

    # ---------- 用量 ----------

    @_safe
    def get_usage(self) -> dict:
        store = UsageStore()
        records = store.load_records(days=30)
        s = store.summary(days=30)
        est = pricing.estimate_records(
            records, days=30,
            overrides=self.ctx.cfg.get("price_overrides") or {})
        today, total = s["today"], s["total"]
        cards = {
            "today_calls": today["calls"],
            "today_tokens": today["prompt"] + today["completion"],
            "today_cache": hit_ratio(today),
            "today_cost": est.get("today"),
            "tokens_30d": total["prompt"] + total["completion"],
            "fail_30d": total["fail"],
            "cost_30d": est.get("total"),
        }
        by_model = []
        for model, b in s["by_model"].items():
            rp = s["reply_by_model"].get(model) or {}
            avg = (rp["latency_sum"] / rp["count"] / 1000.0) if rp.get("count") else None
            by_model.append({"model": model, "calls": b["calls"],
                             "prompt": b["prompt"], "completion": b["completion"],
                             "fail": b["fail"], "cache": hit_ratio(b),
                             "avg_reply": avg, "est": b.get("est_calls", 0)})
        # 近 7 天按天按模型调用量（排除端到端耗时记录 kind=reply 与本地
        # 估算行 src=est——折线图只画实测口径）。天键必须走 _day_key：
        # SQLite 迁移后 ts 是 epoch 浮点，按字符串切片得到的是秒数前缀，
        # 永远匹配不上日期键（历史缺陷：折线图七天全零贴底不动）
        import datetime
        day_names = [(datetime.date.today() - datetime.timedelta(days=i))
                     .isoformat() for i in range(6, -1, -1)]
        day_model = {d: {} for d in day_names}
        for r in records:
            if r.get("kind") == "reply" or r.get("src") == "est":
                continue
            day = store._day_key(r.get("ts") or time.time())
            if day in day_model and r.get("model"):
                m = day_model[day]
                m[r["model"]] = m.get(r["model"], 0) + 1
        return {"ok": True, "cards": cards, "by_model": by_model,
                "days": day_names, "day_model": day_model}

    @_safe
    def refresh_balances(self) -> dict:
        threading.Thread(target=self._balance_worker, daemon=True,
                         name="xiaoli-balance").start()
        return {"ok": True, "started": True}

    def _balance_worker(self):
        rows = []
        for p in self.ctx.cfg.get("providers", []):
            key = str(p.get("api_key") or "").strip()
            if not key:
                continue
            if not balance.is_supported(p.get("id")):
                rows.append({"name": p.get("name") or p.get("id"),
                             "supported": False})
                continue
            r = balance.fetch_balance(p.get("id"), key)
            rows.append({"name": p.get("name") or p.get("id"),
                         "supported": True, **r})
        self.push("balances", {"rows": rows})

    @_safe
    def clear_usage(self) -> dict:
        return {"ok": bool(UsageStore().clear())}

    # ---------- 语音自检 ----------

    def _voice_ready_report(self):
        """语音就绪判定（与 wechat_bot._voice_profile_ready 同口径：端点 +
        激活档案 + 「通用」参考齐全）。返回 (profile or None, missing 列表)。
        读 ctx.cfg 现值——save_config 落盘前已热写同一份 dict，设置页保存
        即时反映。"""
        cfg = self.ctx.cfg
        if not str(cfg.get("tts_endpoint") or "").strip():
            return None, ["未配置 TTS 端点"]
        pid = str(cfg.get("active_voice_profile_id") or "")
        profile = next((p for p in (cfg.get("voice_profiles") or [])
                        if isinstance(p, dict) and str(p.get("id") or "") == pid),
                       None)
        if profile is None:
            return None, ["未激活任何音色档案"]
        base = (profile.get("refs") or {}).get("通用") or {}
        missing = []
        if not str(base.get("ref_audio_path") or "").strip():
            missing.append("「通用」参考音频路径")
        if not str(base.get("prompt_text") or "").strip():
            missing.append("「通用」参考文本稿")
        if missing:
            return None, missing
        return profile, []

    @_safe
    def voice_ready(self) -> dict:
        """语音就绪状态灯（纯本地判定，零 API）。"""
        profile, missing = self._voice_ready_report()
        return {"ok": True, "ready": profile is not None, "missing": missing}

    @_safe
    def voice_selftest(self) -> dict:
        """语音自检（长操作：daemon 线程 + push('voice_test') 回传）。

        先按就绪口径逐项报缺什么；全齐则用激活档案的「通用」参考真实合成
        一句问候，wav 以 base64 data URI 回传前端试听——用户当场验证自己
        接入的 TTS 服务是否可用（合成失败同样回传明确原因）。"""
        if getattr(self, "_voice_testing", False):
            return {"ok": False, "error": "自检已在进行中"}
        self._voice_testing = True

        def worker():
            try:
                profile, missing = self._voice_ready_report()
                if profile is None:
                    self.push("voice_test",
                              {"ok": False, "ready": False,
                               "message": "语音未就绪：缺 " + "、".join(missing)})
                    return
                from .tts import GptSovitsClient, TtsError, wav_duration_seconds
                client = GptSovitsClient(
                    str(self.ctx.cfg.get("tts_endpoint") or ""),
                    timeout=max(10, int(self.ctx.cfg.get("tts_timeout_seconds", 120))))
                wav = client.synthesize("你好呀，我是小漓！", profile["refs"]["通用"])
                dur = wav_duration_seconds(wav)
                audio = ("data:audio/wav;base64,"
                         + base64.b64encode(wav).decode("ascii"))
                self.push("voice_test", {"ok": True, "ready": True,
                                         "duration": round(dur, 1), "audio": audio})
            except Exception as e:
                self.push("voice_test",
                          {"ok": False, "ready": False,
                           "message": f"合成失败：{e}"})
            finally:
                self._voice_testing = False

        threading.Thread(target=worker, daemon=True,
                         name="xiaoli-voice-test").start()
        return {"ok": True, "started": True}

    # ---------- 表情包库 ----------

    @_safe
    def sticker_info(self) -> dict:
        """表情包库信息（设置页卡片）：目录、文件清单与描述（文件名播种 +
        manifest 缓存）。目录不存在返回空清单（功能整体不激活）。"""
        from .sticker_store import StickerStore, default_stickers_dir
        d = default_stickers_dir()
        store = StickerStore(d)
        entries = store.entries()
        return {"ok": True, "dir": d, "count": len(entries), "items": entries}

    @_safe
    def sticker_retag(self) -> dict:
        """全库 AI 打标（长操作：daemon 线程 + push('sticker_retag') 进度）。

        优先走运行中 bot 的视觉调用（重试/用量埋点同链路）；bot 未运行时
        按投影配置直连。打标结果写回 manifest.json。"""
        if getattr(self, "_sticker_retagging", False):
            return {"ok": False, "error": "打标已在进行中"}
        self._sticker_retagging = True

        def worker():
            try:
                from .sticker_store import StickerStore, default_stickers_dir
                store = StickerStore(default_stickers_dir())
                bot = getattr(self.ctx.engine, "bot", None)
                describe = None
                if bot is not None and hasattr(bot, "_describe_sticker"):
                    describe = bot._describe_sticker
                else:
                    from .llm_client import LlmClient
                    from wechat_bot import strip_model_prefix
                    client = LlmClient()
                    url = str(self.ctx.cfg.get("ai_api_url") or "")
                    key = str(self.ctx.cfg.get("ai_api_key") or "")
                    model = strip_model_prefix(
                        str(self.ctx.cfg.get("chat_model") or ""))

                    def describe(path):
                        from .sticker_store import describe_sticker
                        return describe_sticker(client.post, url, key,
                                                model, path)
                if describe is None:
                    self.push("sticker_retag",
                              {"ok": False, "message": "视觉调用不可用"})
                    return
                ok, total = store.reindex(
                    describe, progress_cb=lambda done, t:
                    self.push("sticker_retag",
                              {"ok": True, "done": done, "total": t}))
                self.push("sticker_retag",
                          {"ok": True, "done": total, "total": total,
                           "indexed": ok,
                           "message": f"打标完成：{ok}/{total}"})
            except Exception as e:
                self.push("sticker_retag",
                          {"ok": False, "message": f"{type(e).__name__}: {e}"})
            finally:
                self._sticker_retagging = False

        threading.Thread(target=worker, daemon=True,
                         name="xiaoli-sticker-retag").start()
        return {"ok": True, "started": True}

    # ---------- 任务桥（任务页） ----------

    @_safe
    def list_tasks(self) -> dict:
        """任务目录扫描（与 CLI task-status 共用 task_bridge.scan_task_status）：
        waiting/done 来自顶层任务目录，archived 来自 sent/ 归档。"""
        from .task_bridge import scan_task_status
        tasks_dir = str(self.ctx.cfg.get("tasks_dir") or "").strip()
        entries, waiting, done, archived = scan_task_status(tasks_dir)
        rows = [{"name": n, "state": st, "desc": desc, "mtime": mt}
                for (n, st, desc, mt) in entries]
        return {"ok": True, "rows": rows, "waiting": waiting, "done": done,
                "archived": archived, "tasks_dir": tasks_dir}

    @_safe
    def delete_task(self, name: str) -> dict:
        """删除任务目录（waiting/done = 顶层；archived = sent/ 下的归档）。
        前端必须先经用户确认；名字做路径安全校验（裸目录名，拒绝 sent 与
        隐藏项），防拼接逃逸出任务根目录。"""
        import shutil
        tasks_dir = str(self.ctx.cfg.get("tasks_dir") or "").strip()
        name = str(name or "").strip()
        if not tasks_dir or not name or os.path.basename(name) != name \
                or name == "sent" or name.startswith("."):
            return {"ok": False, "error": "非法任务名"}
        target = os.path.join(tasks_dir, name)
        if not os.path.isdir(target):
            return {"ok": False, "error": "任务目录不存在"}
        shutil.rmtree(target, ignore_errors=True)
        return {"ok": True, "deleted": name}

    # ---------- 环境 / 天枢 / 更新 / 首轮提示词 ----------

    @_safe
    def check_env(self) -> dict:
        return {"ok": True, "report": setup.check_environment(self.ctx.cfg)}

    @_safe
    def install_tianshu(self) -> dict:
        if self._installing:
            return {"ok": False, "error": "已有安装任务进行中"}
        self._installing = True

        def worker():
            def progress(pct):
                self.push("install", {"pct": int(pct), "done": False})
            try:
                dest = os.path.join(os.path.expanduser("~"), "Tianshu")
                found = setup.install_tianshu(dest, progress_cb=progress)
                cfg = self.ctx.cfg
                cfg["tianshu_install_dir"] = found
                config_store.save_config(cfg, self.ctx.cfg_path)
                self.push("install", {"done": True, "ok": True, "dir": found})
            except Exception as e:
                self.push("install", {"done": True, "ok": False,
                                      "error": f"{type(e).__name__}: {e}"})
            finally:
                self._installing = False

        threading.Thread(target=worker, daemon=True,
                         name="xiaoli-install").start()
        return {"ok": True, "started": True}

    @_safe
    def check_update(self) -> dict:
        r = update_check.check_latest_release()
        return {"ok": True, **(r or {})}

    @_safe
    def send_first_prompt(self) -> dict:
        """后台发送首轮提示词（定位天枢 CLI 窗口 → 粘贴 → 两次回车）。
        任务桥关闭时跳过——不唤起天枢也不发（天枢只服务于任务桥）。"""
        if getattr(self, "_prompt_running", False):
            return {"ok": False, "error": "发送已在进行中"}
        if not self.ctx.cfg.get("task_enabled", True):
            self.push("prompt", {"ok": False,
                                 "message": "任务桥未开启（设置页可开启），已跳过天枢唤起与首轮提示词"})
            return {"ok": True, "skipped": True}
        self._prompt_running = True

        def worker():
            try:
                cfg = self.ctx.cfg
                text = setup.build_first_prompt(cfg)
                fp = (cfg.get("first_prompt_path") or "").strip()
                if fp and os.path.isfile(fp):
                    try:
                        setup.open_first_prompt(fp)
                    except Exception:
                        pass
                title, detail = setup.resolve_cli_window(cfg)
                if not title:
                    self.push("prompt", {"ok": False, "message": detail})
                    return
                ok = setup.send_prompt_to_tianshu(text, title)
                self.push("prompt", {"ok": bool(ok),
                                     "message": "首轮提示词已发送给天枢 ✓"
                                     if ok else "发送失败（窗口未响应粘贴）"})
            except Exception as e:
                self.push("prompt", {"ok": False,
                                     "message": f"{type(e).__name__}: {e}"})
            finally:
                self._prompt_running = False

        threading.Thread(target=worker, daemon=True,
                         name="xiaoli-prompt").start()
        return {"ok": True, "started": True}

    # ---------- 首启引导 ----------

    @_safe
    def first_run_defaults(self) -> dict:
        cfg = self.ctx.cfg
        return {"ok": True,
                "tasks_dir": cfg.get("tasks_dir", ""),
                "file_storage_path": cfg.get("file_storage_path", "")
                or default_wechat_files_dir(),
                "memory_file": cfg.get("memory_file", "") or "memory.json",
                "bot_nickname": cfg.get("bot_nickname", "") or "小漓"}

    @_safe
    def save_first_run(self, tasks_dir: str, file_storage_path: str,
                       memory_file: str, bot_nickname: str) -> dict:
        cfg = self.ctx.cfg
        cfg["tasks_dir"] = str(tasks_dir or "").strip()
        cfg["file_storage_path"] = str(file_storage_path or "").strip()
        cfg["memory_file"] = str(memory_file or "").strip() or "memory.json"
        cfg["bot_nickname"] = str(bot_nickname or "").strip() or "小漓"
        config_store.sync_workdir_to_tasks(cfg)
        config_store.save_config(cfg, self.ctx.cfg_path)
        try:
            setup.ensure_bridge_readme(cfg["tasks_dir"])
        except Exception:
            pass
        try:
            config_store.grant_tasks_dir_to_tianshu(cfg["tasks_dir"])
        except Exception:
            pass
        return {"ok": True}

    @_safe
    def set_tasks_dir(self, new_dir: str, remove_old: bool = False) -> dict:
        """变更任务工作目录。remove_old=True 时删除旧目录内容
        （对齐旧 UI 行为；前端须先经用户确认）。"""
        new_dir = str(new_dir or "").strip()
        if not new_dir:
            return {"ok": False, "error": "目录不能为空"}
        cfg = self.ctx.cfg
        old = str(cfg.get("tasks_dir") or "").strip()
        if remove_old and old and os.path.isdir(old) \
                and old != cfg.get("tianshu_workdir"):
            shutil.rmtree(old, ignore_errors=True)
        cfg["tasks_dir"] = new_dir
        cfg["tianshu_guided"] = False
        config_store.sync_workdir_to_tasks(cfg)
        config_store.save_config(cfg, self.ctx.cfg_path)
        try:
            setup.ensure_bridge_readme(new_dir)
        except Exception:
            pass
        try:
            config_store.grant_tasks_dir_to_tianshu(new_dir)
        except Exception:
            pass
        return {"ok": True, "tasks_dir": new_dir,
                "removed_old": bool(remove_old and old and old != new_dir)}

    # ---------- 文件对话框 ----------

    @_safe
    def pick_folder(self, title: str = "选择目录") -> dict:
        if self._win is None:
            return {"ok": False, "error": "窗口未就绪"}
        import webview
        res = self._win.create_file_dialog(webview.FOLDER_DIALOG)
        p = res[0] if isinstance(res, (list, tuple)) and res else (
            res if isinstance(res, str) and res else None)
        return {"ok": True, "path": p, "canceled": not p}

    @_safe
    def pick_file(self, title: str = "选择文件",
                  file_types=None) -> dict:
        """通用文件选择（首轮提示词 / 卡片导入等）。"""
        if self._win is None:
            return {"ok": False, "error": "窗口未就绪"}
        import webview
        types = tuple(file_types) if file_types else ("所有文件 (*.*)",)
        res = self._win.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False, file_types=types)
        p = res[0] if isinstance(res, (list, tuple)) and res else (
            res if isinstance(res, str) and res else None)
        return {"ok": True, "path": p, "canceled": not p}

    @_safe
    def open_external(self, url: str) -> dict:
        """用系统默认程序打开外链（更新页等；WebView2 内不直接导航）。"""
        import webbrowser
        url = str(url or "").strip()
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "非法链接"}
        webbrowser.open(url)
        return {"ok": True}

    # ---------- 窗口控制 ----------

    @_safe
    def win_min(self) -> dict:
        if self._win is not None:
            self._win.minimize()
        return {"ok": True}

    @_safe
    def win_max_toggle(self) -> dict:
        """最大化 ⇄ 还原。pywebview Window 没有 maximized 状态属性可查，
        桥侧自记状态（前端重复点击 / maximize 前置 hide 场景以自记为准）。"""
        win = self._win
        if win is None:
            return {"ok": True}
        self._max_state = not getattr(self, "_max_state", False)
        try:
            if self._max_state:
                win.maximize()
            else:
                win.restore()
        except Exception:
            pass
        return {"ok": True}

    @_safe
    def win_hide(self) -> dict:
        """关窗按钮语义：隐藏到托盘（退出走托盘菜单）。"""
        if self._win is not None:
            self._win.hide()
        self.notify("小漓", "已最小化到托盘，双击图标重新打开")
        return {"ok": True}

    @_safe
    def quit_app(self) -> dict:
        """真退出：销毁窗口令 webview.start() 返回，入口收尾停引擎/托盘。"""
        self._stop.set()
        if self._win is not None:
            try:
                self._win.destroy()
            except Exception:
                pass
        return {"ok": True}
