"""
账号运维 / 浏览 / 创作者操作的 LLM 工具实现（main.py 只做注册）

权限分三级：
- 公开读：搜索、推荐、浏览视频——任何人都能让 LLM 调用
- 管理员读：账号状态、我的作品、评论列表、账号资料——涉及账号隐私，仅管理员
- 危险写：发评论、回复、改资料、删除、发布、切换账号——任何人都只能"提交申请"，
  由 CreatorOps.submit 推给管理员；必须管理员亲自发送 /va_confirm 才执行，没有能代为确认的工具
所有工具都返回 dict，由 main.py 序列化成 JSON 给 LLM。
"""

import os

from ..handlers.account_ops_handler import _work_from_index
from ..services.creator import get_creator, normalize_platform

ADMIN_ONLY = {"error": "该信息仅管理员可查看，请礼貌拒绝"}


def _platform(name: str, ref: str = "") -> str | None:
    if name:
        return normalize_platform(name)
    from ..handlers.account_ops_handler import _BILI_REF

    return "bilibili" if _BILI_REF.search(ref or "") else "douyin"


def _works_view(works) -> list[dict]:
    return [
        {"index": i, "id": w.id, "title": w.title, "author": w.author, "views": w.views,
         "likes": w.digg, "comments": w.comments, "duration_s": w.duration, "url": w.url}
        for i, w in enumerate(works, 1)
    ]


def _allow_request(plugin, event) -> dict | None:
    if event.is_admin() or plugin.config.get("llm_allow_non_admin_requests", True):
        return None
    return {"error": "只有管理员可以发起这个操作，请礼貌拒绝"}


async def _resolve(plugin, platform: str, ref: str, account: str | None):
    work = _work_from_index(plugin, platform, ref) if ref else None
    creator = get_creator(plugin, platform, account)
    return creator, (work.id if work else await creator.resolve_work(ref))


# ── 公开读 ───────────────────────────────────────────────


async def search_videos(plugin, event, keyword: str) -> dict:
    works = await get_creator(plugin, "bilibili").search(keyword, limit=10)
    plugin.creator_ops.last_works["bilibili"] = works
    return {"platform": "bilibili", "keyword": keyword, "results": _works_view(works)}


async def feed(plugin, event, kind: str = "rcmd") -> dict:
    works = await get_creator(plugin, "bilibili").feed("hot" if kind in ("hot", "热门") else "rcmd", limit=10)
    plugin.creator_ops.last_works["bilibili"] = works
    return {"platform": "bilibili", "kind": kind, "results": _works_view(works)}


async def browse_video(plugin, event, video: str) -> dict:
    from ..services.browse_service import browse_work

    platform = _platform("", video)
    creator, work_id = await _resolve(plugin, platform, video, None)
    if not work_id:
        return {"error": f"无法识别视频: {video}"}
    ctx = await creator.work_context(work_id)
    # 观看记录写在机器人自己的账号上，只有管理员发起时才上报
    seconds = 0
    if event.is_admin() and plugin.config.get("browse_report_history", True):
        seconds = int(plugin.config.get("browse_report_seconds", 60) or 0)
    res = await browse_work(plugin, event, platform, work_id, ctx, report_seconds=seconds)
    out = {
        "platform": platform, "id": work_id, "title": ctx.get("title"), "author": ctx.get("author"),
        "summary": res["summary"], "transcript_source": res["transcript_source"],
        "transcript_excerpt": res["transcript"][:1500], "method": res["method"],
    }
    if hasattr(creator, "hot_comments"):
        try:
            out["hot_comments"] = [{"user": c.user, "likes": c.digg, "text": c.text[:120]} for c in await creator.hot_comments(work_id, 3)]
        except Exception:
            pass
    return out


# ── 管理员读 ─────────────────────────────────────────────


async def account_status(plugin, event) -> dict:
    if not event.is_admin():
        return ADMIN_ONLY
    statuses = await plugin.accounts.status_all(deep=False)
    return {"accounts": [{"platform": st.platform, "ok": st.ok, "summary": st.summary, **{k: str(v) for k, v in st.details.items()}} for st in statuses],
            "pool": plugin.accounts.pool_list()}


async def list_works(plugin, event, platform: str = "bilibili", account: str = "") -> dict:
    if not event.is_admin():
        return ADMIN_ONLY
    plat = _platform(platform)
    works = await get_creator(plugin, plat, account or None).list_works(limit=10)
    plugin.creator_ops.last_works[plat] = works
    return {"platform": plat, "account": account or plugin.accounts.active_name(plat) or "当前账号", "works": _works_view(works)}


async def list_comments(plugin, event, work: str, platform: str = "", account: str = "") -> dict:
    if not event.is_admin():
        return ADMIN_ONLY
    plat = _platform(platform, work)
    creator, work_id = await _resolve(plugin, plat, work, account or None)
    if not work_id:
        return {"error": f"无法识别作品: {work}"}
    comments = await creator.list_comments(work_id, max_count=40)
    return {"platform": plat, "work_id": work_id, "comments": [
        {"id": c.id, "user": c.user, "user_id": c.user_id, "likes": c.digg, "time": c.create_time, "text": c.text[:200]} for c in comments
    ]}


async def profile(plugin, event, account: str = "") -> dict:
    if not event.is_admin():
        return ADMIN_ONLY
    p = await get_creator(plugin, "bilibili", account or None).profile()
    return {"platform": "bilibili", **{k: v for k, v in p.items() if k != "face"}}


# ── 危险写（只提交申请） ─────────────────────────────────


async def request_comment(plugin, event, video: str, text: str = "", account: str = "") -> dict:
    if denied := _allow_request(plugin, event):
        return denied
    plat = _platform("", video)
    creator, work_id = await _resolve(plugin, plat, video, account or None)
    if not work_id:
        return {"error": f"无法识别视频: {video}"}
    res = await plugin.creator_ops.prepare_comment(event, plat, work_id, text, account or None)
    return {"error": res} if isinstance(res, str) else await plugin.creator_ops.submit(res)


async def request_reply(plugin, event, work: str = "", target: str = "", platform: str = "", account: str = "") -> dict:
    if denied := _allow_request(plugin, event):
        return denied
    plat = _platform(platform, work)
    ref = work
    if work:
        w = _work_from_index(plugin, plat, work)
        ref = w.id if w else work
    res = await plugin.creator_ops.prepare_replies(event, plat, ref, target, account or None)
    return {"error": res} if isinstance(res, str) else await plugin.creator_ops.submit(res)


async def request_sign(plugin, event, text: str, account: str = "") -> dict:
    if denied := _allow_request(plugin, event):
        return denied
    res = await plugin.creator_ops.prepare_sign(event, "bilibili", text, account or None)
    return {"error": res} if isinstance(res, str) else await plugin.creator_ops.submit(res)


async def request_avatar(plugin, event, image: str = "", account: str = "") -> dict:
    from ..handlers.account_ops_handler import _extract_image

    if denied := _allow_request(plugin, event):
        return denied
    source = image or await _extract_image(event)
    if not source:
        return {"error": "没有找到图片：请让用户在消息里附带或引用一张图片"}
    if not source.startswith(("http://", "https://")) and not os.path.isfile(source):
        return {"error": "图片必须是 http(s) 链接或用户发送的图片"}
    op = await plugin.creator_ops.prepare_avatar(event, "bilibili", source, account or None)
    return await plugin.creator_ops.submit(op)


async def request_delete(plugin, event, work: str, platform: str = "", account: str = "") -> dict:
    if denied := _allow_request(plugin, event):
        return denied
    plat = _platform(platform, work)
    _, work_id = await _resolve(plugin, plat, work, account or None)
    if not work_id:
        return {"error": f"无法识别作品: {work}"}
    return await plugin.creator_ops.submit(plugin.creator_ops.prepare_delete(event, plat, work_id, "", account or None))


async def request_switch_account(plugin, event, platform: str, name: str) -> dict:
    if denied := _allow_request(plugin, event):
        return denied
    plat = normalize_platform(platform)
    if not plat:
        return {"error": f"不支持的平台: {platform}"}
    return await plugin.creator_ops.submit(plugin.creator_ops.prepare_pool_use(event, plat, name))
