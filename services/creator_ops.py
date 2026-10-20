"""
创作者操作的通用流程（与平台无关）

- 危险操作（发布/删除/回复评论）一律先生成 PendingOp，管理员 /va_confirm <码> 后才执行
- 评论精选：48h 内的一级评论按点赞排序，从前 10 条里随机挑 1~N 条，结合视频内容与账号风格由 LLM 起草回复
- 发布素材：管理员在聊天里发视频（或引用视频消息），先落到 UPLOAD_DIR 再等确认
"""

import asyncio
import json
import os
import random
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field

import aiohttp

from astrbot.api import logger

from ..utils.paths import CREATOR_DIR, DOWNLOAD_DIR_DY, UPLOAD_DIR
from .creator import Comment, OpResult, get_creator

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".flv", ".avi"}
TAG_RE = re.compile(r"#([^\s#]+)")


@dataclass
class PendingOp:
    code: str
    kind: str  # upload / delete / reply
    platform: str
    account: str
    requester: str
    umo: str
    expires: float
    payload: dict = field(default_factory=dict)


def split_title_tags(text: str) -> tuple[str, list[str]]:
    tags = TAG_RE.findall(text)
    title = TAG_RE.sub("", text).strip()
    return title, tags


def _ago(ts: int) -> str:
    h = (time.time() - ts) / 3600
    return f"{h * 60:.0f} 分钟前" if h < 1 else f"{h:.0f} 小时前"


class CreatorOps:
    def __init__(self, plugin):
        self.plugin = plugin
        cfg = plugin.config
        self.confirm_timeout = int(cfg.get("confirm_timeout_seconds", 60))
        self.reply_max = max(1, min(5, int(cfg.get("reply_max_comments", 5))))
        self.reply_window_h = int(cfg.get("reply_window_hours", 48))
        self.reply_style = cfg.get("creator_reply_style", "") or "真诚、简短、口语化，像博主本人在和粉丝聊天，不用官腔，不带链接"
        self.reply_provider = cfg.get("creator_reply_provider_id", "")
        self.reply_use_video = cfg.get("creator_reply_video_understanding", True)
        self._pending: dict[str, PendingOp] = {}
        self._upload_waiters: dict[str, dict] = {}
        self.last_works: dict[str, list] = {}  # /va_works 的结果，供按序号引用
        # 待确认操作只在内存里，重载后之前暂存的发布素材都已无主，直接清掉
        shutil.rmtree(UPLOAD_DIR, ignore_errors=True)

    # ── 二次确认 ─────────────────────────────────────────

    def _new_op(self, kind: str, platform: str, event, payload: dict, ttl: int | None = None) -> PendingOp:
        self._gc()
        code = f"{secrets.randbelow(10000):04d}"
        while code in self._pending:
            code = f"{secrets.randbelow(10000):04d}"
        op = PendingOp(
            code=code, kind=kind, platform=platform,
            account=self.plugin.accounts.active_name(platform) or "当前登录账号",
            requester=str(event.get_sender_id()), umo=event.unified_msg_origin,
            expires=time.time() + (ttl or self.confirm_timeout), payload=payload,
        )
        self._pending[code] = op
        return op

    def _gc(self) -> None:
        now = time.time()
        for code in [c for c, op in self._pending.items() if op.expires < now]:
            op = self._pending.pop(code)
            if op.kind == "upload":
                self._remove_staged(op.payload.get("path", ""))

    def pending_list(self) -> list[dict]:
        self._gc()
        return [
            {"code": op.code, "kind": op.kind, "platform": op.platform, "account": op.account,
             "expires_in": int(op.expires - time.time()), "summary": self._summary(op)}
            for op in self._pending.values()
        ]

    def has_pending(self, code: str) -> bool:
        self._gc()
        return code in self._pending

    def cancel(self, code: str) -> str:
        op = self._pending.pop(code, None)
        if not op:
            return f"没有待确认的操作 {code}（可能已过期）"
        if op.kind == "upload":
            self._remove_staged(op.payload.get("path", ""))
        return f"已取消 {code}"

    def edit_reply(self, code: str, index: int, text: str) -> str:
        op = self._pending.get(code)
        if not op or op.kind != "reply":
            return f"没有待确认的评论回复 {code}"
        items = op.payload["items"]
        if not 1 <= index <= len(items):
            return f"序号超出范围 1~{len(items)}"
        items[index - 1]["reply"] = text
        return f"已修改 {code} 第 {index} 条回复，发送 /va_confirm {code} 执行"

    async def confirm(self, code: str, picks: list[int] | None = None) -> str:
        self._gc()
        op = self._pending.pop(code, None)
        if not op:
            return f"没有待确认的操作 {code}（可能已过期，超时 {self.confirm_timeout} 秒）"
        creator = get_creator(self.plugin, op.platform)
        try:
            if op.kind == "upload":
                p = op.payload
                try:
                    res = await creator.upload(p["path"], p["title"], p["tags"])
                finally:
                    self._remove_staged(p["path"])  # 失败也清理，重发视频即可重试
                return self._render_result(f"发布《{p['title']}》", res)
            if op.kind == "delete":
                res = await creator.delete(op.payload["work_id"], op.payload.get("title", ""))
                return self._render_result(f"删除 {op.payload['work_id']}", res)
            if op.kind == "reply":
                return await self._run_replies(creator, op, picks)
        except Exception as e:
            logger.warning(f"[creator] 执行 {op.kind} 失败: {e}")
            return f"执行失败: {e}"
        return f"未知操作类型 {op.kind}"

    @staticmethod
    def _render_result(what: str, res: OpResult) -> str:
        text = f"{'✅' if res.ok else '❌'} {what}: {res.message}"
        if res.screenshot:
            text += f"\n失败截图: {res.screenshot}"
        return text

    def _summary(self, op: PendingOp) -> str:
        p = op.payload
        if op.kind == "upload":
            return f"发布《{p['title']}》 {' '.join('#' + t for t in p['tags'])}（{p['size_mb']:.1f}MB）"
        if op.kind == "delete":
            return f"删除作品 {p['work_id']}" + (f"《{p['title']}》" if p.get("title") else "")
        return f"回复作品 {p['work_id']} 的 {len(p['items'])} 条评论"

    def render_preview(self, op: PendingOp) -> str:
        head = f"⚠️ 待确认 [{op.code}] {op.platform}·{op.account}\n{self._summary(op)}"
        if op.kind == "reply":
            lines = [head, ""]
            for i, it in enumerate(op.payload["items"], 1):
                lines.append(f"[{i}] {it['user']}（👍{it['digg']}，{_ago(it['create_time'])}）: {it['text']}")
                lines.append(f"    ↳ {it['reply']}")
            lines.append(
                f"\n/va_confirm {op.code} 全部发送 · /va_confirm {op.code} 1 3 只发部分\n"
                f"/va_edit {op.code} <序号> <新回复> 修改 · /va_cancel {op.code} 取消（{int(op.expires - time.time())} 秒内有效）"
            )
            return "\n".join(lines)
        return f"{head}\n\n发送 /va_confirm {op.code} 执行，/va_cancel {op.code} 取消（{int(op.expires - time.time())} 秒内有效）"

    # ── 发布 ─────────────────────────────────────────────

    def _remove_staged(self, path: str) -> None:
        if path and os.path.abspath(path).startswith(os.path.abspath(UPLOAD_DIR)) and os.path.exists(path):
            os.remove(path)

    async def stage_video(self, source: str) -> str:
        """把聊天里的视频（本地路径或 URL）复制/下载到 UPLOAD_DIR，返回本地路径。"""
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        dest = os.path.join(UPLOAD_DIR, f"{int(time.time())}_{secrets.token_hex(3)}.mp4")
        if source.startswith(("http://", "https://")):
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as s:
                async with s.get(source) as r:
                    r.raise_for_status()
                    with open(dest, "wb") as f:
                        async for chunk in r.content.iter_chunked(1 << 16):
                            f.write(chunk)
        elif os.path.isfile(source):
            shutil.copyfile(source, dest)
        else:
            raise ValueError(f"找不到视频: {source[:100]}")
        if os.path.getsize(dest) < 1024:
            os.remove(dest)
            raise ValueError("视频文件为空或下载失败")
        return dest

    async def prepare_upload(self, event, platform: str, source: str, title: str, tags: list[str]) -> PendingOp:
        if not title:
            raise ValueError("缺少标题")
        path = await self.stage_video(source)
        size_mb = os.path.getsize(path) / 1048576
        return self._new_op("upload", platform, event, {"path": path, "title": title, "tags": tags, "size_mb": size_mb})

    def wait_for_upload(self, event, platform: str, title: str, tags: list[str]) -> None:
        key = f"{event.unified_msg_origin}|{event.get_sender_id()}"
        self._upload_waiters[key] = {"platform": platform, "title": title, "tags": tags, "expires": time.time() + 120}

    def peek_upload_waiter(self, event, consume: bool = False) -> dict | None:
        key = f"{event.unified_msg_origin}|{event.get_sender_id()}"
        w = self._upload_waiters.get(key)
        if w and w["expires"] < time.time():
            self._upload_waiters.pop(key, None)
            return None
        if w and consume:
            self._upload_waiters.pop(key, None)
        return w

    @staticmethod
    async def extract_video(event) -> str | None:
        """从当前消息或被引用的消息里找视频，返回本地路径或 URL。"""
        from astrbot.core.message.components import File, Reply, Video

        segs = list(getattr(event.message_obj, "message", []) or [])
        for seg in list(segs):
            if isinstance(seg, Reply) and seg.chain:
                segs.extend(seg.chain)
        for seg in segs:
            if isinstance(seg, Video):
                try:
                    return await seg.convert_to_file_path()
                except Exception:
                    return seg.file
            if isinstance(seg, File) and os.path.splitext((seg.name or "").lower())[1] in VIDEO_EXTS:
                try:
                    return await seg.get_file()
                except Exception:
                    return getattr(seg, "url", "") or None
        return None

    def prepare_delete(self, event, platform: str, work_id: str, title: str) -> PendingOp:
        return self._new_op("delete", platform, event, {"work_id": work_id, "title": title})

    # ── 评论精选回复 ─────────────────────────────────────

    def _replied_file(self, platform: str) -> str:
        return os.path.join(CREATOR_DIR, f"replied_{platform}.json")

    def _load_replied(self, platform: str) -> set[str]:
        try:
            with open(self._replied_file(platform), encoding="utf-8") as f:
                return set(json.load(f))
        except (OSError, json.JSONDecodeError):
            return set()

    def _mark_replied(self, platform: str, ids: list[str]) -> None:
        done = self._load_replied(platform) | set(ids)
        os.makedirs(CREATOR_DIR, exist_ok=True)
        with open(self._replied_file(platform), "w", encoding="utf-8") as f:
            json.dump(sorted(done)[-5000:], f)

    def pick_comments(self, platform: str, comments: list[Comment], author: str = "") -> list[Comment]:
        """48h 内、未回复过、非作者本人的评论按点赞取前 10，再随机挑 1~reply_max 条。"""
        since = time.time() - self.reply_window_h * 3600
        replied = self._load_replied(platform)
        fresh = [
            c for c in comments
            if c.create_time >= since and c.id not in replied and c.text.strip() and not (author and c.user == author)
        ]
        top = sorted(fresh, key=lambda c: c.digg, reverse=True)[:10]
        if not top:
            return []
        k = random.randint(1, min(self.reply_max, len(top)))
        return sorted(random.sample(top, k), key=lambda c: c.digg, reverse=True)

    async def _video_summary(self, platform: str, work_id: str, ctx: dict) -> str:
        cache_file = os.path.join(CREATOR_DIR, "video_context.json")
        try:
            with open(cache_file, encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            cache = {}
        key = f"{platform}:{work_id}"
        if key in cache:
            return cache[key]
        if not (self.reply_use_video and ctx.get("media_url")):
            return ""
        from ..tools.understand_tool import handle_understand_video
        from .douyin_service import download

        os.makedirs(DOWNLOAD_DIR_DY, exist_ok=True)
        path = os.path.join(DOWNLOAD_DIR_DY, f"{work_id}.mp4")
        if not os.path.exists(path):
            await download(ctx["media_url"], path, self.plugin.effective_douyin_cookie)
        if not os.path.exists(path):
            return ""
        # 走插件统一的视频理解流程（按 video_understand_method 选择 Gemini/MiMo/本地 ASR 并自动回退）
        res = await handle_understand_video(self.plugin, path, "用 150 字以内概括这个短视频的内容、看点和情绪基调，供博主回复评论时参考。")
        text = res.get("analysis") if isinstance(res, dict) else ""
        if not text:
            logger.info(f"[creator] 视频理解失败，仅用文案起草回复: {res.get('error') if isinstance(res, dict) else res}")
        if text:
            cache[key] = text.strip()
            os.makedirs(CREATOR_DIR, exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=1)
        return text or ""

    async def _draft(self, event, ctx: dict, summary: str, picked: list[Comment]) -> list[str]:
        ctx_obj = self.plugin.context
        provider_id = self.reply_provider or await ctx_obj.get_current_chat_provider_id(event.unified_msg_origin)
        listing = "\n".join(f"{i}. {c.user}（👍{c.digg}）: {c.text}" for i, c in enumerate(picked, 1))
        system = (
            f"你在替短视频博主「{ctx.get('author') or '博主'}」回复粉丝评论。账号风格：{self.reply_style}。\n"
            "要求：每条回复 30 字以内，贴合评论本身和视频内容，不要编造视频里没有的事实，不要引战，不要带链接或联系方式。\n"
            '只输出 JSON 数组，例如 ["回复1", "回复2"]，顺序与评论编号一致。'
        )
        prompt = f"视频文案：{ctx.get('title') or '（无）'}\n视频内容：{summary or '（未分析）'}\n\n评论：\n{listing}"
        resp = await ctx_obj.llm_generate(chat_provider_id=provider_id, prompt=prompt, system_prompt=system)
        text = resp.completion_text or ""
        m = re.search(r"\[.*\]", text, re.S)
        try:
            replies = json.loads(m.group(0)) if m else []
        except json.JSONDecodeError:
            replies = []
        if len(replies) != len(picked):
            raise RuntimeError(f"LLM 返回的回复数量不对：{text[:200]}")
        return [str(r).strip() for r in replies]

    @staticmethod
    def match_commenter(comments: list[Comment], target: str) -> list[Comment]:
        """按账号 ID（抖音号）/ 昵称 / uid 找某个人的评论（忽略开头的 @ 和大小写）。"""
        t = target.lstrip("@＠").strip().lower()
        return [c for c in comments if c.text.strip() and t in (c.handle.lower(), c.user.lower(), c.user_id)]

    async def prepare_replies(self, event, platform: str, work_ref: str, target: str = "") -> PendingOp | str:
        creator = get_creator(self.plugin, platform)
        if work_ref:
            work_id = await creator.resolve_work(work_ref)
            if not work_id:
                return f"无法识别作品：{work_ref}"
        else:
            works = await creator.list_works(limit=1)
            if not works:
                return "当前账号没有作品"
            work_id = works[0].id
        comments = await creator.list_comments(work_id)
        ctx = await creator.work_context(work_id)
        if target:
            # 指定了回复对象：不受时间窗口/随机挑选限制，只回这个人的评论
            picked = self.match_commenter(comments, target)[: self.reply_max]
            if not picked:
                return f"作品 {work_id} 的评论里没找到 {target}（共读取 {len(comments)} 条）"
        else:
            picked = self.pick_comments(platform, comments, ctx.get("author", ""))
        if not picked:
            return f"作品 {work_id} 在 {self.reply_window_h} 小时内没有可回复的新评论（共读取 {len(comments)} 条）"
        summary = await self._video_summary(platform, work_id, ctx)
        drafts = await self._draft(event, ctx, summary, picked)
        items = [
            {"id": c.id, "user": c.user, "text": c.text, "digg": c.digg, "create_time": c.create_time, "reply": r}
            for c, r in zip(picked, drafts)
        ]
        return self._new_op("reply", platform, event, {"work_id": work_id, "items": items}, ttl=max(self.confirm_timeout, 300))

    async def _run_replies(self, creator, op: PendingOp, picks: list[int] | None) -> str:
        items = op.payload["items"]
        chosen = [items[i - 1] for i in picks if 1 <= i <= len(items)] if picks else items
        lines, done = [], []
        for n, it in enumerate(chosen):
            if n:
                await asyncio.sleep(random.uniform(4, 9))  # 别连发，降低风控
            res = await creator.reply_comment(op.payload["work_id"], it["id"], it["reply"])
            lines.append(f"{'✅' if res.ok else '❌'} {it['user']}: {it['reply']}" + ("" if res.ok else f"（{res.message}）"))
            if res.ok:
                done.append(it["id"])
        self._mark_replied(op.platform, done)
        return f"评论回复完成 {len(done)}/{len(chosen)}：\n" + "\n".join(lines)
