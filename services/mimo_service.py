"""
MiMo 视频理解模块：通过 Xiaomi MiMo 多模态模型原生理解视频。

支持的 API 平台：
1. 小米官方 api.xiaomimimo.com（公测期间免费）
2. OpenRouter openrouter.ai（$0.40/1M input tokens 或 免费版 flash）

使用 OpenAI-compatible Chat Completions API，视频通过 base64 编码在 video_url content part 中传输。
"""

import base64
import mimetypes
import os

import httpx

from astrbot.api import logger

# OpenClaw 伪装 headers（仅用于 OpenRouter）
OPENCLAW_HEADERS = {
    "HTTP-Referer": "https://openclaw.ai",
    "X-Title": "OpenClaw",
}

DEFAULT_VIDEO_PROMPT = """你是一位专业的视频内容分析师。请仔细观看这段视频，然后：

1. **内容概要**：用 2-3 段描述视频的主要内容、叙事逻辑和关键信息。
2. **视觉分析**：描述画面中的重要元素，包括场景变化、人物动作、文字/字幕等。
3. **语音/音效**：总结视频中的对话、旁白、背景音乐和音效。
4. **情绪与风格**：分析视频的整体风格和情绪基调。

请使用中文回答，语言自然流畅。"""


async def analyze_video_with_mimo(
    video_path: str,
    api_key: str,
    api_base: str = "https://api.xiaomimimo.com/v1",
    model: str = "mimo-v2-omni",
    prompt: str | None = None,
    timeout: float = 300.0,
) -> str:
    """
    使用 MiMo 多模态模型理解视频。

    支持多种 API 平台：
    - 小米官方: api_base="https://api.xiaomimimo.com/v1", model="mimo-v2-omni"
    - OpenRouter: api_base="https://openrouter.ai/api/v1", model="xiaomi/mimo-v2-omni"

    Args:
        video_path: 本地视频文件路径
        api_key: API key
        api_base: API base URL
        model: 模型 ID
        prompt: 自定义提示词（None 使用默认）
        timeout: 请求超时秒数

    Returns:
        模型的文本分析结果
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"视频文件不存在: {video_path}")

    file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
    logger.info(f"[mimo_video] 准备上传视频: {video_path} ({file_size_mb:.1f}MB)")

    # 读取并 base64 编码
    with open(video_path, "rb") as f:
        video_bytes = f.read()

    video_b64 = base64.b64encode(video_bytes).decode("utf-8")

    # 推断 MIME 类型
    mime_type, _ = mimetypes.guess_type(video_path)
    if not mime_type or not mime_type.startswith("video"):
        mime_type = "video/mp4"

    data_uri = f"data:{mime_type};base64,{video_b64}"

    # 构建 OpenAI-compatible request body
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": prompt or DEFAULT_VIDEO_PROMPT,
                },
                {
                    "type": "video_url",
                    "video_url": {
                        "url": data_uri,
                    },
                },
            ],
        }
    ]

    request_body = {
        "model": model,
        "messages": messages,
        "max_tokens": 4096,
    }

    # 构建 headers
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # 如果是 OpenRouter，加 OpenClaw 伪装 headers
    if "openrouter.ai" in api_base:
        headers.update(OPENCLAW_HEADERS)

    url = f"{api_base.rstrip('/')}/chat/completions"
    logger.info(f"[mimo_video] 发送请求到 {url}，模型: {model}")

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=request_body, headers=headers)

        # 打印错误详情便于调试
        if resp.status_code != 200:
            logger.error(f"[mimo_video] API 错误 {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()

        result = resp.json()

    # 提取回复文本
    choices = result.get("choices", [])
    if not choices:
        raise ValueError(f"模型未返回任何结果: {result}")

    content = choices[0].get("message", {}).get("content", "")

    # 记录 token 使用
    usage = result.get("usage", {})
    if usage:
        logger.info(
            f"[mimo_video] Token 使用: "
            f"prompt={usage.get('prompt_tokens', '?')}, "
            f"completion={usage.get('completion_tokens', '?')}, "
            f"total={usage.get('total_tokens', '?')}"
        )

    return content
