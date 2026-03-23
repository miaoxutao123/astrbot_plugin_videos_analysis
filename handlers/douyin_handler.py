"""
抖音被动解析处理器
"""

import os
import re

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain

from ..services.douyin_service import download
from ..services.gemini_service import process_images_with_gemini
from ..douyin_scraper.douyin_parser import DouyinParser
from ..utils.config_helper import (
    DOWNLOAD_DIR_DY,
    create_node,
    send_file_if_needed,
    send_llm_response,
    should_comprehend,
    cleanup_old_files,
)


async def _process_multi_part_media(plugin, event, result, media_type: str):
    """处理多段媒体（图片或视频）"""
    ns = Comp.Nodes([])
    download_dir = DOWNLOAD_DIR_DY
    os.makedirs(download_dir, exist_ok=True)

    for i in range(len(result["media_urls"])):
        media_url = result["media_urls"][i]
        aweme_id = result.get("aweme_id", "unknown")

        try:
            if media_type == "image" or media_url.endswith(".jpg"):
                local_filename = f"{download_dir}/{aweme_id}_{i}.jpg"
                logger.info(f"开始下载图片 {i+1}: {media_url}")
                success = await download(media_url, local_filename, plugin.effective_douyin_cookie)

                if success and os.path.exists(local_filename):
                    nap_file_path = await send_file_if_needed(local_filename, plugin.nap_server_address, plugin.nap_server_port)
                    content = [Comp.Image.fromFileSystem(nap_file_path)]
                else:
                    try:
                        content = [Comp.Image.fromURL(media_url)]
                    except Exception:
                        content = [Comp.Plain(f"图片 {i+1} 下载失败")]
            else:
                local_filename = f"{download_dir}/{aweme_id}_{i}.mp4"
                logger.info(f"开始下载视频 {i+1}: {media_url}")
                await download(media_url, local_filename, plugin.effective_douyin_cookie)

                if os.path.exists(local_filename):
                    file_size_mb = os.path.getsize(local_filename) / (1024 * 1024)
                    nap_file_path = await send_file_if_needed(local_filename, plugin.nap_server_address, plugin.nap_server_port)

                    if file_size_mb > plugin.max_video_size:
                        content = [Comp.File(file=nap_file_path, name=os.path.basename(nap_file_path))]
                    else:
                        content = [Comp.Video.fromFileSystem(nap_file_path)]
                else:
                    try:
                        content = [Comp.Video.fromURL(media_url)]
                    except Exception:
                        content = [Comp.Plain(f"视频 {i+1} 下载失败")]

        except Exception as e:
            logger.error(f"处理媒体文件 {i+1} 时发生错误: {e}")
            content = [Comp.Plain(f"媒体文件 {i+1} 处理失败: {str(e)}")]

        node = create_node(event, content)
        ns.nodes.append(node)
    return ns


async def _process_single_media(plugin, event, result, media_type: str):
    """处理单个媒体文件"""
    media_url = result["media_urls"][0]
    download_dir = DOWNLOAD_DIR_DY
    os.makedirs(download_dir, exist_ok=True)
    aweme_id = result.get("aweme_id", "unknown")

    try:
        if media_type == "image":
            local_filename = f"{download_dir}/{aweme_id}.jpg"
            logger.info(f"开始下载图片: {media_url}")
            success = await download(media_url, local_filename, plugin.effective_douyin_cookie)

            if success and os.path.exists(local_filename):
                nap_file_path = await send_file_if_needed(local_filename, plugin.nap_server_address, plugin.nap_server_port)
                return [Comp.Image.fromFileSystem(nap_file_path)]
            else:
                try:
                    return [Comp.Image.fromURL(media_url)]
                except Exception:
                    return [Comp.Plain("图片下载失败")]
        else:
            local_filename = f"{download_dir}/{aweme_id}.mp4"
            logger.info(f"开始下载视频: {media_url}")
            await download(media_url, local_filename, plugin.effective_douyin_cookie)

            if os.path.exists(local_filename):
                file_size_mb = os.path.getsize(local_filename) / (1024 * 1024)
                nap_file_path = await send_file_if_needed(local_filename, plugin.nap_server_address, plugin.nap_server_port)

                if file_size_mb > plugin.max_video_size:
                    return [Comp.File(file=nap_file_path, name=os.path.basename(nap_file_path))]
                else:
                    return [Comp.Video.fromFileSystem(nap_file_path)]
            else:
                try:
                    return [Comp.Video.fromURL(media_url)]
                except Exception:
                    return [Comp.Plain("视频下载失败")]

    except Exception as e:
        logger.error(f"处理媒体文件时发生错误: {e}")
        return [Comp.Plain(f"媒体文件处理失败: {str(e)}")]


async def _process_douyin_comprehension(plugin, event, result, content_type, api_key, proxy_url):
    """处理抖音视频/图片的深度理解"""
    download_dir = DOWNLOAD_DIR_DY
    os.makedirs(download_dir, exist_ok=True)

    media_urls = result.get("media_urls", [])
    aweme_id = result.get("aweme_id", "unknown")

    if content_type == "image":
        async for response in _process_douyin_images_comprehension(plugin, event, media_urls, aweme_id, download_dir, api_key, proxy_url):
            yield response
    elif content_type in ["video", "multi_video"]:
        async for response in _process_douyin_videos_comprehension(plugin, event, media_urls, aweme_id, download_dir, api_key, proxy_url):
            yield response


async def _process_douyin_images_comprehension(plugin, event, media_urls, aweme_id, download_dir, api_key, proxy_url):
    """处理抖音图片的深度理解"""
    if plugin.show_progress_messages:
        yield event.plain_result(f"检测到 {len(media_urls)} 张图片，正在下载并分析...")

    image_paths = []
    for i, media_url in enumerate(media_urls):
        local_filename = f"{download_dir}/{aweme_id}_{i}.jpg"
        success = await download(media_url, local_filename, plugin.effective_douyin_cookie)
        if success and os.path.exists(local_filename):
            image_paths.append(local_filename)

    if not image_paths:
        yield event.plain_result("抱歉，无法下载图片进行分析。")
        return

    try:
        if plugin.show_progress_messages:
            yield event.plain_result("正在使用AI分析图片内容...")

        prompt = "请详细描述这些图片的内容，包括场景、人物、物品、文字信息和传达的核心信息。如果是多张图片，请分别描述每张图片的内容。"
        image_response = await process_images_with_gemini(api_key, prompt, image_paths, proxy_url)

        if image_response and image_response[0]:
            for image_path in image_paths:
                nap_file_path = await send_file_if_needed(image_path, plugin.nap_server_address, plugin.nap_server_port)
                yield event.chain_result([Comp.Image.fromFileSystem(nap_file_path)])

            async for response in send_llm_response(plugin.context, event, image_response[0], "抖音"):
                yield response
        else:
            yield event.plain_result("抱歉，我暂时无法理解这些图片的内容。")

    except Exception as e:
        logger.error(f"处理抖音图片理解时发生错误: {e}")
        yield event.plain_result("抱歉，分析图片时出现了问题。")


async def _process_douyin_videos_comprehension(plugin, event, media_urls, aweme_id, download_dir, api_key, proxy_url):
    """处理抖音视频的深度理解"""
    from .video_handler import analyze_video_with_gemini

    media_url = media_urls[0]
    local_filename = f"{download_dir}/{aweme_id}.mp4"

    if plugin.show_progress_messages:
        yield event.plain_result("正在下载视频进行分析...")

    await download(media_url, local_filename, plugin.effective_douyin_cookie)

    if not os.path.exists(local_filename):
        yield event.plain_result("抱歉，无法下载视频进行分析。")
        return

    try:
        nap_file_path = await send_file_if_needed(local_filename, plugin.nap_server_address, plugin.nap_server_port)
        file_size_mb = os.path.getsize(local_filename) / (1024 * 1024)

        if file_size_mb > plugin.max_video_size:
            yield event.chain_result([Comp.File(file=nap_file_path, name=os.path.basename(nap_file_path))])
        else:
            yield event.chain_result([Comp.Video.fromFileSystem(nap_file_path)])

        async for response in analyze_video_with_gemini(plugin, event, local_filename, "抖音", api_key=api_key, proxy_url=proxy_url):
            yield response

    except Exception as e:
        logger.error(f"处理抖音视频理解时发生错误: {e}")
        yield event.plain_result("抱歉，分析视频时出现了问题。")
    finally:
        if os.path.exists(local_filename):
            os.remove(local_filename)


async def handle_douyin_parse(plugin, event):
    """抖音被动解析核心逻辑"""
    cookie = plugin.effective_douyin_cookie
    message_str = event.message_str
    match = re.search(r"(https?://v\.douyin\.com/[a-zA-Z0-9_\-]+(?:-[a-zA-Z0-9_\-]+)?)", message_str)

    if not match:
        return

    await cleanup_old_files(DOWNLOAD_DIR_DY, plugin.delete_time)

    if plugin.show_progress_messages:
        yield event.plain_result("正在解析抖音链接...")

    parser = DouyinParser(cookie=cookie, proxy=plugin.douyin_proxy)
    result = await parser.parse(message_str)

    if not result:
        yield event.plain_result("抱歉，这个抖音链接我不能打开，请检查一下链接是否正确。")
        return

    if isinstance(result, dict) and result.get("error"):
        error_message = result.get("error", "Unknown error")
        details = result.get("details", "")
        aweme_id = result.get("aweme_id")
        logger.error("Douyin parse failed: %s | details=%s | aweme_id=%s", error_message, details, aweme_id)

        if "Empty response" in str(details) or "Invalid JSON" in str(details):
            yield event.plain_result("抖音解析失败，cookie可能已过期。\n管理员可使用 /dy_login 重新扫码登录。")
            return

        message_lines = [f"抱歉，解析这个抖音链接失败：{error_message}"]
        if aweme_id:
            message_lines.append(f"关联作品ID：{aweme_id}")
        if details and details != error_message:
            message_lines.append(f"详细信息：{details}")
        yield event.plain_result("\n".join(message_lines))
        return

    content_type = result.get("type")
    if not content_type or content_type not in ["video", "image", "multi_video"]:
        yield event.plain_result("解析失败，无法识别内容类型。")
        return

    # 抖音深度理解流程
    from ..utils.config_helper import get_gemini_api_config_with_fallback
    do_comprehend = should_comprehend(event, plugin.douyin_video_comprehend, plugin.private_auto_comprehend)
    if do_comprehend and content_type in ["video", "multi_video", "image"]:
        if plugin.show_progress_messages:
            yield event.plain_result("我看到了一个抖音视频链接，让我来仔细分析一下内容，请稍等一下...")

        api_key, proxy_url = await get_gemini_api_config_with_fallback(plugin.context, plugin.gemini_api_key, plugin.gemini_base_url)

        if not api_key:
            yield event.plain_result("抱歉，我需要Gemini API才能理解视频，但是没有找到相关配置。\n请在框架中配置Gemini Provider或在插件配置中提供gemini_api_key。")
        else:
            try:
                async for response in _process_douyin_comprehension(plugin, event, result, content_type, api_key, proxy_url):
                    yield response
                return
            except Exception as e:
                logger.error(f"处理抖音视频理解时发生错误: {e}")
                yield event.plain_result("抱歉，处理这个视频时出现了一些问题，将使用常规模式解析。")

    # 常规解析流程
    media_count = len(result.get("media_urls", []))
    if plugin.show_progress_messages:
        if media_count > 1:
            yield event.plain_result(f"检测到 {media_count} 个文件，正在下载...")
        else:
            yield event.plain_result("正在下载媒体文件...")

    is_multi_part = "media_urls" in result and len(result["media_urls"]) != 1

    try:
        if is_multi_part:
            ns = await _process_multi_part_media(plugin, event, result, content_type)
            await event.send(MessageChain([ns]))
        else:
            content = await _process_single_media(plugin, event, result, content_type)
            await event.send(MessageChain(content))

    except Exception as e:
        logger.error(f"处理抖音媒体时发生错误: {e}")
        yield event.plain_result(f"处理媒体文件时发生错误: {str(e)}")
