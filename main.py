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
import time
import asyncio
import random
import threading
from typing import Dict, Optional, Tuple
from typing import Dict
from cachetools import TTLCache

from .douyin_scraper.douyin_parser import DouyinParser
from .mcmod_get import mcmod_parse
from .file_send_server import send_file
from .bili_get import process_bili_video
from .douyin_download import download
from .auto_delete import delete_old_files
from .xhs_get import xhs_parse
from .gemini_content import process_audio_with_gemini, process_images_with_gemini, process_video_with_gemini
from .videos_cliper import separate_audio_video, extract_frame
from astrbot.core.message.message_event_result import MessageChain

@register("hybird_videos_analysis", "喵喵", "可以解析抖音和bili视频", "0.2.14","https://github.com/miaoxutao123/astrbot_plugin_videos_analysis")
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
        # 抖音深度理解配置
        self.douyin_video_comprehend = config.get("douyin_video_comprehend")
        self.show_progress_messages = config.get("show_progress_messages")
        
        # 二进制退避算法相关配置
        self.video_records = {}  # 存储视频解析记录 {video_id: {"parse_time": timestamp, "expire_time": timestamp, "bot_id": str}}
        self.video_records_lock = threading.Lock()  # 线程锁，保护共享资源
        self.max_retry_attempts = 5  # 最大重试次数
        self.base_backoff_time = 5  # 基础退避时间（秒）
        self.max_backoff_time = 30  # 最大退避时间（秒）
        self.record_expire_time = 300  # 记录过期时间（秒）

        # 外部Bot处理记录
        self.external_handled_videos = {} # {video_id: timestamp}
        self.external_handled_lock = threading.Lock()

        self.external_handled_videos = {} # {video_id: timestamp}
        self.external_handled_lock = threading.Lock()

    async def _recall_msg(self, event: AstrMessageEvent, message_id: int):
        """撤回消息"""
        try:
            if message_id and message_id != 0:
                # 适配不同的平台适配器，这里主要针对 aiocqhttp (NapCat)
                if hasattr(event, 'bot') and hasattr(event.bot, 'api'):
                     await event.bot.api.call_action("delete_msg", message_id=message_id)
                     logger.info(f"✅ 已自动撤回消息: {message_id}")
                else:
                    logger.warning("当前平台不支持或无法调用 delete_msg")
        except Exception as e:
            logger.error(f"撤回消息失败: {e}")

        self.Debounce_time = config.get("debounce_time") or 60
        self.cache = TTLCache(maxsize=1000, ttl=self.Debounce_time)
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
        download_dir = "data/plugins/astrbot_plugin_videos_analysis/download_videos/dy"
        os.makedirs(download_dir, exist_ok=True)

        for i in range(len(result["media_urls"])):
            media_url = result["media_urls"][i]
            aweme_id = result.get("aweme_id", "unknown")

            try:
                if media_type == "image" or media_url.endswith(".jpg"):
                    # 下载图片
                    file_extension = ".jpg"
                    local_filename = f"{download_dir}/{aweme_id}_{i}{file_extension}"

                    logger.info(f"开始下载图片 {i+1}: {media_url}")
                    success = await download(media_url, local_filename, self.doyin_cookie)

                    if success and os.path.exists(local_filename):
                        # 发送本地文件
                        nap_file_path = await self._send_file_if_needed(local_filename)
                        content = [Comp.Image.fromFileSystem(nap_file_path)]
                        logger.info(f"图片 {i+1} 下载并发送成功")
                    else:
                        # 图片下载失败，发送URL备用方案
                        try:
                            content = [Comp.Image.fromURL(media_url)]
                            logger.warning(f"图片 {i+1} 本地下载失败，尝试直接发送URL")
                        except Exception as url_error:
                            content = [Comp.Plain(f"图片 {i+1} 下载失败且URL发送失败")]
                            logger.error(f"图片 {i+1} 下载失败，文件不存在或下载失败: {local_filename}, URL发送也失败: {url_error}")
                else:
                    # 下载视频
                    file_extension = ".mp4"
                    local_filename = f"{download_dir}/{aweme_id}_{i}{file_extension}"

                    logger.info(f"开始下载视频 {i+1}: {media_url}")
                    await download(media_url, local_filename, self.doyin_cookie)

                    # 检查文件大小决定发送方式
                    if os.path.exists(local_filename):
                        file_size_mb = os.path.getsize(local_filename) / (1024 * 1024)
                        nap_file_path = await self._send_file_if_needed(local_filename)

                        if file_size_mb > self.max_video_size:
                            content = [Comp.File(file=nap_file_path, name=os.path.basename(nap_file_path))]
                            logger.info(f"视频 {i+1} 过大({file_size_mb:.2f}MB)，以文件形式发送")
                        else:
                            content = [Comp.Video.fromFileSystem(nap_file_path)]
                            logger.info(f"视频 {i+1} 下载并发送成功({file_size_mb:.2f}MB)")
                    else:
                        # 视频下载失败，尝试URL发送
                        try:
                            content = [Comp.Video.fromURL(media_url)]
                            logger.warning(f"视频 {i+1} 本地下载失败，尝试直接发送URL")
                        except Exception as url_error:
                            content = [Comp.Plain(f"视频 {i+1} 下载失败")]
                            logger.error(f"视频 {i+1} 下载失败，文件不存在: {local_filename}, URL发送也失败: {url_error}")

            except Exception as e:
                logger.error(f"处理媒体文件 {i+1} 时发生错误: {e}")
                content = [Comp.Plain(f"媒体文件 {i+1} 处理失败: {str(e)}")]

            node = self._create_node(event, content)
            ns.nodes.append(node)
        return ns

    async def _process_single_media(self, event, result, media_type: str):
        """Helper function to process single media file"""
        media_url = result["media_urls"][0]
        download_dir = "data/plugins/astrbot_plugin_videos_analysis/download_videos/dy"
        os.makedirs(download_dir, exist_ok=True)
        aweme_id = result.get("aweme_id", "unknown")

        try:
            if media_type == "image":
                # 下载图片
                file_extension = ".jpg"
                local_filename = f"{download_dir}/{aweme_id}{file_extension}"

                logger.info(f"开始下载图片: {media_url}")
                success = await download(media_url, local_filename, self.doyin_cookie)

                if success and os.path.exists(local_filename):
                    # 发送本地文件
                    nap_file_path = await self._send_file_if_needed(local_filename)
                    logger.info("图片下载并发送成功")
                    return [Comp.Image.fromFileSystem(nap_file_path)]
                else:
                    # 图片下载失败，尝试直接发送URL
                    try:
                        logger.warning(f"图片本地下载失败，尝试直接发送URL")
                        return [Comp.Image.fromURL(media_url)]
                    except Exception as url_error:
                        logger.error(f"图片下载失败，文件不存在或下载失败: {local_filename}, URL发送也失败: {url_error}")
                        return [Comp.Plain("图片下载失败")]
            else:
                # 下载视频
                file_extension = ".mp4"
                local_filename = f"{download_dir}/{aweme_id}{file_extension}"

                logger.info(f"开始下载视频: {media_url}")
                await download(media_url, local_filename, self.doyin_cookie)

                # 检查文件大小决定发送方式
                if os.path.exists(local_filename):
                    file_size_mb = os.path.getsize(local_filename) / (1024 * 1024)
                    nap_file_path = await self._send_file_if_needed(local_filename)

                    if file_size_mb > self.max_video_size:
                        logger.info(f"视频过大({file_size_mb:.2f}MB)，以文件形式发送")
                        return [Comp.File(file=nap_file_path, name=os.path.basename(nap_file_path))]
                    else:
                        logger.info(f"视频下载并发送成功({file_size_mb:.2f}MB)")
                        return [Comp.Video.fromFileSystem(nap_file_path)]
                else:
                    # 视频下载失败，尝试直接发送URL
                    try:
                        logger.warning(f"视频本地下载失败，尝试直接发送URL")
                        return [Comp.Video.fromURL(media_url)]
                    except Exception as url_error:
                        logger.error(f"视频下载失败，文件不存在: {local_filename}, URL发送也失败: {url_error}")
                        return [Comp.Plain("视频下载失败")]

        except Exception as e:
            logger.error(f"处理媒体文件时发生错误: {e}")
            return [Comp.Plain(f"媒体文件处理失败: {str(e)}")]

    async def _safe_send_video(self, event, media_component, file_path=None):
        """安全发送视频，包含降级方案"""
        try:
            # 尝试发送视频
            yield event.chain_result([media_component])
            logger.info("视频发送成功")
        except Exception as video_error:
            logger.warning(f"视频发送失败: {video_error}")

            # 降级方案1: 尝试以文件形式发送
            if file_path and os.path.exists(file_path):
                try:
                    nap_file_path = await self._send_file_if_needed(file_path)
                    file_component = Comp.File(file=nap_file_path, name=os.path.basename(nap_file_path))
                    yield event.chain_result([file_component])
                    logger.info("视频改为文件形式发送成功")
                    yield event.plain_result("⚠️ 视频发送失败，已改为文件形式发送")
                except Exception as file_error:
                    logger.error(f"文件形式发送也失败: {file_error}")
                    # 降级方案2: 发送错误提示
                    yield event.plain_result("❌ 视频发送失败，文件可能过大或格式不支持")
            else:
                # 降级方案2: 发送错误提示
                yield event.plain_result("❌ 视频发送失败，文件可能过大或格式不支持")

    async def _cleanup_old_files(self, folder_path: str):
        """Helper function to clean up old files if delete_time is configured"""
        if self.delete_time > 0:
            delete_old_files(folder_path, self.delete_time)

    def _extract_video_id(self, url: str, platform: str) -> Optional[str]:
        """从URL中提取视频ID"""
        try:
            if platform == "bili":
                # B站视频ID提取
                if "BV" in url:
                    match = re.search(r'BV[a-zA-Z0-9]+', url)
                    return match.group(0) if match else None
                elif "av" in url:
                    return f"av{match.group(1)}" if match else None
                else:
                    # 短链接，需要后续解析获取真实ID
                    return None
            elif platform == "douyin":
                # 抖音视频ID提取
                match = re.search(r'aweme_id["\s:]+["\s]?([a-zA-Z0-9]+)', url)
                if match:
                    return match.group(1)
                # 如果无法从URL直接提取，返回None，需要在解析后获取
                return None
            elif platform == "xhs":
                # 小红书ID提取
                # 尝试从 discovery/item/ID 提取
                match = re.search(r'discovery/item/([a-zA-Z0-9]+)', url)
                if match:
                    return match.group(1)
                # 尝试从 xhslink.com/ID 提取 (作为临时ID)
                match = re.search(r'xhslink\.com/([a-zA-Z0-9/]+)', url)
                if match:
                    return match.group(1).replace("/", "_") # 替换斜杠以作为合法ID
                return None
            return None
        except Exception as e:
            logger.error(f"提取视频ID时发生错误: {e}")
            return None

    def _check_existing_parsing(self, video_id: str) -> Tuple[bool, Optional[Dict]]:
        with self.video_records_lock:
            if video_id in self.video_records:
                record = self.video_records[video_id]
                current_time = time.time()
                
                # 检查记录是否过期
                if current_time > record.get("expire_time", 0):
                    # 记录已过期，删除并返回False
                    del self.video_records[video_id]
                    return False, None
                
                # 记录未过期，说明已有bot在处理或已处理
                return True, record
            
            return False, None

    def _record_video_parsing(self, video_id: str, bot_id: str) -> None:
        """记录视频解析开始"""
        with self.video_records_lock:
            current_time = time.time()
            self.video_records[video_id] = {
                "parse_time": current_time,
                "expire_time": current_time + self.record_expire_time,
                "bot_id": bot_id
            }

    def _update_video_expire_time(self, video_id: str) -> None:
        """更新视频记录的失效时间"""
        with self.video_records_lock:
            if video_id in self.video_records:
                current_time = time.time()
                self.video_records[video_id]["expire_time"] = current_time + self.record_expire_time

    def _cleanup_expired_records(self) -> None:
        """清理过期的视频记录"""
        with self.video_records_lock:
            current_time = time.time()
            expired_keys = [
                video_id for video_id, record in self.video_records.items()
                if current_time > record.get("expire_time", 0)
            ]
            for video_id in expired_keys:
                del self.video_records[video_id]
                logger.info(f"清理过期的视频记录: {video_id}")

    def _cleanup_external_records(self) -> None:
        """清理过期的外部Bot处理记录"""
        with self.external_handled_lock:
            current_time = time.time()
            expired_keys = [
                video_id for video_id, timestamp in self.external_handled_videos.items()
                if current_time - timestamp > self.record_expire_time
            ]
            for video_id in expired_keys:
                del self.external_handled_videos[video_id]

    async def _binary_exponential_backoff(self, video_id: str, bot_id: str) -> bool:
        """
        二进制指数退避算法
        返回True表示可以继续解析，False表示应该放弃解析
        """
        for attempt in range(self.max_retry_attempts):
            # 检查是否已有其他bot完成解析
            is_parsed, record = self._check_existing_parsing(video_id)
            
            if not is_parsed:
                # 没有其他bot在处理，记录当前bot开始处理
                self._record_video_parsing(video_id, bot_id)
                logger.info(f"Bot {bot_id} 开始解析视频 {video_id}")
                return True
            
            # 检查是否是当前bot的记录
            if record and record.get("bot_id") == bot_id:
                # 是当前bot的记录，更新失效时间并继续
                self._update_video_expire_time(video_id)
                logger.info(f"Bot {bot_id} 继续解析视频 {video_id}")
                return True
            
            # 有其他bot在处理，计算退避时间
            backoff_time = min(
                self.base_backoff_time * (2 ** attempt) + random.uniform(0, 1),
                self.max_backoff_time
            )
            
            logger.info(f"Bot {bot_id} 检测到视频 {video_id} 正在被其他bot处理，等待 {backoff_time:.2f} 秒后重试 (尝试 {attempt + 1}/{self.max_retry_attempts})")
            
            # 等待退避时间
            await asyncio.sleep(backoff_time)
            
            # 清理过期记录
            self._cleanup_expired_records()
        
        # 超过最大重试次数，放弃解析
        logger.warning(f"Bot {bot_id} 放弃解析视频 {video_id}，超过最大重试次数")
        return False

    def _detect_other_bot_response(self, message_content: str) -> bool:
        """检测消息中是否包含其他bot的解析响应"""
        # 检查是否包含"原始链接:https"等特征
        patterns = [
            r"原始链接\s*:\s*https?://",
            r"原链接\s*:\s*https?://",
            r"视频链接\s*:\s*https?://",
            r"source\s*:\s*https?://",
            r"🧷\s*.*https?://",
        ]
        
        for pattern in patterns:
            if re.search(pattern, message_content, re.IGNORECASE):
                return True
        return False

    async def _get_gemini_api_config(self):
        """获取Gemini API配置的辅助函数"""
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
            api_key = self.gemini_api_key
            proxy_url = self.gemini_base_url
            if api_key:
                logger.info("使用插件配置的API Key。")
                if proxy_url:
                    logger.info(f"使用插件配置的代理地址：{proxy_url}")
                else:
                    logger.info("插件配置中未设置代理地址，将使用官方API。")

        return api_key, proxy_url

    async def _send_llm_response(self, event, video_summary: str, platform: str = "抖音"):
        """将视频摘要提交给框架LLM进行评价 - 异步生成器版本"""
        if not video_summary:
            # 确保这是一个异步生成器，即使没有内容也不yield任何东西
            # 这样async for循环会正常完成而不产生任何结果
            if False:  # 永远不会执行，但确保Python识别这是生成器函数
                yield  # pragma: no cover
        else:
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

            final_prompt = f"{persona_prompt}我刚刚分析了这个{platform}视频的内容：\n\n{video_summary}\n\n请基于这个视频内容，结合你的人格特点，自然地发表你的看法或评论。不要说这是我转述给你的，请像你亲自观看了这个用户给你分享的来自{platform}的视频一样回应。"

            # event.request_llm 可能返回一个async generator，需要正确处理
            llm_result = event.request_llm(
                prompt=final_prompt,
                session_id=curr_cid,
                contexts=context,
                conversation=conversation
            )
            
            # 检查是否是async generator
            if hasattr(llm_result, '__aiter__'):
                # 是async generator，逐个yield结果
                async for result in llm_result:
                    yield result
            else:
                # 不是async generator，直接yield
                yield llm_result

    async def _process_douyin_comprehension(self, event, result, content_type: str, api_key: str, proxy_url: str):
        """处理抖音视频/图片的深度理解"""
        download_dir = "data/plugins/astrbot_plugin_videos_analysis/download_videos/dy"
        os.makedirs(download_dir, exist_ok=True)

        media_urls = result.get("media_urls", [])
        aweme_id = result.get("aweme_id", "unknown")

        if content_type == "image":
            # 处理图片理解
            async for response in self._process_douyin_images_comprehension(event, media_urls, aweme_id, download_dir, api_key, proxy_url):
                yield response
        elif content_type in ["video", "multi_video"]:
            # 处理视频理解
            async for response in self._process_douyin_videos_comprehension(event, media_urls, aweme_id, download_dir, api_key, proxy_url):
                yield response

    async def _process_douyin_images_comprehension(self, event, media_urls, aweme_id, download_dir, api_key, proxy_url):
        """处理抖音图片的深度理解"""
        if self.show_progress_messages:
            yield event.plain_result(f"检测到 {len(media_urls)} 张图片，正在下载并分析...")

        # 下载所有图片
        image_paths = []
        for i, media_url in enumerate(media_urls):
            local_filename = f"{download_dir}/{aweme_id}_{i}.jpg"

            logger.info(f"开始下载图片 {i+1}: {media_url}")
            success = await download(media_url, local_filename, self.doyin_cookie)

            if success and os.path.exists(local_filename):
                image_paths.append(local_filename)
                logger.info(f"图片 {i+1} 下载成功")
            else:
                logger.warning(f"图片 {i+1} 下载失败")
        
        if not image_paths:
            yield event.plain_result("抱歉，无法下载图片进行分析。")
            return
        
        # 使用Gemini分析图片
        try:
            if self.show_progress_messages:
                yield event.plain_result("正在使用AI分析图片内容...")
            
            prompt = "请详细描述这些图片的内容，包括场景、人物、物品、文字信息和传达的核心信息。如果是多张图片，请分别描述每张图片的内容。"
            image_response = await process_images_with_gemini(api_key, prompt, image_paths, proxy_url)
            
            if image_response and image_response[0]:
                # 发送分析后的图片
                for i, image_path in enumerate(image_paths):
                    nap_file_path = await self._send_file_if_needed(image_path)
                    yield event.chain_result([Comp.Image.fromFileSystem(nap_file_path)])
                
                # 发送AI分析结果
                async for response in self._send_llm_response(event, image_response[0], "抖音"):
                    yield response
            else:
                yield event.plain_result("抱歉，我暂时无法理解这些图片的内容。")
                
        except Exception as e:
            logger.error(f"处理抖音图片理解时发生错误: {e}")
            yield event.plain_result("抱歉，分析图片时出现了问题。")

    async def _process_douyin_videos_comprehension(self, event, media_urls, aweme_id, download_dir, api_key, proxy_url):
        """处理抖音视频的深度理解"""
        # 处理第一个视频（抖音通常是单个视频）
        media_url = media_urls[0]
        local_filename = f"{download_dir}/{aweme_id}.mp4"

        if self.show_progress_messages:
            yield event.plain_result("正在下载视频进行分析...")
        
        logger.info(f"开始下载视频: {media_url}")
        await download(media_url, local_filename, self.doyin_cookie)
        
        if not os.path.exists(local_filename):
            yield event.plain_result("抱歉，无法下载视频进行分析。")
            return
        
        try:
            # 检查文件大小并选择策略
            video_size_mb = os.path.getsize(local_filename) / (1024 * 1024)
            video_summary = ""
            
            if video_size_mb > 30:
                # --- 大视频处理流程 (音频+关键帧) ---
                if self.show_progress_messages:
                    yield event.plain_result(f"视频大小为 {video_size_mb:.2f}MB，采用音频+关键帧模式进行分析...")
                
                # a. 分离音视频
                separated_files = await separate_audio_video(local_filename)
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
                    prompt = f"这是关于一个抖音视频的摘要和一些从该视频中提取的关键帧。视频摘要如下：\n\n{description}\n\n请结合摘要和这些关键帧，对整个视频内容进行一个全面、生动的总结。"
                    summary_tuple = await process_images_with_gemini(api_key, prompt, image_paths, proxy_url)
                    video_summary = summary_tuple[0] if summary_tuple else "无法生成最终摘要。"
                
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
                if self.show_progress_messages:
                    yield event.plain_result(f"视频大小为 {video_size_mb:.2f}MB，直接上传视频进行分析...")
                
                video_prompt = "请详细描述这个抖音视频的内容，包括场景、人物、动作、音乐、文字信息和传达的核心信息。"
                video_response = await process_video_with_gemini(api_key, video_prompt, local_filename, proxy_url)
                video_summary = video_response[0] if video_response and video_response[0] else "抱歉，我暂时无法理解这个视频内容。"
            
            # 发送原视频
            nap_file_path = await self._send_file_if_needed(local_filename)
            file_size_mb = os.path.getsize(local_filename) / (1024 * 1024)
            
            if file_size_mb > self.max_video_size:
                yield event.chain_result([Comp.File(file=nap_file_path, name=os.path.basename(nap_file_path))])
            else:
                yield event.chain_result([Comp.Video.fromFileSystem(nap_file_path)])
            
            # 发送AI分析结果
            if video_summary:
                async for response in self._send_llm_response(event, video_summary, "抖音"):
                    yield response
            else:
                yield event.plain_result("抱歉，我无法理解这个视频的内容。")
                
        except Exception as e:
            logger.error(f"处理抖音视频理解时发生错误: {e}")
            yield event.plain_result("抱歉，分析视频时出现了问题。")
        finally:
            # 清理临时文件
            if os.path.exists(local_filename):
                os.remove(local_filename)
                logger.info(f"已清理临时文件: {local_filename}")
            
            # 清理视频记录
            if aweme_id:
                with self.video_records_lock:
                    if aweme_id in self.video_records:
                        del self.video_records[aweme_id]
                        logger.info(f"抖音视频 {aweme_id} 深度理解完成，已清理记录")

@filter.event_message_type(EventMessageType.ALL, priority=10)

    def _debounce_check(self, link: str) -> bool:
        """检查是否在防抖时间内已经处理过相同链接

        Returns:
            True: 链接已处理过，应跳过
            False: 链接未处理过，可以继续
        """
        if self.Debounce_time <= 0:
            return False  # 防抖功能关闭

        if link in self.cache:
            return True  # 在防抖时间内已处理过，跳过

        # 记录这个链接（TTLCache 会自动在 ttl 后过期）
        self.cache[link] = True
        return False
@filter.event_message_type(EventMessageType.ALL)
async def auto_parse_dy(self, event: AstrMessageEvent, *args, **kwargs):
    """
    自动检测消息中是否包含抖音分享链接，并解析。
    """
    cookie = self.doyin_cookie
    message_str = event.message_str
    match = re.search(r"(https?://v\.douyin\.com/[a-zA-Z0-9_\-]+(?:-[a-zA-Z0-9_\-]+)?)", message_str)

    if not match:
        return

    # 检查是否已有其他bot发送了解析结果
    if self._detect_other_bot_response(message_str):
        logger.info("检测到其他bot已发送抖音解析结果，跳过解析")
        
        # 尝试提取视频ID并记录
        video_id = self._extract_video_id(match.group(1), "douyin")
        if video_id:
            with self.external_handled_lock:
                self.external_handled_videos[video_id] = time.time()
                logger.info(f"记录外部Bot已处理抖音视频: {video_id}")
        
        event.stop_event()  # 停止事件传播，避免其他插件继续处理
        return

    # 防抖检查：传入实际匹配到的链接字符串
    if self._debounce_check(match.group(0)):
        logger.info("防抖时间内已处理过相同链接，跳过解析。")
        return

    await self._cleanup_old_files("data/plugins/astrbot_plugin_videos_analysis/download_videos/dy")

    # 发送开始解析的提示
    if self.show_progress_messages:
        yield event.plain_result("正在解析抖音链接...")

    parser = DouyinParser(cookie = cookie)
    result = await parser.parse(message_str)

    if not result:
        yield event.plain_result("抱歉，这个抖音链接我不能打开，请检查一下链接是否正确。")
        return

    if isinstance(result, dict) and result.get("error"):
        error_message = result.get("error", "Unknown error")
        details = result.get("details")
        aweme_id = result.get("aweme_id")
        logger.error(
            "Douyin parse failed: %s | details=%s | aweme_id=%s",
            error_message,
            details,
            aweme_id,
        )
        message_lines = [f"抱歉，解析这个抖音链接失败：{error_message}"]
        if aweme_id:
            message_lines.append(f"关联作品ID：{aweme_id}")
        if details and details != error_message:
            message_lines.append(f"详细信息：{details}")
        yield event.plain_result("\n".join(message_lines))
        return

    content_type = result.get("type")
    if not content_type or content_type not in ["video", "image", "multi_video"]:
        logger.info("解析失败，请检查链接是否正确。无法判断链接内容类型。")
        yield event.plain_result("解析失败，无法识别内容类型。")
        return

    # 获取视频ID并应用二进制退避算法
    video_id = result.get("aweme_id", "")
    if not video_id:
        # 如果无法从解析结果获取ID，尝试从URL提取
        video_id = self._extract_video_id(match.group(1), "douyin")
    
    if video_id:
        # 获取当前bot ID
        bot_id = str(event.get_self_id())
        
        # 应用二进制退避算法
        can_continue = await self._binary_exponential_backoff(video_id, bot_id)
        if not can_continue:
            # 放弃解析
            yield event.plain_result("检测到其他bot正在处理此视频，已放弃解析。")
            event.stop_event()  # 停止事件传播，避免其他插件继续处理
            return

        # 再次检查是否已被外部Bot处理（在等待退避期间可能发生）
        with self.external_handled_lock:
            if video_id in self.external_handled_videos:
                logger.info(f"检测到外部Bot已处理视频 {video_id}，终止处理")
                yield event.plain_result("检测到其他bot已完成处理，终止解析。")
                event.stop_event()
                return

    # --- 抖音深度理解流程 ---
    if self.douyin_video_comprehend and content_type in ["video", "multi_video", "image"]:
        if self.show_progress_messages:
            yield event.plain_result("我看到了一个抖音视频链接，让我来仔细分析一下内容，请稍等一下...")

        # 获取Gemini API配置
        api_key, proxy_url = await self._get_gemini_api_config()
        
        # 如果最终都没有配置，则提示用户
        if not api_key:
            yield event.plain_result("抱歉，我需要Gemini API才能理解视频，但是没有找到相关配置。\n请在框架中配置Gemini Provider或在插件配置中提供gemini_api_key。")
            # 继续执行常规解析流程
        else:
            # 执行深度理解流程
            try:
                async for response in self._process_douyin_comprehension(event, result, content_type, api_key, proxy_url):
                    yield response
                return  # 深度理解完成后直接返回，不执行常规解析
            except Exception as e:
                logger.error(f"处理抖音视频理解时发生错误: {e}")
                yield event.plain_result("抱歉，处理这个视频时出现了一些问题，将使用常规模式解析。")
                # 继续执行常规解析流程

    # --- 常规解析流程 ---
    # 发送下载提示
    media_count = len(result.get("media_urls", []))
    if self.show_progress_messages:
        if media_count > 1:
            yield event.plain_result(f"检测到 {media_count} 个文件，正在下载...")
        else:
            yield event.plain_result("正在下载媒体文件...")

    is_multi_part = False
    if "media_urls" in result and len(result["media_urls"]) != 1:
        is_multi_part = True

    try:
        # 再次检查是否已被外部Bot处理（防止处理期间被抢答）
        with self.external_handled_lock:
            if video_id in self.external_handled_videos:
                logger.info(f"检测到外部Bot已处理视频 {video_id}，终止处理")
                yield event.plain_result("检测到其他bot已完成处理，终止解析。")
                event.stop_event()
                return

        # 处理多段内容
        if is_multi_part:
            ns = await self._process_multi_part_media(event, result, content_type)
            await event.send(MessageChain([ns]))
        else:
            # 处理单段内容
            content = await self._process_single_media(event, result, content_type)
            if content_type == "image":
                logger.info(f"发送单段图片: {content[0]}")
            
            # 使用 event.send 发送
            ret = await event.send(MessageChain(content))
            
            # 发送后再次检查是否冲突
            with self.external_handled_lock:
                if video_id in self.external_handled_videos:
                    logger.info(f"发送后检测到外部Bot已处理视频 {video_id}，尝试撤回")
                    if ret and hasattr(ret, 'message_id'):
                        await self._recall_msg(event, ret.message_id)

    except Exception as e:
        logger.error(f"处理抖音媒体时发生错误: {e}")
        yield event.plain_result(f"处理媒体文件时发生错误: {str(e)}")
        return
    finally:
        pass

@filter.event_message_type(EventMessageType.ALL, priority=10)
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

    # 防抖检查：传入实际匹配到的链接字符串
    if match_json and self._debounce_check(match_json.group(0)):
        logger.info("防抖时间内已处理过相同链接，跳过解析。")
        return
    if match_plain and self._debounce_check(match_plain.group(0)):
        logger.info("防抖时间内已处理过相同链接，跳过解析。")
        return

    url = ""
    if match_plain:
        url = match_plain.group(1)
    elif match_json:
        url = match_json.group(0).replace("\\\\", "\\").replace("\\/", "/")

    # 检查是否已有其他bot发送了解析结果
    if self._detect_other_bot_response(message_str):
        logger.info("检测到其他bot已发送B站解析结果，跳过解析")
        
        # 尝试提取视频ID并记录
        video_id = self._extract_video_id(url, "bili")
        if video_id:
            with self.external_handled_lock:
                self.external_handled_videos[video_id] = time.time()
                logger.info(f"记录外部Bot已处理B站视频: {video_id}")

        event.stop_event()  # 停止事件传播，避免其他插件继续处理
        return

    # 删除过期文件
    await self._cleanup_old_files("data/plugins/astrbot_plugin_videos_analysis/download_videos/bili/")

    # 获取视频ID并应用二进制退避算法
    video_id = self._extract_video_id(url, "bili")
    if video_id:
        # 获取当前bot ID
        bot_id = str(event.get_self_id())
        
        # 应用二进制退避算法
        can_continue = await self._binary_exponential_backoff(video_id, bot_id)
        if not can_continue:
            # 放弃解析
            yield event.plain_result("检测到其他bot正在处理此视频，已放弃解析。")
            event.stop_event()  # 停止事件传播，避免其他插件继续处理
            return

        # 再次检查是否已被外部Bot处理
        with self.external_handled_lock:
            if video_id in self.external_handled_videos:
                logger.info(f"检测到外部Bot已处理视频 {video_id}，终止处理")
                yield event.plain_result("检测到其他bot已完成处理，终止解析。")
                event.stop_event()
                return

    # --- 视频深度理解流程 ---
    if url_video_comprehend:
        if self.show_progress_messages:
            yield event.plain_result("我看到了一个B站视频链接，让我来仔细分析一下内容，请稍等一下...")

        # 获取Gemini API配置
        api_key, proxy_url = await self._get_gemini_api_config()

        # 如果最终都没有配置，则提示用户
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
                if self.show_progress_messages:
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
                if self.show_progress_messages:
                    yield event.plain_result(f"视频大小为 {video_size_mb:.2f}MB，直接上传视频进行分析...")
                video_prompt = "请详细描述这个视频的内容，包括场景、人物、动作和传达的核心信息。"
                video_response = await process_video_with_gemini(api_key, video_prompt, video_path, proxy_url)
                video_summary = video_response[0] if video_response and video_response[0] else "抱歉，我暂时无法理解这个视频内容。"

            # 3. 将摘要提交给框架LLM进行评价
            if video_summary:
                async for response in self._send_llm_response(event, video_summary, "B站"):
                    yield response
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
            
            # 5. 清理视频记录
            if 'video_id' in locals() and video_id:
                with self.video_records_lock:
                    if video_id in self.video_records:
                        del self.video_records[video_id]
                        logger.info(f"B站视频 {video_id} 深度理解完成，已清理记录")
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
            if file_size_mb > 100:
                media_component = Comp.File(file=nap_file_path, name=os.path.basename(nap_file_path))
            else:
                media_component = Comp.Video.fromFileSystem(path = nap_file_path)

        # 构建信息文本，加入错误处理
        try:
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

        # 再次检查是否已被外部Bot处理（防止处理期间被抢答）
        with self.external_handled_lock:
            if video_id in self.external_handled_videos:
                logger.info(f"检测到外部Bot已处理视频 {video_id}，终止处理")
                yield event.plain_result("检测到其他bot已完成处理，终止解析。")
                event.stop_event()
                return

        # 根据回复模式构建响应，视频单独发送提高稳定性
        send_chain = []
        if reply_mode == 0: # 纯文本
            send_chain = [Comp.Plain(info_text)]
        elif reply_mode == 1: # 带图片
            cover_url = result.get("cover")
            if cover_url:
                if zhuanfa:
                    # 合并转发模式
                    ns = Nodes([])
                    ns.nodes.append(self._create_node(event, [Comp.Image.fromURL(cover_url)]))
                    ns.nodes.append(self._create_node(event, [Comp.Plain(info_text)]))
                    send_chain = [ns]
                else:
                    # 分别发送
                    await event.send(MessageChain([Comp.Image.fromURL(cover_url)]))
                    send_chain = [Comp.Plain(info_text)]
            else:
                send_chain = [Comp.Plain("封面图片获取失败\n" + info_text)]
        elif reply_mode == 2: # 带视频
            if media_component:
                if zhuanfa:
                    # 合并转发模式，但视频单独发送
                    await event.send(MessageChain([Comp.Plain(info_text)]))
                    send_chain = [media_component]
                else:
                    # 分别发送
                    send_chain = [media_component]
            else:
                send_chain = [Comp.Plain(info_text)]
        elif reply_mode == 3: # 完整
            cover_url = result.get("cover")
            if zhuanfa:
                # 合并转发模式，视频单独发送
                if cover_url:
                    ns = Nodes([])
                    ns.nodes.append(self._create_node(event, [Comp.Image.fromURL(cover_url)]))
                    ns.nodes.append(self._create_node(event, [Comp.Plain(info_text)]))
                    await event.send(MessageChain([ns]))
                else:
                    await event.send(MessageChain([Comp.Plain("封面图片获取失败\n" + info_text)]))
                # 视频单独发送
                send_chain = [media_component]
            else:
                # 分别发送所有内容
                if cover_url:
                    await event.send(MessageChain([Comp.Image.fromURL(cover_url)]))
                else:
                    await event.send(MessageChain([Comp.Plain("封面图片获取失败")]))
                await event.send(MessageChain([Comp.Plain(info_text)]))
                send_chain = [media_component]
        elif reply_mode == 4: # 仅视频
            if media_component:
                send_chain = [media_component]
        
        # 发送最终消息并检查撤回
        if send_chain:
            try:
                ret = await event.send(MessageChain(send_chain))
                
                # 发送后再次检查是否冲突
                with self.external_handled_lock:
                    if video_id in self.external_handled_videos:
                        logger.info(f"发送后检测到外部Bot已处理视频 {video_id}，尝试撤回")
                        if ret and hasattr(ret, 'message_id'):
                            await self._recall_msg(event, ret.message_id)
            except Exception as e:
                logger.error(f"发送消息或撤回失败: {e}")

    # 发送完成后，清理视频记录
    # if 'video_id' in locals() and video_id:
    #     with self.video_records_lock:
    #         if video_id in self.video_records:
    #             del self.video_records[video_id]
    #             logger.info(f"B站视频 {video_id} 解析完成，已清理记录")

# @filter.event_message_type(EventMessageType.ALL)
# async def auto_parse_ks(self, event: AstrMessageEvent, *args, **kwargs):
#     """
#     自动检测消息中是否包含快手分享链接，并解析。
#     """
#     api_url = "https://api.kxzjoker.cn/api/jiexi_video"
#     message_str = event.message_str
#     match = re.search(r"(https?://v\.k\.ua\.com/[a-zA-Z0-9_\-]+(?:-[a-zA-Z0-9_\-]+)?)", message_str)

@filter.event_message_type(EventMessageType.ALL, priority=10)
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

    # 检查是否已有其他bot发送了解析结果
    if self._detect_other_bot_response(message_str):
        logger.info("检测到其他bot已发送小红书解析结果，跳过解析")
        
        # 尝试提取ID并记录
        url_for_id = ""
        if image_match:
            url_for_id = image_match.group(1)
        elif video_match:
            url_for_id = video_match.group(1)
            
        xhs_id = self._extract_video_id(url_for_id, "xhs")
        if xhs_id:
            with self.external_handled_lock:
                self.external_handled_videos[xhs_id] = time.time()
                logger.info(f"记录外部Bot已处理小红书内容: {xhs_id}")
        
        event.stop_event()
        return

    # 提取ID并进行退避
    url_for_id = ""
    if image_match:
        url_for_id = image_match.group(1)
    elif video_match:
        url_for_id = video_match.group(1)
        
    xhs_id = self._extract_video_id(url_for_id, "xhs")
    
    if xhs_id:
        # 获取当前bot ID
        bot_id = str(event.get_self_id())
        
        # 应用二进制退避算法
        can_continue = await self._binary_exponential_backoff(xhs_id, bot_id)
        if not can_continue:
            # 放弃解析
            yield event.plain_result("检测到其他bot正在处理此小红书内容，已放弃解析。")
            event.stop_event()
            return

        # 再次检查是否已被外部Bot处理
        with self.external_handled_lock:
            if xhs_id in self.external_handled_videos:
                logger.info(f"检测到外部Bot已处理小红书内容 {xhs_id}，终止处理")
                yield event.plain_result("检测到其他bot已完成处理，终止解析。")
                event.stop_event()
                return
    # 防抖检查：传入实际匹配到的链接字符串
    if image_match and self._debounce_check(image_match.group(0)):
        logger.info("防抖时间内已处理过相同链接，跳过解析。")
        return
    if video_match and self._debounce_check(video_match.group(0)):
        logger.info("防抖时间内已处理过相同链接，跳过解析。")
        return

    # 处理图片链接
    if image_match:
        result = await xhs_parse(image_match.group(1))
        if not result or "error" in result:
            logger.error(f"小红书图片解析失败: {result.get('error', '未知错误') if result else '返回结果为空'}")
            yield event.plain_result("小红书链接解析失败，请检查链接是否正确")
            return

        ns = Nodes([]) if replay_mode else None
        title = result.get("title", "小红书内容")  # 提供默认标题
        title_node = self._create_node(event, [Plain(title)])

        if replay_mode:
            ns.nodes.append(title_node)
        else:
            yield event.chain_result([Plain(title)])

        urls = result.get("urls", [])
        if not urls:
            logger.warning("小红书解析结果中没有找到图片URL")
            yield event.plain_result("未找到可用的图片链接")
            return

        for image_url in urls:
            image_node = self._create_node(event, [Image.fromURL(image_url)])
            if replay_mode:
                ns.nodes.append(image_node)
            else:
                yield event.chain_result([Image.fromURL(image_url)])

        if replay_mode:
            ret = await event.send(MessageChain([ns]))
            # 检查撤回
            with self.external_handled_lock:
                if xhs_id and xhs_id in self.external_handled_videos:
                    if ret and hasattr(ret, 'message_id'):
                        await self._recall_msg(event, ret.message_id)

    # 处理视频链接
    if video_match:
        result = await xhs_parse(video_match.group(1))
        if not result or "error" in result:
            logger.error(f"小红书视频解析失败: {result.get('error', '未知错误') if result else '返回结果为空'}")
            yield event.plain_result("小红书链接解析失败，请检查链接是否正确")
            return

        ns = Nodes([]) if replay_mode else None
        title = result.get("title", "小红书内容")  # 提供默认标题
        title_node = self._create_node(event, [Plain(title)])

        if "video_sizes" in result:
            # 处理视频内容
            if replay_mode:
                ns.nodes.append(title_node)
            else:
                yield event.chain_result([Plain(title)])

            urls = result.get("urls", [])
            if not urls:
                logger.warning("小红书解析结果中没有找到视频URL")
                yield event.plain_result("未找到可用的视频链接")
                return

            for url in urls:
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
                yield event.chain_result([Plain(title)])

            urls = result.get("urls", [])
            if not urls:
                logger.warning("小红书解析结果中没有找到图片URL")
                yield event.plain_result("未找到可用的图片链接")
                return

            for image_url in urls:
                image_node = self._create_node(event, [Image.fromURL(image_url)])
                if replay_mode:
                    ns.nodes.append(image_node)
                else:
                    yield event.chain_result([Image.fromURL(image_url)])

        if replay_mode:
            ret = await event.send(MessageChain([ns]))
            # 检查撤回
            with self.external_handled_lock:
                if xhs_id and xhs_id in self.external_handled_videos:
                    if ret and hasattr(ret, 'message_id'):
                        await self._recall_msg(event, ret.message_id)

@filter.event_message_type(EventMessageType.ALL, priority=10)
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

@filter.event_message_type(EventMessageType.ALL, priority=10)
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

            async with aiofiles.open(video_path, "wb") as f:
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

            final_prompt = f"{persona_prompt}我刚刚分析了这个B站视频的内容：\n\n{video_summary}\n\n请基于这个视频内容，结合你的人格特点，自然地发表你的看法或评论。不要说这是我转述给你的，请像你亲自观看了这个用户给你分享的视频一样回应。"

            # event.request_llm 可能返回一个async generator，需要正确处理
            llm_result = event.request_llm(
                prompt=final_prompt,
                session_id=curr_cid,
                contexts=context,
                conversation=conversation
            )
            
            # 检查是否是async generator
            if hasattr(llm_result, '__aiter__'):
                # 是async generator，逐个yield结果
                async for result in llm_result:
                    yield result
            else:
                # 不是async generator，直接yield
                yield llm_result
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
