"""
AstrBot 视频解析与理解插件
支持平台：抖音、B站、MC百科
功能：被动链接解析（默认关闭）、Agent 主动调用视频解析/理解工具、账号运维（账号池轮换/定时巡检/状态页）、
     创作者操作（发布/删除/评论精选回复，二次确认）
"""

import json

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register

from .douyin_login import load_douyin_cookies
from .douyin_scraper.browser_signer import BrowserSigner
from .douyin_scraper.douyin_parser import DouyinParser
from .services.account_manager import AccountManager
from .services.creator_ops import CreatorOps
from .utils.paths import DOUYIN_BROWSER_PROFILE, migrate_legacy_data

# 旧版 _conf_schema 的默认值是个不存在的地址，按未填写处理
_BOGUS_GEMINI_BASE = "https://generative-api.google.com/v1alpha2"


@register(
    "astrbot_plugin_videos_analysis",
    "视频解析与理解",
    "抖音/B站/MC百科视频自动解析与AI理解",
    "0.5.0",
    "https://github.com/Zhalslar/astrbot_plugin_videos_analysis",
)
class hybird_videos_analysis(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context, config)
        self.context = context
        self.config = config

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
        self.douyin_browser_signer_enabled = config.get("douyin_browser_signer", True)

        # 视频理解配置
        self.url_video_comprehend = config.get("url_video_comprehend", False)
        self.upload_video_comprehend = config.get("upload_video_comprehend", False)
        self.private_auto_comprehend = config.get("private_auto_comprehend", True)
        self.video_understand_method = config.get("video_understand_method", "gemini")

        # Gemini 配置
        self.gemini_api_key = config.get("gemini_api_key", "").strip()
        base = config.get("gemini_base_url", "").strip()
        self.gemini_base_url = "" if base == _BOGUS_GEMINI_BASE else base
        self.gemini_model = config.get("gemini_model", "").strip()
        self.gemini_inline_limit_mb = float(config.get("gemini_inline_limit_mb", 20))
        self.gemini_direct_max_mb = float(config.get("gemini_direct_max_mb", 200))

        # MiMo 配置
        self.mimo_api_base = config.get("mimo_api_base", "https://api.xiaomimimo.com/v1")
        self.mimo_api_key = config.get("mimo_api_key", "")
        self.mimo_model = config.get("mimo_model", "mimo-v2-omni")

        # 抖音 cookie 状态（优先级：扫码 > 文件 > 配置）
        self._douyin_cookie_from_config = config.get("doyin_cookie", "")
        self._douyin_cookie_from_file = None
        self._douyin_cookie_from_scan = None

        # 浏览器签名引擎 (全局单例)
        self._browser_signer: BrowserSigner | None = None
        self.browser_signer_error: str = ""

        self.accounts = AccountManager(self)
        self.creator_ops = CreatorOps(self)

    @property
    def effective_douyin_cookie(self) -> str:
        """优先级：扫码cookie > 文件cookie > 配置cookie"""
        return self._douyin_cookie_from_scan or self._douyin_cookie_from_file or self._douyin_cookie_from_config or ""

    @property
    def browser_signer(self) -> BrowserSigner | None:
        """获取浏览器签名引擎 (可能为 None, 如果未初始化)。"""
        return self._browser_signer

    def make_douyin_parser(self) -> DouyinParser:
        """所有抖音解析入口统一从这里取解析器，保证带上浏览器签名引擎。"""
        signer = self._browser_signer if self._browser_signer and self._browser_signer.is_ready else None
        return DouyinParser(cookie=self.effective_douyin_cookie, proxy=self.douyin_proxy, browser_signer=signer)

    async def initialize(self):
        """初始化：迁移旧数据、加载持久化cookie、启动浏览器签名引擎。"""
        migrate_legacy_data()

        cookie = await load_douyin_cookies()
        if cookie:
            self._douyin_cookie_from_file = cookie
            logger.info("已加载抖音cookie文件")

        if self.douyin_browser_signer_enabled:
            await self.start_browser_signer()
        else:
            self.browser_signer_error = "已在配置中禁用"

        from .handlers.web_api import register_web_apis
        register_web_apis(self)
        self.accounts.start_scheduler()

    async def start_browser_signer(self) -> dict:
        """启动（或重启）抖音浏览器签名引擎，返回状态字典。"""
        if self._browser_signer:
            await self._browser_signer.stop()
            self._browser_signer = None
        try:
            signer = BrowserSigner(headless=False, user_data_dir=DOUYIN_BROWSER_PROFILE)
            status = await signer.start()
        except Exception as e:
            status = {"ok": False, "reason": f"启动异常: {e}"}
            signer = None

        if status.get("ok"):
            self._browser_signer = signer
            self.browser_signer_error = ""
            logger.info(f"抖音浏览器签名引擎已就绪 ({status.get('reason')})")
            if not status.get("logged_in"):
                logger.info("抖音未登录, 管理员可使用 /dy_login 扫码登录")
        else:
            self.browser_signer_error = status.get("reason", "未知错误")
            logger.warning(f"抖音浏览器签名引擎不可用: {self.browser_signer_error}")
            logger.warning("抖音解析将回退到纯 Python A-Bogus 签名（目前基本会被 403 拒绝）")
            if signer:
                await signer.stop()
        return status

    async def terminate(self):
        """插件卸载/重载时关闭浏览器与 Xvfb，避免进程泄漏。"""
        await self.accounts.stop_scheduler()
        if self._browser_signer:
            await self._browser_signer.stop()
            self._browser_signer = None

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
        r"https?://(?:www\.|m\.)?bilibili\.com/video/BV1\w{9}|"
        r"https?://(?:www\.|m\.)?bilibili\.com/video/av\d+|"
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
        """检测消息中的视频附件，注入直链到上下文；/va_upload 之后补发的视频在这里接住"""
        if event.is_admin() and self.creator_ops.peek_upload_waiter(event):
            source = await self.creator_ops.extract_video(event)
            if source:
                from .handlers.account_ops_handler import handle_upload_video_arrived
                waiter = self.creator_ops.peek_upload_waiter(event, consume=True)
                async for response in handle_upload_video_arrived(self, event, waiter, source):
                    yield response
                event.stop_event()
                return
        if not self.upload_video_comprehend:
            return
        from .handlers.video_handler import handle_direct_video
        await handle_direct_video(self, event)

    # ==================== 管理员命令 ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_login")
    async def bili_login_command(self, event):
        """B站扫码登录"""
        from .handlers.admin_handler import handle_bili_login
        async for response in handle_bili_login(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("dy_login")
    async def douyin_login_command(self, event):
        """抖音扫码登录"""
        from .handlers.admin_handler import handle_douyin_login
        async for response in handle_douyin_login(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("dy_cookie")
    async def douyin_cookie_command(self, event):
        """手动设置抖音cookie"""
        from .handlers.admin_handler import handle_douyin_cookie
        async for response in handle_douyin_cookie(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("va_accounts", alias={"账号状态"})
    async def accounts_status_command(self, event, mode: str = ""):
        """查看各平台账号/AI 服务状态。/va_accounts deep 会实际测试连通性"""
        from .handlers.admin_handler import handle_accounts_status
        async for response in handle_accounts_status(self, event, deep=(mode == "deep")):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("va_logout")
    async def accounts_logout_command(self, event, platform: str = ""):
        """注销平台登录态：/va_logout douyin|bilibili"""
        yield event.plain_result(await self.accounts.logout(platform.strip().lower()))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("dy_restart")
    async def douyin_restart_command(self, event):
        """重启抖音浏览器签名引擎"""
        yield event.plain_result("正在重启抖音浏览器签名引擎...")
        status = await self.start_browser_signer()
        yield event.plain_result(("✅ " if status.get("ok") else "⚠️ ") + str(status.get("reason")))

    # ==================== 账号运维 ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("va_check")
    async def accounts_check_command(self, event):
        """立即深度巡检所有平台账号"""
        from .handlers.account_ops_handler import handle_health_check
        async for response in handle_health_check(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("va_notify")
    async def accounts_notify_command(self, event):
        """把当前会话设为账号告警接收方：/va_notify [off]"""
        from .handlers.account_ops_handler import handle_notify
        async for response in handle_notify(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("va_pool")
    async def accounts_pool_command(self, event):
        """多账号池：/va_pool [save|use|del] <平台> <名称>"""
        from .handlers.account_ops_handler import handle_pool
        async for response in handle_pool(self, event):
            yield response

    # ==================== 创作者操作（危险操作需二次确认） ====================

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("va_works")
    async def creator_works_command(self, event):
        """查看当前账号最近作品：/va_works [平台]"""
        from .handlers.account_ops_handler import handle_works
        async for response in handle_works(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("va_upload")
    async def creator_upload_command(self, event):
        """发布视频：/va_upload [平台] <标题> #话题（附带/引用视频，或随后补发）"""
        from .handlers.account_ops_handler import handle_upload
        async for response in handle_upload(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("va_delete")
    async def creator_delete_command(self, event):
        """删除作品：/va_delete [平台] <序号|链接|ID>"""
        from .handlers.account_ops_handler import handle_delete
        async for response in handle_delete(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("va_reply")
    async def creator_reply_command(self, event):
        """精选评论回复：/va_reply [平台] [序号|链接|ID] [@抖音号/昵称]，默认最新作品"""
        from .handlers.account_ops_handler import handle_reply
        async for response in handle_reply(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("va_comment")
    async def creator_comment_command(self, event):
        """对任意作品发评论：/va_comment [平台] <序号|链接|BV号> [内容]，不写内容由 AI 起草"""
        from .handlers.account_ops_handler import handle_comment
        async for response in handle_comment(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_refresh")
    async def bili_refresh_command(self, event):
        """B站 cookie 续期：/bili_refresh [force]（平时由定时巡检自动处理）"""
        from .handlers.account_ops_handler import handle_bili_refresh
        async for response in handle_bili_refresh(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_feed")
    async def bili_feed_command(self, event):
        """B站首页推荐/热门：/bili_feed [hot]"""
        from .handlers.account_ops_handler import handle_bili_feed
        async for response in handle_bili_feed(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_search")
    async def bili_search_command(self, event):
        """B站搜索视频：/bili_search <关键词>"""
        from .handlers.account_ops_handler import handle_bili_search
        async for response in handle_bili_search(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_watch")
    async def bili_watch_command(self, event):
        """浏览B站视频（抽帧+语音转写总结，并记入观看历史）：/bili_watch <序号|BV号|链接>"""
        from .handlers.account_ops_handler import handle_bili_watch
        async for response in handle_bili_watch(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_profile")
    async def bili_profile_command(self, event):
        """查看B站账号资料"""
        from .handlers.account_ops_handler import handle_bili_profile
        async for response in handle_bili_profile(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_sign")
    async def bili_sign_command(self, event):
        """修改B站签名（需确认）：/bili_sign <新签名>"""
        from .handlers.account_ops_handler import handle_bili_sign
        async for response in handle_bili_sign(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_avatar")
    async def bili_avatar_command(self, event):
        """更换B站头像（需确认）：/bili_avatar 附带或引用图片"""
        from .handlers.account_ops_handler import handle_bili_avatar
        async for response in handle_bili_avatar(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("va_confirm")
    async def creator_confirm_command(self, event):
        """执行待确认操作：/va_confirm <确认码> [序号...]"""
        from .handlers.account_ops_handler import handle_confirm
        async for response in handle_confirm(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("va_cancel")
    async def creator_cancel_command(self, event):
        """取消待确认操作：/va_cancel <确认码>"""
        from .handlers.account_ops_handler import handle_cancel
        async for response in handle_cancel(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("va_edit")
    async def creator_edit_command(self, event):
        """修改待发送的评论回复：/va_edit <确认码> <序号> <新回复>"""
        from .handlers.account_ops_handler import handle_edit
        async for response in handle_edit(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("va_pending")
    async def creator_pending_command(self, event):
        """查看待确认的操作"""
        from .handlers.account_ops_handler import handle_pending
        async for response in handle_pending(self, event):
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
        支持抖音(v.douyin.com)和B站(b23.tv/bilibili.com/BV号/av号)链接。

        Args:
            link(string): 视频链接URL
        """
        from .tools.parse_tool import handle_parse_video_link
        result = await handle_parse_video_link(self, link)
        return json.dumps(result, ensure_ascii=False)

    @filter.llm_tool(name="understand_video")
    async def understand_video(self, event, video_source: str, prompt: str = ""):
        """深度理解视频内容，返回视频内容的详细分析。
        支持：抖音/B站链接、HTTP视频直链、本地文件路径。
        可用于理解通过 parse_video_link 工具获得的视频直链。

        Args:
            video_source(string): 视频链接URL或本地文件路径
            prompt(string): 可选的自定义分析提示词
        """
        from .tools.understand_tool import handle_understand_video
        result = await handle_understand_video(self, video_source, prompt or None)
        return json.dumps(result, ensure_ascii=False)

    @filter.llm_tool(name="creator_upload_video")
    async def creator_upload_video(self, event, video_source: str, title: str, tags: str = "", platform: str = "douyin"):
        """把视频发布到管理员的短视频账号（仅管理员可用）。不会立即发布：会生成确认码，管理员发送 /va_confirm <确认码> 后才真正发布。

        Args:
            video_source(string): 视频直链URL或 AstrBot data 目录下的本地路径（例如用户发来视频时上下文里给出的直链）
            title(string): 作品标题/文案，30 字以内
            tags(string): 话题，空格分隔，不带 #
            platform(string): 平台，目前支持 douyin
        """
        from .tools.creator_tool import handle_creator_upload
        result = await handle_creator_upload(self, event, video_source, title, tags, platform)
        return json.dumps(result, ensure_ascii=False)

