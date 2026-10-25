"""
LLM Agent 工具：understand_video — 深度理解视频内容

支持：
- 在线视频链接（下载后分析）
- 本地文件路径（直接分析）
- 通过解析获得的视频直链（HTTP直链 → 下载 → 分析）
"""

import os
import re
import uuid

import httpx

from astrbot.api import logger

from ..douyin_scraper.douyin_parser import DOUYIN_LINK
from ..services.gemini_service import process_video_with_gemini
from ..services.mimo_service import analyze_video_with_mimo
from ..services.video_analysis import (
    analyze_video as local_asr_analyze_video,
    local_asr_available,
)
from ..services.douyin_service import download as douyin_download
from ..services.bilibili_service import process_bili_video
from ..utils.config_helper import (
    DOWNLOAD_DIR_DY,
    DOWNLOAD_DIR_DIRECT,
    resolve_gemini_config,
)


VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".flv", ".webm", ".m4v", ".ts"}
# 允许直接读取的本地目录（AstrBot 数据目录，含平台下载的临时文件）
_ALLOWED_LOCAL_ROOT = os.path.realpath("data")


def _is_allowed_local_video(path: str) -> bool:
    """只允许 data/ 下的视频文件，防止 LLM 被诱导把任意本地文件发给外部模型。"""
    real = os.path.realpath(path)
    return (
        os.path.isfile(real)
        and os.path.splitext(real)[1].lower() in VIDEO_EXTS
        and os.path.commonpath([real, _ALLOWED_LOCAL_ROOT]) == _ALLOWED_LOCAL_ROOT
    )


def _is_public_http_url(url: str) -> bool:
    """拒绝指向本机/内网的地址（防 SSRF）。"""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    host = urlparse(url).hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    # 198.18.0.0/15 是 Clash/ShellCrash 等 fake-ip 模式的地址段，属于正常外网访问
    fake_ip = ipaddress.ip_network("198.18.0.0/15")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.version == 4 and ip in fake_ip:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            return False
    return True


async def _download_from_url(url: str, download_dir: str, cookie: str = None, max_mb: float = 500) -> str | None:
    """从 HTTP 直链下载视频到本地（只接受视频/二进制内容）"""
    if not _is_public_http_url(url):
        logger.warning(f"拒绝下载非公网地址: {url}")
        return None
    os.makedirs(download_dir, exist_ok=True)
    filename = f"{uuid.uuid4().hex[:8]}.mp4"
    local_path = os.path.join(download_dir, filename)

    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            if cookie:
                headers["Cookie"] = cookie
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                ctype = response.headers.get("content-type", "").lower()
                if ctype.startswith(("text/", "application/json", "application/xml", "application/javascript")):
                    logger.warning(f"直链返回的不是视频 (content-type={ctype})")
                    return None
                size = 0
                with open(local_path, "wb") as f:
                    async for chunk in response.aiter_bytes(1 << 16):
                        size += len(chunk)
                        if size > max_mb * 1024 * 1024:
                            raise ValueError(f"视频超过 {max_mb}MB 上限")
                        f.write(chunk)

            if os.path.getsize(local_path) < 1024:
                os.remove(local_path)
                return None

            logger.info(f"视频下载成功: {local_path} ({os.path.getsize(local_path) / 1024 / 1024:.1f}MB)")
            return local_path
    except Exception as e:
        logger.error(f"下载视频失败: {e}")
        if os.path.exists(local_path):
            os.remove(local_path)
        return None


_ALL_METHODS = ("llm", "gemini", "mimo", "local_asr")


def _method_order(preferred: str) -> list[str]:
    """首选方法在前，其余作为回退（未开启的 Gemini/MiMo 会在各自的分析函数里直接跳过）。"""
    preferred = preferred if preferred in _ALL_METHODS else "llm"
    return [preferred, *[m for m in _ALL_METHODS if m != preferred]]


async def _analyze_llm(plugin, video_path: str, prompt: str, event=None):
    if event is None:
        return "缺少会话上下文"
    from ..services.browse_service import browse, cleanup_browse_files

    try:
        res = await browse(plugin, event, video_path, {"title": ""}, user_prompt=prompt)
    finally:
        cleanup_browse_files(video_path)
    if not res.get("summary"):
        return "抽帧+转写总结失败"
    return {"analysis": res["summary"], "method": "frames+asr", "transcript_source": res["transcript_source"], "summary_by": res["method"]}


async def _analyze_gemini(plugin, video_path: str, prompt: str, event=None):
    gcfg = resolve_gemini_config(plugin)
    if not gcfg:
        return "未配置 Gemini"
    text, duration = await process_video_with_gemini(gcfg, prompt, video_path)
    if not text:
        return "Gemini 调用失败（详见日志）"
    return {"analysis": text, "method": "gemini", "model": gcfg.model, "duration": f"{duration:.1f}s"}


async def _analyze_mimo(plugin, video_path: str, prompt: str, event=None):
    if not (plugin.mimo_enabled and plugin.mimo_api_key):
        return "未开启 MiMo"
    text = await analyze_video_with_mimo(
        video_path=video_path,
        api_key=plugin.mimo_api_key,
        api_base=plugin.mimo_api_base,
        model=plugin.mimo_model,
        prompt=prompt,
    )
    return {"analysis": text, "method": "mimo", "model": plugin.mimo_model} if text else "MiMo 返回空结果"


async def _analyze_local_asr(plugin, video_path: str, prompt: str, event=None):
    if not local_asr_available():
        return "未安装 funasr"
    asr_result = await local_asr_analyze_video(video_path)
    if not asr_result:
        return "本地 ASR 无结果"
    return {
        "analysis": asr_result.get("text") or "视频分析完成（仅抽帧，无文本内容）",
        "method": "local_asr",
        "frames": asr_result.get("images", []),
    }


_ANALYZERS = {"llm": _analyze_llm, "gemini": _analyze_gemini, "mimo": _analyze_mimo, "local_asr": _analyze_local_asr}


async def handle_understand_video(plugin, video_source: str, prompt: str = None, event=None) -> dict:
    """
    深度理解视频内容。

    支持输入：
    1. 抖音/B站链接 → 自动解析下载后分析
    2. HTTP 直链（.mp4等）→ 下载后分析
    3. 本地文件路径 → 直接分析

    分析方法（按配置自动选择）：
    - Gemini API（默认，视频直接上传）
    - MiMo 多模态模型
    - 本地 ASR + 智能抽帧（大视频回退方案）

    Returns:
        包含分析结果的字典
    """
    if not video_source:
        return {"error": "请提供视频链接或本地路径"}

    video_path = None
    temp_file = False
    platform = "视频"

    try:
        # ==================== 1. 解析视频来源 ====================
        if os.path.exists(video_source):
            # 本地文件（仅限 data/ 下的视频）
            if not _is_allowed_local_video(video_source):
                return {"error": "只能分析 AstrBot data 目录下的视频文件"}
            video_path = video_source
            logger.info(f"使用本地文件: {video_path}")

        elif DOUYIN_LINK.search(video_source):
            # 抖音链接
            platform = "抖音"
            cookie = plugin.effective_douyin_cookie
            result = await plugin.accounts.parse_douyin(video_source)

            if not result or result.get("error"):
                return {"error": f"抖音链接解析失败: {result.get('error', '未知错误') if result else '无法解析'}"}

            if result.get("type") not in ["video", "multi_video"]:
                return {"error": f"该链接不是视频类型（类型: {result.get('type')})"}

            media_url = result["media_urls"][0]
            os.makedirs(DOWNLOAD_DIR_DY, exist_ok=True)
            video_path = f"{DOWNLOAD_DIR_DY}/{result.get('aweme_id', uuid.uuid4().hex[:8])}.mp4"
            await douyin_download(media_url, video_path, cookie)
            temp_file = True

            if not os.path.exists(video_path):
                return {"error": "抖音视频下载失败"}

        elif re.search(r"(b23\.tv|bilibili\.com|BV1\w{9}|av\d+)", video_source):
            # B站链接
            platform = "B站"
            result = await process_bili_video(
                video_source, download_flag=True,
                quality=16, use_login=plugin.bili_use_login, event=None,
            )
            if not result or not result.get("video_path"):
                return {"error": "B站视频下载失败"}
            video_path = result["video_path"]
            temp_file = True

        elif re.match(r"https?://", video_source):
            # HTTP 直链
            video_path = await _download_from_url(video_source, DOWNLOAD_DIR_DIRECT)
            temp_file = True
            if not video_path:
                return {"error": "视频下载失败，可能是链接无效或服务器拒绝访问"}
        else:
            return {"error": f"无法识别的视频来源: {video_source}"}

        # ==================== 2. 分析视频 ====================
        analysis_prompt = prompt or "请详细描述这个视频的内容，包括场景、人物、动作、对话和传达的核心信息。请使用中文回答。"
        errors = []
        for method in _method_order(plugin.video_understand_method):
            try:
                result = await _ANALYZERS[method](plugin, video_path, analysis_prompt, event)
            except Exception as e:
                logger.warning(f"[understand_video] {method} 分析异常: {e}")
                result = None
            if isinstance(result, str):
                errors.append(f"{method}: {result}")
                continue
            if result:
                return {"platform": platform, **result}
        return {"error": "所有分析方法均失败", "details": errors}

    finally:
        if temp_file and video_path and os.path.exists(video_path):
            try:
                os.remove(video_path)
            except OSError:
                pass
