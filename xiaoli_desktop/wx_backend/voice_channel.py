# -*- coding: utf-8 -*-
"""visual_backend 拆分 · 语音发送通道：VB-CABLE 虚拟声卡 + SendInput 右 Alt 触发微信录音。

fail-closed 不变量见门面 send_voice（端点探测/持久渲染流/环回质检/设备
切换恢复）。被门面 re-export，导入路径不变。
"""
from __future__ import annotations

import ctypes
import logging
import time

from .visual_win32 import capture_window

logger = logging.getLogger(__name__)

# ---------- 语音发送（VB-CABLE 虚拟声卡 + SendInput 右 Alt 触发微信录音） ----------
# 蓝本 .rivet/scratch/voice_send_dafeiyu.py（持久流）、voice_send_v3.py
# （SendInput + 胶囊检测）、session_helpers.py（设备切换），真机多轮验证。
# 不变量（交接文档定案）：fail-closed 全链路；默认录音设备 finally 恢复；
# 右 Alt 必须走 SendInput 物理模拟（pyautogui 对 Qt 程序无效）；采样率/
# 端点动态探测；持久渲染流做质检与播放（冷流随机死零）。

_PUL = ctypes.POINTER(ctypes.c_ulong)
_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_INPUT_KEYBOARD = 1
_VK_RMENU = 0xA5
_SCAN_ALT = 0x38


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", _PUL)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", _PUL)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort),
                ("wParamH", ctypes.c_ushort)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT),
                ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("union", _INPUTunion)]


def _send_kb(vk, scan, flags):
    inp = _INPUT(type=_INPUT_KEYBOARD)
    inp.union.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0,
                               dwExtraInfo=None)
    n = u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
    return n == 1


def alt_down(right=True):
    """按下 Alt（物理级模拟）：右 Alt = 语音消息，必须带扩展键标志 + 扫描码
    ——Qt 程序不认裸 VK 码，pyautogui 的 keyDown 无效（真机实测）。"""
    if right:
        return _send_kb(_VK_RMENU, _SCAN_ALT, _KEYEVENTF_EXTENDEDKEY)
    return _send_kb(0xA4, _SCAN_ALT, 0)


def alt_up(right=True):
    if right:
        return _send_kb(_VK_RMENU, _SCAN_ALT,
                        _KEYEVENTF_EXTENDEDKEY | _KEYEVENTF_KEYUP)
    return _send_kb(0xA4, _SCAN_ALT, _KEYEVENTF_KEYUP)


def _capsule_green_count(arr):
    """「松开发送」胶囊绿像素计数（纯函数，单测直测）。

    真机标定：胶囊在窗口底部 y∈[0.86h,0.99h]、x∈[0.5w,0.95w]，绿判据
    G>120 且 G-R>40 且 G-B>20；计数 >150 即录音已触发（实测 915）。"""
    region = arr[int(arr.shape[0] * 0.86):int(arr.shape[0] * 0.99),
                 int(arr.shape[1] * 0.5):int(arr.shape[1] * 0.95)]
    r, g, b = region[:, :, 0], region[:, :, 1], region[:, :, 2]
    mask = (g > 120) & (g - r > 40) & (g - b > 20)
    return int(mask.sum())


def _send_pill_visible(hwnd):
    """按住右 Alt 后检测输入区绿色「松开发送」胶囊是否出现（录音已触发）。"""
    import numpy as np
    shot = capture_window(hwnd)
    if shot is None:
        return False
    arr = np.asarray(shot.convert("RGB"), dtype=np.int16)
    return _capsule_green_count(arr) > 150


def _pick_cable_endpoints(devices, wasapi_idx):
    """从设备清单挑 VB-CABLE 端点对（纯函数，单测直测）。

    播放端=扬声器（VB-Audio）、录音端=CABLE Output，按品牌子串匹配并排除
    带「4-」的第二条声缆（Pack43 装出的别名端点，真机实测）。两个方向
    各取首个命中；缺失为 None。"""
    out_idx = in_idx = None
    for idx, d in enumerate(devices):
        name = str(d.get("name") or "")
        if d.get("hostapi") != wasapi_idx or "4-" in name:
            continue
        if out_idx is None and "VB-Audio" in name \
                and int(d.get("max_output_channels") or 0) > 0:
            out_idx = idx
        elif in_idx is None and "CABLE Output" in name \
                and int(d.get("max_input_channels") or 0) > 0:
            in_idx = idx
    return out_idx, in_idx


# 默认录音设备切换（IPolicyConfig，SoundSwitch 同款公开做法；蓝本
# session_helpers.py 真机验证可用）
_CLSID_POLICY_CONFIG = "{870af99c-171d-4f9e-af0d-e63df40c2bc9}"
_SETDEFAULT_SLOT = 13  # IUnknown(3) + GetMixFormat..SetPropertyValue(10) 之后
_POLICY_IIDS = (
    "{F8679F50-850A-41CF-9C72-430F290290C8}",   # Win7~11 通用
    "{CA286FC3-91FD-42C3-8E9B-CAAFA66242E3}",   # 备用
)


def _policy_guid(s):
    g = (ctypes.c_ulong * 4)()
    hr = ctypes.windll.ole32.IIDFromString(ctypes.c_wchar_p(s), g)
    if hr != 0:
        raise RuntimeError(f"IIDFromString 失败 0x{hr:08x}")
    return g


def _set_default_endpoint(device_id, role=0):
    """role: 0=console 1=multimedia 2=communications。成功返回 True。"""
    from ctypes import POINTER, WINFUNCTYPE, HRESULT, c_int, c_void_p, c_wchar_p
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)
    for iid_s in _POLICY_IIDS:
        clsid = _policy_guid(_CLSID_POLICY_CONFIG)
        iid = _policy_guid(iid_s)
        pp = c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 1,
                                    ctypes.byref(iid), ctypes.byref(pp))
        if hr != 0 or not pp.value:
            continue
        vtbl_ptr = ctypes.cast(pp, POINTER(c_void_p)).contents.value
        slots = ctypes.cast(vtbl_ptr, POINTER(c_void_p * 15)).contents
        fn = WINFUNCTYPE(HRESULT, c_void_p, c_wchar_p, c_int)(slots[_SETDEFAULT_SLOT])
        hr = fn(pp, device_id, role)
        WINFUNCTYPE(HRESULT, c_void_p)(slots[2])(pp)  # Release
        if hr == 0:
            return True
    return False


def _default_capture_id(role_value):
    """当前默认录音端点 id（role 为 ERole 值）。失败返回 None。"""
    try:
        from pycaw.pycaw import AudioUtilities, EDataFlow
        ctypes.windll.ole32.CoInitialize(None)
        enum = AudioUtilities.GetDeviceEnumerator()
        dev = enum.GetDefaultAudioEndpoint(EDataFlow.eCapture.value, role_value)
        return dev.GetId()
    except Exception:
        return None


def _find_endpoint_id_by_name(name_sub, capture=True):
    """按名字找活动端点 id（排除带「4-」的别名声缆）。未找到返回 None。"""
    try:
        from pycaw.constants import DEVICE_STATE
        from pycaw.pycaw import AudioUtilities, EDataFlow
        ctypes.windll.ole32.CoInitialize(None)
        enum = AudioUtilities.GetDeviceEnumerator()
        flow = EDataFlow.eCapture.value if capture else EDataFlow.eRender.value
        coll = enum.EnumAudioEndpoints(flow, DEVICE_STATE.ACTIVE.value)
        for i in range(coll.GetCount()):
            d = coll.Item(i)
            nm = AudioUtilities.CreateDevice(d).FriendlyName or ""
            if name_sub.lower() in nm.lower() and "4-" not in nm:
                return d.GetId()
    except Exception:
        return None
    return None


class _VoiceChannel:
    """VB-CABLE 语音通道（进程内单例）：端点动态探测 + 持久渲染流（桥保温）
    + 环回质检 + 默认录音设备三 role 切换/恢复。

    持久流（用户实测定案）：「播一遍即关」的冷流模式环回质检随机死零；
    进程内常开一条 OutputStream，质检与正式发送共用同一条流写数据，桥
    保持热态。采样率不写死：渲染端按候选表试开、录音端质检时按候选表
    试采——设备不认的组合 PortAudio 直接报错，自动换下一个。"""

    _RENDER_RATES = (48000, 44100)
    _CAPTURE_RATES = (44100, 48000)
    _GATE_ATTEMPTS = 5
    _GATE_MIN_RMS = 1e-4
    _CAPTURE_ROLES = (0, 1, 2)  # eConsole / eMultimedia / eCommunications

    def __init__(self):
        self._stream = None
        self.render_rate = None
        self.out_idx = None
        self.in_idx = None

    def _discover(self):
        """枚举 WASAPI 设备挑 VB-CABLE 端点对。失败返回 False（引导装声缆）。"""
        import sounddevice as sd
        apis = sd.query_hostapis()
        wasapi = next((i for i, a in enumerate(apis)
                       if "WASAPI" in str(a.get("name") or "")), None)
        if wasapi is None:
            return False
        devices = [{"name": str(d["name"]), "hostapi": int(d["hostapi"]),
                    "max_input_channels": int(d["max_input_channels"]),
                    "max_output_channels": int(d["max_output_channels"])}
                   for d in sd.query_devices()]
        out_idx, in_idx = _pick_cable_endpoints(devices, wasapi)
        if out_idx is None or in_idx is None:
            return False
        self.out_idx, self.in_idx = out_idx, in_idx
        return True

    def ensure_stream(self):
        """端点探测 + 持久渲染流确保已开（每次发送前调用，端点消失自动重探）。"""
        import sounddevice as sd
        if self._stream is not None:
            try:
                if self._stream.active:
                    return True
            except Exception:
                pass
            self._close_stream()
        if not self._discover():
            return False
        for rate in self._RENDER_RATES:
            try:
                stream = sd.OutputStream(samplerate=rate, device=self.out_idx,
                                         channels=2, dtype="float32")
                stream.start()
            except Exception:
                continue  # 该采样率设备不认（实测播放端只收 48000）
            self._stream = stream
            self.render_rate = rate
            logger.info(f"[语音] 持久渲染流已启动（端点 {self.out_idx}，"
                        f"{rate}Hz）")
            return True
        logger.warning("[语音] 渲染流启动失败（VB-CABLE 播放端不可用）")
        return False

    def load_resampled(self, wav_path):
        """读 wav 并重采样到渲染流规格（float32 双声道）。
        返回 (data, 时长秒)；内容为空返回 (None, 0)。"""
        import numpy as np
        import soundfile as sf
        data, sr = sf.read(wav_path, dtype="float32", always_2d=True)
        if data.shape[1] == 1:
            data = np.column_stack([data[:, 0], data[:, 0]])
        else:
            data = data[:, :2]
        n = int(data.shape[0] * self.render_rate / max(1, sr))
        if n <= 0:
            return None, 0.0
        out = np.column_stack([
            np.interp(np.linspace(0, data.shape[0] - 1, n),
                      np.arange(data.shape[0]), data[:, c])
            for c in (0, 1)]).astype(np.float32)
        return out, n / float(self.render_rate)

    def loopback_gate(self, data48):
        """环回质检闸门：经持久流写一遍音频，同时从录音端实采，电平达到
        人声量级才放行——采不到信号绝不进入发送（宁可回退文本不发静音）。
        每个候选录音采样率最多 _GATE_ATTEMPTS 次。"""
        import numpy as np
        import sounddevice as sd
        dur = data48.shape[0] / float(self.render_rate)
        for rate in self._CAPTURE_RATES:
            for _attempt in range(self._GATE_ATTEMPTS):
                try:
                    # rec 先开（不阻塞）→ write 阻塞至音频播完 → 再等 0.8s
                    # 恰好补齐 rec 窗口（rec 总长 = dur + 0.8），读数时已录满
                    rec = sd.rec(int(rate * (dur + 0.8)), samplerate=rate,
                                 channels=2, device=self.in_idx,
                                 dtype="float32", blocking=False)
                    self._stream.write(data48)
                    time.sleep(0.8)
                    seg = rec[:, 0]
                    mx = float(np.max(np.abs(seg)))
                    rms = float(np.sqrt(np.mean(seg ** 2)))
                except Exception as e:
                    logger.debug(f"[语音] 环回尝试失败（{rate}Hz）: {e}")
                    break  # 该采样率设备不认，换下一个
                if mx <= 1.0 and self._GATE_MIN_RMS < rms:
                    logger.info(f"[语音] 环回质检通过（{rate}Hz，"
                                f"|max|={mx:.4f} RMS={rms:.6f}）")
                    return True
                time.sleep(0.5)
        logger.warning("[语音] 环回质检未通过，中止（不发静音）")
        return False

    def write(self, data48):
        self._stream.write(data48)

    def switch_default_capture(self):
        """三个 role 的默认录音设备切到 CABLE Output（逐一回读校验）。
        返回原默认端点 {role: id}（供 finally 恢复）；切换未确认返回 None
        （并已就地恢复，真实麦克风保证未动）。"""
        cable_id = _find_endpoint_id_by_name("CABLE Output", capture=True)
        if not cable_id:
            logger.warning("[语音] 未找到 CABLE Output 录音端点")
            return None
        orig = {r: _default_capture_id(r) for r in self._CAPTURE_ROLES}
        ok = all(_set_default_endpoint(cable_id, role=r)
                 for r in self._CAPTURE_ROLES)
        now = {r: _default_capture_id(r) for r in self._CAPTURE_ROLES}
        if not ok or any(now.get(r) != cable_id for r in self._CAPTURE_ROLES):
            logger.warning("[语音] 默认录音设备切换未确认，中止（真实麦克风未动）")
            self.restore_default_capture(orig)
            return None
        return orig

    def restore_default_capture(self, orig):
        """恢复原默认录音设备（三 role；finally 必跑——不恢复用户麦克风失效）。"""
        if not orig:
            return
        for r, dev_id in orig.items():
            if dev_id:
                try:
                    _set_default_endpoint(dev_id, role=r)
                except Exception:
                    pass

    def _close_stream(self):
        try:
            if self._stream is not None:
                self._stream.close()
        except Exception:
            pass
        self._stream = None


_VOICE_CHANNEL = None


def _get_voice_channel():
    """进程内语音通道单例；音频依赖缺失返回 None（语音发送整体停用）。"""
    global _VOICE_CHANNEL
    if _VOICE_CHANNEL is None:
        try:
            import numpy  # noqa: F401
            import sounddevice  # noqa: F401
            import soundfile  # noqa: F401
        except ImportError as e:
            logger.warning(f"[语音] 音频依赖不可用（{e}），语音发送停用")
            _VOICE_CHANNEL = False
            return None
        _VOICE_CHANNEL = _VoiceChannel()
    return _VOICE_CHANNEL or None
