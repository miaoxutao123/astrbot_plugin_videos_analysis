"""
WebUI 管理面板（pages/accounts）的后端接口

外部路径：/api/v1/plugins/extensions/astrbot_plugin_videos_analysis/<route>，需要先登录 AstrBot WebUI。

调试用途，**默认关闭**：配置 debug_webui_enabled=true 后重载插件才会注册下面的管理接口；
关闭时只注册 status 接口，页面据此提示如何开启（关闭时管理接口根本不存在，而不是注册后再拦截）。
任何接口都不返回 cookie / token / API Key 明文。
"""

import asyncio
import base64
import os
import shutil
import time
import uuid
from dataclasses import asdict

from astrbot.api import logger
from astrbot.api.web import request

from ..services.account_manager import PLATFORMS
from ..utils.paths import CREATOR_DIR, DOWNLOAD_DIR, IMAGE_DIR, PLUGIN_NAME


def _ok(data: dict) -> dict:
    return {"status": "ok", "message": None, "data": data}


def _err(msg: str) -> dict:
    return {"status": "error", "message": msg, "data": {}}


class PanelEvent:
    """面板操作的"事件"：以 WebUI 管理员身份执行，消息推送到第一个告警会话（没有就不推送）。"""

    def __init__(self, plugin, text: str = "", username: str = ""):
        targets = plugin.accounts.notify_targets()
        self._username = username or "WebUI 管理员"
        self.unified_msg_origin = targets[0] if targets else ""
        self.message_str = text
        self.message_obj = None
        self._plugin = plugin

    def is_admin(self) -> bool:
        return True

    def is_private_chat(self) -> bool:
        return True

    def get_sender_id(self) -> str:
        return "webui"

    def get_sender_name(self) -> str:
        return f"WebUI:{self._username}"

    def get_self_id(self) -> str:
        return "webui"

    async def send(self, chain) -> None:
        if self.unified_msg_origin:
            await self._plugin.context.send_message(self.unified_msg_origin, chain)

    def stop_event(self) -> None:
        pass


def _dir_size(path: str) -> tuple[int, int]:
    total, count = 0, 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
                count += 1
            except OSError:
                pass
    return total, count


def register_web_apis(plugin) -> None:
    ctx = plugin.context
    base = f"/{PLUGIN_NAME}"
    enabled = bool(plugin.config.get("debug_webui_enabled", False))

    async def status(**kwargs):
        return _ok({"enabled": enabled, "version": getattr(plugin, "version", "")})

    ctx.register_web_api(f"{base}/status", status, ["GET"], "管理面板是否开启")
    if not enabled:
        return

    am, ops = plugin.accounts, plugin.creator_ops
    jobs: dict[str, dict] = {}  # 后台任务：扫码登录、调试看视频

    def new_job(kind: str, coro) -> str:
        jid = uuid.uuid4().hex[:8]
        jobs[jid] = {"kind": kind, "state": "running", "started": time.time(), "result": None, "error": ""}

        async def run():
            try:
                jobs[jid]["result"] = await coro
                jobs[jid]["state"] = "done"
            except Exception as e:
                logger.warning(f"[panel] 任务 {kind} 失败: {e}")
                jobs[jid].update(state="failed", error=str(e)[:300])

        asyncio.create_task(run())
        for old in [k for k, v in jobs.items() if time.time() - v["started"] > 3600]:
            jobs.pop(old, None)
        return jid

    async def body() -> dict:
        return await request.json({}) or {}

    # ── 概览 ─────────────────────────────────────────────

    async def overview(**kwargs):
        from ..services import settings_service as ss

        live = [asdict(st) | {"state": st.state} for st in await am.status_all(deep=False)]
        try:
            problems = await ss.check(plugin, PanelEvent(plugin))
        except Exception as e:
            problems = [f"体检失败: {e}"]
        return _ok({
            "live": live,
            "last_check": am.last_health(),
            "last_check_at": am._health.get("checked_at"),
            "pool": am.pool_list(),
            "pending": ops.pending_list(),
            "problems": problems,
            "settings": {
                "interval_min": am.check_interval_min,
                "notify_targets": len(am.notify_targets()),
                "rotate_threshold": am.rotate_threshold,
                "rotate_cooldown_min": am.rotate_cooldown // 60,
            },
            "now": time.time(),
        })

    async def check(**kwargs):
        statuses, changes = await am.run_health_check(notify=True)
        return _ok({"changes": len(changes), "statuses": [asdict(s) | {"state": s.state} for s in statuses]})

    async def pool_use(**kwargs):
        b = await body()
        plat, name = b.get("platform", ""), b.get("name", "")
        if plat not in PLATFORMS or not name:
            return _err("参数错误")
        try:
            return _ok({"message": await am.pool_use(plat, name)})
        except Exception as e:
            return _err(str(e))

    # ── 审批 ─────────────────────────────────────────────

    async def pending(**kwargs):
        return _ok({"items": ops.pending_list()})

    async def pending_approve(**kwargs):
        b = await body()
        code = str(b.get("code", ""))
        if not ops.has_pending(code):
            return _err(f"没有待确认的操作 {code}（可能已过期）")
        picks = [int(x) for x in b.get("picks") or [] if str(x).isdigit()] or None
        return _ok({"message": await ops.confirm(code, picks, event=PanelEvent(plugin, username=request.username or ""))})

    async def pending_cancel(**kwargs):
        b = await body()
        return _ok({"message": ops.cancel(str(b.get("code", "")), event=PanelEvent(plugin, username=request.username or ""), via="WebUI")})

    async def audit(**kwargs):
        from ..services import audit_log

        limit = int(request.query.get("limit", "100") or 100)
        return _ok({"items": audit_log.recent(limit=min(limit, 500))})

    # ── 设置 ─────────────────────────────────────────────

    async def settings_get(**kwargs):
        from ..services import settings_service as ss

        items = []
        for k, (cn, typ, desc, secret) in ss.CATALOG.items():
            v = plugin.config.get(k)
            items.append({"key": k, "name": cn, "type": typ, "help": desc, "secret": secret,
                          "value": (ss._mask(v) if secret else v)})
        items.append({"key": "debug_webui_enabled", "name": "调试管理面板", "type": "bool",
                      "help": "关闭后本页面不可用，需在插件配置里重新打开", "secret": False,
                      "value": plugin.config.get("debug_webui_enabled", False)})
        return _ok({"items": items})

    async def settings_save(**kwargs):
        """面板里的修改由已登录 WebUI 的管理员本人操作，直接生效（不再走聊天审批），并自动重载插件。"""
        from ..services import settings_service as ss

        changes, raw = {}, (await body()).get("changes") or {}
        for key, val in raw.items():
            if key == "debug_webui_enabled":
                changes[key] = bool(val)
                continue
            if key not in ss.CATALOG:
                return _err(f"未知设置 {key}")
            if ss.CATALOG[key][3] and val in ("", None):
                continue  # 密钥留空 = 不修改；要清空请填 __clear__
            try:
                changes[key] = "" if val == "__clear__" else ss.parse_value(key, val)
            except ValueError as e:
                return _err(str(e))
        if not changes:
            return _ok({"message": "没有修改"})
        return _ok({"message": await ops._apply_config(changes)})

    # ── 登录 ─────────────────────────────────────────────

    async def login_bili(**kwargs):
        from ..services import bilibili_service as bs

        qr = await bs.generate_qrcode()
        if not qr:
            return _err("生成二维码失败")
        jid = new_job("bili_login", bs.check_login_status_loop(qr["qrcode_key"]))
        return _ok({"job": jid, "image": qr["image_base64"], "expires_in": 150})

    async def login_douyin(**kwargs):
        signer = plugin.browser_signer
        if not signer or not signer.is_ready:
            return _err(f"抖音签名引擎未就绪：{plugin.browser_signer_error or '请先在聊天里发送 /dy_restart'}")
        if signer.is_logged_in:
            return _err("抖音已登录。要换号请先在聊天里发送 /va_logout douyin")
        await signer.trigger_login()
        shot = await signer.get_login_qr_screenshot()
        if not shot:
            return _err("没截到登录二维码，请稍后重试")

        async def wait():
            ok = await signer.wait_for_login(timeout_seconds=120)
            if ok:
                plugin._douyin_cookie_from_scan = await signer.get_cookie_string()
            return ok

        jid = new_job("douyin_login", wait())
        return _ok({"job": jid, "image": base64.b64encode(shot).decode(), "expires_in": 120})

    # ── 调试 ─────────────────────────────────────────────

    async def debug_run(**kwargs):
        """调试工具：info/search 当场返回；watch 较慢，放后台任务轮询。"""
        from ..tools import ops_tools

        b = await body()
        action, query, plat = b.get("action", ""), (b.get("query") or "").strip(), b.get("platform", "")
        if not query and action != "recommend":
            return _err("请输入链接 / BV 号 / 关键词")
        ev = PanelEvent(plugin, query)
        if action == "watch":
            return _ok({"job": new_job("watch", _debug_watch(plugin, ev, query))})
        if action in ("info", "search", "recommend", "hot"):
            return _ok({"result": await ops_tools.video_browse(plugin, ev, action, query, "", plat)})
        return _err("action 只能是 info / search / recommend / hot / watch")

    # ── 数据 / 消息 ──────────────────────────────────────

    async def report_get(**kwargs):
        from ..services import ops_monitor

        days = request.query.get("days", "7")
        rep = await ops_monitor.report(plugin, request.query.get("platform", ""), int(days) if days.isdigit() else 7)
        return _ok({"report": rep, "text": ops_monitor.render_report(rep)})

    async def inbox_get(**kwargs):
        from ..services import ops_monitor

        view = await ops_monitor.inbox_view(plugin, request.query.get("platform", ""))
        return _ok({"inbox": view, "text": ops_monitor.render_inbox(view)})

    async def inbox_patrol(**kwargs):
        from ..services import ops_monitor

        pushed = await ops_monitor.patrol_inbox(plugin)
        targets = len(plugin.accounts.notify_targets())
        text = "\n\n".join(pushed) if pushed else "没有新消息（第一次巡检只记录现有消息，之后出现新的才推送）"
        return _ok({"pushed": pushed, "text": text + (f"\n\n已推送到 {targets} 个告警会话" if pushed and targets else "")})

    async def job(**kwargs):
        jid = request.query.get("id", "")
        j = jobs.get(jid)
        return _ok(j or {"state": "missing"})

    async def cache_get(**kwargs):
        out = {}
        for name, path in (("下载的视频", DOWNLOAD_DIR), ("看视频临时文件", os.path.join(DOWNLOAD_DIR, "browse_tmp")),
                           ("二维码/截图", IMAGE_DIR)):
            size, count = _dir_size(path)
            out[name] = {"mb": round(size / 1048576, 1), "files": count}
        cache_file = os.path.join(CREATOR_DIR, "video_context.json")
        try:
            import json

            with open(cache_file, encoding="utf-8") as f:
                out["视频概要缓存"] = {"mb": round(os.path.getsize(cache_file) / 1048576, 2), "files": len(json.load(f))}
        except (OSError, ValueError):
            out["视频概要缓存"] = {"mb": 0, "files": 0}
        return _ok(out)

    async def cache_clear(**kwargs):
        target = (await body()).get("target", "")
        if target == "downloads":
            for sub in ("dy", "bili", "direct", "browse_tmp"):
                shutil.rmtree(os.path.join(DOWNLOAD_DIR, sub), ignore_errors=True)
        elif target == "images":
            shutil.rmtree(IMAGE_DIR, ignore_errors=True)
        elif target == "summaries":
            try:
                os.remove(os.path.join(CREATOR_DIR, "video_context.json"))
            except OSError:
                pass
        else:
            return _err("target 只能是 downloads / images / summaries")
        return _ok({"message": "已清理"})

    routes = [
        ("overview", overview, "GET", "管理面板概览"),
        ("check", check, "POST", "立即巡检账号"),
        ("pool/use", pool_use, "POST", "切换账号池账号"),
        ("pending", pending, "GET", "待审批操作"),
        ("pending/approve", pending_approve, "POST", "批准待审批操作"),
        ("pending/cancel", pending_cancel, "POST", "拒绝待审批操作"),
        ("audit", audit, "GET", "操作审计日志"),
        ("settings", settings_get, "GET", "插件设置"),
        ("settings/save", settings_save, "POST", "保存插件设置"),
        ("login/bilibili", login_bili, "POST", "B站扫码登录"),
        ("login/douyin", login_douyin, "POST", "抖音扫码登录"),
        ("debug/run", debug_run, "POST", "调试：解析/搜索/看视频"),
        ("report", report_get, "GET", "运营数据报表"),
        ("inbox", inbox_get, "GET", "消息概况"),
        ("inbox/patrol", inbox_patrol, "POST", "立即巡检消息"),
        ("job", job, "GET", "后台任务状态"),
        ("cache", cache_get, "GET", "缓存占用"),
        ("cache/clear", cache_clear, "POST", "清理缓存"),
    ]
    for path, fn, method, desc in routes:
        ctx.register_web_api(f"{base}/{path}", fn, [method], desc)
    logger.info(f"[panel] 调试管理面板已开启（{len(routes)} 个接口），WebUI → 插件 → 本插件页面")


async def _debug_watch(plugin, ev, video: str) -> dict:
    """调试版看视频：返回中间过程（转写来源、挑帧方式与时间、转写节选），方便排查。"""
    from ..services.browse_service import browse_work
    from ..tools.ops_tools import _platform, _resolve

    platform = _platform("", video)
    creator, work_id = await _resolve(plugin, ev, platform, video, None)
    if not work_id:
        raise RuntimeError(f"无法识别视频: {video}")
    ctx = await creator.work_context(work_id)
    res = await browse_work(plugin, ev, platform, work_id, ctx, report_seconds=0)
    return {
        "platform": platform, "id": work_id, "title": ctx.get("title"), "up": ctx.get("author"),
        "duration": ctx.get("duration"), "summary": res.get("summary"),
        "transcript_source": res.get("transcript_source"), "transcript_len": res.get("transcript_len"),
        "transcript_excerpt": (res.get("transcript") or "")[:800],
        "frame_method": res.get("frame_method"), "frame_times": res.get("frame_times"),
        "summary_by": res.get("method"), "seconds": res.get("seconds"), "partial": res.get("partial", ""),
    }
