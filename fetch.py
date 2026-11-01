# -*- coding: utf-8 -*-
"""
fetch.py —— 从 hf-mirror 下 faster-whisper 模型文件（自己发 Range 头续传）

为什么要自己写，而不是 `curl -C -`：
  2026-10-29 实测，`curl -C -` 对着 hf-mirror 的 model.bin **不做续传** ——
  服务端回 200 而不是 206，curl 就把文件从头覆盖，来回几次永远停在 20 MB。
  所以这里自己发 `Range: bytes=<已下载>-`：
    · 服务端回 206 → 追加写
    · 服务端回 200 → 不识 Range，只能从头写（会重新写一遍，但至少不是悄悄覆盖）
  另外每次写盘都 fsync 一下，断电/断网不会把已经下到的部分弄没。

用法：
    python fetch.py <repo> <输出目录> [文件1 文件2 ...]
例：
    python fetch.py Systran/faster-whisper-tiny ./models/faster-whisper-tiny
"""
import os
import sys
import time
import urllib.request
import urllib.error

MIRROR = "https://hf-mirror.com"
DEFAULT_FILES = ["config.json", "tokenizer.json", "vocabulary.txt", "model.bin"]

# 这条线路不走代理（代理到 HF 的 CDN 只有几百字节/秒）
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(k, None)


def remote_size(url):
    """先 HEAD；HEAD 不给长度就用 Range: bytes=0-0 从 Content-Range 里读总长。

    为什么要退路（2026-10-29 实测）：hf-mirror 对某些文件的 HEAD 不返回
    Content-Length，此时 rsize=0，代码就会去请求 Range: bytes=<本地大小>-，
    服务端回 416，被当成"中断"反复重试 —— 明明文件已经完整。
    """
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            n = r.headers.get("Content-Length")
            if n:
                return int(n)
    except Exception:
        pass
    try:
        req2 = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req2, timeout=25) as r:
            cr = r.headers.get("Content-Range") or ""
            if "/" in cr:
                return int(cr.rsplit("/", 1)[1])
    except Exception:
        pass
    return 0


def verify_content_range(header, have, rsize):
    """返回 None 表示可信；返回字符串表示不可信的原因。

    VD-03：必须同时满足
      · 起点 == 本地已有字节数（否则拼进去就是错位）
      · 总长与已知远端大小一致（否则是另一个文件）
      · 结束 >= 起点
    服务端不标准实现很常见，所以只做"不符合就重来"，不做"猜它想干嘛"。
    """
    import re as _re
    m = _re.match(r'bytes\s+(\d+)-(\d+)/(\d+|\*)', header.strip())
    if not m:
        return 'Content-Range 无法解析: %r' % header[:60]
    start, end, total = int(m.group(1)), int(m.group(2)), m.group(3)
    if start != have:
        return 'start=%d 但本地已有 %d 字节' % (start, have)
    if end < start:
        return 'end < start（%d-%d）' % (start, end)
    if total != '*' and rsize and int(total) != rsize:
        return '总长 %s 与已知远端 %d 不一致' % (total, rsize)
    return None


def verify_file(out, rsize, min_bytes=4096):
    """VD-01：下载结束后的完整性判定。返回 (ok, reason)。

    原来只要"本地非空"就算成功 —— 远端大小未知时，一个 200 字节的 HTML 错误页
    也能通过，然后被当成模型文件喂给下游。
    """
    if not os.path.exists(out):
        return False, '文件不存在'
    size = os.path.getsize(out)
    if size < min_bytes:
        return False, '文件过小（%d 字节 < %d）' % (size, min_bytes)
    with open(out, 'rb') as f:
        head = f.read(64)
    if head[:1] in (b'<', b'{') and b'<' in head:
        return False, '看起来是 HTML/JSON 错误页，不是二进制资源'
    if rsize and size != rsize:
        return False, '大小不符：%d != 远端 %d' % (size, rsize)
    if not rsize:
        return True, '大小未知，仅验证了非空且非错误页（无哈希可用）'
    return True, '大小一致'


def fetch_one(url, out, max_tries=500):
    rsize = remote_size(url)
    print("远端大小: %s" % (("%.1f MB" % (rsize / 1048576)) if rsize else "未知"), flush=True)

    for attempt in range(1, max_tries + 1):
        have = os.path.getsize(out) if os.path.exists(out) else 0
        if rsize and have >= rsize:
            break
        if rsize == 0 and have > 0 and attempt > 1:
            break

        headers = {"User-Agent": "Mozilla/5.0"}
        if have:
            headers["Range"] = "bytes=%d-" % have
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                code = r.getcode()
                # VD-03（外部审查 2026-11-01）：206 必须核对 Content-Range，
                # 否则服务端/代理回错片段时会把错位字节直接拼进文件，而且不报错。
                if code == 206 and have:
                    cr = r.headers.get("Content-Range") or ""
                    bad = verify_content_range(cr, have, rsize)
                    if bad:
                        print("  ⚠ Content-Range 不可信（%s），丢弃本次响应重来" % bad, flush=True)
                        time.sleep(2)
                        continue
                mode = "ab" if (code == 206 and have) else "wb"
                if mode == "wb" and have:
                    print("  ⚠ 服务端不认 Range（HTTP %d），从头写" % code, flush=True)
                with open(out, mode) as f:
                    while True:
                        chunk = r.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                    f.flush()
                    os.fsync(f.fileno())
        except Exception as e:
            msg = str(e)
            # 416 = 起点已越过文件末尾，说明本地这份其实已经完整
            if "416" in msg and have > 0:
                print("  已有完整文件（服务端 416），跳过", flush=True)
                return True
            print("  第 %d 次中断：%s" % (attempt, msg[:100]), flush=True)
            time.sleep(3)

        now = os.path.getsize(out) if os.path.exists(out) else 0
        pct = ("%5.1f%%" % (now * 100.0 / rsize)) if rsize else "  ?  "
        print("  第 %d 次后：%.1f / %.1f MB  %s" % (attempt, now / 1048576,
                                                 (rsize / 1048576) if rsize else 0, pct), flush=True)

    final = os.path.getsize(out) if os.path.exists(out) else 0
    ok, why = verify_file(out, rsize)
    print("结束 %s：%.1f MB %s —— %s" % (os.path.basename(out), final / 1048576,
                                        "OK" if ok else "未完成", why), flush=True)
    return ok


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    repo, outdir = sys.argv[1], sys.argv[2]
    files = sys.argv[3:] or DEFAULT_FILES
    os.makedirs(outdir, exist_ok=True)
    allok = True
    for f in files:
        url = "%s/%s/resolve/main/%s" % (MIRROR, repo, f)
        out = os.path.join(outdir, f)
        print("==", f, flush=True)
        allok = fetch_one(url, out) and allok
    return 0 if allok else 1


if __name__ == "__main__":
    sys.exit(main())
