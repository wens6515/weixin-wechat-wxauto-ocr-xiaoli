# -*- coding: utf-8 -*-
"""语音合成并发基准：多段回复「串行合成 vs 并发合成」的总耗时对比。

测的是客户端调度层（xiaoli_app.tts.GptSovitsClient 真实代码路径 +
ThreadPoolExecutor 并发方案，即将来 _send_voice_reply 的候选实现）。
真实收益还取决于 TTS 服务端能否并行推理：单卡部署的 GPT-SoVITS api_v2
会把并发请求排队，GPU 推理串行时客户端并发不省总时长——用真机模式跑一次
即可分辨。

两种模式：
  --mock    内置本地 mock TTS 服务（模拟 api_v2 /tts：每段固定推理延迟 +
            返回合法 wav），验证客户端并发调度收益，不依赖外部服务。
  真机      --endpoint 指向实际部署的 api_v2 端点（config.json tts_endpoint
            同值），--ref-audio / --prompt-text 用服务端视角的参考音频。

用法：
  python tools/bench_tts_parallel.py --mock
  python tools/bench_tts_parallel.py --mock --mock-delay 0.5
  python tools/bench_tts_parallel.py --endpoint http://127.0.0.1:9880/tts ^
      --ref-audio "D:/tts/ref.wav" --prompt-text "参考音频说的话"
"""
import argparse
import io
import sys
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app.tts import GptSovitsClient, TtsError  # noqa: E402

if getattr(sys.stdout, "encoding", "utf-8") != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# 场景文本：回复纪律下的典型分段（每段 1-3 句，短叹词独立成行）
SCENES = {
    "3 段（典型回复）": [
        "在的在的，刚才没看手机",
        "你说的是那个游戏活动吧，我看到公告了",
        "周末一起打呀，我正好也在线",
    ],
    "5 段（长回复）": [
        "来啦来啦",
        "作业我帮你看了，第二题思路有点问题",
        "你把公式代错位置了",
        "应该先化简再代入，不然算出来会很奇怪",
        "改完发我看看就行，不着急",
    ],
}


def _tiny_wav(seconds=0.05, rate=16000):
    """生成最小合法 wav（RIFF 魔数校验可过）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


class _MockHandler(BaseHTTPRequestHandler):
    delay = 1.5

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        time.sleep(self.delay)
        wav = _tiny_wav()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav)))
        self.end_headers()
        self.wfile.write(wav)

    def log_message(self, *_a):
        pass


def synthesize_all(client, ref, texts, workers):
    """候选实现：串行（workers=1）或并发合成全部段。任一段失败抛 TtsError
    （与现行 fail-closed 语义一致：合成阶段失败整条回退文本）。"""
    if workers <= 1:
        return [client.synthesize(t, ref) for t in texts]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(lambda t: client.synthesize(t, ref), texts))


def bench(client, ref, workers, rounds):
    """返回 {场景名: [每轮总耗时秒]}；任一轮失败返回 None。"""
    out = {}
    for name, texts in SCENES.items():
        times = []
        for _ in range(rounds):
            t0 = time.perf_counter()
            try:
                synthesize_all(client, ref, texts, workers)
            except TtsError as e:
                print(f"  [失败] {name}: {e}")
                return None
            times.append(time.perf_counter() - t0)
        out[name] = times
    return out


def main():
    ap = argparse.ArgumentParser(description="语音合成并发基准")
    ap.add_argument("--mock", action="store_true", help="使用内置 mock TTS 服务")
    ap.add_argument("--mock-delay", type=float, default=1.5,
                    help="mock 模式每段模拟推理延迟秒（默认 1.5）")
    ap.add_argument("--endpoint", default="", help="真机 api_v2 /tts 端点")
    ap.add_argument("--ref-audio", default="", help="服务端视角参考音频路径")
    ap.add_argument("--prompt-text", default="", help="参考音频对应的提示文本")
    ap.add_argument("--workers", default="1,2,3",
                    help="并发数列表（默认 1,2,3；1=串行基线）")
    ap.add_argument("--rounds", type=int, default=2, help="每配置轮数（默认 2）")
    args = ap.parse_args()

    if args.mock:
        _MockHandler.delay = args.mock_delay
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        endpoint = f"http://127.0.0.1:{srv.server_address[1]}/tts"
        print(f"mock TTS 服务已启动 {endpoint}（每段延迟 {args.mock_delay}s）")
        ref = {"ref_audio_path": "mock/ref.wav", "prompt_text": "mock"}
    else:
        endpoint = args.endpoint.strip()
        ref = {"ref_audio_path": args.ref_audio.strip(),
               "prompt_text": args.prompt_text.strip()}
        if not endpoint or not ref["ref_audio_path"]:
            print("真机模式需要 --endpoint 与 --ref-audio（服务端视角路径）")
            return 1

    client = GptSovitsClient(endpoint, timeout=60)
    worker_list = [int(w) for w in args.workers.split(",") if w.strip()]
    print(f"\n每配置 {args.rounds} 轮，文本经真实 GptSovitsClient 合成\n")

    results = {}
    for workers in worker_list:
        tag = "串行" if workers == 1 else f"并发×{workers}"
        print(f"== {tag} ==")
        r = bench(client, ref, workers, args.rounds)
        if r is None:
            return 1
        results[workers] = r
        for name, times in r.items():
            avg = sum(times) / len(times)
            print(f"  {name}: 平均 {avg:.2f}s（{' / '.join(f'{t:.2f}' for t in times)}）")
        print()

    base = results.get(1)
    if base:
        print("== 相对串行的提速 ==")
        for name in SCENES:
            b = sum(base[name]) / len(base[name])
            row = []
            for workers in worker_list:
                if workers == 1:
                    continue
                t = sum(results[workers][name]) / len(results[workers][name])
                row.append(f"×{workers}: {b / t:.1f} 倍速")
            print(f"  {name}: {'，'.join(row)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
