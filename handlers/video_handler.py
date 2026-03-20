"""
视频处理器：直链视频注入 + 通用视频 Gemini 分析流程 + 理解视频命令
"""

import os
import re

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.message_components import Image, Nodes, Plain

from ..services.bilibili_service import process_bili_video
from ..services.gemini_service import (
    process_audio_with_gemini,
    process_images_with_gemini,
    process_video_with_gemini,
)
from ..douyin_scraper.douyin_parser import DouyinParser
from ..utils.config_helper import (
    DOWNLOAD_DIR_DY,
    create_node,
    get_gemini_api_config_with_fallback,
    send_file_if_needed,
    send_llm_response,
)
from ..utils.media_utils import extract_frame, separate_audio_video


async def analyze_video_with_gemini(plugin, event, video_path: str, platform: str = "视频", api_key: str = None, proxy_url: str = None):
    """
    统一的视频分析流程：根据视频大小自动选择分析策略。
    """
    if not api_key:
        api_key, proxy_url = await get_gemini_api_config_with_fallback(
            plugin.context, plugin.gemini_api_key, plugin.gemini_base_url
        )
    if not api_key:
        yield event.plain_result(
            "抱歉，我需要Gemini API才能理解视频，但是没有找到相关配置。\n"
            "请在框架中配置Gemini Provider或在插件配置中提供gemini_api_key。"
        )
        return

    video_size_mb = os.path.getsize(video_path) / (1024 * 1024)
    video_summary = ""

    if video_size_mb > 30:
        # 大视频处理流程 (音频+关键帧)
        if plugin.show_progress_messages:
            yield event.plain_result(f"视频大小为 {video_size_mb:.2f}MB，采用音频+关键帧模式进行分析...")

        temp_files = []
        try:
            separated_files = await separate_audio_video(video_path)
            if not separated_files:
                yield event.plain_result("抱歉，我无法分离这个视频的音频和视频。")
                return
            audio_path, video_only_path = separated_files
            temp_files.extend([audio_path, video_only_path])

            description, timestamps, _ = await process_audio_with_gemini(api_key, audio_path, proxy_url)
            if not description or not timestamps:
                yield event.plain_result("抱歉，我无法分析这个视频的音频内容。")
                return

            image_paths = []
            ts_and_paths = []
            for ts in timestamps:
                frame_path = await extract_frame(video_only_path, ts)
                if frame_path:
                    image_paths.append(frame_path)
                    temp_files.append(frame_path)
                    ts_and_paths.append((ts, frame_path))

            if not image_paths:
                video_summary = description
            else:
                prompt = f"这是关于一个视频的摘要和一些从该视频中提取的关键帧。视频摘要如下：\n\n{description}\n\n请结合摘要和这些关键帧，对整个视频内容进行一个全面、生动的总结。"
                summary_tuple = await process_images_with_gemini(api_key, prompt, image_paths, proxy_url)
                video_summary = summary_tuple[0] if summary_tuple else "无法生成最终摘要。"

            if ts_and_paths:
                key_frames_nodes = Nodes([])
                key_frames_nodes.nodes.append(create_node(event, [Plain("以下是视频的关键时刻：")]))
                for ts, frame_path in ts_and_paths:
                    nap_frame_path = await send_file_if_needed(frame_path, plugin.nap_server_address, plugin.nap_server_port)
                    node_content = [
                        Image.fromFileSystem(nap_frame_path),
                        Plain(f"时间点: {ts}"),
                    ]
                    key_frames_nodes.nodes.append(create_node(event, node_content))
                yield event.chain_result([key_frames_nodes])
        finally:
            for tmp in temp_files:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
    else:
        # 小视频处理流程 (直接上传)
        if plugin.show_progress_messages:
            yield event.plain_result(f"视频大小为 {video_size_mb:.2f}MB，直接上传视频进行分析...")

        video_prompt = "请详细描述这个视频的内容，包括场景、人物、动作和传达的核心信息。"
        video_response = await process_video_with_gemini(api_key, video_prompt, video_path, proxy_url)
        video_summary = video_response[0] if video_response and video_response[0] else "抱歉，我暂时无法理解这个视频内容。"

    # 发送 AI 分析结果
    if video_summary:
        async for response in send_llm_response(plugin.context, event, video_summary, platform):
            yield response
    else:
        yield event.plain_result("抱歉，我无法理解这个视频的内容。")


async def handle_direct_video(plugin, event):
    """检测消息中的视频附件，将视频直链注入到 message_str 中"""
    if not event.message_obj:
        return

    from astrbot.core.message.components import Video as VideoComponent
    from astrbot.core.message.components import File as FileComponent

    VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".flv", ".webm", ".m4v", ".ts"}

    video_url = None
    messages = getattr(event.message_obj, "message", [])

    for seg in messages:
        if isinstance(seg, VideoComponent):
            video_url = seg.file
            break
        if isinstance(seg, FileComponent):
            name = getattr(seg, "name", "") or ""
            _, ext = os.path.splitext(name.lower())
            if ext in VIDEO_EXTS:
                video_url = getattr(seg, "url", "") or getattr(seg, "file_", "")
                if not video_url:
                    try:
                        video_url = await seg.get_file(allow_return_url=True)
                    except Exception:
                        pass
                break

    if not video_url:
        raw_message = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw_message, dict) and "message" in raw_message:
            for msg_item in raw_message["message"]:
                msg_type = msg_item.get("type", "")
                msg_data = msg_item.get("data", {})
                if msg_type == "video":
                    video_url = msg_data.get("url") or msg_data.get("file")
                    break
                if msg_type == "file":
                    fname = msg_data.get("name", "")
                    _, ext = os.path.splitext(fname.lower())
                    if ext in VIDEO_EXTS:
                        video_url = msg_data.get("url") or msg_data.get("file")
                        break

    if not video_url:
        return

    sender_name = event.get_sender_name() or event.get_sender_id()
    inject_text = f"\n[用户 {sender_name} 发送了一个视频，视频直链: {video_url} 。你可以使用 understand_video 工具分析这个视频的内容。]"

    event.message_str = (event.message_str or "") + inject_text
    logger.info(f"[direct_video] 已注入视频URL到上下文: {video_url[:100]}...")

    event.continue_event()


async def handle_comprehend_video_command(plugin, event):
    """主动触发视频 AI 理解"""
    message_str = event.message_str
    url_match = re.search(
        r"(https?://v\.douyin\.com/\S+|https?://b23\.tv/\S+|"
        r"https?://www\.bilibili\.com/video/\S+|BV1\w{9})",
        message_str,
    )

    if not url_match:
        yield event.plain_result("请提供视频链接，如：/理解视频 https://v.douyin.com/xxx")
        return

    url = url_match.group(0)

    api_key, proxy_url = await get_gemini_api_config_with_fallback(
        plugin.context, plugin.gemini_api_key, plugin.gemini_base_url
    )
    if not api_key:
        yield event.plain_result(
            "需要Gemini API才能理解视频，请先配置。\n"
            "请在框架中配置Gemini Provider或在插件配置中提供gemini_api_key。"
        )
        return

    if re.search(r"v\.douyin\.com", url):
        if plugin.show_progress_messages:
            yield event.plain_result("正在解析抖音链接并分析视频内容...")
        cookie = plugin.effective_douyin_cookie
        parser = DouyinParser(cookie=cookie, proxy=plugin.douyin_proxy)
        result = await parser.parse(url)
        if not result or result.get("error"):
            yield event.plain_result("抖音链接解析失败，请检查链接是否正确。")
            return
        content_type = result.get("type")
        if content_type not in ["video", "multi_video", "image"]:
            yield event.plain_result("无法识别内容类型。")
            return
        from .douyin_handler import _process_douyin_comprehension
        async for resp in _process_douyin_comprehension(plugin, event, result, content_type, api_key, proxy_url):
            yield resp

    elif re.search(r"(b23\.tv|bilibili\.com|BV1\w{9})", url):
        if plugin.show_progress_messages:
            yield event.plain_result("正在下载B站视频并分析内容...")
        download_result = await process_bili_video(url, download_flag=True, quality=plugin.bili_quality, use_login=False, event=None)
        if not download_result or not download_result.get("video_path"):
            yield event.plain_result("B站视频下载失败。")
            return
        video_path = download_result["video_path"]
        try:
            async for resp in analyze_video_with_gemini(plugin, event, video_path, "B站"):
                yield resp
        finally:
            if video_path and os.path.exists(video_path):
                os.remove(video_path)
    else:
        yield event.plain_result("不支持的链接格式。")
