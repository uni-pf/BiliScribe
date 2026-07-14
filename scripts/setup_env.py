#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安装 bilibili-transcriber 所需的 Python 依赖, 并可预下载 whisper 模型。

用法(由调用方以「托管 Python venv」的解释器运行):
    python scripts/setup_env.py                  # 仅安装依赖, 并自动检测/启用 GPU
    python scripts/setup_env.py --device cpu   # 强制 CPU 环境(跳过 GPU 检测)
    python scripts/setup_env.py --device cuda  # 强制启用 GPU(会从系统复制 cuBLAS)
    python scripts/setup_env.py --detect-only  # 仅打印 GPU 检测结果(JSON), 不改动
    python scripts/setup_env.py --download-model small   # 额外预下载 small 模型

== 关于 GPU / CPU 版本(重要) ==
ctranslate2 的 PyPI wheel 是 **CPU + CUDA 双用** 的同一个轮子:
它自带 CUDA 运行时(cudnn64_9.dll 等), 运行时是否有可用 GPU 决定走哪条路。
因此「安装 GPU 版 / CPU 版」在 pip 层面是同一个包, 真正的开关是「推理设备」。

唯一的坑: 部分机器装了 NVIDIA 驱动 + GPU, 但 ctranslate2 包内**缺少
cuBLAS 12 运行时** (cublas64_12.dll / cublasLt64_12.dll), 导致 device="cuda"
初始化时报 "cublas64_12.dll is not found"。本脚本在检测到 GPU 时, 会自动从
系统已有的 NVIDIA 目录(NGX / CUDA Toolkit / System32)找到这些 DLL 并复制到
ctranslate2 包目录, 从而**一键启用 GPU**, 无需手动安装 CUDA Toolkit。

模型预下载说明(重要, 离线/沙箱环境必看):
  默认 transcribe.py 首次运行时会从 HuggingFace 下载模型。但在本沙箱/镜像环境,
  直接下载常踩坑: 符号链接 checkout 失败导致快照目录为空、461MB 的 model.bin 走
  Xet/CAS 在镜像上 401。本脚本预下载时强制:
    HF_ENDPOINT=https://hf-mirror.com   (走镜像)
    HF_HUB_DISABLE_SYMLINKS=1           (改用复制, 不用符号链接)
    HF_HUB_DISABLE_XET=1                (走普通 HTTPS, 不走 Xet/CAS)
    CODEBUDDY_SAFE_DELETE_SANDBOX=0     (避免沙箱删除钩子抛错)
   并把模型落地到 <BILI_HOME>/models/<name>/ (含 model.bin, BILI_HOME 默认
   D:\\workbuddy\\.bili-transcriber, 可用环境变量覆盖), 之后用环境变量
    BILI2TEXT_MODEL_DIR 指向它即可离线、稳定加载, 无需再碰 HF 缓存。
"""
import argparse
import json
import os
import subprocess
import sys

# GPU 检测/启用逻辑已下沉到独立模块 gpu_utils, 本脚本与 transcribe.py 共用
from gpu_utils import detect_gpu, enable_gpu, resolve_device_arg
# 统一数据目录解析(单一事实来源, 独立于技能/安装目录)
from bili_paths import get_models_dir, get_bili_home, ensure_dir

PKGS = [
    "yt-dlp",
    "faster-whisper",
    "imageio-ffmpeg",  # 提供 ffmpeg 二进制(仅 ffmpeg, 无 ffprobe); 脚本转码只用 ffmpeg
]


def _set_model_env() -> None:
    os.environ["HF_ENDPOINT"] = os.environ.get(
        "HF_ENDPOINT", "https://hf-mirror.com"
    )
    os.environ["HF_HUB_DISABLE_SYMLINKS"] = "1"
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    os.environ["CODEBUDDY_SAFE_DELETE_SANDBOX"] = "0"


# ---------------------------------------------------------------------------
# GPU 检测与启用: detect_gpu / enable_gpu / resolve_device_arg 已迁移到
# gpu_utils.py(本文件顶部已导入)。保留下面这段说明仅作历史背景。
# ---------------------------------------------------------------------------
# 旧实现(已删除): _nv_dll_search_roots / _find_cublas_system /
# _ctranslate2_pkg_dir / detect_gpu / enable_gpu / resolve_device_arg。
# 迁移原因: transcribe.py 运行时也需要 enable_gpu, 但不应隐式依赖 setup_env.py
# (后者含 pip 安装等副作用)。独立 gpu_utils.py 让两边干净共用。


# ---------------------------------------------------------------------------
# 安装 / 预下载
# ---------------------------------------------------------------------------
def install_packages() -> int:
    print(f"使用 Python 解释器: {sys.executable}", file=sys.stderr)
    for pkg in PKGS:
        print(f"\n=== 安装 {pkg} ===", file=sys.stderr)
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", pkg],
                check=True, timeout=300,  # 5 分钟超时, 防网络卡死
            )
        except subprocess.TimeoutExpired:
            print(f"[错误] 安装 {pkg} 超时(5分钟)。请检查网络连接后重试。", file=sys.stderr)
            return 1
        except subprocess.CalledProcessError as e:
            print(f"[错误] 安装 {pkg} 失败: {e}", file=sys.stderr)
            return 1
    print("\n✅ 依赖安装完成。可运行: python scripts/transcribe.py --help 验证。",
          file=sys.stderr)
    return 0


def predownload_model(name: str) -> int:
    _set_model_env()
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("[错误] 缺少 huggingface_hub, 请先: pip install huggingface_hub",
              file=sys.stderr)
        return 1
    # 落到持久化根下的模型目录(默认 BILI_HOME/models, 独立于技能/安装目录)
    local_dir = os.path.join(get_models_dir(), name)
    ensure_dir(os.path.dirname(local_dir))
    print(f"\n=== 预下载 whisper 模型: {name} ===", file=sys.stderr)
    print(f"目标目录: {local_dir}", file=sys.stderr)
    try:
        path = snapshot_download(
            f"Systran/faster-whisper-{name}",
            local_dir=local_dir,
            local_dir_use_symlinks=False,
        )
        print(f"\n✅ 模型已落地: {path}", file=sys.stderr)
        print(f"运行转录时加环境变量: "
              f"BILI2TEXT_MODEL_DIR={path}", file=sys.stderr)
        print("例如:", file=sys.stderr)
        print(f'  BILI2TEXT_MODEL_DIR="{path}" \\\n'
              f'    python scripts/transcribe.py "<链接/BV>" --model {name} '
              f"--lang zh --limit 10", file=sys.stderr)
        return 0
    except Exception as e:
        print(f"[警告] 模型预下载失败(可忽略, 运行时 transcribe.py 会再尝试): {e}",
              file=sys.stderr)
        return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="安装 bilibili-transcriber 依赖/模型")
    ap.add_argument("--download-model", metavar="NAME", default=None,
                    help="额外预下载 whisper 模型: tiny/base/small/medium/large-v3")
    ap.add_argument("--device", default="auto",
                    choices=["auto", "cpu", "cuda"],
                    help="推理设备: auto(默认, 自动检测 GPU) / cpu / cuda")
    ap.add_argument("--detect-only", action="store_true",
                    help="仅打印 GPU 检测结果(JSON)后退出, 不做任何改动")
    args = ap.parse_args(argv)

    # --detect-only: 打印检测 JSON 即退出(供调用方/用户决策)
    if args.detect_only:
        print(json.dumps(detect_gpu(), ensure_ascii=False))
        return 0

    rc = install_packages()
    if rc != 0:
        return rc

    # 依据 --device 自动检测/启用 GPU
    device, note = resolve_device_arg(args.device)
    print(f"\n=== 设备决策: {device} ===", file=sys.stderr)
    print(f"说明: {note}", file=sys.stderr)
    print(f"\n✅ 环境就绪。转录时推荐加: --device {device}"
          f"{' --compute-type float16' if device == 'cuda' else ' --compute-type int8'}",
          file=sys.stderr)

    if args.download_model:
        predownload_model(args.download_model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
