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

from ..services.gemini_service import (
    process_video_with_gemini,
    process_images_with_gemini,
)
from ..services.mimo_service import analyze_video_with_mimo
from ..services.video_analysis import (
    analyze_video as local_asr_analyze_video,
)
from ..services.douyin_service import download as douyin_download
from ..services.bilibili_service import process_bili_video
from ..douyin_scraper.douyin_parser import DouyinParser
from ..utils.config_helper import (
    DOWNLOAD_DIR_DY,
    DOWNLOAD_DIR_DIRECT,
    get_gemini_api_config_with_fallback,
)


async def _download_from_url(url: str, download_dir: str, cookie: str = None) -> str | None:
    """从 HTTP 直链下载视频到本地"""
    os.makedirs(download_dir, exist_ok=True)
    filename = f"{uuid.uuid4().hex[:8]}.mp4"
    local_path = os.path.join(download_dir, filename)

    try:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            if cookie:
                headers["Cookie"] = cookie
            response = await client.get(url, headers=headers)
            response.raise_for_status()

            with open(local_path, "wb") as f:
                f.write(response.content)

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


async def handle_understand_video(plugin, video_source: str, prompt: str = None) -> dict:
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
            # 本地文件
            video_path = video_source
            logger.info(f"使用本地文件: {video_path}")

        elif re.search(r"v\.douyin\.com", video_source):
            # 抖音链接
            platform = "抖音"
            cookie = plugin.effective_douyin_cookie
            parser = DouyinParser(cookie=cookie, proxy=plugin.douyin_proxy)
            result = await parser.parse(video_source)

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
        result_parts = []
        method = getattr(plugin, "video_understand_method", "local_asr")

        # 根据配置的 video_understand_method 选择分析路径
        if method == "mimo" and plugin.mimo_api_key:
            # MiMo 优先
            try:
                logger.info("使用 MiMo 模型分析视频...")
                mimo_result = await analyze_video_with_mimo(
                    video_path=video_path,
                    api_key=plugin.mimo_api_key,
                    api_base=plugin.mimo_api_base,
                    model=plugin.mimo_model,
                    prompt=analysis_prompt,
                )
                if mimo_result:
                    result_parts.append({"type": "text", "text": mimo_result, "source": "MiMo"})
                    return {
                        "platform": platform,
                        "analysis": mimo_result,
                        "method": "mimo",
                        "result_parts": result_parts,
                    }
            except Exception as e:
                logger.warning(f"MiMo 分析失败，回退到 Gemini: {e}")

        # Gemini 分析（默认回退 或 method 不是 mimo）
        api_key, proxy_url = await get_gemini_api_config_with_fallback(
            plugin.context, plugin.gemini_api_key, plugin.gemini_base_url,
        )

        if api_key:
            try:
                logger.info("使用 Gemini 分析视频...")
                response_text, duration = await process_video_with_gemini(
                    api_key, analysis_prompt, video_path, proxy_url,
                )
                if response_text:
                    result_parts.append({"type": "text", "text": response_text, "source": "Gemini"})
                    return {
                        "platform": platform,
                        "analysis": response_text,
                        "method": "gemini",
                        "duration": f"{duration:.1f}s" if duration else None,
                        "result_parts": result_parts,
                    }
            except Exception as e:
                logger.warning(f"Gemini 分析失败: {e}")

        # 本地 ASR 回退（method == "local_asr" 或其他方法均失败）
        try:
            logger.info("使用本地 ASR + 抽帧分析视频...")
            asr_result = await local_asr_analyze_video(video_path)
            if asr_result:
                text_content = asr_result.get("text", "")
                image_paths = asr_result.get("images", [])

                if text_content:
                    result_parts.append({"type": "text", "text": text_content, "source": "local_asr"})
                for img_path in image_paths:
                    result_parts.append({"type": "image", "path": img_path, "source": "local_asr"})

                return {
                    "platform": platform,
                    "analysis": text_content or "视频分析完成（仅抽帧，无文本内容）",
                    "method": "local_asr",
                    "result_parts": result_parts,
                }
        except Exception as e:
            logger.warning(f"本地 ASR 分析失败: {e}")

        return {"error": "所有分析方法均失败。请检查 Gemini API 配置或 MiMo API 配置。"}

    finally:
        if temp_file and video_path and os.path.exists(video_path):
            try:
                os.remove(video_path)
            except OSError:
                pass
