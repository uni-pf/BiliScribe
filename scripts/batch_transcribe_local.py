#!/usr/bin/env python3
"""直接转写本地 wav 文件，绕过 yt-dlp 下载步骤。"""
import os, sys, json, time
from pathlib import Path

RUN_DIR = Path("D:/workbuddy/.bili-transcriber/transcripts/20260714/run_1784038434")
MODEL_DIR = Path("D:/workbuddy/.bili-transcriber/models/medium")

# 收集待处理文件
wav_files = sorted(RUN_DIR.glob("*.wav"))
total = len(wav_files)
todo = []
for wf in wav_files:
    txt_file = wf.with_suffix(".txt")
    if not txt_file.exists():
        todo.append(wf)

print(f"总 wav: {total}, 已完成: {total - len(todo)}, 待转录: {len(todo)}")
if not todo:
    print("全部完成！")
    sys.exit(0)

# 加载模型
print(f"加载模型: {MODEL_DIR} ...")
from faster_whisper import WhisperModel
model = WhisperModel(str(MODEL_DIR), device="cuda", compute_type="float16")
print("模型就绪，开始转录...")

# 转录
done = 0
failed = 0
t0 = time.time()
for i, wf in enumerate(todo):
    name = wf.stem
    print(f"[{i+1}/{len(todo)}] {name} ...", end=" ", flush=True)
    try:
        segments, info = model.transcribe(str(wf), language="zh", beam_size=5, vad_filter=True)
        lines = []
        for seg in segments:
            lines.append(seg.text.strip())
        txt = "\n".join(lines)
        txt_file = wf.with_suffix(".txt")
        txt_file.write_text(txt, encoding="utf-8")
        chars = len(txt)
        dur = info.duration
        print(f"✅ {chars}字, {dur:.0f}s")
        done += 1
    except Exception as e:
        print(f"❌ {e}")
        failed += 1

elapsed = time.time() - t0
print(f"\n完成！成功 {done}, 失败 {failed}, 耗时 {elapsed/60:.1f} 分钟")
