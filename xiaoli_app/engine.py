# -*- coding: utf-8 -*-
"""引擎线程：把 AgentBot 包装为可启停的后台线程 + 事件总线。

GUI 启动流程：config_store 加载/迁移/投影 → EngineThread(bot_factory) → start()
bot 在子线程内创建（AgentBot.__init__ 会连微信，可能阻塞，不能在 UI 主线程做）。
UI 侧通过 bus 拉取事件（QTimer 轮询 drain），控制命令走 pause/resume/apply_role。
"""

import queue
import threading
import time
import traceback

BUS_MSG = "message"      # 收到消息
BUS_TASK = "task"        # 任务状态变化
BUS_STATUS = "status"    # 引擎状态（started/paused/running/stopped）
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
    """AgentBot 引擎线程。bot 由工厂延迟创建（连微信阻塞 → 子线程内）。"""

    def __init__(self, bot_factory, bus=None, poll_interval=2.0, name="xiaoli-engine"):
        super().__init__(name=name, daemon=True)
        self._bot_factory = bot_factory
        self.bus = bus or EngineBus()
        self.poll_interval = poll_interval
        self._stop_evt = threading.Event()
        self._lock = threading.RLock()
        self.bot = None  # 子线程内创建；仅用于 UI 检查状态

    # ---------- 生命周期 ----------

    def run(self):
        try:
            self.bot = self._bot_factory()
            self.bus.emit(BUS_STATUS, {"state": "started"})
            self.bot.run(stop_event=self._stop_evt, poll_interval=self.poll_interval)
        except Exception as e:
            self.bus.emit(BUS_ERROR, {"message": f"引擎异常: {e}", "trace": traceback.format_exc()})
        finally:
            self.bus.emit(BUS_STATUS, {"state": "stopped"})

    def stop(self, timeout=10):
        """请求停止并等待线程退出。返回是否已退出。"""
        self._stop_evt.set()
        if self.is_alive():
            self.join(timeout)
        return not self.is_alive()

    # ---------- 控制（线程安全） ----------

    def pause(self):
        with self._lock:
            if self.bot is not None:
                self.bot.paused = True
                self.bus.emit(BUS_STATUS, {"state": "paused"})

    def resume(self):
        with self._lock:
            if self.bot is not None:
                self.bot.paused = False
                self.bus.emit(BUS_STATUS, {"state": "running"})

    def apply_role(self, card, providers=None):
        """热切换角色卡。返回是否已应用（bot 未就绪时 False）。"""
        with self._lock:
            if self.bot is not None:
                self.bot.apply_role(card, providers)
                return True
        return False

    def is_running(self):
        return self.is_alive() and self.bot is not None and not self.bot.paused
