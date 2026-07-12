# 🎙️ BiliScribe

**把 B 站（以及 yt-dlp 支持的任何平台）视频语音转为文字。**  
免费、离线、本地运行——你的视频内容你做主。

---

## ✨ 特性

- **🎯 精准转录**：基于 faster-whisper（medium 模型），中文识别准确率优秀
- **📦 本地运行**：无需 API Key，无需上传，隐私安全
- **📺 B 站原生支持**：BV 号 / av 号 / 完整链接 / b23.tv 短链，开箱即用
- **📚 合集批量**：自动枚举分P/合集/收藏夹，`--limit` 控制数量，`--resume` 断点续传
- **⚡ 智能加速**：自动检测 GPU（CUDA），自动复制 cuBLAS 运行时，无需手动装 CUDA Toolkit
- **🗂️ 多种输出**：纯文本 (.txt) + 字幕 (.srt)，带时间轴，支持说话人分离
- **🔁 缓存复用**：音频缓存 + 转录缓存，同一视频重复转录零消耗
- **🧩 跨平台**：B 站、YouTube、Twitter、抖音……yt-dlp 支持的所有平台

## 🚀 快速开始

### 安装

```bash
# 1. 创建虚拟环境（推荐）
python -m venv venv
source venv/bin/activate      # Linux/macOS
venv\Scripts\activate          # Windows

# 2. 安装依赖
pip install -r requirements.txt

# 3. （可选）预下载 whisper 模型
python scripts/setup_env.py --download-model medium
```

### 使用

```bash
# 单视频转文字
python scripts/transcribe.py "BV1xx411c7mD" --lang zh

# 只列清单不下载
python scripts/transcribe.py "https://www.bilibili.com/video/BV1xx" --list-only

# 合集批量（前 10 集）
python scripts/transcribe.py "BV1xx" --limit 10 --lang zh

# 转 SRT 字幕
python scripts/transcribe.py "BV1xx" --lang zh --format both

# 断点续传
python scripts/transcribe.py "BV1xx" --resume --lang zh

# 环境检查
python scripts/transcribe.py dummy --check-env
```

### 模型选择

| 模型 | 磁盘 | 速度 | 中文准确率 | 推荐场景 |
|------|------|------|-----------|---------|
| `small` | 460 MB | 快 | ⭐⭐⭐ | 长合集/速度优先 |
| `medium` | 1.5 GB | 中 | ⭐⭐⭐⭐ | 默认，多数视频推荐 |
| `large-v3` | 3 GB | 慢 | ⭐⭐⭐⭐⭐ | 术语多/精度要求高 |
| `auto` | — | 按时长自动选 | 平衡 | 省心模式 |

## 📖 完整用法

```
usage: transcribe.py [-h] [--out-dir OUT_DIR] [--list-only] [--limit LIMIT]
                     [--model MODEL] [--lang LANG] [--device DEVICE]
                     [--compute-type COMPUTE_TYPE] [--format FORMAT]
                     [--preview PREVIEW] [--prune-runs PRUNE_RUNS]
                     [--resume] [--progress-file PROGRESS_FILE] [--jobs JOBS]
                     [--cache-dir CACHE_DIR]
                     [--transcript-cache TRANSCRIPT_CACHE] [--compact]
                     [--text-mode {merged,raw}] [--diarize] [--check-env]
                     [--min-silence-duration-ms MIN_SILENCE_DURATION_MS]
                     input
```

### 主要参数

| 参数 | 说明 |
|------|------|
| `--model` | 模型: `tiny/base/small/medium/large-v3` / `auto`（按时长自动选） |
| `--lang` | 语言: `zh`（默认） / `auto`（自动检测） |
| `--device` | 设备: `auto`（默认）/ `cpu` / `cuda` |
| `--format` | 输出: `txt`（默认）/ `srt` / `both` |
| `--limit N` | 最多处理前 N 个视频 |
| `--resume` | 断点续传 |
| `--jobs N` | 并发数（GPU 建议 1，CPU 可 2-4） |
| `--diarize` | 说话人分离（需额外配置） |
| `--check-env` | 环境一键检查 |
| `--min-silence-duration-ms` | VAD 分段灵敏度，默认 500ms |

## 🔧 进阶

### GPU 加速

BiliScribe 自动检测 NVIDIA GPU。如果检测到 GPU 但缺 cuBLAS 运行时，会自动从系统目录复制 DLL 启用加速，无需手动安装 CUDA Toolkit。

```bash
# 查看 GPU 状态
python scripts/transcribe.py dummy --check-env
```

### 说话人分离

```bash
# 安装额外依赖
pip install pyannote.audio

# 在 HuggingFace 接受协议后运行
HF_TOKEN="your_token" python scripts/transcribe.py "BV1xx" --diarize
```

### 环境变量

| 变量 | 作用 |
|------|------|
| `BILI2TEXT_MODEL_DIR` | 指定本地模型目录路径 |
| `HF_ENDPOINT` | HuggingFace 镜像地址（国内：`https://hf-mirror.com`） |
| `HF_TOKEN` | HuggingFace 访问令牌（说话人分离需要） |

## 📁 项目结构

```
BiliScribe/
├── scripts/
│   ├── transcribe.py    # 主程序
│   ├── setup_env.py     # 环境安装工具
│   └── gpu_utils.py     # GPU 检测与启用
├── references/
│   ├── asr_engines.md   # ASR 引擎说明
│   └── bilibili_api.md  # B 站输入格式参考
├── requirements.txt
├── README.md
└── LICENSE
```

## ⚠️ 常见问题

| 问题 | 解决 |
|------|------|
| 视频不存在 | 检查 BV 号或链接是否正确 |
| 下载超时 | 网络问题，稍后重试 |
| 转录很慢 | 加 `--device cuda` 用 GPU；或换 `--model small` |
| 磁盘空间不足 | 清理 `bili_transcripts/` 目录；调 `--prune-runs` |
| 模型下载失败 | 设置 `HF_ENDPOINT=https://hf-mirror.com` 走镜像 |

## 📜 许可证

[MIT License](LICENSE)
