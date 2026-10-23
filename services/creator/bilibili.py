"""
B站创作者操作：我的稿件 / 评论读取 / 回复评论 / 对任意视频发评论

全部走 Web 接口 + 扫码登录的 cookie（不需要浏览器）：
- 稿件列表：member.bilibili.com 创作中心接口
- 评论：api.bilibili.com/x/v2/reply（type=1 视频，oid=aid）
- 写操作带 csrf=bili_jct，并过 WriteGuard 频率闸门；-352/-412 等风控码会上报账号池
发布/删除稿件暂不支持（B站投稿需分片上传，风险和复杂度都高）。
"""

import os
import re
import time

import aiohttp

from astrbot.api import logger

from .. import bili_account, bilibili_service
from .base import Comment, CreatorPlatform, OpResult, Work
from .human import GUARD

API = "https://api.bilibili.com"
MEMBER = "https://member.bilibili.com"
BV_RE = re.compile(r"BV1[0-9A-Za-z]{9}")
AV_RE = re.compile(r"(?:^|[^0-9A-Za-z])av(\d+)", re.I)

# 常见错误码 → 说明
ERRORS = {
    -101: "账号未登录或 cookie 已失效，先 /bili_login",
    -111: "csrf 校验失败（bili_jct 不匹配），请重新 /bili_login",
    -352: "触发风控校验，稍后再试",
    -412: "请求被拦截（风控），稍后再试",
    12002: "评论区已关闭",
    12015: "需要输入验证码（风控），稍后再试",
    12016: "评论内容包含敏感信息",
    12025: "评论字数过多",
    12035: "该账号被 UP 主拉黑",
    12051: "重复评论，请勿刷屏",
}


class BilibiliCreator(CreatorPlatform):
    name = "bilibili"
    label = "B站"

    async def _cookies(self) -> dict:
        cookies = await bilibili_service.load_cookies()
        if not cookies or not cookies.get("SESSDATA"):
            raise RuntimeError("B站未登录，先 /bili_login 扫码")
        return cookies

    async def _get(self, url: str, params: dict | None = None, cookies: dict | None = None, referer: str | None = None) -> dict:
        cookies = cookies if cookies is not None else await self._cookies()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(url, params=params, headers=bili_account.headers(cookies, referer or "https://www.bilibili.com/")) as r:
                return await r.json(content_type=None)

    async def _post(self, url: str, form: dict, referer: str) -> dict:
        cookies = await self._cookies()
        form = {**form, "csrf": cookies.get("bili_jct", "")}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.post(url, data=form, headers=bili_account.headers(cookies, referer)) as r:
                return await r.json(content_type=None)

    @staticmethod
    def _err(data: dict) -> str:
        code = data.get("code")
        return ERRORS.get(code) or f"{code} {data.get('message', '')}".strip()

    async def _check_risk(self, data: dict) -> None:
        if data.get("code") in bilibili_service.RISK_CODES:
            await bilibili_service._report_failure(f"B站接口返回 {data.get('code')}")

    @staticmethod
    def _guard_refused(reason: str) -> OpResult:
        return OpResult(False, f"已拦截：{reason}（防风控频率限制）")

    # ── 读 ───────────────────────────────────────────────

    async def check_login(self) -> tuple[bool, str]:
        info = await bilibili_service.get_login_info()
        if info and info.get("isLogin"):
            return True, f"已登录: {info.get('uname')}"
        return False, "未登录或 cookie 已失效"

    async def list_works(self, limit: int = 10) -> list[Work]:
        data = await self._get(
            f"{MEMBER}/x/web/archives",
            {"status": "is_pubing,pubed,not_pubed", "pn": 1, "ps": max(1, min(limit, 50)), "coop": 1, "interactive": 1},
            referer=f"{MEMBER}/platform/upload-manager/article",
        )
        if data.get("code") != 0:
            await self._check_risk(data)
            raise RuntimeError(f"读取稿件列表失败: {self._err(data)}")
        works = []
        for item in (data.get("data") or {}).get("arc_audits") or []:
            arc = item.get("Archive") or {}
            stat = item.get("stat") or {}
            bvid = arc.get("bvid", "")
            works.append(Work(
                id=bvid, title=arc.get("title") or "(无标题)", create_time=int(arc.get("ptime") or arc.get("ctime") or 0),
                digg=int(stat.get("like") or 0), comments=int(stat.get("reply") or 0),
                url=f"https://www.bilibili.com/video/{bvid}",
            ))
        return works[:limit]

    async def resolve_work(self, ref: str) -> str | None:
        """统一解析成 BV 号（支持 BV 号、av 号、视频链接、b23 短链）。"""
        ref = ref.strip()
        if m := BV_RE.search(ref):
            return m.group(0)
        if m := AV_RE.search(ref):
            info = await bilibili_service.parse_video(aid=m.group(1))
            return info["bvid"] if info else None
        if m := bilibili_service.REG_B23.search(ref):
            info = await bilibili_service.parse_b23(m.group(0))
            return info["bvid"] if info else None
        return None

    async def _view(self, bvid: str) -> dict:
        data = await self._get(f"{API}/x/web-interface/view", {"bvid": bvid}, cookies={})
        if data.get("code") != 0:
            raise RuntimeError(f"读取视频 {bvid} 失败: {self._err(data)}")
        return data["data"]

    async def _aid(self, bvid: str) -> int:
        return int((await self._view(bvid))["aid"])

    async def work_context(self, work_id: str) -> dict:
        v = await self._view(work_id)
        return {
            "title": v.get("title", ""),
            "desc": v.get("desc", ""),
            "author": (v.get("owner") or {}).get("name", ""),
            "media_url": f"https://www.bilibili.com/video/{work_id}",
        }

    async def fetch_video(self, work_id: str, ctx: dict) -> str | None:
        res = await bilibili_service.process_bili_video(work_id, download_flag=True, quality=16, use_login=False)
        return (res or {}).get("video_path")

    async def list_comments(self, work_id: str, max_count: int = 60) -> list[Comment]:
        aid = await self._aid(work_id)
        out: list[Comment] = []
        pn = 1
        while len(out) < max_count:
            # sort=0 按时间，新评论在前，便于挑 48h 内的
            data = await self._get(f"{API}/x/v2/reply", {"type": 1, "oid": aid, "pn": pn, "ps": 20, "sort": 0},
                                   referer=f"https://www.bilibili.com/video/{work_id}")
            if data.get("code") != 0:
                await self._check_risk(data)
                if not out:
                    raise RuntimeError(f"读取评论失败: {self._err(data)}")
                break
            replies = (data.get("data") or {}).get("replies") or []
            for c in replies:
                m = c.get("member") or {}
                out.append(Comment(
                    id=str(c.get("rpid")), text=(c.get("content") or {}).get("message", ""),
                    digg=int(c.get("like") or 0), create_time=int(c.get("ctime") or 0),
                    user=m.get("uname", ""), user_id=str(m.get("mid", "")), handle=str(m.get("mid", "")),
                ))
            if len(replies) < 20:
                break
            pn += 1
        return out[:max_count]

    # ── 写（仅供二次确认后调用） ──────────────────────────

    async def _add_reply(self, work_id: str, text: str, root: str = "", op: str = "reply") -> OpResult:
        if refused := await GUARD.acquire(self.name, op):
            return self._guard_refused(refused)
        try:
            aid = await self._aid(work_id)
            form = {"type": 1, "oid": aid, "message": text, "plat": 1}
            if root:
                form.update(root=root, parent=root)
            data = await self._post(f"{API}/x/v2/reply/add", form, referer=f"https://www.bilibili.com/video/{work_id}")
        except Exception as e:
            return OpResult(False, f"请求失败: {e}")
        if data.get("code") == 0:
            rpid = str(((data.get("data") or {}).get("rpid")) or "")
            return OpResult(True, "已发送" + (f"（rpid={rpid}）" if rpid else ""), {"rpid": rpid})
        await self._check_risk(data)
        logger.warning(f"[bili] 发表评论失败: {data.get('code')} {data.get('message')}")
        return OpResult(False, self._err(data), data)

    async def reply_comment(self, work_id: str, comment_id: str, text: str) -> OpResult:
        return await self._add_reply(work_id, text, root=comment_id, op="reply")

    async def post_comment(self, work_id: str, text: str) -> OpResult:
        return await self._add_reply(work_id, text, op="comment")

    async def delete_comment(self, work_id: str, rpid: str) -> OpResult:
        """删除自己发的评论/回复（或自己稿件下的评论）。"""
        try:
            aid = await self._aid(work_id)
            data = await self._post(f"{API}/x/v2/reply/del", {"type": 1, "oid": aid, "rpid": rpid},
                                    referer=f"https://www.bilibili.com/video/{work_id}")
        except Exception as e:
            return OpResult(False, f"请求失败: {e}")
        return OpResult(data.get("code") == 0, "已删除" if data.get("code") == 0 else self._err(data), data)

    # ── 浏览 ─────────────────────────────────────────────

    @staticmethod
    def _work_from_card(bvid: str, title: str, owner: str, stat: dict, duration=0) -> Work:
        if isinstance(duration, str) and ":" in duration:  # 搜索接口给 "mm:ss"
            parts = [int(x) for x in duration.split(":") if x.isdigit()]
            duration = sum(v * 60 ** i for i, v in enumerate(reversed(parts)))
        return Work(
            id=bvid, title=re.sub(r"<[^>]+>", "", title or "(无标题)"), author=owner,
            views=int(stat.get("view") or stat.get("play") or 0), digg=int(stat.get("like") or 0),
            comments=int(stat.get("reply") or stat.get("review") or 0), duration=int(duration or 0),
            url=f"https://www.bilibili.com/video/{bvid}",
        )

    async def feed(self, kind: str = "rcmd", limit: int = 10) -> list[Work]:
        """首页推荐（登录后按账号口味）或热门。"""
        cookies = await bilibili_service.load_cookies() or {}
        if kind == "hot":
            data = await self._get(f"{API}/x/web-interface/popular", {"pn": 1, "ps": limit}, cookies=cookies)
            items = (data.get("data") or {}).get("list") or []
            return [self._work_from_card(i["bvid"], i.get("title"), (i.get("owner") or {}).get("name", ""), i.get("stat") or {}, i.get("duration")) for i in items][:limit]
        params = await bili_account.wbi_sign({"fresh_type": 4, "ps": limit, "fresh_idx": 1, "fresh_idx_1h": 1, "brush": 1}, cookies)
        data = await self._get(f"{API}/x/web-interface/wbi/index/top/feed/rcmd", params, cookies=cookies)
        if data.get("code") != 0:
            await self._check_risk(data)
            raise RuntimeError(f"读取推荐失败: {self._err(data)}")
        items = [i for i in (data.get("data") or {}).get("item") or [] if i.get("bvid") and i.get("goto") == "av"]
        return [self._work_from_card(i["bvid"], i.get("title"), (i.get("owner") or {}).get("name", ""), i.get("stat") or {}, i.get("duration")) for i in items][:limit]

    async def search(self, keyword: str, limit: int = 10) -> list[Work]:
        cookies = await bilibili_service.load_cookies() or {}
        cookies = await bili_account.ensure_buvid(cookies)  # 搜索接口没有 buvid3 会 -412
        params = await bili_account.wbi_sign({"search_type": "video", "keyword": keyword, "page": 1}, cookies)
        data = await self._get(f"{API}/x/web-interface/wbi/search/type", params, cookies=cookies,
                               referer="https://search.bilibili.com/")
        if data.get("code") != 0:
            await self._check_risk(data)
            raise RuntimeError(f"搜索失败: {self._err(data)}")
        items = (data.get("data") or {}).get("result") or []
        return [self._work_from_card(i.get("bvid", ""), i.get("title"), i.get("author", ""),
                                     {"play": i.get("play"), "like": i.get("like"), "review": i.get("review")}, i.get("duration"))
                for i in items if i.get("bvid")][:limit]

    async def hot_comments(self, work_id: str, n: int = 3) -> list[Comment]:
        aid = await self._aid(work_id)
        data = await self._get(f"{API}/x/v2/reply", {"type": 1, "oid": aid, "pn": 1, "ps": 20, "sort": 1},
                               cookies=await bilibili_service.load_cookies() or {}, referer=f"https://www.bilibili.com/video/{work_id}")
        out = []
        for c in ((data.get("data") or {}).get("replies") or [])[:n]:
            m = c.get("member") or {}
            out.append(Comment(id=str(c.get("rpid")), text=(c.get("content") or {}).get("message", ""),
                               digg=int(c.get("like") or 0), create_time=int(c.get("ctime") or 0),
                               user=m.get("uname", ""), user_id=str(m.get("mid", ""))))
        return out

    async def report_watch(self, work_id: str, seconds: int) -> OpResult:
        """把这次"观看"写进账号的历史记录和播放心跳（登录时才有意义）。"""
        cookies = await bilibili_service.load_cookies()
        if not cookies or not cookies.get("SESSDATA"):
            return OpResult(False, "未登录，不上报观看记录")
        if refused := await GUARD.acquire(self.name, "watch"):
            return self._guard_refused(refused)
        v = await self._view(work_id)
        ref = f"https://www.bilibili.com/video/{work_id}"
        seconds = max(1, min(int(seconds), int(v.get("duration") or seconds)))
        hb = await self._post(f"{API}/x/click-interface/web/heartbeat", {
            "aid": v["aid"], "bvid": work_id, "cid": v["cid"], "mid": cookies.get("DedeUserID", ""),
            "played_time": seconds, "real_played_time": seconds, "realtime": seconds,
            "start_ts": int(time.time()) - seconds, "type": 3, "dt": 2, "play_type": 0,
        }, referer=ref)
        hist = await self._post(f"{API}/x/v2/history/report", {"aid": v["aid"], "cid": v["cid"], "progress": seconds, "platform": "web"}, referer=ref)
        ok = hb.get("code") == 0 and hist.get("code") == 0
        return OpResult(ok, "已记录观看" if ok else f"上报失败: 心跳 {self._err(hb)} / 历史 {self._err(hist)}")

    # ── 账号资料 ─────────────────────────────────────────

    async def profile(self) -> dict:
        cookies = await self._cookies()
        nav = (await self._get(f"{API}/x/web-interface/nav", cookies=cookies)).get("data") or {}
        if not nav.get("isLogin"):
            raise RuntimeError("B站 cookie 已失效，先 /bili_login")
        my = (await self._get(f"{API}/x/space/myinfo", cookies=cookies)).get("data") or {}
        rel = (await self._get(f"{API}/x/relation/stat", {"vmid": nav.get("mid")}, cookies=cookies)).get("data") or {}
        return {
            "mid": nav.get("mid"), "uname": nav.get("uname"), "face": nav.get("face"),
            "sign": my.get("sign", ""), "level": (nav.get("level_info") or {}).get("current_level"),
            "coins": nav.get("money"), "vip": nav.get("vipStatus") == 1,
            "following": rel.get("following"), "follower": rel.get("follower"),
        }

    async def update_sign(self, text: str) -> OpResult:
        if refused := await GUARD.acquire(self.name, "profile"):
            return self._guard_refused(refused)
        data = await self._post(f"{API}/x/member/web/sign/update", {"user_sign": text}, referer="https://account.bilibili.com/")
        if data.get("code") == 0:
            return OpResult(True, "签名已更新（可能需要审核后才对外显示）")
        await self._check_risk(data)
        return OpResult(False, self._err(data), data)

    async def update_avatar(self, image_path: str) -> OpResult:
        if refused := await GUARD.acquire(self.name, "profile"):
            return self._guard_refused(refused)
        cookies = await self._cookies()
        form = aiohttp.FormData()
        form.add_field("dopost", "save")
        form.add_field("DisplayRank", "10000")
        form.add_field("csrf", cookies.get("bili_jct", ""))
        form.add_field("face", open(image_path, "rb"), filename=os.path.basename(image_path), content_type="image/jpeg")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
                async with s.post(f"{API}/x/member/web/face/update", data=form,
                                  headers=bili_account.headers(cookies, "https://account.bilibili.com/")) as r:
                    data = await r.json(content_type=None)
        except Exception as e:
            return OpResult(False, f"上传头像失败: {e}")
        if data.get("code") == 0:
            return OpResult(True, "头像已提交（B站审核通过后生效）")
        await self._check_risk(data)
        return OpResult(False, self._err(data), data)

    # ── 不支持的写操作 ───────────────────────────────────

    async def upload(self, video_path: str, title: str, tags: list[str]) -> OpResult:
        return OpResult(False, "B站投稿暂不支持")

    async def delete(self, work_id: str, title: str = "") -> OpResult:
        return OpResult(False, "B站删除稿件暂不支持")
