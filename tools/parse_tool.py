"""
LLM Agent 工具：parse_video_link — 解析视频链接（抖音/B站）
"""

import os
import re

from astrbot.api import logger

from ..douyin_scraper.douyin_parser import DouyinParser
from ..services.bilibili_service import process_bili_video
from ..services.douyin_service import download


async def handle_parse_video_link(plugin, link: str) -> dict:
    """
    解析视频链接并返回结构化信息。

    支持：
    - 抖音: v.douyin.com
    - B站: b23.tv, www.bilibili.com/video/, BV号, AV号

    Args:
        plugin: 插件实例
        link: 视频链接

    Returns:
        包含解析结果的字典
    """
    if not link:
        return {"error": "请提供视频链接"}

    # 自动提取 URL
    url_match = re.search(
        r"(https?://v\.douyin\.com/\S+|https?://b23\.tv/\S+|"
        r"https?://www\.bilibili\.com/video/\S+|BV1\w{9}|av\d+)",
        link,
    )
    if url_match:
        link = url_match.group(0)

    # ==================== 抖音解析 ====================
    if re.search(r"v\.douyin\.com", link):
        try:
            cookie = plugin.effective_douyin_cookie
            parser = DouyinParser(cookie=cookie, proxy=plugin.douyin_proxy)
            result = await parser.parse(link)

            if not result:
                return {"error": "抖音链接解析失败", "platform": "douyin"}

            if isinstance(result, dict) and result.get("error"):
                return {
                    "error": result["error"],
                    "platform": "douyin",
                    "details": result.get("details", ""),
                }

            response = {
                "platform": "douyin",
                "type": result.get("type", "unknown"),
                "media_urls": result.get("media_urls", []),
                "aweme_id": result.get("aweme_id"),
            }

            # 如果是视频，提供第一个直链
            if result.get("type") in ["video", "multi_video"] and result.get("media_urls"):
                response["direct_video_url"] = result["media_urls"][0]

            return response

        except Exception as e:
            logger.error(f"抖音链接解析异常: {e}")
            return {"error": f"解析失败: {str(e)}", "platform": "douyin"}

    # ==================== B站解析 ====================
    elif re.search(r"(b23\.tv|bilibili\.com|BV1\w{9}|av\d+)", link):
        try:
            result = await process_bili_video(
                link,
                download_flag=False,
                quality=plugin.bili_quality,
                use_login=plugin.bili_use_login,
                event=None,
            )

            if not result:
                return {"error": "B站视频解析失败", "platform": "bilibili"}

            return {
                "platform": "bilibili",
                "title": result.get("title", ""),
                "bvid": result.get("bvid", ""),
                "view_count": result.get("view_count", 0),
                "like_count": result.get("like_count", 0),
                "coin_count": result.get("coin_count", 0),
                "favorite_count": result.get("favorite_count", 0),
                "danmaku_count": result.get("danmaku_count", 0),
                "duration": result.get("duration", 0),
                "cover": result.get("cover"),
                "direct_url": result.get("direct_url"),
                "video_url": f"https://www.bilibili.com/video/{result.get('bvid', '')}",
            }

        except Exception as e:
            logger.error(f"B站链接解析异常: {e}")
            return {"error": f"解析失败: {str(e)}", "platform": "bilibili"}

    else:
        return {"error": "不支持的链接格式。支持：抖音(v.douyin.com)、B站(b23.tv/bilibili.com/BV号)"}
