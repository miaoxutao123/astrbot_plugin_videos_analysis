"""
视频"浏览"：语音转写 → 按转写挑关键帧 → 当前对话模型看图总结

1. 转写（带时间轴），按机器压力从轻到重：
   B站字幕（零算力，自带时间轴）→ 线上 ASR（OpenAI 兼容 /audio/transcriptions；先要 verbose_json
   的分段时间轴，接口不给就把音频切成 30 秒一段并行转写，得到粗时间轴）→ 框架 STT → 本地 funasr
2. 挑帧：有时间轴时，让当前对话模型读带时间的转写，判断哪些时刻"必须看画面才能理解"
   （演示、指代"看这里"、对比、结论等）；模型不可用时按关键词打分兜底。没有转写就按时长均匀切帧。
3. 总结：关键帧（缩到 640 宽，标注时间和挑选原因）+ 转写一起交给当前对话模型（如可看图的新版 DeepSeek）；
   模型不支持图片时，开启了 Gemini 才用 Gemini 看图，否则只用转写。
每次浏览的临时文件（音频、分段、帧）都放在独立目录，结束即删；异常残留超过 1 小时的目录会被顺带清理。
"""

import asyncio
import json
import os
import re
import shutil
import time
import uuid

import aiohttp

from astrbot.api import logger

from ..utils.paths import CREATOR_DIR, DOWNLOAD_DIR
from . import bili_account, bilibili_service
from .bili_http import get_session
from .video_analysis import _get_video_duration, local_asr_available

TMP_ROOT = os.path.join(DOWNLOAD_DIR, "browse_tmp")
CHUNK_SECONDS = 30
SUMMARY_PROMPT = (
    "下面是一个视频的元信息、带时间轴的语音转写，以及按时间顺序挑出的关键帧（每张图的时间和挑选原因见列表）。"
    "请用中文写出：1) 一句话概括；2) 主要内容 3~5 条要点（画面信息和语音信息结合着说）；3) 视频风格与情绪。"
    "不要编造转写和画面里没有的信息。"
)
PLAN_PROMPT = (
    "你在帮忙“看”一个视频。下面是带时间轴的语音转写。请挑出最需要看画面才能理解内容的时刻，"
    "例如：在演示/展示东西、说“看这里/这个/如图”、做对比、出现关键结论或情绪高潮、只靠声音说不清的地方。"
    "要求：最多 {n} 个，尽量覆盖全片，相邻至少间隔 {gap} 秒，时间不能超过 {dur} 秒。"
    '只输出 JSON 数组，例如 [{{"t": 12.5, "why": "展示成品"}}]。\n\n'
)
DEICTIC = re.compile(r"看|这个|这里|这边|如图|画面|演示|展示|效果|对比|左边|右边|屏幕|上面|下面|结果|最后|总结|第一|首先")


def _fmt_t(t: float) -> str:
    return f"{int(t) // 60:02d}:{int(t) % 60:02d}"


async def _run(cmd: list[str]) -> bool:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        logger.warning(f"[browse] ffmpeg 失败: {err.decode(errors='ignore')[-200:]}")
    return proc.returncode == 0


def _sweep_stale_tmp(max_age: float = 3600) -> None:
    if not os.path.isdir(TMP_ROOT):
        return
    for name in os.listdir(TMP_ROOT):
        path = os.path.join(TMP_ROOT, name)
        try:
            if time.time() - os.path.getmtime(path) > max_age:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def cleanup_browse_files(video_path: str) -> None:
    """清理旧版本留在视频旁边的抽帧目录/音频（新版本的临时文件都在 TMP_ROOT 下，自动删除）。"""
    base = os.path.splitext(video_path)[0]
    shutil.rmtree(base + "_smart_frames", ignore_errors=True)
    for suffix in ("_asr.mp3", "_audio.mp3", "_video.mp4"):
        try:
            os.remove(base + suffix)
        except OSError:
            pass


# ── 转写（带时间轴） ─────────────────────────────────────
# 段格式：[{"start": 秒, "end": 秒, "text": "..."}]；没有时间轴时只有一段 start=end=0


async def bili_subtitle(bvid: str) -> list[dict]:
    """B站字幕（需要登录 cookie 才返回字幕列表）；优先人工中文字幕，其次 AI 字幕。"""
    if bilibili_service.bili_read_circuit_open():
        return []
    cookies = await bilibili_service.load_cookies() or {}
    session = get_session()
    timeout = aiohttp.ClientTimeout(total=15)
    async with session.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid},
                           headers=bili_account.headers(cookies), timeout=timeout) as r:
        if r.status in (412, 429):
            await bilibili_service._open_bili_read_circuit(r.status, f"subtitle view HTTP {r.status}")
            return []
        payload = await r.json(content_type=None)
        if isinstance(payload, dict) and payload.get("code") in bilibili_service.RISK_CODES:
            await bilibili_service._open_bili_read_circuit(payload.get("code"), payload.get("message", ""))
            return []
        view = payload.get("data") or {} if isinstance(payload, dict) else {}
    if not view:
        return []
    # view 接口给出本视频字幕的 id（但不给 url）；x/player/v2 给 url，却时常混入别的视频的
    # AI 字幕，所以必须校验：id 一致，或 url 路径以 aid+cid 开头
    expect_ids = {str(x.get("id_str") or x.get("id")) for x in (view.get("subtitle") or {}).get("list") or []}
    if not expect_ids:
        return []
    prefix = f"{view['aid']}{view['cid']}"

    def rank(x):
        lan = x.get("lan", "")
        return 0 if lan.startswith("zh") else 1 if lan.startswith("ai-zh") else 2

    url = ""
    for _ in range(4):
        async with session.get("https://api.bilibili.com/x/player/v2",
                               params={"aid": view["aid"], "cid": view["cid"]},
                               headers=bili_account.headers(cookies, f"https://www.bilibili.com/video/{bvid}"),
                               timeout=timeout) as r:
            text = await r.text()
            status = r.status
        if status in (412, 429):
            await bilibili_service._open_bili_read_circuit(status, f"subtitle player HTTP {status}")
            return []
        if not text.lstrip().startswith("{"):
            raise RuntimeError(f"字幕接口返回非 JSON（HTTP {status}）")
        payload = json.loads(text)
        if not isinstance(payload, dict):
            return []
        if payload.get("code") in bilibili_service.RISK_CODES:
            await bilibili_service._open_bili_read_circuit(payload.get("code"), payload.get("message", ""))
            return []
        subs = ((payload.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
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
        return []
    async with session.get("https:" + url if url.startswith("//") else url, timeout=timeout) as r:
        body = (await r.json(content_type=None)).get("body") or []
    return [{"start": float(b.get("from", 0)), "end": float(b.get("to", 0)), "text": b.get("content", "")}
            for b in body if b.get("content")]


async def _post_asr(audio_path: str, base: str, key: str, model: str, verbose: bool) -> dict:
    form = aiohttp.FormData()
    form.add_field("model", model)
    if verbose:
        form.add_field("response_format", "verbose_json")
        form.add_field("timestamp_granularities[]", "segment")
    with open(audio_path, "rb") as f:
        form.add_field("file", f.read(), filename=os.path.basename(audio_path), content_type="audio/mpeg")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as s:
        async with s.post(f"{base.rstrip('/')}/audio/transcriptions", data=form,
                          headers={"Authorization": f"Bearer {key}"}) as r:
            data = await r.json(content_type=None)
    if r.status != 200:
        raise RuntimeError(f"HTTP {r.status}: {str(data)[:200]}")
    return data


async def online_asr(audio_path: str, tmp: str, duration: float, base: str, key: str, model: str) -> list[dict]:
    """线上 ASR。优先接口自带的分段时间轴；没有就按 30 秒切段并行转写。"""
    data = await _post_asr(audio_path, base, key, model, verbose=True)
    segs = [
        {"start": float(x.get("start", 0)), "end": float(x.get("end", 0)), "text": (x.get("text") or "").strip()}
        for x in data.get("segments") or [] if (x.get("text") or "").strip()
    ]
    if segs:
        return segs
    text = (data.get("text") or "").strip()
    if duration <= CHUNK_SECONDS * 1.5 or not text:
        return [{"start": 0.0, "end": duration, "text": text}] if text else []
    # 接口不给时间轴：切成 30 秒的片段分别转写（SenseVoice 这类模型很快，切片并发 4）
    pattern = os.path.join(tmp, "chunk_%03d.mp3")
    if not await _run(["ffmpeg", "-y", "-loglevel", "error", "-i", audio_path, "-f", "segment",
                       "-segment_time", str(CHUNK_SECONDS), "-c", "copy", pattern]):
        return [{"start": 0.0, "end": duration, "text": text}]
    # 最后一段太短（<2 秒）时接口会报 "Audio is too short"，直接跳过
    chunks = sorted(f for f in os.listdir(tmp) if f.startswith("chunk_"))
    if len(chunks) > 1 and duration - (len(chunks) - 1) * CHUNK_SECONDS < 2:
        chunks = chunks[:-1]
    sem = asyncio.Semaphore(4)

    async def one(i: int, name: str) -> dict:
        async with sem:
            try:
                d = await _post_asr(os.path.join(tmp, name), base, key, model, verbose=False)
                t = (d.get("text") or "").strip()
            except Exception as e:
                logger.info(f"[browse] 分段 {i} 转写失败: {e}")
                t = ""
        return {"start": float(i * CHUNK_SECONDS), "end": float(min(duration, (i + 1) * CHUNK_SECONDS)), "text": t}

    segs = [s for s in await asyncio.gather(*(one(i, n) for i, n in enumerate(chunks))) if s["text"]]
    return segs or [{"start": 0.0, "end": duration, "text": text}]


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


async def transcribe(plugin, video_path: str, tmp: str, duration: float, platform: str = "", work_id: str = "") -> tuple[list[dict], str]:
    """返回 (分段列表, 来源说明)。全部失败返回 ([], 原因)。"""
    errors = []
    if platform == "bilibili" and work_id:
        try:
            segs = await bili_subtitle(work_id)
            if segs:
                return segs, "B站字幕"
            errors.append("无字幕")
        except Exception as e:
            errors.append(f"字幕: {e}")

    cfg = plugin.config
    base, key = cfg.get("browse_asr_api_base", ""), cfg.get("browse_asr_api_key", "")
    model = cfg.get("browse_asr_model", "") or "FunAudioLLM/SenseVoiceSmall"
    max_s = float(cfg.get("browse_asr_max_minutes", 15) or 15) * 60
    audio = os.path.join(tmp, "audio.mp3")
    if not await _run(["ffmpeg", "-y", "-loglevel", "error", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
                       "-b:a", "32k", "-t", str(int(max_s)), audio]):
        return [], "音频提取失败（视频可能无声）"
    span = min(duration or max_s, max_s)
    if base and key:
        try:
            segs = await online_asr(audio, tmp, span, base, key, model)
            if segs:
                return segs, f"线上ASR({model})"
            errors.append("线上ASR 返回空")
        except Exception as e:
            errors.append(f"线上ASR: {e}")
    text = await framework_stt(plugin, audio)
    if text:
        return [{"start": 0.0, "end": span, "text": text}], "框架STT"
    if local_asr_available():
        text = await local_asr(audio)
        if text:
            return [{"start": 0.0, "end": span, "text": text}], "本地ASR"
    if not (base and key):
        errors.append("未配置语音转写（可对机器人说“帮我配置语音转写”）")
    return [], "；".join(errors)


def _has_timeline(segs: list[dict]) -> bool:
    return len(segs) >= 2 and any(s["end"] > 0 for s in segs)


def _timeline_text(segs: list[dict], window: float = 15, limit: int = 5000) -> str:
    """把分段合并成 ~15 秒一行的时间轴文本，控制长度。"""
    lines, cur, start = [], [], None
    for s in segs:
        if start is None:
            start = s["start"]
        cur.append(s["text"])
        if s["end"] - start >= window:
            lines.append(f"[{_fmt_t(start)}] {''.join(cur)}")
            cur, start = [], None
    if cur:
        lines.append(f"[{_fmt_t(start or 0)}] {''.join(cur)}")
    out = "\n".join(lines)
    return out[:limit]


def _plain_text(segs: list[dict]) -> str:
    return "".join(s["text"] + ("，" if not s["text"].endswith(("。", "！", "？", "，")) else "") for s in segs).strip("，")


# ── 挑帧 ─────────────────────────────────────────────────


def _frame_budget(plugin, duration: float) -> int:
    n = int(plugin.config.get("browse_max_frames", 6) or 6)
    return max(2, min(n, int(duration // 5) or 2))


def _interval_plan(duration: float, n: int) -> list[dict]:
    step = duration / (n + 1)
    return [{"t": round(step * (i + 1), 1), "why": "按时长均匀抽取"} for i in range(n)]


def _keyword_plan(segs: list[dict], duration: float, n: int) -> list[dict]:
    """关键词打分：指代/展示类词越多越需要看画面；再保证首尾覆盖。"""
    scored = sorted(
        ((len(DEICTIC.findall(s["text"])), (s["start"] + s["end"]) / 2, s["text"][:20]) for s in segs),
        key=lambda x: -x[0],
    )
    picks = [{"t": round(t, 1), "why": f"转写提到画面：{txt}"} for score, t, txt in scored if score > 0]
    picks += [{"t": round(min(3.0, duration / 10), 1), "why": "开头"}, {"t": round(duration * 0.9, 1), "why": "结尾"}]
    picks += _interval_plan(duration, n)
    return picks


def _dedupe(plan: list[dict], duration: float, n: int, gap: float) -> list[dict]:
    out = []
    for p in plan:
        try:
            t = float(p.get("t"))
        except (TypeError, ValueError):
            continue
        if 0 <= t <= max(0.0, duration - 0.3) and all(abs(t - q["t"]) >= gap for q in out):
            out.append({"t": t, "why": str(p.get("why", ""))[:40]})
        if len(out) >= n:
            break
    return sorted(out, key=lambda x: x["t"])


async def plan_frames(plugin, event, segs: list[dict], duration: float) -> tuple[list[dict], str]:
    """返回 (帧计划, 挑帧方式)。"""
    n = _frame_budget(plugin, duration)
    gap = max(2.0, duration / (n * 3))
    if not _has_timeline(segs):
        return _dedupe(_interval_plan(duration, n), duration, n, gap), "按时长均匀抽帧（没有带时间轴的转写）"
    ctx = plugin.context
    try:
        provider_id = await ctx.get_current_chat_provider_id(event.unified_msg_origin)
        resp = await ctx.llm_generate(
            chat_provider_id=provider_id,
            prompt=PLAN_PROMPT.format(n=n, gap=int(gap), dur=int(duration)) + _timeline_text(segs),
        )
        m = re.search(r"\[.*\]", resp.completion_text or "", re.S)
        raw = json.loads(m.group(0)) if m else []
        # 粗时间轴（每段 ≥20 秒）时，模型常给出段首时间；挪到段内三分之一处，更可能落在正在讲的画面上
        starts = {round(sg["start"], 1): sg["end"] - sg["start"] for sg in segs if sg["end"] - sg["start"] >= 20}
        for p in raw:
            try:
                t = round(float(p.get("t")), 1)
            except (TypeError, ValueError, AttributeError):
                continue
            if t in starts:
                p["t"] = t + starts[t] / 3
        plan = _dedupe(raw, duration, n, gap)
        if plan:
            if len(plan) < n:  # 模型挑得少，用均匀帧补齐覆盖
                plan = _dedupe(plan + _interval_plan(duration, n), duration, n, gap)
            return plan, "对话模型按转写挑帧"
    except Exception as e:
        logger.info(f"[browse] 模型挑帧失败，改用关键词: {e}")
    return _dedupe(_keyword_plan(segs, duration, n), duration, n, gap), "按转写关键词挑帧"


async def extract_frames(video_path: str, plan: list[dict], tmp: str) -> list[dict]:
    """按计划抽帧（直接缩到 640 宽），返回带 path 的计划。"""
    sem = asyncio.Semaphore(3)

    async def one(i: int, p: dict) -> dict | None:
        out = os.path.join(tmp, f"frame_{i:02d}.jpg")
        async with sem:
            ok = await _run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{p['t']:.2f}", "-i", video_path,
                             "-frames:v", "1", "-vf", "scale='min(640,iw)':-2", "-q:v", "4", out])
        return {**p, "path": out} if ok and os.path.exists(out) else None

    return [f for f in await asyncio.gather(*(one(i, p) for i, p in enumerate(plan))) if f]


# ── 总结 ─────────────────────────────────────────────────


def _provider_sees_images(prov) -> bool:
    """provider 的 modalities 没配置时按"支持"处理（与 AstrBot 自身的默认一致）。"""
    mods = (getattr(prov, "provider_config", None) or {}).get("modalities") or []
    return not mods or "image" in mods


async def _current_provider(plugin, event):
    ctx = plugin.context
    try:
        pid = await ctx.get_current_chat_provider_id(event.unified_msg_origin)
        return pid, await ctx.provider_manager.get_provider_by_id(pid)
    except Exception as e:
        logger.info(f"[browse] 取当前对话模型失败: {e}")
        return "", None


async def _shrink(paths: list[str], tmp: str, width: int = 1024) -> list[str]:
    try:
        from PIL import Image
    except ImportError:
        return paths
    out = []
    for i, f in enumerate(paths):
        small = os.path.join(tmp, f"img_{i:02d}.jpg")
        try:
            with Image.open(f) as im:
                im = im.convert("RGB")
                if im.width > width:
                    im = im.resize((width, int(im.height * width / im.width)))
                im.save(small, "JPEG", quality=82)
            out.append(small)
        except Exception:
            out.append(f)
    return out


async def describe_images(plugin, event, image_paths: list[str], prompt: str) -> str:
    """图文作品理解：当前对话模型看图优先；只有开启了 Gemini 才用 Gemini。"""
    pid, prov = await _current_provider(plugin, event)
    tmp = os.path.join(TMP_ROOT, uuid.uuid4().hex[:10])
    os.makedirs(tmp, exist_ok=True)
    try:
        if prov and _provider_sees_images(prov):
            try:
                resp = await plugin.context.llm_generate(
                    chat_provider_id=pid, prompt=prompt,
                    image_urls=[os.path.abspath(p) for p in await _shrink(image_paths, tmp)],
                )
                if resp.completion_text:
                    return resp.completion_text.strip()
            except Exception as e:
                logger.warning(f"[browse] 对话模型看图失败: {e}")
        from ..utils.config_helper import resolve_gemini_config
        from .gemini_service import process_images_with_gemini

        gcfg = resolve_gemini_config(plugin)
        if gcfg:
            text, _ = await process_images_with_gemini(gcfg, prompt, image_paths)
            return text or ""
        return ""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def summarize(plugin, event, meta: dict, segs: list[dict], frames: list[dict], user_prompt: str | None = None) -> tuple[str, str]:
    """关键帧 + 转写 → 概要，返回 (概要, 方式)。逐级降级：对话模型看图 → Gemini 看图（需开启）→ 纯文字 → 转写节选。"""
    transcript = _timeline_text(segs, limit=6000) if _has_timeline(segs) else _plain_text(segs)[:6000]
    frame_list = "\n".join(f"第{i}张图 = {_fmt_t(f['t'])}（{f['why']}）" for i, f in enumerate(frames, 1)) or "（无）"
    info = (
        f"标题：{meta.get('title', '')}\nUP/作者：{meta.get('author', '')}\n简介：{(meta.get('desc') or '')[:300]}\n\n"
        f"关键帧：\n{frame_list}\n\n语音转写：\n{transcript or '（无）'}"
    )
    prompt = SUMMARY_PROMPT + (f"\n另外，用户特别想知道：{user_prompt}" if user_prompt else "")
    pid, prov = await _current_provider(plugin, event)
    images = [os.path.abspath(f["path"]) for f in frames]

    async def by_llm(with_images: bool) -> str:
        if not prov:
            return ""
        try:
            resp = await plugin.context.llm_generate(
                chat_provider_id=pid,
                prompt=f"{prompt}{'' if with_images else '（本次没有画面，只有文字）'}\n\n{info}",
                image_urls=images if with_images else None,
            )
            return (resp.completion_text or "").strip()
        except Exception as e:
            logger.warning(f"[browse] 对话模型总结失败（{'图文' if with_images else '纯文字'}）: {e}")
            return ""

    async def by_gemini() -> str:
        from ..utils.config_helper import resolve_gemini_config
        from .gemini_service import process_images_with_gemini

        gcfg = resolve_gemini_config(plugin)  # 未开启 Gemini 时为 None
        if not (gcfg and images):
            return ""
        text, _ = await process_images_with_gemini(gcfg, f"{prompt}\n\n{info}", images)
        return text or ""

    model = prov.get_model() if prov else ""
    steps = []
    if images and prov is not None and _provider_sees_images(prov):
        steps.append((lambda: by_llm(True), f"关键帧×{len(images)}+转写 → {model or '对话模型'}"))
    gemini_step = (by_gemini, f"关键帧×{len(images)}+转写 → Gemini")
    if plugin.config.get("browse_summary_by", "llm") == "gemini":
        steps.insert(0, gemini_step)
    else:
        steps.append(gemini_step)
    steps.append((lambda: by_llm(False), f"仅转写 → {model or '对话模型'}"))
    for fn, label in steps:
        text = await fn()
        if text:
            return text, label
    plain = _plain_text(segs)
    if plain:
        return f"（AI 总结暂不可用，以下为语音转写节选）\n{plain[:500]}", "仅转写节选"
    return "", "无可用的总结方式"


async def browse(plugin, event, video_path: str, meta: dict, platform: str = "", work_id: str = "", user_prompt: str | None = None) -> dict:
    """完整浏览流程。返回 {summary, method, frame_method, transcript_source, transcript, frames, seconds}，临时文件全部清理。"""
    start = time.monotonic()
    _sweep_stale_tmp()
    tmp = os.path.join(TMP_ROOT, uuid.uuid4().hex[:10])
    os.makedirs(tmp, exist_ok=True)
    try:
        duration = await _get_video_duration(video_path) or 60.0
        segs, source = await transcribe(plugin, video_path, tmp, duration, platform, work_id)
        plan, frame_method = await plan_frames(plugin, event, segs, duration)
        frames = await extract_frames(video_path, plan, tmp)
        logger.info(f"[browse] 转写={source}（{len(segs)} 段）挑帧={frame_method} 帧={[_fmt_t(f['t']) for f in frames]}")
        summary, method = await summarize(plugin, event, meta, segs, frames, user_prompt)
        return {
            "summary": summary, "method": method, "frame_method": frame_method,
            "frame_times": [_fmt_t(f["t"]) for f in frames],
            "transcript_source": source, "transcript": _plain_text(segs),
            "transcript_len": len(_plain_text(segs)), "seconds": round(time.monotonic() - start, 1),
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cached_summary(plugin, platform: str, work_id: str) -> str:
    from .analysis_cache import get_summary

    ttl_days = float(plugin.config.get("analysis_cache_ttl_days", 7) or 7)
    return get_summary(platform, work_id, ttl_days)

def max_seconds(plugin) -> int:
    return int(float(plugin.config.get("browse_max_minutes", 20) or 20) * 60)


async def browse_work(plugin, event, platform: str, work_id: str, ctx: dict, report_seconds: int = 0) -> dict:
    """下载作品（长视频只取开头）→ 浏览 → 缓存概要；可选把观看写进账号历史。视频文件用完即删。"""
    from .creator import get_creator

    creator = get_creator(plugin, platform)
    limit = max_seconds(plugin)
    path = await creator.fetch_video(work_id, ctx, max_seconds=limit)
    if not path or not os.path.exists(path):
        raise RuntimeError("视频下载失败")
    try:
        res = await browse(plugin, event, path, ctx, platform, work_id)
    finally:
        cleanup_browse_files(path)
        try:
            os.remove(path)
        except OSError:
            pass
    if ctx.get("duration", 0) > limit:
        res["partial"] = f"视频共 {ctx['duration'] // 60} 分钟，只看了前 {limit // 60} 分钟"
    if res.get("summary"):
        plugin.creator_ops.cache_summary(platform, work_id, res["summary"])
    if report_seconds and hasattr(creator, "report_watch"):
        try:
            res["watch_report"] = (await creator.report_watch(work_id, report_seconds)).message
        except Exception as e:
            res["watch_report"] = f"上报失败: {e}"
    return res
