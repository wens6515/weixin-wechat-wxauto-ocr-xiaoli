# -*- coding: utf-8 -*-
"""语音发送后端纯逻辑测试：VB-CABLE 端点挑选、录音胶囊绿像素判定、
wav 重采样加载。

不碰真实声卡/窗口——SendInput、持久流、设备切换的集成路径由真机验收，
这里钉住可纯测的判定函数（阈值/匹配口径改动即测试红）。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from wx_backend.visual_backend import (_VoiceChannel, _capsule_green_count,
                                       _pick_cable_endpoints)


def _dev(name, hostapi=0, inch=0, outch=0):
    return {"name": name, "hostapi": hostapi,
            "max_input_channels": inch, "max_output_channels": outch}


WASAPI = 0


class TestPickCableEndpoints(unittest.TestCase):
    def test_picks_pair_and_excludes_alias_cable(self):
        """按品牌子串配对，排除带「4-」的第二条声缆别名端点。"""
        devices = [
            _dev("麦克风阵列 (Senary Audio)", WASAPI, inch=2),
            _dev("扬声器 (VB-Audio Virtual Cable)", WASAPI, outch=2),
            _dev("CABLE Output (VB-Audio Virtual Cable)", WASAPI, inch=2),
            _dev("扬声器 (4- VB-Audio Virtual Cable)", WASAPI, outch=2),
            _dev("CABLE Output (4- VB-Audio Virtual Cable)", WASAPI, inch=2),
        ]
        out_idx, in_idx = _pick_cable_endpoints(devices, WASAPI)
        self.assertEqual(out_idx, 1)
        self.assertEqual(in_idx, 2)

    def test_missing_recording_endpoint(self):
        devices = [_dev("扬声器 (VB-Audio Virtual Cable)", WASAPI, outch=2)]
        out_idx, in_idx = _pick_cable_endpoints(devices, WASAPI)
        self.assertEqual(out_idx, 0)
        self.assertIsNone(in_idx)

    def test_direction_must_match(self):
        """录音端不能顶播放端（方向过滤）。"""
        devices = [_dev("CABLE Output (VB-Audio Virtual Cable)", WASAPI, inch=2)]
        out_idx, in_idx = _pick_cable_endpoints(devices, WASAPI)
        self.assertIsNone(out_idx)
        self.assertEqual(in_idx, 0)

    def test_other_hostapi_ignored(self):
        devices = [
            _dev("扬声器 (VB-Audio Virtual Cable)", hostapi=1, outch=2),
            _dev("CABLE Output (VB-Audio Virtual Cable)", hostapi=1, inch=2),
        ]
        out_idx, in_idx = _pick_cable_endpoints(devices, WASAPI)
        self.assertIsNone(out_idx)
        self.assertIsNone(in_idx)


class TestCapsuleGreenCount(unittest.TestCase):
    def _arr(self, fill_region=True):
        """整窗灰底 + 按需把胶囊检测区涂绿（窗口 200x1000 模拟）。"""
        arr = np.full((1000, 200, 3), (47, 47, 48), dtype=np.int16)
        if fill_region:
            arr[int(1000 * 0.86):int(1000 * 0.99),
                int(200 * 0.5):int(200 * 0.95)] = (30, 210, 141)
        return arr

    def test_green_capsule_detected(self):
        self.assertGreater(_capsule_green_count(self._arr(True)), 150)

    def test_gray_window_not_triggered(self):
        self.assertEqual(_capsule_green_count(self._arr(False)), 0)

    def test_green_outside_region_ignored(self):
        """绿块不在检测区（如消息气泡绿）不计数。"""
        arr = np.full((1000, 200, 3), (47, 47, 48), dtype=np.int16)
        arr[:100, :50] = (30, 210, 141)
        self.assertEqual(_capsule_green_count(arr), 0)


class TestLoadResampled(unittest.TestCase):
    def test_mono_resampled_to_render_rate_stereo(self):
        import soundfile as sf
        ch = _VoiceChannel()
        ch.render_rate = 100
        data = np.full(50, 0.5, dtype=np.float32)  # 50Hz 采样 50 帧 = 1s
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            sf.write(path, data, 50)
            out, dur = ch.load_resampled(path)
        finally:
            os.unlink(path)
        self.assertEqual(out.shape, (100, 2))  # 重采样到 100Hz → 100 帧
        self.assertAlmostEqual(dur, 1.0, places=6)

    def test_empty_content_returns_none(self):
        import soundfile as sf
        ch = _VoiceChannel()
        ch.render_rate = 100
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            sf.write(path, np.zeros(0, dtype=np.float32), 50)
            out, dur = ch.load_resampled(path)
        finally:
            os.unlink(path)
        self.assertIsNone(out)
        self.assertEqual(dur, 0.0)


if __name__ == "__main__":
    unittest.main()
