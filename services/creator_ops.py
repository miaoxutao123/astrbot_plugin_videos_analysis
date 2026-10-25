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
from dataclasses import asdict, dataclass, field

import aiohttp

from astrbot.api import logger

from ..utils.paths import CREATOR_DIR, UPLOAD_DIR
from ..utils.redact import redact
from . import audit_log
from .creator import Comment, OpResult, get_creator

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".flv", ".avi"}
PENDING_FILE = os.path.join(CREATOR_DIR, "pending.json")  # 待审批操作（重载/重启后恢复）
PUBLISH_LOG = os.path.join(CREATOR_DIR, "published.jsonl")  # 发布结果（含来源插件），运营洞察按它统计
MAX_TTL = 86400
# 其他插件调用 publish_request 时可带的元数据，不传给平台
# （B站的 source 是转载来源，所以回传用 caller / caller_ref）
META_OPTS = ("confirm_ttl", "caller", "caller_ref")
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
    account_key: str = ""  # 执行时用的账号池名称（空 = 当前账号）
    requester_name: str = ""
    requester_admin: bool = True


def _work_url(platform: str, work_id: str) -> str:
    if not work_id:
        return ""
    return f"https://www.bilibili.com/video/{work_id}" if platform == "bilibili" else f"https://www.douyin.com/video/{work_id}"


def read_publish_log(limit: int = 200) -> list[dict]:
    """最近的发布结果（新→旧）。"""
    try:
        with open(PUBLISH_LOG, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    except FileNotFoundError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


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
        # 发布要先看视频、常常无人值守（定时生成），默认给更长的确认时间
        self.publish_timeout = min(MAX_TTL, max(self.confirm_timeout, int(float(cfg.get("publish_confirm_minutes", 720) or 0) * 60)))
        self.reply_max = max(1, min(5, int(cfg.get("reply_max_comments", 5))))
        self.reply_window_h = int(cfg.get("reply_window_hours", 48))
        self.reply_style = cfg.get("creator_reply_style", "") or "真诚、简短、口语化，像博主本人在和粉丝聊天，不用官腔，不带链接"
        self.reply_provider = cfg.get("creator_reply_provider_id", "")
        self.reply_use_video = cfg.get("creator_reply_video_understanding", True)
        self._pending: dict[str, PendingOp] = {}
        self.max_pending_per_user = int(cfg.get("max_pending_per_user", 3) or 3)
        self._upload_waiters: dict[str, dict] = {}
        # 最近一次列表（作品/搜索/推荐）按"会话+平台"分别保存，群里多人同时用也不会串号
        self.last_works: dict[str, list] = {}
        self.last_list_platform: dict[str, str] = {}
        self._load_pending()

    @staticmethod
    def _list_key(event, platform: str) -> str:
        return f"{event.unified_msg_origin}|{platform}"

    def remember_list(self, event, platform: str, works: list) -> None:
        self.last_works[self._list_key(event, platform)] = works
        self.last_list_platform[event.unified_msg_origin] = platform
        if len(self.last_works) > 500:  # 防止长期运行无限增长
            self.last_works.pop(next(iter(self.last_works)))

    def recall_list(self, event, platform: str) -> list:
        return self.last_works.get(self._list_key(event, platform)) or []

    def recall_platform(self, event) -> str:
        """当前会话最近一次列表来自哪个平台（用于"看第 2 个"这类按序号引用）。"""
        return self.last_list_platform.get(event.unified_msg_origin, "")

    # ── 二次确认 ─────────────────────────────────────────

    def _new_op(self, kind: str, platform: str, event, payload: dict, ttl: int | None = None, account: str | None = None) -> PendingOp:
        self._gc()
        requester = str(event.get_sender_id())
        mine = [o for o in self._pending.values() if o.requester == requester]
        if len(mine) >= self.max_pending_per_user:
            raise RuntimeError(f"你已有 {len(mine)} 个待审批的操作，请等管理员处理或 /va_cancel 后再提交")
        code = f"{secrets.randbelow(10000):04d}"
        while code in self._pending:
            code = f"{secrets.randbelow(10000):04d}"
        key = account or self.plugin.accounts.active_name(platform) or ""
        op = PendingOp(
            code=code, kind=kind, platform=platform,
            account=key or "当前登录账号", account_key=key,
            requester=requester, umo=event.unified_msg_origin,
            requester_name=str(event.get_sender_name() or requester), requester_admin=bool(event.is_admin()),
            expires=time.time() + (ttl or self.confirm_timeout), payload=payload,
        )
        self._pending[code] = op
        self._save_pending()
        audit_log.record("created", op, summary=self._summary(op), admin=op.requester_admin)
        return op

    def _gc(self) -> None:
        now = time.time()
        expired = [c for c, op in self._pending.items() if op.expires < now]
        for code in expired:
            op = self._pending.pop(code)
            audit_log.record("expired", op)
            self._remove_op_files(op)
            self._report(op, "expired", "超过有效期没有确认，已作废")
        if expired:
            self._save_pending()

    # ── 待审批持久化 ─────────────────────────────────────

    def _save_pending(self) -> None:
        try:
            os.makedirs(CREATOR_DIR, exist_ok=True)
            tmp = PENDING_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump([asdict(op) for op in self._pending.values()], f, ensure_ascii=False, default=str)
            os.replace(tmp, PENDING_FILE)
        except Exception as e:
            logger.warning(f"[creator] 保存待审批列表失败: {e}")

    def _load_pending(self) -> None:
        """恢复重载前的待审批操作；暂存目录里没人引用的素材才清掉。"""
        try:
            with open(PENDING_FILE, encoding="utf-8") as f:
                rows = json.load(f)
        except FileNotFoundError:
            rows = []
        except Exception as e:
            logger.warning(f"[creator] 读取待审批列表失败，按空处理: {e}")
            rows = []
        names = set(PendingOp.__dataclass_fields__)
        for row in rows if isinstance(rows, list) else []:
            try:
                op = PendingOp(**{k: v for k, v in row.items() if k in names})
            except TypeError:
                continue
            self._pending[op.code] = op
        keep = {os.path.abspath(f) for op in self._pending.values() for f in self._op_files(op)}
        if os.path.isdir(UPLOAD_DIR):
            for name in os.listdir(UPLOAD_DIR):
                path = os.path.abspath(os.path.join(UPLOAD_DIR, name))
                if path not in keep:
                    shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)
        if self._pending:
            logger.info(f"[creator] 恢复 {len(self._pending)} 个待审批操作")
        self._gc()

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

    def cancel(self, code: str, event=None, via: str = "") -> str:
        op = self._pending.pop(code, None)
        if not op:
            return f"没有待确认的操作 {code}（可能已过期）"
        self._save_pending()
        by, by_name = self._who(event)
        by_name = by_name or via
        audit_log.record("rejected", op, by=by, by_name=by_name)
        self._remove_op_files(op)
        self._report(op, "rejected", f"管理员拒绝了（{by_name or '管理员'}）")
        if not op.requester_admin:
            asyncio.create_task(self._send(op.umo, f"你提交的申请 [{code}]（{self._summary(op)}）未获管理员批准，已取消。"))
        return f"已取消 {code}"

    # ── 发布结果回传 ─────────────────────────────────────

    def _report(self, op: PendingOp, status: str, message: str, data: dict | None = None) -> None:
        """发布类申请有了结局（published/failed/rejected/expired）：记入发布日志，并回调来源插件。"""
        if op.kind != "upload":
            return
        p = op.payload
        work_id = str((data or {}).get("bvid") or (data or {}).get("aweme_id") or "")
        result = {
            "code": op.code, "status": status, "platform": op.platform, "account": op.account,
            "title": p.get("title", ""), "tags": p.get("tags", []), "work_id": work_id,
            "url": _work_url(op.platform, work_id), "message": redact(str(message))[:300],
            "caller": p.get("caller", ""), "caller_ref": p.get("caller_ref", ""), "at": int(time.time()),
        }
        try:
            asyncio.get_running_loop().create_task(self._deliver_result(result))
        except RuntimeError:  # 没有事件循环（理论上不会）：至少记下日志
            logger.warning(f"[creator] 无法回传发布结果 {op.code}: {status}")

    async def _deliver_result(self, result: dict) -> None:
        if result["status"] == "published" and not result["work_id"] and result["platform"] == "douyin":
            # 抖音发布页不返回作品 ID：稍等后按标题在作品列表里找
            await asyncio.sleep(5)
            try:
                for w in await get_creator(self.plugin, "douyin").list_works(5):
                    if result["title"] and (w.title.startswith(result["title"][:20]) or result["title"][:20] in w.title):
                        result.update(work_id=w.id, url=w.url)
                        break
            except Exception as e:
                logger.info(f"[creator] 查找刚发布的抖音作品失败: {e}")
        try:
            os.makedirs(CREATOR_DIR, exist_ok=True)
            with open(PUBLISH_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(result, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"[creator] 写发布日志失败: {e}")
        caller = result.get("caller")
        if not caller:
            return
        meta = self.plugin.context.get_registered_star(caller)
        fn = getattr(getattr(meta, "star_cls", None), "on_publish_result", None) if meta else None
        if not fn:
            logger.info(f"[creator] 来源插件 {caller} 未提供 on_publish_result，结果只记入发布日志")
            return
        try:
            await asyncio.wait_for(fn(dict(result)), timeout=30)
        except Exception as e:
            logger.warning(f"[creator] 回调 {caller}.on_publish_result 失败: {e}")

    # ── 提权审批 ─────────────────────────────────────────

    async def _send(self, umo: str, text: str) -> bool:
        from astrbot.core.message.message_event_result import MessageChain

        try:
            ok = await self.plugin.context.send_message(umo, MessageChain().message(text))
            logger.info(f"[creator] 已推送到 {umo}: {text.splitlines()[0][:60]}")
            return ok is not False
        except Exception as e:
            logger.warning(f"[creator] 发送到 {umo} 失败: {e}")
            return False

    async def submit(self, op: PendingOp) -> dict:
        """
        把待确认操作交给有权限的人：
        - 申请人是管理员：预览发回当前会话，由管理员本人 /va_confirm
        - 申请人不是管理员：预览推送到管理员告警会话（/va_notify），申请人只收到"已提交审批"
        LLM 只能走到这里——批准必须由管理员亲自发送 /va_confirm，没有任何工具能代为确认。
        """
        preview = self.render_preview(op)
        if op.requester_admin:
            await self._send(op.umo, preview)
            return {"status": "pending_confirmation", "code": op.code,
                    "message": f"尚未执行。确认信息已发到当前会话，需要管理员本人回复「确认 {op.code}」才会执行。请如实告知，不要声称已完成，也不要替用户确认。"}
        head = (
            f"🔐 权限申请：{op.requester_name}（{op.requester}）请求执行以下操作\n"
            f"来源会话：{op.umo}\n批准请回复「确认 {op.code}」，拒绝请回复「拒绝 {op.code}」\n\n"
        )
        sent = 0
        for umo in self.plugin.accounts.notify_targets():
            if umo != op.umo and await self._send(umo, head + preview):
                sent += 1
        if not sent:
            self._pending.pop(op.code, None)
            self._save_pending()
            audit_log.record("dropped", op, error="管理员未设置审批通知会话（/va_notify）")
            self._remove_op_files(op)
            return {"status": "rejected", "error": "该操作需要管理员批准，但管理员还没有设置审批通知会话（管理员私聊发送 /va_notify），已取消。"}
        logger.info(f"[creator] 申请 {op.code}（{op.kind}，来自 {op.requester}）已推送给 {sent} 个管理员会话")
        await self._send(op.umo, f"已提交管理员审批（申请码 {op.code}），管理员批准前不会执行。")
        return {"status": "pending_admin_approval", "code": op.code,
                "message": "尚未执行。已推送给管理员审批，批准前不会执行。请如实告知用户，不要声称已完成。"}

    def edit_reply(self, code: str, index: int, text: str) -> str:
        op = self._pending.get(code)
        if op and op.kind in ("comment", "sign"):
            op.payload.update(text=text, drafted=False)
            self._save_pending()
            return f"已修改 {code} 的评论内容，发送 /va_confirm {code} 执行"
        if not op or op.kind != "reply":
            return f"没有待确认的评论回复 {code}"
        items = op.payload["items"]
        if not 1 <= index <= len(items):
            return f"序号超出范围 1~{len(items)}"
        items[index - 1]["reply"] = text
        self._save_pending()
        return f"已修改 {code} 第 {index} 条回复，发送 /va_confirm {code} 执行"

    @staticmethod
    def _who(event) -> tuple[str, str]:
        if event is None:
            return "", ""
        uid = str(event.get_sender_id())
        return uid, str(event.get_sender_name() or uid)

    @staticmethod
    def _classify(text: str) -> tuple[str, str]:
        """执行结果文本 → (ok/failed/partial, 失败原因)。"""
        if m := re.match(r"评论回复完成 (\d+)/(\d+)", text):
            done, total = int(m.group(1)), int(m.group(2))
            fails = "；".join(re.findall(r"（([^）]+)）$", text, re.M))[:200]
            if done == total and total:
                return "ok", ""
            return ("partial" if done else "failed"), fails or "全部失败"
        if text.startswith("✅"):
            return "ok", ""
        first = text.splitlines()[0] if text else "无结果"
        return "failed", re.sub(r"^❌\s*", "", first)[:200]

    async def confirm(self, code: str, picks: list[int] | None = None, event=None) -> str:
        self._gc()
        op = self._pending.get(code)
        if not op:
            return f"没有待确认的操作 {code}（可能已过期或已处理）"
        result = await self._execute(op, picks)
        status, error = self._classify(result)
        if op.kind == "upload":
            res = op.payload.get("_result") or {}
            self._report(op, "published" if status == "ok" else "failed", res.get("message") or error or result,
                         res.get("data") or {})
        approver, approver_name = self._who(event)
        audit_log.record("approved", op, approver=approver, approver_name=approver_name,
                         result=status, error=error, summary=self._summary(op))
        if not op.requester_admin:
            await self._send(op.umo, f"你提交的申请 [{code}] 已由管理员批准并执行：\n{result}")
        return result

    async def _execute(self, op: PendingOp, picks: list[int] | None) -> str:
        self._pending.pop(op.code, None)
        self._save_pending()
        if op.platform == "douyin" and op.account_key != (self.plugin.accounts.active_name("douyin") or ""):
            return f"❌ 申请时用的是抖音账号「{op.account}」，现在已切换到「{self.plugin.accounts.active_name('douyin') or '其他账号'}」，为避免发错账号已取消，请重新发起"
        try:
            if op.kind == "config":
                return await self._apply_config(op.payload["changes"])
            if op.kind == "pool_use":
                return await self.plugin.accounts.pool_use(op.platform, op.payload["name"])
            creator = get_creator(self.plugin, op.platform, op.account_key or None)
            if op.kind == "upload":
                p = op.payload
                opts = dict(p.get("opts") or {})
                try:
                    res = await creator.upload(p["path"], p["title"], p["tags"], **opts)
                    p["_result"] = {"ok": res.ok, "message": res.message, "data": dict(res.data or {})}
                finally:
                    self._remove_staged(p["path"])  # 失败也清理，重发视频即可重试
                    self._remove_staged(opts.get("cover", ""))
                return self._render_result(f"发布《{p['title']}》", res)
            if op.kind == "delete":
                res = await creator.delete(op.payload["work_id"], op.payload.get("title", ""))
                return self._render_result(f"删除 {op.payload['work_id']}", res)
            if op.kind == "reply":
                return await self._run_replies(creator, op, picks)
            if op.kind == "comment":
                p = op.payload
                res = await creator.post_comment(p["work_id"], p["text"])
                return self._render_result(f"评论《{p.get('title') or p['work_id']}》", res)
            if op.kind == "sign":
                return self._render_result("修改签名", await creator.update_sign(op.payload["text"]))
            if op.kind == "avatar":
                try:
                    res = await creator.update_avatar(op.payload["path"])
                finally:
                    self._remove_staged(op.payload["path"])
                return self._render_result("修改头像", res)
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
            extra = ""
            if op.platform == "bilibili":
                o = p.get("opts") or {}
                tid = o.get("tid") or self.plugin.config.get("bili_upload_tid", 21)
                extra = f"，分区 {tid}，{'自定义封面' if o.get('cover') else '封面取视频第 1 秒'}"
            return f"发布《{p['title']}》 {' '.join('#' + t for t in p['tags'])}（{p['size_mb']:.1f}MB{extra}）"
        if op.kind == "delete":
            hint = "（B站若要求短信验证码，会改为仅自己可见）" if op.platform == "bilibili" else ""
            return f"删除作品 {p['work_id']}" + (f"《{p['title']}》" if p.get("title") else "") + hint
        if op.kind == "comment":
            who = f"（UP：{p['author']}）" if p.get("author") else ""
            return f"评论作品 {p['work_id']}《{p.get('title', '')}》{who}"
        if op.kind == "sign":
            return f"把签名改为「{p['text']}」（原签名「{p.get('old', '')}」）"
        if op.kind == "pool_use":
            return f"把 {op.platform} 当前账号切换为「{p['name']}」"
        if op.kind == "config":
            from .settings_service import diff_text

            return "修改插件设置：\n" + diff_text(p["changes"], self.plugin.config)
        if op.kind == "avatar":
            return f"更换头像（{p['size_kb']:.0f}KB）"
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
        if op.kind == "comment":
            src = "（LLM 起草）" if op.payload.get("drafted") else ""
            return (
                f"{head}\n\n评论内容{src}：{op.payload['text']}\n\n"
                f"回复「确认 {op.code}」发送 · /va_edit {op.code} 1 <新内容> 修改 · 回复「拒绝 {op.code}」取消"
                f"（{int(op.expires - time.time())} 秒内有效）"
            )
        return f"{head}\n\n回复「确认 {op.code}」执行，「拒绝 {op.code}」取消（也可用 /va_confirm、/va_cancel；{int(op.expires - time.time())} 秒内有效）"

    # ── 发布 ─────────────────────────────────────────────

    @staticmethod
    def _op_files(op: PendingOp) -> list[str]:
        if op.kind not in ("upload", "avatar"):
            return []
        return [f for f in (op.payload.get("path", ""), (op.payload.get("opts") or {}).get("cover", "")) if f]

    def _remove_op_files(self, op: PendingOp) -> None:
        for f in self._op_files(op):
            self._remove_staged(f)

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

    async def prepare_upload(self, event, platform: str, source: str, title: str, tags: list[str],
                             account: str | None = None, opts: dict | None = None) -> PendingOp:
        """opts（平台特有，目前 B站用）：cover=封面图路径/链接，tid=分区 ID，desc=简介。
        通用（不传给平台）：confirm_ttl=等待确认的秒数（最长 24 小时，默认 publish_confirm_minutes）；
        caller=来源插件名、caller_ref=对方的任务号——执行/拒绝/过期后回调 caller 插件的 on_publish_result。"""
        opts = dict(opts or {})
        meta = {k: opts.pop(k) for k in META_OPTS if k in opts}
        try:
            ttl = min(int(meta.get("confirm_ttl") or 0), MAX_TTL) or self.publish_timeout
        except (TypeError, ValueError):
            ttl = self.publish_timeout
        if not title:
            raise ValueError("缺少标题")
        if platform == "bilibili" and not tags:
            raise ValueError("B站投稿至少需要 1 个标签（#话题）")
        opts = {k: v for k, v in (opts or {}).items() if k in ("cover", "tid", "desc", "copyright", "source") and v}
        if platform != "bilibili":
            opts = {}  # 抖音发布页暂不支持这些参数
        if opts.get("cover"):
            opts["cover"] = await self.stage_image(opts["cover"], "cover")
        path = await self.stage_video(source)
        size_mb = os.path.getsize(path) / 1048576
        payload = {"path": path, "title": title, "tags": tags, "size_mb": size_mb, "opts": opts,
                   "caller": str(meta.get("caller") or ""), "caller_ref": str(meta.get("caller_ref") or "")}
        return self._new_op("upload", platform, event, payload, ttl=ttl, account=account)

    async def stage_image(self, source: str, prefix: str) -> str:
        """把图片（本地路径或 URL）复制到 UPLOAD_DIR 并转成 JPEG。"""
        from PIL import Image

        os.makedirs(UPLOAD_DIR, exist_ok=True)
        dest = os.path.join(UPLOAD_DIR, f"{prefix}_{int(time.time())}_{secrets.token_hex(3)}.jpg")
        src = source
        if source.startswith(("http://", "https://")):
            src = dest + ".src"
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
                async with s.get(source) as r:
                    r.raise_for_status()
                    with open(src, "wb") as f:
                        f.write(await r.read())
        elif not os.path.isfile(source):
            raise ValueError(f"找不到图片: {source[:100]}")
        try:
            with Image.open(src) as im:
                im.convert("RGB").save(dest, "JPEG", quality=92)
        finally:
            if src != source and os.path.exists(src):
                os.remove(src)
        return dest

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

    def prepare_config(self, event, changes: dict) -> PendingOp:
        return self._new_op("config", "plugin", event, {"changes": changes})

    async def _apply_config(self, changes: dict) -> str:
        """写入配置文件并稍后重载插件，让所有缓存的配置项生效。"""
        from .settings_service import diff_text

        text = diff_text(changes, self.plugin.config)
        for k, v in changes.items():
            self.plugin.config[k] = v
        self.plugin.config.save_config()
        mgr = getattr(self.plugin.context, "_star_manager", None)
        if mgr:
            async def _reload():
                await asyncio.sleep(1.5)  # 先把结果发出去再重载
                try:
                    await mgr.reload("astrbot_plugin_videos_analysis")
                except Exception as e:
                    logger.warning(f"[settings] 重载插件失败，请手动重启: {e}")
            asyncio.create_task(_reload())
            return f"✅ 设置已保存，插件将自动重载生效：\n{text}"
        return f"✅ 设置已保存（请重启插件生效）：\n{text}"

    def prepare_pool_use(self, event, platform: str, name: str) -> PendingOp:
        if not self.plugin.accounts._find(platform, name):
            raise ValueError(f"账号池里没有 {platform}/{name}")
        return self._new_op("pool_use", platform, event, {"name": name})

    def prepare_delete(self, event, platform: str, work_id: str, title: str, account: str | None = None) -> PendingOp:
        return self._new_op("delete", platform, event, {"work_id": work_id, "title": title}, account=account)

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

    def cache_summary(self, platform: str, work_id: str, text: str) -> None:
        cache_file = os.path.join(CREATOR_DIR, "video_context.json")
        try:
            with open(cache_file, encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, json.JSONDecodeError):
            cache = {}
        cache[f"{platform}:{work_id}"] = text.strip()
        os.makedirs(CREATOR_DIR, exist_ok=True)
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)

    async def _video_summary(self, platform: str, work_id: str, ctx: dict, event=None) -> str:
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
        if event is not None:
            # B站走轻量浏览（字幕/线上ASR + 抽帧），结果会写进缓存
            from .browse_service import browse_work

            try:
                return (await browse_work(self.plugin, event, platform, work_id, ctx)).get("summary", "")
            except Exception as e:
                logger.info(f"[creator] 浏览视频失败，仅用文案起草: {e}")
                return ""
        from ..tools.understand_tool import handle_understand_video

        try:
            path = await get_creator(self.plugin, platform).fetch_video(work_id, ctx)
        except Exception as e:
            logger.info(f"[creator] 下载作品视频失败，仅用文案起草: {e}")
            path = None
        if not path or not os.path.exists(path):
            return ""
        # 走插件统一的视频理解流程（按 video_understand_method 选择 Gemini/MiMo/本地 ASR 并自动回退）
        res = await handle_understand_video(self.plugin, path, "用 150 字以内概括这个短视频的内容、看点和情绪基调，供博主回复评论时参考。", event)
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

    async def prepare_replies(self, event, platform: str, work_ref: str, target: str = "", account: str | None = None) -> PendingOp | str:
        creator = get_creator(self.plugin, platform, account)
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
        summary = await self._video_summary(platform, work_id, ctx, event)
        drafts = await self._draft(event, ctx, summary, picked)
        items = [
            {"id": c.id, "user": c.user, "text": c.text, "digg": c.digg, "create_time": c.create_time, "reply": r}
            for c, r in zip(picked, drafts)
        ]
        return self._new_op("reply", platform, event, {"work_id": work_id, "items": items}, ttl=max(self.confirm_timeout, 300), account=account)

    async def prepare_comment(self, event, platform: str, work_ref: str, text: str = "", account: str | None = None) -> PendingOp | str:
        """对任意作品发一级评论；不给内容时结合视频内容由 LLM 起草，确认前可 /va_edit 修改。"""
        creator = get_creator(self.plugin, platform, account)
        logged_in, msg = await creator.check_login()
        if not logged_in:
            return f"{creator.label}未登录，无法发评论：{msg}"
        work_id = await creator.resolve_work(work_ref)
        if not work_id:
            return f"无法识别作品：{work_ref}"
        ctx = await creator.work_context(work_id)
        drafted = False
        if not text:
            summary = await self._video_summary(platform, work_id, ctx, event)
            text = await self._draft_comment(event, ctx, summary)
            drafted = True
        payload = {"work_id": work_id, "title": ctx.get("title", ""), "author": ctx.get("author", ""), "text": text, "drafted": drafted}
        return self._new_op("comment", platform, event, payload, ttl=max(self.confirm_timeout, 300), account=account)

    async def prepare_sign(self, event, platform: str, text: str, account: str | None = None) -> PendingOp | str:
        creator = get_creator(self.plugin, platform, account)
        if len(text) > 70:
            return f"签名最多 70 字，当前 {len(text)} 字"
        prof = await creator.profile()
        return self._new_op("sign", platform, event, {"text": text, "old": prof.get("sign", "")}, account=account)

    async def prepare_avatar(self, event, platform: str, source: str, account: str | None = None) -> PendingOp:
        from PIL import Image

        os.makedirs(UPLOAD_DIR, exist_ok=True)
        dest = os.path.join(UPLOAD_DIR, f"avatar_{int(time.time())}_{secrets.token_hex(3)}.jpg")
        tmp = ""
        if source.startswith(("http://", "https://")):
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
                async with s.get(source) as r:
                    r.raise_for_status()
                    raw = await r.read()
            tmp = dest + ".src"
            with open(tmp, "wb") as f:
                f.write(raw)
            source = tmp
        try:
            # 统一裁成不超过 1024px 的正方形 JPEG（B站头像要求方形、<2MB）
            with Image.open(source) as im:
                im = im.convert("RGB")
                side = min(im.size)
                left, top = (im.width - side) // 2, (im.height - side) // 2
                im.crop((left, top, left + side, top + side)).resize((min(side, 1024),) * 2).save(dest, "JPEG", quality=90)
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        return self._new_op("avatar", platform, event, {"path": dest, "size_kb": os.path.getsize(dest) / 1024}, account=account)

    async def _draft_comment(self, event, ctx: dict, summary: str) -> str:
        ctx_obj = self.plugin.context
        provider_id = self.reply_provider or await ctx_obj.get_current_chat_provider_id(event.unified_msg_origin)
        system = (
            f"你是一位普通观众，要在 UP 主「{ctx.get('author') or 'UP主'}」的视频下发一条评论。风格：{self.reply_style}。\n"
            "要求：40 字以内，针对视频具体内容，不要编造视频里没有的事实，不要引战，不要带链接或联系方式，不要自称 AI。只输出评论正文。"
        )
        prompt = f"视频标题：{ctx.get('title') or '（无）'}\n简介：{(ctx.get('desc') or '（无）')[:300]}\n视频内容：{summary or '（未分析）'}"
        resp = await ctx_obj.llm_generate(chat_provider_id=provider_id, prompt=prompt, system_prompt=system)
        text = (resp.completion_text or "").strip().strip('"“”')
        if not text:
            raise RuntimeError("LLM 没有返回评论内容")
        return text

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
