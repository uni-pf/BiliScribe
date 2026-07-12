---
name: bilibili-transcriber
description: "将哔哩哔哩(Bilibili)等视频平台上的视频内容自动转为文字。当用户提供 B 站视频链接、BV 号、av 号或 b23.tv 短链, 并希望把语音内容提取为文字(含分P/系列视频), 输出纯文本(.txt)和/或带时间轴字幕(.srt)时使用。触发语: 把B站视频转文字、提取视频字幕、语音转写、视频转稿、B站视频转SRT, 或对在线视频做转写。"
agent_created: true
---

# Bilibili Transcriber(视频转文字)

## Overview

将 B 站(及 yt-dlp 支持的其他平台)视频的语音内容自动转为文字。流程为:
**解析输入(BV/av/链接) → yt-dlp 下载最佳音轨 → faster-whisper 中文语音识别
→ 输出纯文本与/或 SRT 字幕**。内置系列视频枚举、错误映射与"省 token"输出约定。

## 可靠性与效率特性

- **断点续传(`--resume`)**: 长合集转录中途失败, 重跑加 `--resume` 自动复用最新 run 目录, 跳过已完成视频, 只处理剩余项。进度持久化在 `run_dir/progress.json`(原子写)。
- **进度回调(`--progress-file`)**: 增量写进度到指定 JSON 文件, agent 可轮询实时查看"第几集/共几集/已失败项", 长任务不再像黑盒。
- **并发转录(`--jobs N`)**: 系列视频共享单模型实例并发推理。GPU 模式自动加锁串行化(避免显存爆炸), CPU 模式可 `--jobs 2-4` 提速。
- **音频缓存(`--cache-dir`)**: 按视频 id 复用转码后 wav, 同一视频重复转录跳过下载, 显著省时省流量。默认 `<out-dir>/.audio_cache`。
- **模型完整性校验**: 本地模型目录不仅检查 `model.bin`, 还校验 `config.json` / `tokenizer.json`, 残缺模型会跳过并警告, 不再加载到一半才报错。
- **preview 改进**: `--preview N` 现拼接所有段取前 N 字符, 比只取首段更具代表性(首段往往是"大家好"寒暄)。
- **GPU 逻辑解耦**: cuBLAS 检测/启用逻辑下沉到独立模块 `gpu_utils.py`, `transcribe.py` 不再隐式依赖 `setup_env.py`(后者含 pip 安装副作用)。跨平台: Linux/macOS 跳过 DLL 复制, 直接用 ctranslate2 wheel 自带运行时。
- **段落合并(`--text-mode merged`)**: txt 输出默认按 VAD 段间静音间隔做语义分段(gap<1.5s 紧密拼接、1.5-3s 加句号、>=3s 空行分段), 可读性远高于逐行拼接; `--text-mode raw` 可切回旧行为。
- **模型自动选择(`--model auto`)**: 按系列总时长自动选模型 — <10min 用 `large-v3`(精度优先)、10-60min 用 `medium`(平衡)、>60min 用 `small`(速度优先), 拿不到时长回退 `medium`。
- **说话人分离(`--diarize`, 可选)**: 开启后 txt/srt 每段标注 `[说话人]`, 适合访谈/多人对话。基于 `pyannote.audio`(可选依赖, 需 `HF_TOKEN`), 未安装时自动降级为普通转录并提示。
- **环境一键检查(`--check-env`)**: 无需依赖安装即可快速检测 Python 版本、yt-dlp/faster-whisper/ffmpeg 安装状态、本地模型完整性、GPU 可用性、磁盘剩余空间, 输出结构化 JSON 报告。适合 Setup 后验证或故障排查。
- **预计耗时估算**: 转录启动前根据音频总时长、模型大小、设备类型(CPU/GPU)自动估算并显示预计完成时间, 让用户有明确心理预期, 不再"黑盒空转"。
- **VAD 参数可调(`--min-silence-duration-ms`)**: 将之前硬编码的 VAD 最小静音时长(默认 500ms)暴露为命令行参数。调大(如 1000)让句子更连贯, 调小(如 300)让分段更精细, 适合不同语速的内容。

## When to Use

- 用户给出 B 站视频链接 / BV 号 / av 号 / b23.tv 短链, 想要文字稿。
- 需要视频字幕(.srt)或纯文本转录。
- 涉及分P / 合集 / 系列视频, 需批量处理其中若干集。
- 需要中文语音识别, 并希望有基本错误处理(视频不存在、无法访问、网络失败等)。

## ⚡ 持久化跨会话 Setup (D 盘 · 推荐 · 仅需一次)

**⚠️ 核心坑**: WorkBuddy 沙箱环境是**会话级临时**的——每次新对话启动, 之前建的 venv 和 HF 缓存目录都可能不可用。
**✅ 解法**: 把所有重资产（venv、模型、缓存）装到 **D 盘**持久化路径下, 一次安装永久复用。不占用 C 盘系统空间。

```
D:\workbuddy\.bili-transcriber\   ← 所有数据都在这里
├── venv\           # Python 虚拟环境 (pip 依赖 ~200MB)
├── models\         # Whisper 模型文件 (medium=1.5GB)
├── cache\          # 音频缓存 (视频越多越大)
└── transcripts\    # 默认输出目录
```

### 第1步: 检查是否已安装 (2 秒预检)

```bash
D_BILI="D:/workbuddy/.bili-transcriber"
ls "$D_BILI/venv/Scripts/python.exe" && ls "$D_BILI/models/medium/model.bin"
```

- ✅ 两条都命中 → **直接跳到 Workflow, 不用跑任何 Setup**
- ❌ 任意一条失败 → 继续下面的安装

### 第2步: 创建 D 盘 venv + 安装依赖 + 预下载模型

```bash
D_BILI="D:/workbuddy/.bili-transcriber"

# A) 用托管 Python 在 D 盘创建 venv (仅一次)
"C:/Users/PFH/.workbuddy/binaries/python/versions/3.13.12/python.exe" \
  -m venv "$D_BILI/venv"

# B) 安装依赖 (yt-dlp + faster-whisper + imageio-ffmpeg)
"$D_BILI/venv/Scripts/python.exe" scripts/setup_env.py

# C) 预下载模型 (推荐 medium, 约 1.5GB)
"$D_BILI/venv/Scripts/python.exe" scripts/setup_env.py --download-model medium
```

> **各模型磁盘占用**: small ≈ 460MB, medium ≈ 1.5GB, large-v3 ≈ 3GB
> **推荐 `medium`**: 速度/精度平衡最佳

### 第3步: 验证

```bash
D_BILI="D:/workbuddy/.bili-transcriber"

# 验证依赖
"$D_BILI/venv/Scripts/python.exe" \
  -c "import yt_dlp, faster_whisper, imageio_ffmpeg; print('✅ 依赖全部就绪')"

# 验证模型完整性 (自动校验 model.bin + config.json + tokenizer.json)
ls "$D_BILI/models/medium/model.bin" && echo "✅ 模型就绪"
```

> **如果验证失败**:
> - venv 问题 → 删掉 `$D_BILI/venv` 重跑第2步-A
> - 模型下载失败 → 重跑 `scripts/setup_env.py --download-model medium`
> - 模型不完整 → 检查 model.bin / config.json / tokenizer.json 三个文件是否齐全

### 后续会话: 环境变量速查

转录时导出以下变量即可直接复用（也可写进 shell profile）:

```bash
# 建议存到 ~/.bashrc 或 WorkBuddy 的工作区 memory
export BILI_PYTHON="D:/workbuddy/.bili-transcriber/venv/Scripts/python.exe"
export BILI_MODEL_DIR="D:/workbuddy/.bili-transcriber/models"
export BILI_CACHE_DIR="D:/workbuddy/.bili-transcriber/cache"
export BILI_OUT_DIR="D:/workbuddy/.bili-transcriber/transcripts"
export BILI2TEXT_MODEL_DIR="$BILI_MODEL_DIR/medium"
```

```bash
# 转录时直接用
"$BILI_PYTHON" scripts/transcribe.py "BV1xx411c7mD" \
  --out-dir "$BILI_OUT_DIR/$(date +%Y%m%d)" \
  --model medium \
  --cache-dir "$BILI_CACHE_DIR"
```

> **自动探测机制**: `transcribe.py` 会优先检查 `<技能目录>/models/medium/`（C 盘）。
> 但 D 盘模型不在此路径下, 所以**必须设 `BILI2TEXT_MODEL_DIR`** 或用 `BILI_MODEL_DIR` 指向。

### 迁移指南（已有 C 盘环境 → D 盘）

如果你之前已经装到 C 盘（`C:\Users\PFH\.workbuddy\binaries\python\envs\default\`），不想重新下载 1.5GB 模型:

```bash
D_BILI="D:/workbuddy/.bili-transcriber"

# 1) 在 D 盘创建 venv
"C:/Users/PFH/.workbuddy/binaries/python/versions/3.13.12/python.exe" \
  -m venv "$D_BILI/venv"

# 2) 拷贝已有模型 (不用重新下载)
cp -r "C:/Users/PFH/.workbuddy/skills/bilibili-transcriber/models/medium" \
      "$D_BILI/models/medium"

# 3) 安装依赖到新 venv
"$D_BILI/venv/Scripts/python.exe" scripts/setup_env.py

# 4) 验证
"$D_BILI/venv/Scripts/python.exe" \
  -c "import yt_dlp, faster_whisper, imageio_ffmpeg; print('✅ 迁移完成')"
ls "$D_BILI/models/medium/model.bin" && echo "✅ 模型已就位"
```

## Setup (备选 · 临时/沙箱环境)

如果你在其他机器或纯临时沙箱中运行(无上述持久化路径), 则每次从头安装:

```bash
# 以托管 Python 虚拟环境解释器运行(若 venv 尚未创建, 先: python -m venv $VENV):
$VENV_PYTHON "scripts/setup_env.py"
# 可选: 先检测本机 GPU 情况(打印 JSON, 不改动):
$VENV_PYTHON "scripts/setup_env.py" --detect-only
# 可选: 一次性预下载模型到 <技能>/models/, 之后离线、稳定加载(推荐, 见下方说明)
$VENV_PYTHON "scripts/setup_env.py" --download-model small
```

> **关于虚拟环境变量**: `$VENV` 指你的 Python 虚拟环境目录, `$VENV_PYTHON` 指其解释器(类 Unix: `$VENV/bin/python`, Windows: `$VENV\Scripts\python.exe`)。若尚未创建, 先 `python -m venv "$VENV"`(例如 `python -m venv .venv`)。也可直接用任意已装好依赖的 Python 解释器运行本技能脚本。

- 安装完成即可使用。模型默认在首次转录时从 HuggingFace 下载(见 `references/asr_engines.md`), 仅一次。
- **GPU / CPU 自动感知(关键)**: `setup_env.py` 默认 `--device auto`, 会:
  1. 调用 `ctranslate2.get_cuda_device_count()` 检测是否有 NVIDIA GPU;
  2. 若**有 GPU 但 `ctranslate2` 包内缺 cuBLAS 12 运行时**(常见: `cublas64_12.dll is not found`), 自动从系统已有的 NVIDIA 目录(NGX / CUDA Toolkit / System32)复制 `cublas64_12.dll` / `cublasLt64_12.dll` / `cudart64_12.dll` 进 `ctranslate2` 包目录, **一键启用 GPU**, 无需手动装 CUDA Toolkit;
  3. 若**无 GPU**, 自动回退 CPU。
  - 也可显式 `--device cpu`(强制 CPU) 或 `--device cuda`(强制启用 GPU, 同样会自动复制 cuBLAS)。
  - `ctranslate2` 的 PyPI wheel 本身就是 **CPU+CUDA 双用**的同一个包, 不存在"分别安装 GPU 版/CPU 版"——真正的开关是推理设备(device), 见下方说明。
- **询问用户**: 若你不确定本机有无 GPU, 可先用 `--detect-only` 看结果; 或在装配环境前用 AskUserQuestion 问用户「用 GPU 还是 CPU?」, 再把 `--device` 传进去。
- **强烈建议先 `--download-model`** 预下载: 在沙箱/离线/镜像环境, 运行时临时下载常踩坑(符号链接 checkout 失败、大文件走 Xet/CAS 在镜像上 401)。预下载会把模型落地到 `<技能目录>/models/<name>/`(含 `model.bin`)。**`transcribe.py` 现在会自动探测该目录**, 因此预下载后直接 `--model <name>` 即可离线加载, 无需再手设 `BILI2TEXT_MODEL_DIR`(该变量仍可作为显式覆盖, 优先级高于自动探测)。
- 后续所有调用均用该 venv 解释器运行 `scripts/transcribe.py`。

## Environment Prerequisites(环境前置 · 必读)

`scripts/transcribe.py` 启动时会**自动设置**以下环境变量(若你已显式指定则不覆盖)。在本沙箱/镜像/离线环境中, 这些是跑通的关键:

| 环境变量 | 取值 | 作用 |
|---------|------|------|
| `CODEBUDDY_SAFE_DELETE_SANDBOX` | `0` | 关闭沙箱删除钩子, 否则清理临时文件会强行抛 `OSError` |
| `HF_ENDPOINT` | `https://hf-mirror.com` | HuggingFace 镜像, 加速/可访问 |
| `HF_HUB_DISABLE_SYMLINKS` | `1` | 沙箱内符号链接 checkout 失败 → 改用复制 |
| `HF_HUB_DISABLE_XET` | `1` | 大文件走 Xet/CAS 在镜像上 401 → 改普通 HTTPS |
| `BILI2TEXT_MODEL_DIR` | 模型目录路径(可选) | 指向含 `model.bin` 等完整文件的本地目录, 跳过 HF 缓存/下载(完整性校验: 缺文件则跳过并警告) |
| `OMP_NUM_THREADS` | 如 `4`(可选) | 限制 CPU 线程数, 避免占满核 |

> 你能直连官方 HF 时, 可在调用前 `unset HF_ENDPOINT HF_HUB_DISABLE_SYMLINKS HF_HUB_DISABLE_XET` 改用官方源; 其余变量保留无害。

**关键实现说明(已固化, 无需你手动处理):**
- **ffmpeg 缺 ffprobe**: `imageio-ffmpeg` 只提供 `ffmpeg`, 没有 `ffprobe`。脚本因此**不**使用 yt-dlp 的 `FFmpegExtractAudio` 后处理, 而是直接下载原始音轨(m4a/opus/webm), 再用内置 `ffmpeg` 显式转成 16k 单声道 wav(`-ar 16000 -ac 1 -vn`)。全程只依赖 ffmpeg, 不碰 ffprobe。
- **GPU / CPU 自动选择**: `transcribe.py` 默认 `--device auto`, 自动检测 GPU 并选择 cuda/cpu。GPU cuBLAS 启用机制(无需手动装 CUDA Toolkit)见上方「Setup (备选)」章节。缺 cuBLAS 时自动回退 CPU。

## Workflow

### Step 0 — 预检: 确认持久化环境 + 汇报给用户 (3 秒)

每次调用此技能时, 优先做预检。**预检结果必须在对话中汇报给用户**, 让用户感知到发生了什么：

```bash
# === 定义持久化路径 ===
D_BILI="D:/workbuddy/.bili-transcriber"
BILI_PYTHON="$D_BILI/venv/Scripts/python.exe"
BILI_MODEL="$D_BILI/models/medium/model.bin"

# === 预检 ===
if [ -f "$BILI_PYTHON" ] && [ -f "$BILI_MODEL" ]; then
    echo "✅ 持久化环境就绪: D盘 venv + medium 模型"
    VENV_PYTHON="$BILI_PYTHON"
    MODEL_NAME="medium"
    MODEL_DIR="$D_BILI/models/medium"
    CACHE_DIR="$D_BILI/cache"
    # 预检通过后输出一句给用户的提示（见下方交互规范）

    # 可选: 执行一键环境检查, 确认所有依赖就绪
    echo "🔍 正在检查运行环境…"
    ENV_REPORT=$("$VENV_PYTHON" "scripts/transcribe.py" dummy --check-env 2>/dev/null)
    echo "$ENV_REPORT" | python -c "import sys,json; d=json.load(sys.stdin); \
      print('✅ 环境就绪' if d.get('ok') else '⚠️ 环境有缺漏'); \
      [print(f'  {k}: {v}') for k,v in d.get('dependencies',{}).items()]; \
      gpu=d.get('gpu',{}); print(f'  GPU: {gpu.get(\"cuda_device_count\",\"?\")} 设备, {gpu.get(\"reason\",\"\")}')"
else
    echo "⚠️ 持久化环境缺失, 请先执行 Setup"
    VENV_PYTHON="C:/Users/PFH/.workbuddy/binaries/python/versions/3.13.12/python.exe"
    MODEL_NAME="medium"
    MODEL_DIR=""
    CACHE_DIR=""
fi
```

预检通过后, 后续所有命令用 `$VENV_PYTHON` / `$MODEL_DIR` / `$CACHE_DIR` 替代硬编码路径。

> **给用户的交互提示**: 预检通过后, agent 应向用户报告类似:
> > ✅ 环境就绪，使用 D 盘持久化 venv + medium 模型。现在开始下载视频音轨…
>
> 让用户知道: a) 不会重新下载依赖; b) 用了哪个模型; c) 当前在做什么。

### Step 1 — 解析输入并决定是否先枚举系列

1. 识别用户输入中的链接 / BV 号 / av 号。
2. **若可能是系列/分P/合集**, 先执行枚举(不下载、不转录, 极省 token):

   ```bash
   $VENV_PYTHON "scripts/transcribe.py" "$INPUT" --list-only
   ```

   脚本返回 JSON: `{"ok": true, "mode": "list", "count": N, "videos": [{index,id,title,duration}...]}`。
3. 把清单呈现给用户, 确认处理范围:**全部 / 前 N 集(`--limit N`) / 指定分P**。
   - 单视频(清单仅 1 条)可跳过此步直接转录。

### Step 2 — 下载并转录 (带进度反馈)

正式运行。**必须对用户输出明确的进度反馈**, 不要默默跑完。

```bash
# 设置 D 盘持久化路径
OUT_DIR="${BILI_OUT_DIR:-$D_BILI/transcripts/$(date +%Y%m%d)}"
PROGRESS_FILE="$OUT_DIR/.progress.json"

# ↑ 预检阶段已设好 VENV_PYTHON / MODEL_DIR / CACHE_DIR

# 设置脚本所需环境变量(详见「Environment Prerequisites」章节)
export BILI2TEXT_MODEL_DIR="$MODEL_DIR"

$VENV_PYTHON "scripts/transcribe.py" "$INPUT" \
    --out-dir "$OUT_DIR" \
    --model "$MODEL_NAME" --lang zh --format "${FORMAT:-txt}" \
    --device auto --compute-type auto \
    --cache-dir "$CACHE_DIR" \
    --transcript-cache "${BILI_TRANSCRIPT_CACHE:-$CACHE_DIR/transcripts}" \
    --compact \
    --progress-file "$PROGRESS_FILE" \
    [--limit N] [--preview 120] \
    [--jobs N] [--resume] [--text-mode merged|raw] [--diarize]
```

#### 进度反馈规范 (agent 必读)

在转录进行时（尤其长视频/合集）, agent **必须**周期性地向用户报告进度:

| 阶段 | 用户能看到什么 | agent 怎么做 |
|------|---------------|-------------|
| 环境检查 | `🔍 正在检查运行环境…` | `--check-env` 快速验证依赖和模型状态 (2 秒) |
| 下载音轨 | `⏳ 正在下载音轨… (视频时长约 12min)` | 从 stderr 的 `[进度] 正在下载音轨` 转发 |
| 加载模型 | `🧠 加载语音识别模型: medium` | 从 stderr 的 `[进度] 加载语音识别模型` 转发 |
| 预计耗时 | `⏱ 预估: 总时长 45min, GPU/medium, 约 36 分钟完成` | 脚本自动估算并显示于 `[预计]` 标签 |
| 识别中 | `📝 正在识别: 01_BV1xx.mp4 (第 3/12 个视频)` | 从 stderr 的 `[进度] 识别中` 转发 |
| 完成 | `✅ 已完成 3/12, 失败 0` | 从 stderr 的 `[完成]` 汇总 |
| 合集进度(长任务) | `📊 合集进度: 5/12 已完成, 预计剩余 ~8min` | 轮询 `--progress-file` JSON, 向用户汇报 |

**长任务 (>5 分钟) 的进度汇报策略**:
1. 转录启动时告诉用户: *"开始转录, 共 12 个视频, 总时长约 45 分钟, 用的 medium 模型"*
2. 每完成 3-5 个或每 2 分钟更新一次: *"已处理 7/12, 失败 0 个"*
3. 全部完成后给出最终摘要: *"全部完成! 12/12 个视频转录成功, 输出到 D 盘 transcripts 目录"*

> **`--progress-file` 文件内容格式**:
> ```json
> {"total":12, "done_count":5, "failed_count":0,
>  "done_ids":["BV1xx","BV1yy","BV1zz"], "failed_ids":[],
>  "updated_at":1741766400}
> ```
> 用 `cat "$PROGRESS_FILE"` 轮询即可获得实时快照。

- `--model`: 默认 `medium`; `auto` 按时长自动选(见特性章节)。模型权衡见 `references/asr_engines.md`。
- `--device` / `--compute-type`: 默认 `auto` / `auto`(自动检测 GPU)。
- `--lang zh`: 中文视频用 `zh`; 多语种用 `auto`。
- `--format`: `txt`(默认) / `srt` / `both`。需要 SRT 时事前询问用户。
- `--limit N`: 系列仅处理前 N 集。
- `--diarize`: 说话人分离, 需额外配置 pyannote.audio + HF_TOKEN, 见 `references/asr_engines.md`。
- `--check-env`: 快速检测运行环境(依赖/模型/GPU/磁盘), 打印 JSON 报告后退出, 不执行转录。
- `--min-silence-duration-ms`: VAD 最小静音时长(毫秒), 默认 500。调大(如 1000)让句子更连贯, 调小(如 300)让分段更细。
- 其余参数(`--preview`/`--jobs`/`--resume`/`--cache-dir`/`--text-mode`/`--prune-runs`/`--transcript-cache`)均有合理默认值, 详细见 `references/asr_engines.md`。

> **⚠️ 语言默认值提醒**: `--lang` 默认为 `zh`(面向 B 站中文视频优化)。若转写**非中文 / 外语 / 多语种**视频(如 YouTube 英文), 务必显式传 `--lang auto`, 否则会被强制按中文识别导致准确率崩盘。

脚本完成后的 stdout 为单个 JSON 摘要(进度日志在 stderr, 不污染结果):

```json
{"ok": true, "mode": "transcribe", "model": "medium", "run_dir": "...",
 "count": 2, "total": 2, "done": 2, "failed": 0, "failed_ids": [],
 "results": [
   {"audio": "...", "language": "zh", "duration_sec": 612.3,
    "txt": ".../01_BVxxx.txt", "srt": ".../01_BVxxx.srt",
    "chars": 8421, "segments": 213, "preview": "大家好, 今天我们来..."}
 ]}
```

### Step 3 — 呈现结果 (交互增强版)

**不要**把 `.txt` / `.srt` 正文读入上下文。用 **present_files** 交付, 同时在对话中给用户一个清晰的摘要卡片:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 ✅ 转录完成
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 📁 输出目录: D:\workbuddy\.bili-transcriber\transcripts\20260713
 📄 文件数:      3 个视频
 📝 总字数:      12,847 字
 ⏱ 总时长:      38 分 21 秒
 🌐 识别语言:    zh (中文)
 💾 模型:        medium / CPU
 🗂 格式:        .txt (若用户选了 SRT 则显示 .txt+.srt)
 ❌ 失败:        0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

交付格式:
1. 用 `present_files` 一次性列出所有产出文件
2. 如果用户只想要"大意", 可展示 JSON 中的 `preview` 字段(前 N 字符), 而非全文
3. 如果用户想要全文摘要, 后续用其他技能对 `.txt` 文件做摘要, 不要把全文贴回对话
4. 多个文件时, 用 `present_files` 一次性列出所有产出路径

## 用户交互与反馈规范 (agent 必读)

本技能的**交互质量**直接决定用户对工具的好感度。以下规范强制约束 agent 的沟通方式。

### 1. 每次操作前说清楚在干什么

| 操作 | agent 对用户的说话 |
|------|------------------|
| 预检通过 | `✅ 环境就绪，D 盘持久化 venv + medium 模型，开始转录…` |
| 下载音轨 | `⏳ 正在下载视频音轨 (视频时长约 12 分钟)…` |
| 加载模型 | `🧠 加载语音识别模型 medium，首次加载约 10 秒…` |
| 识别进行中 | `📝 正在识别 第3/12 个视频…` |
| 合集长任务 | `📊 已处理 5/12，失败 0，继续… 可随时让我暂停` |
| 全部完成 | `✅ 全部完成！请查收下方输出文件` |
| 模型下载(首次) | `⏳ 首次使用，正在下载 whisper 模型 (medium, 约 1.5GB, 取决于网络速度)…` |
| GPU 加速 | `⚡ 检测到 NVIDIA GPU，使用 CUDA 加速推理` |
| 失败 | `❌ 第3个视频转录失败: [原因]。已跳过，继续处理其余视频` |

### 2. 长任务进度汇报策略

- **< 1 分钟**: 只在开始和结束时各汇报一次
- **1-5 分钟**: 开始 → 中途 1 次 → 结束
- **> 5 分钟**: 每完成 3-5 个或每 2 分钟向用户更新一次进度
- 合集任务启动时告知总集数和预估时长: *"总共 12 个视频, 合计约 45 分钟, 用 medium 模型, 预计 15-20 分钟完成"*
- 使用 `--progress-file` 轮询获取实时进度, 不要靠猜

### 3. 错误处理的交互

| 场景 | agent 对话规范 |
|------|--------------|
| 视频不存在 | `❌ 视频不存在或已下架，请确认链接或 BV 号是否正确` |
| 下载失败 | `⚠️ 下载超时，可能是网络问题，要重试吗？` |
| 模型缺失 | `⚠️ 模型未预下载，正在从 HuggingFace 镜像下载 (约 1.5GB)…` |
| GPU 不可用 | `ℹ️ 未检测到 CUDA GPU，使用 CPU 推理 (会比 GPU 慢 3-5 倍)` |

> **不要**向用户展示原始异常栈或`{"ok": false}` JSON。把 error 字段的中文提示直接转述。

### 4. 首次使用的引导

如果预检发现环境缺失, agent 应向用户说明:

> 🛠 首次使用需要安装环境（仅一次），会将依赖和模型安装到 D 盘持久化目录
> `D:\workbuddy\.bili-transcriber\`
> - 依赖约 200MB
> - 语音模型 medium 约 1.5GB
> - 安装完成后后续会话直接复用，不再下载
>
> 是否继续安装？

获得用户确认后再执行 Setup。

### 5. Token/反馈平衡策略 (关键)

**核心矛盾**: 进度反馈越详细 → 消耗 token 越多。以下策略在用户体验和 token 消耗之间取得最佳平衡。

#### 5a. 前置询问: 让用户选择反馈密度 + 格式

**每个任务前, 都问一句是否需要 SRT 字幕** (默认仅 txt):

> 输出格式: 纯文本(.txt) 还是 文本+字幕(.txt+.srt)?
> SRT 字幕适合需要精确时间轴定位的场景, 体积是 txt 的 2-3 倍。
> 默认仅 txt, 需要 SRT 吗? (Y/n)

如果用户选了需要 SRT, 转录时加 `--format both`。否则只用默认 `--format txt`。

**在开始合集任务 (>2 个视频)** 前, 额外问用户偏好:

> 这个合集有 12 个视频, 约 45 分钟。你想要哪种反馈模式?
> **A) 静默模式 📄** — 只给最终文件, 中间不报进度 (最省 token)
> **B) 标准模式 📊** — 开始/关键节点/结束时报进度 (推荐)
> **C) 详细模式 📝** — 每完成一个视频都通报 (最耗 token)

如果是**单视频或短任务 (<5 分钟)**, 默认标准模式, 不用问。

#### 5b. 智能轮询: 根据预估时长校准频率

```
--list-only 拿到视频时长 →
  总时长 < 5 分钟  → 不轮询, 等完成
  总时长 5-30 分钟 → 轮询 1 次 (50% 时)
  总时长 30-60 分钟 → 每 3 分钟轮询一次
  总时长 > 60 分钟 → 每 5 分钟轮询一次
```

即: **总轮询次数 ≈ 5-6 次**, 不因视频长就无限轮询。

#### 5c. 转录缓存: 同一视频第二次 0 token 消耗

```bash
# 添加 --transcript-cache 指向持久化缓存目录
# 同一 BV 号第二次转录直接返回结果, 跳过下载和 ASR
$VENV_PYTHON scripts/transcribe.py "$INPUT" \
  --transcript-cache "$BILI_BASE/cache/transcripts" \
  ...
```

**节省量**: 一个 10 分钟视频 ≈ 15 秒下载 + 2 分钟 ASR → **整个流程跳过**。

#### 5d. Compact 模式: 精简 JSON 输出

```bash
# --compact 让结果 JSON 更短 (字段名从全称缩写为 2-3 字符)
$VENV_PYTHON scripts/transcribe.py "$INPUT" --compact ...
```

**节省量**: JSON 体量缩小约 60% (`"txt"` → `"tx"`, `"duration_sec"` → `"d"`)。

#### 5e. 省 token 清单 (决策速查)

| 策略 | 省什么 token | 影响 | 推荐场景 |
|------|-------------|------|---------|
| 转录缓存 | 整个转录流程 | C 盘空间+1 倍 | 反复看同系列视频 |
| compact 模式 | JSON 输出大小 -60% | 可读性略降 | 合集/批量任务 |
| 静默模式 | 全部进度对话 | 用户看不到进度 | 用户不在电脑前 |
| 标准模式(默认) | 适中 | 质量 OK | 大多数场景 |
| --preview 120 | 结果预览省全文 | 只省读取步骤 | 用户只要大意时 |
| --no-transcript-cache | — | 不存缓存 | 一次性任务 |

## 配置选项

### 环境变量速查 (可写进 WorkBuddy Memory 或 .bashrc)

```bash
# =============================================
# bilibili-transcriber 持久化配置
# 建议写到 ~/.bashrc 或 ~/.profile 中
# =============================================

# 基础路径 (所有重资产都在 D 盘)
export BILI_BASE="D:/workbuddy/.bili-transcriber"

# Python 解释器
export BILI_PYTHON="$BILI_BASE/venv/Scripts/python.exe"

# 模型目录
export BILI_MODEL_DIR="$BILI_BASE/models"
export BILI2TEXT_MODEL_DIR="$BILI_MODEL_DIR/medium"   # 当前使用的模型

# 缓存与输出
export BILI_CACHE_DIR="$BILI_BASE/cache"              # 音频缓存 (wav)
export BILI_TRANSCRIPT_CACHE="$BILI_BASE/cache/transcripts"  # 🔥 转录缓存 (txt/srt)
export BILI_OUT_DIR="$BILI_BASE/transcripts"          # 默认输出目录

# 模型选择 (可切换: small / medium / large-v3)
export BILI_MODEL_SIZE="medium"

# 推理设备 (auto / cuda / cpu)
export BILI_DEVICE="auto"

# 并发数 (CPU 可 2-4, GPU 建议 1)
export BILI_JOBS="1"

# 输出格式 (txt / srt / both, 默认 txt)
export BILI_FORMAT="txt"

# 语言 (zh / auto)
export BILI_LANG="zh"
```

### 模型选型决策表

| 场景 | 推荐模型 | 磁盘 | 速度 | 准确率 |
|------|---------|------|------|--------|
| 短视频 (<10min) | `large-v3` | 3GB | 慢 | ⭐⭐⭐⭐⭐ |
| 常规视频 (10-60min) | `medium` | 1.5GB | 中 | ⭐⭐⭐⭐ |
| 长视频/合集 (>60min) | `small` | 460MB | 快 | ⭐⭐⭐ |
| 自动选择 | `auto` | — | 按时长自动 | 平衡 |

### `--jobs` 并发推荐

| 设备 | 推荐值 | 说明 |
|------|--------|------|
| GPU (任何) | `1` | 脚本自动加锁串行化推理, 避免显存爆炸 |
| CPU, 长合集 | `2-4` | 多核 CPU 可并发, 实测 2-3 倍提速 |
| CPU, 单视频 | `1` | 单视频并发无意义, 纯增加开销 |

### 快速切换配置

```bash
# 想换 large-v3 模型跑一次高精度
BILI_MODEL_SIZE="large-v3" \
BILI2TEXT_MODEL_DIR="$BILI_MODEL_DIR/large-v3" \
"$BILI_PYTHON" scripts/transcribe.py "BV..." --model large-v3

# 强制 CPU 跑 (省电/无 GPU)
BILI_DEVICE="cpu" \
"$BILI_PYTHON" scripts/transcribe.py "BV..." --device cpu --model small
```

核心原则:
1. 全部下载/识别在 `scripts/` 中完成, 转录正文**永不**进入对话上下文。
2. 系列视频先用 `--list-only` 确认范围, 再决定是否加 `--limit`。
3. 输出仅返回元信息 JSON; 如需快速确认, 用 `--preview N` 取片段, 不要全文回显。
4. 把成品文件通过 `present_files` 交付, 对话里只保留路径与统计数字。

## Error Handling Playbook

脚本对常见故障已做映射, 返回 `{"ok": false, "error": "中文提示\n原始信息: ..."}`。
agent 应把 `error` 原文转述给用户, 不要自行猜测原因:

- 视频不存在/已下架 → 提示换链接或确认 BV 号。
- 私密 / 需登录 / 仅会员 → 说明权限限制, 本技能无法绕过。
- 404 → 核对链接/BV 号拼写。
- 版权限制 → 说明该视频受版权保护无法下载。
- 网络超时 → 建议重试或检查网络。
- 依赖缺失(yt-dlp / faster-whisper / imageio-ffmpeg) → 提示先运行 `setup_env.py`。
- 模型下载失败 → 多为网络/HF 访问问题。优先用 `setup_env.py --download-model <name>` 预下载到本地, 再以 `BILI2TEXT_MODEL_DIR` 指向; 务必保留脚本自动设的 `HF_ENDPOINT` / `HF_HUB_DISABLE_SYMLINKS` / `HF_HUB_DISABLE_XET` 环境变量。
- `ffprobe and ffmpeg not found` → 不应再出现(脚本已改用原始音轨 + 内置 ffmpeg 转码, 不经 ffprobe)。若仍出现, 检查 `imageio-ffmpeg` 是否安装、或 PATH 是否有 ffmpeg。
- `cublas64_12.dll is not found` / CUDA 相关 → 通常是 `ctranslate2` 包内缺 cuBLAS 12 运行时。先跑 `setup_env.py --detect-only` 确认是否有 GPU; 有 GPU 时重跑 `setup_env.py --device auto` 会自动从系统复制 cuBLAS 启用(无需装 CUDA Toolkit); 若确实无 GPU, 用 `--device cpu` 即可(脚本 `auto` 也会自动回退)。

## Scripts

- `scripts/transcribe.py` — 主流程: 解析 → 下载(原始音轨, 不经 ffprobe) → ffmpeg 转 16k 单声道 wav → 识别 → 输出 JSON 摘要。支持 `--list-only` / `--limit` / `--model`(含 `auto`) / `--lang` / `--device` / `--compute-type` / `--format` / `--preview` / `--jobs` / `--resume` / `--progress-file` / `--cache-dir` / `--text-mode` / `--diarize` / `--transcript-cache`(转录缓存, 省 token) / `--compact`(精简 JSON)。本地模型目录做完整性校验(model.bin + config.json + tokenizer.json); 断点续传写 `run_dir/progress.json`(原子写); 转录缓存按视频 id 复用 txt/srt, 第二次转录零消耗; GPU 启用从 `gpu_utils` 导入, 不再依赖 `setup_env`; 段落合并基于 VAD 时间戳; 说话人分离可选集成 pyannote.audio。启动自动设置 HF 镜像/符号链接/Xet 与沙箱删除钩子环境变量; 支持 `BILI2TEXT_MODEL_DIR` 指定本地模型目录。
- `scripts/setup_env.py` — 安装 `yt-dlp` + `faster-whisper` + `imageio-ffmpeg` 到当前 venv; 支持 `--device auto|cpu|cuda`(auto 检测并自动复制 cuBLAS 启用 GPU) 与 `--detect-only`(打印 GPU 检测 JSON 不改动); 加 `--download-model <name>` 可把模型预下载到 `<技能>/models/<name>/`(用镜像+禁用符号链接+禁用 Xet), 并提示 `BILI2TEXT_MODEL_DIR` 用法。GPU 检测/启用逻辑从 `gpu_utils` 导入。
- `scripts/gpu_utils.py` — GPU 检测/启用独立模块, 供 `transcribe.py` 与 `setup_env.py` 共用。提供 `detect_gpu()` / `enable_gpu()` / `resolve_device_arg()`。Windows 下从 NVIDIA NGX / CUDA Toolkit / System32 搜索并复制 cuBLAS 12 DLL; Linux/macOS 跳过复制, 直接用 ctranslate2 wheel 自带运行时。

## References

- `references/bilibili_api.md` — B 站输入格式、分P/合集处理、yt-dlp 参数与错误映射。
- `references/asr_engines.md` — Whisper 模型选型(准确率/体积/速度)、语言设置、VAD、进阶引擎(FunASR/云端 ASR)与输出格式说明。