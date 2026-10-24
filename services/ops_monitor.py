"""
运营巡检：数据看板（每日采样 + 报表）与消息巡检（新评论 / @ / 系统通知推送给管理员）

全部是只读操作，不走审批。数据只存数字（每平台每账号每天一行），体积很小；
报表对比"现在"与 N 天前的采样，平台自带的近 7 天指标一并展示。
"""

import asyncio
import json
import os
import random
import time

from astrbot.api import logger

from ..utils.paths import CREATOR_DIR
from .creator import get_creator

STATS_FILE = os.path.join(CREATOR_DIR, "stats.jsonl")
INBOX_STATE_FILE = os.path.join(CREATOR_DIR, "inbox_state.json")
PLATFORMS = ("bilibili", "douyin")
LABEL = {"bilibili": "B站", "douyin": "抖音"}
TOTAL_LABEL = {"fans": "粉丝", "plays": "播放", "likes": "获赞", "comments": "评论", "coins": "投币",
               "favs": "收藏", "shares": "分享", "danmaku": "弹幕", "following": "关注"}
SEEN_CAP = 300


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _load_state() -> dict:
    try:
        with open(INBOX_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(CREATOR_DIR, exist_ok=True)
    tmp = INBOX_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, INBOX_STATE_FILE)


def _samples(platform: str, account: str) -> list[dict]:
    out = []
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("platform") == platform and r.get("account") == account:
                    out.append(r)
    except OSError:
        pass
    return out


async def logged_in(plugin, platform: str) -> bool:
    if platform == "bilibili":
        return bool(((await plugin.accounts.bili_cookies_for(None)) or {}).get("SESSDATA"))
    s = plugin.browser_signer
    return bool(s and s.is_ready and s.is_logged_in and not s.risk_status)


def _account(plugin, platform: str) -> str:
    return plugin.accounts.active_name(platform) or "当前账号"


# ── 数据看板 ────────────────────────────────────────────


async def sample_stats(plugin, force: bool = False) -> list[str]:
    """每个已登录平台每天记一次累计数据；返回日志行。"""
    logs = []
    for plat in PLATFORMS:
        if not await logged_in(plugin, plat):
            continue
        acc = _account(plugin, plat)
        if not force and any(r.get("date") == _today() for r in _samples(plat, acc)[-3:]):
            continue
        try:
            st = await get_creator(plugin, plat).stats()
        except Exception as e:
            logs.append(f"{LABEL[plat]}采样失败: {e}")
            continue
        _append_sample(plat, acc, st)
        logs.append(f"{LABEL[plat]}已采样")
    return logs


def _append_sample(platform: str, account: str, st: dict) -> None:
    os.makedirs(CREATOR_DIR, exist_ok=True)
    row = {"date": _today(), "ts": int(time.time()), "platform": platform, "account": account,
           "totals": st.get("totals") or {}, "period": st.get("period") or {}}
    with open(STATS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


async def report(plugin, platform: str = "", days: int = 7) -> dict:
    """实时读取数据，并与 days 天前（或最早）的采样对比。"""
    days = max(1, min(90, int(days or 7)))
    plats = [platform] if platform else list(PLATFORMS)
    out = {"days": days, "platforms": []}
    for plat in plats:
        if not await logged_in(plugin, plat):
            out["platforms"].append({"platform": plat, "error": "未登录或签名引擎未就绪"})
            continue
        acc = _account(plugin, plat)
        try:
            st = await get_creator(plugin, plat).stats()
        except Exception as e:
            out["platforms"].append({"platform": plat, "account": acc, "error": str(e)})
            continue
        hist = _samples(plat, acc)
        if not any(r.get("date") == _today() for r in hist[-3:]):
            _append_sample(plat, acc, st)  # 顺便记今天的样本
        cutoff = time.time() - days * 86400 + 3600
        base = next((r for r in reversed(hist) if r.get("ts", 0) <= cutoff), hist[0] if hist else None)
        item = {"platform": plat, "account": acc, "totals": st.get("totals"), "period": st.get("period"),
                "period_days": st.get("period_days", 7)}
        for k in ("yesterday", "diagnosis"):
            if st.get(k):
                item[k] = st[k]
        if base and base.get("date") != _today():
            item["change"] = {k: v - (base.get("totals") or {}).get(k, v) for k, v in (st.get("totals") or {}).items()
                              if isinstance(v, (int, float))}
            item["since"] = base.get("date")
        out["platforms"].append(item)
    return out


def _signed(n) -> str:
    return f"+{n}" if n > 0 else str(n)


def render_report(rep: dict) -> str:
    lines = [f"📊 运营数据（对比约 {rep['days']} 天前）"]
    for p in rep["platforms"]:
        head = f"\n【{LABEL.get(p['platform'], p['platform'])}】" + (f" {p['account']}" if p.get("account") else "")
        if p.get("error"):
            lines.append(f"{head}\n  ⚠️ {p['error']}")
            continue
        lines.append(head)
        ch = p.get("change") or {}
        tot = "  ".join(f"{TOTAL_LABEL.get(k, k)} {v}" + (f"({_signed(ch[k])})" if ch.get(k) else "")
                        for k, v in (p.get("totals") or {}).items())
        lines.append(f"  累计：{tot}")
        if p.get("since"):
            lines.append(f"  （括号内为 {p['since']} 以来的变化）")
        elif not p.get("change"):
            lines.append("  （今天开始记录，明天起可看变化）")
        if p.get("period"):
            lines.append(f"  近{p.get('period_days', 7)}天：" + "  ".join(f"{k} {v}" for k, v in p["period"].items()))
        if p.get("yesterday"):
            lines.append("  昨日新增：" + "  ".join(f"{k} {v}" for k, v in p["yesterday"].items()))
        if p.get("diagnosis"):
            tips = []
            for k, v in p["diagnosis"].items():
                own, sim = v.get("own", 0), v.get("similar", 0)
                fmt = (lambda x: f"{x * 100:.1f}%") if k in ("互动率", "完播率") else (lambda x: f"{x:g}")
                mark = "↓" if own < sim else "↑" if own > sim else "="
                tips.append(f"{k} {fmt(own)}{mark}同类 {fmt(sim)}")
            lines.append("  同类作者对比：" + "  ".join(tips))
    return "\n".join(lines)


# ── 消息巡检 ────────────────────────────────────────────


def _day(t) -> str:
    """消息时间统一成日期：B站系统通知是字符串，其余是秒级时间戳。"""
    if isinstance(t, str):
        return t[:10]
    return time.strftime("%Y-%m-%d", time.localtime(t)) if t else ""


def _clip(s: str, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


async def patrol_inbox(plugin, notify: bool = True) -> list[str]:
    """拉取各平台消息，只推送上次巡检之后的新评论/@/系统通知；首次只建立基线不推送。"""
    state = _load_state()
    pushed = []
    for plat in PLATFORMS:
        if not await logged_in(plugin, plat):
            continue
        acc = _account(plugin, plat)
        key = f"{plat}:{acc}"
        try:
            box = await get_creator(plugin, plat).inbox()
        except Exception as e:
            logger.info(f"[monitor] {LABEL[plat]}消息巡检失败: {e}")
            continue
        seen = state.get(key)
        ids = [f"c:{c['id']}" for c in box.get("comments", [])] + [f"n:{n['id']}" for n in box.get("notices", [])]
        if seen is None:
            state[key] = ids[-SEEN_CAP:]
            continue
        seen_set = set(seen)
        new_c = [c for c in box.get("comments", []) if f"c:{c['id']}" not in seen_set]
        new_n = [n for n in box.get("notices", []) if f"n:{n['id']}" not in seen_set]
        state[key] = (seen + [i for i in ids if i not in seen_set])[-SEEN_CAP:]
        if not (new_c or new_n):
            continue
        lines = [f"📬 {LABEL[plat]}「{acc}」有新消息："]
        for c in new_c[:8]:
            where = f"《{_clip(c.get('title'), 16)}》" if c.get("title") else ""
            lines.append(f"· [{_day(c.get('time'))}] {c.get('kind', '评论')} {c.get('user', '')}{where}：{_clip(c.get('text'), 60)}")
        if len(new_c) > 8:
            lines.append(f"  …另有 {len(new_c) - 8} 条")
        for n in new_n[:3]:
            lines.append(f"· [{_day(n.get('time'))}] 🔔 {_clip(n.get('title'), 30)}：{_clip(n.get('content'), 80)}")
        if new_c:
            lines.append("需要回复可以对我说“帮我回复新评论”，或用 /va_reply")
        pushed.append("\n".join(lines))
    _save_state(state)
    if notify and pushed:
        await plugin.accounts.notify("\n\n".join(pushed))
    return pushed


async def inbox_view(plugin, platform: str = "") -> dict:
    """当前消息概况（不改变巡检基线），给命令和 LLM 工具用。"""
    out = []
    for plat in [platform] if platform else PLATFORMS:
        if not await logged_in(plugin, plat):
            out.append({"platform": plat, "error": "未登录或签名引擎未就绪"})
            continue
        try:
            box = await get_creator(plugin, plat).inbox()
        except Exception as e:
            out.append({"platform": plat, "error": str(e)})
            continue
        out.append({
            "platform": plat, "account": _account(plugin, plat), "unread": box.get("unread") or {},
            "today": _today(),
            "comments": [{"date": _day(c.get("time")), "kind": c.get("kind"), "user": c.get("user"),
                          "title": _clip(c.get("title"), 20), "text": _clip(c.get("text"), 60)}
                         for c in box.get("comments", [])[:5]],
            "notices": [{"date": _day(n.get("time")), "title": _clip(n.get("title"), 30), "content": _clip(n.get("content"), 60)}
                        for n in box.get("notices", [])[:3]],
        })
    return {"inbox": out}


def render_inbox(view: dict) -> str:
    lines = ["📬 消息概况"]
    for p in view["inbox"]:
        lines.append(f"\n【{LABEL.get(p['platform'], p['platform'])}】" + (f" {p['account']}" if p.get("account") else ""))
        if p.get("error"):
            lines.append(f"  ⚠️ {p['error']}")
            continue
        lines.append("  未读：" + ("  ".join(f"{k} {v}" for k, v in p["unread"].items()) or "无"))
        for c in p["comments"]:
            where = f"《{c['title']}》" if c.get("title") else ""
            lines.append(f"  · [{c['date']}] {c['kind']} {c['user']}{where}：{c['text']}")
        for n in p["notices"]:
            lines.append(f"  · [{n['date']}] 🔔 {n['title']}：{n['content']}")
    return "\n".join(lines)


# ── 定时任务 ────────────────────────────────────────────


class OpsMonitor:
    def __init__(self, plugin):
        self.plugin = plugin
        self.interval_min = int(plugin.config.get("inbox_patrol_minutes", 30) or 0)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self.interval_min > 0 and not self._task:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(120)  # 错开账号巡检和签名引擎启动
        while True:
            try:
                for msg in await sample_stats(self.plugin):
                    logger.info(f"[monitor] {msg}")
                if self.plugin.accounts.notify_targets():
                    await patrol_inbox(self.plugin)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[monitor] 巡检异常: {e}")
            # 加一点随机，避免每次都在整点打开页面
            await asyncio.sleep(self.interval_min * 60 * random.uniform(0.85, 1.15))
