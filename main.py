"""
AstrBot 视频解析与理解插件
支持平台：抖音、B站、MC百科
功能：被动链接解析（默认关闭）、Agent 主动调用视频解析/理解工具、账号运维（账号池轮换/定时巡检/状态页）、
     创作者操作（发布/删除/评论精选回复，二次确认）
"""

import asyncio
import json
import os
import sys

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star, register

from .douyin_login import load_douyin_cookies
from .douyin_scraper.browser_signer import BrowserSigner
from .douyin_scraper.douyin_parser import DOUYIN_LINK, DouyinParser
from .services.account_manager import AccountManager
from .services.creator_ops import CreatorOps
from .services.plugin_admins import COMMAND_PREFIXES, PluginAdmins
from .utils.paths import DOUYIN_BROWSER_PROFILE, migrate_legacy_data
from .utils.redact import redact

# 旧版 _conf_schema 的默认值是个不存在的地址，按未填写处理
_BOGUS_GEMINI_BASE = "https://generative-api.google.com/v1alpha2"


@register(
    "astrbot_plugin_videos_analysis",
    "视频解析与理解",
    "抖音/B站/MC百科视频自动解析与AI理解",
    "0.14.1",
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
        # 默认只用"抽帧 + ASR + 当前对话模型"；Gemini / MiMo 需要在配置里显式开启
        self.video_understand_method = config.get("video_understand_method", "llm")
        self.gemini_enabled = bool(config.get("gemini_enabled", False))
        self.mimo_enabled = bool(config.get("mimo_enabled", False))

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

        self.admins = PluginAdmins(self)
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

        # 浏览器放到后台启动：解析公开视频、创作中心数据都走 HTTP，不必等 Chromium（冷启动 5~30 秒）
        self._signer_task = None
        if self.douyin_browser_signer_enabled:
            self.browser_signer_error = "正在后台启动，稍等十几秒"
            self._signer_task = asyncio.create_task(self._start_signer_background())
        else:
            self.browser_signer_error = "已在配置中禁用"

        from .handlers.web_api import register_web_apis
        register_web_apis(self)
        self.accounts.start_scheduler()
        self.creator_ops.start()
        from .services.ops_monitor import OpsMonitor
        self.monitor = OpsMonitor(self)
        self.monitor.start()

    async def _start_signer_background(self) -> None:
        try:
            status = await self.start_browser_signer()
            reason = status.get("reason", "")
            if status.get("ok"):
                await self._maybe_install_chrome()
            if not status.get("ok") and ("Executable doesn't exist" in reason or "playwright install" in reason):
                # 首次部署常见：装了 playwright 包但没下载浏览器，自动下载一次（约 150MB）
                self.browser_signer_error = "正在自动下载 Chromium（约 150MB，几分钟）"
                logger.info("[setup] 未找到 Chromium，正在执行 playwright install chromium ...")
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "playwright", "install", "chromium",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
                if proc.returncode == 0:
                    logger.info("[setup] Chromium 下载完成，重新启动抖音浏览器")
                    await self.start_browser_signer()
                else:
                    self.browser_signer_error = ("自动下载 Chromium 失败，请在 AstrBot 所在环境执行: "
                                                 "python -m playwright install --with-deps chromium")
                    logger.warning(f"[setup] playwright install 失败: {out.decode(errors='ignore')[-500:]}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.browser_signer_error = f"启动异常: {e}"
            logger.warning(f"[setup] 后台启动抖音浏览器失败: {e}")

    async def _maybe_install_chrome(self) -> None:
        """
        用的是 Playwright 自带的 Chromium 时，后台装一份正式版 Google Chrome，下次启动浏览器时自动换过去。

        Chromium 的 Client Hints 品牌里没有 "Google Chrome"，而普通用户几乎都用正式版，
        这是网页端最容易被识别的环境差异之一。只在 Linux 且以 root 运行（容器里，能用 apt 装依赖）时自动装；
        正式版比配置目录的版本旧时 BrowserSigner 不会切换，避免损坏登录态。
        """
        s = self._browser_signer
        if (not s or s.browser_name != "Chromium" or not self.config.get("douyin_browser_install_chrome", True)
                or not sys.platform.startswith("linux") or getattr(os, "geteuid", lambda: 1)() != 0):
            return
        logger.info("[setup] 抖音浏览器是 Chromium，后台安装正式版 Google Chrome（约 120MB）...")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "playwright", "install", "chrome",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
        except (OSError, asyncio.TimeoutError) as e:
            logger.warning(f"[setup] 安装 Google Chrome 失败: {e}")
            return
        if proc.returncode == 0:
            logger.info("[setup] Google Chrome 安装完成，下次重载插件或 /dy_restart 时切换（登录态保留）")
        else:
            logger.warning(f"[setup] 安装 Google Chrome 失败: {out.decode(errors='ignore')[-300:]}")

    async def start_browser_signer(self) -> dict:
        """启动（或重启）抖音浏览器签名引擎，返回状态字典。"""
        if self._browser_signer:
            await self._browser_signer.stop()
            self._browser_signer = None
        try:
            signer = BrowserSigner(headless=False, user_data_dir=DOUYIN_BROWSER_PROFILE,
                                   low_cpu=bool(self.config.get("douyin_browser_low_cpu", True)))
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
            logger.warning("公开视频解析、创作中心数据不受影响；搜索、评论、发布、扫码登录暂不可用")
            if signer:
                await signer.stop()
        return status

    # ==================== 对其他插件开放的接口（如视频生成插件） ====================

    async def publish_request(self, event, video_path: str, title: str, tags: list[str] | None = None,
                              platform: str = "douyin", account: str | None = None, **opts) -> dict:
        """
        其他插件申请把视频发布到机器人的平台账号（与 LLM 工具同样走审批，管理员确认后才发布）。

        用法（在别的插件里）::

            star = self.context.get_registered_star("astrbot_plugin_videos_analysis")
            res = await star.star_cls.publish_request(event, "/abs/path/out.mp4", "标题", ["话题"], "douyin")
            # res["status"] in {"pending_confirmation", "pending_admin_approval", "rejected"}

        video_path 可以是本地文件或 http(s) 直链；返回 dict，不会抛异常。
        B站可选 opts：cover=封面图路径/直链，tid=分区 ID，desc=简介（不给封面就取视频第 1 秒）。
        通用 opts：confirm_ttl=审批有效期（秒，最长 24 小时；默认 publish_confirm_minutes，12 小时）；
        caller=你的插件名、caller_ref=你的任务号——发布成功/失败/被拒/过期后会调用
        ``await <caller 插件>.on_publish_result(result: dict)``，result 含 status(published/failed/rejected/expired)、
        code、platform、title、work_id、url、message、caller_ref。待审批申请保存在磁盘上，本插件重载不会丢。
        """
        from .services.creator import normalize_platform

        plat = normalize_platform(platform)
        if not plat:
            return {"status": "rejected", "error": f"不支持的平台: {platform}"}
        try:
            op = await self.creator_ops.prepare_upload(event, plat, video_path, title, list(tags or []), account, opts)
        except Exception as e:
            return {"status": "rejected", "error": f"准备发布失败: {e}"}
        return await self.creator_ops.submit(op)

    async def post_request(self, event, images: list[str], title: str = "", text: str = "", tags: list[str] | None = None,
                           platform: str = "douyin", account: str | None = None, **opts) -> dict:
        """
        其他插件申请发布图文（与 publish_request 相同：走审批，管理员确认后才发布；返回 dict，不会抛异常）。

        images：本地图片路径或 http(s) 链接（抖音最多 35 张，B站最多 18 张）；title 最多 20 字，可只给 text。
        opts：visibility=public/friends/private，ai_generated=True 声明“内容由 AI 生成”，
        以及 confirm_ttl / caller / caller_ref（结局回调 on_publish_result，result["kind"] == "images"）。
        """
        from .services.creator import normalize_platform

        plat = normalize_platform(platform)
        if not plat:
            return {"status": "rejected", "error": f"不支持的平台: {platform}"}
        try:
            op = await self.creator_ops.prepare_post(event, plat, list(images or []), title, text, list(tags or []), account, opts)
        except Exception as e:
            return {"status": "rejected", "error": f"准备图文失败: {redact(e)}"}
        return await self.creator_ops.submit(op)

    async def ops_insight(self, platform: str = "", days: int = 7, refresh: bool = False) -> dict:
        """
        给内容生产插件（如 media_gen）选题用的运营摘要，只读、不需要审批，结果缓存 10 分钟。

        返回 {"days", "platforms": [{platform, label, account, totals, change, period, vs_similar,
        recent_works, best_works, posts_in_period, trending, our_publishes} 或 {platform, error}]}；不会抛异常。
        """
        from .services import ops_monitor

        try:
            return await ops_monitor.insight(self, platform, days, refresh)
        except Exception as e:
            logger.warning(f"[insight] 生成运营洞察失败: {e}")
            return {"days": days, "platforms": [], "error": redact(e)[:200]}

    def publish_history(self, caller: str = "", limit: int = 20) -> list[dict]:
        """发布结果记录（新→旧），可按来源插件过滤；与 on_publish_result 回调内容相同。"""
        from .services.creator_ops import read_publish_log

        rows = read_publish_log(500)
        return [r for r in rows if not caller or r.get("caller") == caller][:max(1, int(limit))]

    async def terminate(self):
        """插件卸载/重载时关闭浏览器与 Xvfb，避免进程泄漏。"""
        task = getattr(self, "_signer_task", None)
        if task and not task.done():
            task.cancel()
        await self.accounts.stop_scheduler()
        await self.creator_ops.stop()
        if getattr(self, "monitor", None):
            await self.monitor.stop()
        if self._browser_signer:
            await self._browser_signer.stop()
            self._browser_signer = None

    # ==================== 被动解析 ====================

    @filter.event_message_type(EventMessageType.ALL)
    @filter.regex(DOUYIN_LINK.pattern)
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
    @filter.command("va_setup", alias={"插件体检", "快速配置"})
    async def setup_command(self, event):
        """一键体检：环境、账号、模型、转写、告警会话，逐项给出下一步"""
        from .services import settings_service as ss

        yield event.plain_result(ss.render_checklist(await ss.checklist(self, event)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("va_check")
    async def accounts_check_command(self, event):
        """立即深度巡检所有平台账号"""
        from .handlers.account_ops_handler import handle_health_check
        async for response in handle_health_check(self, event):
            yield response

    @filter.event_message_type(EventMessageType.ALL)
    async def observe_admins(self, event):
        """记下 AstrBot 管理员的私聊会话；管理员私聊里用本插件命令、且还没设置插件管理员时，问一次要不要设置。"""
        self.admins.observe(event)
        text = (event.message_str or "").strip().lstrip("/")
        if (text.startswith(COMMAND_PREFIXES) and not text.startswith("va_admins") and event.is_private_chat()
                and self.admins.should_ask(event)):
            asyncio.create_task(self._ask_admins(event.unified_msg_origin))
        if False:  # 本 handler 不回复消息，只是让 AstrBot 把它当作异步生成器
            yield

    async def _ask_admins(self, umo: str) -> None:
        from astrbot.core.message.message_event_result import MessageChain

        await asyncio.sleep(2)  # 等命令自己的回复先发出去
        try:
            await self.context.send_message(umo, MessageChain().message(self.admins.prompt()))
        except Exception as e:
            logger.warning(f"[admins] 发送插件管理员设置提示失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("va_admins", alias={"插件管理员"})
    async def plugin_admins_command(self, event):
        """从 AstrBot 管理员里选本插件管理员：/va_admins [1 3|全部|我|清空|跳过]"""
        from .handlers.account_ops_handler import command_args

        yield event.plain_result(self.admins.select(event, command_args(event, "va_admins")))

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
    @filter.command("va_post", alias={"发图文"})
    async def creator_post_command(self, event):
        """发布图文：/va_post [平台] [仅自己可见] [AI] <标题> #话题，第二行起正文，图片附在消息里或引用图片消息"""
        from .handlers.account_ops_handler import handle_post
        async for response in handle_post(self, event):
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

    @filter.event_message_type(EventMessageType.ALL)
    @filter.regex(r"^\s*(确认|同意|批准|执行|拒绝|取消|驳回)\s*(\d{4})((?:\s+\d{1,2})*)\s*$")
    async def plain_approval(self, event):
        """管理员直接回复「确认 1234」「拒绝 1234」完成审批（必须是管理员本人发的消息）。"""
        import re as _re

        m = _re.match(r"^\s*(确认|同意|批准|执行|拒绝|取消|驳回)\s*(\d{4})((?:\s+\d{1,2})*)\s*$", event.message_str or "")
        if not m or not event.is_admin() or not self.creator_ops.has_pending(m.group(2)):
            return  # 不是待审批的码，交给其他插件/LLM 正常处理
        if not self.admins.is_admin(event):
            yield event.plain_result(f"这条申请由本插件管理员（{self.admins.names()}）审批")
            event.stop_event()
            return
        verb, code = m.group(1), m.group(2)
        if verb in ("拒绝", "取消", "驳回"):
            result = self.creator_ops.cancel(code, event)
        else:
            picks = [int(x) for x in m.group(3).split()] or None
            result = await self.creator_ops.confirm(code, picks, event)
        # 结果发完再停止事件：提前 stop 会让 AstrBot 在第一条消息后就不再执行本 handler；
        # 停止事件也避免这条"确认"被当成普通对话交给 LLM
        yield event.plain_result(result)
        event.stop_event()

    @filter.command("va_download", alias={"下载视频"})
    async def download_command(self, event):
        """下载视频发给我：/va_download <链接|BV号|序号>"""
        from .tools import ops_tools

        args = (event.message_str or "").split()[1:]
        if not args:
            yield event.plain_result("用法: /va_download <B站/抖音链接|BV号|列表序号>")
            return
        res = await ops_tools.video_download(self, event, args[0])
        if res.get("error"):
            yield event.plain_result(res["error"])

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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("va_audit", alias={"操作记录"})
    async def creator_audit_command(self, event):
        """操作审计：/va_audit [条数] [平台] [申请码]"""
        from .handlers.account_ops_handler import handle_audit
        async for response in handle_audit(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("va_report", alias={"运营数据"})
    async def ops_report_command(self, event):
        """运营数据看板：/va_report [平台] [天数]"""
        from .handlers.account_ops_handler import handle_report
        async for response in handle_report(self, event):
            yield response

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("va_inbox", alias={"消息概况"})
    async def ops_inbox_command(self, event):
        """消息概况（未读、最新评论/@、系统通知）：/va_inbox [平台]"""
        from .handlers.account_ops_handler import handle_inbox
        async for response in handle_inbox(self, event):
            yield response

    # ==================== 主动触发命令 ====================

    @filter.command("理解视频")
    async def comprehend_video_command(self, event):
        """主动触发视频AI理解"""
        from .handlers.video_handler import handle_comprehend_video_command
        async for response in handle_comprehend_video_command(self, event):
            yield response

    # ==================== LLM Agent 工具 ====================

    # 工具描述保持静态且简短：不写入账号名/配置值等动态内容（保证提示词缓存稳定），
    # 详细信息由工具结果按需返回；危险操作只提交申请，批准必须由管理员本人发消息完成。

    async def _run_tool(self, fn, event, *args):
        """统一执行 LLM 工具：异常转成 error 返回，结果紧凑序列化为 JSON。"""
        try:
            result = await fn(self, event, *args)
        except Exception as e:
            logger.warning(f"[llm_tool] {fn.__name__} 失败: {e}")
            result = {"error": str(e)[:300]}
        # 工具结果会进入对话历史并发给模型服务商，兜底脱敏
        return redact(json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")))

    @filter.llm_tool(name="video_browse")
    async def llm_video_browse(self, event, action: str, query: str = "", question: str = "", platform: str = ""):
        """浏览B站或抖音。search=搜索；recommend=首页推荐；hot=热门（抖音为热搜词）；watch=真正看视频内容（抽帧+语音转写后总结，附热评），用户问“讲了什么/内容/怎么样”时必须用 watch；info=只查标题、播放量、直链等信息，不含内容。列表结果带序号 i，后续可直接用序号。

        Args:
            action(string): search / recommend / hot / watch / info
            query(string): search 填关键词；watch/info 填 BV号、B站或抖音链接、或列表序号
            question(string): 可选，watch 时用户特别想了解的问题
            platform(string): search/recommend/hot 用：bilibili 或 douyin，默认 bilibili；watch 自动识别
        """
        from .tools import ops_tools
        return await self._run_tool(ops_tools.video_browse, event, action, query, question, platform)

    @filter.llm_tool(name="video_download")
    async def llm_video_download(self, event, video: str):
        """下载B站/抖音视频（或抖音图文）并直接发到当前聊天。

        Args:
            video(string): 视频链接、BV号，或列表序号
        """
        from .tools import ops_tools
        return await self._run_tool(ops_tools.video_download, event, video)

    @filter.llm_tool(name="video_account")
    async def llm_video_account(self, event, action: str, platform: str = "", target: str = "", account: str = ""):
        """查看机器人自己的平台账号（仅管理员）。status=各平台登录状态与账号池；works=最近作品；comments=某作品的评论；profile=账号资料；audit=最近的操作申请与审批记录；report=运营数据（粉丝/播放等及变化、同类对比）；inbox=未读消息与最新评论/@/系统通知。

        Args:
            action(string): status / works / comments / profile / audit / report / inbox
            platform(string): bilibili 或 douyin，默认 bilibili；report/inbox 留空=全部平台
            target(string): comments 时填作品序号/BV号/链接；audit 可填申请码；report 可填对比天数（默认 7）
            account(string): 账号池里的账号名，留空=当前账号
        """
        from .tools import ops_tools
        return await self._run_tool(ops_tools.video_account, event, action, platform, target, account)

    @filter.llm_tool(name="video_action")
    async def llm_video_action(self, event, action: str, target: str = "", text: str = "", platform: str = "", account: str = "",
                               title: str = "", tags: str = "", options: str = ""):
        """用机器人的平台账号执行操作。不会立即执行，只提交申请，管理员本人确认后才执行；拿到结果后如实告诉用户“已提交审批”，不要说已完成，也不要替用户确认。
comment=在视频下发评论（text 留空则自动起草）；reply=回复自己作品的评论（text 可填只回复某人的昵称/UID）；sign=改签名；avatar=换头像（用用户发的图）；delete=删除作品；switch_account=切换当前账号；upload=发布视频；post=发布图文（用用户发的图片）。

        Args:
            action(string): comment / reply / sign / avatar / delete / switch_account / upload / post
            target(string): comment/delete/reply 填视频或作品（序号/BV号/链接）；switch_account 填账号名；upload 填视频直链；avatar/post 可填图片链接（post 多张用空格分隔，留空用用户发的图）
            text(string): 评论内容 / 新签名 / reply 时只回复的人 / upload 时的标题 / post 的正文
            platform(string): bilibili 或 douyin，可留空自动识别（post 默认 douyin）
            account(string): 用账号池里哪个账号，留空=当前账号
            title(string): upload/post 的标题（post 最多 20 字）
            tags(string): upload/post 的话题，空格分隔
            options(string): upload/post 可选：仅自己可见 / 好友可见，AI（声明内容由 AI 生成）
        """
        from .tools import ops_tools
        return await self._run_tool(ops_tools.video_action, event, action, target, text, platform, account, title, tags, options)

    @filter.llm_tool(name="video_plugin_settings")
    async def llm_video_plugin_settings(self, event, action: str, setting: str = "", value: str = ""):
        """查看或修改视频插件的设置（仅管理员），用于用自然语言配置插件。show=列出所有设置（中文名、说明、当前值）；check=体检，告诉用户还缺什么配置；set=修改（需管理员确认后生效）。不知道设置名时先 show。

        Args:
            action(string): show / check / set
            setting(string): set 时填设置的中文名或键名，或预设名（如 siliconflow_asr）
            value(string): set 时的新值；一次改多项可填 JSON 对象如 {"浏览抽帧数": 6}
        """
        from .tools import ops_tools
        return await self._run_tool(ops_tools.video_plugin_settings, event, action, setting, value)
