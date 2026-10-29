# -*- coding: utf-8 -*-
"""
watch.py —— 「看一条视频」的三档工具（不要一次搬 55 MB）

为什么有三档（2026-10-29 实测得出的）：
  · 抖音视频 55.4 MB，其中音轨只有 1.78 MB（3.2%），但 MP4 把音画**交织**存，
    要拿全部音轨就必须从头读到尾 —— 所以"只抽音频"省不了时间。
  · 服务器**支持 Range**（实测要 1 MB 就回 206）。
    ⇒ 只取某一段或某几帧，非常便宜；只有要全稿才值得整条下载。
  · 画面**根本不用下载**：按秒 seek 单帧，每次只读那一小段。

三档：
  L0  只看结构     ── 均匀抽 N 帧（默认 8）      下载量 ≈ 几 MB     约半分钟
  L1  听某一段     ── 指定秒数区间抽帧 + 转写    下载量 ≈ 4–12 MB   约 1–2 分钟
  L2  要全稿/入库  ── 整条下载 + 场景切分抽帧 + 全片转写   55 MB    约 10 分钟

用法（URL 是浏览器里 <video>.currentSrc 那条 https 直链，3 小时内有效）：
  python watch.py --url "<直链>" --id <视频id> --tier L0
  python watch.py --url "<直链>" --id <视频id> --tier L1 --seg 60-90
  python watch.py --url "<直链>" --id <视频id> --tier L2
  python watch.py --url "<直链>" --id <视频id> --tier L2 --reuse "E:\\...\\test.mp4"   # 已有文件就不重下

产物落在 <outroot>\\<日期>_<id>\\ ：
  key_01.jpg …   关键帧
  transcript.txt 带时间戳的文字稿（L1/L2）
  raw.wav        转写用的 16k 单声道音频
  摘要.md        待你填一句话的骨架 + 本次的实测数字
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = os.environ.get("FFPROBE") or shutil.which("ffprobe") or "ffprobe"
HERE = os.path.dirname(os.path.abspath(__file__))
# 默认用当前解释器；想指定别的就用 --python
PY = sys.executable
DEFAULT_MODEL = os.environ.get("WHISPER_MODEL_DIR") or os.path.join(HERE, "models", "faster-whisper-tiny")
OUTROOT = os.path.join(HERE, "out")

# 这条线路不要走代理（代理到 HF 的 CDN 只有几百字节/秒）
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(k, None)

HEADERS = "Referer: https://www.douyin.com/\r\n"


def inp(src):
    """输入参数：只有 http(s) 源才加 -headers。

    ⚠ 2026-10-29 踩过：`-headers` 只对 http 协议有效，对着本地文件加它会直接报错，
    表现为「本地 mp4 明明完好，抽帧和转写却全失败」。
    """
    if src.startswith("http"):
        return ["-headers", HEADERS]
    return []


def run(cmd, quiet=True):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0 and not quiet:
        print(p.stderr[-800:])
    return p


def probe(url_or_file):
    """返回 (时长秒, 视频码率kbps, 音频码率kbps, 分辨率)"""
    p = run([FFPROBE, "-v", "error"] + inp(url_or_file) +
             ["-show_entries", "format=duration,bit_rate:stream=codec_type,bit_rate,width,height",
             "-of", "default=nw=1", url_or_file])
    txt = p.stdout
    dur = 0.0
    for line in txt.splitlines():
        if line.startswith("duration="):
            try:
                dur = float(line.split("=", 1)[1])
            except ValueError:
                pass
    return dur, txt


def grab_frames(url, times, outdir, idx_start=1):
    """按秒数逐帧取。每次是一条 Range 读，很便宜。"""
    made = []
    for i, t in enumerate(times):
        dst = os.path.join(outdir, "key_%02d.jpg" % (idx_start + i))
        p = run([FFMPEG, "-hide_banner", "-loglevel", "error"] + inp(url) +
                 ["-ss", "%.2f" % t, "-i", url, "-frames:v", "1", "-q:v", "2", "-y", dst])
        if os.path.exists(dst) and os.path.getsize(dst) > 1000:
            made.append((t, dst))
            print("  帧 @%6.1fs → %s" % (t, os.path.basename(dst)), flush=True)
        else:
            print("  帧 @%6.1fs 失败" % t, flush=True)
    return made


def scene_times(url_or_file, max_frames=40, threshold=0.25):
    """用 ffmpeg 的场景切换检测找剪辑点（比均匀抽帧准，能省八成帧）"""
    p = run([FFMPEG, "-hide_banner"] + inp(url_or_file) + ["-i", url_or_file,
             "-vf", "select='gt(scene,%s)',metadata=print:file=-" % threshold,
             "-an", "-f", "null", "-"])
    times = [float(m) for m in re.findall(r"pts_time:([\d.]+)", p.stdout + p.stderr)]
    times = sorted(set(times))
    if not times:
        return []
    if len(times) > max_frames:
        step = len(times) / float(max_frames)
        times = [times[int(i * step)] for i in range(max_frames)]
    return times


def extract_audio(url_or_file, out_wav, start=None, dur=None):
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error"] + inp(url_or_file)
    if start is not None:
        cmd += ["-ss", "%.2f" % start]
    if dur is not None:
        cmd += ["-t", "%.2f" % dur]
    cmd += ["-i", url_or_file, "-vn", "-ac", "1", "-ar", "16000", "-y", out_wav]
    run(cmd)
    return os.path.exists(out_wav) and os.path.getsize(out_wav) > 1000


def transcribe(wav, out_txt, model=DEFAULT_MODEL, lang="zh"):
    cmd = [PY, "-X", "utf8", os.path.join(HERE, "asr.py"), wav, model, lang]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    txt = p.stdout
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write(txt)
    n = txt.count("[")
    print("  转写 %d 段 → %s" % (n, os.path.basename(out_txt)), flush=True)
    tail = [l for l in txt.splitlines() if l.startswith("----")]
    if tail:
        print("  " + tail[-1], flush=True)
    return txt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="<video>.currentSrc 的 https 直链")
    ap.add_argument("--id", default="", help="视频 id，用来命名目录")
    ap.add_argument("--tier", default="L0", choices=["L0", "L1", "L2"])
    ap.add_argument("--seg", default="", help="L1 用：起止秒，如 60-90")
    ap.add_argument("--frames", type=int, default=8, help="L0 抽几帧")
    ap.add_argument("--reuse", default="", help="已有本地 mp4（L2 用，省一次下载）")
    ap.add_argument("--outroot", default=OUTROOT, help="产物根目录，默认 ./out")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="faster-whisper 模型目录（也可用 WHISPER_MODEL_DIR 环境变量）")
    ap.add_argument("--python", default=PY, help="跑 asr.py 的解释器，默认当前解释器")
    a = ap.parse_args()

    vid = a.id or re.sub(r"\W", "", a.url)[-16:]
    stamp = time.strftime("%Y%m%d_%H%M")
    outdir = os.path.join(a.outroot, "%s_%s" % (stamp, vid))
    os.makedirs(outdir, exist_ok=True)
    t_start = time.time()
    print("=== %s  档位 %s" % (os.path.basename(outdir), a.tier), flush=True)

    dur, info = probe(a.reuse or a.url)
    print("  时长 %.1f 秒" % dur, flush=True)

    src = a.reuse or a.url
    notes = ["视频 id: %s" % vid, "档位: %s" % a.tier, "时长: %.1f 秒" % dur, ""]

    if a.tier == "L0":
        n = max(2, a.frames)
        times = [dur * (i + 0.5) / n for i in range(n)] if dur else []
        print("  L0：均匀抽 %d 帧（0 下载视频文件）" % n, flush=True)
        grab_frames(src, times, outdir)
        notes.append("抽帧：均匀 %d 帧" % n)

    elif a.tier == "L1":
        if not a.seg:
            print("L1 需要 --seg 起-止，例如 --seg 60-90")
            return 2
        s, e = [float(x) for x in a.seg.split("-")]
        print("  L1：只取 %.0f–%.0f 秒" % (s, e), flush=True)
        grab_frames(src, [s + (e - s) * (i + 0.5) / 4 for i in range(4)], outdir)
        wav = os.path.join(outdir, "raw.wav")
        if extract_audio(src, wav, start=s, dur=e - s):
            transcribe(wav, os.path.join(outdir, "transcript.txt"))
        notes.append("音频区间：%.0f–%.0f 秒" % (s, e))

    else:  # L2
        mp4 = a.reuse
        if not mp4:
            mp4 = os.path.join(outdir, "video.mp4")
            print("  L2：整条下载（这一步最慢，主要耗在网络）", flush=True)
            run([FFMPEG, "-hide_banner", "-loglevel", "error", "-headers", HEADERS,
                 "-i", a.url, "-c", "copy", "-y", mp4])
            print("  下载完成 %.1f MB" % (os.path.getsize(mp4) / 1048576.0), flush=True)
        times = scene_times(mp4)
        print("  场景切换检测到 %d 个剪辑点" % len(times), flush=True)
        if times:
            grab_frames(mp4, times, outdir)
        else:
            grab_frames(mp4, [dur * (i + 0.5) / 8 for i in range(8)], outdir)
        wav = os.path.join(outdir, "raw.wav")
        if extract_audio(mp4, wav):
            transcribe(wav, os.path.join(outdir, "transcript.txt"))
        notes.append("抽帧：按场景切换 %d 个" % len(times))

    cost = time.time() - t_start
    notes += ["总耗时: %.1f 秒" % cost, "", "## 一句话摘要", "", "（待填）", "",
              "## 关键点", "", "- ", "", "## 出处", "", "- 视频 id: %s" % vid]
    with open(os.path.join(outdir, "摘要.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(notes))
    print("=== 完成，%.1f 秒，产物在 %s" % (cost, outdir), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
