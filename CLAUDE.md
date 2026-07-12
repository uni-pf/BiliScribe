# BiliScribe — Claude Code 指令

BiliScribe 是一个视频语音转文字工具，基于 faster-whisper + yt-dlp，支持 B 站、YouTube 等多平台。

## 环境初始化

当你首次在此项目中被调用时，先检查环境是否就绪：

```bash
# 快速检查
python scripts/transcribe.py dummy --check-env
```

如果环境缺失，按以下步骤初始化：

```bash
# 创建 venv + 安装依赖
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 预下载模型（推荐 medium）
python scripts/setup_env.py --download-model medium
```

## 使用模式

### 标准转录（推荐）

**始终使用 `--claude` 模式**，它会输出结构化文字报告，包含统计摘要和内容预览，方便我直接阅读：

```bash
python scripts/transcribe.py "<BV号/链接>" --lang zh --claude
```

### 枚举合集

先列清单确认范围，再转录：

```bash
python scripts/transcribe.py "<合集链接>" --list-only --claude
python scripts/transcribe.py "<合集链接>" --limit 5 --lang zh --claude
```

### 关键参数

| 参数 | 何时使用 |
|------|---------|
| `--lang auto` | 英文/外语视频 |
| `--claude` | **始终开启**，输出结构化报告 |
| `--format both` | 需要 SRT 字幕时 |
| `--limit N` | 合集只处理前 N 集 |
| `--model auto` | 按时长自动选模型 |
| `--preview N` | 控制预览字数 |
| `--resume` | 长合集中断后继续 |

## 输出规范

转录完成后：
1. 输出目录在 `--out-dir` 指定的目录下（默认 `./bili_transcripts/`）
2. 用 `cat` 或 `present_files` 读取 `.txt` 文件
3. 如果用户要求总结，基于 `.txt` 内容做摘要

## 项目结构

```
BiliScribe/
├── CLAUDE.md              # ← 当前文件，Claude Code 指令
├── SKILL.md               # WorkBuddy 技能入口
├── scripts/
│   ├── transcribe.py     # 主程序
│   ├── setup_env.py      # 环境安装
│   └── gpu_utils.py      # GPU 检测
├── references/            # 参考文档
├── requirements.txt
└── README.md
```
