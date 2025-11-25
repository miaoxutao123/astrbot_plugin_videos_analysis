import asyncio
import json
import mimetypes
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import google.generativeai as genai
import httpx

# 注意：此脚本需要安装 "google-generativeai" 和 "Pillow" 库。
# 您可以使用以下命令安装：
# pip install google-generativeai pillow

async def send_to_gemini_async(
    api_key: str,
    prompt: str,
    image_paths: list[str | Path] = None,
    video_path: str | Path | None = None,
    audio_path: str | Path | None = None,
    model_name: str = "gemini-2.5-flash",
    reverse_proxy_url: str | None = None,
):
    """
    异步地将多模态提示（文本、图像、视频、音频）发送到Gemini API。

    Args:
        api_key (str): 您的Google AI API密钥。
        prompt (str): 要发送给模型的文本提示。
        image_paths (List[Union[str, Path]], optional): 图像文件的路径列表。默认为None。
        video_path (Optional[Union[str, Path]], optional): 视频文件的路径。默认为None。
        audio_path (Optional[Union[str, Path]], optional): 音频文件的路径。默认为None。
        model_name (str, optional): 要使用的Gemini模型名称。默认为"gemini-1.5-pro-latest"。
        reverse_proxy_url (Optional[str], optional): 反向代理的URL。
                                                     例如："generativelanguage.googleapis.com"。默认为None。

    Returns:
        tuple[str, float]: 包含模型生成的文本响应和请求持续时间的元组。

    Raises:
        FileNotFoundError: 如果提供的任何文件路径不存在。
        ValueError: 如果未提供image_paths、video_path或audio_path。
    """
    if image_paths is None and video_path is None and audio_path is None:
        raise ValueError("必须至少提供一个图像、视频或音频。")

    # 配置API客户端
    if reverse_proxy_url:
        # 解析URL以确保我们有正确的格式 (scheme://netloc)
        parsed_url = urlparse(reverse_proxy_url)
        # 重新构建不带路径的端点，例如 "http://my-proxy.com:8080"
        endpoint = f"{parsed_url.scheme}://{parsed_url.netloc}"

        # 直接将端点作为字典传递给 client_options，并强制使用 "rest" 传输
        # 这会指示客户端将所有API请求直接发送到我们的HTTP代理。
        genai.configure(
            api_key=api_key,
            transport="rest",
            client_options={"api_endpoint": endpoint}
        )
    else:
        # 不使用代理时，恢复默认配置
        genai.configure(api_key=api_key)

    # 准备内容部分
    content_parts = [prompt]

    # 处理图像
    if image_paths:
        for image_path in image_paths:
            path = Path(image_path)
            if not path.exists():
                raise FileNotFoundError(f"找不到图像文件: {path}")
            mime_type, _ = mimetypes.guess_type(path)
            if not mime_type or not mime_type.startswith("image"):
                raise ValueError(f"不支持的图像文件类型: {path}")
            content_parts.append({"mime_type": mime_type, "data": path.read_bytes()})

    # 处理视频
    if video_path:
        path = Path(video_path)
        if not path.exists():
            raise FileNotFoundError(f"找不到视频文件: {path}")

        print(f"正在上传文件: {path}...")

        # 使用 httpx 手动上传文件以控制超时
        # 注意：使用正确的上传端点 `/upload/v1beta/files`
        upload_url = f"https://generativelanguage.googleapis.com/upload/v1beta/files?key={api_key}"

        # 如果提供了反向代理，则构建代理的上传 URL
        if reverse_proxy_url:
            parsed_proxy = urlparse(reverse_proxy_url)
            base_proxy_url = f"{parsed_proxy.scheme}://{parsed_proxy.netloc}"
            # 确保代理URL也指向正确的上传路径
            upload_url = f"{base_proxy_url}/upload/v1beta/files?key={api_key}"

        video_file = None
        async with httpx.AsyncClient() as client:
            try:
                with open(path, "rb") as f:
                    file_name = os.path.basename(path)
                    mime_type, _ = mimetypes.guess_type(path)
                    if mime_type is None:
                        mime_type = "application/octet-stream"

                    # 构造一个符合 Google API 要求的 multipart/form-data 请求
                    # Part 1: JSON metadata
                    metadata = {"file": {"display_name": file_name}}
                    # Part 2: File bytes
                    files = {
                        "json": (None, json.dumps(metadata), "application/json"),
                        "file": (file_name, f, mime_type),
                    }

                    # 设置一个较长的超时时间，例如 10 分钟 (600 秒)
                    response = await client.post(upload_url, files=files, timeout=600.0)

                    response.raise_for_status()

                    # 现在响应应该是包含文件元数据的正确JSON
                    uploaded_file_data = response.json()

                    # 从 "file" 键中提取元数据
                    file_info = uploaded_file_data.get("file")
                    if not file_info or "name" not in file_info:
                        raise ValueError(f"从服务器返回的响应格式不正确: {uploaded_file_data}")

                    # 使用上传后的文件名字获取文件状态
                    video_file = genai.get_file(name=file_info["name"])

            except httpx.RequestError as e:
                raise ConnectionError(f"文件上传期间发生网络错误: {e}")
            except Exception as e:
                raise Exception(f"文件上传失败: {e}")

        # 等待文件处理完成
        while video_file.state.name == "PROCESSING":
            print(".", end="", flush=True)
            await asyncio.sleep(5)
            video_file = genai.get_file(name=video_file.name)

        if video_file.state.name == "FAILED":
            raise ValueError(f"视频文件处理失败: {video_file.name}")

        content_parts.append(video_file)

    # 处理音频
    if audio_path:
        path = Path(audio_path)
        if not path.exists():
            raise FileNotFoundError(f"找不到音频文件: {path}")
        mime_type, _ = mimetypes.guess_type(path)
        if not mime_type or not mime_type.startswith("audio"):
            raise ValueError(f"不支持的音频文件类型: {path}")
        content_parts.append({"mime_type": mime_type, "data": path.read_bytes()})

    # 创建模型并生成内容
    model = genai.GenerativeModel(model_name)
    start_time = time.monotonic()

    # 当使用反向代理 (transport="rest") 时，`generate_content_async` 可能会出现问题。
    # 一个更稳妥的方法是，在这种情况下，在线程池中运行同步的 `generate_content` 方法。
    if reverse_proxy_url:
        # 在 executor 中运行同步方法以避免阻塞事件循环
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,  # 使用默认的 ThreadPoolExecutor
            lambda: model.generate_content(content_parts, stream=False)
        )
    else:
        # 在默认情况下，使用异步方法
        response = await model.generate_content_async(content_parts, stream=False)

    end_time = time.monotonic()
    duration = end_time - start_time

    return response.text, duration


async def process_video_with_gemini(api_key: str, prompt: str, video_path: str, reverse_proxy_url: str | None = None):
    """
    使用Gemini处理单个视频和文本提示。
    """
    try:
        print(f"\n--- 正在处理视频: {video_path} ---")
        response_text, duration = await send_to_gemini_async(
            api_key=api_key,
            prompt=prompt,
            video_path=video_path,
            reverse_proxy_url=reverse_proxy_url
        )
        print(f"Gemini 响应: {response_text}")
        print(f"请求耗时: {duration:.2f} 秒")
        return response_text, duration
    except FileNotFoundError as e:
        print(f"错误: {e}。请确保视频文件存在。")
    except Exception as e:
        print(f"处理视频时发生意外错误: {e}")
    return None, None


async def process_images_with_gemini(api_key: str, prompt: str, image_paths: list[str], reverse_proxy_url: str | None = None):
    """
    使用Gemini处理多个图像和文本提示。
    """
    try:
        print(f"\n--- 正在处理图像: {', '.join(image_paths)} ---")
        response_text, duration = await send_to_gemini_async(
            api_key=api_key,
            prompt=prompt,
            image_paths=image_paths,
            reverse_proxy_url=reverse_proxy_url
        )
        print(f"Gemini 响应: {response_text}")
        print(f"请求耗时: {duration:.2f} 秒")
        return response_text, duration
    except FileNotFoundError as e:
        print(f"错误: {e}。请确保所有图像文件都存在。")
    except Exception as e:
        print(f"处理图像时发生意外错误: {e}")
    return None, None


async def process_audio_with_gemini(api_key: str, audio_path: str, reverse_proxy_url: str | None = None):
    """
    使用Gemini处理单个音频，并返回音频描述和关键时刻的时间戳。
    """
    # 更新后的Prompt，要求同时返回描述和时间戳
    prompt = """
    你是一位专业的视频内容分析师。你的任务是分析所提供的音频，并完成以下两项工作：
    1.  为整个音频内容撰写一段简洁、全面的文字描述。
    2.  识别出音频中暗示着重要视觉事件发生的关键时刻（例如：突然的巨响、对话的转折点、情绪高潮等），并提供这些时刻的时间戳。

    请将你的回答严格格式化为单个JSON对象，该对象包含两个键：
    -   `"description"`: 一个包含音频内容描述的字符串。
    -   `"timestamps"`: 一个由 "HH:MM:SS" 格式的时间戳字符串组成的数组。

    输出示例:
    {
      "description": "这段音频记录了一次激烈的辩论，讨论了关于气候变化的话题。开头气氛平静，但随后因一个争议性观点而变得紧张。结尾处，一位参与者提出了一个意想不到的解决方案。",
      "timestamps": [
        "00:01:25",
        "00:03:40",
        "00:05:15"
      ]
    }

    现在，请分析提供的音频，并按上述JSON格式返回你的分析结果。
    """
    try:
        print(f"\n--- 正在处理音频以提取描述和时间戳: {audio_path} ---")
        response_text, duration = await send_to_gemini_async(
            api_key=api_key,
            prompt=prompt,
            audio_path=audio_path,
            reverse_proxy_url=reverse_proxy_url
        )
        print(f"Gemini 响应 (原始JSON): {response_text}")
        print(f"请求耗时: {duration:.2f} 秒")

        # 尝试解析JSON
        try:
            # 清理可能的Markdown代码块标记
            cleaned_response = response_text.strip().removeprefix("```json").removesuffix("```").strip()
            data = json.loads(cleaned_response)

            description = data.get("description", "")
            timestamps = data.get("timestamps", [])

            if not isinstance(description, str) or not isinstance(timestamps, list):
                print('错误: JSON响应的格式不正确（"description"应为字符串，"timestamps"应为列表）。')
                return None, None, duration

            print(f"成功提取描述: {description}")
            print(f"成功提取时间戳: {timestamps}")
            return description, timestamps, duration

        except json.JSONDecodeError:
            print("错误: Gemini的响应不是有效的JSON格式。")
            return None, None, duration

    except FileNotFoundError as e:
        print(f"错误: {e}。请确保音频文件存在。")
    except Exception as e:
        print(f"处理音频时发生意外错误: {e}")
    return None, None, None


# async def main():
#     """
#     使用新封装函数 `process_video_with_gemini`、`process_images_with_gemini` 和 `process_audio_with_gemini` 的示例。

#     要运行此示例：
#     1. 在环境变量中设置 GOOGLE_API_KEY 或在下方提供。
#     2. 确保示例视频、图像和音频文件路径正确。
#     """
#     api_key = os.getenv("GOOGLE_API_KEY", "AxxxxxxxWs")
#     if not api_key or api_key == "YOUR_API_KEY":
#         print("请设置 GOOGLE_API_KEY 环境变量或在脚本中提供您的API密钥。")
#         return

#     # 可选：如果您需要通过代理，请设置此URL
#     proxy_url = "http://xxxx/"

#     # 注释掉视频和图像处理的示例，专注于音频
#     # # --- 示例 1: 处理视频和文本 ---
#     # video_file = "xxxxxx"
#     # video_prompt = "这个视频里面描述了什么"
#     # await process_video_with_gemini(
#     #     api_key=api_key,
#     #     prompt=video_prompt,
#     #     video_path=video_file,
#     #     reverse_proxy_url=proxy_url
#     # )

#     # # --- 示例 2: 处理图像和文本 ---
#     # image_files = [
#     #     "xxxxxx",
#     #     "xxxxxx"
#     # ]
#     # image_prompt = "这些图片里有什么？"
#     # await process_images_with_gemini(
#     #     api_key=api_key,
#     #     prompt=image_prompt,
#     #     image_paths=image_files,
#     #     reverse_proxy_url=proxy_url
#     # )

#     # --- 示例 3: 处理音频以获取描述和关键帧时间戳 ---
#     # 注意：请确保此路径指向一个实际存在的音频文件
#     audio_file = "data/astrbot_plugin_videos_analysis/test.mp3"
#     if not os.path.exists(audio_file):
#         print(f"错误：找不到示例音频文件 "{audio_file}"。请更新路径。")
#         return

#     description, timestamps, _ = await process_audio_with_gemini(
#         api_key=api_key,
#         audio_path=audio_file,
#         reverse_proxy_url=proxy_url
#     )

#     if description is not None and timestamps is not None:
#         print("\n--- 分析结果 ---")
#         print(f"音频描述: {description}")
#         print(f"关键帧时间戳: {timestamps}")

#         if timestamps:
#             print("\n这些时间戳可用于 "videos_cliper.py" 中的 "extract_frame" 函数来提取关键帧。")
#             # 这是一个如何使用时间戳的示例：
#             # from videos_cliper import extract_frame
#             # video_file_for_clipping = "path/to/your/video.mp4" # 需要与音频对应的视频文件
#             # for ts in timestamps:
#             #     frame_path = await extract_frame(video_file_for_clipping, ts)
#             #     if frame_path:
#             #         print(f"已在 {ts} 提取帧: {frame_path}")

if __name__ == "__main__":
    # 您可以在终端中执行 `python gemini_content.py` 来运行此脚本。
    # asyncio.run(main())
    pass
