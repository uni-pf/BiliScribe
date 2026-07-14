#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路径发现工具 — 向 agent 输出结构中化的运行环境信息。

调用方式:
    <解释器> scripts/find_paths.py

输出(JSON):
    {
        "ok": true/false,              // 整体是否就绪
        "bili_home": "...",            // BILI_HOME 解析结果
        "venv_python": "...",
        "venv_exists": true/false,
        "models": {                    // 已下载的模型列表
            "medium": {...},
            "small": {...},
            ...
        },
        "active_model": "medium",      // 默认使用的模型
        "cache_dir": "...",
        "out_dir": "...",
        "dependencies": {              // 依赖检查
            "yt_dlp": true/false,
            "faster_whisper": true/false,
            "imageio_ffmpeg": true/false
        },
        "gpu": {                       // GPU 检测
            "available": true/false,
            "device_count": 0,
            "device_type": "cuda"/"cpu"
        },
        "issues": ["..."]              // 需要处理的问题列表(无 GPU、缺模型等)
        "notes": ["..."]               // 正面附加信息(GPU 可用等)
    }
"""

from __future__ import annotations

import json
import os
import sys

# 确保能找到同目录的 bili_paths
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from bili_paths import get_bili_home, get_models_dir, get_cache_dir, get_transcripts_dir


def _venv_python() -> str | None:
    """当前解释器是否在 venv 中？返回其路径。"""
    if hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix
    ):
        # 在 venv 内
        if sys.platform.startswith("win"):
            return os.path.join(sys.prefix, "Scripts", "python.exe")
        return os.path.join(sys.prefix, "bin", "python")
    return None


def _check_models() -> dict:
    """扫描模型目录, 返回 {name: {exists, size_mb, complete}}"""
    models_dir = get_models_dir()
    if not os.path.isdir(models_dir):
        return {}
    result = {}
    for name in os.listdir(models_dir):
        model_dir = os.path.join(models_dir, name)
        if not os.path.isdir(model_dir):
            continue
        required = ["model.bin", "config.json", "tokenizer.json"]
        missing = [f for f in required if not os.path.isfile(os.path.join(model_dir, f))]
        bin_path = os.path.join(model_dir, "model.bin")
        size_mb = round(os.path.getsize(bin_path) / (1024 * 1024), 1) if os.path.isfile(bin_path) else 0
        result[name] = {
            "exists": len(missing) == 0,
            "size_mb": size_mb,
            "complete": len(missing) == 0,
            "missing_files": missing if missing else [],
            "path": model_dir,
        }
    return result


def _check_deps() -> dict:
    deps = {}
    for mod_name, import_name in [
        ("yt_dlp", "yt_dlp"),
        ("faster_whisper", "faster_whisper"),
        ("imageio_ffmpeg", "imageio_ffmpeg"),
    ]:
        try:
            __import__(import_name)
            deps[mod_name] = True
        except ImportError:
            deps[mod_name] = False
    return deps


def _check_gpu() -> dict:
    info = {"available": False, "device_count": 0, "device_type": "cpu"}
    try:
        from gpu_utils import detect_gpu, resolve_device_arg
        gpu_info = detect_gpu(verbose=False)
        info["available"] = gpu_info.get("cuda_ready", False)
        info["device_count"] = gpu_info.get("cuda_device_count", 0)
        dev, _note = resolve_device_arg("auto", verbose=False)
        info["device_type"] = dev
    except Exception:
        pass
    return info


def _collect_issues(result: dict) -> tuple[list[str], list[str]]:
    """返回 (issues, notes)。issues 是需要用户关注的问题, notes 是正面的附加信息。"""
    issues = []
    notes = []
    # Python 版本
    py_major, py_minor = sys.version_info[:2]
    if py_major < 3 or (py_major == 3 and py_minor < 8):
        issues.append(f"Python {py_major}.{py_minor} 版本过低, 建议 >= 3.8")

    # venv
    if not result.get("venv_python"):
        issues.append("当前不在虚拟环境中运行, 建议使用 D 盘持久化 venv")

    # 模型
    active = result.get("active_model")
    models = result.get("models", {})
    if active and active in models:
        if not models[active]["complete"]:
            issues.append(
                f"模型 {active} 不完整, 缺少: {', '.join(models[active]['missing_files'])}"
            )
    elif not active:
        issues.append("未找到可用模型, 需要先 setup_env.py --download-model")

    # 依赖
    deps = result.get("dependencies", {})
    missing_deps = [k for k, v in deps.items() if not v]
    if missing_deps:
        issues.append(f"缺少依赖: {', '.join(missing_deps)}, 需运行 setup_env.py")

    # GPU
    gpu = result.get("gpu", {})
    if gpu.get("available"):
        notes.append("GPU 可用")  # 正面信息, 放入 notes
    elif gpu.get("device_count", 0) > 0:
        issues.append("检测到 GPU 但 cuBLAS 未就绪, 转录可能偏慢")
    else:
        issues.append("无 GPU, 使用 CPU 推理 (会比 GPU 慢 3-5 倍)")

    return issues, notes


def main() -> None:
    base = get_bili_home()
    models = _check_models()
    deps = _check_deps()
    gpu = _check_gpu()

    # 选择活动模型: 优先 medium → small → 第一个可用的
    active_model = None
    for preferred in ("medium", "small", "large-v3", "base", "tiny"):
        if preferred in models and models[preferred]["complete"]:
            active_model = preferred
            break
    if not active_model and models:
        active_model = list(models.keys())[0]

    result = {
        "ok": True,
        "bili_home": base,
        "venv_python": _venv_python(),
        "venv_exists": _venv_python() is not None and os.path.isfile(_venv_python() or ""),
        "current_python": sys.executable,
        "models": models,
        "active_model": active_model,
        "cache_dir": get_cache_dir(),
        "out_dir": get_transcripts_dir(),
        "dependencies": deps,
        "gpu": gpu,
    }

    issues, notes = _collect_issues(result)
    result["issues"] = issues
    result["notes"] = notes
    result["ok"] = (
        result["active_model"] is not None
        and all(deps.values())
        and result["venv_exists"]
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
