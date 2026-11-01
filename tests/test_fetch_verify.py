#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_fetch_verify.py —— 下载完整性判定的单元测试（不联网）

对应外部审查（2026-11-01）的两条：
  VD-01 未知远端大小时，不完整下载可能被判成功
  VD-03 断点续传没有验证 Content-Range，错位片段会被静默拼进文件

这两个函数是纯函数，所以可以完全离线测。
"""
import os
import sys
import tempfile

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import fetch  # noqa: E402

FAILS = []


def ok(cond, msg):
    print(('  ok   ' if cond else '  FAIL ') + msg)
    if not cond:
        FAILS.append(msg)


def main():
    # ── VD-03：Content-Range 校验 ────────────────────────────────
    ok(fetch.verify_content_range('bytes 100-199/1000', 100, 1000) is None,
       'VD-03 起点与总长一致 → 接受')
    ok(fetch.verify_content_range('bytes 100-199/*', 100, 0) is None,
       'VD-03 总长为 * 且未知名 → 接受')
    r = fetch.verify_content_range('bytes 50-199/1000', 100, 1000)
    ok(isinstance(r, str), 'VD-03 起点不符 → 拒绝（%s）' % r)
    r = fetch.verify_content_range('bytes 100-199/2000', 100, 1000)
    ok(isinstance(r, str), 'VD-03 总长不符 → 拒绝（%s）' % r)
    r = fetch.verify_content_range('bytes 200-100/1000', 200, 1000)
    ok(isinstance(r, str), 'VD-03 end < start → 拒绝（%s）' % r)
    r = fetch.verify_content_range('not-a-range', 0, 0)
    ok(isinstance(r, str), 'VD-03 无法解析 → 拒绝（%s）' % r)

    # ── VD-01：结束后的完整性判定 ───────────────────────────────
    tmp = tempfile.mkdtemp(prefix='vd01-')

    small = os.path.join(tmp, 'small.bin')
    open(small, 'wb').write(b'x' * 100)
    good, why = fetch.verify_file(small, 0)
    ok(not good, 'VD-01 100 字节的文件不算完成（%s）' % why)

    html = os.path.join(tmp, 'error.html')
    open(html, 'wb').write(b'<!DOCTYPE html><html><body>404</body></html>' + b' ' * 5000)
    good, why = fetch.verify_file(html, 0)
    ok(not good, 'VD-01 HTML 错误页不算完成（%s）' % why)

    binfile = os.path.join(tmp, 'model.bin')
    open(binfile, 'wb').write(os.urandom(8192))
    good, why = fetch.verify_file(binfile, 0)
    ok(good, 'VD-01 未知大小但内容像二进制 → 接受并标注（%s）' % why)
    ok('未知' in why or '无法' in why or '仅' in why, 'VD-01 判定理由里说明"大小未知"（%s）' % why)

    good, why = fetch.verify_file(binfile, 999999)
    ok(not good, 'VD-01 已知远端大小但本地不足 → 拒绝（%s）' % why)

    good, why = fetch.verify_file(binfile, 8192)
    ok(good, 'VD-01 大小完全一致 → 接受（%s）' % why)

    missing = os.path.join(tmp, 'nope.bin')
    good, why = fetch.verify_file(missing, 0)
    ok(not good, 'VD-01 文件不存在 → 拒绝（%s）' % why)

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print('\n' + ('===== %d FAILED =====' % len(FAILS) if FAILS else '===== all fetch checks passed ====='))
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
