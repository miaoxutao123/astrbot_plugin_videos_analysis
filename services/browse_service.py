"""
视频"浏览"：抽帧 + 语音转写 → 文字概要（不把整段视频交给多模态模型，省钱也省算力）

转写按机器压力从轻到重依次尝试：
1. 平台自带字幕（B站 CC / AI 字幕）——零算力
2. 线上 ASR（OpenAI 兼容 /audio/transcriptions，如 SiliconFlow 免费的 SenseVoiceSmall）
3. AstrBot 框架里配置的 STT Provider
4. 本地 funasr（仅在已安装时，最重）
音频统一压成 16kHz 单声道 32kbps，并按 browse_asr_max_minutes 截断，线上接口传输量很小。

抽帧用 video_analysis.extract_smart_frames（场景切换 + 均匀补帧），再把关键帧 + 转写
交给用户当前的对话模型（如 DeepSeek，可看图）一起总结；不支持图片时用 Gemini 看图，再不行只用转写。
"""

import asyncio
import json
import os
import time

import aiohttp

from astrbot.api import logger

from . import bili_account, bilibili_service
from .video_analysis import extract_smart_frames, local_asr_available

SUMMARY_PROMPT = (
    "下面是一个视频的元信息、语音转写和若干关键帧（按时间顺序）。"
    "请用中文写出：1) 一句话概括；2) 主要内容 3~5 条要点；3) 视频风格与情绪。不要编造转写和画面里没有的信息。"
)


async def _run(cmd: list[str]) -> bool:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        logger.warning(f"[browse] ffmpeg 失败: {err.decode(errors='ignore')[-200:]}")
    return proc.returncode == 0


async def compress_audio(video_path: str, max_minutes: float) -> str | None:
    out = os.path.splitext(video_path)[0] + "_asr.mp3"
    ok = await _run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
        "-b:a", "32k", "-t", str(int(max_minutes * 60)), out,
    ])
    return out if ok and os.path.exists(out) else None


# ── 转写各档 ─────────────────────────────────────────────


async def bili_subtitle(bvid: str) -> str:
    """B站字幕（需要登录 cookie 才返回字幕列表）；优先人工中文字幕，其次 AI 字幕。"""
    cookies = await bilibili_service.load_cookies() or {}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid},
                         headers=bili_account.headers(cookies)) as r:
            view = (await r.json(content_type=None)).get("data") or {}
        if not view:
            return ""
        # view 接口给出本视频字幕的 id（但不给 url）；x/player/v2 给 url，却时常混入别的视频的
        # AI 字幕，所以必须校验：id 一致，或 url 路径以 "{aid}{cid}" 开头
        expect_ids = {str(x.get("id_str") or x.get("id")) for x in (view.get("subtitle") or {}).get("list") or []}
        if not expect_ids:
            return ""
        prefix = f"{view['aid']}{view['cid']}"

        def rank(x):  # 人工中文 → AI 中文 → 其他语言
            lan = x.get("lan", "")
            return 0 if lan.startswith("zh") else 1 if lan.startswith("ai-zh") else 2

        url = ""
        for _ in range(4):
            # 用非 wbi 的 x/player/v2：wbi 版对脚本请求经常直接 412
            async with s.get("https://api.bilibili.com/x/player/v2", params={"aid": view["aid"], "cid": view["cid"]},
                             headers=bili_account.headers(cookies, f"https://www.bilibili.com/video/{bvid}")) as r:
                text = await r.text()
            if not text.lstrip().startswith("{"):
                raise RuntimeError(f"字幕接口被拦截 (HTTP {r.status})")
            subs = ((json.loads(text).get("data") or {}).get("subtitle") or {}).get("subtitles") or []
            good = [
                x for x in subs
                if x.get("subtitle_url") and (
                    str(x.get("id_str") or x.get("id")) in expect_ids
                    or x["subtitle_url"].rsplit("/", 1)[-1].startswith(prefix)
                )
            ]
            if good:
                url = sorted(good, key=rank)[0]["subtitle_url"]
                break
            await asyncio.sleep(0.5)
        if not url:
            logger.info(f"[browse] {bvid} 的字幕接口返回的都不是本视频的字幕，改用语音转写")
            return ""
        async with s.get("https:" + url if url.startswith("//") else url) as r:
            body = (await r.json(content_type=None)).get("body") or []
    return "".join(seg.get("content", "") + "，" for seg in body).strip("，")


async def online_asr(audio_path: str, base: str, key: str, model: str) -> str:
    form = aiohttp.FormData()
    form.add_field("model", model)
    form.add_field("file", open(audio_path, "rb"), filename=os.path.basename(audio_path), content_type="audio/mpeg")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as s:
        async with s.post(f"{base.rstrip('/')}/audio/transcriptions", data=form,
                          headers={"Authorization": f"Bearer {key}"}) as r:
            data = await r.json(content_type=None)
    if r.status != 200:
        raise RuntimeError(f"HTTP {r.status}: {str(data)[:200]}")
    return (data.get("text") or "").strip()


async def framework_stt(plugin, audio_path: str) -> str:
    insts = getattr(plugin.context.provider_manager, "stt_provider_insts", []) or []
    for prov in insts:
        try:
            text = await prov.get_text(os.path.abspath(audio_path))
            if text:
                return text.strip()
        except Exception as e:
            logger.info(f"[browse] 框架 STT {prov.meta().id} 失败: {e}")
    return ""


async def local_asr(audio_path: str) -> str:
    from .video_analysis import transcribe_audio

    res = await transcribe_audio(audio_path)
    return (res or {}).get("text", "") if isinstance(res, dict) else ""


async def transcribe(plugin, video_path: str, platform: str = "", work_id: str = "") -> tuple[str, str]:
    """返回 (转写文本, 来源)。全部失败返回 ("", 原因)。"""
    errors = []
    if platform == "bilibili" and work_id:
        try:
            text = await bili_subtitle(work_id)
            if text:
                return text, "B站字幕"
            errors.append("无字幕")
        except Exception as e:
            errors.append(f"字幕: {e}")

    cfg = plugin.config
    base, key = cfg.get("browse_asr_api_base", ""), cfg.get("browse_asr_api_key", "")
    model = cfg.get("browse_asr_model", "") or "FunAudioLLM/SenseVoiceSmall"
    audio = await compress_audio(video_path, float(cfg.get("browse_asr_max_minutes", 15) or 15))
    if not audio:
        return "", "音频提取失败（视频可能无声）"
    try:
        if base and key:
            try:
                text = await online_asr(audio, base, key, model)
                if text:
                    return text, f"线上ASR({model})"
                errors.append("线上ASR 返回空")
            except Exception as e:
                errors.append(f"线上ASR: {e}")
        text = await framework_stt(plugin, audio)
        if text:
            return text, "框架STT"
        if local_asr_available():
            text = await local_asr(audio)
            if text:
                return text, "本地ASR"
        return "", "；".join(errors) or "未配置线上 ASR（browse_asr_api_base/key），也没有框架 STT"
    finally:
        try:
            os.remove(audio)
        except OSError:
            pass


# ── 总结 ─────────────────────────────────────────────────


def _provider_sees_images(prov) -> bool:
    """provider 的 modalities 没配置时按"支持"处理（与 AstrBot 自身的默认一致）。"""
    mods = (getattr(prov, "provider_config", None) or {}).get("modalities") or []
    return not mods or "image" in mods


async def _shrink_frames(frames: list[str], width: int = 640) -> list[str]:
    """把关键帧缩到 640 宽的 JPEG，减少多模态 token。失败就用原图。"""
    out = []
    try:
        from PIL import Image
    except ImportError:
        return frames
    for f in frames:
        small = os.path.splitext(f)[0] + "_s.jpg"
        try:
            with Image.open(f) as im:
                im = im.convert("RGB")
                if im.width > width:
                    im = im.resize((width, int(im.height * width / im.width)))
                im.save(small, "JPEG", quality=80)
            out.append(small)
        except Exception:
            out.append(f)
    return out


async def summarize(plugin, event, meta: dict, transcript: str, frames: list[str]) -> tuple[str, str]:
    """
    关键帧 + 转写 → 概要，返回 (概要, 方式)。

    默认交给用户当前的对话模型（如 DeepSeek，新版可直接看图）；
    browse_summary_by=gemini 时优先 Gemini。逐级降级：图文 → 另一种图文 → 纯文字 → 转写节选。
    """
    info = (
        f"标题：{meta.get('title', '')}\nUP/作者：{meta.get('author', '')}\n简介：{(meta.get('desc') or '')[:300]}\n\n"
        f"语音转写：{transcript[:6000] or '（无）'}"
    )
    from ..utils.config_helper import resolve_gemini_config
    from .gemini_service import process_images_with_gemini

    ctx = plugin.context
    frames = await _shrink_frames(frames) if frames else []
    provider_id, prov = "", None
    try:
        provider_id = await ctx.get_current_chat_provider_id(event.unified_msg_origin)
        prov = await ctx.provider_manager.get_provider_by_id(provider_id)
    except Exception as e:
        logger.info(f"[browse] 取当前对话模型失败: {e}")

    async def by_llm(with_images: bool) -> str:
        if not prov:
            return ""
        try:
            resp = await ctx.llm_generate(
                chat_provider_id=provider_id,
                prompt=f"{SUMMARY_PROMPT}{'' if with_images else '（本次没有画面，只有文字）'}\n\n{info}",
                image_urls=[os.path.abspath(f) for f in frames] if with_images else None,
            )
            return (resp.completion_text or "").strip()
        except Exception as e:
            logger.warning(f"[browse] 对话模型总结失败（{'图文' if with_images else '纯文字'}）: {e}")
            return ""

    async def by_gemini() -> str:
        gcfg = resolve_gemini_config(plugin)
        if not (gcfg and frames):
            return ""
        text, _ = await process_images_with_gemini(gcfg, f"{SUMMARY_PROMPT}\n\n{info}", frames)
        return text or ""

    model = prov.get_model() if prov else ""
    llm_vision = bool(frames) and prov is not None and _provider_sees_images(prov)
    steps = [("gemini", by_gemini), ("llm_vision", lambda: by_llm(True))]
    if plugin.config.get("browse_summary_by", "llm") != "gemini":
        steps.reverse()
    steps.append(("llm_text", lambda: by_llm(False)))
    labels = {
        "llm_vision": f"关键帧×{len(frames)}+转写 → {model or '对话模型'}",
        "gemini": f"关键帧×{len(frames)}+转写 → Gemini",
        "llm_text": f"仅转写 → {model or '对话模型'}",
    }
    for name, fn in steps:
        if name == "llm_vision" and not llm_vision:
            continue
        text = await fn()
        if text:
            return text, labels[name]
    if transcript:
        return f"（AI 总结暂不可用，以下为语音转写节选）\n{transcript[:500]}", "仅转写节选"
    return "", "无可用的总结方式"


async def browse(plugin, event, video_path: str, meta: dict, platform: str = "", work_id: str = "") -> dict:
    """完整浏览流程，返回 {summary, method, transcript_source, transcript_len, frames, seconds}。"""
    start = time.monotonic()
    max_frames = int(plugin.config.get("browse_max_frames", 6) or 6)
    (transcript, source), frames = await asyncio.gather(
        transcribe(plugin, video_path, platform, work_id),
        extract_smart_frames(video_path, max_frames=max_frames),
    )
    summary, method = await summarize(plugin, event, meta, transcript, frames or [])
    return {
        "summary": summary, "method": method, "transcript_source": source,
        "transcript_len": len(transcript), "transcript": transcript, "frames": frames or [],
        "seconds": round(time.monotonic() - start, 1),
    }


async def browse_work(plugin, event, platform: str, work_id: str, ctx: dict, report_seconds: int = 0) -> dict:
    """下载作品 → 浏览 → 缓存概要；可选把观看写进账号历史。临时文件全部清理。"""
    import shutil

    from .creator import get_creator

    creator = get_creator(plugin, platform)
    path = await creator.fetch_video(work_id, ctx)
    if not path or not os.path.exists(path):
        raise RuntimeError("视频下载失败")
    try:
        res = await browse(plugin, event, path, ctx, platform, work_id)
    finally:
        shutil.rmtree(os.path.splitext(path)[0] + "_smart_frames", ignore_errors=True)
        try:
            os.remove(path)
        except OSError:
            pass
    if res.get("summary"):
        plugin.creator_ops.cache_summary(platform, work_id, res["summary"])
    if report_seconds and hasattr(creator, "report_watch"):
        try:
            res["watch_report"] = (await creator.report_watch(work_id, report_seconds)).message
        except Exception as e:
            res["watch_report"] = f"上报失败: {e}"
    return res
