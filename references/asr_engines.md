# 语音识别(ASR)引擎说明

本技能默认使用 **faster-whisper**(OpenAI Whisper 的 CTranslate2 加速实现), 离线、免费、无需 API Key, 中文支持良好。

## 1. 模型选择: 准确率 vs 体积 vs 速度

`--model` 参数控制模型规模。首次使用会从 HuggingFace 下载(仅一次)。

| 模型 | 约下载体积 | 中文准确率 | CPU(int8) 速度参考 | 适用场景 |
|------|-----------|-----------|-------------------|---------|
| `tiny` | ~75 MB | 低 | 很快 | 仅想快速看个大意 |
| `base` | ~140 MB | 中低 | 快 | 英文尚可, 中文不推荐 |
| `small` | ~460 MB | 中等 | 中等 | 平衡之选 |
| `medium` | ~1.5 GB | **良好(默认)** | 较慢 | 多数中文视频推荐 |
| `large-v3` | ~3 GB | **最佳** | 慢 | 对准确率要求高、专有名词多 |

> 默认 `medium`: 在准确率与体积间取得较好平衡。若内容含大量术语/人名/英文混读, 改用 `large-v3`。
> 设备默认 `--device auto --compute-type auto`: 运行时自动检测 NVIDIA GPU —— 有则用 `cuda`/`float16`(显著加速, 实测整体可达 CPU 数倍), 无则回退 `cpu`/`int8`。`setup_env.py --device auto` 会在检测到 GPU 时自动从系统复制 cuBLAS 12 运行时启用 GPU, 无需手动装 CUDA Toolkit。也可显式 `--device cuda --compute-type float16` 或 `--device cpu --compute-type int8`。

## 1.1 模型下载与加载(离线/沙箱环境必看)

`faster-whisper` 首次加载模型会从 HuggingFace 拉取权重。在沙箱/镜像/离线环境中, 直接下载常踩以下坑(脚本 `transcribe.py` 已自动规避, 预下载时 `setup_env.py` 也强制设置):

| 现象 | 原因 | 解决 |
|------|------|------|
| 快照目录为空 / 报 `Unable to open file 'model.bin'` | 沙箱内符号链接 checkout 失败, 权重没链进快照目录 | 设 `HF_HUB_DISABLE_SYMLINKS=1`, 改用复制 |
| `CAS Client Error 401` | 461MB 的 `model.bin` 走了 Xet/CAS 大文件传输, 镜像不支持/鉴权失败 | 设 `HF_HUB_DISABLE_XET=1`, 改走普通 HTTPS |
| 下载极慢 / 连接超时 | 直连 HuggingFace 被墙或慢 | 设 `HF_ENDPOINT=https://hf-mirror.com` 走镜像 |
| 清理临时文件抛 `OSError` | 沙箱 `safe-delete` 钩子在回收站不可用时强行报错 | 设 `CODEBUDDY_SAFE_DELETE_SANDBOX=0` |

**推荐做法(最稳)**: 用 `setup_env.py --download-model <name>` 一次性把模型落地到 `<技能目录>/models/<name>/`(含 `model.bin` + 配置), 之后每次运行转写前 `export BILI2TEXT_MODEL_DIR="<技能目录>/models/<name>"`, 脚本会直接加载本地目录, **完全跳过 HF 缓存与运行时下载**。

## 2. 语言设置

- `--lang zh`: 强制中文, 最快最准(已知是中文视频时首选)。
- `--lang auto`: 交给模型自动检测(适合多语种/外语视频), 略慢。
- 输出 JSON 中的 `language` 字段会回显实际识别语言。

> **默认值提醒**: `--lang` 默认 `zh`(针对 B 站中文内容优化)。处理**非中文 / 外语**视频请改用 `--lang auto`, 否则会被强制按中文识别, 准确率显著下降。

## 3. VAD(语音活动检测)

脚本默认开启 `vad_filter=True`, 自动剔除静音段, 减少幻觉、提升分段质量。`min_silence_duration_ms=500` 控制分句灵敏度, 如需更长句子可上调。

## 4. 进阶/替代引擎(按需扩展)

若对中文准确率有极致要求, 可替换为以下后端(需自行改造 `transcribe.py` 的 `transcribe_file`):

- **FunASR (ModelScope Paraformer-large / SenseVoice)**: 阿里达摩院, 中文/方言/说话人分离表现优异, 离线免费。
- **云端 ASR**: 百度/阿里云/腾讯云/讯飞 一句话转写 API, 准确率最高, 但需 API Key 且按量计费, 适合生产级长音频。
- **whisperX**: 在 Whisper 基础上做词级时间轴对齐, 需更精细字幕时选用。

> 替换时保持 `transcribe_file()` 的接口约定: 输入音频路径 + 语言, 输出 `[(start, end, text), ...]`, 以保证 `write_outputs()` 与 JSON 摘要不变。

### 说话人分离(`--diarize`, 已内置)

本技能已集成 `pyannote.audio` 的 `speaker-diarization-3.1` 模型, 用法:

1. `pip install pyannote.audio`(可选依赖, 不装不影响普通转录)
2. 访问 https://huggingface.co/pyannote/speaker-diarization-3.1 接受模型使用协议
3. 在 https://huggingface.co/settings/tokens 创建 access token
4. 设环境变量 `HF_TOKEN=<你的token>`
5. 运行时加 `--diarize` 开关

未安装/未配置时, `--diarize` 会自动降级为普通转录并提示原因, 不会中断流程。

## 5. 输出格式

- **纯文本 (.txt)**: `--text-mode merged`(默认)按 VAD 段间静音间隔做语义分段, 段间空行, 可读性高; `--text-mode raw` 逐段一行行拼接(旧行为), 适合检索/二次处理。
- **字幕 (.srt)**: 标准 SubRip, 含序号 + `HH:MM:SS,mmm --> HH:MM:SS,mmm` 时间轴, 可直接导入剪辑软件或字幕工具。
- `--format both`(默认) 同时产出两者; 也可只取其一以省磁盘。
- **段落合并规则**(`merged` 模式): gap < 1.5s 紧密拼接; 1.5-3s 同段加句号分隔; >= 3s 新段落(空行)。基于 faster-whisper 段时间戳, 无需额外模型。
- **说话人标签**(`--diarize`): 开启后 txt 段落开头与 srt 每条文本前加 `[SPEAKER_00]` 等标签。基于 `pyannote.audio`(可选依赖), 未安装时降级为普通转录。

## 6. 模型自动选择(`--model auto`)

按时长自动选模型, 无需手动权衡:

| 系列总时长 | 自动选择 | 理由 |
|-----------|---------|------|
| < 10 分钟 | `large-v3` | 短视频耗时可接受, 精度优先 |
| 10-60 分钟 | `medium` | 默认平衡之选 |
| > 60 分钟 | `small` | 长合集避免太久, 速度优先 |
| 拿不到时长 | `medium` | 安全回退 |

> 拿不到时长(list 返回 duration 为空)时回退 `medium`。auto 解析后 JSON 摘要的 `model` 字段会回显实际选中的模型名。
