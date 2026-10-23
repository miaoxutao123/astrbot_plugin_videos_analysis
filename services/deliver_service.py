"""
下载视频并发给用户（命令 /va_download 与 LLM 工具共用）

- B站：按 bili_quality 下载（开启 bili_use_login 且已登录时可下高清），超过 max_video_size 改发文件
- 抖音：解析后下载视频 / 图文 / 多段视频，图文按图片发送
- 同一用户 30 秒内只能触发一次，避免被刷；下载文件按 delete_time 由插件统一清理
"""

import os
import re
import time

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain

from ..utils.config_helper import DOWNLOAD_DIR_DY, cleanup_old_files, send_file_if_needed
from . import bilibili_service
from .douyin_service import download as douyin_download

_last: dict[str, float] = {}
COOLDOWN = 30
DOUYIN_RE = re.compile(r"https?://(?:v\.douyin\.com/[\w\-]+/?|www\.douyin\.com/(?:video|note)/\d+)")


async def _media(plugin, path: str, kind: str):
    nap = await send_file_if_needed(path, plugin.nap_server_address, plugin.nap_server_port)
    if kind == "image":
        return Comp.Image.fromFileSystem(nap)
    if os.path.getsize(path) / 1048576 > plugin.max_video_size:
        return Comp.File(file=nap, name=os.path.basename(path))
    return Comp.Video.fromFileSystem(nap)


async def deliver(plugin, event, ref: str) -> dict:
    """解析 ref（链接 / BV / av / 序号已在调用方换成 ID）→ 下载 → 发到当前会话。"""
    key = f"{event.unified_msg_origin}|{event.get_sender_id()}"
    if not event.is_admin() and time.time() - _last.get(key, 0) < COOLDOWN:
        return {"error": f"下载太频繁了，请 {COOLDOWN} 秒后再试"}
    _last[key] = time.time()

    if DOUYIN_RE.search(ref):
        return await _deliver_douyin(plugin, event, DOUYIN_RE.search(ref).group(0))
    if re.search(r"BV1\w{9}|av\d+|b23\.tv|bili2233\.cn|bilibili\.com", ref, re.I):
        return await _deliver_bili(plugin, event, ref)
    return {"error": f"不支持的链接：{ref[:80]}（目前支持B站、抖音）"}


async def _deliver_bili(plugin, event, ref: str) -> dict:
    await cleanup_old_files(bilibili_service.BILI_DOWNLOAD_DIR, plugin.delete_time)
    res = await bilibili_service.process_bili_video(
        ref, download_flag=True, quality=plugin.bili_quality, use_login=plugin.bili_use_login,
    )
    if not res or not res.get("video_path") or not os.path.exists(res["video_path"]):
        return {"error": "B站视频下载失败"}
    path = res["video_path"]
    size = os.path.getsize(path) / 1048576
    await event.send(MessageChain([Comp.Plain(f"📺 {res.get('title', '')}（{size:.1f}MB）"), await _media(plugin, path, "video")]))
    return {"status": "sent", "platform": "bilibili", "title": res.get("title"), "bvid": res.get("bvid"),
            "size_mb": round(size, 1), "as_file": size > plugin.max_video_size}


async def _deliver_douyin(plugin, event, url: str) -> dict:
    await cleanup_old_files(DOWNLOAD_DIR_DY, plugin.delete_time)
    info = await plugin.accounts.parse_douyin(url)
    if not info or info.get("error"):
        return {"error": f"抖音解析失败: {(info or {}).get('error', '未知错误')}"}
    urls = info.get("media_urls") or []
    if not urls:
        return {"error": "没有可下载的媒体"}
    os.makedirs(DOWNLOAD_DIR_DY, exist_ok=True)
    chain, sent = [Comp.Plain(f"🎵 {(info.get('desc') or '')[:60]}")], 0
    for i, u in enumerate(urls[:9]):
        is_img = info.get("type") == "image" or u.split("?")[0].endswith((".jpg", ".jpeg", ".webp", ".png"))
        path = os.path.join(DOWNLOAD_DIR_DY, f"{info.get('aweme_id', 'dy')}_{i}.{'jpg' if is_img else 'mp4'}")
        try:
            ok = await douyin_download(u, path, plugin.effective_douyin_cookie)
        except Exception as e:
            logger.warning(f"[deliver] 抖音第 {i + 1} 个文件下载失败: {e}")
            ok = False
        if ok and os.path.exists(path):
            chain.append(await _media(plugin, path, "image" if is_img else "video"))
            sent += 1
    if not sent:
        return {"error": "抖音媒体下载失败"}
    await event.send(MessageChain(chain))
    return {"status": "sent", "platform": "douyin", "type": info.get("type"), "files": sent, "desc": (info.get("desc") or "")[:60]}
