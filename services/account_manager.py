"""
账号管理：统一查看/注销各平台登录态与 AI 服务配置

目前覆盖：抖音（浏览器签名 + cookie）、B站（扫码 cookie）、Gemini、MiMo。
后续的账号运维功能（多账号轮换、定时巡检、失效提醒等）建议都挂在这里，
handler 只负责把 AccountStatus 渲染成消息。
"""

import os
import shutil
import time
from dataclasses import dataclass, field

import aiohttp

from astrbot.api import logger

from ..douyin_scraper.cookie_extractor import extract_douyin_cookies
from ..utils.paths import BILI_COOKIE_FILE, DOUYIN_BROWSER_PROFILE, DOUYIN_COOKIE_FILE
from . import bilibili_service
from .gemini_service import test_gemini

PLATFORMS = ("douyin", "bilibili", "gemini", "mimo")


@dataclass
class AccountStatus:
    platform: str
    ok: bool
    summary: str
    details: dict = field(default_factory=dict)
    checked_at: float = field(default_factory=time.time)

    def render(self) -> str:
        icon = "✅" if self.ok else "⚠️"
        lines = [f"{icon} {self.platform}: {self.summary}"]
        lines += [f"    · {k}: {v}" for k, v in self.details.items() if v not in (None, "")]
        return "\n".join(lines)


class AccountManager:
    def __init__(self, plugin):
        self.plugin = plugin

    async def status(self, platform: str, deep: bool = False) -> AccountStatus:
        checker = {
            "douyin": self._douyin,
            "bilibili": self._bilibili,
            "gemini": self._gemini,
            "mimo": self._mimo,
        }[platform]
        try:
            return await checker(deep)
        except Exception as e:
            logger.warning(f"[accounts] 检查 {platform} 失败: {e}")
            return AccountStatus(platform, False, f"检查失败: {e}")

    async def status_all(self, deep: bool = False) -> list[AccountStatus]:
        return [await self.status(p, deep) for p in PLATFORMS]

    # ── 各平台 ───────────────────────────────────────────

    async def _douyin(self, deep: bool) -> AccountStatus:
        p = self.plugin
        signer = p.browser_signer
        cookie = p.effective_douyin_cookie
        source = (
            "扫码/手动注入" if p._douyin_cookie_from_scan
            else "cookie 文件" if p._douyin_cookie_from_file
            else "插件配置" if p._douyin_cookie_from_config
            else "无"
        )
        _, has_login_fields, _ = extract_douyin_cookies(cookie) if cookie else ({}, False, [])
        signer_state = (
            "已就绪" if signer and signer.is_ready
            else "已禁用" if not p.douyin_browser_signer_enabled
            else f"未就绪 ({p.browser_signer_error or '未知原因'})"
        )
        logged_in = bool(signer and signer.is_logged_in) or has_login_fields
        ok = bool(signer and signer.is_ready)
        summary = ("可解析" if ok else "无法解析（纯 Python 签名已失效，需要浏览器签名引擎）") + ("，已登录" if logged_in else "，未登录")
        return AccountStatus("douyin", ok, summary, {"签名引擎": signer_state, "cookie 来源": source})

    async def _bilibili(self, deep: bool) -> AccountStatus:
        cookies = await bilibili_service.load_cookies()
        if not cookies:
            return AccountStatus("bilibili", True, "未登录（可解析，最高 720P）", {"登录": "管理员发送 /bili_login 扫码"})
        info = await bilibili_service.get_login_info(cookies)
        if info and info.get("isLogin"):
            vip = info.get("vipStatus") == 1
            return AccountStatus(
                "bilibili", True, f"已登录: {info.get('uname')}",
                {"mid": info.get("mid"), "大会员": "是" if vip else "否", "使用登录态": "是" if self.plugin.bili_use_login else "否（bili_use_login 未开启）"},
            )
        return AccountStatus("bilibili", False, "cookie 已失效", {"处理": "发送 /bili_login 重新扫码"})

    async def _gemini(self, deep: bool) -> AccountStatus:
        from ..utils.config_helper import resolve_gemini_config

        gcfg = resolve_gemini_config(self.plugin)
        if not gcfg:
            return AccountStatus("gemini", False, "未配置", {"处理": "填写 gemini_api_key / gemini_base_url"})
        details = {"来源": gcfg.source, "地址": gcfg.root, "模型": gcfg.model, "key": f"***{gcfg.api_key[-4:]}"}
        if not deep:
            return AccountStatus("gemini", True, "已配置（加 deep 参数可测试连通性）", details)
        ok, msg = await test_gemini(gcfg)
        details["测试"] = msg
        return AccountStatus("gemini", ok, "连通正常" if ok else "调用失败", details)

    async def _mimo(self, deep: bool) -> AccountStatus:
        p = self.plugin
        if not p.mimo_api_key:
            return AccountStatus("mimo", False, "未配置（可选）")
        details = {"地址": p.mimo_api_base, "模型": p.mimo_model, "key": f"***{p.mimo_api_key[-4:]}"}
        if deep:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                    async with s.get(f"{p.mimo_api_base.rstrip('/')}/models", headers={"Authorization": f"Bearer {p.mimo_api_key}"}) as r:
                        details["测试"] = f"HTTP {r.status}"
                        return AccountStatus("mimo", r.status == 200, "连通正常" if r.status == 200 else "调用失败", details)
            except Exception as e:
                details["测试"] = str(e)[:120]
                return AccountStatus("mimo", False, "调用失败", details)
        return AccountStatus("mimo", True, "已配置", details)

    # ── 注销 ─────────────────────────────────────────────

    async def logout(self, platform: str) -> str:
        p = self.plugin
        if platform == "bilibili":
            if os.path.exists(BILI_COOKIE_FILE):
                os.remove(BILI_COOKIE_FILE)
            return "已清除 B站 登录 cookie。"
        if platform == "douyin":
            if p.browser_signer:
                await p.browser_signer.stop(save=False)
                p._browser_signer = None
            for path in (DOUYIN_COOKIE_FILE, DOUYIN_BROWSER_PROFILE):
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.exists(path):
                    os.remove(path)
            p._douyin_cookie_from_scan = None
            p._douyin_cookie_from_file = None
            await p.start_browser_signer()
            return "已清除抖音登录态并重启签名引擎（插件配置里的 doyin_cookie 不受影响）。"
        return f"不支持注销 {platform}，可选: douyin / bilibili"
