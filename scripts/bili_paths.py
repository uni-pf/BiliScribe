#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bilibili-transcriber 统一数据目录解析(单一事实来源)。

设计目标: 把所有"重资产"(模型、音频缓存、转录产物)统一解析到一个
【独立于技能目录与安装目录】的持久位置, 避免技能从市场更新时被整体
覆盖导致数据丢失。venv 由调用方创建/指定, 不在本模块管理。

解析优先级(get_bili_home):
  1. 环境变量 BILI_HOME(显式覆盖, 最高优先)
  2. D:\\workbuddy\\.bili-transcriber(已推荐/已存在的持久化路径)
  3. 若 D: 盘存在则自动创建上述路径
  4. 回退到用户配置目录: ~/.bili-transcriber(跨机器/无 D 盘时)

各子目录:
  <home>/models       whisper 模型(medium 等, 体积大)
  <home>/cache        音频缓存(.wav, 按视频 id 复用)
  <home>/transcripts  默认转录输出目录
"""
from __future__ import annotations

import os

# 推荐的持久化根(独立于技能目录与安装目录)
PREFERRED_HOME = "D:/workbuddy/.bili-transcriber"


def get_bili_home() -> str:
    """解析持久化数据根目录(单一事实来源)。

    兼容旧文档中使用的 BILI_BASE 环境变量(同义)。
    """
    env = (os.environ.get("BILI_HOME", "").strip()
           or os.environ.get("BILI_BASE", "").strip())
    if env:
        return env

    # 已存在则直接复用(含完整 venv/模型/缓存/产物)
    if os.path.isdir(PREFERRED_HOME):
        return PREFERRED_HOME

    # D: 盘存在 -> 自动创建持久化根
    if os.path.isdir("D:/"):
        try:
            os.makedirs(PREFERRED_HOME, exist_ok=True)
            return PREFERRED_HOME
        except OSError:
            pass

    # 回退: 用户配置目录(与安装目录/技能目录无关)
    return os.path.expanduser("~/.bili-transcriber")


def get_models_dir() -> str:
    return os.path.join(get_bili_home(), "models")


def get_cache_dir() -> str:
    return os.path.join(get_bili_home(), "cache")


def get_transcripts_dir() -> str:
    return os.path.join(get_bili_home(), "transcripts")


def ensure_dir(path: str) -> str:
    """健壮地创建目录; 权限/初始化失败时抛出带中文说明的异常。

    返回规范化的绝对路径。
    """
    path = os.path.abspath(path)
    try:
        os.makedirs(path, exist_ok=True)
    except PermissionError as e:
        raise PermissionError(
            f"无权限创建目录: {path}。请检查该路径的写入权限, "
            f"或设置环境变量 BILI_HOME 指向可写路径。"
        ) from e
    except OSError as e:
        raise OSError(
            f"无法创建目录: {path} ({e})。可设置环境变量 BILI_HOME 指向可写路径。"
        ) from e
    return path


def legacy_skill_models_dir() -> str:
    """兼容旧版: 技能目录内的 models/(更新时可能被覆盖, 仅作回退探测)。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "models")
