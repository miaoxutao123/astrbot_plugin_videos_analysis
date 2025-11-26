import asyncio
import os


async def run_ffmpeg_command(command: list[str]):
    """
    异步执行一个 FFmpeg 命令。

    :param command: 要执行的命令列表。
    :return: 命令是否成功执行。
    """
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        print(f"错误：ffmpeg 执行失败，错误信息: {stderr.decode()}")
        return False
    return True

async def separate_audio_video(video_path: str):
    """
    从视频文件中异步分离音频和视频。

    :param video_path: 输入视频文件的路径。
    :return: 一个包含音频和视频文件路径的元组，如果出错则返回 None。
    """
    if not os.path.exists(video_path):
        print(f"错误：找不到视频文件 {video_path}")
        return None

    base, ext = os.path.splitext(video_path)
    audio_path = f"{base}_audio.mp3"
    video_only_path = f"{base}_video.mp4"

    # 分离音频的命令
    audio_command = [
        "ffmpeg", "-i", video_path, "-vn", "-acodec", "mp3", "-y", audio_path
    ]
    # 分离视频的命令
    video_command = [
        "ffmpeg", "-i", video_path, "-an", "-vcodec", "copy", "-y", video_only_path
    ]

    audio_success = await run_ffmpeg_command(audio_command)
    video_success = await run_ffmpeg_command(video_command)

    if audio_success and video_success:
        return audio_path, video_only_path
    else:
        return None

async def extract_frame(video_path: str, time_point: str):
    """
    从视频文件的指定时间点异步提取一帧图像。

    :param video_path: 输入视频文件的路径。
    :param time_point: "HH:MM:SS" 格式的时间点。
    :return: 提取出的帧图像的路径，如果出错则返回 None。
    """
    if not os.path.exists(video_path):
        print(f"错误：找不到视频文件 {video_path}")
        return None

    base, ext = os.path.splitext(video_path)
    frame_path = f"{base}_frame_at_{time_point.replace(':', '-')}.png"

    # 提取帧的命令
    command = [
        "ffmpeg", "-i", video_path, "-ss", time_point, "-vframes", "1", "-y", frame_path
    ]

    success = await run_ffmpeg_command(command)

    if success:
        return frame_path
    else:
        return None


async def extract_frames_by_interval(video_path: str, interval: int):
    """
    从视频文件中按固定时间间隔异步提取帧图像。

    :param video_path: 输入视频文件的路径。
    :param interval: 提取帧的时间间隔（秒）。
    :return: 包含提取出的所有帧图像路径的列表，如果出错则返回 None。
    """
    if not os.path.exists(video_path):
        print(f"错误：找不到视频文件 {video_path}")
        return None

    base, ext = os.path.splitext(video_path)
    output_dir = f"{base}_frames_interval_{interval}s"
    os.makedirs(output_dir, exist_ok=True)

    frame_pattern = os.path.join(output_dir, "frame_%04d.png")

    # 使用 fps 过滤器提取帧的命令
    # fps=1/interval 表示每 interval 秒提取一帧
    command = [
        "ffmpeg", "-i", video_path, "-vf", f"fps=1/{interval}", "-y", frame_pattern
    ]

    success = await run_ffmpeg_command(command)

    if success:
        # 获取生成的文件列表
        extracted_frames = sorted([
            os.path.join(output_dir, f)
            for f in os.listdir(output_dir)
            if f.startswith("frame_") and f.endswith(".png")
        ])
        return extracted_frames
    else:
        return None


if __name__ == "__main__":
    # 示例用法
    video_path = "D:/code/python/astrbot/AstrBot/data/astrbot_plugin_videos_analysis/test.mp4"
    asyncio.run(separate_audio_video(video_path))
    asyncio.run(extract_frame(video_path, "00:00:10"))
    asyncio.run(extract_frames_by_interval(video_path, 3))
