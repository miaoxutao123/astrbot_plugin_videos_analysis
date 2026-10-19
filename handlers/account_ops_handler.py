"""
账号运维 & 创作者操作命令（只做参数解析与渲染，逻辑在 AccountManager / CreatorOps）
"""

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


def _take_platform(args: list[str]) -> tuple[str, list[str]]:
    if args and normalize_platform(args[0]):
        return normalize_platform(args[0]), args[1:]
    return "douyin", args


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
    platform, _ = _take_platform(command_args(event, "va_works"))
    try:
        works = await get_creator(plugin, platform).list_works(limit=10)
    except Exception as e:
        yield event.plain_result(f"读取作品失败: {e}")
        return
    plugin.creator_ops.last_works[platform] = works
    if not works:
        yield event.plain_result("当前账号没有作品")
        return
    lines = [f"{platform}·{plugin.accounts.active_name(platform) or '当前账号'} 最近作品："]
    for i, w in enumerate(works, 1):
        ts = time.strftime("%m-%d %H:%M", time.localtime(w.create_time)) if w.create_time else ""
        lines.append(f"{i}. {w.title[:30]}  👍{w.digg} 💬{w.comments}  {ts}\n   {w.id}")
    lines.append("\n/va_reply <序号> 精选评论回复 · /va_delete <序号> 删除")
    yield event.plain_result("\n".join(lines))


def _work_from_index(plugin, platform: str, ref: str):
    works = plugin.creator_ops.last_works.get(platform) or []
    if ref.isdigit() and len(ref) <= 2:
        idx = int(ref)
        if 1 <= idx <= len(works):
            return works[idx - 1]
        raise ValueError("序号无效，先发 /va_works 查看作品列表")
    return None


async def handle_upload(plugin, event):
    platform, args = _take_platform(command_args(event, "va_upload"))
    title, tags = split_title_tags(" ".join(args))
    if not title:
        yield event.plain_result(f"用法: /va_upload [平台] <标题> #话题1 #话题2\n可同时附带视频或引用一条视频消息；否则在 2 分钟内补发视频。\n支持平台: {', '.join(REGISTRY)}")
        return
    source = await plugin.creator_ops.extract_video(event)
    if not source:
        plugin.creator_ops.wait_for_upload(event, platform, title, tags)
        yield event.plain_result(f"请在 2 分钟内发送要发布到 {platform} 的视频。")
        return
    async for r in _prepare_upload(plugin, event, platform, source, title, tags):
        yield r


async def handle_upload_video_arrived(plugin, event, waiter: dict, source: str):
    """/va_upload 之后补发的视频。"""
    async for r in _prepare_upload(plugin, event, waiter["platform"], source, waiter["title"], waiter["tags"]):
        yield r


async def _prepare_upload(plugin, event, platform, source, title, tags):
    try:
        op = await plugin.creator_ops.prepare_upload(event, platform, source, title, tags)
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
        work = _work_from_index(plugin, platform, args[0])
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
    platform, args = _take_platform(command_args(event, "va_reply"))
    ref = args[0] if args else ""
    try:
        work = _work_from_index(plugin, platform, ref) if ref else None
    except ValueError as e:
        yield event.plain_result(str(e))
        return
    yield event.plain_result("正在读取评论并起草回复（可能需要分析视频，约 10~60 秒）...")
    try:
        res = await plugin.creator_ops.prepare_replies(event, platform, work.id if work else ref)
    except Exception as e:
        yield event.plain_result(f"起草失败: {e}")
        return
    yield event.plain_result(res if isinstance(res, str) else plugin.creator_ops.render_preview(res))


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
    text = await plugin.creator_ops.confirm(args[0], picks or None)
    yield event.plain_result(text)
    shot = text.split("失败截图: ", 1)[1].strip() if "失败截图: " in text else ""
    if shot:
        nap_path = await send_file_if_needed(shot, plugin.nap_server_address, plugin.nap_server_port)
        yield event.chain_result([Comp.Image.fromFileSystem(nap_path)])


async def handle_cancel(plugin, event):
    args = command_args(event, "va_cancel")
    yield event.plain_result(plugin.creator_ops.cancel(args[0]) if args else "用法: /va_cancel <确认码>")


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
