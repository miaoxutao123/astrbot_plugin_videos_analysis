"""
抖音创作者操作：作品列表 / 发布 / 删除 / 评论读取与回复

全部复用 BrowserSigner 的已登录 Chromium：
- 读接口（评论列表、作品详情）走 www.douyin.com 页面内 fetch，SDK 自动签名，未登录也能读
- 作品列表在创作者中心作品管理页拦截页面自己发出的 work_list 响应，不用自己算签名
- 发布/删除用页面自动化（creator.douyin.com），失败时截图，方便对照页面改版修选择器
- 防风控：页面交互用 human.py 的人类化点击/逐字输入；写操作过 WriteGuard 频率闸门；
  打开页面和关键操作后检查验证码，出现就熔断（与签名请求共用 BrowserSigner 的风控状态）
"""

import asyncio
import os
import re
import time

from astrbot.api import logger

from ...utils.paths import IMAGE_DIR
from .base import Comment, CreatorPlatform, OpResult, Work
from .human import GUARD, human_click, human_fill, human_hover, human_type, pause

CREATOR = "https://creator.douyin.com"
UPLOAD_URL = f"{CREATOR}/creator-micro/content/upload"
MANAGE_URL = f"{CREATOR}/creator-micro/content/manage"
# 设备参数（系统/浏览器/屏幕等）由 fetch_signed_api 按浏览器真实环境填充
WEB_PARAMS = {
    "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
    "pc_client_type": "1", "version_code": "170400", "version_name": "17.4.0",
    "platform": "PC",
}


class CaptchaError(RuntimeError):
    pass


class DouyinCreator(CreatorPlatform):
    name = "douyin"
    label = "抖音"

    @property
    def signer(self):
        s = self.plugin.browser_signer
        if not s or not s.is_ready:
            raise RuntimeError(f"抖音签名引擎未就绪：{self.plugin.browser_signer_error or '请先 /dy_restart'}")
        if s.risk_status:
            raise RuntimeError(f"抖音风控冷却中：{s.risk_status}")
        return s

    async def _screenshot(self, page, tag: str) -> str:
        try:
            os.makedirs(IMAGE_DIR, exist_ok=True)
            path = os.path.join(IMAGE_DIR, f"creator_{tag}_{int(time.time())}.png")
            await page.screenshot(path=path, full_page=False)
            return path
        except Exception:
            return ""

    async def _creator_page(self, url: str):
        page = await self.signer.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await pause(1.0, 2.5)  # 页面打开后先"看一眼"，不立刻操作
            await self._check_captcha(page)
        except Exception:
            await page.close()
            raise
        return page

    async def _check_captcha(self, page, when: str = "打开页面") -> None:
        """出现验证码就熔断并中止，不在验证码面前继续点。"""
        s = self.plugin.browser_signer
        if s and await s.has_captcha(page):
            shot = await self._screenshot(page, "captcha")
            s.trip_risk(f"创作者中心{when}时弹出验证码")
            raise CaptchaError(f"创作者中心弹出验证码，已暂停抖音操作（{s.risk_status}）" + (f"\n失败截图: {shot}" if shot else ""))

    @staticmethod
    def _guard_refused(reason: str) -> OpResult:
        return OpResult(False, f"已拦截：{reason}（防风控频率限制）")

    @staticmethod
    async def _is_login_page(page) -> bool:
        if "login" in page.url or not page.url.startswith(CREATOR):
            return True
        for text in ("扫码登录", "手机号登录", "验证码登录"):
            if await page.get_by_text(text).count():
                return True
        return False

    # ── 读 ───────────────────────────────────────────────

    async def check_login(self) -> tuple[bool, str]:
        if not self.signer.is_logged_in:
            return False, "未登录"
        page = await self._creator_page(UPLOAD_URL)
        try:
            await asyncio.sleep(3)
            if await self._is_login_page(page):
                return False, "创作者中心要求重新登录（cookie 可能已过期）"
            return True, "创作者中心可用"
        finally:
            await page.close()

    async def list_works(self, limit: int = 10) -> list[Work]:
        page = await self.signer.new_page()
        try:
            async with page.expect_response(lambda r: "work_list" in r.url, timeout=30000) as resp_info:
                await page.goto(MANAGE_URL, wait_until="domcontentloaded", timeout=30000)
            data = await (await resp_info.value).json()
            await self._check_captcha(page)
            if data.get("status_code") not in (0, None) or await self._is_login_page(page):
                raise RuntimeError(f"创作者中心未登录或登录已失效（{data.get('status_msg') or data.get('status_code')}），先 /dy_login 扫码")
        except RuntimeError:
            raise
        except Exception as e:
            await self._check_captcha(page)
            if await self._is_login_page(page):
                raise RuntimeError("创作者中心未登录，先 /dy_login 扫码") from e
            raise RuntimeError(f"读取作品列表失败（页面可能改版）: {e}") from e
        finally:
            await page.close()
        works = []
        for a in (data.get("aweme_list") or [])[:limit]:
            st = a.get("statistics") or {}
            works.append(Work(
                id=str(a.get("aweme_id")), title=a.get("desc") or a.get("item_title") or "(无标题)",
                create_time=int(a.get("create_time") or 0), digg=st.get("digg_count", 0),
                comments=st.get("comment_count", 0), url=f"https://www.douyin.com/video/{a.get('aweme_id')}",
            ))
        return works

    async def resolve_work(self, ref: str) -> str | None:
        ref = ref.strip()
        if ref.isdigit() and len(ref) > 10:
            return ref
        m = re.search(r"/(?:video|note)/(\d+)", ref) or re.search(r"modal_id=(\d+)", ref)
        if m:
            return m.group(1)
        m = re.search(r"https?://v\.douyin\.com/[\w\-]+/?", ref)
        if m:
            parser = self.plugin.make_douyin_parser()
            return await parser.id_fetcher.get_aweme_id(m.group(0), proxy=parser.proxy)
        return None

    async def work_context(self, work_id: str) -> dict:
        info = await self.signer.parse_video(work_id)
        if not info and await self.signer.refresh():
            info = await self.signer.parse_video(work_id)
        info = info or {}
        urls = info.get("media_urls") or []
        return {
            "title": info.get("desc") or "",
            "author": info.get("author_nickname") or "",
            "media_url": urls[0] if info.get("type") == "video" and urls else "",
        }

    async def list_comments(self, work_id: str, max_count: int = 60) -> list[Comment]:
        out, cursor = [], 0
        while len(out) < max_count:
            params = {**WEB_PARAMS, "aweme_id": work_id, "cursor": cursor, "count": 20, "item_type": 0}
            data = await self.signer.fetch_signed_api("aweme/v1/web/comment/list/", params)
            if not data:
                if not out and await self.signer.refresh():
                    data = await self.signer.fetch_signed_api("aweme/v1/web/comment/list/", params)
                if not data:
                    break
            for c in data.get("comments") or []:
                user = c.get("user") or {}
                if any(o.id == str(c.get("cid")) for o in out):
                    continue  # 翻页结果有重叠
                out.append(Comment(
                    id=str(c.get("cid")), text=c.get("text", ""), digg=int(c.get("digg_count") or 0),
                    create_time=int(c.get("create_time") or 0), user=user.get("nickname", ""),
                    user_id=str(user.get("uid", "")),
                ))
            if not data.get("has_more"):
                break
            cursor = data.get("cursor", cursor + 20)
        return out

    # ── 写（仅供二次确认后调用） ──────────────────────────

    async def reply_comment(self, work_id: str, comment_id: str, text: str) -> OpResult:
        if refused := await GUARD.acquire(self.name, "reply"):
            return self._guard_refused(refused)
        form = {
            "aweme_id": work_id, "text": text, "reply_id": comment_id, "reply_to_reply_id": "0",
            "text_extra": "[]", "paste_edit_method": "non_paste", "comment_source": "0",
        }
        data = await self.signer.post_form("aweme/v1/web/comment/publish", WEB_PARAMS, form)
        if data and data.get("status_code") == 0:
            return OpResult(True, "已回复", {"cid": (data.get("comment") or {}).get("cid")})
        if not data:
            msg = "接口无响应（可能触发风控）"
        elif data.get("status_code") == 8:
            msg = "未登录，先 /dy_login 扫码"
        else:
            msg = data.get("status_msg") or f"status_code={data.get('status_code')}"
        return OpResult(False, f"回复失败: {msg}", data or {})

    async def upload(self, video_path: str, title: str, tags: list[str]) -> OpResult:
        if refused := await GUARD.acquire(self.name, "upload"):
            return self._guard_refused(refused)
        try:
            page = await self._creator_page(UPLOAD_URL)
        except CaptchaError as e:
            return OpResult(False, str(e))
        try:
            if await self._is_login_page(page):
                return OpResult(False, "创作者中心未登录，先 /dy_login 扫码", screenshot=await self._screenshot(page, "upload_login"))
            await page.locator("input[type=file]").first.set_input_files(video_path)
            await page.wait_for_url(re.compile(r".*/content/(publish|post/video).*"), timeout=60000)

            await pause(1.5, 3.0)
            await self._check_captcha(page, "上传视频")
            title_input = page.locator("input[placeholder*='标题']")
            if await title_input.count():
                await human_fill(page, title_input.first, title[:30])
                await pause()
            editor = page.locator(".zone-container, [contenteditable='true']").first
            await human_click(page, editor)
            if not await title_input.count():
                await human_type(page, title)
            for tag in tags:
                await human_type(page, f" #{tag}")
                await pause(0.6, 1.2)
                await page.keyboard.press("Space")

            # 等视频上传完成：出现“重新上传”说明文件已传完；“上传失败”直接退出
            for _ in range(300):
                if await page.get_by_text("上传失败").count():
                    return OpResult(False, "抖音提示上传失败", screenshot=await self._screenshot(page, "upload_fail"))
                if await page.get_by_text("重新上传").count():
                    break
                await asyncio.sleep(2)
            else:
                return OpResult(False, "等待上传完成超时（10 分钟）", screenshot=await self._screenshot(page, "upload_timeout"))

            await pause(2.0, 4.0)  # 发布前停一下，像在检查内容
            await human_click(page, page.get_by_role("button", name="发布", exact=True))
            await pause(1.5, 3.0)
            await self._check_captcha(page, "发布")
            try:
                await page.wait_for_url(re.compile(r".*/content/manage.*"), timeout=60000)
            except Exception:
                return OpResult(False, "点了发布但没跳转到作品管理页（可能需要验证码/审核弹窗）", screenshot=await self._screenshot(page, "publish"))
            return OpResult(True, "已发布，抖音审核通过后可见")
        except CaptchaError as e:
            return OpResult(False, str(e))
        except Exception as e:
            logger.warning(f"[creator] 抖音发布失败: {e}")
            return OpResult(False, f"发布失败: {e}", screenshot=await self._screenshot(page, "upload_err"))
        finally:
            await page.close()

    async def delete(self, work_id: str, title: str = "") -> OpResult:
        if not title:
            works = await self.list_works(limit=50)
            title = next((w.title for w in works if w.id == work_id), "")
            if not title:
                return OpResult(False, f"在最近 50 个作品里没找到 {work_id}")
        if refused := await GUARD.acquire(self.name, "delete"):
            return self._guard_refused(refused)
        try:
            page = await self._creator_page(MANAGE_URL)
        except CaptchaError as e:
            return OpResult(False, str(e))
        try:
            await pause(1.5, 3.0)
            if await self._is_login_page(page):
                return OpResult(False, "创作者中心未登录", screenshot=await self._screenshot(page, "delete_login"))
            # 作品卡片：包含标题文本的最近一个带“删除”入口的祖先
            title_el = page.get_by_text(title[:20], exact=False).first
            await title_el.wait_for(timeout=15000)
            card = title_el.locator("xpath=ancestor::*[.//*[contains(text(),'删除')] or .//*[contains(text(),'更多')]][1]")
            await human_hover(page, card)  # 鼠标移到卡片上，露出操作入口
            await pause()
            more = card.get_by_text("更多")
            if await more.count():
                await human_click(page, more.first)
                await pause()
            await human_click(page, page.get_by_text("删除作品").or_(card.get_by_text("删除", exact=True)).first)
            await pause(0.8, 1.8)
            await human_click(page, page.get_by_role("button", name=re.compile("确定|确认|删除")).last)
            await pause(2.5, 4.0)
            await self._check_captcha(page, "删除")
            if await page.get_by_text(title[:20], exact=False).count():
                return OpResult(False, "点了删除但作品仍在列表中", screenshot=await self._screenshot(page, "delete_still"))
            return OpResult(True, "已删除")
        except CaptchaError as e:
            return OpResult(False, str(e))
        except Exception as e:
            logger.warning(f"[creator] 抖音删除失败: {e}")
            return OpResult(False, f"删除失败: {e}", screenshot=await self._screenshot(page, "delete_err"))
        finally:
            await page.close()
