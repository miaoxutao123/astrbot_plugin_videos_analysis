"""
Gemini 多模态调用（REST 直连，不依赖已废弃的 google-generativeai SDK）

- 兼容官方 generativelanguage.googleapis.com 与 new-api / one-api 等中转站
- 小文件以 inline_data 内联发送（中转站通常不支持 File API）
- 超过内联阈值时尝试 File API（resumable upload），失败抛 GeminiError 由调用方回退
"""

import asyncio
import base64
import json
import mimetypes
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from astrbot.api import logger

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
DEFAULT_MODEL = "gemini-2.5-flash"


class GeminiError(Exception):
    pass


@dataclass
class GeminiConfig:
    api_key: str
    base_url: str = ""
    model: str = DEFAULT_MODEL
    inline_limit_mb: float = 20
    timeout: float = 300
    source: str = "plugin"
    fallback_models: list[str] = field(default_factory=list)

    @property
    def root(self) -> str:
        """规范化为不带版本号的根地址，如 https://api.example.com"""
        base = (self.base_url or DEFAULT_BASE_URL).strip().rstrip("/")
        for suffix in ("/v1beta", "/v1alpha", "/v1"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        return base

    def masked(self) -> str:
        key = self.api_key or ""
        return f"{self.root} | {self.model} | key=***{key[-4:]}" if key else f"{self.root} | {self.model} | key=<empty>"


def _guess_mime(path: Path, default_prefix: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    if not mime:
        return {"video": "video/mp4", "audio": "audio/mpeg", "image": "image/jpeg"}.get(default_prefix, "application/octet-stream")
    return mime


class GeminiClient:
    def __init__(self, cfg: GeminiConfig):
        if not cfg.api_key:
            raise GeminiError("未配置 Gemini API Key")
        self.cfg = cfg
        self.headers = {"x-goog-api-key": cfg.api_key}

    # ── 媒体 ────────────────────────────────────────────

    async def media_part(self, path: str | Path, kind: str) -> dict:
        """返回 generateContent 的一个 part；小文件内联，大文件走 File API。"""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"找不到文件: {path}")
        mime = _guess_mime(path, kind)
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb <= self.cfg.inline_limit_mb:
            data = base64.b64encode(path.read_bytes()).decode()
            return {"inline_data": {"mime_type": mime, "data": data}}
        logger.info(f"[gemini] {path.name} {size_mb:.1f}MB 超过内联阈值 {self.cfg.inline_limit_mb}MB，尝试 File API 上传")
        uri = await self.upload_file(path, mime)
        return {"file_data": {"mime_type": mime, "file_uri": uri}}

    async def upload_file(self, path: Path, mime: str) -> str:
        size = path.stat().st_size
        start_headers = {
            **self.headers,
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime,
        }
        async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:
            try:
                r = await client.post(
                    f"{self.cfg.root}/upload/v1beta/files",
                    headers=start_headers,
                    json={"file": {"display_name": path.name}},
                )
                upload_url = r.headers.get("x-goog-upload-url")
                if r.status_code != 200 or not upload_url:
                    raise GeminiError(f"File API 不可用 (HTTP {r.status_code})，中转站通常不支持，可调大 gemini_inline_limit_mb")
                with open(path, "rb") as f:
                    r = await client.post(
                        upload_url,
                        headers={"X-Goog-Upload-Command": "upload, finalize", "X-Goog-Upload-Offset": "0"},
                        content=f.read(),
                    )
                r.raise_for_status()
                file_info = r.json().get("file", {})
                name, uri = file_info.get("name"), file_info.get("uri")
                if not name or not uri:
                    raise GeminiError(f"File API 返回异常: {r.text[:200]}")
                state = file_info.get("state")
                deadline = time.monotonic() + self.cfg.timeout
                while state == "PROCESSING" and time.monotonic() < deadline:
                    await asyncio.sleep(3)
                    r = await client.get(f"{self.cfg.root}/v1beta/{name}", headers=self.headers)
                    state = r.json().get("state")
                if state != "ACTIVE":
                    raise GeminiError(f"File API 文件处理失败: state={state}")
                return uri
            except httpx.HTTPError as e:
                raise GeminiError(f"File API 上传失败: {e}") from e

    # ── 生成 ────────────────────────────────────────────

    async def generate(self, parts: list[dict]) -> tuple[str, float]:
        """依次尝试主模型和备用模型；只有 5xx / 404（模型不可用、上游故障）才换下一个。"""
        models = [self.cfg.model, *[m for m in self.cfg.fallback_models if m and m != self.cfg.model]]
        last: GeminiError | None = None
        for i, model in enumerate(models):
            try:
                return await self._generate_once(model, parts)
            except GeminiError as e:
                last = e
                retryable = getattr(e, "status", 0) >= 500 or getattr(e, "status", 0) == 404
                if not retryable or i == len(models) - 1:
                    raise
                logger.warning(f"[gemini] {model} 不可用（HTTP {e.status}），改用备用模型 {models[i + 1]}")
        raise last or GeminiError("没有可用模型")

    async def _generate_once(self, model: str, parts: list[dict]) -> tuple[str, float]:
        url = f"{self.cfg.root}/v1beta/models/{model}:generateContent"
        body = {"contents": [{"role": "user", "parts": parts}]}
        start = time.monotonic()
        async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:
            try:
                r = await client.post(url, headers=self.headers, json=body)
            except httpx.HTTPError as e:
                raise GeminiError(f"请求 Gemini 失败: {e}") from e
        duration = time.monotonic() - start
        if r.status_code != 200:
            err = GeminiError(f"Gemini HTTP {r.status_code}: {r.text[:300]}")
            err.status = r.status_code
            raise err
        data = r.json()
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "无候选结果")
            raise GeminiError(f"Gemini 未返回内容: {reason}")
        text = "".join(
            p.get("text", "")
            for p in candidates[0].get("content", {}).get("parts", [])
            if not p.get("thought")
        ).strip()
        if not text:
            raise GeminiError(f"Gemini 返回空文本 (finishReason={candidates[0].get('finishReason')})")
        return text, duration


async def send_to_gemini_async(
    cfg: GeminiConfig,
    prompt: str,
    image_paths: list[str | Path] | None = None,
    video_path: str | Path | None = None,
    audio_path: str | Path | None = None,
) -> tuple[str, float]:
    if not image_paths and video_path is None and audio_path is None:
        raise ValueError("必须至少提供一个图像、视频或音频。")
    client = GeminiClient(cfg)
    parts: list[dict] = [{"text": prompt}]
    for p in image_paths or []:
        parts.append(await client.media_part(p, "image"))
    if video_path:
        parts.append(await client.media_part(video_path, "video"))
    if audio_path:
        parts.append(await client.media_part(audio_path, "audio"))
    return await client.generate(parts)


async def test_gemini(cfg: GeminiConfig) -> tuple[bool, str]:
    """连通性自检，用于账号/配置状态面板。"""
    try:
        text, duration = await GeminiClient(cfg).generate([{"text": "只回复 ok"}])
        return True, f"{text[:20]} ({duration:.1f}s)"
    except Exception as e:
        return False, str(e)[:200]


async def process_video_with_gemini(cfg: GeminiConfig, prompt: str, video_path: str):
    """返回 (text, duration)；失败返回 (None, None)。"""
    try:
        size_mb = os.path.getsize(video_path) / (1024 * 1024)
        logger.info(f"[gemini] 分析视频 {video_path} ({size_mb:.1f}MB) via {cfg.masked()}")
        text, duration = await send_to_gemini_async(cfg, prompt, video_path=video_path)
        logger.info(f"[gemini] 视频分析完成，耗时 {duration:.1f}s")
        return text, duration
    except Exception as e:
        logger.error(f"[gemini] 处理视频失败: {e}")
    return None, None


async def process_images_with_gemini(cfg: GeminiConfig, prompt: str, image_paths: list[str]):
    try:
        logger.info(f"[gemini] 分析 {len(image_paths)} 张图片 via {cfg.masked()}")
        text, duration = await send_to_gemini_async(cfg, prompt, image_paths=image_paths)
        logger.info(f"[gemini] 图片分析完成，耗时 {duration:.1f}s")
        return text, duration
    except Exception as e:
        logger.error(f"[gemini] 处理图片失败: {e}")
    return None, None


AUDIO_PROMPT = """
你是一位专业的视频内容分析师。你的任务是分析所提供的音频，并完成以下两项工作：
1.  为整个音频内容撰写一段简洁、全面的文字描述。
2.  识别出音频中暗示着重要视觉事件发生的关键时刻（例如：突然的巨响、对话的转折点、情绪高潮等），并提供这些时刻的时间戳。

请将你的回答严格格式化为单个JSON对象，该对象包含两个键：
-   `"description"`: 一个包含音频内容描述的字符串。
-   `"timestamps"`: 一个由 "HH:MM:SS" 格式的时间戳字符串组成的数组（最多 8 个）。

只输出 JSON，不要输出其他内容。
"""


async def process_audio_with_gemini(cfg: GeminiConfig, audio_path: str):
    """返回 (description, timestamps, duration)；失败返回 (None, None, duration|None)。"""
    try:
        logger.info(f"[gemini] 分析音频 {audio_path}")
        text, duration = await send_to_gemini_async(cfg, AUDIO_PROMPT, audio_path=audio_path)
    except Exception as e:
        logger.error(f"[gemini] 处理音频失败: {e}")
        return None, None, None

    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.error(f"[gemini] 音频分析结果不是 JSON: {text[:200]}")
        return None, None, duration
    description = data.get("description", "")
    timestamps = data.get("timestamps", [])
    if not isinstance(description, str) or not isinstance(timestamps, list):
        logger.error("[gemini] 音频分析 JSON 格式不正确")
        return None, None, duration
    return description, [str(t) for t in timestamps], duration
