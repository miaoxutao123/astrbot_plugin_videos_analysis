"""
账号管理：统一查看/注销各平台登录态与 AI 服务配置

覆盖：抖音（浏览器签名 + cookie）、B站（扫码 cookie）、Gemini、MiMo。
- 账号池：同一平台保存多个登录快照，遇到 403/风控连续失败时自动轮换
- 定时巡检：后台任务定期深度检查，状态变化（正常↔异常）时推送给管理员会话
handler 只负责把 AccountStatus 渲染成消息。
"""

import asyncio
import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import unquote

import aiohttp

from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain

from ..douyin_scraper.cookie_extractor import extract_douyin_cookies
from ..utils.paths import (
    BILI_COOKIE_FILE,
    DOUYIN_BROWSER_PROFILE,
    DOUYIN_COOKIE_FILE,
    HEALTH_FILE,
    POOL_FILE,
)
from . import bili_account, bilibili_service
from .gemini_service import test_gemini

PLATFORMS = ("douyin", "bilibili", "gemini", "mimo")
POOL_PLATFORMS = ("douyin", "bilibili")
EXPIRY_WARN_DAYS = 5


@dataclass
class AccountStatus:
    platform: str
    ok: bool
    summary: str
    details: dict = field(default_factory=dict)
    checked_at: float = field(default_factory=time.time)
    warn: bool = False  # 可用但需要关注（如登录即将过期）

    @property
    def state(self) -> str:
        return "bad" if not self.ok else "warn" if self.warn else "ok"

    def render(self) -> str:
        icon = {"ok": "✅", "warn": "🟡", "bad": "⚠️"}[self.state]
        lines = [f"{icon} {self.platform}: {self.summary}"]
        lines += [f"    · {k}: {v}" for k, v in self.details.items() if v not in (None, "")]
        return "\n".join(lines)


def _load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _fmt_expiry(ts: float | None) -> tuple[str, bool]:
    """返回 (展示文本, 是否即将过期)。"""
    if not ts or ts <= 0:
        return "", False
    left = ts - time.time()
    text = time.strftime("%Y-%m-%d", time.localtime(ts)) + (f"（剩 {left / 86400:.0f} 天）" if left > 0 else "（已过期）")
    return text, left < EXPIRY_WARN_DAYS * 86400


def _bili_expiry(cookies: dict) -> float | None:
    # SESSDATA 形如 "xxx%2C1745000000%2Cabc*"，第二段是过期时间戳
    parts = unquote(str(cookies.get("SESSDATA", ""))).split(",")
    return float(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None


class AccountManager:
    def __init__(self, plugin):
        self.plugin = plugin
        cfg = plugin.config
        self.check_interval_min = int(cfg.get("health_check_interval_minutes", 360) or 0)
        self.rotate_threshold = max(1, int(cfg.get("rotate_fail_threshold", 2)))
        self.rotate_cooldown = int(cfg.get("rotate_cooldown_minutes", 30)) * 60
        self._pool = _load_json(POOL_FILE, {"accounts": {}, "active": {}})
        self._health = _load_json(HEALTH_FILE, {"notify": [], "last": {}})
        self._fails: dict[str, int] = {}
        self._last_fail: dict[str, float] = {}
        self._rotate_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        bilibili_service.FAILURE_HOOK = lambda reason: self.report_failure("bilibili", reason)

    # ── 状态查询 ─────────────────────────────────────────

    async def status(self, platform: str, deep: bool = False) -> AccountStatus:
        checker = {
            "douyin": self._douyin,
            "bilibili": self._bilibili,
            "gemini": self._gemini,
            "mimo": self._mimo,
        }[platform]
        try:
            st = await checker(deep)
        except Exception as e:
            logger.warning(f"[accounts] 检查 {platform} 失败: {e}")
            st = AccountStatus(platform, False, f"检查失败: {e}")
        active = self.active_name(platform)
        if active:
            st.details["账号池"] = f"当前 {active}（共 {len(self._accounts(platform))} 个）"
        return st

    async def status_all(self, deep: bool = False) -> list[AccountStatus]:
        return [await self.status(p, deep) for p in PLATFORMS]

    async def _douyin(self, deep: bool) -> AccountStatus:
        p = self.plugin
        signer = p.browser_signer
        cookie = p.effective_douyin_cookie
        source = (
            "扫码/手动注入" if p._douyin_cookie_from_scan
            else "cookie 文件" if p._douyin_cookie_from_file
            else "插件配置" if p._douyin_cookie_from_config
            else "无"
        )
        _, has_login_fields, _ = extract_douyin_cookies(cookie) if cookie else ({}, False, [])
        signer_state = (
            "已就绪" if signer and signer.is_ready
            else "已禁用" if not p.douyin_browser_signer_enabled
            else f"未就绪 ({p.browser_signer_error or '未知原因'})"
        )
        logged_in = bool(signer and signer.is_logged_in) or has_login_fields
        ok = bool(signer and signer.is_ready)
        details = {"签名引擎": signer_state, "cookie 来源": source}
        warn = False
        if signer and signer.is_ready:
            sess = next((c for c in await signer.export_cookies() if c["name"] == "sessionid"), None)
            if sess:
                details["登录过期"], warn = _fmt_expiry(sess.get("expires"))
            if signer.risk_status:
                details["风控"], warn = signer.risk_status, True
        from .creator.human import GUARD, OP_LABEL

        details["今日写操作"] = " · ".join(f"{OP_LABEL.get(op, op)} {n}" for op, n in GUARD.usage("douyin").items())
        if deep and ok and logged_in:
            from .creator import get_creator

            try:
                creator_ok, msg = await get_creator(p, "douyin").check_login()
            except Exception as e:
                creator_ok, msg = False, f"检查失败: {str(e).splitlines()[0][:100]}"
            details["创作者中心"] = msg
            if not creator_ok:
                logged_in, warn = False, True
        summary = ("可解析" if ok else "无法解析（纯 Python 签名已失效，需要浏览器签名引擎）") + ("，已登录" if logged_in else "，未登录")
        return AccountStatus("douyin", ok, summary, details, warn=warn)

    async def _bilibili(self, deep: bool) -> AccountStatus:
        cookies = await bilibili_service.load_cookies()
        if not cookies:
            return AccountStatus("bilibili", True, "未登录（可解析，最高 720P）", {"登录": "管理员发送 /bili_login 扫码"})
        info = await bilibili_service.get_login_info(cookies)
        if info and info.get("isLogin"):
            vip = info.get("vipStatus") == 1
            expiry, warn = _fmt_expiry(_bili_expiry(cookies))
            details = {
                "mid": info.get("mid"), "大会员": "是" if vip else "否", "登录过期": expiry,
                "自动续期": "可用" if cookies.get(bili_account.REFRESH_KEY) else "不可用（无 refresh_token，需重新 /bili_login）",
                "使用登录态": "是" if self.plugin.bili_use_login else "否（bili_use_login 未开启）",
            }
            if deep:
                details.update(await self._bili_pool_health())
            return AccountStatus("bilibili", True, f"已登录: {info.get('uname')}", details, warn=warn)
        return AccountStatus("bilibili", False, "cookie 已失效", {"处理": "发送 /bili_login 重新扫码"})

    # ── B站多账号凭据 ─────────────────────────────────────

    async def bili_cookies_for(self, name: str | None = None) -> dict | None:
        """取指定账号的 cookie；不指定或就是当前账号时用当前登录态（cookie 文件）。"""
        if name and name != self.active_name("bilibili"):
            acc = self._find("bilibili", name)
            if not acc:
                raise RuntimeError(f"B站账号池里没有「{name}」，可用: {', '.join(a['name'] for a in self._accounts('bilibili')) or '无'}")
            return dict(acc["secret"])
        return await bilibili_service.load_cookies()

    async def save_bili_cookies_for(self, name: str | None, cookies: dict) -> None:
        """写回 cookie（续期、补 ticket 后），当前账号同时写 cookie 文件。"""
        active = self.active_name("bilibili")
        target = name or active
        acc = self._find("bilibili", target) if target else None
        if acc:
            acc.update(secret=cookies, saved_at=time.time(), valid=True)
            self._save_pool()
        if not name or name == active:
            await bilibili_service.save_cookies_dict(cookies)

    async def refresh_bilibili(self, force: bool = False, name: str | None = None) -> str:
        """按需续期一个 B站账号（默认当前账号），成功后写回 cookie 文件/账号池。"""
        cookies = await self.bili_cookies_for(name)
        label = name or self.active_name("bilibili") or "当前账号"
        if not cookies:
            return "B站未登录，先 /bili_login 扫码"
        new, msg = await bili_account.refresh_cookies(cookies, force=force)
        if not new:
            return f"{label}: {msg}"
        await self.save_bili_cookies_for(name, new)
        expiry, _ = _fmt_expiry(_bili_expiry(new))
        logger.info(f"[accounts] B站「{label}」cookie 已续期，新过期时间 {expiry}")
        return f"{label}: B站 cookie 已续期，新过期时间 {expiry}"

    async def refresh_all_bilibili(self, force: bool = False) -> list[str]:
        """当前账号 + 账号池里每个 B站账号都检查一遍续期（按 DedeUserID 去重）。"""
        results, seen = [], set()
        targets = [None] + [a["name"] for a in self._accounts("bilibili") if a["name"] != self.active_name("bilibili")]
        for name in targets:
            try:
                cookies = await self.bili_cookies_for(name)
            except Exception as e:
                results.append(f"{name}: {e}")
                continue
            uid = (cookies or {}).get("DedeUserID")
            if not uid or uid in seen:
                continue
            seen.add(uid)
            results.append(await self.refresh_bilibili(force=force, name=name))
        return results

    # ── 风控熔断（按账号） ───────────────────────────────

    def _risk_key(self, platform: str, account: str | None) -> str:
        return f"{platform}:{account or self.active_name(platform) or '_active'}"

    def write_blocked(self, platform: str, account: str | None = None) -> str:
        """该账号写操作是否处于风控冷却中，返回原因（空串表示可写）。"""
        rec = self._health.setdefault("risk", {}).get(self._risk_key(platform, account))
        if rec and rec.get("until", 0) > time.time():
            left = int(rec["until"] - time.time()) // 60 + 1
            return f"账号触发风控（{rec.get('reason', '')}），写操作暂停约 {left} 分钟"
        return ""

    async def trip_risk(self, platform: str, account: str | None, reason: str) -> None:
        minutes = int(self.plugin.config.get("risk_cooldown_minutes", 60) or 60)
        key = self._risk_key(platform, account)
        self._health.setdefault("risk", {})[key] = {"until": time.time() + minutes * 60, "reason": reason[:120]}
        _save_json(HEALTH_FILE, self._health)
        await self.notify(f"[视频解析] {key} 触发风控：{reason[:120]}，该账号写操作暂停 {minutes} 分钟（读操作不受影响）")

    async def _bili_pool_health(self) -> dict:
        """检查池里非当前的 B站账号是否还有效（便宜：一次 nav 请求/账号）。"""
        out = {}
        active = self.active_name("bilibili")
        for acc in self._accounts("bilibili"):
            if acc["name"] == active:
                continue
            info = await bilibili_service.get_login_info(acc["secret"])
            acc["valid"] = bool(info and info.get("isLogin"))
            out[f"池·{acc['name']}"] = "有效" if acc["valid"] else "已失效"
        self._save_pool()
        return out

    async def _gemini(self, deep: bool) -> AccountStatus:
        from ..utils.config_helper import resolve_gemini_config

        gcfg = resolve_gemini_config(self.plugin)
        if not gcfg:
            return AccountStatus("gemini", False, "未配置", {"处理": "填写 gemini_api_key / gemini_base_url"})
        details = {"来源": gcfg.source, "地址": gcfg.root, "模型": gcfg.model, "key": f"***{gcfg.api_key[-4:]}"}
        if not deep:
            return AccountStatus("gemini", True, "已配置（加 deep 参数可测试连通性）", details)
        ok, msg = await test_gemini(gcfg)
        details["测试"] = msg
        return AccountStatus("gemini", ok, "连通正常" if ok else "调用失败", details)

    async def _mimo(self, deep: bool) -> AccountStatus:
        p = self.plugin
        if not p.mimo_api_key:
            return AccountStatus("mimo", True, "未配置（可选）")
        details = {"地址": p.mimo_api_base, "模型": p.mimo_model, "key": f"***{p.mimo_api_key[-4:]}"}
        if deep:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                    async with s.get(f"{p.mimo_api_base.rstrip('/')}/models", headers={"Authorization": f"Bearer {p.mimo_api_key}"}) as r:
                        details["测试"] = f"HTTP {r.status}"
                        return AccountStatus("mimo", r.status == 200, "连通正常" if r.status == 200 else "调用失败", details)
            except Exception as e:
                details["测试"] = str(e)[:120]
                return AccountStatus("mimo", False, "调用失败", details)
        return AccountStatus("mimo", True, "已配置", details)

    # ── 注销 ─────────────────────────────────────────────

    async def logout(self, platform: str) -> str:
        p = self.plugin
        if platform == "bilibili":
            if os.path.exists(BILI_COOKIE_FILE):
                os.remove(BILI_COOKIE_FILE)
            self._pool["active"].pop("bilibili", None)
            self._save_pool()
            return "已清除 B站 登录 cookie（账号池里保存的快照不受影响）。"
        if platform == "douyin":
            if p.browser_signer:
                await p.browser_signer.stop(save=False)
                p._browser_signer = None
            for path in (DOUYIN_COOKIE_FILE, DOUYIN_BROWSER_PROFILE):
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                elif os.path.exists(path):
                    os.remove(path)
            p._douyin_cookie_from_scan = None
            p._douyin_cookie_from_file = None
            self._pool["active"].pop("douyin", None)
            self._save_pool()
            await p.start_browser_signer()
            return "已清除抖音登录态并重启签名引擎（插件配置里的 doyin_cookie 与账号池快照不受影响）。"
        return f"不支持注销 {platform}，可选: douyin / bilibili"

    # ── 账号池 ───────────────────────────────────────────

    def _accounts(self, platform: str) -> list[dict]:
        return self._pool["accounts"].setdefault(platform, [])

    def _find(self, platform: str, name: str) -> dict | None:
        return next((a for a in self._accounts(platform) if a["name"] == name), None)

    def _save_pool(self) -> None:
        _save_json(POOL_FILE, self._pool)

    def active_name(self, platform: str) -> str:
        return self._pool["active"].get(platform, "")

    def pool_list(self, platform: str | None = None) -> dict[str, list[dict]]:
        """账号池概览（不含凭据），给命令和 WebUI 用。"""
        now = time.time()
        out = {}
        for plat in ([platform] if platform else POOL_PLATFORMS):
            out[plat] = [
                {
                    "name": a["name"],
                    "active": a["name"] == self.active_name(plat),
                    "saved_at": a.get("saved_at"),
                    "fails": a.get("fails", 0),
                    "cooldown_left": max(0, int(a.get("cooldown_until", 0) - now)),
                    "last_error": a.get("last_error", ""),
                    "valid": a.get("valid"),
                }
                for a in self._accounts(plat)
            ]
        return out

    async def _current_secret(self, platform: str):
        if platform == "douyin":
            signer = self.plugin.browser_signer
            if not signer or not signer.is_ready:
                raise RuntimeError("抖音签名引擎未就绪，无法读取当前登录态")
            if not signer.is_logged_in:
                raise RuntimeError("抖音当前未登录，先 /dy_login 扫码")
            return await signer.export_cookies()
        cookies = await bilibili_service.load_cookies()
        if not cookies:
            raise RuntimeError("B站当前未登录，先 /bili_login 扫码")
        return cookies

    async def _apply_secret(self, platform: str, secret) -> None:
        if platform == "douyin":
            signer = self.plugin.browser_signer
            if not signer or not signer.is_ready:
                raise RuntimeError("抖音签名引擎未就绪")
            await signer.replace_cookies(secret)
            self.plugin._douyin_cookie_from_scan = await signer.get_cookie_string()
        else:
            await bilibili_service.save_cookies_dict(secret)

    async def pool_save(self, platform: str, name: str) -> str:
        """把当前登录态存为账号池里的一个命名账号（已存在则覆盖）。"""
        secret = await self._current_secret(platform)
        acc = self._find(platform, name)
        if acc is None:
            acc = {"name": name}
            self._accounts(platform).append(acc)
        acc.update(secret=secret, saved_at=time.time(), fails=0, cooldown_until=0, last_error="", valid=True)
        self._pool["active"][platform] = name
        self._save_pool()
        return f"已把当前 {platform} 登录态保存为「{name}」，账号池共 {len(self._accounts(platform))} 个。"

    async def pool_use(self, platform: str, name: str) -> str:
        acc = self._find(platform, name)
        if not acc:
            return f"账号池里没有 {platform}/{name}"
        await self._apply_secret(platform, acc["secret"])
        acc.update(fails=0, cooldown_until=0)
        self._pool["active"][platform] = name
        self._fails[platform] = 0
        self._save_pool()
        return f"已切换 {platform} 到「{name}」。"

    def pool_delete(self, platform: str, name: str) -> str:
        acc = self._find(platform, name)
        if not acc:
            return f"账号池里没有 {platform}/{name}"
        self._accounts(platform).remove(acc)
        if self.active_name(platform) == name:
            self._pool["active"].pop(platform, None)
        self._save_pool()
        return f"已从账号池删除 {platform}/{name}（当前登录态不变）。"

    # ── 失败上报与自动轮换 ───────────────────────────────

    def report_success(self, platform: str) -> None:
        if self._fails.get(platform):
            self._fails[platform] = 0
            acc = self._find(platform, self.active_name(platform))
            if acc and acc.get("fails"):
                acc["fails"] = 0
                self._save_pool()

    async def report_failure(self, platform: str, reason: str) -> str | None:
        """记录一次风控/403 类失败；连续失败达到阈值且池里有可用账号时轮换，返回新账号名。"""
        # 只算"连续"失败：距上次失败超过 10 分钟就重新计数
        if time.time() - self._last_fail.get(platform, 0) > 600:
            self._fails[platform] = 0
        self._last_fail[platform] = time.time()
        self._fails[platform] = self._fails.get(platform, 0) + 1
        logger.info(f"[accounts] {platform} 失败 {self._fails[platform]}/{self.rotate_threshold}: {reason}")
        if self._fails[platform] < self.rotate_threshold:
            return None
        async with self._rotate_lock:
            if self._fails.get(platform, 0) < self.rotate_threshold:
                return None  # 等锁期间别的请求已经轮换过了
            accounts = self._accounts(platform)
            current = self.active_name(platform)
            cur = self._find(platform, current)
            if cur:
                cur.update(fails=cur.get("fails", 0) + 1, last_error=reason[:200], cooldown_until=time.time() + self.rotate_cooldown)
            now = time.time()
            start = accounts.index(cur) + 1 if cur in accounts else 0
            candidates = [a for a in accounts[start:] + accounts[:start] if a is not cur and a.get("cooldown_until", 0) <= now and a.get("valid") is not False]
            self._save_pool()
            if not candidates:
                logger.warning(f"[accounts] {platform} 连续失败但账号池无可用账号可切换")
                return None
            nxt = candidates[0]
            try:
                await self.pool_use(platform, nxt["name"])
            except Exception as e:
                logger.warning(f"[accounts] 切换到 {nxt['name']} 失败: {e}")
                return None
        await self.notify(f"[视频解析] {platform} 连续失败（{reason[:80]}），已自动从「{current or '未命名'}」切换到「{nxt['name']}」")
        return nxt["name"]

    async def parse_douyin(self, url: str) -> dict:
        """抖音解析统一入口：失败上报，轮换账号后重试一次。"""
        result = await self.plugin.make_douyin_parser().parse(url)
        if result.get("error") == "Failed to fetch video data":
            if await self.report_failure("douyin", result.get("details", "空响应/风控")):
                result = await self.plugin.make_douyin_parser().parse(url)
        elif not result.get("error"):
            self.report_success("douyin")
        return result

    # ── 定时巡检与告警 ───────────────────────────────────

    def notify_targets(self) -> list[str]:
        return list(self._health.get("notify", []))

    def set_notify(self, umo: str, enable: bool) -> str:
        targets = self._health.setdefault("notify", [])
        if enable and umo not in targets:
            targets.append(umo)
        elif not enable and umo in targets:
            targets.remove(umo)
        _save_json(HEALTH_FILE, self._health)
        return f"{'已' if enable else '已取消'}把本会话设为账号告警接收方（当前 {len(targets)} 个）。"

    async def notify(self, text: str) -> None:
        for umo in self.notify_targets():
            try:
                await self.plugin.context.send_message(umo, MessageChain().message(text))
            except Exception as e:
                logger.warning(f"[accounts] 推送告警到 {umo} 失败: {e}")

    def last_health(self) -> dict:
        return self._health.get("last", {})

    async def run_health_check(self, notify: bool = True) -> tuple[list[AccountStatus], list[str]]:
        """深度检查所有平台，与上次结果对比；状态变化时推送（仅变化时，不重复刷屏）。"""
        try:
            for msg in await self.refresh_all_bilibili():
                if "已续期" in msg or "失败" in msg:
                    logger.info(f"[accounts] {msg}")
        except Exception as e:
            logger.warning(f"[accounts] B站自动续期异常: {e}")
        statuses = await self.status_all(deep=True)
        last = self._health.setdefault("last", {})
        changes = []
        for st in statuses:
            prev = last.get(st.platform, {}).get("state")
            if prev and prev != st.state:
                verb = "已恢复" if st.state == "ok" else "需要关注" if st.state == "warn" else "异常"
                changes.append(f"{verb} ← 之前 {prev}\n{st.render()}")
            last[st.platform] = {**asdict(st), "state": st.state}
        self._health["checked_at"] = time.time()
        _save_json(HEALTH_FILE, self._health)
        if notify and changes:
            await self.notify("[视频解析] 账号巡检发现状态变化：\n\n" + "\n\n".join(changes))
        return statuses, changes

    def start_scheduler(self) -> None:
        if self.check_interval_min > 0 and not self._task:
            self._task = asyncio.create_task(self._loop())

    async def stop_scheduler(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(60)  # 等签名引擎等启动完再做第一次检查
        while True:
            try:
                await self.run_health_check()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[accounts] 定时巡检异常: {e}")
            await asyncio.sleep(self.check_interval_min * 60)
