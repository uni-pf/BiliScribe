#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bili_direct — B站直连模块，作为 yt-dlp Bilibili extractor 的备用方案。

实战背景（2026-07-20）：
  yt-dlp 的 Bilibili extractor 在部分网络环境下频繁超时/连接重置
  （WinError 10054 / The read operation timed out），导致 --list-only
  和下载音轨双双失败。而直接用 httpx 请求 Bilibili 页面却能正常返回。

  本模块通过模拟浏览器直接请求 Bilibili 页面和官方 API 来绕过此问题：
    1. 用 httpx 请求视频页面，提取 window.__INITIAL_STATE__ 数据
    2. 获取完整分P列表（含 title / cid / duration）
    3. 通过 B站 playurl API 直接获取音频流地址并下载

设计原则：
  - 仅作为 fallback：主流程仍优先走 yt-dlp（更通用），本模块在
    yt-dlp 失败时自动降级启用
  - 零额外依赖：仅用 httpx（faster-whisper 的依赖链已包含）
  - 可被 agent 独立调用用于系列枚举（不经过 transcribe.py 主流程）
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Optional

import httpx

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

BILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}

PAGE_URL_TPL = "https://www.bilibili.com/video/{bvid}"
PLAYURL_API_TPL = (
    "https://api.bilibili.com/x/player/playurl"
    "?bvid={bvid}&cid={cid}&qn=80&fnval=4048&fourk=1"
)

MAX_RETRIES = 3
RETRY_DELAY = 3  # seconds


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def parse_bvid(text: str) -> Optional[str]:
    """从 URL 或纯文本中提取 BV 号。"""
    m = re.search(r"(?i)BV[0-9A-Za-z]+", text)
    return m.group(0) if m else None


def parse_p(text: str) -> Optional[int]:
    """从 URL 查询参数中提取 p（分P序号）。"""
    m = re.search(r"[?&]p=(\d+)", text)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# 1. 系列枚举 — 从页面 HTML 提取分P列表
# ---------------------------------------------------------------------------

def bilibili_direct_list(url: str) -> list[dict]:
    """从 B站页面 HTML 直接提取分P/系列视频列表。

    与 yt-dlp 的 list_videos() 返回格式兼容：
        index: str, 分P序号
        id: str, cid（内容ID，用于 playurl API）
        title: str, 分P标题
        duration: int, 时长（秒）
        bvid: str, 视频 BV 号

    Args:
        url: B站视频链接或 BV 号（支持 ?p=N 参数）

    Raises:
        RuntimeError: 页面请求失败或数据解析失败
    """
    bvid = parse_bvid(url)
    if not bvid:
        raise RuntimeError(f"无法从输入中提取 BV 号: {url!r}")

    page_url = PAGE_URL_TPL.format(bvid=bvid)

    # --- 带重试的页面请求 ---
    html = _fetch_page(page_url)
    if html is None:
        raise RuntimeError(
            f"B站页面请求失败（重试{MAX_RETRIES}次），请检查网络连接"
        )

    # --- 提取 window.__INITIAL_STATE__ ---
    initial_state = _extract_initial_state(html)
    if initial_state is None:
        raise RuntimeError("页面未找到 __INITIAL_STATE__ 数据，可能B站页面结构已变更")

    video_data = initial_state.get("videoData", {})
    pages = video_data.get("pages", [])
    series_title = video_data.get("title", "")

    if not pages:
        # 单视频模式
        cid = video_data.get("cid", "")
        duration = video_data.get("duration", 0)
        if isinstance(duration, float):
            duration = int(duration)
        return [{
            "index": "1",
            "id": str(cid) if cid else "",
            "title": series_title,
            "duration": duration,
            "bvid": bvid,
        }]

    # 分P模式
    items = []
    for i, pg in enumerate(pages, 1):
        duration = pg.get("duration", 0)
        if isinstance(duration, float):
            duration = int(duration)
        items.append({
            "index": str(i),
            "id": str(pg.get("cid", "")),
            "title": pg.get("part", ""),
            "duration": duration,
            "bvid": bvid,
        })

    return items


def _fetch_page(url: str) -> Optional[str]:
    """带重试的页面请求。成功返回 HTML 文本，失败返回 None。"""
    for attempt in range(MAX_RETRIES):
        try:
            resp = httpx.get(
                url,
                headers=BILI_HEADERS,
                follow_redirects=True,
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.text
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
    return None


def _extract_initial_state(html: str) -> Optional[dict]:
    """从 HTML 中提取 window.__INITIAL_STATE__ JSON 数据。"""
    match = re.search(
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});",
        html, re.DOTALL,
    )
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# 2. 音频下载 — 通过 B站 playurl API
# ---------------------------------------------------------------------------

def bilibili_direct_download_audio(url: str, output_dir: str) -> str:
    """通过 B站官方 playurl API 直接下载音频流，绕过 yt-dlp。

    流程：
        1. 先请求视频页面获取 cid（分P内容ID）
        2. 用 bvid + cid 请求 playurl API 获取音频流地址
        3. 下载音频 .m4s 文件

    Args:
        url: B站视频链接（支持 ?p=N 指定分P）
        output_dir: 输出目录

    Returns:
        str: 下载后的音频文件路径

    Raises:
        RuntimeError: 任意步骤失败
    """
    bvid = parse_bvid(url)
    if not bvid:
        raise RuntimeError(f"无法从输入中提取 BV 号: {url!r}")

    target_p = parse_p(url) or 1

    # Step 1: 获取列表，找到目标分P的 cid
    items = bilibili_direct_list(url)
    target_item = _find_target_part(items, target_p)
    if not target_item:
        raise RuntimeError(f"未找到第 {target_p} 分P")

    cid = target_item["id"]
    title = target_item.get("title", f"P{target_p}")
    if not cid:
        raise RuntimeError("cid 为空，无法获取音频流")

    # Step 2: 请求 playurl API 获取音频流地址
    api_url = PLAYURL_API_TPL.format(bvid=bvid, cid=cid)
    api_data = _fetch_playurl(api_url)
    if api_data is None:
        raise RuntimeError("playurl API 请求失败")

    audio_list = api_data.get("data", {}).get("dash", {}).get("audio", [])
    if not audio_list:
        raise RuntimeError("API 返回无音频流")

    # 选最高带宽的音频流
    best = max(audio_list, key=lambda x: x.get("bandwidth", 0))
    audio_url = best.get("baseUrl", "")
    if not audio_url:
        raise RuntimeError("音频URL为空")

    # Step 3: 下载音频
    safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:50]
    out_file = os.path.join(
        output_dir, f"{safe_title}_{bvid}_p{target_p}.m4s"
    )

    _download_file(audio_url, out_file)
    return out_file


def _find_target_part(items: list[dict], target_p: int) -> Optional[dict]:
    """在列表中查找指定分P。找不到时返回第一项（单视频兼容）。"""
    for it in items:
        if str(it.get("index", "")) == str(target_p):
            return it
    # 单视频或无匹配，取第一个
    return items[0] if items else None


def _fetch_playurl(api_url: str) -> Optional[dict]:
    """请求 playurl API，返回 JSON 数据。失败返回 None。"""
    for attempt in range(MAX_RETRIES):
        try:
            resp = httpx.get(
                api_url,
                headers=BILI_HEADERS,
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(
                    f"API 返回错误: code={data.get('code')}, "
                    f"msg={data.get('message', '')}"
                )
            return data
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
    return None


def _download_file(url: str, out_path: str) -> None:
    """下载文件到指定路径。"""
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            with httpx.Client(
                headers=BILI_HEADERS,
                follow_redirects=True,
                timeout=300.0,
            ) as client:
                resp = client.get(url)
                resp.raise_for_status()
                with open(out_path, "wb") as f:
                    f.write(resp.content)
            return
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
    raise RuntimeError(f"文件下载失败: {last_error}")


# ---------------------------------------------------------------------------
# 3. 安全 fallback 接口 — 供 transcribe.py 调用，不抛异常
# ---------------------------------------------------------------------------

def try_direct_list(url: str) -> Optional[list[dict]]:
    """尝试直连枚举，失败返回 None（不抛异常）。"""
    try:
        return bilibili_direct_list(url)
    except Exception:
        return None


def try_direct_download(url: str, output_dir: str) -> Optional[str]:
    """尝试直连下载音频，失败返回 None。"""
    try:
        return bilibili_direct_download_audio(url, output_dir)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 4. 自测入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python bili_direct.py <BV号/链接> [--list-only|下载目录]")
        sys.exit(1)

    url = sys.argv[1]
    cmd = sys.argv[2] if len(sys.argv) > 2 else "--list-only"

    if cmd == "--list-only":
        items = bilibili_direct_list(url)
        print(f"共 {len(items)} 个视频:")
        for it in items:
            dur = it.get("duration", 0)
            print(f"  P{it['index']:>3} | {it['title']} | {dur//60}:{dur%60:02d}")

        # 如含 p 参数，标出目标
        target = parse_p(url)
        if target:
            for it in items:
                if str(it["index"]) == str(target):
                    dur = it.get("duration", 0)
                    print(f"\n  👉 目标 P{target}: {it['title']} ({dur//60}:{dur%60:02d})")
    else:
        out_dir = cmd
        path = bilibili_direct_download_audio(url, out_dir)
        print(f"✅ 音频已下载: {path}")
        size = os.path.getsize(path)
        print(f"   大小: {size:,} bytes ({size/1024/1024:.1f} MB)")
