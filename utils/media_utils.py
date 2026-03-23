"""
媒体处理工具：FFmpeg 操作封装（音视频分离、抽帧）
"""

import asyncio
import os

from astrbot.api import logger


async def run_ffmpeg_command(command: list[str]) -> bool:
    """异步执行 FFmpeg 命令"""
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        logger.error(f"ffmpeg 执行失败: {stderr.decode()}")
        return False
    return True


async def separate_audio_video(video_path: str) -> tuple[str, str] | None:
    """从视频文件中分离音频和视频，返回 (audio_path, video_only_path)"""
    if not os.path.exists(video_path):
        logger.error(f"找不到视频文件: {video_path}")
        return None

    base, _ = os.path.splitext(video_path)
    audio_path = f"{base}_audio.mp3"
    video_only_path = f"{base}_video.mp4"

    audio_command = [
        "ffmpeg", "-i", video_path, "-vn", "-acodec", "mp3", "-y", audio_path
    ]
    video_command = [
        "ffmpeg", "-i", video_path, "-an", "-vcodec", "copy", "-y", video_only_path
    ]

    audio_success, video_success = await asyncio.gather(
        run_ffmpeg_command(audio_command),
        run_ffmpeg_command(video_command),
    )

    if audio_success and video_success:
        return audio_path, video_only_path
    return None


async def extract_frame(video_path: str, time_point: str) -> str | None:
    """从视频指定时间点提取一帧图像"""
    if not os.path.exists(video_path):
        logger.error(f"找不到视频文件: {video_path}")
        return None

    base, _ = os.path.splitext(video_path)
    frame_path = f"{base}_frame_at_{time_point.replace(':', '-')}.png"

    command = [
        "ffmpeg", "-i", video_path, "-ss", time_point, "-vframes", "1", "-y", frame_path
    ]

    success = await run_ffmpeg_command(command)
    return frame_path if success else None


async def extract_frames_by_interval(video_path: str, interval: int) -> list[str] | None:
    """从视频中按固定时间间隔提取帧图像"""
    if not os.path.exists(video_path):
        logger.error(f"找不到视频文件: {video_path}")
        return None

    base, _ = os.path.splitext(video_path)
    output_dir = f"{base}_frames_interval_{interval}s"
    os.makedirs(output_dir, exist_ok=True)

    frame_pattern = os.path.join(output_dir, "frame_%04d.png")

    command = [
        "ffmpeg", "-i", video_path, "-vf", f"fps=1/{interval}", "-y", frame_pattern
    ]

    success = await run_ffmpeg_command(command)
    if success:
        return sorted([
            os.path.join(output_dir, f)
            for f in os.listdir(output_dir)
            if f.startswith("frame_") and f.endswith(".png")
        ])
    return None
