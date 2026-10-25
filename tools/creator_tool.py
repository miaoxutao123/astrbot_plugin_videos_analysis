"""
LLM Agent 工具：creator_upload_video — 让 Agent 申请发布视频（只提交审批，不直接发布）
"""

from ..services.creator import normalize_platform
from ..services.creator_ops import split_title_tags
from .understand_tool import _is_allowed_local_video


async def handle_creator_upload(plugin, event, video_source: str, title: str, tags: str, platform: str, account: str = "",
                                opts: dict | None = None) -> dict:
    if not event.is_admin() and not plugin.config.get("llm_allow_non_admin_requests", True):
        return {"error": "只有管理员可以发布视频，请拒绝该请求"}
    plat = normalize_platform(platform or "douyin")
    if not plat:
        return {"error": f"不支持的平台: {platform}"}
    if not video_source.startswith(("http://", "https://")) and not _is_allowed_local_video(video_source):
        return {"error": "只能发布视频直链或 AstrBot data 目录下的视频文件"}
    title, inline_tags = split_title_tags(title or "")
    tag_list = inline_tags + [t.lstrip("#") for t in (tags or "").split() if t.strip("#")]
    try:
        op = await plugin.creator_ops.prepare_upload(event, plat, video_source, title, tag_list, account or None, opts)
    except Exception as e:
        return {"error": f"准备发布失败: {e}"}
    return await plugin.creator_ops.submit(op)
