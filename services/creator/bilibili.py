"""
B站创作者操作：我的稿件 / 评论读取 / 回复评论 / 对任意视频发评论 / 投稿 / 删除稿件

全部走 Web 接口 + 扫码登录的 cookie（不需要浏览器）：
- 稿件列表：member.bilibili.com 创作中心接口
- 评论：api.bilibili.com/x/v2/reply（type=1 视频，oid=aid）
- 写操作带 csrf=bili_jct，并过 WriteGuard 频率闸门；-352/-412 等风控码会上报账号池
- 投稿：preupload 拿上传凭证 → upos 分片 PUT → 合并 → 上传封面 → add/v3 提交稿件（封面、分区、标签）
"""

import asyncio
import base64
import json
import os
import random
import re
import time

import aiohttp

from astrbot.api import logger

from ...utils.paths import CREATOR_DIR
from .. import bili_account, bilibili_service
from .base import Comment, CreatorPlatform, OpResult, Work
from .human import GUARD

API = "https://api.bilibili.com"
MEMBER = "https://member.bilibili.com"
BV_RE = re.compile(r"BV1[0-9A-Za-z]{9}")
AV_RE = re.compile(r"(?:^|[^0-9A-Za-z])av(\d+)", re.I)

# 写操作遇到这些码就熔断该账号的写操作
WRITE_RISK_CODES = {-352, -412, 12015, 12051, 12053, 12073}

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


LINK_RE = re.compile(r"https?://|www\.|\.com\b|\.cn\b|b23\.tv", re.I)
SENT_FILE = os.path.join(CREATOR_DIR, "bili_sent.json")
_pace_lock = asyncio.Lock()
_last_call = [0.0]


async def _pace() -> None:
    """所有 B站请求串行并留 0.6~1.6 秒间隔，避免脚本式的密集请求。"""
    async with _pace_lock:
        wait = _last_call[0] + random.uniform(0.6, 1.6) - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call[0] = time.time()


class BilibiliCreator(CreatorPlatform):
    name = "bilibili"
    label = "B站"

    def __init__(self, plugin, account: str | None = None):
        super().__init__(plugin)
        # None/"" = 当前使用中的账号；指定名称则直接用账号池里该账号的 cookie
        self.account = account or None

    @property
    def account_label(self) -> str:
        return self.account or (self.plugin.accounts.active_name("bilibili") if self.plugin else "") or "当前账号"

    async def _cookies(self) -> dict:
        cookies = await self.plugin.accounts.bili_cookies_for(self.account)
        if not cookies or not cookies.get("SESSDATA"):
            raise RuntimeError("B站未登录，先 /bili_login 扫码")
        # 补齐 buvid3/4 和 bili_ticket，有变化就写回，下次直接用
        fixed = await bili_account.ensure_buvid(cookies)
        fixed, _ = await bili_account.ensure_ticket(fixed)
        if fixed != cookies:
            await self.plugin.accounts.save_bili_cookies_for(self.account, fixed)
        return fixed

    async def _get(self, url: str, params: dict | None = None, cookies: dict | None = None, referer: str | None = None) -> dict:
        cookies = cookies if cookies is not None else await self._cookies()
        await _pace()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.get(url, params=params, headers=bili_account.headers(cookies, referer or "https://www.bilibili.com/")) as r:
                return await r.json(content_type=None)

    async def _post(self, url: str, form: dict, referer: str) -> dict:
        cookies = await self._cookies()
        form = {**form, "csrf": cookies.get("bili_jct", "")}
        await _pace()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.post(url, data=form, headers=bili_account.headers(cookies, referer)) as r:
                return await r.json(content_type=None)

    @staticmethod
    def _err(data: dict) -> str:
        code = data.get("code")
        return ERRORS.get(code) or f"{code} {data.get('message', '')}".strip()

    async def _check_risk(self, data: dict, write: bool = False) -> None:
        code = data.get("code")
        if code in bilibili_service.RISK_CODES:
            await bilibili_service._report_failure(f"B站接口返回 {code}")
        # 写操作遇到风控/验证码：该账号的写操作熔断一段时间
        if write and code in WRITE_RISK_CODES:
            await self.plugin.accounts.trip_risk(self.name, self.account, f"{code} {ERRORS.get(code, data.get('message', ''))}")

    def _write_precheck(self, text: str = "") -> str:
        if blocked := self.plugin.accounts.write_blocked(self.name, self.account):
            return blocked
        if text and LINK_RE.search(text):
            return "内容里有链接/网址，B站会判定为广告"
        if text and self._recently_sent(text):
            return "24 小时内已用这个账号发过同样的内容（B站会判重复刷屏）"
        return ""

    def _sent_key(self) -> str:
        return self.account or self.plugin.accounts.active_name(self.name) or "_active"

    def _recently_sent(self, text: str) -> bool:
        try:
            with open(SENT_FILE, encoding="utf-8") as f:
                rec = json.load(f).get(self._sent_key(), [])
        except (OSError, ValueError):
            return False
        return any(r["t"] == text.strip() and time.time() - r["at"] < 86400 for r in rec)

    def _remember_sent(self, text: str) -> None:
        try:
            with open(SENT_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        rec = [r for r in data.get(self._sent_key(), []) if time.time() - r["at"] < 86400][-200:]
        rec.append({"t": text.strip(), "at": time.time()})
        data[self._sent_key()] = rec
        os.makedirs(CREATOR_DIR, exist_ok=True)
        with open(SENT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    @staticmethod
    def _guard_refused(reason: str) -> OpResult:
        return OpResult(False, f"已拦截：{reason}（防风控频率限制）")

    # ── 读 ───────────────────────────────────────────────

    async def _cookies_optional(self) -> dict:
        """浏览类接口：有登录态就带上（推荐更准、能拿字幕），没有也能用。"""
        try:
            return await self._cookies()
        except RuntimeError:
            return {}

    async def check_login(self) -> tuple[bool, str]:
        cookies = await self._cookies_optional()
        info = await bilibili_service.get_login_info(cookies) if cookies else None
        if info and info.get("isLogin"):
            return True, f"「{self.account_label}」已登录: {info.get('uname')}"
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
                digg=int(stat.get("like") or 0), comments=int(stat.get("reply") or 0), views=int(stat.get("view") or 0),
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
        pages = v.get("pages") or []
        return {
            "title": v.get("title", ""),
            "desc": v.get("desc", ""),
            "author": (v.get("owner") or {}).get("name", ""),
            "media_url": f"https://www.bilibili.com/video/{work_id}",
            # 多 P 视频只处理第 1P（下载接口也只取第 1P），时长以它为准
            "duration": int((pages[0].get("duration") if pages else v.get("duration")) or 0),
            "parts": len(pages),
        }

    async def fetch_video(self, work_id: str, ctx: dict, max_seconds: int = 0) -> str | None:
        if max_seconds and ctx.get("duration", 0) > max_seconds:
            # 长视频：ffmpeg 直接拉流只截取开头，不下载整个文件
            url = await bilibili_service.get_video_download_url_by_bvid(work_id, 16)
            if url:
                os.makedirs(bilibili_service.BILI_DOWNLOAD_DIR, exist_ok=True)
                out = os.path.join(bilibili_service.BILI_DOWNLOAD_DIR, f"{work_id}_head{max_seconds}.mp4")
                hdr = f"Referer: https://www.bilibili.com/\r\nUser-Agent: {bili_account.UA}\r\n"
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-loglevel", "error", "-headers", hdr, "-t", str(max_seconds), "-i", url,
                    "-c", "copy", out, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                )
                _, err = await proc.communicate()
                if proc.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 1024:
                    return out
                logger.warning(f"[bili] 截取开头失败，改为完整下载: {err.decode(errors='ignore')[-200:]}")
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
        if reason := self._write_precheck(text):
            return OpResult(False, f"已拦截：{reason}")
        if refused := await GUARD.acquire(self.name, op, self._sent_key()):
            return self._guard_refused(refused)
        await asyncio.sleep(random.uniform(1.5, 4.0))  # 模拟打字/思考时间
        try:
            aid = await self._aid(work_id)
            form = {"type": 1, "oid": aid, "message": text, "plat": 1}
            if root:
                form.update(root=root, parent=root)
            data = await self._post(f"{API}/x/v2/reply/add", form, referer=f"https://www.bilibili.com/video/{work_id}")
        except Exception as e:
            return OpResult(False, f"请求失败: {e}")
        if data.get("code") == 0:
            self._remember_sent(text)
            rpid = str(((data.get("data") or {}).get("rpid")) or "")
            return OpResult(True, f"已用「{self.account_label}」发送" + (f"（rpid={rpid}）" if rpid else ""), {"rpid": rpid})
        await self._check_risk(data, write=True)
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
        cookies = await self._cookies_optional()
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
        cookies = await self._cookies_optional()
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
                               cookies=await self._cookies_optional(), referer=f"https://www.bilibili.com/video/{work_id}")
        out = []
        for c in ((data.get("data") or {}).get("replies") or [])[:n]:
            m = c.get("member") or {}
            out.append(Comment(id=str(c.get("rpid")), text=(c.get("content") or {}).get("message", ""),
                               digg=int(c.get("like") or 0), create_time=int(c.get("ctime") or 0),
                               user=m.get("uname", ""), user_id=str(m.get("mid", ""))))
        return out

    async def report_watch(self, work_id: str, seconds: int) -> OpResult:
        """把这次"观看"写进账号的历史记录和播放心跳（登录时才有意义）。"""
        cookies = await self._cookies_optional()
        if not cookies.get("SESSDATA"):
            return OpResult(False, "未登录，不上报观看记录")
        if refused := await GUARD.acquire(self.name, "watch", self._sent_key()):
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

    # ── 数据看板 / 消息（只读） ─────────────────────────

    async def stats(self) -> dict:
        cookies = await self._cookies()
        ref = f"{MEMBER}/platform/home"
        st = await self._get(f"{MEMBER}/x/web/index/stat", cookies=cookies, referer=ref)
        if st.get("code") != 0:
            await self._check_risk(st)
            raise RuntimeError(f"读取B站数据失败: {self._err(st)}")
        d = st.get("data") or {}
        # pandect type=1：每日新增播放（最近 30 天左右）
        pan = await self._get(f"{MEMBER}/x/web/data/pandect", {"type": 1}, cookies=cookies, referer=ref)
        daily = [int(x.get("total_inc") or 0) for x in (pan.get("data") or [])] if pan.get("code") == 0 else []
        totals = {
            "fans": d.get("total_fans", 0), "plays": d.get("total_click", 0), "likes": d.get("total_like", 0),
            "comments": d.get("total_reply", 0), "coins": d.get("total_coin", 0), "favs": d.get("total_fav", 0),
            "shares": d.get("total_share", 0), "danmaku": d.get("total_dm", 0),
        }
        yesterday = {"播放": d.get("incr_click", 0), "粉丝": d.get("incr_fans", 0), "点赞": d.get("inc_like", 0),
                     "评论": d.get("incr_reply", 0), "投币": d.get("inc_coin", 0), "收藏": d.get("inc_fav", 0)}
        return {"totals": totals, "period_days": 7, "period": {"播放": sum(daily[-7:])} if daily else {},
                "yesterday": {k: v for k, v in yesterday.items() if v}}

    async def inbox(self) -> dict:
        cookies = await self._cookies()
        ref = "https://message.bilibili.com/"
        unread = (await self._get(f"{API}/x/msgfeed/unread", cookies=cookies, referer=ref)).get("data") or {}
        chat = (await self._get("https://api.vc.bilibili.com/session_svr/v1/session_svr/single_unread",
                                {"unread_type": 0, "show_unfollow_list": 1, "build": 0, "mobi_app": "web"},
                                cookies=cookies, referer=ref)).get("data") or {}
        comments = []
        for kind, url in (("回复", f"{API}/x/msgfeed/reply"), ("@", f"{API}/x/msgfeed/at")):
            data = await self._get(url, cookies=cookies, referer=ref)
            for it in ((data.get("data") or {}).get("items") or [])[:10]:
                item = it.get("item") or {}
                comments.append({
                    "id": f"{kind}{it.get('id')}", "kind": kind, "user": (it.get("user") or {}).get("nickname", ""),
                    "text": item.get("source_content", ""), "title": item.get("title", ""), "url": item.get("uri", ""),
                    "time": int(it.get("reply_time") or it.get("at_time") or 0),
                })
        sysmsg = await self._get("https://message.bilibili.com/x/sys-msg/query_user_notify",
                                 {"csrf": cookies.get("bili_jct", ""), "page_size": 10, "build": 0, "mobi_app": "web"},
                                 cookies=cookies, referer=ref)
        notices = [{"id": str(n.get("id")), "title": n.get("title", ""),
                    "content": re.sub(r"#\{([^}]*)\}\{[^}]*\}", r"\1", n.get("content", "")), "time": n.get("time_at", "")}
                   for n in ((sysmsg.get("data") or {}).get("system_notify_list") or [])]
        counts = {"回复": unread.get("reply", 0), "@我": unread.get("at", 0), "点赞": unread.get("like", 0),
                  "系统通知": unread.get("sys_msg", 0),
                  "私信": int(chat.get("follow_unread") or 0) + int(chat.get("unfollow_unread") or 0)}
        return {"unread": {k: v for k, v in counts.items() if v}, "comments": comments, "notices": notices}

    async def update_sign(self, text: str) -> OpResult:
        if reason := self._write_precheck():
            return OpResult(False, f"已拦截：{reason}")
        if refused := await GUARD.acquire(self.name, "profile", self._sent_key()):
            return self._guard_refused(refused)
        data = await self._post(f"{API}/x/member/web/sign/update", {"user_sign": text}, referer="https://account.bilibili.com/")
        if data.get("code") == 0:
            return OpResult(True, f"「{self.account_label}」签名已更新（可能需要审核后才对外显示）")
        await self._check_risk(data, write=True)
        return OpResult(False, self._err(data), data)

    async def update_avatar(self, image_path: str) -> OpResult:
        if reason := self._write_precheck():
            return OpResult(False, f"已拦截：{reason}")
        if refused := await GUARD.acquire(self.name, "profile", self._sent_key()):
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
            return OpResult(True, f"「{self.account_label}」头像已提交（B站审核通过后生效）")
        await self._check_risk(data, write=True)
        return OpResult(False, self._err(data), data)

    # ── 投稿 / 删除稿件 ─────────────────────────────────

    async def _upos_upload(self, video_path: str, cookies: dict) -> tuple[str, int]:
        """分片上传视频，返回 (filename, biz_id)，供 add/v3 引用。"""
        name = os.path.basename(video_path)
        size = os.path.getsize(video_path)
        headers = bili_account.headers(cookies, f"{MEMBER}/platform/upload/video/frame")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.get(f"{MEMBER}/preupload", params={
                "name": name, "r": "upos", "profile": "ugcfx/bup", "ssl": 0, "version": "2.14.0.0",
                "build": 2140000, "size": size, "upcdn": "bda2", "probe_version": 20221109,
            }, headers=headers) as r:
                pre = await r.json(content_type=None)
        if pre.get("OK") != 1:
            raise RuntimeError(f"申请上传失败: {pre.get('message') or pre}")
        url = f"https:{pre['endpoint']}/{pre['upos_uri'].removeprefix('upos://')}"
        up_headers = {"X-Upos-Auth": pre["auth"], "User-Agent": bili_account.UA, "Referer": "https://member.bilibili.com/"}
        chunk_size = int(pre.get("chunk_size") or 10 * 1024 * 1024)
        chunks = max(1, -(-size // chunk_size))
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as s:
            init_q = f"uploads&output=json&profile=ugcfx%2Fbup&filesize={size}&partsize={chunk_size}&biz_id={pre['biz_id']}"
            async with s.post(f"{url}?{init_q}", headers=up_headers) as r:
                init = await r.json(content_type=None)
            upload_id = init.get("upload_id")
            if not upload_id:
                raise RuntimeError(f"初始化分片上传失败: {init}")
            with open(video_path, "rb") as f:
                for i in range(chunks):
                    start = i * chunk_size
                    data = f.read(chunk_size)
                    params = {"partNumber": i + 1, "uploadId": upload_id, "chunk": i, "chunks": chunks,
                              "size": len(data), "start": start, "end": start + len(data), "total": size}
                    for attempt in range(3):
                        try:
                            async with s.put(url, params=params, data=data, headers=up_headers) as r:
                                if r.status == 200:
                                    break
                                err = f"HTTP {r.status} {(await r.text())[:100]}"
                        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                            err = str(e) or type(e).__name__
                        await asyncio.sleep(2 * (attempt + 1))
                    else:
                        raise RuntimeError(f"分片 {i + 1}/{chunks} 上传失败: {err}")
                    logger.info(f"[bili] 投稿分片 {i + 1}/{chunks} 已上传")
            parts = {"parts": [{"partNumber": i + 1, "eTag": "etag"} for i in range(chunks)]}
            async with s.post(url, params={"output": "json", "name": name, "profile": "ugcfx/bup",
                                           "uploadId": upload_id, "biz_id": pre["biz_id"]},
                              json=parts, headers=up_headers) as r:
                done = await r.json(content_type=None)
        if done.get("OK") != 1:
            raise RuntimeError(f"合并分片失败: {done}")
        filename = os.path.splitext(os.path.basename(pre["upos_uri"]))[0]
        return filename, int(pre["biz_id"])

    @staticmethod
    async def _make_cover(video_path: str) -> str:
        """没给封面就从第 1 秒截一帧，补成 16:10 的 1280x800。"""
        out = os.path.splitext(video_path)[0] + "_cover.jpg"
        vf = "scale=1280:800:force_original_aspect_ratio=decrease,pad=1280:800:(ow-iw)/2:(oh-ih)/2"
        for ss in ("1", "0"):
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-loglevel", "error", "-ss", ss, "-i", video_path, "-frames:v", "1", "-vf", vf, "-q:v", "3", out,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 1024:
                return out
        raise RuntimeError("无法从视频截取封面")

    async def _upload_cover(self, path: str) -> str:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
        data = await self._post(f"{MEMBER}/x/vu/web/cover/up", {"cover": f"data:{mime};base64,{b64}"},
                                referer=f"{MEMBER}/platform/upload/video/frame")
        if data.get("code") != 0:
            await self._check_risk(data, write=True)
            raise RuntimeError(f"上传封面失败: {self._err(data)}")
        return data["data"]["url"]

    async def upload(self, video_path: str, title: str, tags: list[str], **opts) -> OpResult:
        """
        投稿（只能由审批流程调用）。opts：cover=封面图路径，tid=分区 ID，desc=简介，
        copyright=1 自制 / 2 转载（转载需 source）。
        """
        if reason := self._write_precheck():
            return OpResult(False, f"已拦截：{reason}")
        title = (title or "").strip()[:80]
        tags = [t.strip().lstrip("#")[:20] for t in tags if t.strip().lstrip("#")][:10]
        if not title:
            return OpResult(False, "缺少标题")
        if not tags:
            return OpResult(False, "B站投稿至少需要 1 个标签（#话题）")
        if refused := await GUARD.acquire(self.name, "upload", self._sent_key()):
            return self._guard_refused(refused)
        cover_tmp = ""
        try:
            cookies = await self._cookies()
            filename, biz_id = await self._upos_upload(video_path, cookies)
            cover_path = opts.get("cover") or ""
            if not cover_path or not os.path.isfile(cover_path):
                cover_path = cover_tmp = await self._make_cover(video_path)
            cover_url = await self._upload_cover(cover_path)
            copyright_ = 2 if int(opts.get("copyright") or 1) == 2 else 1
            body = {
                "cover": cover_url, "title": title, "copyright": copyright_,
                "tid": int(opts.get("tid") or self.plugin.config.get("bili_upload_tid", 21) or 21),
                "tag": ",".join(tags), "desc_format_id": 9999, "desc": (opts.get("desc") or "")[:2000],
                "recreate": -1, "dynamic": "", "interactive": 0,
                "videos": [{"filename": filename, "title": "", "desc": "", "cid": biz_id}],
                "act_reserve_create": 0, "no_disturbance": 0, "no_reprint": 1,
                "subtitle": {"open": 0, "lan": ""}, "dolby": 0, "lossless_music": 0,
                "up_selection_reply": False, "up_close_reply": False, "up_close_danmu": False, "web_os": 3,
            }
            if copyright_ == 2:
                body["source"] = (opts.get("source") or "")[:200]
            await _pace()
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
                async with s.post(f"{MEMBER}/x/vu/web/add/v3", params={"ts": int(time.time() * 1000), "csrf": cookies.get("bili_jct", "")},
                                  json=body, headers=bili_account.headers(cookies, f"{MEMBER}/platform/upload/video/frame")) as r:
                    data = await r.json(content_type=None)
        except Exception as e:
            logger.warning(f"[bili] 投稿失败: {e}")
            return OpResult(False, f"投稿失败: {e}")
        finally:
            if cover_tmp and os.path.exists(cover_tmp):
                os.remove(cover_tmp)
        if data.get("code") == 0:
            d = data.get("data") or {}
            return OpResult(True, f"已用「{self.account_label}」投稿 {d.get('bvid', '')}，B站审核通过后公开", {"bvid": d.get("bvid"), "aid": d.get("aid")})
        await self._check_risk(data, write=True)
        return OpResult(False, f"提交稿件失败: {self._err(data)}", data)

    async def delete(self, work_id: str, title: str = "") -> OpResult:
        if reason := self._write_precheck():
            return OpResult(False, f"已拦截：{reason}")
        try:
            aid = await self._aid(work_id)
        except RuntimeError:
            # 审核中/未通过的稿件公开接口查不到，到自己的稿件列表里找
            works = await self._get(f"{MEMBER}/x/web/archives", {"status": "is_pubing,pubed,not_pubed", "pn": 1, "ps": 50},
                                    referer=f"{MEMBER}/platform/upload-manager/article")
            arcs = [(i.get("Archive") or {}) for i in ((works.get("data") or {}).get("arc_audits") or [])]
            aid = next((a.get("aid") for a in arcs if a.get("bvid") == work_id), None)
            if not aid:
                return OpResult(False, f"在自己的稿件里没找到 {work_id}")
        if refused := await GUARD.acquire(self.name, "delete", self._sent_key()):
            return self._guard_refused(refused)
        data = await self._post(f"{MEMBER}/x/web/archive/delete", {"aid": aid}, referer=f"{MEMBER}/platform/upload-manager/article")
        if data.get("code") == 0:
            return OpResult(True, f"已删除稿件 {work_id}")
        if data.get("code") == 340022:
            # Web 端删稿要先给绑定手机发短信验证码，机器人不代收验证码，先改成仅自己可见
            hidden = await self.set_only_self(aid)
            if hidden.ok:
                return OpResult(True, f"B站删稿需要短信验证码，已先把 {work_id} 改为仅自己可见；要彻底删除请到 B站 App 或创作中心操作",
                                {"hidden": True, "aid": aid})
            return OpResult(False, f"B站删除稿件需要短信验证码，改为仅自己可见也失败了（{hidden.message}）；请在 B站 App 或创作中心手动处理 {work_id}", data)
        await self._check_risk(data, write=True)
        return OpResult(False, f"删除失败: {self._err(data)}", data)

    async def set_only_self(self, aid: int) -> OpResult:
        """把稿件改为仅自己可见：取回创作中心里的稿件信息，原样提交编辑并打开 is_only_self。"""
        view = await self._get(f"{MEMBER}/x/vupre/web/archive/view", {"topic_grey": 1, "aid": aid},
                               referer=f"{MEMBER}/platform/upload-manager/article")
        d = view.get("data") or {}
        a = d.get("archive") or {}
        if view.get("code") != 0 or not a:
            return OpResult(False, f"读取稿件失败: {self._err(view)}", view)
        if a.get("is_only_self") == 1:
            return OpResult(True, "已是仅自己可见")
        body = {
            "aid": aid, "copyright": a.get("copyright", 1), "tid": a.get("tid"), "title": a.get("title"),
            "cover": a.get("cover"), "tag": a.get("tag", ""), "desc": a.get("desc", ""),
            "desc_format_id": a.get("desc_format_id", 9999), "source": a.get("source", ""),
            "no_reprint": a.get("no_reprint", 1), "dynamic": a.get("dynamic", ""),
            "interactive": a.get("interactive", 0), "act_reserve_create": int(d.get("act_reserve_create") or 0),
            "no_disturbance": int(d.get("no_disturbance") or 0), "is_only_self": 1, "web_os": 3, "recreate": -1, "dtime": 0,
            # 缺 subtitle 会报 21001 参数错误；评论区/弹幕等开关不传，保持原样
            "subtitle": {"open": 1 if (d.get("subtitle") or {}).get("allow") else 0, "lan": (d.get("subtitle") or {}).get("lan", "")},
            "videos": [{"filename": v.get("filename"), "title": v.get("title", ""), "desc": v.get("desc", ""), "cid": v.get("cid")}
                       for v in (d.get("videos") or [])],
        }
        cookies = await self._cookies()
        await _pace()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
            async with s.post(f"{MEMBER}/x/vu/web/edit", params={"t": int(time.time() * 1000), "csrf": cookies.get("bili_jct", "")},
                              json=body, headers=bili_account.headers(cookies, f"{MEMBER}/platform/upload/video/frame")) as r:
                data = await r.json(content_type=None)
        if data.get("code") == 0:
            return OpResult(True, "已改为仅自己可见")
        await self._check_risk(data, write=True)
        return OpResult(False, self._err(data), data)
