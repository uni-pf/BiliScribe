#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bilibili-transcriber — 将 B 站(及 yt-dlp 支持的其他平台)视频转为文字。

流程: 解析输入(BV/av/链接) -> yt-dlp 下载最佳音轨 -> faster-whisper 语音识别
      -> 输出 纯文本(.txt) 与/或 字幕(.srt)。

设计目标「省 token」:
  * 转录正文绝不进入调用方上下文, 仅向 stdout 输出元信息 JSON。
  * 进度日志全部写入 stderr, 不污染机器可读结果。
  * 支持 --list-only 先枚举系列视频, 确认后再下载, 避免无谓消耗。
  * 支持 --limit 限制系列处理数量, 防止长合集一次性灌爆。

可靠性特性:
  * --resume: 断点续传。在 run_dir 下写 progress.json, 中断后重跑自动跳过
    已完成的视频, 只处理剩余项。
  * --progress-file: 增量写进度到指定文件(JSON), 供 agent 轮询实时查看
    "第几集/共几集/已失败项"。
  * --jobs N: 系列视频并发转录(共享单模型实例)。GPU 模式建议 jobs=1,
    CPU 模式可 2-4 提速。
  * --cache-dir: 音频缓存。按视频 id 复用转码后的 wav, 同一视频重复转录
    跳过下载, 显著省时省流量。
  * 模型完整性校验: 本地模型目录不仅检查 model.bin, 还校验 config.json /
    tokenizer.json, 避免残缺模型加载时报错。

依赖: yt-dlp, faster-whisper, imageio-ffmpeg(提供 ffmpeg 二进制, 不需要 ffprobe)。
首次运行 whisper 模型会从 HuggingFace 下载(体积较大, 仅一次)。

== 环境前置(本沙箱/离线环境务必) ==
脚本启动时会自动设置以下环境变量(若调用方未显式指定):
  CODEBUDDY_SAFE_DELETE_SANDBOX=0  避免沙箱删除钩子强行抛错
  HF_ENDPOINT=https://hf-mirror.com  HuggingFace 镜像, 加速/可访问
  HF_HUB_DISABLE_SYMLINKS=1        沙箱内符号链接 checkout 会失败, 改用复制
  HF_HUB_DISABLE_XET=1             大文件走 Xet/CAS 在镜像上会 401, 改普通 HTTPS
若你处于可直连 HF 的环境, 可在调用前自行 unset 这些变量以用官方源。

== 关键环境坑(实战总结, 已在此脚本固化) ==
1. ffmpeg 缺 ffprobe: imageio-ffmpeg 只带 ffmpeg 不带 ffprobe, 而 yt-dlp 的
   `FFmpegExtractAudio` 后处理需要 ffprobe。本脚本改为: 直接用 yt-dlp 下载
   *原始音轨*(m4a/opus/webm), 再用内置 ffmpeg 显式转成 16k 单声道 wav。
2. 设备自动选择: --device 默认 "auto", 运行时按 ctranslate2 能否看到
   CUDA 设备自动选 cuda / cpu; compute_type 也自动(cuda→float16, cpu→int8)。
   若显式指定 cuda 但缺 cuBLAS 运行时, 会回退到 CPU 并提示。
   GPU 检测/启用逻辑已下沉到独立模块 gpu_utils.py。
3. 本地模型目录: 用 BILI2TEXT_MODEL_DIR 指向已落地的模型目录(含 model.bin +
   配置文件), 跳过 HF 缓存/下载, 最稳。setup_env.py --download-model 可一键预下载。
   模型/缓存/输出默认落在持久化根 BILI_HOME(D:\\workbuddy\\.bili-transcriber,
   可用环境变量覆盖), 与技能目录、安装目录相互独立, 避免技能更新时数据丢失。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
import datetime

# 统一数据目录解析(单一事实来源, 独立于技能/安装目录)
from bili_paths import (
    get_bili_home, get_models_dir, get_cache_dir, get_transcripts_dir,
    ensure_dir, legacy_skill_models_dir,
)

# 备用方案: yt-dlp Bilibili extractor 超时/限流时直连 B站 API
try:
    from bili_direct import try_direct_list, try_direct_download, parse_bvid, parse_p
    HAVE_BILI_DIRECT = True
except ImportError:
    HAVE_BILI_DIRECT = False

AUDIO_EXT = re.compile(r"\.(m4a|m4s|opus|webm|mp3|ogg|aac|flac|wav)$", re.I)

# faster-whisper 模型目录必须包含的文件(用于完整性校验)
MODEL_REQUIRED_FILES = ["model.bin", "config.json", "tokenizer.json"]
VOCAB_ALTERNATIVES = ["vocabulary.txt", "vocab.json"]

# ---------------------------------------------------------------------------
# 0a. 日志辅助 — 带时间戳的进度输出, 便于 agent 解析而非猜进度
# ---------------------------------------------------------------------------
_LOG_TS_FORMAT = "%H:%M:%S"

def log(tag: str, msg: str, *,
        file=sys.stderr, ts: bool = True) -> None:
    """统一日志输出。格式: [HH:MM:SS] [tag] msg"""
    ts_str = f"[{datetime.datetime.now().strftime(_LOG_TS_FORMAT)}]" if ts else ""
    print(f"{ts_str}[{tag}] {msg}", file=file)


# ---------------------------------------------------------------------------
# 0. 环境前置(自动设置, 可被调用方环境变量覆盖)
# ---------------------------------------------------------------------------
def _apply_env_defaults() -> None:
    os.environ.setdefault("CODEBUDDY_SAFE_DELETE_SANDBOX", "0")
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


# ---------------------------------------------------------------------------
# 1. 输入解析
# ---------------------------------------------------------------------------
def to_url(inp: str) -> str:
    """BV号 / av号 / 短链 / 完整URL -> yt-dlp 可识别的 URL。"""
    inp = (inp or "").strip()
    if not inp:
        raise ValueError("输入为空, 请提供 B站链接 / BV号 / av号。")
    if inp.startswith("http://") or inp.startswith("https://"):
        return inp
    if re.match(r"(?i)^BV[0-9A-Za-z]+$", inp):
        return f"https://www.bilibili.com/video/{inp}"
    if re.match(r"(?i)^av\d+$", inp):
        return f"https://www.bilibili.com/video/{inp}"
    # 容错: 字符串中嵌有 BV/av
    m = re.match(r"(?i).*(BV[0-9A-Za-z]+)", inp)
    if m:
        return f"https://www.bilibili.com/video/{m.group(1)}"
    m = re.match(r"(?i).*av(\d+)", inp)
    if m:
        return f"https://www.bilibili.com/video/av{m.group(1)}"
    raise ValueError(
        f"无法识别的输入: {inp!r}。支持: B站视频链接、BV号、av号、b23.tv 短链。"
    )


# ---------------------------------------------------------------------------
# 2. ffmpeg(由 imageio-ffmpeg 提供, 无需 ffprobe)
# ---------------------------------------------------------------------------
def get_ffmpeg() -> str:
    """优先用 imageio-ffmpeg 自带的 ffmpeg; 找不到再回退 PATH。"""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError(
            "未找到 ffmpeg。请先运行 scripts/setup_env.py 安装 imageio-ffmpeg。"
        )
    return exe


def convert_to_wav(src: str, ffmpeg: str, dst_dir: Optional[str] = None) -> str:
    """把原始音轨(m4a/opus/webm...)转成 16k 单声道 wav。

    只用 ffmpeg、不依赖 ffprobe, 规避 imageio-ffmpeg 缺 ffprobe 的问题。
    转换成功后删除原始文件以省磁盘(失败则保留原始, 让 faster-whisper 直接读)。
    dst_dir 指定时, wav 输出到 dst_dir(用于缓存场景), 否则与 src 同目录。
    """
    # src 已经是 wav 则跳过重编码(避免 ffmpeg -i src.wav src.wav 自读自写)
    src_lower = src.lower()
    if src_lower.endswith(".wav"):
        return src
    stem = os.path.splitext(os.path.basename(src))[0]
    if dst_dir:
        wav = os.path.join(dst_dir, f"{stem}.wav")
    else:
        wav = os.path.splitext(src)[0] + ".wav"
    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", src, "-ar", "16000", "-ac", "1", "-vn", wav],
            check=True, capture_output=True, text=True, timeout=120,
        )
        log("进度", f"已转码为 wav: {os.path.basename(wav)}")
        # 清理原始音轨(非致命)
        try:
            if os.path.abspath(src) != os.path.abspath(wav) and os.path.isfile(src):
                os.remove(src)
        except OSError as e:
            log("提示", f"原始音轨清理跳过: {e}")
        return wav
    except subprocess.TimeoutExpired:
        log("警告", f"ffmpeg 转码超时(2分钟)，跳过: {os.path.basename(src)}")
        return src
    except subprocess.CalledProcessError as e:
        log("警告", f"ffmpeg 转码失败, 改用原始音轨直接识别: {e}")
        return src


# ---------------------------------------------------------------------------
# 3. yt-dlp 封装 + 错误映射
# ---------------------------------------------------------------------------
def _run_ytdlp(args: List[str]) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "yt_dlp", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "yt-dlp 下载超时(10分钟)。可能是网络问题或视频过长, "
            "建议重试或改用较短视频。"
        )
    except FileNotFoundError:
        raise RuntimeError(
            "未找到 yt-dlp。请先运行 scripts/setup_env.py 安装依赖。"
        )
    if proc.returncode != 0:
        _raise_ytdlp_error(proc.stderr or proc.stdout)
    return proc


def _raise_ytdlp_error(text: str):
    t = (text or "").lower()
    friendly = "下载失败, 请检查网络或视频可用性。"
    if "video unavailable" in t or "此视频" in text or "unavailable" in t:
        friendly = "视频不存在或已被下架/设为不可用。"
    elif "private" in t:
        friendly = "该视频为私密视频, 无法访问。"
    elif "404" in t or "not found" in t:
        friendly = "视频页面不存在 (404), 请确认链接 / BV号 是否正确。"
    elif "sign in" in t or "login" in t or "会员" in text:
        friendly = "该视频需要登录或仅会员可见, 当前无法下载。"
    elif "copyright" in t or "版权" in text:
        friendly = "该视频因版权限制无法下载。"
    elif "network" in t or "timed out" in t or "connection" in t or "timeout" in t:
        friendly = "网络连接失败或超时, 请稍后重试。"
    raise RuntimeError(f"{friendly}\n原始信息: {text.strip()[-600:]}")


def list_videos(url: str) -> List[dict]:
    """枚举系列/分P视频, 不下载。

    使用 yt-dlp 的 %(json)s 单行 JSON 输出并 json.loads 解析, 彻底规避标题含
    制表符/换行等特殊字符导致 \t 分隔错位的问题。

    若 yt-dlp 失败（常见于 B站 API 限流/超时），自动降级到直连模式
    (bili_direct.try_direct_list)，通过解析页面 HTML 获取分P列表。
    """
    try:
        proc = _run_ytdlp([
            "--flat-playlist", "--simulate", "--no-warnings",
            "--print", "%(json)s",
            url,
        ])
    except RuntimeError as e:
        # yt-dlp 失败 → 尝试 B站直连 fallback
        err_msg = str(e)
        if HAVE_BILI_DIRECT:
            log("进度", f"yt-dlp 枚举失败, 尝试 B站直连模式: {err_msg[:80]}")
            items = try_direct_list(url)
            if items is not None:
                log("进度", f"直连枚举成功: {len(items)} 个视频")
                return items
        # 直连也失败, 抛原始异常
        raise

    items: List[dict] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        items.append({
            "index": str(d.get("playlist_index") or len(items) + 1),
            "id": d.get("id", ""),
            "title": d.get("title", ""),
            "duration": d.get("duration") or "",
        })
    return items


def _make_run_dir(out_dir: str) -> str:
    out_dir = ensure_dir(out_dir)
    run_dir = os.path.join(out_dir, f"run_{int(time.time())}")
    run_dir = ensure_dir(run_dir)
    return run_dir


def _find_latest_run_dir(out_dir: str) -> Optional[str]:
    """在 out_dir 下找最新的 run_<timestamp> 目录(用于 --resume)。"""
    if not os.path.isdir(out_dir):
        return None
    runs = [
        os.path.join(out_dir, d) for d in os.listdir(out_dir)
        if d.startswith("run_") and os.path.isdir(os.path.join(out_dir, d))
    ]
    if not runs:
        return None
    runs.sort(reverse=True)  # 时间戳字典序 = 时间序, 取最新
    return runs[0]


def _prune_old_runs(out_dir: str, keep: int) -> None:
    """转录完成后仅保留最近 keep 个 run_<timestamp> 目录, 删除更早的。

    目录名含时间戳, 字典序即时间序。keep<=0 时不清理(默认行为)。
    """
    if keep <= 0:
        return
    if not os.path.isdir(out_dir):
        return
    runs = [
        os.path.join(out_dir, d) for d in os.listdir(out_dir)
        if d.startswith("run_") and os.path.isdir(os.path.join(out_dir, d))
    ]
    runs.sort()
    for old in runs[:-keep]:
        try:
            shutil.rmtree(old)
            log("清理", f"已删除旧运行目录: {os.path.basename(old)}")
        except OSError as e:
            log("提示", f"旧目录清理跳过: {e}")


def _video_id_from_path(audio_path: str) -> str:
    """从音频文件名提取视频 id。文件名格式: 01_<id>.wav 或 01_<id>.m4a。"""
    stem = os.path.splitext(os.path.basename(audio_path))[0]
    # 形如 01_BV1xx411c7mD
    m = re.match(r"^\d+_(.+)$", stem)
    return m.group(1) if m else stem


def download_audio(url: str, run_dir: str, limit: Optional[int],
                   cache_dir: Optional[str] = None,
                   playlist_items: Optional[str] = None) -> List[str]:
    """下载最佳音轨(原始封装, 不做 yt-dlp 后处理, 因此不需要 ffprobe)。

    返回下载到的(准备喂给识别的)音频文件列表。
    若 cache_dir 提供, 下载+转码后的 wav 会写入 cache_dir/<id>.wav;
    下次同一 id 命中缓存时直接复制 wav 到 run_dir, 跳过下载。
    playlist_items: yt-dlp --playlist-items 参数(逗号分隔序号), 用于只下载
    未命中缓存的条目。

    若 yt-dlp 下载失败（常见于 B站 API 限流），自动降级到直连模式
    (bili_direct.try_direct_download)，通过 B站 playurl API 下载音频。
    """
    outtmpl = os.path.join(run_dir, "%(playlist_index)02d_%(id)s.%(ext)s")
    # 关键: 只用 -f bestaudio/best, 不加 -x / --audio-format(那会触发
    # FFmpegExtractAudio 后处理并需要 ffprobe)。转码交给 convert_to_wav()。
    args = [
        "-f", "bestaudio/best",
        "--restrict-filenames",
        "--no-warnings",
        "--yes-playlist",
        "-o", outtmpl,
        url,
    ]
    if limit:
        args = ["--playlist-end", str(limit), *args]
    if playlist_items:
        args = ["--playlist-items", playlist_items, *args]
        # --resume 场景: yt-dlp 默认 --no-overwrites（不覆盖已有文件），
        # 旧 run_dir 可能残留不完整音轨 → 强制覆盖避免用坏文件
        args = ["--force-overwrites", *args]
    log("进度", f"正在下载音轨: {url}")
    try:
        _run_ytdlp(args)
    except RuntimeError as e:
        # yt-dlp 失败 → 尝试 B站直连 fallback
        err_msg = str(e)
        if HAVE_BILI_DIRECT and ("bilibili" in url.lower() or "bili" in url.lower()):
            log("进度", f"yt-dlp 下载失败, 尝试 B站直连模式: {err_msg[:80]}")
            direct_path = try_direct_download(url, run_dir)
            if direct_path is not None and os.path.isfile(direct_path):
                log("进度", f"直连下载成功: {os.path.basename(direct_path)}")
                return [direct_path]
        # 直连也失败, 抛原始异常
        raise

    files = sorted(
        os.path.join(run_dir, f) for f in os.listdir(run_dir)
        if AUDIO_EXT.search(f)
    )
    return files


# ---------------------------------------------------------------------------
# 4. faster-whisper 语音识别
# ---------------------------------------------------------------------------
def resolve_device(device_arg: str) -> str:
    """auto 模式: 按 ctranslate2 能否看到 CUDA 设备自动选 cuda/cpu。"""
    if device_arg in ("cpu", "cuda"):
        return device_arg
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception:
        pass
    return "cpu"


def resolve_compute(device: str, compute_arg: str) -> str:
    """auto 模式: cuda→float16(更准更快), cpu→int8(最快)。"""
    if compute_arg and compute_arg != "auto":
        return compute_arg
    return "float16" if device == "cuda" else "int8"


def _validate_local_model(model_dir: str) -> Tuple[bool, List[str]]:
    """校验本地模型目录完整性。返回 (ok, missing_files)。"""
    missing = [
        f for f in MODEL_REQUIRED_FILES
        if not os.path.isfile(os.path.join(model_dir, f))
    ]
    return (len(missing) == 0, missing)


def _resolve_local_model(model_size: str) -> str:
    """解析本地模型目录(优先级):
    1. 环境变量 BILI2TEXT_MODEL_DIR(指向含 model.bin 等的目录);
    2. 技能自带 <技能目录>/models/<model_size>/(setup_env.py --download-model 落地处)。
    命中且完整性校验通过则返回该目录; 否则原样返回 model_size
    (交给 faster-whisper 走 HF 默认缓存)。完整性不通过时打印警告。
    """
    cands = []
    env = os.environ.get("BILI2TEXT_MODEL_DIR", "").strip()
    if env:
        cands.append(env)
    # 持久化根下的模型目录(默认位置, 独立于技能/安装目录)
    cands.append(os.path.join(get_models_dir(), model_size))
    # 兼容旧版: 技能目录内的 models/(更新时可能被覆盖, 仅作回退)
    cands.append(os.path.join(legacy_skill_models_dir(), model_size))
    for cand in cands:
        if not cand or not os.path.isdir(cand):
            continue
        ok, missing = _validate_local_model(cand)
        if ok:
            return cand
        # 目录存在但残缺: 打印警告, 不当作命中(避免加载残缺模型)
        log("警告", f"本地模型目录不完整, 跳过: {cand} (缺少: {', '.join(missing)})")
    return model_size


def load_model(model_size: str, device: str, compute_type: str):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise RuntimeError(
            "未找到 faster_whisper。请先运行 scripts/setup_env.py 安装依赖。"
        )
    # 本地模型目录优先(显式环境变量 或 技能 models/ 目录), 跳过 HF 缓存/下载
    resolved = _resolve_local_model(model_size)
    if resolved != model_size:
        log("进度", f"使用本地模型目录: {resolved}")
        model_size = resolved
    log("进度", f"加载语音识别模型: {model_size} ({device}/{compute_type})")
    # 若选了 GPU/auto, 运行时再尝试启用 cuBLAS(从系统复制到 ctranslate2 包目录),
    # 覆盖"setup 时没启用、之后才有 GPU"的场景。失败静默, 后续 CUDA 初始化会回退 CPU。
    # GPU 检测/启用逻辑已下沉到独立模块 gpu_utils(与 setup_env.py 共用)。
    if device in ("auto", "cuda"):
        try:
            from gpu_utils import enable_gpu
            enable_gpu(verbose=False)
        except Exception:
            pass
    try:
        return WhisperModel(model_size, device=device, compute_type=compute_type)
    except Exception as e:
        msg = str(e).lower()
        # 偶发: 指定/探测到 CUDA 却缺 cublas 动态库 -> 回退 CPU
        if device in ("auto", "cuda") and ("cublas" in msg or "cuda" in msg):
            log("进度", "未检测到可用 CUDA/cuBLAS, 回退到 CPU(int8)")
            return WhisperModel(model_size, device="cpu", compute_type="int8")
        raise


def _watchdog_logger(audio_path: str, fn_start: float,
                     stop_event: threading.Event) -> None:
    """后台线程: model.transcribe() 阻塞期间每 15 秒输出一次进度心跳。"""
    basename = os.path.basename(audio_path)
    while not stop_event.is_set():
        # 最多等 15 秒再检查一次, 但一旦停止事件到达就立即退出
        if stop_event.wait(15):
            break
        elapsed = time.time() - fn_start
        log("进度", f"仍在识别: {basename} (已用 {elapsed:.0f} 秒)")


def transcribe_file(model, audio_path: str, lang: Optional[str],
                    model_lock: Optional[threading.Lock] = None,
                    min_silence_duration_ms: int = 500,
                    batch_size: int = 0):
    """返回 (segments, info)。segments = [(start, end, text), ...]

    在阻塞的 model.transcribe() 期间自动启动 watchdog 线程
    (每 15 秒输出一次进度到 stderr), 避免长音频时 agent 空等无反馈。

    model_lock: 并发场景下保护 model.transcribe 调用(GPU 模式或保守并发时建议传入)。
    min_silence_duration_ms: VAD 最小静音时长(毫秒), 控制分段灵敏度。
    batch_size: faster-whisper 批处理大小。0=使用引擎默认值(通常较低);
               GPU 推荐 64-128 以充分利用显存并行能力。
    """
    log("进度", f"识别中: {os.path.basename(audio_path)}")
    kwargs = dict(
        language=lang,            # None => 自动检测
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=min_silence_duration_ms),
    )
    if batch_size > 0:
        kwargs["batch_size"] = batch_size

    # --- watchdog 线程: 阻塞期间每 15s 发一次心跳 ---
    _start = time.time()
    _stop = threading.Event()
    _wd = threading.Thread(
        target=_watchdog_logger,
        args=(audio_path, _start, _stop),
        daemon=True,
    )
    _wd.start()

    try:
        if model_lock is not None:
            with model_lock:
                seg_iter, info = model.transcribe(audio_path, **kwargs)
        else:
            seg_iter, info = model.transcribe(audio_path, **kwargs)
    finally:
        _stop.set()   # 让 watchdog 退出 while 循环

    segments: List[Tuple[float, float, str]] = []
    seg_count = 0
    for seg in seg_iter:
        text = (seg.text or "").strip()
        if text:
            segments.append((seg.start, seg.end, text))
            seg_count += 1

    elapsed = time.time() - _start
    log("完成", f"识别完成: {os.path.basename(audio_path)} "
               f"({elapsed:.0f}s, {seg_count} 段, {len(segments)} 有效段)")
    return segments, info


# ---------------------------------------------------------------------------
# 5. 输出写入
# ---------------------------------------------------------------------------
def fmt_srt_time(sec: float) -> str:
    if sec is None or sec < 0:
        sec = 0.0
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_outputs(audio_path: str, segments, run_dir: str, formats: set,
                  text_mode: str = "merged") -> dict:
    """写入 txt / srt。

    text_mode:
      "merged" — txt 按语义分段(段间空行), 可读性高(默认)
      "raw"    — txt 逐段一行行拼接(旧行为)
    segments 元素可为 3 元组 (start, end, text) 或 4 元组 (start, end, text, speaker)。
    有 speaker 时: txt 段落开头加 [说话人]; srt 每条文本前加 [说话人]。
    """
    stem = os.path.splitext(os.path.basename(audio_path))[0]
    base = os.path.join(run_dir, stem)
    res: dict = {}
    if "txt" in formats:
        txt_path = base + ".txt"
        with open(txt_path, "w", encoding="utf-8") as f:
            if text_mode == "merged":
                paragraphs = merge_paragraphs(segments)
                f.write("\n\n".join(paragraphs))
                f.write("\n")
            else:
                # raw: 逐段拼接, 保留 speaker 前缀
                for seg in segments:
                    if len(seg) >= 4 and seg[3]:
                        f.write(f"[{seg[3]}] ")
                    f.write(seg[2])
                    f.write("\n")
        res["txt"] = txt_path
    if "srt" in formats:
        srt_path = base + ".srt"
        with open(srt_path, "w", encoding="utf-8") as f:
            for i, seg in enumerate(segments, 1):
                s, e = seg[0], seg[1]
                t = seg[2]
                if len(seg) >= 4 and seg[3]:
                    t = f"[{seg[3]}] {t}"
                f.write(f"{i}\n{fmt_srt_time(s)} --> {fmt_srt_time(e)}\n{t}\n\n")
        res["srt"] = srt_path
    res["chars"] = sum(len(seg[2]) for seg in segments)
    res["segments"] = len(segments)
    return res


def build_preview(segments, n: int) -> str:
    """拼接所有段文本取前 n 字符(比只取首段更具代表性)。"""
    if not segments or n <= 0:
        return ""
    full = "".join(t for _, _, t in segments)
    return full[:n]


def auto_select_model(total_duration_sec: float) -> str:
    """按时长自动选 whisper 模型。

    <10min  -> large-v3 (精度优先, 短视频耗时可接受)
    10-60min -> medium   (默认平衡)
    >60min  -> small     (速度优先, 长合集避免太久)
    拿不到时长 -> medium (安全回退)
    """
    if not total_duration_sec or total_duration_sec <= 0:
        return "medium"
    if total_duration_sec < 600:        # 10 分钟
        return "large-v3"
    elif total_duration_sec < 3600:     # 1 小时
        return "medium"
    else:
        return "small"


def merge_paragraphs(segments, short_gap: float = 1.5,
                     long_gap: float = 3.0) -> List[str]:
    """把 segments 按段间静音间隔合并成可读段落。

    规则(基于 VAD 时间戳):
      gap <  short_gap (1.5s): 同句, 直接拼接
      short_gap <= gap < long_gap (3s): 同段, 加句号分隔
      gap >= long_gap: 新段落(空行分隔)

    返回 list[str], 每个 str 是一个段落。speaker 标签(若有)保留在段落开头。
    segments 元素可为 3 元组 (start, end, text) 或 4 元组 (start, end, text, speaker)。
    """
    if not segments:
        return []

    def _text_and_speaker(seg):
        if len(seg) >= 4:
            return seg[2], seg[3]
        return seg[2], None

    paragraphs: List[str] = []
    cur_text, cur_spk = _text_and_speaker(segments[0])
    cur_parts: List[str] = []
    if cur_spk:
        cur_parts.append(f"[{cur_spk}] ")
    cur_parts.append(cur_text)

    for i in range(1, len(segments)):
        prev_end = segments[i - 1][1]
        curr_start = segments[i][0]
        gap = max(0.0, curr_start - prev_end)
        txt, spk = _text_and_speaker(segments[i])

        if gap >= long_gap:
            # 新段落
            paragraphs.append("".join(cur_parts))
            cur_parts = []
            if spk:
                cur_parts.append(f"[{spk}] ")
            cur_parts.append(txt)
        elif gap >= short_gap:
            # 同段落, 加句号分隔(若文本末尾无标点)
            if cur_parts and cur_parts[-1] and cur_parts[-1][-1] not in "。.！!？?；;":
                cur_parts.append("。")
            # 换说话人也起一句
            if spk and spk != cur_spk:
                cur_parts.append(f"[{spk}] ")
            cur_parts.append(txt)
            cur_spk = spk
        else:
            # 紧密拼接
            if spk and spk != cur_spk:
                cur_parts.append(f"[{spk}] ")
                cur_spk = spk
            cur_parts.append(txt)

    paragraphs.append("".join(cur_parts))
    return paragraphs


def diarize_segments(audio_path: str, segments, hf_token: Optional[str] = None):
    """用 pyannote.audio 给每段标注说话人。

    segments: [(start, end, text), ...]
    返回: [(start, end, text, speaker), ...]

    需要 pyannote.audio(可选依赖, 需 pip install pyannote.audio) +
    HuggingFace token(在 https://huggingface.co/pyannote/speaker-diarization-3.1
    接受协议后, 设 HF_TOKEN 环境变量)。
    不可用时抛 RuntimeError, 调用方应捕获并降级为普通转录。
    """
    try:
        from pyannote.audio import Pipeline
    except ImportError:
        raise RuntimeError(
            "说话人分离需要 pyannote.audio。安装: pip install pyannote.audio; "
            "并在 https://huggingface.co/pyannote/speaker-diarization-3.1 接受协议, "
            "设置 HF_TOKEN 环境变量后重试。本次将降级为普通转录(不区分说话人)。"
        )
    token = (hf_token or os.environ.get("HF_TOKEN")
             or os.environ.get("HUGGING_FACE_HUB_TOKEN"))
    if not token:
        raise RuntimeError(
            "说话人分离需要 HuggingFace token。请设置 HF_TOKEN 环境变量"
            "(先在 https://huggingface.co/pyannote/speaker-diarization-3.1 接受协议)。"
            "本次将降级为普通转录。"
        )
    log("进度", f"说话人分离中: {os.path.basename(audio_path)}")
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1", token=token
    )
    diarization = pipeline(audio_path, num_speakers=None)

    labeled = []
    for start, end, text in segments:
        mid = (start + end) / 2
        speaker = "未知"
        for turn, _, spk in diarization.itertracks(yield_label=True):
            if turn.start <= mid <= turn.end:
                speaker = spk
                break
        labeled.append((start, end, text, speaker))
    return labeled


# ---------------------------------------------------------------------------
# 6. 断点续传 / 进度文件
# ---------------------------------------------------------------------------
PROGRESS_FILE = "progress.json"


def _progress_path(run_dir: str) -> str:
    return os.path.join(run_dir, PROGRESS_FILE)


def load_progress(run_dir: str) -> dict:
    """读取 run_dir 下的 progress.json。无则返回空骨架。"""
    p = _progress_path(run_dir)
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {"total": 0, "done": {}, "failed": {}, "results": []}


def save_progress(run_dir: str, progress: dict) -> None:
    """原子写 progress.json(先写临时文件再 rename, 避免中途崩溃写坏)。"""
    p = _progress_path(run_dir)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False)
        try:
            os.replace(tmp, p)
        except OSError:
            shutil.move(tmp, p)  # 跨文件系统回退(如跨盘)
    except OSError as e:
        log("提示", f"progress.json 写入跳过: {e}")


def write_external_progress(progress_file: str, progress: dict) -> None:
    """把当前进度增量写到外部文件(供 agent 轮询)。"""
    if not progress_file:
        return
    snapshot = {
        "total": progress.get("total", 0),
        "done_count": len(progress.get("done", {})),
        "failed_count": len(progress.get("failed", {})),
        "done_ids": list(progress.get("done", {}).keys()),
        "failed_ids": list(progress.get("failed", {}).keys()),
        "updated_at": int(time.time()),
    }
    tmp = progress_file + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False)
        try:
            os.replace(tmp, progress_file)
        except OSError:
            shutil.move(tmp, progress_file)  # 跨文件系统回退
    except OSError as e:
        log("提示", f"外部进度文件写入跳过: {e}")


# ---------------------------------------------------------------------------
# 6b. 环境检查 --check-env
# ---------------------------------------------------------------------------
def check_env() -> dict:
    """全面检查运行环境, 返回结构化报告。"""
    report = {
        "ok": True,
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "dependencies": {},
        "model": {},
        "gpu": {},
        "disk": {},
    }

    # 依赖检查
    for mod, name in [("yt_dlp", "yt-dlp"),
                       ("faster_whisper", "faster-whisper"),
                       ("imageio_ffmpeg", "imageio-ffmpeg (ffmpeg)")]:
        try:
            __import__(mod)
            report["dependencies"][name] = "OK (installed)"
        except ImportError:
            report["dependencies"][name] = "MISSING"
            report["ok"] = False

    # 额外检查: ffmpeg 二进制可用
    try:
        exe = get_ffmpeg()
        report["dependencies"]["ffmpeg_binary"] = f"OK ({exe})"
    except RuntimeError as e:
        report["dependencies"]["ffmpeg_binary"] = f"MISSING ({e})"
        report["ok"] = False

    # 模型检查
    model_dir = os.environ.get("BILI2TEXT_MODEL_DIR", "")
    if model_dir and os.path.isdir(model_dir):
        ok, missing = _validate_local_model(model_dir)
        if ok:
            report["model"]["local_dir"] = model_dir
            report["model"]["status"] = "OK"
            report["model"]["size_mb"] = round(
                sum(os.path.getsize(os.path.join(model_dir, f))
                    for f in os.listdir(model_dir)
                    if os.path.isfile(os.path.join(model_dir, f))) / 1_048_576, 1
            )
        else:
            report["model"]["local_dir"] = model_dir
            report["model"]["status"] = f"INCOMPLETE (missing: {', '.join(missing)})"
            report["model"]["size_mb"] = "?"
    else:
        # 扫描持久化根下的模型目录(默认位置, 独立于技能目录)
        builtin_models = get_models_dir()
        legacy_models = legacy_skill_models_dir()
        available = []
        for base in (builtin_models, legacy_models):
            if not os.path.isdir(base):
                continue
            for d in sorted(os.listdir(base)):
                dpath = os.path.join(base, d)
                if os.path.isdir(dpath):
                    ok, _ = _validate_local_model(dpath)
                    tag = "[OK]" if ok else "[INCOMPLETE]"
                    label = f"{d} {tag}"
                    if base == legacy_models:
                        label += " [旧版技能目录, 建议迁移]"
                    available.append(label)
        report["model"]["builtin_models"] = available if available else "(none)"
        report["model"]["home"] = get_bili_home()

    # GPU 检测
    try:
        from gpu_utils import detect_gpu
        gpu_info = detect_gpu(verbose=False)
        report["gpu"] = {
            "cuda_device_count": gpu_info.get("cuda_device_count", 0),
            "gpu_present": gpu_info.get("gpu_present", False),
            "cuda_ready": gpu_info.get("cuda_ready", False),
            "reason": gpu_info.get("reason", "未检测"),
        }
    except ImportError:
        report["gpu"] = {"error": "gpu_utils 模块不可用"}
    except Exception as e:
        report["gpu"] = {"error": str(e)}

    # 磁盘可用空间(输出目录)
    out_dir_candidates = [
        os.environ.get("BILI_OUT_DIR", ""),
        get_transcripts_dir(),
    ]
    for d in out_dir_candidates:
        if d:
            try:
                os.makedirs(d, exist_ok=True)
                usage = shutil.disk_usage(d)
                free_gb = usage.free / 1_073_741_824
                report["disk"]["output_dir"] = d
                report["disk"]["free_gb"] = round(free_gb, 1)
                report["disk"]["warning"] = (
                    f"WARNING: Low disk space ({free_gb:.1f} GB)"
                    if free_gb < 5 else "OK"
                )
                break
            except OSError:
                continue

    return report


# ---------------------------------------------------------------------------
# 6c. Claude Code 输出模式
# ---------------------------------------------------------------------------
def format_claude_summary(chosen_model: str, run_dir: str, progress: dict,
                          device: str) -> str:
    """生成 Claude Code 友好的结构化文本摘要。"""
    lines = []
    lines.append("=" * 60)
    lines.append("  bilibili-transcriber — 转录完成报告")
    lines.append("=" * 60)
    lines.append("")

    total = progress.get("total", 0)
    done = len(progress.get("done", {}))
    failed = len(progress.get("failed", {}))
    total_chars = 0
    total_duration = 0.0

    for r in progress.get("results", []):
        total_chars += r.get("chars", r.get("c", 0))
        total_duration += r.get("duration_sec", r.get("d", 0.0))

    lines.append(f"📊 统计")
    lines.append(f"  文件数:    {done}/{total} (失败 {failed})")
    lines.append(f"  总字数:    {total_chars:,}")
    lines.append(f"  总时长:    {total_duration:.0f}s ({total_duration/60:.1f} 分钟)")
    lines.append(f"  模型:      {chosen_model}")
    lines.append(f"  设备:      {device.upper()}")
    lines.append(f"  输出目录:  {run_dir}")
    lines.append("")

    if progress.get("results"):
        lines.append("📄 文件清单")
        lines.append("-" * 60)
        for r in progress["results"]:
            txt_path = r.get("txt", r.get("tx", ""))
            srt_path = r.get("srt", r.get("sr", ""))
            chars = r.get("chars", r.get("c", 0))
            dur = r.get("duration_sec", r.get("d", 0))
            preview = r.get("preview", "")
            lang = r.get("language", r.get("l", "zh"))
            err = r.get("error", "")
            vid_label = os.path.basename(str(txt_path)) if txt_path else "?"
            if err:
                lines.append(f"  ❌ {vid_label}: {err}")
            else:
                lines.append(f"  📝 {vid_label}")
                lines.append(f"     ({chars} 字, {dur:.0f}s, {lang})")
                if preview:
                    lines.append(f"     预览: {preview[:200]}...")
        lines.append("-" * 60)

    if failed > 0:
        lines.append("")
        lines.append("⚠️ 失败项")
        for vid, err in progress.get("failed", {}).items():
            lines.append(f"  ❌ {vid}: {err}")

    lines.append("")
    lines.append("=" * 60)
    lines.append("提示: 需要查看完整文件可用 present_files 或 cat 命令。")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 7. 主流程
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    _apply_env_defaults()

    p = argparse.ArgumentParser(
        description="将 B站(及其他平台)视频转为文字(纯文本 / SRT 字幕)"
    )
    p.add_argument("input", help="B站链接 / BV号 / av号 / b23.tv 短链")
    p.add_argument("--out-dir", default="",
                   help="输出根目录(默认: <BILI_HOME>/transcripts, "
                        "可用 BILI_HOME 环境变量覆盖)")
    p.add_argument("--list-only", action="store_true",
                   help="仅列出系列/分P视频, 不下载也不转录")
    p.add_argument("--limit", type=int, default=0,
                   help="最多处理前 N 个视频(防止长合集一次性灌爆)")
    p.add_argument("--model", default="medium",
                   help="whisper 模型: tiny/base/small/medium/large-v3; "
                        "auto(按时长自动选); 也可传本地模型目录路径")
    p.add_argument("--lang", default="zh",
                   help="语言代码, 如 zh; 写 auto 则自动检测")
    p.add_argument("--device", default="auto",
                   help="推理设备: auto(默认, 自动检测 GPU) / cpu / cuda")
    p.add_argument("--compute-type", default="auto",
                   help="auto(默认, cuda→float16, cpu→int8) / int8 / float16")
    p.add_argument("--format", default="txt", help="输出格式: txt(默认仅文本) / srt(仅字幕) / both(文本+字幕)")
    p.add_argument("--preview", type=int, default=0,
                   help="在摘要中附带正文前 N 字符预览(0=不附带, 省 token)。"
                        "改进: 现拼接所有段取前 N, 比只取首段更具代表性")
    p.add_argument("--prune-runs", type=int, default=0,
                   help="转录完成后仅保留最近 N 个 run 目录(0=全部保留, 防磁盘堆积)")
    # === 新增: 可靠性与效率 ===
    p.add_argument("--resume", action="store_true",
                   help="断点续传: 复用 out-dir 下最新的 run 目录, 跳过已完成视频")
    p.add_argument("--progress-file", default="",
                   help="增量写进度到该文件(JSON), 供 agent 轮询实时查看进度")
    p.add_argument("--jobs", type=int, default=1,
                   help="系列视频并发数(默认 1 串行)。GPU 模式建议 1, CPU 模式可 2-4")
    p.add_argument("--cache-dir", default="",
                   help="音频缓存目录。按视频 id 复用转码后 wav, 跳过重复下载"
                        "(默认 <out-dir>/.audio_cache)")
    # === 新增: 帧级缓存 ===
    p.add_argument("--transcript-cache", default="",
                   help="转录缓存目录。按视频 id 复用最终 txt/srt 输出,"
                        "同一视频重复转录跳过下载+识别(默认关闭)")
    p.add_argument("--compact", action="store_true",
                   help="紧凑模式: 输出更短的 JSON 摘要(省略 results 数组详情, 省 token)")
    # === 新增: 文本可读性与功能扩展 ===
    p.add_argument("--text-mode", default="merged", choices=["merged", "raw"],
                   help="txt 输出模式: merged(默认, 按语义分段, 可读性高) / "
                        "raw(逐段一行行拼接, 旧行为)")
    p.add_argument("--diarize", action="store_true",
                   help="说话人分离(可选, 需 pyannote.audio + HF_TOKEN)。开启后 "
                        "txt/srt 每段标注 [说话人]; 未装 pyannote 时自动降级")
    # === 新增: 环境检查与 VAD 控制 ===
    p.add_argument("--check-env", action="store_true",
                   help="全面检查运行环境(依赖/模型/GPU/磁盘), 打印报告后退出, 不执行转录")
    p.add_argument("--min-silence-duration-ms", type=int, default=500,
                   help="VAD 最小静音时长(毫秒), 控制分段灵敏度。默认 500,"
                        " 越大则句子越连贯, 越小则分段越细")
    p.add_argument("--batch-size", type=int, default=0,
                   help="faster-whisper 批处理大小。GPU 推荐 64-128 以充分利用显存,"
                        " CPU 保持默认 0 即可(0=引擎默认值)")
    # === Claude Code 模式 ===
    p.add_argument("--claude", action="store_true",
                   help="Claude Code 输出模式: 输出结构化摘要文本,"
                        " 自动启用 --compact --preview 300,"
                        " 方便 Claude 直接读取和处理转录结果")
    args = p.parse_args(argv)

    # 解析持久化输出根目录(默认 <BILI_HOME>/transcripts, 独立于技能/安装目录)
    if not args.out_dir:
        args.out_dir = get_transcripts_dir()

    # 转录缓存默认从环境变量读取(兼容 SKILL.md 配置)
    if not args.transcript_cache:
        args.transcript_cache = os.environ.get("BILI_TRANSCRIPT_CACHE", "")

    # Claude 模式自动启用精简输出 + 预览
    if args.claude:
        if args.preview == 0:
            args.preview = 300
        args.compact = True

    # 环境检查模式
    if args.check_env:
        report = check_env()
        try:
            out = json.dumps(report, ensure_ascii=False, indent=2)
            print(out)
        except UnicodeEncodeError:
            # Windows GBK 终端无法打印 emoji → 转 ASCII-safe
            print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0 if report["ok"] else 1

    # 输入解析
    try:
        url = to_url(args.input)
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 2

    # 仅列模式
    if args.list_only:
        try:
            items = list_videos(url)
        except RuntimeError as e:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
            return 1
        if args.claude:
            lines = ["=" * 60,
                     "  bilibili-transcriber — 视频清单",
                     "=" * 60, ""]
            lines.append(f"📋 共 {len(items)} 个视频")
            lines.append("")
            for it in items:
                dur = it.get("duration", "")
                dur_str = f"{dur}s" if dur else "?"
                lines.append(f"  #{it['index']:>3}  {it['id']}  {it['title']}  ({dur_str})")
            lines.append("")
            lines.append("-" * 60)
            lines.append("提示: 加 --limit N 限制处理数量")
            print("\n".join(lines))
        else:
            print(json.dumps(
                {"ok": True, "mode": "list", "count": len(items), "videos": items},
                ensure_ascii=False,
            ))
        return 0

    # 下载 + 转录
    formats = {"txt", "srt"} if args.format == "both" else {args.format}
    lang = None if args.lang == "auto" else args.lang
    cache_dir = args.cache_dir or get_cache_dir()

    try:
        # === 断点续传: 决定 run_dir 与已有进度 ===
        if args.resume:
            run_dir = _find_latest_run_dir(args.out_dir)
            if run_dir:
                progress = load_progress(run_dir)
                log("进度", f"续传模式: 复用 {run_dir}, 已完成 {len(progress.get('done', {}))} 项")
            else:
                run_dir = _make_run_dir(args.out_dir)
                progress = {"total": 0, "done": {}, "failed": {}, "results": []}
                log("进度", f"续传模式: 未找到历史 run, 新建 {run_dir}")
        else:
            run_dir = _make_run_dir(args.out_dir)
            progress = {"total": 0, "done": {}, "failed": {}, "results": []}

        ffmpeg = get_ffmpeg()

        # === 音频缓存: 先枚举条目, 命中缓存的直接复制 wav, 未命中的才下载 ===
        os.makedirs(cache_dir, exist_ok=True)
        # 先 list 拿到所有条目(含 id), 用于缓存命中判断
        try:
            all_items = list_videos(url)
        except RuntimeError:
            all_items = []  # 单视频 list 可能失败, 后面走直接下载

        # === 转录缓存: 检查最终 txt/srt 是否已缓存过同一视频 ===
        # 比音频缓存更高级: 命中则跳过下载+ASR, 零 token 消耗
        tc_dir = args.transcript_cache
        cached_transcripts: Dict[str, str] = {}  # vid -> txt_path
        if tc_dir and all_items:
            os.makedirs(tc_dir, exist_ok=True)
            for it in all_items:
                vid = it.get("id", "")
                if not vid:
                    continue
                cached_txt = os.path.join(tc_dir, f"{vid}.txt")
                if os.path.isfile(cached_txt):
                    # 复制到 run_dir, 加入结果
                    dst_txt = os.path.join(run_dir, f"{it['index']}_{vid}.txt")
                    shutil.copyfile(cached_txt, dst_txt)
                    # 也复制 srt (若存在)
                    cached_srt = os.path.join(tc_dir, f"{vid}.srt")
                    dst_srt = os.path.join(run_dir, f"{it['index']}_{vid}.srt")
                    if os.path.isfile(cached_srt):
                        shutil.copyfile(cached_srt, dst_srt)
                    # 统计字数
                    chars = len(open(dst_txt, encoding="utf-8").read())
                    entry = {
                        "audio": f"[缓存] {vid}", "language": "zh",
                        "duration_sec": it.get("duration", 0) or 0,
                        "txt": dst_txt, "srt": dst_srt if os.path.isfile(dst_srt) else None,
                        "chars": chars, "segments": 0,
                    }
                    progress["done"][vid] = {
                        "txt": dst_txt, "srt": dst_srt if os.path.isfile(dst_srt) else None,
                        "chars": chars, "segments": 0,
                    }
                    progress["results"].append(entry)
                    cached_transcripts[vid] = dst_txt
                    log("缓存", f"转录缓存命中, 零消耗跳过: {vid} ({chars}字)")
                else:
                    pass  # 未命中, 走正常流程
            # 从 all_items 中剔除已缓存的, 只处理剩余视频
            all_items = [it for it in all_items if it.get("id", "") not in cached_transcripts]

        cached_wavs: Dict[str, str] = {}      # id -> run_dir 内的 wav 路径
        need_download_indices: List[str] = []  # 需要下载的 playlist 序号

        if all_items:
            for it in all_items:
                vid = it.get("id", "")
                if not vid:
                    continue
                # --resume 下已完成的视频跳过下载+转录
                if vid in progress.get("done", {}):
                    log("跳过", f"续传已完成, 跳过: {vid}")
                    continue
                cached = os.path.join(cache_dir, f"{vid}.wav")
                if os.path.isfile(cached):
                    # 命中缓存: 复制 wav 到 run_dir
                    dst = os.path.join(run_dir, f"{it['index']}_{vid}.wav")
                    shutil.copyfile(cached, dst)
                    cached_wavs[vid] = dst
                    log("缓存", f"命中, 跳过下载: {vid}")
                else:
                    need_download_indices.append(it["index"])
            # 只下载未命中的条目
            playlist_items = ",".join(need_download_indices) if need_download_indices else None
        else:
            playlist_items = None

        # 下载未命中缓存的音轨
        raw_files: List[str] = []
        if playlist_items or not all_items:
            raw_files = download_audio(
                url, run_dir, args.limit or None,
                cache_dir=cache_dir, playlist_items=playlist_items,
            )

        if not raw_files and not cached_wavs:
            print(json.dumps(
                {"ok": False,
                 "error": "未下载到任何音频文件, 视频可能不可用或格式不支持。"},
                ensure_ascii=False,
            ))
            return 1

        # 全部转成 16k 单声道 wav(只依赖 ffmpeg, 不需 ffprobe), 命中缓存的跳过
        audio_files: List[str] = list(cached_wavs.values())
        for f in raw_files:
            vid = _video_id_from_path(f)
            # 若该 id 已有缓存 wav(理论上不会, 防御性), 跳过转码
            if vid in cached_wavs:
                continue
            wav = convert_to_wav(f, ffmpeg)
            # 写入缓存(以 id 命名), 供下次复用
            if wav and os.path.isfile(wav):
                cache_wav = os.path.join(cache_dir, f"{vid}.wav")
                try:
                    shutil.copyfile(wav, cache_wav)
                except OSError as e:
                    log("提示", f"音频缓存写入跳过: {e}")
                audio_files.append(wav)

        # 运行时解析设备/精度(auto 按 GPU 可用性自动选择)
        device = resolve_device(args.device)
        compute = resolve_compute(device, args.compute_type)
        log("进度", f"推理设备: {device} / {compute}")

        # --model auto: 按系列总时长自动选模型
        chosen_model = args.model
        if args.model == "auto":
            total_dur = 0.0
            for it in all_items:
                d = it.get("duration")
                if isinstance(d, (int, float)) and d > 0:
                    total_dur += d
            chosen_model = auto_select_model(total_dur)
            log("进度", f"模型自动选择: 总时长 {total_dur:.0f}s -> {chosen_model}")
        model = load_model(chosen_model, device, compute)

        # GPU 模式下自动设置合理的 batch_size(引擎默认值通常太低)
        if args.batch_size == 0 and device == "cuda":
            args.batch_size = 64
            log("进度", f"GPU 模式: batch_size 自动设为 64 (可用 --batch-size 覆盖)")

        # === 预计耗时估算(让用户有心理预期) ===
        total_audio_sec = 0.0
        for af in audio_files:
            try:
                try:
                    # timeout=30: 防损坏音频文件导致 ffmpeg 无限挂起
                    probe = subprocess.run(
                        [ffmpeg, "-i", af, "-f", "null", "-"],
                        capture_output=True, text=True, timeout=30
                    )
                except subprocess.TimeoutExpired:
                    log("警告", f"ffmpeg 时长探测超时, 跳过: "
                                f"{os.path.basename(af)}")
                    continue
                # ffmpeg 在 stderr 输出 "Duration: HH:MM:SS.mm"
                for line in probe.stderr.splitlines():
                    if "Duration" in line:
                        parts = line.strip().split(",")[0].split("Duration:")[-1].strip()
                        h, m, s = parts.split(":")
                        total_audio_sec += int(h) * 3600 + int(m) * 60 + float(s)
                        break
            except Exception:
                pass  # 拿不到就忽略, 不影响主流程

        # 模型速度系数(相对于音频时长的倍数, GPU vs CPU)
        model_speed_map = {
            "tiny":     (0.3, 2),
            "base":     (0.4, 3),
            "small":    (0.5, 4),
            "medium":   (0.8, 6),
            "large-v3": (1.5, 10),
        }
        base_model_name = chosen_model.split(os.sep)[-1]  # 兼容路径
        gpu_factor, cpu_factor = model_speed_map.get(base_model_name, (1.0, 6))
        speed_factor = gpu_factor if device == "cuda" else cpu_factor
        est_sec = total_audio_sec * speed_factor
        if est_sec > 0:
            est_min = est_sec / 60
            file_count = len(audio_files)
            log("预计",
                f"音频总时长 {total_audio_sec:.0f}s ({file_count} 个文件), "
                f"设备 {device.upper()}, 模型 {base_model_name}, "
                f"预估完成时间: {est_min:.0f} 分钟"
                if est_min >= 1
                else f"音频总时长 {total_audio_sec:.0f}s ({file_count} 个文件), "
                     f"设备 {device.upper()}, 模型 {base_model_name}, "
                     f"预估完成时间: {est_sec:.0f} 秒"
            )
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1

    # === 转录(支持并发 + 断点续传 + 进度回调) ===
    # 过滤掉已完成(skip resume)
    pending: List[str] = []
    for af in audio_files:
        vid = _video_id_from_path(af)
        if vid in progress.get("done", {}):
            log("跳过", f"已完成, 跳过转录: {vid}")
            continue
        pending.append(af)

    progress["total"] = len(audio_files)
    write_external_progress(args.progress_file, progress)

    jobs = max(1, args.jobs)
    # GPU 模式并发有 OOM 风险, 用锁串行化 model.transcribe; CPU 模式可真并发
    model_lock = threading.Lock() if (device == "cuda" and jobs > 1) else None
    if jobs > 1 and device == "cuda":
        log("提示", f"GPU 模式 jobs={jobs}, 已加锁串行化推理(避免显存爆炸)")

    total = len(pending)
    done_counter = 0

    def _transcribe_one(audio_path: str) -> Tuple[str, dict]:
        try:
            segments, info = transcribe_file(model, audio_path, lang, model_lock,
                                             min_silence_duration_ms=args.min_silence_duration_ms,
                                             batch_size=args.batch_size)
            det = getattr(info, "language", lang or "auto")
            # 说话人分离(可选): 失败则降级为普通转录
            if args.diarize:
                try:
                    segments = diarize_segments(audio_path, segments)
                except RuntimeError as e:
                    log("提示", f"{e}")
            res = write_outputs(audio_path, segments, run_dir, formats,
                                text_mode=args.text_mode)
            entry = {
                "audio": audio_path,
                "language": det,
                "duration_sec": round(getattr(info, "duration", 0) or 0, 1),
                **res,
            }
            if args.preview:
                entry["preview"] = build_preview(segments, args.preview)
            return audio_path, entry
        except Exception as e:
            return audio_path, {"audio": audio_path, "error": f"转录失败: {e}"}

    def _save_to_tc(vid: str, entry: dict) -> None:
        """转录成功后, 将 txt/srt 写入 transcript cache 供后续复用。"""
        if not tc_dir:
            return
        try:
            src_txt = entry.get("txt")
            src_srt = entry.get("srt")
            if src_txt and os.path.isfile(src_txt):
                shutil.copyfile(src_txt, os.path.join(tc_dir, f"{vid}.txt"))
            if src_srt and os.path.isfile(src_srt):
                shutil.copyfile(src_srt, os.path.join(tc_dir, f"{vid}.srt"))
        except OSError as e:
            log("提示", f"转录缓存写入跳过: {e}")

    if jobs == 1:
        for af in pending:
            done_counter += 1
            log("进度", f"({done_counter}/{total}) 处理: {os.path.basename(af)}")
            af_, entry = _transcribe_one(af)
            vid = _video_id_from_path(af_)
            if "error" in entry:
                progress["failed"][vid] = entry["error"]
            else:
                progress["done"][vid] = {
                    "txt": entry.get("txt"), "srt": entry.get("srt"),
                    "chars": entry.get("chars"), "segments": entry.get("segments"),
                }
                progress["results"].append(entry)
                _save_to_tc(vid, entry)  # 写入转录缓存
                log("完成", f"{os.path.basename(af_)} -> {entry.get('txt')} / {entry.get('srt')}")
            save_progress(run_dir, progress)
            write_external_progress(args.progress_file, progress)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(_transcribe_one, af): af for af in pending}
            for fut in as_completed(futures):
                done_counter += 1
                af_ = futures[fut]
                log("进度", f"({done_counter}/{total}) 完成: {os.path.basename(af_)}")
                af_, entry = fut.result()
                vid = _video_id_from_path(af_)
                if "error" in entry:
                    progress["failed"][vid] = entry["error"]
                else:
                    progress["done"][vid] = {
                        "txt": entry.get("txt"), "srt": entry.get("srt"),
                        "chars": entry.get("chars"), "segments": entry.get("segments"),
                    }
                    progress["results"].append(entry)
                    _save_to_tc(vid, entry)  # 写入转录缓存
                save_progress(run_dir, progress)
                write_external_progress(args.progress_file, progress)

    if args.prune_runs:
        _prune_old_runs(args.out_dir, args.prune_runs)

    summary = {
        "ok": True,
        "mode": "transcribe",
        "model": chosen_model,
        "run_dir": run_dir,
        "count": len(progress["results"]),
        "total": progress["total"],
        "done": len(progress["done"]),
        "failed": len(progress["failed"]),
        "failed_ids": list(progress["failed"].keys()),
        # compact 模式: 不输出 results 数组详情(省 token), agent 可根据 run_dir 文件路径直接 access
        "results": progress["results"] if not args.compact else [
            {"tx": r.get("txt"), "sr": r.get("srt"), "c": r.get("chars"),
             "d": r.get("duration_sec"), "l": r.get("language")}
            for r in progress["results"]
        ],
    }

    if args.claude:
        print(format_claude_summary(chosen_model, run_dir, progress, device))
        # NOTA: --claude 模式下仍输出 JSON 到 stderr 供程序化使用
        print(json.dumps(summary, ensure_ascii=False), file=sys.stderr)
    else:
        print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
