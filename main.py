"""
AstrBot 视频解析与理解插件
支持平台：抖音、B站、MC百科
功能：被动链接解析（默认关闭）、Agent 主动调用视频解析/理解工具
"""

import json
import os
import re

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.star import Context, Star, register

from astrbot.api.event import filter
from astrbot.api.event.filter import EventMessageType
from astrbot.core.message.message_event_result import MessageChain

from .douyin_login import load_douyin_cookies
from .utils.file_utils import delete_old_files


@register(
    "astrbot_plugin_videos_analysis",
    "视频解析与理解",
    "抖音/B站/MC百科视频自动解析与AI理解",
    "0.3.0",
    "https://github.com/Zhalslar/astrbot_plugin_videos_analysis",
)
class hybird_videos_analysis(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context, config)
        self.context = context

        # NAP 文件服务器
        self.nap_server_address = config.get("nap_server_address", "localhost")
        self.nap_server_port = config.get("nap_server_port", 3658)
        self.delete_time = config.get("delete_time", 60)
        self.max_video_size = config.get("max_video_size", 200)

        # 被动解析开关（默认关闭）
        self.auto_parse_enabled = config.get("auto_parse_enabled", False)

        # B站配置
        self.bili_quality = config.get("bili_quality", 32)
        self.bili_reply_mode = config.get("bili_reply_mode", 3)
        self.bili_url_mode = config.get("bili_url_mode", True)
        self.Merge_and_forward = config.get("Merge_and_forward", False)
        self.bili_use_login = config.get("bili_use_login", False)

        # 抖音配置
        self.douyin_video_comprehend = config.get("douyin_video_comprehend", False)
        self.show_progress_messages = config.get("show_progress_messages", True)
        self.douyin_proxy = config.get("douyin_proxy", "")

        # 视频理解配置
        self.url_video_comprehend = config.get("url_video_comprehend", False)
        self.upload_video_comprehend = config.get("upload_video_comprehend", False)
        self.private_auto_comprehend = config.get("private_auto_comprehend", True)
        self.gemini_api_key = config.get("gemini_api_key", "")
        self.gemini_base_url = config.get("gemini_base_url", "")
        self.video_understand_method = config.get("video_understand_method", "local_asr")

        # MiMo 配置
        self.mimo_api_base = config.get("mimo_api_base", "https://api.xiaomimimo.com/v1")
        self.mimo_api_key = config.get("mimo_api_key", "")
        self.mimo_model = config.get("mimo_model", "mimo-v2-omni")

        # 抖音 cookie 状态（优先级：扫码 > 文件 > 配置）
        self._douyin_cookie_from_config = config.get("doyin_cookie", "")
        self._douyin_cookie_from_file = None
        self._douyin_cookie_from_scan = None

    @property
    def effective_douyin_cookie(self) -> str:
        """优先级：扫码cookie > 文件cookie > 配置cookie"""
        return self._douyin_cookie_from_scan or self._douyin_cookie_from_file or self._douyin_cookie_from_config or ""

    async def initialize(self):
        """初始化：加载持久化cookie"""
        cookie = await load_douyin_cookies()
        if cookie:
            self._douyin_cookie_from_file = cookie
            logger.info("已加载抖音cookie文件")

    # ==================== 被动解析 ====================

    @filter.event_message_type(EventMessageType.ALL)
    @filter.regex(r"(https?://v\.douyin\.com/[a-zA-Z0-9_\-]+(?:-[a-zA-Z0-9_\-]+)?)")
    async def auto_parse_dy(self, event):
        """被动解析抖音链接（需开启 auto_parse_enabled）"""
        if not self.auto_parse_enabled:
            return
        from .handlers.douyin_handler import handle_douyin_parse
        async for response in handle_douyin_parse(self, event):
            yield response

    @filter.event_message_type(EventMessageType.ALL)
    @filter.regex(
        r"(https?://b23\.tv/[\w]+|https?://bili2233\.cn/[\w]+|"
        r"https?://www\.bilibili\.com/video/BV1\w{9}|"
        r"https?://www\.bilibili\.com/video/av\d+|"
        r"BV1\w{9}|av\d+)"
    )
    async def auto_parse_bili(self, event):
        """被动解析B站链接（需开启 auto_parse_enabled）"""
        if not self.auto_parse_enabled:
            return
        from .handlers.bilibili_handler import handle_bilibili_parse
        async for response in handle_bilibili_parse(self, event):
            yield response

    @filter.event_message_type(EventMessageType.ALL)
    @filter.regex(r"(https?://www\.mcmod\.cn/(class|modpack)/\d+\.html)")
    async def auto_parse_mcmod(self, event):
        """被动解析MC百科链接（需开启 auto_parse_enabled）"""
        if not self.auto_parse_enabled:
            return
        from .handlers.mcmod_handler import handle_mcmod_parse
        async for response in handle_mcmod_parse(self, event):
            yield response

    # ==================== 直链视频注入 ====================

    @filter.event_message_type(EventMessageType.ALL)
    async def process_direct_video(self, event):
        """检测消息中的视频附件，注入直链到上下文"""
        if not self.upload_video_comprehend:
            return
        from .handlers.video_handler import handle_direct_video
        await handle_direct_video(self, event)

    # ==================== 管理员命令 ====================

    @filter.command("bili_login")
    async def bili_login_command(self, event):
        """B站扫码登录"""
        from .handlers.admin_handler import handle_bili_login
        async for response in handle_bili_login(self, event):
            yield response

    @filter.command("dy_login")
    async def douyin_login_command(self, event):
        """抖音扫码登录"""
        from .handlers.admin_handler import handle_douyin_login
        async for response in handle_douyin_login(self, event):
            yield response

    @filter.command("dy_cookie")
    async def douyin_cookie_command(self, event):
        """手动设置抖音cookie"""
        from .handlers.admin_handler import handle_douyin_cookie
        async for response in handle_douyin_cookie(self, event):
            yield response

    # ==================== 主动触发命令 ====================

    @filter.command("理解视频")
    async def comprehend_video_command(self, event):
        """主动触发视频AI理解"""
        from .handlers.video_handler import handle_comprehend_video_command
        async for response in handle_comprehend_video_command(self, event):
            yield response

    # ==================== LLM Agent 工具 ====================

    @filter.llm_tool(name="parse_video_link")
    async def parse_video_link(self, event, link: str):
        """解析视频链接，获取视频信息（标题、封面、直链等）。
        支持抖音(v.douyin.com)和B站(b23.tv/bilibili.com)链接。

        Args:
            link(str): 视频链接URL
        """
        from .tools.parse_tool import handle_parse_video_link
        result = await handle_parse_video_link(self, link)
        return result

    @filter.llm_tool(name="understand_video")
    async def understand_video(self, event, video_source: str, prompt: str = ""):
        """深度理解视频内容，返回视频内容的详细分析。
        支持：抖音/B站链接、HTTP视频直链、本地文件路径。
        可用于理解通过 parse_video_link 工具获得的视频直链。

        Args:
            video_source(str): 视频链接URL或本地文件路径
            prompt(str): 可选的自定义分析提示词
        """
        from .tools.understand_tool import handle_understand_video
        result = await handle_understand_video(self, video_source, prompt or None)
        return result
