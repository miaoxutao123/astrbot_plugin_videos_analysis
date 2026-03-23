"""
B站被动解析处理器
"""

import os
import re

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.message_components import Image, Nodes, Plain
from astrbot.core.message.message_event_result import MessageChain

from ..services.bilibili_service import process_bili_video
from ..utils.config_helper import (
    DOWNLOAD_DIR_BILI,
    create_node,
    send_file_if_needed,
    should_comprehend,
    cleanup_old_files,
)
from ..utils.file_utils import send_file


async def handle_bilibili_parse(plugin, event):
    """B站被动解析核心逻辑"""
    message_str = event.message_str
    message_obj_str = str(event.message_obj)

    # 检查是否是回复消息
    if re.search(r"reply", message_obj_str):
        return

    # 查找 Bilibili 链接
    match_json = re.search(r"https:\\\\/\\\\/(b23\.tv|www\.bilibili\.com)\\\\/[a-zA-Z0-9/]+", message_obj_str)
    match_plain = re.search(
        r"(https?://b23\.tv/[\w]+|https?://bili2233\.cn/[\w]+|https?://www\.bilibili\.com/video/BV1\w{9}|https?://www\.bilibili\.com/video/av\d+|BV1\w{9}|av\d+)",
        message_str,
    )

    if not (match_plain or match_json):
        return

    url = ""
    if match_plain:
        url = match_plain.group(0)
    elif match_json:
        url = match_json.group(0).replace("\\\\", "\\").replace("\\/", "/")

    await cleanup_old_files(DOWNLOAD_DIR_BILI, plugin.delete_time)

    # 视频深度理解流程
    do_comprehend = should_comprehend(event, plugin.url_video_comprehend, plugin.private_auto_comprehend)
    if do_comprehend:
        if plugin.show_progress_messages:
            yield event.plain_result("我看到了一个B站视频链接，让我来仔细分析一下内容，请稍等一下...")

        from .video_handler import analyze_video_with_gemini

        video_path = None
        try:
            download_result = await process_bili_video(url, download_flag=True, quality=16, use_login=plugin.bili_use_login, event=None)
            if not download_result or not download_result.get("video_path"):
                yield event.plain_result("抱歉，我无法下载这个视频。")
                return

            video_path = download_result["video_path"]

            async for response in analyze_video_with_gemini(plugin, event, video_path, "B站"):
                yield response

        except Exception as e:
            logger.error(f"处理B站视频理解时发生错误: {e}")
            yield event.plain_result("抱歉，处理这个视频时出现了一些问题。")
        finally:
            if video_path and os.path.exists(video_path):
                os.remove(video_path)
        return

    # 常规视频解析流程
    quality = plugin.bili_quality
    reply_mode = plugin.bili_reply_mode
    url_mode = plugin.bili_url_mode
    use_login = plugin.bili_use_login
    videos_download = reply_mode in [2, 3, 4]
    merge_forward = plugin.Merge_and_forward

    result = await process_bili_video(url, download_flag=videos_download, quality=quality, use_login=use_login, event=None)

    if result:
        file_path = result.get("video_path")
        media_component = None
        if file_path and os.path.exists(file_path):
            nap_file_path = await send_file(file_path, HOST=plugin.nap_server_address, PORT=plugin.nap_server_port) if plugin.nap_server_address != "localhost" else file_path
            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            if file_size_mb > plugin.max_video_size:
                media_component = Comp.File(file=nap_file_path, name=os.path.basename(nap_file_path))
            else:
                media_component = Comp.Video.fromFileSystem(path=nap_file_path)

        # 构建信息文本
        try:
            stats = result.get("stats", {})
            info_text = (
                f"📜 视频标题：{result.get('title', '未知标题')}\n"
                f"👀 观看次数：{result.get('view_count', 0)}\n"
                f"👍 点赞次数：{result.get('like_count', 0)}\n"
                f"💰 投币次数：{result.get('coin_count', 0)}\n"
                f"📂 收藏次数：{result.get('favorite_count', 0)}\n"
                f"💬 弹幕量：{result.get('danmaku_count', 0)}\n"
                f"⏳ 视频时长：{int(result.get('duration', 0) / 60)}分{result.get('duration', 0) % 60}秒\n"
            )
            if url_mode:
                info_text += f"🎥 视频直链：{result.get('direct_url', '无')}\n"
            info_text += f"🧷 原始链接：https://www.bilibili.com/video/{result.get('bvid', 'unknown')}"
        except Exception as e:
            logger.error(f"构建B站信息文本时出错: {e}")
            info_text = f"B站视频信息获取失败: {result.get('title', '未知视频')}"

        # 根据回复模式构建响应
        send_chain = []
        if reply_mode == 0:
            send_chain = [Comp.Plain(info_text)]
        elif reply_mode == 1:
            cover_url = result.get("cover")
            if cover_url:
                if merge_forward:
                    ns = Nodes([])
                    ns.nodes.append(create_node(event, [Comp.Image.fromURL(cover_url)]))
                    ns.nodes.append(create_node(event, [Comp.Plain(info_text)]))
                    send_chain = [ns]
                else:
                    await event.send(MessageChain([Comp.Image.fromURL(cover_url)]))
                    send_chain = [Comp.Plain(info_text)]
            else:
                send_chain = [Comp.Plain("封面图片获取失败\n" + info_text)]
        elif reply_mode == 2:
            if media_component:
                if merge_forward:
                    await event.send(MessageChain([Comp.Plain(info_text)]))
                    send_chain = [media_component]
                else:
                    send_chain = [media_component]
            else:
                send_chain = [Comp.Plain(info_text)]
        elif reply_mode == 3:
            cover_url = result.get("cover")
            if merge_forward:
                if cover_url:
                    ns = Nodes([])
                    ns.nodes.append(create_node(event, [Comp.Image.fromURL(cover_url)]))
                    ns.nodes.append(create_node(event, [Comp.Plain(info_text)]))
                    await event.send(MessageChain([ns]))
                else:
                    await event.send(MessageChain([Comp.Plain("封面图片获取失败\n" + info_text)]))
                send_chain = [media_component] if media_component else [Comp.Plain(info_text)]
            else:
                if cover_url:
                    await event.send(MessageChain([Comp.Image.fromURL(cover_url)]))
                else:
                    await event.send(MessageChain([Comp.Plain("封面图片获取失败")]))
                await event.send(MessageChain([Comp.Plain(info_text)]))
                send_chain = [media_component] if media_component else []
        elif reply_mode == 4:
            if media_component:
                send_chain = [media_component]

        if send_chain:
            try:
                await event.send(MessageChain(send_chain))
            except Exception as e:
                logger.error(f"发送消息失败: {e}")
