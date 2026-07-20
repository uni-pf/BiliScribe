# B 站输入格式与 yt-dlp 要点

本技能通过 `yt-dlp` 拉取音轨, 因此它天然支持 B 站所有常见入口, 也兼容 yt-dlp 支持的千余个平台(YouTube、抖音等)。

> ⚠️ **2026-07 实战发现**: yt-dlp 的 Bilibili extractor 在中国大陆部分网络环境下
> 频繁出现 `WinError 10054`（远程主机强迫关闭连接）和超时。脚本已内置 **直连 fallback**
> (`scripts/bili_direct.py`)，在 yt-dlp 失败时自动降级到模拟浏览器请求 + B站官方 API，
> 无需手动干预。详见下文第 6 节。

## 1. 支持的输入形式

| 用户输入 | 示例 | 脚本处理 |
|---------|------|---------|
| 完整视频页链接 | `https://www.bilibili.com/video/BV1xx411c7mD` | 原样传给 yt-dlp |
| 带分P参数 | `.../BVxxxx?p=3` | yt-dlp 自动定位到第 3P(视 `--yes-playlist` 行为) |
| BV 号 | `BV1xx411c7mD` | 自动补全为 `https://www.bilibili.com/video/BV1xx411c7mD` |
| av 号 | `av170001` | 自动补全为 `https://www.bilibili.com/video/av170001` |
| 短链 | `https://b23.tv/xxxxxx` | yt-dlp 自动 302 跳转到真实 BV 页 |

> 解析逻辑见 `scripts/transcribe.py` 的 `to_url()`。无法识别的输入会返回友好错误。

## 2. 系列 / 分P / 合集

- **分P 视频**(一个 BV 内含多集): yt-dlp 会把它当作一个 playlist, 默认下载全部分P。脚本已加 `--yes-playlist`, 并用 `%(playlist_index)02d_` 前缀区分各集。
- **合集 / 收藏夹 / 频道列表**: 直接把对应 URL 传给脚本即可, yt-dlp 会枚举全部条目。
- **防溢出**: 长合集可能几十上百集。务必先用 `--list-only` 查看清单, 再用 `--limit N` 限制处理数量, 避免一次性下载/转录灌爆上下文与磁盘。

### 推荐工作流(省 token)
1. `python scripts/transcribe.py "<链接/BV>" --list-only`
   → 仅返回 JSON 清单 `{count, videos:[{index,id,title,duration}]}`, 不下载、不转录。
2. 与用户确认要处理的范围(全部 / 前 N 集 / 指定分P)。
3. 正式运行(必要时加 `--limit N`)。

## 3. 下载参数说明

脚本使用的核心 yt-dlp 参数:
- `-f bestaudio/best`: 只取最佳音轨的**原始封装**(m4a / opus / webm), **不**加 `-x --audio-format` 后处理。
- 为什么不做后处理: `imageio-ffmpeg` 只提供 `ffmpeg`, 没有 `ffprobe`, 而 yt-dlp 的 `FFmpegExtractAudio` 后处理需要 ffprobe, 会报 `ffprobe and ffmpeg not found`。
- 转码交给脚本: 下载原始音轨后, 脚本用内置的 `ffmpeg` 显式转成 **16k 单声道 wav**(`-ar 16000 -ac 1 -vn`), 全程只依赖 ffmpeg、不碰 ffprobe。faster-whisper 对 wav 兼容性最好。
- `--restrict-filenames`: 文件名仅保留安全字符, 规避 Windows 非法字符问题(中文标题仍保留)。
- 输出模板: `%(playlist_index)02d_%(id)s.%(ext)s`, 以视频 id 命名, 稳定且可溯源。

> 若 `convert_to_wav()` 因故失败, 脚本会回退为直接用原始音轨(m4a/opus/webm)喂给 faster-whisper(PyAV 通常也能解), 不会整体中断。

## 4. 常见错误与含义(脚本已做映射)

| yt-dlp 报错特征 | 中文提示 |
|----------------|---------|
| `Video unavailable` / `此视频` | 视频不存在或已下架/设为不可用 |
| `This video is private` | 私密视频, 无法访问 |
| `HTTP Error 404` | 页面不存在, 链接/BV 号有误 |
| `Sign in to watch` / 会员 | 需登录或仅会员可见 |
| `copyright` / 版权 | 版权限制无法下载 |
| `timed out` / `connection` | 网络失败或超时 |

调用方(agent)拿到 `{"ok": false, "error": "..."}` 后, 应把 `error` 原文转述给用户, 不要自行猜测。

## 5. yt-dlp Bilibili API 限流问题与直连 fallback

### 问题现象

中国大陆部分网络环境下, yt-dlp 的 Bilibili extractor 会频繁报错:

```
ERROR: [BiliBili] 1yjz5BLEoY: Unable to download webpage:
[WinError 10054] 远程主机强迫关闭了一个现有的连接
```

或: `The read operation timed out`

### 解决方案: bili_direct.py

脚本现已内置 `bili_direct.py` 模块, 在 yt-dlp 失败时**自动降级**到直连模式:

```
yt-dlp 失败 → 判断是否为 B站链接 → 尝试 bili_direct 直连
  ├── --list-only: 用 httpx 抓取页面 HTML, 提取 __INITIAL_STATE__ 
  │                获取完整分P列表(含 title / cid / duration)
  └── 下载音轨:   用 cid 请求 playurl API, 获取音频流地址并下载
```

**自动降级, 无需手动干预**。agent 只会看到正常流程, 唯一变化是 stderr 多一条
`尝试 B站直连模式` 的日志。

### 手动调用 (agent 备用)

```bash
# 枚举系列 (替代 --list-only)
$VENV_PYTHON scripts/bili_direct.py "BV1yjz5BLEoY" --list-only

# 下载单集音频 (替代 yt-dlp 下载)
$VENV_PYTHON scripts/bili_direct.py "https://www.bilibili.com/video/BV1yjz5BLEoY?p=19" <输出目录>
```

### 局限

- 仅支持 Bilibili (不是通用方案, yt-dlp 覆盖 1000+ 平台)
- 依赖 `httpx` (faster-whisper 的依赖链已包含, 无需额外安装)
- 下载的音频为 `.m4s` 格式 (B站 DASH 分段格式), 需 ffmpeg 转码为 wav

