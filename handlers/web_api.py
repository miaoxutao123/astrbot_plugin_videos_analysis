"""
WebUI 插件页（pages/accounts）的后端接口

外部路径：/api/v1/plugins/extensions/astrbot_plugin_videos_analysis/<route>，需 WebUI 登录。
返回内容不含任何 cookie / key 明文。
"""

import time
from dataclasses import asdict

from astrbot.api.web import request

from ..services.account_manager import PLATFORMS
from ..utils.paths import PLUGIN_NAME


def _ok(data: dict) -> dict:
    return {"status": "ok", "message": None, "data": data}


def _err(msg: str) -> dict:
    return {"status": "error", "message": msg, "data": {}}


def register_web_apis(plugin) -> None:
    am, ops = plugin.accounts, plugin.creator_ops

    async def overview(**kwargs):
        live = [asdict(st) | {"state": st.state} for st in await am.status_all(deep=False)]
        return _ok({
            "live": live,
            "last_check": am.last_health(),
            "last_check_at": am._health.get("checked_at"),
            "pool": am.pool_list(),
            "pending": ops.pending_list(),
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
        body = await request.json({}) or {}
        plat, name = body.get("platform", ""), body.get("name", "")
        if plat not in PLATFORMS or not name:
            return _err("参数错误")
        try:
            return _ok({"message": await am.pool_use(plat, name)})
        except Exception as e:
            return _err(str(e))

    async def pending_cancel(**kwargs):
        body = await request.json({}) or {}
        return _ok({"message": ops.cancel(str(body.get("code", "")))})

    base = f"/{PLUGIN_NAME}"
    ctx = plugin.context
    ctx.register_web_api(f"{base}/overview", overview, ["GET"], "账号运维概览")
    ctx.register_web_api(f"{base}/check", check, ["POST"], "立即巡检账号")
    ctx.register_web_api(f"{base}/pool/use", pool_use, ["POST"], "切换账号池账号")
    ctx.register_web_api(f"{base}/pending/cancel", pending_cancel, ["POST"], "取消待确认操作")
