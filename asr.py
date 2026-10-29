# -*- coding: utf-8 -*-
"""
asr.py —— 把音频文件转成带时间戳的中文文字稿

用法：
    python asr.py <音频路径> [模型大小] [语言]

    ｜模型大小｜显存/内存 ｜速度 ｜备注 ｜
    |---|---|---|---|
    | tiny   | 很小 | 飞快 | 只能听个大概 |
    | base   | 小   | 快   | |
    | small  | 中   | 可以 | **默认**，中文够用 |
    | medium | 1.5G | 慢   | 中文明显更准，6G 显存跑得动（CPU 很慢） |
    | large-v3 | 3G | 最慢 | 6G 显存紧张 |

为什么默认 small：先证明链路通，再谈准确率。换模型只改一个参数，不用改代码。

设计上的三条：
1. **优先 GPU，失败自动退 CPU** —— Windows 上 ctranslate2 的 CUDA/cuDNN 经常缺库，
   不能让它一崩就整个跑不成。
2. **输出带时间戳**（秒），因为要跟抽帧对齐：0.5s 处说的那句话，对应哪一帧。
3. **内存友好**：逐段 yield，不一次性拼完再打印（长音频会撑爆）。

输出格式（stdout）：
    [   3.20 →    6.80] 文本……
    ...
    ---- 统计：共 N 段，音频 X 秒，耗时 Y 秒，RTF=Z ----
"""
import sys
import os
import time

# HuggingFace 下模型在国内可能连不上，默认走镜像。
# 想用官方源就把这行注释掉（或者设 HF_ENDPOINT 环境变量覆盖）。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")


def load_wav_16k(path):
    """自己读 wav，绕过 faster-whisper 的 PyAV 解码。

    为什么要这个（2026-10-29 实测）：faster-whisper 1.2.1 调 `av.open(..., metadata_errors="ignore")`，
    而当前装的 PyAV 19.0.2 已经不认这个参数了 → TypeError。
    修法有两条：降级 PyAV，或者干脆不用它。
    选后者 —— 我们这边的音频本来就固定是 ffmpeg 抽出来的 16 kHz / 单声道 / 16 bit PCM，
    用标准库 wave 读进来就是 numpy float32，一个字节都不用转。
    （非 wav 或不合适时返回 None，交回原来的解码路径。）
    """
    import wave
    import numpy as np

    try:
        with wave.open(path, "rb") as w:
            if w.getnchannels() != 1 or w.getframerate() != 16000 or w.getsampwidth() != 2:
                return None
            raw = w.readframes(w.getnframes())
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    except Exception:
        return None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    audio = sys.argv[1]
    size = sys.argv[2] if len(sys.argv) > 2 else "small"
    lang = sys.argv[3] if len(sys.argv) > 3 else "zh"

    if not os.path.exists(audio):
        print("找不到音频：", audio)
        return 2

    from faster_whisper import WhisperModel

    t0 = time.time()

    # 优先自己解码 wav（绕开 PyAV 的坑，见 load_wav_16k 的注释）
    audio_in = load_wav_16k(audio)
    if audio_in is not None:
        print("── 音频：自己解码 wav，%.1f 秒" % (len(audio_in) / 16000.0))
    else:
        print("── 音频：交给 faster-whisper 解码（PyAV）")
        audio_in = audio

    # ⚠ 2026-10-29 的坑：ctranslate2 **加载** cuda 模型时不检查 cuBLAS，
    # 一直要到真正 encode 才报 `Library cublas64_12.dll is not found`。
    # 所以"能不能用 GPU"必须在 transcribe 里才验得出来 —— 整个 transcribe 都要包在 try 里。
    # 又因为 segments 是生成器，异常可能在遍历时才冒出来，所以这里 list() 先物化。
    # （音频都只有几分钟，物化不心疼；真处理长音频再改成流式。）
    segs, info, used = None, None, None
    for dev, ctype in (("cuda", "float16"), ("cpu", "int8")):
        try:
            model = WhisperModel(size, device=dev, compute_type=ctype)
            segs, info = model.transcribe(audio_in, language=lang, vad_filter=True,
                                          beam_size=5, initial_prompt=None)
            segs = list(segs)
            used = "%s (%s)" % (dev, ctype)
            print("── 设备：%s" % used)
            break
        except Exception as e:
            msg = str(e).splitlines()[0][:120] if str(e) else type(e).__name__
            print("── %s/%s 不可用：%s" % (dev, ctype, msg))
    if segs is None:
        print("两种设备都跑不动，检查 ctranslate2 / CUDA 运行库")
        return 1

    t_load = time.time() - t0

    n = 0
    last_end = 0.0
    for s in segs:
        n += 1
        last_end = s.end
        print("[%7.2f → %7.2f] %s" % (s.start, s.end, s.text.strip()))

    audio_len = getattr(info, "duration", last_end) or last_end
    cost = time.time() - t0
    rtf = (cost / audio_len) if audio_len else 0
    print("---- 统计：共 %d 段，音频 %.1f 秒，总耗时 %.1f 秒（含加载 %.1f），RTF=%.2f ----"
          % (n, audio_len, cost, t_load, rtf))
    return 0


if __name__ == "__main__":
    sys.exit(main())
