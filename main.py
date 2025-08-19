from astrbot.api.all import *
from astrbot.api.message_components import Node, Plain, Image, Video, Nodes
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
import astrbot.api.message_components as Comp

import re
import json
import os
import httpx
import aiofiles
import asyncio

from douyin_scraper.douyin_parser import DouyinParser
from .mcmod_get import mcmod_parse
from .file_send_server import send_file
from .bili_get import process_bili_video
from .douyin_get import process_douyin
from .auto_delete import delete_old_files
from .xhs_get import xhs_parse
from .gemini_content import process_audio_with_gemini, process_images_with_gemini, process_video_with_gemini
from .videos_cliper import separate_audio_video, extract_frame

@register("hybird_videos_analysis", "喵喵", "可以解析抖音和bili视频", "0.2.8","https://github.com/miaoxutao123/astrbot_plugin_videos_analysis")
class hybird_videos_analysis(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.nap_server_address = config.get("nap_server_address")
        self.nap_server_port = config.get("nap_server_port")
        self.delete_time = config.get("delete_time")
        self.max_video_size = config.get("max_video_size")

        # self.douyin_api_url = config.get("douyin_api_url")
        self.url_video_comprehend = config.get("url_video_comprehend")
        self.gemini_base_url = config.get("gemini_base_url")
        self.upload_video_comprehend = config.get("upload_video_comprehend")
        self.gemini_api_key = config.get("gemini_api_key")

        self.doyin_cookie = config.get("doyin_cookie")

        self.bili_quality = config.get("bili_quality")
        self.bili_reply_mode = config.get("bili_reply_mode")
        self.bili_url_mode = config.get("bili_url_mode")
        self.Merge_and_forward = config.get("Merge_and_forward")
        self.bili_use_login = config.get("bili_use_login")

        self.xhs_reply_mode = config.get("xhs_reply_mode")

    async def _send_file_if_needed(self, file_path: str) -> str:
        """Helper function to send file through NAP server if needed"""
        if self.nap_server_address != "localhost":
            return await send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
        return file_path

    def _create_node(self, event, content):
        """Helper function to create a node with consistent format"""
        return Node(
            uin=event.get_self_id(),
            name="astrbot",
            content=content
        )

    async def _process_multi_part_media(self, event, result, media_type: str):
        """Helper function to process multi-part media (images or videos)"""
        ns = Nodes([])
        for i in range(len(result["media_urls"])):
            file_path = result["media_urls"][i]
            # nap_file_path = await self._send_file_if_needed(file_path)

            if media_type == "image" or file_path.endswith(".jpg"):
                content = [Comp.Image.fromURL(file_path)]
            else:
                content = [Comp.Video.fromURL(file_path)]

            node = self._create_node(event, content)
            ns.nodes.append(node)
        return ns

    async def _process_single_media(self, event, result, media_type: str):
        """Helper function to process single media file"""
        file_path = result["media_urls"][0]
        # nap_file_path = await self._send_file_if_needed(file_path)

        if media_type == "image":
            return [Comp.Image.fromURL(file_path)]
        else:
            return [Comp.Video.fromURL(file_path)]

    async def _cleanup_old_files(self, folder_path: str):
        """Helper function to clean up old files if delete_time is configured"""
        if self.delete_time > 0:
            delete_old_files(folder_path, self.delete_time)

@filter.event_message_type(EventMessageType.ALL)
async def auto_parse_dy(self, event: AstrMessageEvent, *args, **kwargs):
    """
    自动检测消息中是否包含抖音分享链接，并解析。
    """
    cookie = self.doyin_cookie
    message_str = event.message_str
    match = re.search(r"(https?://v\.douyin\.com/[a-zA-Z0-9_\-]+(?:-[a-zA-Z0-9_\-]+)?)", message_str)

    await self._cleanup_old_files("data/plugins/astrbot_plugin_videos_analysis/download_videos/dy")

    if not match:
        return

    parser = DouyinParser(cookie = cookie)

    result = await parser.parse(message_str)

    if not result:
        yield event.plain_result("抱歉，这个抖音链接我不能打开，请检查一下链接是否正确。")
        return

    content_type = result["type"]
    if content_type not in ["video", "image"]:
        logger.info("解析失败，请检查链接是否正确。无法判断链接内容类型。")
        return

    if "media_urls" in result and len(result["media_urls"]) != 1:
        is_multi_part = True

    # 处理多段内容
    if is_multi_part:
        ns = await self._process_multi_part_media(event, result, content_type)
        yield event.chain_result([ns])
    else:
        # 处理单段内容
        content = await self._process_single_media(event, result, content_type)
        if content_type == "image":
            print(f"发送单段图片: {content[0]}")
        yield event.chain_result(content)

@filter.event_message_type(EventMessageType.ALL)
async def auto_parse_bili(self, event: AstrMessageEvent, *args, **kwargs):
    """
    自动检测消息中是否包含bili分享链接，并根据配置进行解析或深度理解。
    """
    message_str = event.message_str
    message_obj_str = str(event.message_obj)

    gemini_base_url = self.gemini_base_url
    url_video_comprehend = self.url_video_comprehend
    gemini_api_key = self.gemini_api_key
    # 检查是否是回复消息，如果是则忽略
    if re.search(r"reply", message_obj_str):
        return

    # 查找Bilibili链接
    match_json = re.search(r"https:\\\\/\\\\/b23\.tv\\\\/[a-zA-Z0-9]+", message_obj_str)
    match_plain = re.search(r"(https?://b23\.tv/[\w]+|https?://bili2233\.cn/[\w]+|BV1\w{9}|av\d+)", message_str)

    if not (match_plain or match_json):
        return

    url = ""
    if match_plain:
        url = match_plain.group(1)
    elif match_json:
        url = match_json.group(0).replace("\\\\", "\\").replace("\\/", "/")

    # 删除过期文件
    await self._cleanup_old_files("data/plugins/astrbot_plugin_videos_analysis/download_videos/bili/")

    # --- 视频深度理解流程 ---
    if url_video_comprehend:
        yield event.plain_result("我看到了一个B站视频链接，让我来仔细分析一下内容，请稍等一下...")

        # --- 获取Gemini API配置 ---
        api_key = None
        proxy_url = None

        # 1. 优先尝试从框架的默认Provider获取
        provider = self.context.provider_manager.curr_provider_inst
        if provider and provider.meta().type == "googlegenai_chat_completion":
            logger.info("检测到框架默认LLM为Gemini，将使用框架配置。")
            api_key = provider.get_current_key()
            # 获取代理URL，支持多种可能的属性名
            proxy_url = getattr(provider, "api_base", None) or getattr(provider, "base_url", None)
            if proxy_url:
                logger.info(f"使用框架配置的代理地址：{proxy_url}")
            else:
                logger.info("框架配置中未找到代理地址，将使用官方API。")

        # 2. 如果默认Provider不是Gemini，尝试查找其他Gemini Provider
        if not api_key:
            logger.info("默认Provider不是Gemini，搜索其他Provider...")
            for provider_name, provider_inst in self.context.provider_manager.providers.items():
                if provider_inst and provider_inst.meta().type == "googlegenai_chat_completion":
                    logger.info(f"在Provider列表中找到Gemini配置：{provider_name}，将使用该配置。")
                    api_key = provider_inst.get_current_key()
                    proxy_url = getattr(provider_inst, "api_base", None) or getattr(provider_inst, "base_url", None)
                    if proxy_url:
                        logger.info(f"使用Provider {provider_name} 的代理地址：{proxy_url}")
                    break

        # 3. 如果框架中没有找到Gemini配置，则回退到插件自身配置
        if not api_key:
            logger.info("框架中未找到Gemini配置，回退到插件自身配置。")
            api_key = gemini_api_key
            proxy_url = gemini_base_url
            if api_key:
                logger.info("使用插件配置的API Key。")
                if proxy_url:
                    logger.info(f"使用插件配置的代理地址：{proxy_url}")
                else:
                    logger.info("插件配置中未设置代理地址，将使用官方API。")

        # 4. 如果最终都没有配置，则提示用户
        if not api_key:
            yield event.plain_result("抱歉，我需要Gemini API才能理解视频，但是没有找到相关配置。\n请在框架中配置Gemini Provider或在插件配置中提供gemini_api_key。")
            return

        video_path = None
        temp_dir = None
        try:
            # 1. 下载视频 (强制不使用登录)
            download_result = await process_bili_video(url, download_flag=True, quality=self.bili_quality, use_login=False, event=None)
            if not download_result or not download_result.get("video_path"):
                yield event.plain_result("抱歉，我无法下载这个视频。")
                return

            video_path = download_result["video_path"]
            temp_dir = os.path.dirname(video_path)
            video_summary = ""
            temp_dir = temp_dir
            # 2. 检查文件大小并选择策略
            video_size_mb = os.path.getsize(video_path) / (1024 * 1024)

            if video_size_mb > 30:
                # --- 大视频处理流程 (音频+关键帧) ---
                yield event.plain_result(f"视频大小为 {video_size_mb:.2f}MB，采用音频+关键帧模式进行分析...")

                # a. 分离音视频
                separated_files = await separate_audio_video(video_path)
                if not separated_files:
                    yield event.plain_result("抱歉，我无法分离这个视频的音频和视频。")
                    return
                audio_path, video_only_path = separated_files

                # b. 分析音频获取描述和时间戳
                description, timestamps, _ = await process_audio_with_gemini(api_key, audio_path, proxy_url)
                if not description or not timestamps:
                    yield event.plain_result("抱歉，我无法分析这个视频的音频内容。")
                    return

                # c. 提取关键帧并记录时间戳
                image_paths = []
                ts_and_paths = []
                for ts in timestamps:
                    frame_path = await extract_frame(video_only_path, ts)
                    if frame_path:
                        image_paths.append(frame_path)
                        ts_and_paths.append((ts, frame_path))

                if not image_paths:
                    # 如果没有提取到关键帧，仅使用音频描述
                    video_summary = description
                else:
                    # d. 结合音频描述和关键帧进行综合理解
                    prompt = f"这是关于一个视频的摘要和一些从该视频中提取的关键帧。视频摘要如下：\n\n{description}\n\n请结合摘要和这些关键帧，对整个视频内容进行一个全面、生动的总结。"
                    summary_tuple = await process_images_with_gemini(api_key, prompt, image_paths, proxy_url)
                    video_summary = summary_tuple[0] if summary_tuple else "无法生成最终摘要。"

                # 新增：将提取的关键帧和时间戳发送给用户
                if ts_and_paths:
                    key_frames_nodes = Nodes([])
                    key_frames_nodes.nodes.append(self._create_node(event, [Plain("以下是视频的关键时刻：")]))
                    for ts, frame_path in ts_and_paths:
                        # 确保文件可以通过网络访问
                        nap_frame_path = await self._send_file_if_needed(frame_path)
                        node_content = [
                            Image.fromFileSystem(nap_frame_path),
                            Plain(f"时间点: {ts}")
                        ]
                        key_frames_nodes.nodes.append(self._create_node(event, node_content))
                    yield event.chain_result([key_frames_nodes])

            else:
                # --- 小视频处理流程 (直接上传) ---
                yield event.plain_result(f"视频大小为 {video_size_mb:.2f}MB，直接上传视频进行分析...")
                video_prompt = "请详细描述这个视频的内容，包括场景、人物、动作和传达的核心信息。"
                video_response = await process_video_with_gemini(api_key, video_prompt, video_path, proxy_url)
                video_summary = video_response[0] if video_response and video_response[0] else "抱歉，我暂时无法理解这个视频内容。"

            # 3. 将摘要提交给框架LLM进行评价
            if video_summary:
                # 获取当前对话和人格信息
                curr_cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
                conversation = None
                context = []
                if curr_cid:
                    conversation = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, curr_cid)
                    if conversation:
                        context = json.loads(conversation.history)

                # 获取当前人格设定
                provider = self.context.provider_manager.curr_provider_inst
                current_persona = None
                if provider and hasattr(provider, 'personality'):
                    current_persona = provider.personality
                elif self.context.provider_manager.selected_default_persona:
                    current_persona = self.context.provider_manager.selected_default_persona

                # 构造包含人格和视频摘要的提示
                persona_prompt = ""
                if current_persona and hasattr(current_persona, 'prompt'):
                    persona_prompt = f"请保持你的人格设定：{current_persona.prompt}\n\n"

                final_prompt = f"{persona_prompt}我刚刚分析了这个B站视频的内容：\n\n{video_summary}\n\n请基于这个视频内容，结合你的人格特点，自然地发表你的看法或评论。不要说这是我转述给你的，请像你亲自观看了这个视频一样回应。"

                yield event.request_llm(
                    prompt=final_prompt,
                    session_id=curr_cid,
                    contexts=context,
                    conversation=conversation
                )
            else:
                yield event.plain_result("抱歉，我无法理解这个视频的内容。")

        except Exception as e:
            logger.error(f"处理B站视频理解时发生错误: {e}")
            yield event.plain_result("抱歉，处理这个视频时出现了一些问题。")
        finally:
            # 4. 清理临时文件
            if video_path and os.path.exists(video_path):
                # 之前这里会把整个bili文件夹删了，现在只删除本次下载的视频
                os.remove(video_path)
                logger.info(f"已清理临时文件: {video_path}")
        return # 结束函数，不执行后续的常规解析

    # --- 常规视频解析流程 (如果深度理解未开启) ---
    qulity = self.bili_quality
    reply_mode = self.bili_reply_mode
    url_mode = self.bili_url_mode
    use_login = self.bili_use_login
    videos_download = reply_mode in [2, 3, 4]
    zhuanfa = self.Merge_and_forward

    result = await process_bili_video(url, download_flag=videos_download, quality=qulity, use_login=use_login, event=None)

    if result:
        file_path = result.get("video_path")
        media_component = None
        if file_path and os.path.exists(file_path):
            nap_file_path = await send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port) if self.nap_server_address != "localhost" else file_path
            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            if file_size_mb > 200:
                media_component = Comp.File(file=nap_file_path, name=os.path.basename(nap_file_path))
            else:
                media_component = Comp.Video.fromFileSystem(path = nap_file_path)

        info_text = (
            f"📜 视频标题：{result['title']}\n"
            f"👀 观看次数：{result['view_count']}\n"
            f"👍 点赞次数：{result['like_count']}\n"
            f"💰 投币次数：{result['coin_count']}\n"
            f"📂 收藏次数：{result['favorite_count']}\n"
            f"💬 弹幕量：{result['danmaku_count']}\n"
            f"⏳ 视频时长：{int(result['duration'] / 60)}分{result['duration'] % 60}秒\n"
        )
        if url_mode:
            info_text += f"🎥 视频直链：{result['direct_url']}\n"
        info_text += f"🧷 原始链接：https://www.bilibili.com/video/{result['bvid']}"

        # 根据回复模式构建响应
        if reply_mode == 0: # 纯文本
            content = [Comp.Plain(info_text)]
        elif reply_mode == 1: # 带图片
            content = [Comp.Image.fromURL(result["cover"]),Comp.Plain(info_text)]
        elif reply_mode == 2: # 带视频
            content = [media_component, Comp.Plain(info_text)] if media_component else [Plain(info_text)]
        elif reply_mode == 3: # 完整
            content = [Comp.Image(file=result["cover"]), media_component,Comp.Plain(info_text)]
            content = [c for c in content if c] # 移除None
        elif reply_mode == 4: # 仅视频
            content = [media_component] if media_component else []
        else:
            content = []

        if content:
            if zhuanfa:
                # 将所有内容放入一个Node中进行合并转发
                flat_content = []
                for item in content:
                    if isinstance(item, list):
                        flat_content.extend(item)
                    else:
                        flat_content.append(item)
                node = Node(uin=event.get_self_id(), name="astrbot", content=flat_content)
                yield event.chain_result([node])
            else:
                # 逐条发送
                for item in content:
                    yield event.chain_result([item])

# @filter.event_message_type(EventMessageType.ALL)
# async def auto_parse_ks(self, event: AstrMessageEvent, *args, **kwargs):
#     """
#     自动检测消息中是否包含快手分享链接，并解析。
#     """
#     api_url = "https://api.kxzjoker.cn/api/jiexi_video"
#     message_str = event.message_str
#     match = re.search(r"(https?://v\.k\.ua\.com/[a-zA-Z0-9_\-]+(?:-[a-zA-Z0-9_\-]+)?)", message_str)

@filter.event_message_type(EventMessageType.ALL)
async def auto_parse_xhs(self, event: AstrMessageEvent, *args, **kwargs):
    """
    自动检测消息中是否包含小红书分享链接，并解析。
    """
    replay_mode = self.xhs_reply_mode

    images_pattern = r"(https?://xhslink\.com/[a-zA-Z0-9/]+)"
    video_pattern = r"(https?://www\.xiaohongshu\.com/discovery/item/[a-zA-Z0-9]+)"

    message_str = event.message_str
    message_obj_str = str(event.message_obj)

    # 搜索匹配项
    image_match = re.search(images_pattern, message_obj_str) or re.search(images_pattern, message_str)
    video_match = re.search(video_pattern, message_obj_str) or re.search(video_pattern, message_str)
    contains_reply = re.search(r"reply", message_obj_str)

    if contains_reply:
        return

    # 处理图片链接
    if image_match:
        result = await xhs_parse(image_match.group(1))
        ns = Nodes([]) if replay_mode else None
        title_node = self._create_node(event, [Plain(result["title"])])

        if replay_mode:
            ns.nodes.append(title_node)
        else:
            yield event.chain_result([Plain(result["title"])])

        for image_url in result["urls"]:
            image_node = self._create_node(event, [Image.fromURL(image_url)])
            if replay_mode:
                ns.nodes.append(image_node)
            else:
                yield event.chain_result([Image.fromURL(image_url)])

        if replay_mode:
            yield event.chain_result([ns])

    # 处理视频链接
    if video_match:
        result = await xhs_parse(video_match.group(1))
        ns = Nodes([]) if replay_mode else None
        title_node = self._create_node(event, [Plain(result["title"])])

        if "video_sizes" in result:
            if replay_mode:
                ns.nodes.append(title_node)
            else:
                yield event.chain_result([Plain(result["title"])])

            for url in result["urls"]:
                video_node = self._create_node(event, [Video.fromURL(url)])
                if replay_mode:
                    ns.nodes.append(video_node)
                else:
                    yield event.chain_result([video_node])
        else:
            # 处理图片内容
            if replay_mode:
                ns.nodes.append(title_node)
            else:
                yield event.chain_result([Plain(result["title"])])

            for image_url in result["urls"]:
                image_node = self._create_node(event, [Image.fromURL(image_url)])
                if replay_mode:
                    ns.nodes.append(image_node)
                else:
                    yield event.chain_result([Image.fromURL(image_url)])

        if replay_mode:
            yield event.chain_result([ns])

@filter.event_message_type(EventMessageType.ALL)
async def auto_parse_mcmod(self, event: AstrMessageEvent, *args, **kwargs):
    """
    自动检测消息中是否包含mcmod分享链接，并解析。
    """
    mod_pattern = r"(https?://www\.mcmod\.cn/class/\d+\.html)"
    modpack_pattern = r"(https?://www\.mcmod\.cn/modpack/\d+\.html)"

    message_str = event.message_str
    message_obj_str = str(event.message_obj)

    # 搜索匹配项
    match = (re.search(mod_pattern, message_obj_str) or
             re.search(mod_pattern, message_str) or
             re.search(modpack_pattern, message_obj_str) or
             re.search(modpack_pattern, message_str))

    contains_reply = re.search(r"reply", message_obj_str)

    if not match or contains_reply:
        return

    logger.info(f"解析MCmod链接: {match.group(1)}")
    results = await mcmod_parse(match.group(1))

    if not results or not results[0]:
        yield event.plain_result("抱歉，我不能打开这个MC百科链接，请检查一下链接是否正确。")
        return

    result = results[0]
    logger.info(f"解析结果: {result}")

    # 使用合并转发发送解析内容
    ns = Nodes([])

    # 添加名称
    ns.nodes.append(self._create_node(event, [Plain(f"📦 {result.name}")]))

    # 添加图标
    if result.icon_url:
        ns.nodes.append(self._create_node(event, [Image.fromURL(result.icon_url)]))

    # 添加分类
    if result.categories:
        categories_str = "/".join(result.categories)
        ns.nodes.append(self._create_node(event, [Plain(f"🏷️ 分类: {categories_str}")]))

    # 添加描述
    if result.description:
        ns.nodes.append(self._create_node(event, [Plain(f"📝 描述:\n{result.description}")]))

    # 添加描述图片
    if result.description_images:
        for img_url in result.description_images:
            ns.nodes.append(self._create_node(event, [Image.fromURL(img_url)]))

    yield event.chain_result([ns])

@filter.event_message_type(EventMessageType.ALL)
async def process_direct_video(self, event: AstrMessageEvent, *args, **kwargs):
    """
    处理用户直接发送的视频消息进行理解
    """
    # 检查是否开启了视频理解功能
    if not self.url_video_comprehend:
        return

    # 检查消息是否包含视频
    if not event.message_obj or not hasattr(event.message_obj, 'message'):
        return

    # 查找视频消息
    video_url = None
    video_filename = None
    video_size = None

    # 从raw_message中提取视频信息
    raw_message = event.message_obj.raw_message
    if 'message' in raw_message:
        for msg_item in raw_message['message']:
            if msg_item.get('type') == 'video':
                video_data = msg_item.get('data', {})
                video_url = video_data.get('url')
                video_filename = video_data.get('file', 'unknown.mp4')
                video_size = video_data.get('file_size')
                break

    if not video_url:
        return

    logger.info(f"检测到用户发送的视频消息，开始处理: {video_filename}")
    yield event.plain_result("收到了你的视频，让我来看看里面都有什么内容...")

    # --- 获取Gemini API配置 ---
    api_key = None
    proxy_url = None

    # 1. 优先尝试从框架的默认Provider获取
    provider = self.context.provider_manager.curr_provider_inst
    if provider and provider.meta().type == "googlegenai_chat_completion":
        logger.info("检测到框架默认LLM为Gemini，将使用框架配置。")
        api_key = provider.get_current_key()
        proxy_url = getattr(provider, "api_base", None) or getattr(provider, "base_url", None)
        if proxy_url:
            logger.info(f"使用框架配置的代理地址：{proxy_url}")
        else:
            logger.info("框架配置中未找到代理地址，将使用官方API。")

    # 2. 如果默认Provider不是Gemini，尝试查找其他Gemini Provider
    if not api_key:
        logger.info("默认Provider不是Gemini，搜索其他Provider...")
        for provider_name, provider_inst in self.context.provider_manager.providers.items():
            if provider_inst and provider_inst.meta().type == "googlegenai_chat_completion":
                logger.info(f"在Provider列表中找到Gemini配置：{provider_name}，将使用该配置。")
                api_key = provider_inst.get_current_key()
                proxy_url = getattr(provider_inst, "api_base", None) or getattr(provider_inst, "base_url", None)
                if proxy_url:
                    logger.info(f"使用Provider {provider_name} 的代理地址：{proxy_url}")
                break

    # 3. 如果框架中没有找到Gemini配置，则回退到插件自身配置
    if not api_key:
        logger.info("框架中未找到Gemini配置，回退到插件自身配置。")
        api_key = self.gemini_api_key
        proxy_url = self.gemini_base_url
        if api_key:
            logger.info("使用插件配置的API Key。")
            if proxy_url:
                logger.info(f"使用插件配置的代理地址：{proxy_url}")
            else:
                logger.info("插件配置中未设置代理地址，将使用官方API。")

    # 4. 如果最终都没有配置，则提示用户
    if not api_key:
        yield event.plain_result("❌ 视频理解失败：\n未在框架中找到Gemini配置，且插件配置中缺少gemini_api_key。\n请在框架中配置Gemini Provider或在插件配置中提供gemini_api_key。")
        return

    video_path = None
    try:
        # 1. 下载视频到本地
        download_dir = "data/plugins/astrbot_plugin_videos_analysis/download_videos/direct/"
        os.makedirs(download_dir, exist_ok=True)

        video_path = os.path.join(download_dir, video_filename)

        logger.info(f"开始下载视频: {video_url}")
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.get(video_url)
            response.raise_for_status()

            async with aiofiles.open(video_path, 'wb') as f:
                await f.write(response.content)

        logger.info(f"视频下载完成: {video_path}")

        # 清理旧文件
        await self._cleanup_old_files(download_dir)

        # 2. 检查文件大小并选择处理策略
        video_size_mb = os.path.getsize(video_path) / (1024 * 1024)
        video_summary = ""

        if video_size_mb > 30:
            # --- 大视频处理流程 (音频+关键帧) ---
            yield event.plain_result(f"视频大小为 {video_size_mb:.2f}MB，采用音频+关键帧模式进行分析...")

            # a. 分离音视频
            separated_files = await separate_audio_video(video_path)
            if not separated_files:
                yield event.plain_result("音视频分离失败。")
                return
            audio_path, video_only_path = separated_files

            # b. 分析音频获取描述和时间戳
            description, timestamps, _ = await process_audio_with_gemini(api_key, audio_path, proxy_url)
            if not description or not timestamps:
                yield event.plain_result("抱歉，我无法分析这个视频的音频内容。")
                return

            # c. 提取关键帧并记录时间戳
            image_paths = []
            ts_and_paths = []
            for ts in timestamps:
                frame_path = await extract_frame(video_only_path, ts)
                if frame_path:
                    image_paths.append(frame_path)
                    ts_and_paths.append((ts, frame_path))

            if not image_paths:
                # 如果没有提取到关键帧，仅使用音频描述
                video_summary = description
            else:
                # d. 结合音频描述和关键帧进行综合理解
                image_prompt = f"这是关于一个视频的摘要和一些从该视频中提取的关键帧。视频摘要如下：\n\n{description}\n\n请结合摘要和这些关键帧，对整个视频内容进行一个全面、生动的总结。"
                image_response = await process_images_with_gemini(api_key, image_prompt, image_paths, proxy_url)
                video_summary = image_response[0] if image_response and image_response[0] else "无法生成最终摘要。"

            # 发送关键帧和时间戳给用户
            if ts_and_paths:
                key_frames_nodes = Nodes([])
                key_frames_nodes.nodes.append(self._create_node(event, [Plain("以下是视频的关键时刻：")]))
                for ts, frame_path in ts_and_paths:
                    nap_frame_path = await self._send_file_if_needed(frame_path)
                    node_content = [
                        Image.fromFileSystem(nap_frame_path),
                        Plain(f"时间点: {ts}")
                    ]
                    key_frames_nodes.nodes.append(self._create_node(event, node_content))
                yield event.chain_result([key_frames_nodes])

        else:
            # --- 小视频处理流程 (直接上传) ---
            yield event.plain_result(f"视频大小为 {video_size_mb:.2f}MB，直接上传视频进行分析...")
            video_prompt = "请详细描述这个视频的内容，包括场景、人物、动作和传达的核心信息。"
            video_response = await process_video_with_gemini(api_key, video_prompt, video_path, proxy_url)
            video_summary = video_response[0] if video_response and video_response[0] else "抱歉，我暂时无法理解这个视频内容。"

        # 3. 将摘要提交给框架LLM进行评价
        if video_summary:
            # 获取当前对话和人格信息
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            conversation = None
            context = []
            if curr_cid:
                conversation = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, curr_cid)
                if conversation:
                    context = json.loads(conversation.history)

            # 获取当前人格设定
            provider = self.context.provider_manager.curr_provider_inst
            current_persona = None
            if provider and hasattr(provider, 'personality'):
                current_persona = provider.personality
            elif self.context.provider_manager.selected_default_persona:
                current_persona = self.context.provider_manager.selected_default_persona

            # 构造包含人格和视频摘要的提示
            persona_prompt = ""
            if current_persona and hasattr(current_persona, 'prompt'):
                persona_prompt = f"请保持你的人格设定：{current_persona.prompt}\n\n"

            final_prompt = f"{persona_prompt}我刚刚看了你发送的视频，内容是这样的：\n\n{video_summary}\n\n请基于这个视频内容，结合你的人格特点，自然地回应我。就像你亲自看过这个视频一样。"

            yield event.request_llm(
                prompt=final_prompt,
                session_id=curr_cid,
                contexts=context,
                conversation=conversation
            )
        else:
            yield event.plain_result("未能生成视频摘要，无法进行评论。")

    except Exception as e:
        logger.error(f"处理视频消息时发生错误: {e}")
        yield event.plain_result("处理视频时发生未知错误。")
    finally:
        # 4. 清理临时文件
        if video_path and os.path.exists(video_path):
            os.remove(video_path)
            logger.info(f"已清理临时文件: {video_path}")
