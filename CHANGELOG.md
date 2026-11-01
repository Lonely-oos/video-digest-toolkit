# CHANGELOG

## 1.1.0 — 2026-11-01

按外部静态审查（三项目审查报告）修 VD-01..05，并补离线单元测试。

- **VD-01（高）未知远端大小被判成功**：新增 `verify_file()` —— 校验最小体积、拒绝 HTML/JSON 错误页、
  已知大小时要求精确一致；大小未知时在日志里明确标注"仅验证非空且非错误页"。
- **VD-02 / VD-04 子进程失败静默**：`run()` 增加 `check=True`（失败即抛，带 stderr 摘要），
  并为抽帧、场景切分、L2 整条下载、ffprobe 全部接上；新增 `need_file()` 校验产物非空。
- **VD-03 断点续传未验 Content-Range**：206 响应解析 `bytes start-end/total`，
  起点必须等于本地偏移、总长必须与已知远端一致，否则丢弃本次响应重新协商。
- **VD-05 ASR 一次性物化**：改边遍历边写 JSONL（`<audio>.segments.jsonl`，
  可用 `ASR_SEGMENTS_JSONL` / `ASR_MAX_SEGMENTS` 控制），峰值内存不再随段数线性增长。
  ⚠️ 实现上有个坑：先 `next()` 一段逼出 CUDA 设备错误之后，**必须用 `itertools.chain` 把它接回去**，
  否则整条转写只剩第一段（自测时真踩到：13 段变 1 段）。

## 1.0.0 — 2026-10-29

首个公开版本：三档抽帧 + Range 下载 + faster-whisper 转写。