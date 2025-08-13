from astrbot.api.all import *
from astrbot.api.message_components import Node, Plain, Image, Video, Nodes, File
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
import re
import json
import os
from pathlib import Path

from .mcmod_get import mcmod_parse
from .file_send_server import send_file
from .bili_get import process_bili_video
from .douyin_get import process_douyin
from .auto_delete import delete_old_files
from .xhs_get import xhs_parse
import shutil
from .videos_cliper import separate_audio_video, extract_frame

@register("hybird_videos_analysis", "喵喵", "可以解析抖音和bili视频", "0.2.8","https://github.com/miaoxutao123/astrbot_plugin_videos_analysis")
class hybird_videos_analysis(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.nap_server_address = config.get("nap_server_address")
        self.nap_server_port = config.get("nap_server_port")
        self.delete_time = config.get("delete_time")
        self.max_video_size = config.get("max_video_size")

        self.douyin_api_url = config.get("douyin_api_url")
        self.url_video_comprehend = config.get("url_video_comprehend")
        self.gemini_base_url = config.get("gemini_base_url")
        self.upload_video_comprehend = config.get("upload_video_comprehend")
        self.gemini_api_key = config.get("gemini_api_key")

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
        for i in range(result["count"]):
            file_path = result["save_path"][i]
            nap_file_path = await self._send_file_if_needed(file_path)

            if media_type == "image" or file_path.endswith(".jpg"):
                content = [Image.fromFileSystem(nap_file_path)]
            else:
                content = [Video.fromFileSystem(nap_file_path)]

            node = self._create_node(event, content)
            ns.nodes.append(node)
        return ns

    async def _process_single_media(self, event, result, media_type: str):
        """Helper function to process single media file"""
        file_path = result["save_path"][0]
        nap_file_path = await self._send_file_if_needed(file_path)

        if media_type == "image":
            return [Image.fromFileSystem(nap_file_path)]
        else:
            return [Video.fromFileSystem(nap_file_path)]

    async def _cleanup_old_files(self, folder_path: str):
        """Helper function to clean up old files if delete_time is configured"""
        if self.delete_time > 0:
            delete_old_files(folder_path, self.delete_time)
            
    @filter.event_message_type(EventMessageType.ALL)
    async def auto_parse_dy(self, event: AstrMessageEvent, *args, **kwargs):
        """
        自动检测消息中是否包含抖音分享链接，并解析。
        """
        api_url = self.douyin_api_url
        message_str = event.message_str
        match = re.search(r"(https?://v\.douyin\.com/[a-zA-Z0-9_\-]+(?:-[a-zA-Z0-9_\-]+)?)", message_str)

        await self._cleanup_old_files("data/plugins/astrbot_plugin_videos_analysis/download_videos/dy")

        if not match:
            return

        url = match.group(1)
        result = await process_douyin(url, api_url)

        if not result:
            yield event.plain_result("检测到抖音链接，但解析失败，请检查链接是否正确。")
            return

        content_type = result["type"]
        if content_type not in ["video", "image"]:
            print("解析失败，请检查链接是否正确。")
            return

        # 处理多段内容
        if result["is_multi_part"]:
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
        if self.url_video_comprehend:
            yield event.plain_result("检测到B站视频链接，正在进行深度理解，请稍候...")
            try:
                # 1. 下载视频 (强制不使用登录)
                download_result = await process_bili_video(url, download_flag=True, quality=self.bili_quality, use_login=False, event=None)
                if not download_result or not download_result.get("video_path"):
                    yield event.plain_result("视频下载失败，无法进行理解。")
                    return
                video_path = download_result["video_path"]
                # 2. 调用重构后的核心处理函数
                async for result in self._perform_deep_comprehension(event, video_path):
                    yield result
            except Exception as e:
                logger.error(f"处理B站链接深度理解时发生错误: {e}")
                yield event.plain_result("处理B站链接时发生未知错误。")
            return

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
                    media_component = File(name=os.path.basename(nap_file_path), file=nap_file_path)
                else:
                    media_component = Video.fromFileSystem(nap_file_path)

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
                content = [Plain(info_text)]
            elif reply_mode == 1: # 带图片
                content = [Image(file=result["cover"]), Plain(info_text)]
            elif reply_mode == 2: # 带视频
                content = [media_component, Plain(info_text)] if media_component else [Plain(info_text)]
            elif reply_mode == 3: # 完整
                content = [Image(file=result["cover"]), media_component, Plain(info_text)]
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
            yield event.plain_result("解析MC百科信息失败，请检查链接是否正确。")
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

    async def _perform_deep_comprehension(self, event: AstrMessageEvent, video_path: str):
        """对给定的视频文件执行深度理解流程"""
        provider = self.context.provider_manager.curr_provider_inst
        if not (provider and provider.meta().type == "googlegenai_chat_completion"):
            yield event.plain_result("❌ 视频深度理解失败：\n框架的默认LLM不是Gemini，请配置一个Gemini Provider并将其设置为默认。")
            return

        try:
            video_summary = ""
            video_size_mb = os.path.getsize(video_path) / (1024 * 1024)
            ts_and_paths = [] # 初始化以备finally使用

            if video_size_mb > 30:
                # --- 大视频处理流程 (音频+关键帧) ---
                yield event.plain_result(f"视频大小为 {video_size_mb:.2f}MB，采用音频+关键帧模式进行分析...")
                separated_files = await separate_audio_video(video_path)
                if not separated_files:
                    yield event.plain_result("音视频分离失败。")
                    return
                audio_path, video_only_path = separated_files

                audio_prompt = """
    你是一位专业的视频内容分析师。你的任务是分析所提供的音频，并完成以下两项工作：
    1.  为整个音频内容撰写一段简洁、全面的文字描述。
    2.  识别出音频中暗示着重要视觉事件发生的关键时刻（例如：突然的巨响、对话的转折点、情绪高潮等），并提供这些时刻的时间戳。

    请将你的回答严格格式化为单个JSON对象，该对象包含两个键：
    -   `"description"`: 一个包含音频内容描述的字符串。
    -   `"timestamps"`: 一个由 "HH:MM:SS" 格式的时间戳字符串组成的数组。
    """
                audio_response = await provider.text_chat(prompt=audio_prompt, audio_path=audio_path)
                
                description = ""
                timestamps = []
                try:
                    cleaned_response = audio_response.completion_text.strip().removeprefix("```json").removesuffix("```").strip()
                    audio_data = json.loads(cleaned_response)
                    description = audio_data.get("description", "")
                    timestamps = audio_data.get("timestamps", [])
                except (json.JSONDecodeError, AttributeError):
                    yield event.plain_result("音频分析失败：无法解析模型返回的JSON。")
                    return

                if not description or not timestamps:
                    yield event.plain_result("音频分析失败，无法提取关键信息。")
                    return

                image_paths = []
                for ts in timestamps:
                    frame_path = await extract_frame(video_only_path, ts)
                    if frame_path:
                        image_paths.append(frame_path)
                        ts_and_paths.append((ts, frame_path))
                
                if not image_paths:
                    video_summary = description
                else:
                    image_prompt = f"这是关于一个视频的摘要和一些从该视频中提取的关键帧。视频摘要如下：\n\n{description}\n\n请结合摘要和这些关键帧，对整个视频内容进行一个全面、生动的总结。"
                    image_urls = [Path(p).as_uri() for p in image_paths]
                    image_response = await provider.text_chat(prompt=image_prompt, image_urls=image_urls)
                    video_summary = image_response.completion_text if image_response else "无法生成最终摘要。"

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
                video_response = await provider.text_chat(prompt=video_prompt, video_path=video_path)
                video_summary = video_response.completion_text if video_response else "视频分析失败。"

            if video_summary:
                summary_message_for_context = f"（系统提示：我刚刚深度分析了一个视频，以下是视频内容的摘要：\n\n{video_summary}）"
                curr_cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
                conversation = None
                context = []
                if curr_cid:
                    conversation = await self.context.conversation_manager.get_conversation(event.unified_msg_origin, curr_cid)
                    if conversation:
                        context = json.loads(conversation.history)
                context.append({"role": "user", "content": summary_message_for_context})
                await self.context.conversation_manager.update_conversation(
                    unified_msg_origin=event.unified_msg_origin,
                    conversation_id=curr_cid,
                    history=context
                )
                if conversation:
                    conversation.history = json.dumps(context)
                final_prompt = "请你基于以上内容（特别是刚刚提供的视频摘要），并结合你当前的人设和对话上下文，对这个视频发表一下你的看法或评论。"
                yield event.request_llm(
                    prompt=final_prompt,
                    session_id=curr_cid,
                    contexts=context,
                    conversation=conversation
                )
            else:
                yield event.plain_result("未能生成视频摘要，无法进行评论。")

        except Exception as e:
            logger.error(f"处理视频深度理解时发生错误: {e}")
            yield event.plain_result("处理视频时发生未知错误。")
        finally:
            # 清理临时文件
            if video_path and os.path.exists(video_path):
                base, _ = os.path.splitext(video_path)
                related_paths = [f"{base}_audio.mp3", f"{base}_video.mp4"]
                for ts, p in ts_and_paths:
                    related_paths.append(p)
                
                related_paths.append(video_path)

                for p in related_paths:
                    if os.path.exists(p):
                        try:
                            if os.path.isdir(p):
                                shutil.rmtree(p)
                            else:
                                os.remove(p)
                            logger.info(f"已清理伴生文件/目录: {p}")
                        except OSError as e:
                            logger.error(f"清理伴生文件/目录失败: {e}")

    @filter.event_message_type(EventMessageType.ALL)
    async def on_video_message(self, event: AstrMessageEvent, *args, **kwargs):
        """自动检测用户发送的视频并进行深度理解"""
        if not self.upload_video_comprehend:
            return

        # 检查消息中是否包含视频
        video_component = None
        for component in event.message_chain:
            if isinstance(component, Video):
                video_component = component
                break
        
        if not video_component:
            return

        # 检查是否是回复消息或包含文本，如果是则忽略，避免干扰
        if event.message_str or re.search(r"reply", str(event.message_obj)):
            return

        yield event.plain_result("检测到视频，正在进行深度理解，请稍候...")
        
        # 下载视频
        video_path = None
        try:
            # Video组件的file属性通常是本地路径或可下载的URL
            # 这里需要一个健壮的下载逻辑
            import httpx
            from urllib.parse import urlparse
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            
            video_url = None
            # 尝试从原始消息中解析URL
            raw_msg = event.raw_message
            if hasattr(raw_msg, 'message'):
                for msg_part in raw_msg.message:
                    if msg_part.get('type') == 'video' and msg_part.get('data', {}).get('url'):
                        video_url = msg_part['data']['url']
                        break
            
            if not video_url:
                logger.warning("在消息中未找到可下载的视频URL")
                return

            temp_dir = os.path.join(get_astrbot_data_path(), "videos_analysis", "temp_downloads")
            os.makedirs(temp_dir, exist_ok=True)
            
            # 从URL中提取文件名或生成一个
            file_name = os.path.basename(urlparse(video_url).path) or f"{event.message_id}.mp4"
            video_path = os.path.join(temp_dir, file_name)

            async with httpx.AsyncClient() as client:
                async with client.stream("GET", video_url, timeout=120.0) as response:
                    response.raise_for_status()
                    with open(video_path, "wb") as f:
                        async for chunk in response.aiter_bytes():
                            f.write(chunk)
            
            logger.info(f"视频已下载到: {video_path}")

            # 调用核心处理函数
            async for result in self._perform_deep_comprehension(event, video_path):
                yield result

        except Exception as e:
            logger.error(f"处理用户上传的视频时发生错误: {e}")
            yield event.plain_result("处理您发送的视频时发生未知错误。")
        finally:
            # 确保下载的临时文件也被清理
            if video_path and os.path.exists(video_path):
                try:
                    os.remove(video_path)
                    logger.info(f"已清理下载的临时视频: {video_path}")
                except OSError as e:
                    logger.error(f"清理下载的临时视频失败: {e}")
