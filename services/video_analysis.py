"""
视频理解模块：SenseVoice ASR + 智能抽帧

使用 FunASR 框架的 SenseVoice-Small 模型进行：
- 语音识别 (ASR)
- 情绪检测 (SER)
- 音频事件检测 (AED)
- 语言识别 (LID)

智能抽帧策略：
1. ffmpeg 场景切换检测（visual cuts）
2. ASR 语音段起始时间点抽帧
3. 合并去重 + 均匀补充
"""

import asyncio
import gc
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from astrbot.api import logger

# ==================== 模型管理（带自动卸载） ====================

# 闲置超时后自动从显存卸载（秒）
MODEL_IDLE_TIMEOUT = 300  # 5 分钟

_model = None
_model_lock = threading.Lock()
_unload_timer: threading.Timer | None = None


def _schedule_unload():
    """重置自动卸载计时器"""
    global _unload_timer
    if _unload_timer is not None:
        _unload_timer.cancel()
    _unload_timer = threading.Timer(MODEL_IDLE_TIMEOUT, _unload_model)
    _unload_timer.daemon = True  # 不阻止进程退出
    _unload_timer.start()


def _unload_model():
    """从显存中卸载模型"""
    global _model, _unload_timer
    with _model_lock:
        if _model is not None:
            logger.info("SenseVoice 模型闲置超时，自动从显存卸载...")
            del _model
            _model = None
            _unload_timer = None
            # 强制清理 GPU 显存
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass
            gc.collect()
            logger.info("SenseVoice 模型已卸载，显存已释放。")


def _get_model():
    """获取 SenseVoice 模型（懒加载 + 使用后自动倒计时卸载）"""
    global _model
    with _model_lock:
        if _model is not None:
            _schedule_unload()  # 重置卸载计时器
            return _model

        logger.info("正在加载 SenseVoice-Small 模型（首次加载需下载约 450MB）...")
        start = time.monotonic()

        try:
            from funasr import AutoModel

            _model = AutoModel(
                model="iic/SenseVoiceSmall",
                trust_remote_code=True,
                device="cuda",
            )
            elapsed = time.monotonic() - start
            logger.info(f"SenseVoice-Small (GPU) 加载完成，耗时 {elapsed:.1f}s，{MODEL_IDLE_TIMEOUT}s 闲置后自动卸载")

        except Exception as e:
            logger.error(f"SenseVoice GPU 加载失败: {e}，回退到 CPU")
            try:
                from funasr import AutoModel

                _model = AutoModel(
                    model="iic/SenseVoiceSmall",
                    trust_remote_code=True,
                    device="cpu",
                )
                elapsed = time.monotonic() - start
                logger.info(f"SenseVoice-Small (CPU) 加载完成，耗时 {elapsed:.1f}s")
            except Exception as e2:
                logger.error(f"SenseVoice CPU 加载也失败: {e2}")
                raise

        _schedule_unload()  # 启动卸载计时器
        return _model


def _parse_sensevoice_output(raw_text: str) -> dict:
    """
    解析 SenseVoice 输出的富文本标签。

    SenseVoice 输出格式示例:
    <|zh|><|HAPPY|><|BGM|><|Speech|>大家好欢迎来到我的频道

    返回:
    {
        "text": "大家好欢迎来到我的频道",
        "language": "zh",
        "emotion": "HAPPY",
        "events": ["BGM", "Speech"]
    }
    """
    # 提取所有 <|...|> 标签
    tags = re.findall(r"<\|([^|]+)\|>", raw_text)
    # 移除标签得到纯文本
    clean_text = re.sub(r"<\|[^|]+\|>", "", raw_text).strip()

    language = ""
    emotion = ""
    events = []

    # 已知语言标签
    lang_tags = {"zh", "en", "ja", "ko", "yue", "nospeech"}
    # 已知情绪标签
    emotion_tags = {"HAPPY", "SAD", "ANGRY", "NEUTRAL"}

    for tag in tags:
        if tag.lower() in lang_tags:
            language = tag.lower()
        elif tag.upper() in emotion_tags:
            emotion = tag
        else:
            events.append(tag)

    return {
        "text": clean_text,
        "language": language,
        "emotion": emotion,
        "events": events,
    }


# 情绪 emoji 映射
EMOTION_EMOJI = {
    "HAPPY": "😊开心",
    "SAD": "😢悲伤",
    "ANGRY": "😠生气",
    "NEUTRAL": "😐中性",
}

# 语言名称映射
LANGUAGE_NAME = {
    "zh": "中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "yue": "粤语",
    "nospeech": "无语音",
}


async def transcribe_audio(audio_path: str) -> dict:
    """
    使用 SenseVoice 对音频进行语音识别。

    Returns:
        {
            "text": "识别的完整文本",
            "language": "zh",
            "emotion": "HAPPY",
            "events": ["BGM", "Speech"],
            "segments": [{"text": "...", "start": 0.0, "end": 2.5}, ...],
            "raw": "原始输出"
        }
    """
    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"音频文件不存在: {audio_path}")

    logger.info(f"开始 ASR 识别: {audio_path}")
    start = time.monotonic()

    # 在线程池中运行模型推理（避免阻塞事件循环）
    loop = asyncio.get_running_loop()

    def _run_inference():
        model = _get_model()
        result = model.generate(
            input=audio_path,
            cache={},
            language="auto",
            use_itn=True,
            batch_size_s=60,
        )
        return result

    result = await loop.run_in_executor(None, _run_inference)

    elapsed = time.monotonic() - start
    logger.info(f"ASR 识别完成，耗时 {elapsed:.1f}s")

    if not result or len(result) == 0:
        return {
            "text": "",
            "language": "nospeech",
            "emotion": "NEUTRAL",
            "events": [],
            "segments": [],
            "raw": "",
        }

    # 解析结果
    all_text_parts = []
    segments = []
    primary_language = ""
    primary_emotion = ""
    all_events = set()

    for item in result:
        raw_text = item.get("text", "")
        parsed = _parse_sensevoice_output(raw_text)

        if parsed["text"]:
            all_text_parts.append(parsed["text"])

        if parsed["language"] and not primary_language:
            primary_language = parsed["language"]
        if parsed["emotion"] and not primary_emotion:
            primary_emotion = parsed["emotion"]
        all_events.update(parsed["events"])

        # 构建 segment — 包含时间戳（如果 SenseVoice 返回的话）
        seg_data = {
            "text": parsed["text"],
            "language": parsed["language"],
            "emotion": parsed["emotion"],
            "events": parsed["events"],
        }
        # SenseVoice 可能返回 timestamp 信息
        if "timestamp" in item:
            seg_data["timestamp"] = item["timestamp"]
        if parsed["text"]:
            segments.append(seg_data)

    return {
        "text": "".join(all_text_parts),
        "language": primary_language or "nospeech",
        "emotion": primary_emotion or "NEUTRAL",
        "events": sorted(all_events),
        "segments": segments,
        "raw": str(result),
        "duration": elapsed,
    }


# ==================== 智能抽帧 ====================


async def _get_video_duration(video_path: str) -> float:
    """获取视频时长（秒）"""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_format", video_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    try:
        info = json.loads(stdout.decode())
        return float(info["format"]["duration"])
    except Exception:
        return 0.0


async def _extract_scene_change_timestamps(video_path: str, threshold: float = 0.3) -> list[float]:
    """
    使用 ffmpeg 场景切换检测，获取画面变化较大的时间点。

    Args:
        threshold: 场景变化阈值 (0-1)，越小越敏感

    Returns:
        时间点列表（秒）
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", video_path,
        "-vf", f"select='gt(scene\\,{threshold})',showinfo",
        "-vsync", "vfr", "-f", "null", "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    timestamps = []
    # showinfo 输出格式: [Parsed_showinfo_...] n:...  pts:... pts_time:12.345 ...
    for line in stderr.decode(errors="replace").split("\n"):
        if "pts_time:" in line:
            match = re.search(r"pts_time:(\d+\.?\d*)", line)
            if match:
                timestamps.append(float(match.group(1)))

    logger.debug(f"[smart_frames] 场景切换检测到 {len(timestamps)} 个切点: {timestamps[:10]}")
    return timestamps


async def _extract_frame_at_time(video_path: str, timestamp: float, output_path: str) -> bool:
    """在指定时间点精确抽取一帧"""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-ss", f"{timestamp:.3f}",
        "-i", video_path,
        "-frames:v", "1",
        "-q:v", "2",
        "-y", output_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    return proc.returncode == 0 and os.path.exists(output_path)


async def extract_smart_frames(
    video_path: str,
    asr_segments: list[dict] | None = None,
    max_frames: int = 10,
    min_gap: float = 2.0,
) -> list[str]:
    """
    智能抽帧：结合场景切换检测和 ASR 时间戳。

    策略：
    1. ffmpeg scene detection 获取视觉切点
    2. 从 ASR segments 的 timestamp 获取语音关键时刻
    3. 合并所有时间点，按最小间隔去重
    4. 不足 max_frames 时用均匀采样补充
    5. 始终包含首帧和末帧

    Args:
        video_path: 视频路径
        asr_segments: ASR 段落列表（含 timestamp）
        max_frames: 最大帧数
        min_gap: 两帧之间最小间隔（秒）
    """
    duration = await _get_video_duration(video_path)
    if duration <= 0:
        logger.warning(f"[smart_frames] 无法获取视频时长: {video_path}")
        return []

    # --- 收集候选时间点 ---
    candidates: set[float] = set()

    # 1. 始终包含首帧和末帧附近
    candidates.add(0.5)  # 开头
    candidates.add(max(0.5, duration - 0.5))  # 结尾

    # 2. 场景切换检测
    scene_ts = await _extract_scene_change_timestamps(video_path)
    candidates.update(scene_ts)

    # 3. ASR 时间戳 — 在每个语音段的开始时刻抽帧
    if asr_segments:
        for seg in asr_segments:
            ts = seg.get("timestamp")
            if ts and isinstance(ts, (list, tuple)):
                # timestamp 可能是 [[start_ms, end_ms], ...] 格式
                for pair in ts:
                    if isinstance(pair, (list, tuple)) and len(pair) >= 1:
                        start_sec = pair[0] / 1000.0 if pair[0] > 100 else pair[0]
                        candidates.add(start_sec)
                        if len(pair) >= 2:
                            # 也在结尾附近抽帧
                            end_sec = pair[1] / 1000.0 if pair[1] > 100 else pair[1]
                            candidates.add(end_sec)

    # 4. 不足时用均匀采样补充
    if len(candidates) < max_frames:
        interval = duration / (max_frames + 1)
        for i in range(1, max_frames + 1):
            candidates.add(i * interval)

    # --- 去重 + 排序 ---
    sorted_ts = sorted(t for t in candidates if 0 <= t <= duration)

    # 按最小间隔去重
    filtered = []
    for t in sorted_ts:
        if not filtered or (t - filtered[-1]) >= min_gap:
            filtered.append(t)

    # 限制最大帧数（保留首尾，中间均匀采样）
    if len(filtered) > max_frames:
        if max_frames >= 3:
            first, last = filtered[0], filtered[-1]
            middle = filtered[1:-1]
            step = len(middle) / (max_frames - 2)
            sampled = [first] + [middle[int(i * step)] for i in range(max_frames - 2)] + [last]
            filtered = sampled
        else:
            step = len(filtered) / max_frames
            filtered = [filtered[int(i * step)] for i in range(max_frames)]

    logger.info(f"[smart_frames] 最终抽帧 {len(filtered)} 帧: {[f'{t:.1f}s' for t in filtered]}")

    # --- 并行抽取帧 ---
    base = os.path.splitext(video_path)[0]
    output_dir = f"{base}_smart_frames"
    os.makedirs(output_dir, exist_ok=True)

    tasks = []
    output_paths = []
    for i, t in enumerate(filtered):
        out_path = os.path.join(output_dir, f"frame_{i:03d}_{t:.1f}s.jpg")
        output_paths.append(out_path)
        tasks.append(_extract_frame_at_time(video_path, t, out_path))

    results = await asyncio.gather(*tasks)
    frame_paths = [p for p, ok in zip(output_paths, results) if ok]

    logger.info(f"[smart_frames] 成功提取 {len(frame_paths)}/{len(filtered)} 帧")
    return frame_paths


# ==================== 辅助函数 ====================


async def separate_audio(video_path: str) -> str | None:
    """从视频分离音频，返回音频路径"""
    from ..utils.media_utils import separate_audio_video

    result = await separate_audio_video(video_path)
    if result:
        audio_path, _ = result
        return audio_path
    return None


# ==================== 主分析流程 ====================


async def analyze_video(video_path: str, frame_interval: int = 5, max_frames: int = 10) -> dict:
    """
    完整视频分析流程：先 ASR，再用 ASR 结果指导智能抽帧。

    流程:
    1. 分离音频 → ASR 语音识别
    2. 利用 ASR 的时间戳 + 场景检测 → 智能抽帧
    3. 返回 ASR 结果 + 关键帧路径

    Returns:
        {
            "asr": {ASR 结果},
            "frame_paths": [帧图片路径列表],
        }
    """
    logger.info(f"开始视频分析: {video_path}")

    # --- 第一步：分离音频 + ASR（先做，结果用于指导抽帧） ---
    audio_path = await separate_audio(video_path)

    asr_result = None
    if audio_path:
        try:
            asr_result = await transcribe_audio(audio_path)
        except Exception as e:
            logger.error(f"ASR 识别失败: {e}")
            asr_result = {
                "text": "",
                "language": "nospeech",
                "emotion": "NEUTRAL",
                "events": [],
                "segments": [],
                "error": str(e),
            }
        finally:
            # 清理临时音频文件
            if os.path.exists(audio_path):
                os.remove(audio_path)
            video_only = audio_path.replace("_audio.mp3", "_video.mp4")
            if os.path.exists(video_only):
                os.remove(video_only)

    # --- 第二步：智能抽帧（利用 ASR 时间戳） ---
    asr_segments = (asr_result or {}).get("segments", [])
    frame_paths = await extract_smart_frames(
        video_path,
        asr_segments=asr_segments,
        max_frames=max_frames,
    )

    return {
        "asr": asr_result or {
            "text": "(无法提取音频)",
            "language": "nospeech",
            "emotion": "NEUTRAL",
            "events": [],
            "segments": [],
        },
        "frame_paths": frame_paths or [],
    }


def format_asr_for_llm(asr_result: dict) -> str:
    """将 ASR 结果格式化为 LLM 友好的文本"""
    parts = []

    # 元信息
    lang = LANGUAGE_NAME.get(asr_result.get("language", ""), asr_result.get("language", ""))
    emotion = EMOTION_EMOJI.get(asr_result.get("emotion", ""), asr_result.get("emotion", ""))
    events = asr_result.get("events", [])

    meta = f"🌐语言: {lang}"
    if emotion:
        meta += f" | 🎭情绪: {emotion}"
    if events:
        meta += f" | 🔊音频事件: [{', '.join(events)}]"
    parts.append(meta)

    # 识别文本
    text = asr_result.get("text", "")
    if text:
        parts.append(f"\n📝 语音内容:\n{text}")
    else:
        parts.append("\n(未检测到语音内容)")

    # 耗时
    duration = asr_result.get("duration")
    if duration:
        parts.append(f"\n⏱️ ASR 耗时: {duration:.1f}s")

    return "\n".join(parts)
