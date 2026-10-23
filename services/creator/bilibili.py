"""
B站创作者操作：我的稿件 / 评论读取 / 回复评论 / 对任意视频发评论

全部走 Web 接口 + 扫码登录的 cookie（不需要浏览器）：
- 稿件列表：member.bilibili.com 创作中心接口
- 评论：api.bilibili.com/x/v2/reply（type=1 视频，oid=aid）
- 写操作带 csrf=bili_jct，并过 WriteGuard 频率闸门；-352/-412 等风控码会上报账号池
发布/删除稿件暂不支持（B站投稿需分片上传，风险和复杂度都高）。
"""

import asyncio
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

    # ── 不支持的写操作 ───────────────────────────────────

    async def upload(self, video_path: str, title: str, tags: list[str]) -> OpResult:
        return OpResult(False, "B站投稿暂不支持")

    async def delete(self, work_id: str, title: str = "") -> OpResult:
        return OpResult(False, "B站删除稿件暂不支持")
