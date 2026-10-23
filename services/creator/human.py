"""
创作者操作的"像人一样"交互 + 写操作频率闸门（各平台通用）

平台风控除了看设备指纹，也看操作行为：瞬间填满输入框（没有按键事件）、
0ms 间隔连续打字、鼠标不移动直接点中元素正中心、短时间内连续发布/删除，都是典型的脚本特征。
"""

import asyncio
import json
import os
import random
import time

from ...utils.paths import DATA_DIR

# 每个平台每类写操作：每天上限、两次之间的最小间隔（秒）
WRITE_LIMITS = {
    "douyin": {
        "upload": {"daily": 3, "interval": 600},
        "delete": {"daily": 10, "interval": 60},
        "reply": {"daily": 30, "interval": 20},
    },
    "bilibili": {
        "reply": {"daily": 30, "interval": 20},
        "comment": {"daily": 10, "interval": 60},
        "watch": {"daily": 300, "interval": 3},
        "profile": {"daily": 5, "interval": 60},
    },
}
OP_LABEL = {"upload": "发布", "delete": "删除", "reply": "回复评论", "comment": "发评论", "watch": "观看上报", "profile": "修改资料"}
# 间隔不足但差得不多时直接等，超过这个时长才拒绝
MAX_WAIT = 90
QUOTA_FILE = os.path.join(DATA_DIR, "creator_quota.json")


async def pause(lo: float = 0.4, hi: float = 1.2) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


async def _move_to(page, locator) -> tuple[float, float] | None:
    """滚到可见处，鼠标分几步移到元素内的随机位置；拿不到位置返回 None。"""
    await locator.scroll_into_view_if_needed()
    box = await locator.bounding_box()
    if not box:
        return None
    x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
    y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
    await page.mouse.move(x, y, steps=random.randint(8, 20))
    return x, y


async def human_hover(page, locator) -> None:
    if not await _move_to(page, locator):
        await locator.hover()


async def human_click(page, locator) -> None:
    """鼠标移过去，停顿一下再按下/松开（不瞬移到元素正中心）。"""
    pos = await _move_to(page, locator)
    if not pos:
        await locator.click()
        return
    await pause(0.1, 0.35)
    await page.mouse.click(*pos, delay=random.randint(40, 120))


async def human_type(page, text: str) -> None:
    """逐字输入，字间 50~180ms，偶尔停顿一下。"""
    for ch in text:
        await page.keyboard.type(ch)
        await asyncio.sleep(random.uniform(0.05, 0.18))
        if random.random() < 0.05:
            await pause(0.3, 0.8)


async def human_fill(page, locator, text: str) -> None:
    """点进输入框、清空、再逐字输入（代替 locator.fill 的瞬间赋值）。"""
    await human_click(page, locator)
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await pause(0.2, 0.5)
    await human_type(page, text)


def _today(ts: float) -> str:
    # 按北京时间算"每天"
    return time.strftime("%Y-%m-%d", time.gmtime(ts + 8 * 3600))


class WriteGuard:
    """写操作频率闸门，计数持久化到 plugin_data，插件重载/容器重启后仍然有效。"""

    def __init__(self, path: str = QUOTA_FILE):
        self.path = path
        self._lock = asyncio.Lock()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    async def acquire(self, platform: str, op: str) -> str:
        """
        检查并占用一次写操作额度。间隔不足 MAX_WAIT 秒时等待；
        超出每日上限或需要等太久时返回拒绝原因，允许执行返回空串。
        """
        limit = WRITE_LIMITS.get(platform, {}).get(op)
        if not limit:
            return ""
        async with self._lock:
            data = self._load()
            rec = data.setdefault(platform, {}).setdefault(op, {})
            now = time.time()
            if rec.get("day") != _today(now):
                rec.update(day=_today(now), count=0)
            label = OP_LABEL.get(op, op)
            if rec["count"] >= limit["daily"]:
                return f"今天的{label}次数已达上限（{limit['daily']} 次），明天再试"
            wait = rec.get("last", 0) + limit["interval"] * random.uniform(1.0, 1.5) - now
            if wait > MAX_WAIT:
                return f"距上次{label}太近，请约 {int(wait) // 60 + 1} 分钟后再试"
            if wait > 0:
                await asyncio.sleep(wait)
            rec["count"] += 1
            rec["last"] = time.time()
            self._save(data)
            return ""

    def usage(self, platform: str) -> dict:
        data = self._load().get(platform, {})
        today = _today(time.time())
        return {
            op: f"{(data.get(op) or {}).get('count', 0) if (data.get(op) or {}).get('day') == today else 0}/{lim['daily']}"
            for op, lim in WRITE_LIMITS.get(platform, {}).items()
        }


GUARD = WriteGuard()
