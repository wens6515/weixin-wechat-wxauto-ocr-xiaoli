# -*- coding: utf-8 -*-
"""引擎线程：把 AgentBot 包装为可启停的后台线程 + 事件总线（生命周期状态机）。

状态机：idle → initializing → initialized → running ⇄ paused → stopped
- idle：线程已启动但未初始化（应用启动即空闲，什么都不跑）
- initialize()：子线程创建 AgentBot（连微信/加载记忆/去重/成果登记）→ initialized
- start_bot()：进入主循环 → running
- pause()/resume()：running ⇄ paused
- stop()：退出线程 → stopped

UI 侧通过 bus 拉取状态事件（QTimer 轮询 drain），控制命令走 initialize/start_bot/pause/resume。
"""

import queue
import threading
import time
import traceback

BUS_MSG = "message"      # 收到消息
BUS_TASK = "task"        # 任务状态变化
BUS_STATUS = "status"    # 引擎状态（idle/initializing/initialized/running/paused/error/stopped）
BUS_ERROR = "error"      # 引擎异常


class EngineBus:
    """引擎 → UI 事件总线（线程安全队列，UI 定时拉取）。"""

    def __init__(self):
        self._q = queue.Queue()

    def emit(self, kind, payload=None):
        self._q.put((kind, payload or {}))

    def drain(self, limit=200):
        """取走全部待处理事件（上限 limit，防 UI 卡顿）。"""
        out = []
        while len(out) < limit:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                break
        return out


class EngineThread(threading.Thread):
    """AgentBot 引擎线程。bot 由工厂在 initialize() 时创建（连微信阻塞 → 子线程内）。

    poll_interval 默认 0.5s 恒定快档（定案：不间断监听优先）。每轮 =
    后台静默截图 + 红圈像素检测，真机实测约占总 CPU 1%（tools/poll_benchmark.py
    可复测）；无红圈时零 OCR 零点击零前台抢占。
    """

    def __init__(self, bot_factory, bus=None, poll_interval=0.5, name="xiaoli-engine"):
        super().__init__(name=name, daemon=True)
        self._bot_factory = bot_factory
        self.bus = bus or EngineBus()
        self.poll_interval = poll_interval
        self._stop_evt = threading.Event()
        self._init_request = threading.Event()
        self._run_request = threading.Event()
        self._lock = threading.RLock()
        self.bot = None          # 子线程内创建；仅用于 UI 检查状态
        self.state = "idle"      # idle|initializing|initialized|running|paused|error|stopped
        self.error = None        # 初始化失败信息

    # ---------- 线程主体：指令循环 ----------

    def run(self):
        while not self._stop_evt.is_set():
            if self._init_request.is_set():
                self._init_request.clear()
                self._do_initialize()
            if (self._run_request.is_set() and self.bot is not None
                    and not self.bot.paused and not self._stop_evt.is_set()):
                self._run_loop()
            else:
                self._stop_evt.wait(0.05)

    def _do_initialize(self):
        self._set_state("initializing")
        try:
            # factory 可接受可选 stop_event 关键字（bot 连接微信时可被引擎
            # 停止中断）；无参 factory（测试/旧调用方）保持兼容。
            try:
                self.bot = self._bot_factory(stop_event=self._stop_evt)
            except TypeError:
                self.bot = self._bot_factory()
            self._set_state("initialized")
        except Exception as e:
            self.error = f"初始化失败: {e}"
            self.bus.emit(BUS_ERROR, {"message": self.error, "trace": traceback.format_exc()})
            self._set_state("error")

    def _run_loop(self):
        """主循环：直到 stop / pause / run_request 被清。"""
        while (not self._stop_evt.is_set() and self._run_request.is_set()
               and self.bot is not None and not self.bot.paused):
            try:
                self.bot.process_new_messages()
            except Exception as e:
                self.bus.emit(BUS_ERROR, {"message": f"主循环异常: {e}", "trace": traceback.format_exc()})
            # 可中断睡眠：pause/stop 响应延迟 ≤100ms
            remain = self.poll_interval
            while (remain > 1e-9 and not self._stop_evt.is_set()
                   and self._run_request.is_set() and self.bot is not None and not self.bot.paused):
                time.sleep(min(0.1, remain))
                remain -= 0.1

    def _set_state(self, s):
        self.state = s
        self.bus.emit(BUS_STATUS, {"state": s})

    # ---------- 对外 API ----------

    def initialize(self):
        """请求初始化（创建 bot：连微信/加载记忆）。非阻塞，状态经 bus 通知。
        返回是否接受了请求（已初始化/运行中时拒绝重复初始化）。"""
        with self._lock:
            if self.state in ("initialized", "running", "paused", "initializing"):
                return False
        if not self.is_alive():
            self.start()
        self._init_request.set()
        return True

    def start_bot(self):
        """启动主循环（需已初始化）。返回是否已进入 running。"""
        with self._lock:
            if self.bot is None:
                return False
            self.bot.paused = False
            self._run_request.set()
        self._set_state("running")
        return True

    def pause(self):
        with self._lock:
            if self.bot is not None:
                self.bot.paused = True
            self._run_request.clear()
        self._set_state("paused")

    def resume(self):
        with self._lock:
            if self.bot is not None:
                self.bot.paused = False
            self._run_request.set()
        self._set_state("running")

    def stop(self, timeout=10):
        """请求停止并等待线程退出。返回是否已退出。"""
        self._stop_evt.set()
        if self.is_alive():
            self.join(timeout)
        if not self.is_alive():
            self._set_state("stopped")
        return not self.is_alive()

    def apply_role(self, card, providers=None):
        """热切换角色卡。返回是否已应用（bot 未就绪时 False）。"""
        with self._lock:
            if self.bot is not None:
                self.bot.apply_role(card, providers)
                return True
        return False

    def clear_memory(self):
        """清空运行中 bot 的记忆（内存 memory_db + 落盘）。返回是否已清空。

        bot 未就绪（未初始化）或清空失败时返回 False，调用方据此回退到
        直接清文件。历史缺陷：GUI 只写空文件、不动 bot 内存 memory_db，
        bot 节流写盘（_schedule_save_memory/_flush_memory）会把旧记忆覆盖
        回磁盘，导致「清空全部记忆」按钮失效。
        """
        with self._lock:
            bot = self.bot
            if bot is not None and hasattr(bot, "clear_history"):
                try:
                    bot.clear_history()
                    return True
                except Exception:
                    return False
        return False

    def is_running(self):
        return self.state == "running"
