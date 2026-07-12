#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GPU 检测与启用工具(独立模块, 供 setup_env.py 与 transcribe.py 共用)。

设计目的: 把 cuBLAS 检测/复制逻辑从 setup_env.py 解耦出来, 避免 transcribe.py
运行时隐式依赖 setup_env.py(后者还带有 pip 安装等副作用)。

核心功能:
  * detect_gpu()            —— 结构化检测 GPU 可用性(JSON 友好)
  * enable_gpu()            —— 从系统复制 cuBLAS 12 运行时到 ctranslate2 包目录
  * resolve_device_arg()    —— 根据 --device 参数 + 实际检测, 决定最终推理设备

跨平台说明:
  Windows 下 cuBLAS 以 .dll 形式存在(cublas64_12.dll 等), 本模块会从
  NVIDIA NGX / CUDA Toolkit / System32 / CUDA_PATH 等候选目录搜索并复制。
  Linux/macOS 下 cuBLAS 通常由 ldconfig / CUDA_PATH 统一管理, ctranslate2
  wheel 自带的运行时即可加载, 无需手动复制; 本模块在非 Windows 平台
  会跳过 DLL 复制, 仅做设备探测。
"""
from __future__ import annotations

import glob
import os
import shutil
import sys

# 启用 GPU 时, 需要从系统复制到 ctranslate2 包内的 CUDA 运行时文件(Windows)
CUDA_DLLS = ["cublas64_12.dll", "cublasLt64_12.dll", "cudart64_12.dll"]

# Linux 下对应的共享库名(用于探测系统是否已装 cuBLAS, 不做复制)
CUDA_SO = ["libcublas.so.12", "libcublasLt.so.12", "libcudart.so.12"]


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _nv_dll_search_roots() -> list:
    """返回可能含 cuBLAS 12 的候选根目录(Windows)。"""
    roots = []
    # 1. NVIDIA NGX 目录(驱动有时会顺带装 CUDA 运行时)
    ngx = "C:/ProgramData/NVIDIA/NGX"
    if os.path.isdir(ngx):
        roots.append(ngx)
    # 2. CUDA Toolkit
    toolkit = "C:/Program Files/NVIDIA GPU Computing Toolkit"
    if os.path.isdir(toolkit):
        roots.append(toolkit)
    # 3. 系统目录
    sysroot = os.environ.get("SystemRoot", "C:/Windows")
    roots.append(os.path.join(sysroot, "System32"))
    roots.append(os.path.join(sysroot, "SysWOW64"))
    # 4. CUDA_PATH 环境变量
    for k in ("CUDA_PATH", "CUDA_HOME"):
        v = os.environ.get(k)
        if v:
            roots.append(v)
            roots.append(os.path.join(v, "bin"))
    return roots


def _find_cublas_system() -> str | None:
    """在系统里找含 cublas64_12.dll 的目录(优先同时含另两个 DLL 的)。Windows 专用。"""
    roots = _nv_dll_search_roots()
    candidates = []  # (dir, score)
    for root in roots:
        if not os.path.isdir(root):
            continue
        # 限制递归深度, 避免全盘扫描卡死
        for base, _dirs, files in os.walk(root):
            depth = base[len(root):].count(os.sep)
            if depth > 6:
                continue
            if "cublas64_12.dll" in files:
                score = 3 if "cublasLt64_12.dll" in files else 1
                score += 1 if "cudart64_12.dll" in files else 0
                candidates.append((base, score))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]


def _ctranslate2_pkg_dir() -> str | None:
    try:
        import ctranslate2
        return os.path.dirname(ctranslate2.__file__)
    except Exception:
        return None


def detect_gpu(verbose: bool = True) -> dict:
    """检测 GPU 可用性。返回结构化信息字典(跨平台)。

    字段:
      cuda_device_count : int   ctranslate2 看到的 CUDA 设备数
      gpu_present       : bool  是否有可用 GPU
      cublas_in_pkg    : bool   ctranslate2 包内是否已含 cuBLAS(Windows)
      cublas_system     : str|None 系统里找到的含 cublas 的目录(Windows)
      cuda_ready       : bool  GPU 存在 且 包内/系统已有可加载的 cuBLAS
      reason           : str   人类可读说明
    """
    info = {
        "cuda_device_count": 0,
        "gpu_present": False,
        "cublas_in_pkg": False,
        "cublas_system": None,
        "cuda_ready": False,
        "reason": "",
    }
    try:
        import ctranslate2
        n = ctranslate2.get_cuda_device_count()
        info["cuda_device_count"] = n
        info["gpu_present"] = n > 0
    except Exception as e:  # ctranslate2 未装或 CUDA 探测失败
        info["reason"] = f"ctranslate2 CUDA 探测失败(可能尚未安装): {e}"
        return info

    # Windows: 检查 cuBLAS DLL 是否已就位
    if _is_windows():
        pkg = _ctranslate2_pkg_dir()
        if pkg and glob.glob(os.path.join(pkg, "cublas64_12.dll")):
            info["cublas_in_pkg"] = True
        info["cublas_system"] = _find_cublas_system()

        if not info["gpu_present"]:
            info["reason"] = "未检测到 NVIDIA GPU (cuda_device_count=0)。将使用 CPU。"
            return info

        if info["cublas_in_pkg"]:
            info["cuda_ready"] = True
            info["reason"] = "检测到 GPU, 且 ctranslate2 包内已含 cuBLAS, GPU 可直接使用。"
        elif info["cublas_system"]:
            info["cuda_ready"] = True
            info["reason"] = (
                f"检测到 GPU, 但 ctranslate2 包内缺 cuBLAS; "
                f"可从系统目录复制启用: {info['cublas_system']}"
            )
        else:
            info["reason"] = (
                "检测到 GPU, 但系统也找不到 cuBLAS 12 运行时 "
                "(cublas64_12.dll)。请安装 NVIDIA 驱动或 CUDA Toolkit 后重试。"
            )
    else:
        # Linux/macOS: ctranslate2 wheel 自带 CUDA 运行时, 能看到设备即视为就绪
        if not info["gpu_present"]:
            info["reason"] = "未检测到 NVIDIA GPU (cuda_device_count=0)。将使用 CPU。"
            return info
        info["cuda_ready"] = True
        info["reason"] = "检测到 GPU, ctranslate2 wheel 自带 CUDA 运行时, 可直接使用。"

    return info


def enable_gpu(verbose: bool = True) -> bool:
    """若 GPU 存在但 ctranslate2 包内缺 cuBLAS, 从系统复制所需 DLL(Windows)。

    成功返回 True (GPU 已就绪); 无 GPU 或无法复制则返回 False。
    Linux/macOS 下不执行复制, 直接按 detect_gpu() 的 cuda_ready 返回。
    """
    info = detect_gpu(verbose=False)
    if not info["gpu_present"]:
        if verbose:
            print("[信息] 未检测到 GPU, 跳过 GPU 启用 (将使用 CPU)。",
                  file=sys.stderr)
        return False

    # 非 Windows: 不需要复制 DLL, 直接返回 cuda_ready
    if not _is_windows():
        if verbose and info["cuda_ready"]:
            print("[信息] GPU 可用, 直接使用 ctranslate2 wheel 自带 CUDA 运行时。",
                  file=sys.stderr)
        return info["cuda_ready"]

    # Windows: 可能需要从系统复制 cuBLAS
    if info["cublas_in_pkg"]:
        if verbose:
            print("[信息] ctranslate2 包内已含 cuBLAS, GPU 已可直接使用。",
                  file=sys.stderr)
        return True

    src_dir = info["cublas_system"]
    pkg = _ctranslate2_pkg_dir()
    if not src_dir or not pkg:
        if verbose:
            print("[警告] 找不到系统 cuBLAS 源目录, 无法自动启用 GPU。",
                  file=sys.stderr)
        return False

    copied = []
    for dll in CUDA_DLLS:
        s = os.path.join(src_dir, dll)
        d = os.path.join(pkg, dll)
        if os.path.isfile(s) and not os.path.isfile(d):
            try:
                shutil.copyfile(s, d)
                copied.append(dll)
            except OSError as e:
                if verbose:
                    print(f"[警告] 复制 {dll} 失败: {e}", file=sys.stderr)
    if copied:
        if verbose:
            print(f"[成功] 已复制 cuBLAS 运行时到 ctranslate2 包目录, 启用 GPU: "
                  f"{', '.join(copied)}", file=sys.stderr)
        return True
    # 源目录没有我们要的全部 DLL(例如只找到 cublas 没有 cudart)
    if verbose:
        print("[警告] 系统 cuBLAS 源目录缺少必要 DLL, GPU 可能无法初始化。",
              file=sys.stderr)
    return bool(info["cublas_in_pkg"])


def resolve_device_arg(device_arg: str, verbose: bool = True) -> tuple:
    """根据 --device 参数 + 实际检测, 决定最终推理设备与说明。

    返回 (device, note)。
      device: "cuda" | "cpu"
    """
    if device_arg == "cpu":
        return "cpu", "已显式指定 CPU。"
    if device_arg == "cuda":
        ok = enable_gpu(verbose=verbose)
        if ok:
            return "cuda", "已显式指定 GPU, 并已尝试启用 cuBLAS。"
        return "cuda", ("已指定 GPU, 但无法自动启用 cuBLAS; "
                        "运行时若失败会自动回退 CPU。")
    # auto
    info = detect_gpu(verbose=verbose)
    if info["gpu_present"]:
        ok = enable_gpu(verbose=verbose)
        if ok:
            return "cuda", "自动检测到 GPU, 已启用。"
        return "cpu", "检测到 GPU 但无法启用 cuBLAS, 回退 CPU。"
    return "cpu", "自动检测: 无 GPU, 使用 CPU。"
