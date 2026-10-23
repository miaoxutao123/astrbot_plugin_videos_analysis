"""
LLM 工具实现（main.py 只做注册）——5 个按 action 路由的工具，控制上下文体积

| 工具                    | 权限         | action |
|-------------------------|--------------|--------|
| video_browse            | 所有人       | search / recommend / hot / watch / info |
| video_download          | 所有人(限频) | —（下载并发到当前会话）|
| video_account           | 仅管理员     | status / works / comments / profile |
| video_action            | 申请→管理员批准 | comment / reply / sign / avatar / delete / switch_account / upload |
| video_plugin_settings   | 仅管理员     | show / check / set（set 需批准）|

设计约束：
- 工具描述是静态文本，不含账号名、配置值等动态内容 → 工具前缀稳定，不破坏模型的提示词缓存
- 工具结果会被 AstrBot 写入对话历史、之后每轮都会重发 → 结果只返回必要字段并截断长文本
- 危险操作只能"提交申请"；批准必须是管理员本人发的消息，没有任何工具能代为确认
"""

import json
import os

from ..handlers.account_ops_handler import _BILI_REF, _work_from_index
from ..services.creator import get_creator, normalize_platform

ADMIN_ONLY = {"error": "仅管理员可用，请礼貌拒绝"}


def _cut(text: str, n: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[:n] + "…"


def _platform(name: str, ref: str = "") -> str:
    return normalize_platform(name) if name else ("bilibili" if _BILI_REF.search(ref or "") else "douyin")


def _works_view(works, with_stats: bool = False) -> list[dict]:
    out = []
    for i, w in enumerate(works, 1):
        item = {"i": i, "id": w.id, "title": _cut(w.title, 40)}
        if w.author:
            item["up"] = w.author
        if w.views:
            item["views"] = w.views
        if w.duration:
            item["dur"] = f"{w.duration // 60}:{w.duration % 60:02d}"
        if with_stats:
            item.update(likes=w.digg, comments=w.comments)
        out.append(item)
    return out


async def _resolve(plugin, event, platform: str, ref: str, account: str | None):
    work = _work_from_index(plugin, event, platform, ref) if ref and ref.isdigit() and len(ref) <= 2 else None
    creator = get_creator(plugin, platform, account)
    return creator, (work.id if work else await creator.resolve_work(ref))


def _allow_request(plugin, event) -> dict | None:
    if event.is_admin() or plugin.config.get("llm_allow_non_admin_requests", True):
        return None
    return {"error": "只有管理员可以发起这个操作，请礼貌拒绝"}


# ── video_browse ─────────────────────────────────────────


async def video_browse(plugin, event, action: str, query: str = "", question: str = "") -> dict:
    action = (action or "").strip().lower()
    creator = get_creator(plugin, "bilibili")
    if action == "search":
        if not query:
            return {"error": "search 需要 query（关键词）"}
        works = await creator.search(query, limit=10)
    elif action in ("recommend", "hot"):
        works = await creator.feed("hot" if action == "hot" else "rcmd", limit=10)
    elif action == "watch":
        return await _watch(plugin, event, query, question)
    elif action == "info":
        from .parse_tool import handle_parse_video_link

        res = await handle_parse_video_link(plugin, query)
        return {k: v for k, v in res.items() if k not in ("cover",)}
    else:
        return {"error": "action 只能是 search / recommend / hot / watch / info"}
    plugin.creator_ops.remember_list(event, "bilibili", works)
    return {"platform": "bilibili", "results": _works_view(works), "tip": "watch/download/comment 可直接用 i 序号"}


async def _watch(plugin, event, video: str, question: str = "") -> dict:
    """
    看视频。工具调用有超时（AstrBot 默认 120 秒），所以：
    - 已经看过的直接返回缓存
    - 短视频（≤ browse_sync_max_minutes）当场看完返回
    - 长视频转后台：立刻返回"正在看"，看完把结果推到当前会话，并写缓存供之后追问
    """
    import asyncio

    from ..services.browse_service import browse_work, cached_summary, max_seconds

    if not video:
        return {"error": "watch 需要 query（BV号/链接/序号）"}
    platform = _platform("", video)
    creator, work_id = await _resolve(plugin, event, platform, video, None)
    if not work_id:
        return {"error": f"无法识别视频: {video}"}
    ctx = await creator.work_context(work_id)
    base = {"id": work_id, "title": _cut(ctx.get("title"), 50), "up": ctx.get("author")}
    if (cached := cached_summary(plugin, platform, work_id)) and not question:
        return {**base, "summary": cached, "how": "之前看过（缓存）"}

    seconds = 0  # 观看记录写在机器人账号上，只有管理员发起才上报
    if event.is_admin() and plugin.config.get("browse_report_history", True):
        seconds = int(plugin.config.get("browse_report_seconds", 60) or 0)

    async def run() -> dict:
        res = await browse_work(plugin, event, platform, work_id, ctx, report_seconds=seconds)
        out = {**base, "summary": res["summary"], "how": f"{res['transcript_source']}；{res['frame_method']}；{res['method']}"}
        if res.get("partial"):
            out["note"] = res["partial"]
        if hasattr(creator, "hot_comments"):
            try:
                out["hot_comments"] = [f"{c.user}: {_cut(c.text, 60)}" for c in await creator.hot_comments(work_id, 3)]
            except Exception:
                pass
        return out

    dur = min(ctx.get("duration", 0) or 0, max_seconds(plugin))
    sync_limit = float(plugin.config.get("browse_sync_max_minutes", 8) or 8) * 60
    if dur <= sync_limit:
        return await run()

    async def background():
        try:
            out = await run()
            text = f"📺 看完了《{out['title']}》（UP {out.get('up', '')}）\n\n{out['summary']}"
            if out.get("note"):
                text += f"\n\n（{out['note']}）"
        except Exception as e:
            text = f"看《{base['title']}》时出错了：{e}"
        await plugin.creator_ops._send(event.unified_msg_origin, text)

    asyncio.create_task(background())
    return {**base, "status": "watching_in_background",
            "message": f"视频较长（约 {int(dur // 60)} 分钟），已开始在后台观看，看完会直接把总结发到聊天里（约 1~3 分钟）。请告诉用户稍等，不要重复调用。"}


# ── video_download ───────────────────────────────────────


async def video_download(plugin, event, video: str) -> dict:
    from ..services.deliver_service import deliver

    if not video:
        return {"error": "需要视频链接/BV号/序号"}
    ref = video
    if video.isdigit() and len(video) <= 2:
        w = None
        for plat in ("bilibili", "douyin"):
            try:
                w = _work_from_index(plugin, event, plat, video)
            except ValueError:
                continue
            if w:
                break
        if not w:
            return {"error": "序号无效：先搜索或列出视频，再按序号下载"}
        ref = w.url or w.id
    res = await deliver(plugin, event, ref)
    if res.get("status") == "sent":
        res["message"] = "已发送到当前会话，告诉用户查收即可，不要重复发送"
    return res


# ── video_account（管理员读） ─────────────────────────────


async def video_account(plugin, event, action: str, platform: str = "", target: str = "", account: str = "") -> dict:
    if not event.is_admin():
        return ADMIN_ONLY
    action = (action or "").strip().lower()
    if action == "status":
        sts = await plugin.accounts.status_all(deep=False)
        return {"accounts": [{"platform": st.platform, "ok": st.ok, "summary": st.summary} for st in sts],
                "pool": {k: [a["name"] + ("(当前)" if a["active"] else "") for a in v] for k, v in plugin.accounts.pool_list().items()}}
    plat = _platform(platform or "bilibili", target)
    if action == "works":
        works = await get_creator(plugin, plat, account or None).list_works(limit=10)
        plugin.creator_ops.remember_list(event, plat, works)
        return {"platform": plat, "works": _works_view(works, with_stats=True)}
    if action == "comments":
        creator, work_id = await _resolve(plugin, event, plat, target, account or None)
        if not work_id:
            return {"error": f"无法识别作品: {target}"}
        comments = await creator.list_comments(work_id, max_count=20)
        return {"work_id": work_id, "comments": [
            {"user": c.user, "uid": c.user_id, "likes": c.digg, "text": _cut(c.text, 80)} for c in comments[:20]
        ]}
    if action == "profile":
        p = await get_creator(plugin, "bilibili", account or None).profile()
        return {k: v for k, v in p.items() if k != "face"}
    return {"error": "action 只能是 status / works / comments / profile"}


# ── video_action（危险写：只提交申请） ─────────────────────


async def video_action(plugin, event, action: str, target: str = "", text: str = "", platform: str = "",
                       account: str = "", title: str = "", tags: str = "") -> dict:
    if denied := _allow_request(plugin, event):
        return denied
    action = (action or "").strip().lower()
    ops = plugin.creator_ops
    acc = account or None
    if action == "comment":
        plat = _platform(platform, target)
        _, work_id = await _resolve(plugin, event, plat, target, acc)
        if not work_id:
            return {"error": f"无法识别视频: {target}"}
        res = await ops.prepare_comment(event, plat, work_id, text, acc)
    elif action == "reply":
        plat = _platform(platform, target)
        ref = target
        if target and target.isdigit() and len(target) <= 2:
            w = _work_from_index(plugin, event, plat, target)
            ref = w.id if w else target
        res = await ops.prepare_replies(event, plat, ref, text, acc)  # text 这里是"只回复某人"
    elif action == "sign":
        if not text:
            return {"error": "sign 需要 text（新签名）"}
        res = await ops.prepare_sign(event, "bilibili", text, acc)
    elif action == "avatar":
        from ..handlers.account_ops_handler import _extract_image

        source = target or await _extract_image(event)
        if not source:
            return {"error": "没有找到图片：请让用户在消息里附带或引用一张图片"}
        if not source.startswith(("http://", "https://")) and not os.path.isfile(source):
            return {"error": "图片必须是 http(s) 链接或用户发送的图片"}
        res = await ops.prepare_avatar(event, "bilibili", source, acc)
    elif action == "delete":
        plat = _platform(platform, target)
        _, work_id = await _resolve(plugin, event, plat, target, acc)
        if not work_id:
            return {"error": f"无法识别作品: {target}"}
        res = ops.prepare_delete(event, plat, work_id, "", acc)
    elif action == "switch_account":
        plat = normalize_platform(platform)
        if not plat or not target:
            return {"error": "switch_account 需要 platform 和 target（账号池里的账号名）"}
        res = ops.prepare_pool_use(event, plat, target)
    elif action == "upload":
        from .creator_tool import handle_creator_upload

        return await handle_creator_upload(plugin, event, target, title or text, tags, platform or "douyin", account)
    else:
        return {"error": "action 只能是 comment / reply / sign / avatar / delete / switch_account / upload"}
    return {"error": res} if isinstance(res, str) else await ops.submit(res)


# ── video_plugin_settings ────────────────────────────────


async def video_plugin_settings(plugin, event, action: str, setting: str = "", value: str = "") -> dict:
    from ..services import settings_service as ss

    if not event.is_admin():
        return ADMIN_ONLY
    action = (action or "").strip().lower()
    if action == "show":
        return ss.show(plugin.config)
    if action == "check":
        return {"problems": await ss.check(plugin, event)}
    if action == "set":
        changes = {}
        if setting in ss.PRESETS:
            changes.update(ss.PRESETS[setting][0])
            setting = ""
        pairs = []
        # 批量：setting 或 value 任一是 JSON 对象都按"多项修改"处理（模型常把 JSON 放进 setting）
        blob = next((x for x in (setting, value) if (x or "").strip().startswith("{")), "")
        if blob:
            try:
                pairs += list(json.loads(blob).items())
            except json.JSONDecodeError:
                return {"error": "批量修改需要合法的 JSON 对象，如 {\"看视频时抽几帧\": 4}"}
        elif setting:
            pairs.append((setting, value))
        for name, raw in pairs:
            key = ss.resolve_key(name)
            if not key:
                return {"error": f"没有叫「{name}」的设置", "可能是": ss.suggest(name)}
            try:
                changes[key] = ss.parse_value(key, raw)
            except ValueError as e:
                return {"error": str(e)}
        if not changes:
            return {"error": "没有要修改的内容"}
        return await plugin.creator_ops.submit(plugin.creator_ops.prepare_config(event, changes))
    return {"error": "action 只能是 show / check / set"}
