"""
账号运维 & 创作者操作命令（只做参数解析与渲染，逻辑在 AccountManager / CreatorOps）
"""

import re
import time

import astrbot.api.message_components as Comp

from ..services.account_manager import POOL_PLATFORMS
from ..services.creator import REGISTRY, get_creator, normalize_platform
from ..services.creator_ops import split_title_tags
from ..utils.config_helper import send_file_if_needed


def command_args(event, *names: str) -> list[str]:
    """去掉命令词，返回剩余参数（按空白切分）。"""
    parts = (event.message_str or "").strip().split()
    if parts and parts[0].lstrip("/") in names:
        parts = parts[1:]
    return parts


_BILI_REF = re.compile(r"BV1[0-9A-Za-z]{9}|bilibili\.com/video/|b23\.tv/|bili2233\.cn/|^av\d+$", re.I)


def _take_platform(args: list[str]) -> tuple[str, list[str]]:
    """第一个参数是平台名就取走；否则按链接猜（B站链接/BV/av 号 → bilibili），默认抖音。"""
    if args and normalize_platform(args[0]):
        return normalize_platform(args[0]), args[1:]
    if args and _BILI_REF.search(args[0]):
        return "bilibili", args
    return "douyin", args


def _take_account(args: list[str]) -> tuple[str | None, list[str]]:
    """取出 "-a 账号名"（账号池里的名称），返回 (账号, 剩余参数)。"""
    if "-a" in args:
        i = args.index("-a")
        if i + 1 < len(args):
            return args[i + 1], args[:i] + args[i + 2:]
    return None, args


def _cmd_prefix(platform: str) -> str:
    return "" if platform == "douyin" else f"{platform} "


# ── 巡检 / 告警 ──────────────────────────────────────────


async def handle_health_check(plugin, event):
    yield event.plain_result("正在深度检查各平台账号...")
    statuses, changes = await plugin.accounts.run_health_check(notify=False)
    text = "\n".join(st.render() for st in statuses)
    if changes:
        text += f"\n\n与上次相比有 {len(changes)} 项状态变化。"
    every = plugin.accounts.check_interval_min
    text += f"\n\n定时巡检: {'每 ' + str(every) + ' 分钟' if every else '已关闭'} · 告警接收会话 {len(plugin.accounts.notify_targets())} 个（/va_notify 设置）"
    yield event.plain_result(text)


async def handle_notify(plugin, event):
    args = command_args(event, "va_notify")
    enable = not (args and args[0] in ("off", "关", "取消"))
    yield event.plain_result(plugin.accounts.set_notify(event.unified_msg_origin, enable))


# ── 账号池 ───────────────────────────────────────────────

POOL_USAGE = (
    "用法:\n"
    "/va_pool  查看账号池\n"
    "/va_pool save <douyin|bilibili> <名称>  保存当前登录态为命名账号\n"
    "/va_pool use <平台> <名称>  切换\n"
    "/va_pool del <平台> <名称>  删除快照\n"
    "添加第二个账号: /va_logout douyin → /dy_login 扫新号 → /va_pool save douyin 小号"
)


async def handle_pool(plugin, event):
    args = command_args(event, "va_pool")
    am = plugin.accounts
    if not args or args[0] == "list":
        lines = []
        for plat, accs in am.pool_list().items():
            lines.append(f"【{plat}】" + ("" if accs else " 空"))
            for a in accs:
                flag = "▶" if a["active"] else "  "
                extra = []
                if a["cooldown_left"]:
                    extra.append(f"冷却 {a['cooldown_left'] // 60} 分钟")
                if a["valid"] is False:
                    extra.append("已失效")
                if a["last_error"]:
                    extra.append(f"上次错误: {a['last_error'][:40]}")
                saved = time.strftime("%m-%d %H:%M", time.localtime(a["saved_at"])) if a["saved_at"] else ""
                lines.append(f" {flag} {a['name']}  保存于 {saved} {' · '.join(extra)}")
        yield event.plain_result("\n".join(lines) + f"\n\n连续失败 {am.rotate_threshold} 次自动轮换，冷却 {am.rotate_cooldown // 60} 分钟\n\n" + POOL_USAGE)
        return
    if len(args) < 3 or args[0] not in ("save", "use", "del") or args[1] not in POOL_PLATFORMS:
        yield event.plain_result(POOL_USAGE)
        return
    action, plat, name = args[0], args[1], " ".join(args[2:])
    try:
        if action == "save":
            msg = await am.pool_save(plat, name)
        elif action == "use":
            msg = await am.pool_use(plat, name)
        else:
            msg = am.pool_delete(plat, name)
    except Exception as e:
        msg = f"失败: {e}"
    yield event.plain_result(msg)


# ── 创作者操作 ───────────────────────────────────────────


async def handle_works(plugin, event):
    account, args = _take_account(command_args(event, "va_works"))
    platform, _ = _take_platform(args)
    try:
        works = await get_creator(plugin, platform, account).list_works(limit=10)
    except Exception as e:
        yield event.plain_result(f"读取作品失败: {e}")
        return
    plugin.creator_ops.remember_list(event, platform, works)
    if not works:
        yield event.plain_result("当前账号没有作品")
        return
    lines = [f"{platform}·{account or plugin.accounts.active_name(platform) or '当前账号'} 最近作品："]
    for i, w in enumerate(works, 1):
        ts = time.strftime("%m-%d %H:%M", time.localtime(w.create_time)) if w.create_time else ""
        lines.append(f"{i}. {w.title[:30]}  👍{w.digg} 💬{w.comments}  {ts}\n   {w.id}")
    pre = _cmd_prefix(platform)
    tail = f"\n/va_reply {pre}<序号> 精选评论回复 · /va_comment {pre}<序号> [内容] 发评论"
    if platform == "douyin":
        tail += " · /va_delete <序号> 删除"
    lines.append(tail)
    yield event.plain_result("\n".join(lines))


def _work_from_index(plugin, event, platform: str, ref: str):
    works = plugin.creator_ops.recall_list(event, platform)
    if ref.isdigit() and len(ref) <= 2:
        idx = int(ref)
        if 1 <= idx <= len(works):
            return works[idx - 1]
        raise ValueError("序号无效，先发 /va_works 查看作品列表")
    return None


_TID_RE = re.compile(r"^(?:tid|分区)[=:：](\d+)$", re.I)


async def handle_upload(plugin, event):
    platform, args = _take_platform(command_args(event, "va_upload"))
    opts = {}
    for a in list(args):
        if m := _TID_RE.match(a):
            opts["tid"] = int(m.group(1))
            args.remove(a)
    title, tags = split_title_tags(" ".join(args))
    if not title:
        yield event.plain_result(
            f"用法: /va_upload [平台] <标题> #话题1 #话题2 [tid=分区ID]\n可同时附带视频或引用一条视频消息；否则在 2 分钟内补发视频。\n"
            f"B站：同一条消息里带一张图片作为封面（不带就取视频第 1 秒），至少 1 个 #话题。\n支持平台: {', '.join(REGISTRY)}"
        )
        return
    if cover := await _extract_image(event):
        opts["cover"] = cover
    source = await plugin.creator_ops.extract_video(event)
    if not source:
        plugin.creator_ops.wait_for_upload(event, platform, title, tags)
        plugin.creator_ops.peek_upload_waiter(event)["opts"] = opts
        yield event.plain_result(f"请在 2 分钟内发送要发布到 {platform} 的视频。")
        return
    async for r in _prepare_upload(plugin, event, platform, source, title, tags, opts):
        yield r


async def handle_upload_video_arrived(plugin, event, waiter: dict, source: str):
    """/va_upload 之后补发的视频。"""
    async for r in _prepare_upload(plugin, event, waiter["platform"], source, waiter["title"], waiter["tags"], waiter.get("opts")):
        yield r


async def _prepare_upload(plugin, event, platform, source, title, tags, opts=None):
    try:
        op = await plugin.creator_ops.prepare_upload(event, platform, source, title, tags, opts=opts)
    except Exception as e:
        yield event.plain_result(f"准备发布失败: {e}")
        return
    yield event.plain_result(plugin.creator_ops.render_preview(op))


async def handle_delete(plugin, event):
    platform, args = _take_platform(command_args(event, "va_delete"))
    if not args:
        yield event.plain_result("用法: /va_delete [平台] <作品序号|链接|ID>（序号来自 /va_works）")
        return
    try:
        work = _work_from_index(plugin, event, platform, args[0])
        work_id = work.id if work else await get_creator(plugin, platform).resolve_work(args[0])
        title = work.title if work else ""
    except Exception as e:
        yield event.plain_result(str(e))
        return
    if not work_id:
        yield event.plain_result(f"无法识别作品: {args[0]}")
        return
    op = plugin.creator_ops.prepare_delete(event, platform, work_id, title)
    yield event.plain_result(plugin.creator_ops.render_preview(op))


async def handle_reply(plugin, event):
    account, args = _take_account(command_args(event, "va_reply"))
    platform, args = _take_platform(args)
    # @抖音号/@昵称：只回复这个人的评论
    target = next((a for a in args if a.startswith(("@", "＠"))), "")
    args = [a for a in args if a != target]
    ref = args[0] if args else ""
    try:
        work = _work_from_index(plugin, event, platform, ref) if ref else None
    except ValueError as e:
        yield event.plain_result(str(e))
        return
    yield event.plain_result("正在读取评论并起草回复（可能需要分析视频，约 10~60 秒）...")
    try:
        res = await plugin.creator_ops.prepare_replies(event, platform, work.id if work else ref, target, account)
    except Exception as e:
        yield event.plain_result(f"起草失败: {e}")
        return
    yield event.plain_result(res if isinstance(res, str) else plugin.creator_ops.render_preview(res))


async def handle_comment(plugin, event):
    account, args = _take_account(command_args(event, "va_comment"))
    platform, args = _take_platform(args)
    if not args:
        yield event.plain_result(
            "用法: /va_comment [平台] [-a 账号名] <序号|链接|BV号> [评论内容]\n"
            "不写内容时会结合视频内容由 AI 起草，确认前可 /va_edit <确认码> 1 <新内容> 修改。"
        )
        return
    ref, text = args[0], " ".join(args[1:]).strip()
    try:
        work = _work_from_index(plugin, event, platform, ref)
    except ValueError as e:
        yield event.plain_result(str(e))
        return
    if not text:
        yield event.plain_result("正在理解视频并起草评论（约 10~60 秒）...")
    try:
        res = await plugin.creator_ops.prepare_comment(event, platform, work.id if work else ref, text, account)
    except Exception as e:
        yield event.plain_result(f"准备评论失败: {e}")
        return
    yield event.plain_result(res if isinstance(res, str) else plugin.creator_ops.render_preview(res))


async def handle_bili_refresh(plugin, event):
    force = "force" in command_args(event, "bili_refresh")
    yield event.plain_result("\n".join(await plugin.accounts.refresh_all_bilibili(force=force)) or "B站未登录")


async def handle_confirm(plugin, event):
    args = command_args(event, "va_confirm")
    if not args:
        yield event.plain_result("用法: /va_confirm <确认码> [序号...]")
        return
    if not plugin.creator_ops.has_pending(args[0]):
        yield event.plain_result(f"没有待确认的操作 {args[0]}（可能已过期或已取消）")
        return
    picks = [int(a) for a in args[1:] if a.isdigit()]
    yield event.plain_result("执行中...")
    text = await plugin.creator_ops.confirm(args[0], picks or None, event)
    yield event.plain_result(text)
    shot = text.split("失败截图: ", 1)[1].strip() if "失败截图: " in text else ""
    if shot:
        nap_path = await send_file_if_needed(shot, plugin.nap_server_address, plugin.nap_server_port)
        yield event.chain_result([Comp.Image.fromFileSystem(nap_path)])


async def handle_cancel(plugin, event):
    args = command_args(event, "va_cancel")
    yield event.plain_result(plugin.creator_ops.cancel(args[0], event) if args else "用法: /va_cancel <确认码>")


async def handle_edit(plugin, event):
    args = command_args(event, "va_edit")
    if len(args) < 3 or not args[1].isdigit():
        yield event.plain_result("用法: /va_edit <确认码> <序号> <新回复>")
        return
    yield event.plain_result(plugin.creator_ops.edit_reply(args[0], int(args[1]), " ".join(args[2:])))


async def handle_pending(plugin, event):
    ops = plugin.creator_ops.pending_list()
    if not ops:
        yield event.plain_result("没有待确认的操作")
        return
    yield event.plain_result("\n".join(f"[{o['code']}] {o['platform']}·{o['account']} {o['summary']}（剩 {o['expires_in']} 秒）" for o in ops))


async def handle_audit(plugin, event):
    """/va_audit [条数] [平台] [申请码]"""
    from ..services import audit_log

    args = command_args(event, "va_audit")
    limit, platform, code = 10, "", ""
    for a in args:
        if normalize_platform(a):
            platform = normalize_platform(a)
        elif a.isdigit() and len(a) == 4:
            code = a
        elif a.isdigit():
            limit = max(1, min(50, int(a)))
    items = audit_log.recent(limit=limit, code=code, platform=platform)
    yield event.plain_result(("操作审计（新→旧）：\n" if items else "") + audit_log.render(items))


async def handle_report(plugin, event):
    """/va_report [平台] [天数]"""
    from ..services import ops_monitor

    platform, days = "", 7
    for a in command_args(event, "va_report", "运营数据"):
        if normalize_platform(a):
            platform = normalize_platform(a)
        elif a.isdigit():
            days = int(a)
    yield event.plain_result("正在读取各平台数据…")
    yield event.plain_result(ops_monitor.render_report(await ops_monitor.report(plugin, platform, days)))


async def handle_inbox(plugin, event):
    """/va_inbox [平台]"""
    from ..services import ops_monitor

    args = command_args(event, "va_inbox", "消息概况")
    platform = normalize_platform(args[0]) if args else ""
    yield event.plain_result(ops_monitor.render_inbox(await ops_monitor.inbox_view(plugin, platform or "")))


# ── B站浏览 / 账号资料 ───────────────────────────────────


def _fmt_count(n: int) -> str:
    return f"{n / 10000:.1f}万" if n >= 10000 else str(n)


def _render_list(platform: str, title: str, works) -> str:
    lines = [title]
    for i, w in enumerate(works, 1):
        dur = f"{w.duration // 60}:{w.duration % 60:02d}" if w.duration else ""
        lines.append(f"{i}. {w.title[:32]}\n   UP {w.author} · ▶{_fmt_count(w.views)} 👍{_fmt_count(w.digg)} {dur} · {w.id}")
    lines.append(f"\n/bili_watch <序号> 浏览（抽帧+语音转写）· /va_comment {_cmd_prefix(platform)}<序号> [内容] 发评论")
    return "\n".join(lines)


async def handle_bili_feed(plugin, event):
    args = command_args(event, "bili_feed")
    kind = "hot" if args and args[0] in ("hot", "热门") else "rcmd"
    try:
        works = await get_creator(plugin, "bilibili").feed(kind, limit=10)
    except Exception as e:
        yield event.plain_result(f"读取失败: {e}")
        return
    plugin.creator_ops.remember_list(event, "bilibili", works)
    yield event.plain_result(_render_list("bilibili", "B站热门：" if kind == "hot" else "B站首页推荐：", works) if works else "没有内容")


async def handle_bili_search(plugin, event):
    kw = " ".join(command_args(event, "bili_search")).strip()
    if not kw:
        yield event.plain_result("用法: /bili_search <关键词>")
        return
    try:
        works = await get_creator(plugin, "bilibili").search(kw, limit=10)
    except Exception as e:
        yield event.plain_result(f"搜索失败: {e}")
        return
    plugin.creator_ops.remember_list(event, "bilibili", works)
    yield event.plain_result(_render_list("bilibili", f"B站搜索「{kw}」：", works) if works else f"没有搜到「{kw}」")


async def handle_bili_watch(plugin, event):
    from ..services.browse_service import browse_work

    args = command_args(event, "bili_watch")
    if not args:
        yield event.plain_result("用法: /bili_watch <序号|BV号|链接>（序号来自 /bili_feed、/bili_search、/va_works bili）")
        return
    creator = get_creator(plugin, "bilibili")
    try:
        work = _work_from_index(plugin, event, "bilibili", args[0])
        work_id = work.id if work else await creator.resolve_work(args[0])
        if not work_id:
            yield event.plain_result(f"无法识别视频: {args[0]}")
            return
        ctx = await creator.work_context(work_id)
    except Exception as e:
        yield event.plain_result(f"读取视频失败: {e}")
        return
    yield event.plain_result(f"正在浏览《{ctx['title'][:30]}》（下载 360P → 抽帧 + 语音转写，约 20~90 秒）...")
    seconds = int(plugin.config.get("browse_report_seconds", 60) or 0) if plugin.config.get("browse_report_history", True) else 0
    try:
        res = await browse_work(plugin, event, "bilibili", work_id, ctx, report_seconds=seconds)
    except Exception as e:
        yield event.plain_result(f"浏览失败: {e}")
        return
    lines = [f"📺 {ctx['title']}\nUP {ctx.get('author', '')} · {work_id}", "", res["summary"] or "（没有生成概要）", ""]
    try:
        hot = await creator.hot_comments(work_id, 3)
        if hot:
            lines.append("🔥 热评：")
            lines += [f"· {c.user}（👍{_fmt_count(c.digg)}）: {c.text[:60]}" for c in hot]
    except Exception:
        pass
    lines.append(
        f"\n转写: {res['transcript_source']}（{res['transcript_len']} 字）· 抽帧: {res['frame_method']} {' '.join(res['frame_times'])}\n"
        f"总结: {res['method']} · 用时 {res['seconds']}s"
    )
    if res.get("watch_report"):
        lines.append(f"观看记录: {res['watch_report']}")
    lines.append(f"/va_comment {work_id} [内容] 发评论（不写内容由 AI 结合本次浏览起草）")
    yield event.plain_result("\n".join(lines))


async def handle_bili_profile(plugin, event):
    account, _ = _take_account(command_args(event, "bili_profile"))
    try:
        p = await get_creator(plugin, "bilibili", account).profile()
    except Exception as e:
        yield event.plain_result(f"读取资料失败: {e}")
        return
    text = (
        f"B站账号: {p['uname']}（mid {p['mid']}，Lv{p['level']}{'，大会员' if p['vip'] else ''}）\n"
        f"签名: {p['sign'] or '（空）'}\n关注 {p['following']} · 粉丝 {p['follower']} · 硬币 {p['coins']}\n\n"
        "/bili_sign <新签名> 改签名 · /bili_avatar（附带或引用一张图）换头像"
    )
    chain = [Comp.Plain(text)]
    if p.get("face"):
        chain.insert(0, Comp.Image.fromURL(p["face"]))
    yield event.chain_result(chain)


async def handle_bili_sign(plugin, event):
    account, args = _take_account(command_args(event, "bili_sign"))
    text = " ".join(args).strip()
    if not text:
        yield event.plain_result("用法: /bili_sign <新签名>（70 字以内）")
        return
    try:
        res = await plugin.creator_ops.prepare_sign(event, "bilibili", text, account)
    except Exception as e:
        yield event.plain_result(f"准备失败: {e}")
        return
    yield event.plain_result(res if isinstance(res, str) else plugin.creator_ops.render_preview(res))


async def _extract_image(event) -> str | None:
    from astrbot.core.message.components import Image, Reply

    segs = list(getattr(event.message_obj, "message", []) or [])
    for seg in list(segs):
        if isinstance(seg, Reply) and seg.chain:
            segs.extend(seg.chain)
    for seg in segs:
        if isinstance(seg, Image):
            try:
                return await seg.convert_to_file_path()
            except Exception:
                return seg.url or seg.file
    return None


async def handle_bili_avatar(plugin, event):
    source = await _extract_image(event)
    if not source:
        yield event.plain_result("请在 /bili_avatar 消息里附带一张图片，或引用一条图片消息再发 /bili_avatar")
        return
    try:
        account, _ = _take_account(command_args(event, "bili_avatar"))
        op = await plugin.creator_ops.prepare_avatar(event, "bilibili", source, account)
    except Exception as e:
        yield event.plain_result(f"处理图片失败: {e}")
        return
    nap = await send_file_if_needed(op.payload["path"], plugin.nap_server_address, plugin.nap_server_port)
    yield event.chain_result([Comp.Image.fromFileSystem(nap), Comp.Plain(plugin.creator_ops.render_preview(op))])
