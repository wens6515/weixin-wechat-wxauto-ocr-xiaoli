# -*- coding: utf-8 -*-
"""TTS 客户端契约测试（音源接口化）：payload 形态、wav 时长、fail-closed、
情绪参考自动回退「通用」。

不连真实端点——requests.post 打桩；情绪回退用替身客户端记录每次用的
参考音频路径。
"""
import io
import sys
import unittest
import wave
from unittest import mock

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

import requests

from xiaoli_app.tts import (GptSovitsClient, TtsError, strip_unspeakable,
                            synthesize_with_emotion, wav_duration_seconds)


def _wav_bytes(seconds=0.5, rate=8000):
    """构造一段合法 wav 字节流（单声道 16bit 静音）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class _FakeResp:
    def __init__(self, status=200, content=b""):
        self.status_code = status
        self.content = content


PROFILE = {
    "id": "p1", "name": "三月七",
    "refs": {
        "通用": {"ref_audio_path": "/srv/ref.wav", "prompt_text": "通用文本"},
        "开心": {"ref_audio_path": "/srv/happy.wav", "prompt_text": "开心文本"},
    },
}


class TestWavDuration(unittest.TestCase):
    def test_duration_matches_frames_over_rate(self):
        dur = wav_duration_seconds(_wav_bytes(1.0, 8000))
        self.assertAlmostEqual(dur, 1.0, places=2)

    def test_non_wav_raises(self):
        with self.assertRaises(TtsError):
            wav_duration_seconds(b"not a wav at all")

    def test_empty_raises(self):
        with self.assertRaises(TtsError):
            wav_duration_seconds(b"")


class TestGptSovitsClient(unittest.TestCase):
    def _client(self):
        return GptSovitsClient("http://127.0.0.1:9880/tts", timeout=30)

    def test_payload_shape(self):
        """payload 是 GPT-SoVITS api_v2 /tts 的 JSON 子集：cut0 + wav。"""
        ref = {"ref_audio_path": "/srv/ref.wav", "prompt_text": "文本稿"}
        with mock.patch("xiaoli_app.tts.requests.post",
                        return_value=_FakeResp(200, _wav_bytes())) as post:
            self._client().synthesize("你好呀", ref)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["text"], "你好呀")
        self.assertEqual(payload["text_lang"], "zh")
        self.assertEqual(payload["ref_audio_path"], "/srv/ref.wav")
        self.assertEqual(payload["prompt_text"], "文本稿")
        self.assertEqual(payload["prompt_lang"], "zh")
        self.assertEqual(payload["text_split_method"], "cut0")
        self.assertEqual(payload["media_type"], "wav")

    def test_ok_returns_wav_bytes(self):
        wav = _wav_bytes()
        with mock.patch("xiaoli_app.tts.requests.post",
                        return_value=_FakeResp(200, wav)):
            self.assertEqual(self._client().synthesize("hi", {
                "ref_audio_path": "/x.wav", "prompt_text": "词"}), wav)

    def test_http_error_raises(self):
        with mock.patch("xiaoli_app.tts.requests.post",
                        return_value=_FakeResp(500, b"err")):
            with self.assertRaises(TtsError):
                self._client().synthesize("hi", {
                    "ref_audio_path": "/x.wav", "prompt_text": "词"})

    def test_non_wav_body_raises(self):
        """端点把错误页/JSON 当 200 返回——魔数校验拦截，绝不发垃圾。"""
        with mock.patch("xiaoli_app.tts.requests.post",
                        return_value=_FakeResp(200, b'{"error": "x"}')):
            with self.assertRaises(TtsError):
                self._client().synthesize("hi", {
                    "ref_audio_path": "/x.wav", "prompt_text": "词"})

    def test_request_exception_raises(self):
        with mock.patch("xiaoli_app.tts.requests.post",
                        side_effect=requests.ConnectionError("boom")):
            with self.assertRaises(TtsError):
                self._client().synthesize("hi", {
                    "ref_audio_path": "/x.wav", "prompt_text": "词"})

    def test_empty_endpoint_raises(self):
        with self.assertRaises(TtsError):
            GptSovitsClient("").synthesize("hi", {
                "ref_audio_path": "/x.wav", "prompt_text": "词"})

    def test_empty_ref_path_raises(self):
        with self.assertRaises(TtsError):
            self._client().synthesize("hi", {"prompt_text": "词"})


class _RecordingClient:
    """替身客户端：记录每次合成用的参考路径；happy 路径恒失败（测回退）。"""

    def __init__(self, fail_paths=()):
        self.calls = []
        self.fail_paths = set(fail_paths)

    def synthesize(self, text, ref):
        path = ref["ref_audio_path"]
        self.calls.append(path)
        if path in self.fail_paths:
            raise TtsError("该参考不可用")
        return _wav_bytes(0.2)


class TestStripUnspeakable(unittest.TestCase):
    """语音合成前剥除颜文字/emoji（真机实测：(｡･ω･｡) 被读成「欧米奇」）。"""

    def test_user_reported_case(self):
        self.assertEqual(
            strip_unspeakable("那就好，下回别趴着压肚子了(｡･ω･｡)"),
            "那就好，下回别趴着压肚子了")

    def test_kaomoji_with_attached_decoration(self):
        """括号外的假名/emoji 残留一并剥掉：(｡･ω･｡)ﾉ♡"""
        self.assertEqual(strip_unspeakable("(｡･ω･｡)ﾉ♡你好呀"), "你好呀")

    def test_discipline_example_with_arabic(self):
        """(๑•̀ㅁ•́๑)و✧ → 括号段 + و + ✧ 全剥。"""
        self.assertEqual(strip_unspeakable("开心(๑•̀ㅁ•́๑)و✧"), "开心")

    def test_ascii_kaomoji(self):
        self.assertEqual(strip_unspeakable("(>_<)别这样"), "别这样")

    def test_contentful_parens_kept(self):
        """含汉字/字母数字的括号是真内容，保留。"""
        self.assertEqual(strip_unspeakable("好耶～（真心话）20分钟(OK)"),
                         "好耶～（真心话）20分钟(OK)")

    def test_table_flip(self):
        self.assertEqual(strip_unspeakable("气死了(╯°□°）╯︵┻━┻"), "气死了")

    def test_empty_after_strip(self):
        self.assertEqual(strip_unspeakable("(≧▽≦)"), "")

    def test_plain_text_untouched(self):
        s = "那就好，下回别趴着压肚子了！It's 20:14 ~ 好耶～"
        self.assertEqual(strip_unspeakable(s), s)


class TestEmotionFallback(unittest.TestCase):
    def test_emotion_ref_used_directly(self):
        client = _RecordingClient()
        out = synthesize_with_emotion(client, PROFILE, "你好", "开心")
        self.assertEqual(client.calls, ["/srv/happy.wav"])
        self.assertEqual(out, _wav_bytes(0.2))

    def test_emotion_ref_failure_falls_back_to_common(self):
        """情绪参考合成失败 → 自动退「通用」重试一次。"""
        client = _RecordingClient(fail_paths={"/srv/happy.wav"})
        out = synthesize_with_emotion(client, PROFILE, "你好", "开心")
        self.assertEqual(client.calls, ["/srv/happy.wav", "/srv/ref.wav"])
        self.assertEqual(out, _wav_bytes(0.2))

    def test_no_emotion_uses_common(self):
        client = _RecordingClient()
        synthesize_with_emotion(client, PROFILE, "你好", None)
        self.assertEqual(client.calls, ["/srv/ref.wav"])

    def test_unknown_emotion_uses_common(self):
        client = _RecordingClient()
        synthesize_with_emotion(client, PROFILE, "你好", "生气")
        self.assertEqual(client.calls, ["/srv/ref.wav"])

    def test_all_failures_raise(self):
        client = _RecordingClient(fail_paths={"/srv/happy.wav", "/srv/ref.wav"})
        with self.assertRaises(TtsError):
            synthesize_with_emotion(client, PROFILE, "你好", "开心")

    def test_missing_common_ref_raises(self):
        profile = {"id": "p", "refs": {"开心": PROFILE["refs"]["开心"]}}
        with self.assertRaises(TtsError):
            synthesize_with_emotion(_RecordingClient(), profile, "你好", None)


if __name__ == "__main__":
    unittest.main()
