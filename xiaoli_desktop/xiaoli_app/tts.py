# -*- coding: utf-8 -*-
"""TTS 客户端（音源接口化）：小漓只做 HTTP 客户端，语音模型/权重/参考音频
全部在用户自己部署的服务端——小漓出厂不携带任何音色，端点与音色档案由
用户在设置页配置。

一期协议 = GPT-SoVITS api_v2 /tts 的 JSON 子集。「接口化」落在代码层而非
配置层：字段映射集中在 GptSovitsClient 一个 adapter 里，将来接其他引擎
= 新增一个 adapter 类，发送链路零改动。

音色档案（config.json voice_profiles 项）结构：
  {"id": "...", "name": "三月七",
   "refs": {"通用": {"ref_audio_path": "...", "prompt_text": "..."},
            "开心": {...}, "生气": {...}}}
- ref_audio_path 是 **TTS 服务器文件系统上的路径**（不是小漓所在机器的
  路径——单机部署时是同一台机，分布式部署时填那台机上的路径）
- 「通用」参考必填；其余键是情绪参考（键名即 send_voice 工具暴露的
  emotion 枚举），可选——用户模型包自带哪些情绪目录就配哪些键
"""
import io
import re
import wave

import requests


class TtsError(Exception):
    """TTS 合成失败（端点不可达/超时/返回非音频）。调用方 fail-closed 回退文本。"""


# ---------- 合成前文本清洗（颜文字/emoji 读不出人话） ----------
# 回复纪律允许颜文字，但 TTS 会把它们读成乱语（真机实测：(｡･ω･｡) 被读成
# 「欧米奇」）。剥除只作用于**语音合成的输入**——记忆与文本回复保持原样，
# 人设的颜文字风味不丢。
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF"    # emoji 主体（含扩展段）
    "\U0001F1E6-\U0001F1FF"     # 区域指示符（旗帜）
    "\u2600-\u27BF"             # 杂项符号与装饰（♡✧☆★…）
    "\u2B00-\u2BFF"             # 其他符号（⭐等）
    "\uFE0F\u200D]+")           # 变体选择符 / 零宽连接符
_PAREN_KAOMOJI_RE = re.compile(r"([（(])([^()（）]*)([)）])")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ALNUM_RE = re.compile(r"[0-9A-Za-z]")
# 颜文字剥除后挂在括号外的残留装饰字符类：假名（含半角 ﾉ）、希腊/阿拉伯
# 字母（ω و）、制表与几何符号（╯┻▽）、纵向 presentation 符号（︵）——
# 中文语音文本里这些字符不会合法出现，整类剥掉
_RESIDUE_RE = re.compile(
    "[\u2500-\u25FF\u0370-\u03FF\u0600-\u06FF"
    "\u3040-\u30FF\uFF66-\uFF9F\uFE30-\uFE4F]+")


def strip_unspeakable(text):
    """剥掉 TTS 读不出人话的装饰段，返回可读文本。

    - 括号段内容**只有符号/生僻字母**（无汉字、无 ASCII 字母数字）→ 判为
      颜文字整段剥掉（含括号）：(｡･ω･｡)、(>_<)、(๑•̀ㅁ•́๑)；
      里面有汉字或字母数字的括号是真内容，保留：（真心话）、(20分钟)、(OK)
    - emoji 与颜文字专用字符类（假名/希腊/阿拉伯/制表符号）整类剥掉
    - 中文、英文、数字与常规标点（含 ～ … — ——）不受影响
    """
    t = _PAREN_KAOMOJI_RE.sub(
        lambda m: m.group(0)
        if (_CJK_RE.search(m.group(2)) or _ALNUM_RE.search(m.group(2)))
        else "", text or "")
    t = _EMOJI_RE.sub("", t)
    t = _RESIDUE_RE.sub("", t)
    return re.sub(r"\s{2,}", " ", t).strip()


def wav_duration_seconds(wav_bytes):
    """wav 字节流的精确时长（秒，帧数/采样率）。非 wav 抛 TtsError。"""
    if not wav_bytes or bytes(wav_bytes[:4]) != b"RIFF":
        raise TtsError("TTS 返回内容不是 wav 音频")
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            rate = w.getframerate()
            if rate <= 0:
                raise TtsError("wav 采样率非法")
            return w.getnframes() / float(rate)
    except wave.Error as e:
        raise TtsError(f"wav 解析失败: {e}") from e


class GptSovitsClient:
    """GPT-SoVITS api_v2 /tts HTTP 客户端（无状态，可按次构造）。"""

    def __init__(self, endpoint, timeout=120, text_lang="zh", prompt_lang="zh"):
        self.endpoint = str(endpoint or "").strip()
        self.timeout = float(timeout)
        self.text_lang = text_lang
        self.prompt_lang = prompt_lang

    def synthesize(self, text, ref):
        """按参考音频合成一段文本，返回 wav 字节流。失败抛 TtsError。

        ref: {"ref_audio_path", "prompt_text"}——均为服务端视角。
        text_split_method=cut0（服务端不切）：分段由小漓按回复段控制，
        每次合成的就是一条语音条的量。
        """
        if not self.endpoint:
            raise TtsError("TTS 端点未配置")
        ref_path = str((ref or {}).get("ref_audio_path") or "").strip()
        prompt_text = str((ref or {}).get("prompt_text") or "").strip()
        if not ref_path:
            raise TtsError("参考音频路径为空")
        payload = {
            "text": text,
            "text_lang": self.text_lang,
            "ref_audio_path": ref_path,
            "prompt_text": prompt_text,
            "prompt_lang": self.prompt_lang,
            "text_split_method": "cut0",
            "media_type": "wav",
        }
        try:
            resp = requests.post(self.endpoint, json=payload, timeout=self.timeout)
        except requests.RequestException as e:
            raise TtsError(f"TTS 端点请求失败: {e}") from e
        if resp.status_code != 200:
            raise TtsError(f"TTS 端点返回 {resp.status_code}")
        data = resp.content or b""
        if data[:4] != b"RIFF":
            # 端点把错误页/JSON 当音频返回的情况——校验魔数，绝不把垃圾发出去
            raise TtsError("TTS 返回内容不是 wav 音频")
        return data


def synthesize_with_emotion(client, profile, text, emotion=None):
    """按情绪选参考合成：情绪参考失败自动退「通用」重试一次（情绪目录
    配错路径不该整条丢语音），再失败抛 TtsError（调用方整条回退文本）。

    profile：音色档案 dict（refs 键 = 情绪名，「通用」必填）。
    """
    refs = dict((profile or {}).get("refs") or {})
    base = refs.get("通用")
    if not base or not str(base.get("ref_audio_path") or "").strip():
        raise TtsError("音色档案缺少「通用」参考音频")
    attempts = []
    if emotion and emotion != "通用" and refs.get(emotion):
        attempts.append(refs[emotion])
    attempts.append(base)
    last = None
    for ref in attempts:
        try:
            return client.synthesize(text, ref)
        except TtsError as e:
            last = e
    raise last
