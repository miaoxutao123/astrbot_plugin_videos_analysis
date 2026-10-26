"""
抖音创作者操作：作品列表 / 发布 / 删除 / 评论读取与回复 / 浏览（推荐、搜索、热榜）/ 发评论 / 资料

全部复用 BrowserSigner 的已登录 Chromium：
- 读接口（评论列表、作品详情）走 www.douyin.com 页面内 fetch，SDK 自动签名，未登录也能读
- 作品列表在创作者中心作品管理页拦截页面自己发出的 work_list 响应，不用自己算签名
- 发布/删除用页面自动化（creator.douyin.com），失败时截图，方便对照页面改版修选择器
- 浏览：推荐流、热榜未登录也能读；搜索要求登录（未登录返回 2483）
- 防风控：页面交互用 human.py 的人类化点击/逐字输入；写操作过 WriteGuard 频率闸门；
  打开页面和关键操作后检查验证码，出现就熔断（与签名请求共用 BrowserSigner 的风控状态）
"""

import asyncio
import json
import os
import re
import time

import aiohttp

from astrbot.api import logger

from ...utils import client_identity
from ...utils.paths import DOWNLOAD_DIR_DY, IMAGE_DIR
from .base import Comment, CreatorPlatform, OpResult, Work
from .human import GUARD, human_click, human_fill, human_hover, human_type, pause

CREATOR = "https://creator.douyin.com"
SELF_URL = "https://www.douyin.com/user/self"
# 浏览器内直接请求创作中心接口时停靠的页面：创作中心前端自己最先请求的接口，返回 JSON，不渲染
CREATOR_LANDING = "/aweme/v1/creator/user/info/"
UPLOAD_URL = f"{CREATOR}/creator-micro/content/upload"
MANAGE_URL = f"{CREATOR}/creator-micro/content/manage"
# 设备参数（系统/浏览器/屏幕等）由 fetch_signed_api 按浏览器真实环境填充
WEB_PARAMS = {
    "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
    "pc_client_type": "1", "version_code": "170400", "version_name": "17.4.0",
    "platform": "PC",
}


# 写接口返回这些码说明账号被风控/要求验证，熔断该账号的写操作
WRITE_RISK_CODES = {-1, 8, 2154, 2155, 3057, 2496}


class CaptchaError(RuntimeError):
    pass


def _aweme_to_work(a: dict) -> Work:
    st = a.get("statistics") or {}
    aid = str(a.get("aweme_id") or "")
    return Work(
        id=aid, title=(a.get("desc") or a.get("item_title") or "(无标题)").strip(),
        create_time=int(a.get("create_time") or 0), digg=int(st.get("digg_count") or 0),
        comments=int(st.get("comment_count") or 0), views=int(st.get("play_count") or 0),
        author=(a.get("author") or {}).get("nickname", ""),
        duration=int((a.get("video") or {}).get("duration") or a.get("duration") or 0) // 1000,
        url=f"https://www.douyin.com/video/{aid}",
    )


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

    async def _screenshot(self, page, tag: str, full: bool = False) -> str:
        try:
            os.makedirs(IMAGE_DIR, exist_ok=True)
            path = os.path.join(IMAGE_DIR, f"creator_{tag}_{int(time.time())}.png")
            await page.screenshot(path=path, full_page=full)
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

    def _account_key(self) -> str:
        """抖音只能用当前账号；频率闸门与风控熔断按当前账号名分别计数。"""
        return self.plugin.accounts.active_name(self.name) or "_active"

    async def _write_gate(self, op: str) -> str:
        """写操作前置检查：账号风控熔断 + 账号级频率闸门，返回拒绝原因（空串=放行）。"""
        if blocked := self.plugin.accounts.write_blocked(self.name):
            return f"已拦截：{blocked}"
        if refused := await GUARD.acquire(self.name, op, self._account_key()):
            return f"已拦截：{refused}（防风控频率限制）"
        return ""

    async def _write_failed(self, data: dict | None, what: str) -> OpResult:
        """统一处理写接口失败：风控类错误熔断该账号的写操作。"""
        if not data:
            msg = "接口无响应（可能触发风控）"
        elif data.get("status_code") == -1 and "空响应" in str(data.get("status_msg")):
            # 新设备上的写操作，抖音会先弹“短信验证码 / 原设备扫码”验证本人，接口直接返回空
            msg = "抖音没有返回结果，通常是要求验证本人（短信验证码或原设备扫码），该账号写操作已暂停；请管理员在抖音 App 操作或完成一次设备验证"
        elif data.get("status_code") == 8:
            msg = "未登录，先 /dy_login 扫码"
        else:
            msg = data.get("status_msg") or f"status_code={data.get('status_code')}"
        code = (data or {}).get("status_code")
        if data is None or code in WRITE_RISK_CODES and code != 8:
            await self.plugin.accounts.trip_risk(self.name, None, f"{what}失败: {msg}")
        return OpResult(False, f"{what}失败: {msg}", data or {})

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
        if await self._creator_http([("/aweme/v1/creator/user/info/", "GET")]) is not None:
            return True, "创作者中心可用"
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
        got = await self._creator_http([("/janus/douyin/creator/pc/work_list?status=0&count=12&max_cursor=0", "GET")])
        data = got[0] if got else await self._works_via_page()
        works = []
        for a in (data.get("aweme_list") or [])[:limit]:
            st = a.get("statistics") or {}
            works.append(Work(
                id=str(a.get("aweme_id")), title=a.get("desc") or a.get("item_title") or "(无标题)",
                create_time=int(a.get("create_time") or 0), digg=st.get("digg_count", 0),
                comments=st.get("comment_count", 0), views=int(st.get("play_count") or 0),
                url=f"https://www.douyin.com/video/{a.get('aweme_id')}",
            ))
        return works

    async def _works_via_page(self) -> dict:
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
        return data

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
            "duration": int(info.get("duration") or 0),
        }

    async def fetch_video(self, work_id: str, ctx: dict, max_seconds: int = 0) -> str | None:
        if not ctx.get("media_url"):
            return None
        from ..douyin_service import download

        os.makedirs(DOWNLOAD_DIR_DY, exist_ok=True)
        path = os.path.join(DOWNLOAD_DIR_DY, f"{work_id}.mp4")
        if not os.path.exists(path):
            await download(ctx["media_url"], path, self.plugin.effective_douyin_cookie)
        return path if os.path.exists(path) else None

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
                    handle=str(user.get("unique_id") or user.get("short_id") or ""),
                ))
            if not data.get("has_more"):
                break
            cursor = data.get("cursor", cursor + 20)
        return out

    # ── 写（仅供二次确认后调用） ──────────────────────────

    async def _publish_comment(self, work_id: str, text: str, reply_id: str, op: str) -> OpResult:
        if refused := await self._write_gate(op):
            return OpResult(False, refused)
        if not self.signer.is_logged_in:
            return OpResult(False, "抖音未登录，先 /dy_login 扫码")
        await pause(1.5, 4.0)  # 模拟打字/思考时间
        form = {
            "aweme_id": work_id, "text": text, "reply_id": reply_id, "reply_to_reply_id": "0",
            "text_extra": "[]", "paste_edit_method": "non_paste", "comment_source": "0",
        }
        if not reply_id:
            form.pop("reply_id")
            form.pop("reply_to_reply_id")
        data = await self.signer.post_form("aweme/v1/web/comment/publish", WEB_PARAMS, form)
        if data and data.get("status_code") == 0:
            cid = str((data.get("comment") or {}).get("cid") or "")
            return OpResult(True, ("已回复" if reply_id else "已发表评论") + (f"（cid={cid}）" if cid else ""), {"cid": cid})
        return await self._write_failed(data, "回复" if reply_id else "发评论")

    async def reply_comment(self, work_id: str, comment_id: str, text: str) -> OpResult:
        return await self._publish_comment(work_id, text, comment_id, "reply")

    async def post_comment(self, work_id: str, text: str) -> OpResult:
        return await self._publish_comment(work_id, text, "", "comment")

    async def delete_comment(self, work_id: str, cid: str) -> OpResult:
        """删除自己发的评论（测试清理用）。"""
        data = await self.signer.post_form("aweme/v1/web/comment/delete/", WEB_PARAMS, {"cid": cid, "aweme_id": work_id})
        ok = bool(data) and data.get("status_code") == 0
        return OpResult(ok, "已删除" if ok else f"删除失败: {(data or {}).get('status_msg') or data}", data or {})

    async def upload(self, video_path: str, title: str, tags: list[str], **opts) -> OpResult:
        if refused := await self._write_gate("upload"):
            return OpResult(False, refused)
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
            await self._dismiss_tips(page)
            await self._apply_publish_opts(page, opts)

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

    # ── 图文 ─────────────────────────────────────────────

    max_images = 35
    _VISIBILITY = {"private": "仅自己可见", "friends": "好友可见"}

    async def _dismiss_tips(self, page) -> None:
        """首次进入发布页会弹“发文助手”引导，点掉。"""
        btn = page.get_by_role("button", name="完成", exact=True)
        if await btn.count():
            try:
                await human_click(page, btn.first)
                await pause(0.5, 1.2)
            except Exception:
                pass

    async def _apply_publish_opts(self, page, opts: dict) -> None:
        """自主声明（内容由 AI 生成）与可见范围，视频和图文发布页通用。"""
        if opts.get("ai_generated"):
            entry = page.get_by_text("请选择自主声明")
            if await entry.count():
                await human_click(page, entry.first)
                await pause(0.8, 1.5)
                await human_click(page, page.get_by_text("内容由AI生成", exact=True).first)
                await pause(0.4, 0.9)
                await human_click(page, page.get_by_role("button", name="确定", exact=True).first)
                await pause(0.6, 1.2)
        label = self._VISIBILITY.get(opts.get("visibility") or "")
        if label:
            await human_click(page, page.get_by_text(label, exact=True).first)
            await pause(0.4, 0.9)

    async def post_images(self, images: list[str], title: str, text: str, tags: list[str], **opts) -> OpResult:
        """创作中心“发布图文”：选图 → 标题/描述/话题 → 声明与可见范围 → 等图片传完 → 发布（dry_run=True 不点发布）。"""
        if not opts.get("dry_run") and (refused := await self._write_gate("upload")):
            return OpResult(False, refused)
        images = [p for p in images if os.path.isfile(p)][:self.max_images]
        if not images:
            return OpResult(False, "没有可发布的图片")
        try:
            page = await self._creator_page(f"{UPLOAD_URL}?default-tab=3")
        except CaptchaError as e:
            return OpResult(False, str(e))
        try:
            if await self._is_login_page(page):
                return OpResult(False, "创作者中心未登录，先 /dy_login 扫码", screenshot=await self._screenshot(page, "note_login"))
            picker = page.locator("input[type=file][accept*='image']")
            if not await picker.count():
                await human_click(page, page.get_by_text("发布图文", exact=True).first)
                await pause()
            await picker.first.set_input_files(images)
            await page.wait_for_url(re.compile(r".*/content/post/image.*"), timeout=60000)

            await pause(1.5, 3.0)
            await self._check_captcha(page, "上传图片")
            await self._dismiss_tips(page)
            title_input = page.locator("input[placeholder*='标题']")
            if title and await title_input.count():
                await human_fill(page, title_input.first, title[:20])
                await pause()
            editor = page.locator(".zone-container, [contenteditable='true']").first
            await human_click(page, editor)
            if text:
                await human_type(page, text[:900])
            for tag in tags:
                await human_type(page, f" #{tag}")
                await pause(0.6, 1.2)
                await page.keyboard.press("Space")
            await self._apply_publish_opts(page, opts)

            # 等所有图片传完：出现“已添加N张图片”且没有“上传中”
            done = page.get_by_text(f"已添加{len(images)}张图片")
            for _ in range(90):
                if await page.get_by_text("上传失败").count():
                    return OpResult(False, "抖音提示图片上传失败", screenshot=await self._screenshot(page, "note_fail"))
                if await done.count() and not await page.get_by_text("上传中").count():
                    break
                await asyncio.sleep(2)
            else:
                return OpResult(False, "等待图片上传完成超时（3 分钟）", screenshot=await self._screenshot(page, "note_timeout"))

            if opts.get("dry_run"):  # 预检：表单填好、图片传完就停，不点发布
                shot = await self._screenshot(page, "note_dryrun", full=True)
                state = {"ai_declared": await page.get_by_text("内容由AI生成").count() > 0,
                         "title": await title_input.first.input_value() if await title_input.count() else ""}
                return OpResult(True, f"预检通过（{len(images)} 张图已上传，表单已填好，未发布）", state, screenshot=shot)
            await pause(2.0, 4.0)
            await human_click(page, page.get_by_role("button", name="发布", exact=True))
            await pause(1.5, 3.0)
            await self._check_captcha(page, "发布图文")
            try:
                await page.wait_for_url(re.compile(r".*/content/manage.*"), timeout=60000)
            except Exception:
                return OpResult(False, "点了发布但没跳转到作品管理页（可能需要验证码/审核弹窗）", screenshot=await self._screenshot(page, "note_publish"))
            vis = f"（{self._VISIBILITY[opts['visibility']]}）" if opts.get("visibility") in self._VISIBILITY else ""
            return OpResult(True, f"已发布图文{vis}，抖音审核通过后可见")
        except CaptchaError as e:
            return OpResult(False, str(e))
        except Exception as e:
            logger.warning(f"[creator] 抖音图文发布失败: {e}")
            return OpResult(False, f"发布图文失败: {e}", screenshot=await self._screenshot(page, "note_err"))
        finally:
            await page.close()

    async def delete(self, work_id: str, title: str = "") -> OpResult:
        if not title:
            works = await self.list_works(limit=50)
            title = next((w.title for w in works if w.id == work_id), "")
            if not title:
                return OpResult(False, f"在最近 50 个作品里没找到 {work_id}")
        if refused := await self._write_gate("delete"):
            return OpResult(False, refused)
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

    # ── 浏览 ─────────────────────────────────────────────

    async def _api(self, endpoint: str, params: dict) -> dict:
        data = await self.signer.fetch_signed_api(endpoint, {**WEB_PARAMS, **params})
        if data is None and await self.signer.refresh():
            data = await self.signer.fetch_signed_api(endpoint, {**WEB_PARAMS, **params})
        if data is None:
            raise RuntimeError(f"抖音接口无响应（{self.plugin.browser_signer.risk_status or '可能触发风控，稍后再试'}）")
        return data

    async def feed(self, kind: str = "rcmd", limit: int = 10) -> list[Work]:
        """推荐流（登录后按账号口味）。热榜是热词而不是视频，见 hot_words()。"""
        out: list[Work] = []
        for page in range(4):  # 每次只给 2~6 条，最多翻 4 页
            data = await self._api("aweme/v1/web/tab/feed/", {
                "count": 10, "refresh_index": page + 1, "video_type_select": 1,
                "aweme_pc_rec_raw_data": json.dumps({"is_client": False}), "globalwid": "",
                "pull_type": 0 if page == 0 else 2, "min_window": 0, "free_right": 0, "ug_source": "", "creative_id": "",
            })
            for a in data.get("aweme_list") or []:
                # 只要普通视频：跳过直播、广告、图文
                if a.get("aweme_type") not in (0, None) or a.get("is_ads") or not a.get("video"):
                    continue
                if not any(w.id == str(a.get("aweme_id")) for w in out):
                    out.append(_aweme_to_work(a))
            if len(out) >= limit or not data.get("has_more", True):
                break
            await pause(0.8, 2.0)
        return out[:limit]

    async def hot_words(self, limit: int = 10) -> list[dict]:
        data = await self._api("aweme/v1/web/hot/search/list/", {"detail_list": 1, "source": 6, "board_type": 0, "board_sub_type": ""})
        words = (data.get("data") or {}).get("word_list") or []
        return [{"word": w.get("word", ""), "hot": int(w.get("hot_value") or 0)} for w in words if w.get("word")][:limit]

    async def search(self, keyword: str, limit: int = 10) -> list[Work]:
        if not self.signer.is_logged_in:
            raise RuntimeError("抖音搜索需要登录：管理员先 /dy_login 扫码（推荐、热榜不需要登录）")
        data = await self._api("aweme/v1/web/search/item/", {
            "search_channel": "aweme_video_web", "keyword": keyword, "search_source": "normal_search",
            "query_correct_type": 1, "is_filter_search": 0, "offset": 0, "count": max(10, limit),
        })
        if data.get("status_code") not in (0, None):
            if data.get("status_code") == 2483:
                raise RuntimeError("抖音搜索要求重新登录（cookie 可能失效），先 /dy_login 扫码")
            raise RuntimeError(f"抖音搜索失败: {data.get('status_msg') or data.get('status_code')}")
        items = [d.get("aweme_info") for d in data.get("data") or [] if isinstance(d, dict) and d.get("aweme_info")]
        return [_aweme_to_work(a) for a in items if a.get("video")][:limit]

    async def hot_comments(self, work_id: str, n: int = 3) -> list[Comment]:
        data = await self._api("aweme/v1/web/comment/list/", {"aweme_id": work_id, "cursor": 0, "count": 20, "item_type": 0})
        out = []
        for c in sorted(data.get("comments") or [], key=lambda c: int(c.get("digg_count") or 0), reverse=True)[:n]:
            user = c.get("user") or {}
            out.append(Comment(id=str(c.get("cid")), text=c.get("text", ""), digg=int(c.get("digg_count") or 0),
                               create_time=int(c.get("create_time") or 0), user=user.get("nickname", ""),
                               user_id=str(user.get("uid", ""))))
        return out

    # ── 账号资料 ─────────────────────────────────────────

    async def profile(self) -> dict:
        if not self.signer.is_logged_in:
            raise RuntimeError("抖音未登录，先 /dy_login 扫码")
        data = await self._api("aweme/v1/web/user/profile/self/", {"publish_video_strategy_type": 2, "source": "channel_pc_web"})
        u = data.get("user") or {}
        if not u:
            raise RuntimeError(f"读取抖音资料失败: {data.get('status_msg') or data.get('status_code')}")
        return {
            "uid": u.get("uid"), "uname": u.get("nickname"), "douyin_id": u.get("unique_id") or u.get("short_id"),
            "sign": u.get("signature", ""), "following": u.get("following_count"), "follower": u.get("follower_count"),
            "likes": u.get("total_favorited"), "works": u.get("aweme_count"),
            "face": ((u.get("avatar_larger") or u.get("avatar_thumb") or {}).get("url_list") or [""])[0],
        }

    # ── 数据看板 / 消息（只读，创作中心页面内 fetch，与页面自己的请求同源同 cookie） ──

    _FETCH_JS = """async (r) => {
        try {
            const resp = await fetch(r.url, {method: r.method, credentials: 'include',
                headers: r.method === 'POST' ? {'content-type': 'application/json'} : {},
                body: r.method === 'POST' ? '{}' : undefined});
            return await resp.json();
        } catch (e) { return null; }
    }"""

    async def _cookie_header(self) -> str:
        """creator.douyin.com 的 cookie：浏览器在跑就取最新的（会滚动续期），否则用 cookie 文件。"""
        s = self.plugin.browser_signer
        if s and s.is_ready:
            try:
                if cs := await s.cookies_for(CREATOR):
                    return cs
            except Exception:
                pass
        from ..douyin_service import clean_cookie

        return clean_cookie(self.plugin.effective_douyin_cookie)

    @staticmethod
    def _creator_ok(data) -> bool:
        return isinstance(data, dict) and data.get("status_code", data.get("code", 0)) in (0, None)

    async def _creator_http(self, reqs: list[tuple[str, str]]) -> list[dict] | None:
        """
        直接请求创作中心读接口（只认 cookie，不需要签名）。登录失效等异常返回 None 交给页面兜底。

        浏览器在跑时由浏览器发（origin_fetch：真实 Chrome 的 TLS/UA/cookie，和用户自己刷新数据页一样）；
        浏览器没起来才用 aiohttp，UA 和 Client Hints 与浏览器保持一致。
        """
        s = self.plugin.browser_signer
        if s and s.risk_status:
            raise RuntimeError(f"抖音风控冷却中：{s.risk_status}")
        if s and s.is_ready:
            try:
                out = []
                for i, (path, method) in enumerate(reqs):
                    if i:
                        await pause(0.2, 0.6)
                    data = await s.origin_fetch(CREATOR, CREATOR_LANDING, CREATOR + path, method, {} if method == "POST" else None)
                    if not self._creator_ok(data):
                        logger.info(f"[douyin] 创作中心接口 {path.split('?')[0]} 返回 {str(data)[:120]}，改用页面读取")
                        return None
                    out.append(data)
                return out
            except Exception as e:
                logger.info(f"[douyin] 浏览器内读取创作中心失败，改用页面读取: {e}")
                return None
        cookie = await self._cookie_header()
        if "sessionid" not in cookie:
            return None
        headers = client_identity.headers(Cookie=cookie, Referer=f"{CREATOR}/creator-micro/home",
                                          Accept="application/json, text/plain, */*")
        out = []
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                for i, (path, method) in enumerate(reqs):
                    if i:
                        await pause(0.2, 0.6)
                    async with session.request(method, CREATOR + path, headers=headers, proxy=self.plugin.douyin_proxy or None,
                                               **({"json": {}} if method == "POST" else {})) as resp:
                        data = await resp.json(content_type=None)
                    if not self._creator_ok(data):
                        logger.info(f"[douyin] 创作中心接口 {path.split('?')[0]} 返回 {str(data)[:120]}，改用页面读取")
                        return None
                    out.append(data)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            logger.info(f"[douyin] 创作中心 HTTP 读取失败，改用页面读取: {e}")
            return None
        return out

    async def _creator_reads(self, reqs: list[tuple[str, str]]) -> list[dict]:
        """读取若干创作中心接口：先直接请求（约 1 秒），失败再打开创作中心首页在页面里读。"""
        if (out := await self._creator_http(reqs)) is not None:
            return out
        if not self.signer.is_logged_in:
            raise RuntimeError("抖音未登录，先 /dy_login 扫码")
        page = await self._creator_page(f"{CREATOR}/creator-micro/home")
        try:
            if await self._is_login_page(page):
                raise RuntimeError("创作者中心未登录或登录已失效，先 /dy_login 扫码")
            out = []
            for path, method in reqs:
                await pause(0.3, 0.9)
                out.append(await page.evaluate(self._FETCH_JS, {"url": CREATOR + path, "method": method}) or {})
            return out
        finally:
            await page.close()

    async def stats(self) -> dict:
        info, overview, diag = await self._creator_reads([
            ("/aweme/v1/creator/user/info/", "GET"),
            ("/aweme/janus/creator/data/overview/all/?last_days_type=1", "GET"),  # 1=近7天 2=近30天
            ("/dp/douyin/v1/creator/item/author_diagnosis", "POST"),
        ])
        if info.get("status_code") not in (0, None) or not info.get("user_profile"):
            raise RuntimeError(f"读取抖音创作者数据失败: {info.get('status_msg') or info.get('status_code')}")
        prof = info.get("user_profile") or {}
        verify = info.get("douyin_user_verify_info") or {}
        ov = overview.get("data") or {}

        def num(key: str) -> int:
            try:
                return int(float((ov.get(key) or {}).get("current_count") or 0))
            except ValueError:
                return 0

        labels = {"play": "播放", "digg": "点赞", "comment": "评论", "share": "分享", "profile": "主页访问",
                  "new_fans": "新增粉丝", "cancel_fans": "取关", "post_search": "作品被搜索"}
        out = {
            "totals": {"fans": prof.get("follower_count") or verify.get("follower_count") or 0,
                       "likes": verify.get("total_favorited") or 0, "following": prof.get("following_count") or 0},
            "period_days": 7,
            "period": {cn: num(k) for k, cn in labels.items() if k in ov},
        }
        names = {"PlayCnt": "播放", "Interact": "互动率", "NewFans": "新增粉丝", "PlayFinishRatio": "完播率", "PublishActivation": "发布数"}
        dd = diag.get("data") or {}
        if diag.get("code") == 0 and dd:
            out["diagnosis"] = {cn: {"own": round((dd.get(k) or {}).get("OwnValue") or 0, 4),
                                     "similar": round((dd.get(k) or {}).get("SimilarValue") or 0, 4)}
                                for k, cn in names.items() if k in dd}
        return out

    async def inbox(self) -> dict:
        unread, notice, msgs = await self._creator_reads([
            ("/aweme/v1/creator/user_message/unread_count/", "GET"),
            ("/aweme/v1/creator/notice/comment/", "GET"),
            ("/aweme/v1/creator/user_message/list/?cursor=0&count=10", "GET"),
        ])
        comments = []
        for c in notice.get("comments") or []:
            cm = c.get("comment") if isinstance(c.get("comment"), dict) else c
            user = cm.get("user") or c.get("user") or {}
            aweme = c.get("aweme") or c.get("item") or {}
            comments.append({
                "id": str(cm.get("cid") or cm.get("comment_id") or c.get("id") or ""), "kind": "评论",
                "user": user.get("nickname", "") if isinstance(user, dict) else "",
                "text": cm.get("text") or cm.get("content") or "", "title": aweme.get("desc") or aweme.get("title") or "",
                "time": int(cm.get("create_time") or 0),
            })
        notices = [{"id": str(m.get("user_message_id") or m.get("task_id")), "title": m.get("title", ""),
                    "content": re.sub(r"<[^>]+>", "", m.get("content", "")), "time": int(m.get("create_time") or 0),
                    "read": bool(m.get("is_read"))}
                   for m in msgs.get("user_message_list") or []]
        counts = {"系统通知": int(unread.get("count") or 0)}
        return {"unread": {k: v for k, v in counts.items() if v}, "comments": [c for c in comments if c["id"]], "notices": notices}

    async def _open_profile_editor(self):
        """打开个人主页的“编辑资料”弹窗（昵称旁的铅笔图标），返回 (page, 保存时捕获的接口响应列表)。"""
        page = await self.signer.new_page()
        updates: list[dict] = []

        async def on_response(r):
            # 只记录资料提交接口（aweme/v1/web/commit/user/）的结果，用来判断是否真的保存成功
            if r.request.method == "POST" and "/aweme/v1/web/commit/user" in r.url:
                try:
                    updates.append({"url": r.url.split("?")[0], "data": await r.json()})
                except Exception:
                    updates.append({"url": r.url.split("?")[0], "status": r.status})

        page.on("response", on_response)
        try:
            await page.goto(SELF_URL, wait_until="domcontentloaded", timeout=30000)
            await pause(2.0, 3.5)
            await self._check_captcha(page, "打开个人主页")
            pencil = page.locator("h1 + span[role=img], h1 ~ .semi-icon").first
            await pencil.wait_for(timeout=15000)
            await human_click(page, pencil)
            await page.get_by_text("编辑资料", exact=True).first.wait_for(timeout=10000)
            await pause()
        except Exception:
            await page.close()
            raise
        return page, updates

    async def _click_save(self, page, updates: list[dict]) -> None:
        """点“保存”并等结果出现（提交接口返回 / 弹出短信验证 / 弹窗关闭），没反应就再点一次。"""
        for attempt in range(2):
            # 编辑资料、修改头像两层弹窗各有一个“保存”，只点当前可见的那个
            save = page.locator("button:visible").filter(has_text=re.compile(r"^\s*保存\s*$")).last
            if attempt:
                await save.click(timeout=5000)
            else:
                await human_click(page, save)
            for _ in range(20):
                await asyncio.sleep(0.5)
                if updates or await page.get_by_text("短信验证码").count() or not await save.is_visible():
                    return
        await pause()

    async def _sms_gate(self, page) -> str:
        """
        抖音网页改资料时常要求“接收短信验证码”验证本人身份。机器人无法也不应代收验证码：
        检测到就点取消退出，返回提示；不点“获取验证码”，不会给用户发短信。
        """
        try:
            # 提交接口先返回（非 JSON），验证弹窗随后才渲染，所以要等一会儿
            await page.get_by_text("短信验证码").first.wait_for(timeout=6000)
        except Exception:
            return ""
        shot = await self._screenshot(page, "profile_sms")
        try:
            await page.get_by_role("button", name="取消", exact=True).last.click(timeout=5000)
        except Exception:
            pass
        return "抖音要求短信验证码确认本人操作，机器人无法代为完成（未发送短信，资料未修改）。请在抖音 App 里修改。" + (f"\n截图: {shot}" if shot else "")

    @staticmethod
    def _update_result(updates: list[dict]) -> tuple[bool | None, str]:
        """从捕获的接口响应判断结果：True 成功 / False 失败（带原因）/ None 没看到接口。"""
        logger.info(f"[creator] 抖音资料修改接口: {[(u['url'], (u.get('data') or {}).get('status_code', u.get('status'))) for u in updates]}")
        for u in reversed(updates):
            d = u.get("data")
            if isinstance(d, dict) and "status_code" in d:
                if d.get("status_code") == 0:
                    return True, ""
                return False, d.get("status_msg") or f"status_code={d.get('status_code')}"
        return None, ""

    async def update_sign(self, text: str) -> OpResult:
        if refused := await self._write_gate("profile"):
            return OpResult(False, refused)
        try:
            page, updates = await self._open_profile_editor()
        except CaptchaError as e:
            return OpResult(False, str(e))
        except Exception as e:
            return OpResult(False, f"打开编辑资料失败（页面可能改版）: {e}")
        try:
            editor = page.locator(".public-DraftEditor-content").first
            await human_click(page, editor)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await pause(0.3, 0.8)
            await human_type(page, text)
            await pause(1.0, 2.0)
            await self._click_save(page, updates)
            await self._check_captcha(page, "保存资料")
            if sms := await self._sms_gate(page):
                return OpResult(False, sms)
            ok, why = self._update_result(updates)
            if ok is False:
                return await self._write_failed({"status_code": -2, "status_msg": why}, "修改签名")
            if ok:
                return OpResult(True, "抖音签名已提交（可能需要审核后对外显示）")
            prof = await self.profile()
            if prof.get("sign", "").strip() == text.strip():
                return OpResult(True, "抖音签名已更新")
            return OpResult(False, "点了保存但抖音没有提交（刚改过资料时会短暂忽略保存，请几分钟后再试）", screenshot=await self._screenshot(page, "sign"))
        except CaptchaError as e:
            return OpResult(False, str(e))
        except Exception as e:
            logger.warning(f"[creator] 抖音改签名失败: {e}")
            return OpResult(False, f"修改签名失败: {e}", screenshot=await self._screenshot(page, "sign_err"))
        finally:
            await page.close()

    async def update_avatar(self, image_path: str) -> OpResult:
        if refused := await self._write_gate("profile"):
            return OpResult(False, refused)
        try:
            page, updates = await self._open_profile_editor()
        except CaptchaError as e:
            return OpResult(False, str(e))
        except Exception as e:
            return OpResult(False, f"打开编辑资料失败（页面可能改版）: {e}")
        try:
            await page.locator("input[type=file][accept*=png]").first.set_input_files(image_path)
            await page.get_by_text("修改头像", exact=True).first.wait_for(timeout=15000)
            await pause(1.5, 3.0)  # 像在看裁剪预览
            await self._click_save(page, updates)
            await self._check_captcha(page, "保存头像")
            if sms := await self._sms_gate(page):
                return OpResult(False, sms)
            ok, why = self._update_result(updates)
            if ok is None and await page.get_by_text("编辑资料", exact=True).count():
                # 有的版本裁剪后回到编辑资料弹窗，还要再点一次保存
                save = page.locator("button:visible").filter(has_text=re.compile(r"^\s*保存\s*$")).last
                if await save.count() and await save.is_enabled():
                    await self._click_save(page, updates)
                    if sms := await self._sms_gate(page):
                        return OpResult(False, sms)
                    ok, why = self._update_result(updates)
            if ok:
                return OpResult(True, "抖音头像已提交（审核通过后展示）")
            if ok is False:
                return await self._write_failed({"status_code": -2, "status_msg": why}, "修改头像")
            return OpResult(False, "点了保存但抖音没有提交（刚改过资料时会短暂忽略保存，请几分钟后再试）", screenshot=await self._screenshot(page, "avatar"))
        except CaptchaError as e:
            return OpResult(False, str(e))
        except Exception as e:
            logger.warning(f"[creator] 抖音换头像失败: {e}")
            return OpResult(False, f"修改头像失败: {e}", screenshot=await self._screenshot(page, "avatar_err"))
        finally:
            await page.close()
